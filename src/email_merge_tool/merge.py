"""The mail-merge workflow: validate everything, then (optionally) send."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.errors import MessageError
from email.message import EmailMessage
from pathlib import Path

from .data import (
    DataError,
    Issue,
    _valid_address_list,
    is_valid_address,
    load_address_list,
    load_recipients,
    normalize_address,
    validate_recipients,
)
from .experiment import Assignment, assign_variants, write_assignments
from .message import attachment_name, build_message, stamp_date, wire_bytes
from .sendlog import SendLog, bounced_addresses, sent_addresses
from .templates import ADDRESS_HEADERS, Template, TemplateError, fill, header_name, load_template, placeholders
from .transport import (
    DryRunTransport,
    FatalSendError,
    PermanentSendError,
    Transport,
    TransportError,
    UncertainSendError,
    _bounced,
    _describe_refused,
    _enhanced_status,
    _mailbox_problem,
    _text,
)

__all__ = ["mail_merge", "MergeReport", "Result"]

logger = logging.getLogger(__name__)

TemplateLike = Template | str | Path

#: Messages larger than this get a warning: many receiving servers refuse them.
LARGE_MESSAGE = 10_000_000


@dataclass
class Result:
    """Outcome for one recipient.

    Attributes:
        to: The recipient's address, as in the data.
        status: ``"sent"``, ``"dry_run"``, ``"failed"``, ``"skipped"``,
            ``"bounced"`` (the server refused the mailbox for good: an unknown
            user or a disabled mailbox; resume skips it), or ``"uncertain"``
            (the connection dropped after the message data went out, so the
            server may have accepted it; it is not retried, and resume skips it).
        variant: The template this recipient was assigned, with several templates.
        message_id: The message's ``Message-ID``.
        detail: Why a message failed, bounced or was skipped, or which other
            recipients were refused.
        smtp_code: The server's reply code behind a failure or bounce, such as 550.
        enhanced_status: The reply's enhanced status code (RFC 3463), such
            as ``"5.1.1"``, when the server gave one.
    """

    to: str
    status: str
    variant: str | None = None
    message_id: str | None = None
    detail: str = ""
    smtp_code: int | None = None
    enhanced_status: str | None = None


@dataclass
class MergeReport:
    """Summary of a run.

    Attributes:
        results: One :class:`Result` per recipient considered.
        issues: Validation warnings (errors abort the run before sending).
        messages: The built messages in a dry run, for inspection.
        assignments: Experimental assignments, if variants were used.
    """

    results: list[Result] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    messages: list[EmailMessage] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(r.status == status for r in self.results)

    @property
    def sent(self) -> int:
        return self.count("sent")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def skipped(self) -> int:
        return self.count("skipped")

    @property
    def uncertain(self) -> int:
        return self.count("uncertain")

    @property
    def bounced(self) -> int:
        return self.count("bounced")

    def summary(self) -> str:
        statuses = ("sent", "dry_run", "failed", "bounced", "uncertain", "skipped")
        parts = [f"{s}: {self.count(s)}" for s in statuses if self.count(s)]
        return ", ".join(parts) or "nothing to do"


def _as_templates(templates: TemplateLike | Sequence[TemplateLike] | Mapping[str, TemplateLike]) -> dict[str, Template]:
    items: list[tuple[str | None, TemplateLike]]
    if isinstance(templates, Mapping):
        items = [(name, t) for name, t in templates.items()]
    elif isinstance(templates, (Template, str, Path)):
        items = [(None, templates)]
    else:
        items = [(None, t) for t in templates]
    result: dict[str, Template] = {}
    for name, item in items:
        tpl = item if isinstance(item, Template) else load_template(item)
        if name is not None and tpl.name != name:
            tpl = Template(tpl.subject, tpl.text, tpl.html, tpl.headers, name=name)
        if tpl.name in result:
            raise TemplateError(f"two templates are named {tpl.name!r}; give them distinct names")
        result[tpl.name] = tpl
    if not result:
        raise TemplateError("no templates given")
    return result


def mail_merge(
    templates: TemplateLike | Sequence[TemplateLike] | Mapping[str, TemplateLike],
    recipients: object,
    *,
    sender: str | None = None,
    transport: Transport | None = None,
    dry_run: bool = True,
    to_field: str = "email",
    seed: int | str | None = None,
    strata: str | Sequence[str] | None = None,
    weights: Sequence[float] | None = None,
    variant_field: str | None = None,
    assignments_path: str | Path | None = None,
    exclude: object = None,
    log_path: str | Path | None = None,
    resume: bool = False,
    retry_bounced: bool = False,
    limit: int | None = None,
    min_interval: float = 0.0,
    list_unsubscribe: str | None = None,
    one_click_unsubscribe: bool = False,
    extra_headers: Mapping[str, str] | None = None,
    allow_duplicates: bool = False,
    attachments: Sequence[str | Path] = (),
    attachment_field: str | None = None,
    max_message_size: int | None = 25_000_000,
    output_dir: str | Path | None = None,
    progress: Callable[[Result], None] | None = None,
    max_consecutive_failures: int = 5,
) -> MergeReport:
    """Validate recipients against the template(s) and send one message each.

    **Nothing is sent unless** ``dry_run=False`` **and a transport is given.**
    Every recipient is validated before the first message goes out, so a bad
    row 900 cannot leave you having emailed rows 1-899 and not knowing what to
    do next.

    Args:
        templates: A :class:`Template`, a template file path, or several of
            them (list or ``{name: template}``). With more than one template
            each recipient receives exactly one, chosen by random assignment
            (``seed`` required) or by ``variant_field``.
        recipients: CSV/JSON path, list of dicts, or pandas DataFrame.
        sender: From address (``"Name <addr>"``). Required unless every
            template sets ``From:``.
        transport: e.g. ``SMTPTransport(SMTPConfig.from_service("gmail", user, pw))``,
            or your own; see :class:`Transport`. It is used only with
            ``dry_run=False``, and a warning says so otherwise. A transport
            whose ``delivers`` attribute is false, such as
            :class:`DryRunTransport`, makes the run a dry run either way.
        dry_run: Build and validate all messages without sending (default).
        to_field: Column holding each recipient's address.
        seed: Random seed for assigning variants. Record it with your results.
        strata: Column(s) to stratify (block) the random assignment on.
        weights: Relative allocation to each template, e.g. ``[1, 1]``.
        variant_field: Column naming the template for each row, if you did the
            assignment yourself (mutually exclusive with ``seed``).
        assignments_path: Write the assignment table (CSV) here.
        exclude: Addresses to skip (e.g. people who already responded): a
            ``.txt``/``.csv``/``.json`` file, a DataFrame, or a list of
            addresses, records or file paths. Every entry must be a valid
            address; see :func:`load_address_list`.
        log_path: JSON Lines send log (audit trail; required for ``resume``).
            If the file already records sent messages, ``resume=True`` is
            required, so running the same command twice cannot email
            everyone twice. Sending without a log adds a warning to the
            report, since a run that stops part-way cannot be resumed.
        resume: Skip recipients the log already records as ``sent`` (or
            ``uncertain``), and addresses it records as ``bounced``.
        retry_bounced: With ``resume``, send again to addresses the log
            records as ``bounced`` (for example after fixing a mail server).
        limit: Send at most this many messages this run (e.g. a daily quota).
        min_interval: Seconds to wait between send attempts, failed ones
            included (rate limiting).
        list_unsubscribe: ``List-Unsubscribe`` header value; may contain
            placeholders, e.g. ``"<https://example.org/unsub?id={id}>"``.
        one_click_unsubscribe: Add the RFC 8058 one-click header too.
        extra_headers: More headers for every message, e.g.
            ``{"Feedback-ID": "{campaign}:lab", "X-Campaign": "spring"}``;
            values may contain placeholders. The names a template may set
            are allowed (see :func:`header_name`), except ``Subject`` and
            ``From``. A header may not be set both here and in a template.
        allow_duplicates: Permit the same address on more than one row.
        attachments: Files attached to every message.
        attachment_field: Column with per-recipient file path(s), separated
            by ``;`` (e.g. a personalised certificate). Paths may contain
            placeholders. Relative paths are resolved from the working directory.
        max_message_size: The largest message allowed, in bytes, measured as
            sent (attachments grow by about a third when encoded). A larger
            message fails validation for its row, naming its attachments.
            The default, 25 MB, is a common provider limit; ``None`` or ``0``
            turns the check off. Many receiving servers set lower limits, so
            messages over 10 MB get a warning. Once connected, the server's
            own SIZE limit (from EHLO) is checked too, before the first send.
        output_dir: In a dry run, also write each message as a ``.eml`` file
            here (or pass ``transport=DryRunTransport(output_dir)``, not both).
        progress: Called with each :class:`Result` as it is produced.
        max_consecutive_failures: Stop the run with :class:`FatalSendError`
            after this many failed messages in a row (default 5; ``0`` never
            stops). Failures that name a bad address or mailbox (enhanced
            status codes X.1.x and X.2.x, such as 5.1.1 for an unknown user)
            do not count, and a sent message starts the count again. Many
            failures in a row usually mean a problem with the account or
            the server, which every remaining message would meet too.

    Returns:
        A :class:`MergeReport`.

    Raises:
        DataError: validation found errors (see ``exc.issues``), or the send
            log already records sent messages and ``resume`` is false;
            nothing was sent.
        FatalSendError: the connection or login failed, reconnecting
            failed through every retry, the server refused the account
            (:class:`AccountRefusedError`, e.g. a daily quota), or
            ``max_consecutive_failures`` messages failed in a row. The message
            being sent when that happened is logged as ``failed``. The
            exception's ``report`` attribute holds the :class:`MergeReport`
            up to the stop, so the results so far are known with or without
            a log. Re-run with ``resume=True`` to continue.
        TransportError: the transport raised another exception (the
            original is ``__cause__``). It is a :class:`FatalSendError` with
            ``report`` too; the message being sent is logged ``uncertain``.
    """
    tpls = _as_templates(templates)
    records = load_recipients(recipients)
    if resume and log_path is None:
        raise ValueError("resume=True needs log_path")
    if retry_bounced and not resume:
        raise ValueError("retry_bounced=True needs resume=True: only a resumed run skips bounced addresses")
    if not dry_run and transport is None:
        raise ValueError("dry_run=False needs a transport (e.g. SMTPTransport)")
    previewer = transport is not None and not getattr(transport, "delivers", True)
    if previewer and output_dir is not None:
        raise ValueError("pass output_dir= or a DryRunTransport, not both: each says where the dry run writes")
    dry_run = dry_run or previewer
    if seed is not None and variant_field is not None:
        raise ValueError("use either seed (random assignment) or variant_field (pre-assigned), not both")
    if len(tpls) > 1 and seed is None and variant_field is None:
        raise ValueError("several templates given: pass seed= for random assignment, or variant_field=")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    if max_consecutive_failures < 0:
        raise ValueError("max_consecutive_failures must be >= 0 (0 turns the check off)")
    if max_message_size is not None and max_message_size < 0:
        raise ValueError("max_message_size must be >= 0 (0 or None turns the check off)")

    extra = _extra_headers(extra_headers or {}, tpls)
    fields: set[str] = set().union(*(t.fields for t in tpls.values()), *(placeholders(v) for v in extra.values()))
    if list_unsubscribe:
        fields |= placeholders(list_unsubscribe)
    if strata:
        fields |= {strata} if isinstance(strata, str) else set(strata)
    if variant_field:
        fields.add(variant_field)
    if attachment_field:
        fields.add(attachment_field)
    issues = validate_recipients(records, fields, to_field=to_field, allow_duplicates=allow_duplicates)
    keys = {normalize_address(r.get(to_field) or "") for r in records}
    skip: set[str] = set()
    if exclude is not None:
        try:
            skip = load_address_list(exclude, to_field=to_field)
        except DataError as exc:
            issues.insert(0, Issue(None, "error", str(exc).replace("\n", "\n    "), where="exclude list"))
        else:
            if not skip:
                issues.append(
                    Issue(None, "warning", "it holds no addresses, so nobody is excluded", where="exclude list")
                )
            elif not skip & keys:
                note = f"none of its {len(skip)} address(es) is in the recipient data"
                issues.append(Issue(None, "warning", note, where="exclude list"))
    already: set[str] = set()
    bounced: set[str] = set()  # resume skips these too, unless retry_bounced
    if log_path is not None:
        already, log_issue = _check_send_log(Path(log_path), resume, keys)
        if log_issue is not None:
            issues.insert(0, log_issue)
        if resume and not retry_bounced:
            bounced = bounced_addresses(log_path)
    if sender and not is_valid_address(sender):
        issues.insert(0, Issue(None, "error", f"invalid sender address {sender!r}"))
    if dry_run and transport is not None and not previewer:
        issues.append(
            Issue(
                None,
                "warning",
                f"{type(transport).__name__} is not used: this is a dry run (dry_run=True is the default), "
                "so nothing is sent; pass dry_run=False to send",
                where="transport",
            )
        )
    if not dry_run and log_path is None:
        issues.append(
            Issue(
                None,
                "warning",
                "sending without a log: a run that stops part-way cannot be resumed, and only the "
                "report (exc.report on an error) says who was sent the message; pass log_path=",
                where="send log",
            )
        )
    if not sender and any("From" not in t.headers for t in tpls.values()):
        issues.insert(0, Issue(None, "error", "no sender: pass sender= or add a 'From:' header to the template"))
    for tpl in tpls.values():
        if sender and tpl.headers.get("From", sender) != sender:
            note = f"its From: {tpl.headers['From']!r} replaces sender={sender!r}"
            issues.append(Issue(None, "warning", note, where=f"template {tpl.name!r}"))
    for path in attachments:
        if not Path(path).is_file():
            issues.append(Issue(None, "error", f"attachment not found: {path}"))
    shared_names = [attachment_name(p) for p in attachments]
    clash = _repeated_name(shared_names, 0)
    if clash:
        issues.append(Issue(None, "warning", _clash_note(clash), where="attachments"))
    if variant_field and not any(i.severity == "error" for i in issues):
        for row, record in enumerate(records, start=1):
            if record[variant_field] not in tpls:
                issues.append(
                    Issue(row, "error", f"{variant_field}={record[variant_field]!r} is not one of {sorted(tpls)}")
                )

    # Render every message now, so template problems surface before sending.
    report = MergeReport(issues=[i for i in issues if i.severity == "warning"])
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise DataError(_describe(errors), errors)
    for issue in report.issues:  # so a caller of the API sees them before anything is sent
        logger.warning("%s", issue)

    if len(tpls) > 1 and seed is not None:
        report.assignments = assign_variants(
            records, list(tpls), seed=seed, to_field=to_field, strata=strata, weights=weights
        )
        variants = [a.variant for a in report.assignments]
        if assignments_path is not None:
            write_assignments(assignments_path, report.assignments, seed=seed, strata=strata)
    elif variant_field:
        variants = [r[variant_field] for r in records]
    else:
        variants = [next(iter(tpls))] * len(records)

    built: list[tuple[str, str, EmailMessage, int]] = []  # To, variant, message, size as sent
    large: list[tuple[int, int]] = []  # (row, size) of messages over LARGE_MESSAGE
    row_warnings: list[Issue] = []
    for row, (record, variant) in enumerate(zip(records, variants, strict=True), start=1):
        try:
            rendered = tpls[variant].render(record, to_field=to_field)
            if extra:
                filled = {name: fill(value, record) for name, value in extra.items()}
                for name, value in filled.items():
                    if "\r" in value or "\n" in value:
                        raise TemplateError(f"{name} header would contain a line break (possible header injection)")
                rendered = dataclasses.replace(rendered, headers={**rendered.headers, **filled})
            for name in ADDRESS_HEADERS:
                value = rendered.headers.get(name, "")
                if value and not _valid_address_list(value):
                    raise TemplateError(f"invalid address in {name}: {value!r}")
            unsub = fill(list_unsubscribe, record) if list_unsubscribe else None
            files = [Path(p) for p in attachments]
            if attachment_field:
                files += [Path(fill(p.strip(), record)) for p in record[attachment_field].split(";") if p.strip()]
            missing = [str(p) for p in files if not p.is_file()]
            if missing:
                raise TemplateError(f"attachment not found: {', '.join(missing)}")
            clash = _repeated_name(
                shared_names + [attachment_name(p) for p in files[len(attachments) :]], len(attachments)
            )
            if clash:
                row_warnings.append(Issue(row, "warning", _clash_note(clash)))
            message = build_message(
                rendered,
                sender=sender or "",
                list_unsubscribe=unsub,
                one_click_unsubscribe=one_click_unsubscribe,
                attachments=files,
            )
            size = _check_writable(message)
            if max_message_size and size > max_message_size:
                raise TemplateError(_too_large(size, max_message_size, files))
            if size > LARGE_MESSAGE:
                large.append((row, size))
        except MessageError as exc:
            errors.append(Issue(row, "error", f"the message cannot be written: {exc}"))
            continue
        except (TemplateError, ValueError, OSError) as exc:
            errors.append(Issue(row, "error", str(exc)))
            continue
        built.append((rendered.to, variant, message, size))
    if errors:
        raise DataError(_describe(errors), errors)
    if large:
        row, size = max(large, key=lambda item: item[1])
        note = (
            f"{len(large)} message(s) are over {_mb(LARGE_MESSAGE)} (the largest, row {row}, is {_mb(size)}); "
            "many receiving servers refuse messages that size, so consider linking to large files"
        )
        row_warnings.insert(0, Issue(None, "warning", note, where="message size"))
    for issue in row_warnings:
        logger.warning("%s", issue)
    report.issues += row_warnings

    if not resume:
        already = set()
        bounced = set()

    log: SendLog | None = None
    if log_path is not None and not dry_run:
        # Hold the log for the whole run, and read it again under the lock:
        # another run may have sent and logged since validation.
        log = SendLog(log_path)
        try:
            log.lock()
            current = sent_addresses(log_path)
        except BaseException:
            log.close()
            raise
        if current and not resume:
            log.close()
            issue = _reuse_issue(Path(log_path), current)
            raise DataError(_describe([issue]), [issue])
        if resume:
            already = current
            bounced = set() if retry_bounced else bounced_addresses(log_path)

    active: Transport = DryRunTransport(output_dir)
    if transport is not None and (previewer or not dry_run):
        active = transport
    delivered = attempted = 0
    consecutive = 0  # failed messages in a row, not counting bad addresses
    finished = False
    try:
        _open(active)
        done = skip | already | bounced
        _check_size_limit(
            active, [(row, b[3]) for row, b in enumerate(built, 1) if normalize_address(b[0]) not in done]
        )
        for to, variant, message, _size in built:
            key = normalize_address(to)
            variant_label = variant if len(tpls) > 1 else None
            stop: FatalSendError | None = None
            if key in skip or key in already or key in bounced:
                if key in skip:
                    reason = "excluded"
                elif key in already:
                    reason = "already in the send log"
                else:
                    reason = "bounced in an earlier run (retry_bounced or --retry-bounced sends to it again)"
                result = Result(to, "skipped", variant_label, detail=reason)
            elif limit is not None and delivered >= limit:
                result = Result(to, "skipped", variant_label, detail="limit reached")
            else:
                if attempted and min_interval and not dry_run:
                    time.sleep(min_interval)
                attempted += 1
                counts = False
                try:
                    result, counts = _deliver(active, message, to, variant_label, dry_run)
                except FatalSendError as exc:  # e.g. a quota or a failed reconnect: this one was not sent
                    # An unexpected exception from a transport says nothing about whether it was sent.
                    status = "uncertain" if isinstance(exc, TransportError) else "failed"
                    code, text = _error_reply(exc)
                    result = Result(
                        to, status, variant_label, str(message["Message-ID"]), str(exc), code, _enhanced_status(text)
                    )
                    stop = exc
                except BaseException:
                    if log is not None and getattr(active, "unconfirmed", False):  # e.g. Ctrl-C awaiting the reply
                        detail = "interrupted after the message was sent; it may have arrived"
                        _write(log, Result(to, "uncertain", variant_label, str(message["Message-ID"]), detail), seed)
                    raise
                if result.status in {"sent", "dry_run", "uncertain"}:  # an uncertain one most likely went out
                    delivered += 1
                if dry_run:
                    report.messages.append(message)
                if result.status == "sent":
                    consecutive = 0
                elif counts:
                    consecutive += 1
                    if max_consecutive_failures and consecutive >= max_consecutive_failures:
                        stop = FatalSendError(
                            f"stopped after {consecutive} failed messages in a row; the last: {result.detail}. "
                            "Check the account and the server, then resume to continue (failed messages are "
                            "tried again). max_consecutive_failures (--max-failures) sets the number; 0 turns "
                            "this check off."
                        )
            report.results.append(result)
            if log is not None:
                _write(log, result, seed)
            if progress is not None:
                progress(result)
            if stop is not None:
                raise stop
        finished = True
    except FatalSendError as exc:
        exc.report = report
        raise
    finally:
        try:
            _close(active, report, finished)
        finally:
            if log is not None:
                log.close()
    return report


def _error_reply(exc: BaseException) -> tuple[int | None, str]:
    """The ``smtp_code`` and ``smtp_error`` of a send error, if it has them."""
    code = getattr(exc, "smtp_code", None)
    return (code if isinstance(code, int) else None), _text(getattr(exc, "smtp_error", None) or "")


def _write(log: SendLog, result: Result, seed: int | str | None) -> None:
    log.write(
        to=result.to,
        status=result.status,
        variant=result.variant,
        message_id=result.message_id,
        detail=result.detail,
        smtp_code=result.smtp_code,
        enhanced_status=result.enhanced_status,
        seed=seed,
    )


def _open(transport: Transport) -> None:
    try:
        transport.open()
    except FatalSendError:
        raise
    except Exception as exc:
        raise TransportError(f"the transport could not open: {_exception_text(exc)}") from exc


def _close(transport: Transport, report: MergeReport, finished: bool) -> None:
    """Close the transport: a failure is an error after a complete run, and a warning while another error stops it."""
    try:
        transport.close()
    except Exception as exc:
        if not finished:
            logger.warning("closing the transport failed: %s", _exception_text(exc))
            return
        error = TransportError(f"closing the transport failed after the run: {_exception_text(exc)}")
        error.report = report
        raise error from exc


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _check_send_log(path: Path, resume: bool, keys: set[str]) -> tuple[set[str], Issue | None]:
    """Read the send log before a run, and say what is wrong with using it.

    Returns the addresses the log records as sent or uncertain, and an issue:
    an error when reusing the log could email people twice (or when a resume
    would skip nobody because the log is missing or belongs to other people),
    or a warning when a resume finds nothing to skip.
    """
    logged = sent_addresses(path)
    if not resume:
        return logged, _reuse_issue(path, logged) if logged else None
    if not path.exists():
        return logged, Issue(
            None,
            "error",
            f"{path} does not exist, so resuming would skip nobody; check the file name "
            "(leave out --resume for the first run of a mailing)",
            where="send log",
        )
    if not logged:
        return logged, Issue(None, "warning", f"{path} records no sent messages yet", where="send log")
    if not logged & keys:
        return logged, Issue(
            None,
            "error",
            f"{path} records {len(logged)} address(es), and none of them is in this recipient data; "
            "check that it is the log of this mailing",
            where="send log",
        )
    return logged, None


def _reuse_issue(path: Path, logged: set[str]) -> Issue:
    return Issue(
        None,
        "error",
        f"{path} already records {len(logged)} sent or uncertain address(es); resume to skip them "
        "(--resume, or resume=True), or use a new log file for a new mailing",
        where="send log",
    )


def _extra_headers(headers: Mapping[str, str], tpls: Mapping[str, Template]) -> dict[str, str]:
    """Check ``extra_headers`` and return it with the usual header spelling.

    Raises:
        TemplateError: a name a template may not set, ``Subject`` or ``From``,
            the same header twice, or a header a template also sets.
    """
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = header_name(key)
        if name in ("Subject", "From"):
            where = "the template" if name == "Subject" else "the template or sender="
            raise TemplateError(f"set {name} in {where}, not in extra_headers")
        if name.lower() in {n.lower() for n in result}:
            raise TemplateError(f"extra_headers sets {name} twice")
        for tpl in tpls.values():
            if name.lower() in {n.lower() for n in tpl.headers}:
                raise TemplateError(f"{name} is set both in extra_headers and in template {tpl.name!r}")
        result[name] = value
    return result


def _repeated_name(names: Sequence[str], start: int) -> str | None:
    """The first of ``names[start:]`` that appears twice, ignoring case, or None."""
    folded = [n.casefold() for n in names]
    return next((names[i] for i in range(start, len(names)) if folded.count(folded[i]) > 1), None)


def _clash_note(name: str) -> str:
    return f"two attachments are named {name!r}; mail clients rename one of them or save one over the other"


def _check_writable(message: EmailMessage) -> int:
    """Serialize ``message`` as delivery will, so a header that cannot be written stops the run before any send.

    Returns:
        The size of the message as sent, in bytes.

    Raises:
        MessageError: for any failure, with the standard library's reason.
    """
    try:
        return len(wire_bytes(message))
    except MessageError:
        raise
    except Exception as exc:  # noqa: BLE001 - UnicodeEncodeError, RecursionError and others on odd input
        raise MessageError(str(exc) or type(exc).__name__) from exc


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 100_000 else f"{size:,} bytes"


def _too_large(size: int, limit: int, files: Sequence[Path]) -> str:
    """Why a message is too large for ``limit``, naming its attachments, largest first."""
    sizes = sorted(((p.stat().st_size, p.name) for p in files), reverse=True)
    listed = ", ".join(f"{name} ({_mb(n)})" for n, name in sizes) or "none"
    return (
        f"the message is {_mb(size)} as sent, over the {_mb(limit)} limit (max_message_size, --max-size); "
        f"attachments: {listed}. Encoding makes attachments about a third larger; link to large files instead"
    )


def _check_size_limit(transport: Transport, sizes: Sequence[tuple[int, int]]) -> None:
    """Stop before the first send if a message is larger than the transport's ``size_limit``.

    ``sizes`` holds (row, size) for the messages this run would send.
    """
    limit = getattr(transport, "size_limit", None)
    if not isinstance(limit, int) or limit <= 0:
        return
    over = [(row, size) for row, size in sizes if size > limit]
    if over:
        rows = ", ".join(f"row {row} ({_mb(size)})" for row, size in over[:10])
        more = f" and {len(over) - 10} more" if len(over) > 10 else ""
        raise FatalSendError(
            f"the server accepts messages up to {_mb(limit)} (its SIZE limit), and {len(over)} message(s) are "
            f"larger: {rows}{more}. Nothing was sent; make the attachments smaller or link to the files instead."
        )


def _deliver(
    transport: Transport, message: EmailMessage, to: str, variant: str | None, dry_run: bool
) -> tuple[Result, bool]:
    """Send one message. Also says whether a failure counts toward ``max_consecutive_failures``."""
    message_id = str(message["Message-ID"])
    stamp_date(message)  # the send time, not the build time, which can be hours earlier
    try:
        returned = transport.send(message)
    except UncertainSendError as exc:
        logger.warning("delivery to %s is uncertain: %s", to, exc)
        code, text = _error_reply(exc)
        return Result(to, "uncertain", variant, message_id, str(exc), code, _enhanced_status(text)), False
    except PermanentSendError as exc:
        code, text = _error_reply(exc)
        status = "bounced" if _bounced(code, text) else "failed"
        logger.warning("%s: %s: %s", status, to, exc)
        result = Result(to, status, variant, message_id, str(exc), code, _enhanced_status(text))
        return result, not _mailbox_problem(text)
    except FatalSendError:
        raise
    except Exception as exc:  # outside the transport contract: stop, and keep the report
        raise TransportError(
            f"the transport raised {_exception_text(exc)}; the message may or may not have been sent"
        ) from exc
    refused = _refusals(returned)
    key = normalize_address(to)
    mine = {address: reply for address, reply in refused.items() if normalize_address(address) == key}
    if mine:  # a Cc or Bcc took it, but the person this row is for did not
        detail = f"recipient refused: {_describe_refused(mine)}; the other recipients were sent the message"
        code, text = next(iter(mine.values()))
        status = "bounced" if _bounced(code, text) else "failed"
        logger.warning("%s: %s: %s", status, to, detail)
        return Result(to, status, variant, message_id, detail, code, _enhanced_status(text)), not _mailbox_problem(text)
    detail = f"some recipients refused: {_describe_refused(refused)}" if refused else ""
    return Result(to, "dry_run" if dry_run else "sent", variant, message_id, detail), False


def _refusals(returned: object) -> dict[str, tuple[int, str]]:
    """The refused recipients in what ``send`` returned: the ``{address: (code, reply)}`` entries.

    Other values, such as a provider's response, and entries of another
    shape say nothing about refusals.
    """
    if not isinstance(returned, Mapping):
        return {}
    return {
        address: (reply[0], _text(reply[1]))
        for address, reply in returned.items()
        if isinstance(address, str)
        and isinstance(reply, tuple)
        and len(reply) == 2
        and isinstance(reply[0], int)
        and isinstance(reply[1], (bytes, str))
    }


def _describe(errors: Sequence[Issue], shown: int = 20) -> str:
    lines = [str(i) for i in errors[:shown]]
    if len(errors) > shown:
        lines.append(f"... and {len(errors) - shown} more")
    return f"{len(errors)} problem(s) found; nothing was sent:\n  " + "\n  ".join(lines)
