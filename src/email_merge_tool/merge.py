"""The mail-merge workflow: validate everything, then (optionally) send."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path

from .data import (
    DataError,
    Issue,
    is_valid_address,
    load_address_list,
    load_recipients,
    normalize_address,
    validate_recipients,
)
from .experiment import Assignment, assign_variants, write_assignments
from .message import build_message
from .sendlog import SendLog, sent_addresses
from .templates import Template, TemplateError, fill, load_template, placeholders
from .transport import DryRunTransport, FatalSendError, PermanentSendError, Transport

__all__ = ["mail_merge", "MergeReport", "Result"]

logger = logging.getLogger(__name__)

TemplateLike = Template | str | Path


@dataclass
class Result:
    """Outcome for one recipient."""

    to: str
    status: str  # "sent", "dry_run", "failed", "skipped"
    variant: str | None = None
    message_id: str | None = None
    detail: str = ""


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

    def summary(self) -> str:
        parts = [f"{s}: {self.count(s)}" for s in ("sent", "dry_run", "failed", "skipped") if self.count(s)]
        return ", ".join(parts) or "nothing to do"


def _as_templates(templates: TemplateLike | Sequence[TemplateLike] | Mapping[str, TemplateLike]) -> dict[str, Template]:
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
    exclude: Iterable[str] | str | Path | None = None,
    log_path: str | Path | None = None,
    resume: bool = False,
    limit: int | None = None,
    min_interval: float = 0.0,
    list_unsubscribe: str | None = None,
    one_click_unsubscribe: bool = False,
    allow_duplicates: bool = False,
    attachments: Sequence[str | Path] = (),
    attachment_field: str | None = None,
    output_dir: str | Path | None = None,
    progress: Callable[[Result], None] | None = None,
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
        transport: e.g. ``SMTPTransport(SMTPConfig.from_service("gmail", user, pw))``.
        dry_run: Build and validate all messages without sending (default).
        to_field: Column holding each recipient's address.
        seed: Random seed for assigning variants. Record it with your results.
        strata: Column(s) to stratify (block) the random assignment on.
        weights: Relative allocation to each template, e.g. ``[1, 1]``.
        variant_field: Column naming the template for each row, if you did the
            assignment yourself (mutually exclusive with ``seed``).
        assignments_path: Write the assignment table (CSV) here.
        exclude: Addresses to skip (e.g. people who already responded), as an
            iterable, or a ``.txt``/``.csv``/``.json`` file.
        log_path: JSON Lines send log (audit trail; required for ``resume``).
        resume: Skip recipients the log already records as ``sent``.
        limit: Send at most this many messages this run (e.g. a daily quota).
        min_interval: Seconds to wait between messages (rate limiting).
        list_unsubscribe: ``List-Unsubscribe`` header value; may contain
            placeholders, e.g. ``"<https://example.org/unsub?id={id}>"``.
        one_click_unsubscribe: Add the RFC 8058 one-click header too.
        allow_duplicates: Permit the same address on more than one row.
        attachments: Files attached to every message.
        attachment_field: Column with per-recipient file path(s), separated
            by ``;`` (e.g. a personalised certificate). Paths may contain
            placeholders. Relative paths are resolved from the working directory.
        output_dir: In a dry run, also write each message as a ``.eml`` file here.
        progress: Called with each :class:`Result` as it is produced.

    Returns:
        A :class:`MergeReport`.

    Raises:
        DataError: validation found errors (see ``exc.issues``); nothing was sent.
        FatalSendError: the connection or login failed; see the log for what
            was already sent, and re-run with ``resume=True``.
    """
    tpls = _as_templates(templates)
    records = load_recipients(recipients)
    if resume and log_path is None:
        raise ValueError("resume=True needs log_path")
    if not dry_run and transport is None:
        raise ValueError("dry_run=False needs a transport (e.g. SMTPTransport)")
    if seed is not None and variant_field is not None:
        raise ValueError("use either seed (random assignment) or variant_field (pre-assigned), not both")
    if len(tpls) > 1 and seed is None and variant_field is None:
        raise ValueError("several templates given: pass seed= for random assignment, or variant_field=")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    fields: set[str] = set().union(*(t.fields for t in tpls.values()))
    if list_unsubscribe:
        fields |= placeholders(list_unsubscribe)
    if strata:
        fields |= {strata} if isinstance(strata, str) else set(strata)
    if variant_field:
        fields.add(variant_field)
    if attachment_field:
        fields.add(attachment_field)
    issues = validate_recipients(records, fields, to_field=to_field, allow_duplicates=allow_duplicates)
    skip: set[str] = set()
    if exclude is not None:
        try:
            skip = load_address_list(exclude, to_field=to_field)
        except DataError as exc:
            issues.insert(0, Issue(None, "error", f"exclude list: {exc}"))
    if sender and not is_valid_address(sender):
        issues.insert(0, Issue(None, "error", f"invalid sender address {sender!r}"))
    if not sender and any("From" not in t.headers for t in tpls.values()):
        issues.insert(0, Issue(None, "error", "no sender: pass sender= or add a 'From:' header to the template"))
    for path in attachments:
        if not Path(path).is_file():
            issues.append(Issue(None, "error", f"attachment not found: {path}"))
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

    built: list[tuple[str, str, EmailMessage]] = []
    for row, (record, variant) in enumerate(zip(records, variants, strict=True), start=1):
        try:
            rendered = tpls[variant].render(record, to_field=to_field)
            for name, value in rendered.headers.items():
                if value and not all(is_valid_address(v) for v in _split_addresses(value)):
                    raise TemplateError(f"invalid address in {name}: {value!r}")
            unsub = fill(list_unsubscribe, record) if list_unsubscribe else None
            files = [Path(p) for p in attachments]
            if attachment_field:
                files += [Path(fill(p.strip(), record)) for p in record[attachment_field].split(";") if p.strip()]
            missing = [str(p) for p in files if not p.is_file()]
            if missing:
                raise TemplateError(f"attachment not found: {', '.join(missing)}")
            message = build_message(
                rendered,
                sender=sender or "",
                list_unsubscribe=unsub,
                one_click_unsubscribe=one_click_unsubscribe,
                attachments=files,
            )
        except (TemplateError, ValueError, OSError) as exc:
            errors.append(Issue(row, "error", str(exc)))
            continue
        built.append((rendered.to, variant, message))
    if errors:
        raise DataError(_describe(errors), errors)

    already = sent_addresses(log_path) if resume and log_path is not None else set()

    active: Transport = transport if (transport is not None and not dry_run) else DryRunTransport(output_dir)
    log = SendLog(log_path) if log_path is not None else None
    delivered = 0
    try:
        active.open()
        for to, variant, message in built:
            key = normalize_address(to)
            variant_label = variant if len(tpls) > 1 else None
            if key in skip or key in already:
                reason = "excluded" if key in skip else "already sent (resume)"
                result = Result(to, "skipped", variant_label, detail=reason)
            elif limit is not None and delivered >= limit:
                result = Result(to, "skipped", variant_label, detail="limit reached")
            else:
                if delivered and min_interval and not dry_run:
                    time.sleep(min_interval)
                result = _deliver(active, message, to, variant_label, dry_run)
                if result.status in {"sent", "dry_run"}:
                    delivered += 1
                if dry_run:
                    report.messages.append(message)
            report.results.append(result)
            if log is not None and not dry_run:
                log.write(
                    to=to,
                    status=result.status,
                    variant=result.variant,
                    message_id=result.message_id,
                    detail=result.detail,
                    seed=seed,
                )
            if progress is not None:
                progress(result)
    finally:
        active.close()
        if log is not None:
            log.close()
    return report


def _deliver(transport: Transport, message: EmailMessage, to: str, variant: str | None, dry_run: bool) -> Result:
    message_id = str(message["Message-ID"])
    try:
        refused = transport.send(message)
    except PermanentSendError as exc:
        logger.warning("failed to send to %s: %s", to, exc)
        return Result(to, "failed", variant, message_id, str(exc))
    except FatalSendError:
        raise
    detail = f"some recipients refused: {refused}" if refused else ""
    return Result(to, "dry_run" if dry_run else "sent", variant, message_id, detail)


def _split_addresses(value: str) -> list[str]:
    """Split a header like "A <a@x.org>, b@y.org" into single addresses."""
    return [formataddr(pair) for pair in getaddresses([value])]


def _describe(errors: Sequence[Issue], shown: int = 20) -> str:
    lines = [str(i) for i in errors[:shown]]
    if len(errors) > shown:
        lines.append(f"... and {len(errors) - shown} more")
    return f"{len(errors)} problem(s) found; nothing was sent:\n  " + "\n  ".join(lines)
