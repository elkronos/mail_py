"""Ways to deliver messages: real SMTP, or a dry run that writes ``.eml`` files."""

from __future__ import annotations

import hashlib
import logging
import re
import smtplib
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.errors import MessageError
from email.message import EmailMessage, Message
from email.utils import getaddresses
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO, runtime_checkable

from .errors import EmailMergeError
from .message import wire_bytes

if TYPE_CHECKING:
    from .merge import MergeReport

__all__ = [
    "PRESETS",
    "SMTPConfig",
    "Transport",
    "SMTPTransport",
    "DryRunTransport",
    "PermanentSendError",
    "UncertainSendError",
    "FatalSendError",
    "AccountRefusedError",
    "TransportError",
]

logger = logging.getLogger(__name__)

#: Known providers: host, port, security. Password (basic) authentication must
#: be allowed on the account; see the "Sending" docs page for provider notes.
PRESETS: dict[str, tuple[str, int, str]] = {
    # Gmail requires an app password (2-Step Verification on) for SMTP.
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    # Outlook.com / Hotmail and Microsoft 365 use STARTTLS on 587, not port 465.
    "outlook": ("smtp-mail.outlook.com", 587, "starttls"),
    "office365": ("smtp.office365.com", 587, "starttls"),
}


class PermanentSendError(EmailMergeError):
    """This one message cannot be delivered (e.g. 5xx: recipient rejected).

    Attributes:
        smtp_code: The server's reply code, when a reply caused the error.
        smtp_error: The text of that reply.
    """

    def __init__(self, *args: object, smtp_code: int | None = None, smtp_error: str = "") -> None:
        super().__init__(*args)
        self.smtp_code = smtp_code
        self.smtp_error = smtp_error


class UncertainSendError(PermanentSendError):
    """The connection failed after the message data was sent.

    The server may already have accepted and queued the message, so it is not
    retried: a retry here is how one person ends up with several copies.
    """


class FatalSendError(EmailMergeError):
    """The run cannot continue (e.g. authentication failed).

    Attributes:
        report: The :class:`~email_merge_tool.MergeReport` of the run up to
            the stop, when :func:`~email_merge_tool.mail_merge` raised this:
            who was sent the message, who was not, and the warnings. ``None``
            when the error comes from calling a transport directly.
    """

    report: MergeReport | None = None


class TransportError(FatalSendError):
    """The transport raised an exception that is not one of this package's send errors.

    :func:`~email_merge_tool.mail_merge` stops the run with this error, and
    the original exception is its ``__cause__``. The message being sent is
    logged ``uncertain``, because the transport may have sent it before it
    failed.
    """


class AccountRefusedError(FatalSendError):
    """The server refuses mail from this account, not just this one message.

    Raised for a sending quota or rate limit (enhanced status 5.4.5, a 4.7.x
    or 5.7.x reply that mentions a quota, rate or limit, Microsoft's
    SubmissionQuotaExceeded), a login the server asks for while sending (530,
    535), or a sender address it refuses (5xx to MAIL FROM, except 552 for
    size). Sending more would fail the same way. The message that met the
    reply was not sent.

    Attributes:
        smtp_code: The server's reply code.
        smtp_error: The text of that reply.
    """

    def __init__(self, *args: object, smtp_code: int | None = None, smtp_error: str = "") -> None:
        super().__init__(*args)
        self.smtp_code = smtp_code
        self.smtp_error = smtp_error


@dataclass
class SMTPConfig:
    """Connection settings for an SMTP server.

    Args:
        host: Server name.
        port: Port (465 for implicit TLS, 587 for STARTTLS, 25 for relays).
        security: ``"ssl"`` (implicit TLS), ``"starttls"``, or ``"none"``
            (only for a local relay or test server; credentials are refused).
        username: Login name; ``None`` to skip authentication.
        password: Login secret (for Gmail, an app password).
        timeout: Socket timeout in seconds.
    """

    host: str
    port: int
    security: str = "starttls"
    username: str | None = None
    password: str | None = None
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.security not in {"ssl", "starttls", "none"}:
            raise ValueError("security must be 'ssl', 'starttls' or 'none'")
        if self.security == "none" and self.password:
            raise ValueError("refusing to send a password over an unencrypted connection")

    @classmethod
    def from_service(cls, service: str, username: str | None = None, password: str | None = None) -> SMTPConfig:
        """Settings for a named provider in :data:`PRESETS`."""
        try:
            host, port, security = PRESETS[service.lower()]
        except KeyError:
            raise ValueError(f"unknown service {service!r}; choose from {', '.join(PRESETS)} or give a host") from None
        return cls(host, port, security, username, password)

    def __repr__(self) -> str:  # never show the password in logs or tracebacks
        return (
            f"SMTPConfig(host={self.host!r}, port={self.port}, security={self.security!r}, "
            f"username={self.username!r}, password={'***' if self.password else None})"
        )


@runtime_checkable
class Transport(Protocol):
    """What :func:`~email_merge_tool.mail_merge` needs from a transport, built in or your own.

    ``open()`` is called once before the first message and ``close()`` once
    at the end, also when the run stops with an error. ``send(message)``
    delivers one message to the addresses in its ``To``, ``Cc`` and ``Bcc``
    headers; :func:`~email_merge_tool.wire_bytes` gives the bytes SMTP would
    transmit, without ``Bcc``. What ``send`` does sets the recipient's status:

    * Return ``None`` (or an empty mapping): delivered, ``sent``.
    * Return ``{address: (code, reply)}``: delivered to everyone except these
      recipients. The row is ``sent``, or ``failed``/``bounced`` when its
      ``To`` address is among them. Entries of another shape, and return
      values that are not mappings (such as a provider's response), are
      ignored.
    * Raise :class:`PermanentSendError`: this message was not delivered; the
      run goes on. Pass the reply as ``smtp_code=`` and ``smtp_error=`` so a
      bounce (such as ``550``, ``"5.1.1 no such user"``) is recorded as
      ``bounced``.
    * Raise :class:`UncertainSendError`: it may have been delivered (the
      request went out, the answer did not come back); logged ``uncertain``
      and never sent again.
    * Raise :class:`FatalSendError` (or :class:`AccountRefusedError`): stop
      the run; this message was not sent and resume sends it.
    * Any other exception stops the run with :class:`TransportError`, and
      the message is logged ``uncertain``.

    ``send`` should retry temporary failures itself; ``mail_merge`` does not.
    A transport that only previews sets the attribute ``delivers = False``,
    as :class:`DryRunTransport` does, and ``mail_merge`` then runs a dry run.
    """

    def open(self) -> None: ...

    def send(self, message: EmailMessage) -> Mapping[str, tuple[int, bytes | str]] | None: ...

    def close(self) -> None: ...


class _DataPhase:
    """Track whether the whole message has been written to an SMTP connection.

    ``smtplib``'s ``sendmail`` calls the connection's ``data()`` method
    once MAIL and RCPT have succeeded, and ``data()`` writes twice: the DATA
    command, then the message ending in ``"\\r\\n.\\r\\n"``. Shadowing ``data()``
    and ``send()`` on this one instance marks the moment the second write
    completes. From then on the server may have the message. The socket
    timeout is raised for the server's reply to it.
    """

    def __init__(self, smtp: smtplib.SMTP, timeout: float | None) -> None:
        self.sent = False
        self._smtp = smtp
        self._timeout = timeout

    def __enter__(self) -> _DataPhase:
        smtp = self._smtp
        original_data = getattr(smtp, "data", None)
        original_send = getattr(smtp, "send", None)
        if original_data is None or original_send is None:
            return self

        def data(msg: Any) -> Any:
            writes = 0
            sock: Any = None
            previous: float | None = None

            def send(payload: Any) -> None:
                nonlocal writes, sock, previous
                original_send(payload)
                writes += 1
                if writes == 2:  # the DATA command, then the message itself
                    self.sent = True
                    sock = getattr(smtp, "sock", None)
                    previous = sock.gettimeout() if sock is not None else None
                    if previous is not None:
                        sock.settimeout(None if self._timeout is None else max(previous, self._timeout))

            setattr(smtp, "send", send)  # noqa: B010 - shadows the method on this instance only
            try:
                return original_data(msg)
            finally:
                vars(smtp).pop("send", None)
                current = getattr(smtp, "sock", None)
                if previous is not None and current is not None and current is sock:  # still connected
                    current.settimeout(previous)

        setattr(smtp, "data", data)  # noqa: B010 - shadows the method on this instance only
        return self

    def __exit__(self, *exc: object) -> None:
        vars(self._smtp).pop("data", None)
        vars(self._smtp).pop("send", None)


def _envelope(message: Message) -> tuple[str, list[str], set[str]]:
    """The envelope sender and recipients, chosen as ``smtplib``'s ``send_message`` does.

    The sender is the ``Sender`` address, or else the ``From`` address. The
    recipients are every address in ``To``, ``Bcc`` and ``Cc``. A message with
    one block of ``Resent-`` headers uses those instead. The third item holds
    the main recipients: the ``To`` addresses, or every recipient if there
    are none.
    """
    resent = message.get_all("Resent-Date")
    if resent is None:
        prefix = ""
    elif len(resent) == 1:
        prefix = "Resent-"
    else:
        raise ValueError("message has more than one 'Resent-' header block")
    sender = message[prefix + "Sender"] if prefix + "Sender" in message else message[prefix + "From"]
    fields = [message[prefix + name] for name in ("To", "Bcc", "Cc") if message[prefix + name] is not None]
    recipients = [address for _, address in getaddresses(fields)]
    to = message[prefix + "To"]
    main = {address for _, address in getaddresses([to])} & set(recipients) if to is not None else set()
    return getaddresses([sender])[0][1], recipients, main or set(recipients)


def _flatten(message: Message, *, utf8: bool = False, eight_bit: bool = True) -> bytes:
    """The message as transmitted: ``Bcc`` removed and CRLF line endings.

    ``smtplib`` flattens with ``mangle_from_=True``, which turns body lines
    starting ``From `` into ``>From ``; that quoting belongs to mbox files,
    not SMTP. Without ``eight_bit``, 8-bit text parts are re-encoded (base64
    or quoted-printable) so the result is 7-bit. Preflight serializes with the
    same function, :func:`email_merge_tool.message.wire_bytes`.
    """
    return wire_bytes(message, utf8=utf8, seven_bit=not eight_bit)


def _wire(smtp: smtplib.SMTP, message: Message, sender: str, recipients: list[str]) -> tuple[bytes, list[str]]:
    """The bytes to send to this server, and the MAIL options they need."""
    smtp.ehlo_or_helo_if_needed()
    if not "".join([sender, *recipients]).isascii():
        if not smtp.has_extn("smtputf8"):
            raise smtplib.SMTPNotSupportedError(
                "an address needs internationalized email (SMTPUTF8), which the server does not offer"
            )
        return _flatten(message, utf8=True), ["SMTPUTF8", "BODY=8BITMIME"]
    payload = _flatten(message)
    if payload.isascii():
        return payload, []
    if smtp.has_extn("8bitmime"):
        return payload, ["BODY=8BITMIME"]  # RFC 6152: 8-bit data must be declared
    return _flatten(message, eight_bit=False), []


def _text(reply: bytes | str) -> str:
    return reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)


def _describe_refused(refused: Mapping[str, object]) -> str:
    """``{"a@x.org": (550, b"5.1.1 no such user")}`` as ``a@x.org (550 5.1.1 no such user)``."""
    parts = []
    for address, reply in refused.items():
        if isinstance(reply, tuple) and len(reply) == 2:
            reply = f"{reply[0]} {_text(reply[1])}"
        parts.append(f"{address} ({reply})")
    return ", ".join(parts)


def _reason(error: BaseException) -> str:
    if isinstance(error, FatalSendError) and error.__cause__ is not None:  # a failed reconnect
        return f"reconnecting failed: {_reason(error.__cause__)}"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return f"recipient refused: {_describe_refused(error.recipients)}"
    if isinstance(error, smtplib.SMTPResponseException):
        return f"{error.smtp_code} {_text(error.smtp_error)}"
    return str(error) or type(error).__name__


def _temporary(reply: tuple[int, bytes]) -> bool:
    return 400 <= reply[0] < 500


def _temporary_open_failure(error: BaseException | None) -> bool:
    """Whether connecting may work when tried again: no answer, a dropped connection, or a 4xx reply.

    A 5xx reply, a TLS failure, a refused login and a missing extension are
    left alone, because trying again meets them again.
    """
    if isinstance(error, smtplib.SMTPResponseException):  # a greeting, EHLO or AUTH reply
        return 400 <= error.smtp_code < 500
    if isinstance(error, (ssl.SSLError, smtplib.SMTPNotSupportedError)):
        return False
    return isinstance(error, OSError)  # refused, timed out, reset, or closed by the server


_ENHANCED_STATUS = re.compile(r"\s*([245])\.(\d{1,3})\.(\d{1,3})(?!\d)")
_LIMIT_WORDS = re.compile(r"quota|\brate\b|ratelimit|\blimit|exceed|too many", re.IGNORECASE)


def _account_refusal(code: int, text: str) -> str | None:
    """Why a 4xx or 5xx reply refuses the account rather than one message, or None."""
    if not 400 <= code < 600:
        return None
    quota = "The sending quota is used up; continue after it resets."
    if code in (530, 535):
        return "It asked for a login while sending; check the username and password."
    if "submissionquotaexceeded" in text.lower():  # Microsoft 365
        return quota
    status = _ENHANCED_STATUS.match(text)
    if status is None:
        return None
    if status.groups() == ("5", "4", "5"):  # Gmail: "Daily user sending limit exceeded"
        return quota
    if status.group(2) == "7" and _LIMIT_WORDS.search(text):
        return "It limits how much this account may send; continue later, or send more slowly."
    return None


def _mailbox_problem(text: str) -> bool:
    """Whether a reply's enhanced status names the recipient's address or mailbox (X.1.x, X.2.x).

    X.1.7 and X.1.8 are about the sender's address, so they do not count.
    """
    status = _ENHANCED_STATUS.match(text)
    if status is None:
        return False
    return status.group(2) == "2" or (status.group(2) == "1" and status.group(3) not in ("7", "8"))


def _enhanced_status(text: str) -> str | None:
    """The enhanced status code (RFC 3463) a reply starts with, such as ``"5.1.1"``, or None."""
    status = _ENHANCED_STATUS.match(text)
    return ".".join(status.groups()) if status is not None else None


# Reply text that names an unknown recipient, for a 550 without an enhanced status code.
_UNKNOWN_USER = re.compile(
    r"\b(?:unknown (?:user|recipient|mailbox)|(?:user|recipient|mailbox) unknown|no such (?:user|recipient|mailbox)"
    r"|(?:user|recipient|mailbox|address)(?: \S+)? (?:does not|doesn't) exist|invalid (?:recipient|mailbox))\b",
    re.IGNORECASE,
)


def _bounced(code: int | None, text: str) -> bool:
    """Whether a reply refuses the recipient's mailbox for good: a hard bounce.

    That is a 5xx reply with enhanced status 5.1.x (a bad or unknown address;
    5.1.7 and 5.1.8 are about the sender, so they do not count) or 5.2.1 (the
    mailbox is disabled), or a 550 without an enhanced status whose text says
    the user is unknown. A full mailbox (5.2.2) and policy refusals (5.7.x)
    are not bounces: the address exists and may take mail later.
    """
    if code is None or not 500 <= code < 600:
        return False
    status = _ENHANCED_STATUS.match(text)
    if status is None:
        return code == 550 and _UNKNOWN_USER.search(text) is not None
    kind, subject, detail = status.groups()
    return kind == "5" and ((subject == "1" and detail not in ("7", "8")) or (subject, detail) == ("2", "1"))


def _reply(error: BaseException, main: set[str]) -> tuple[int | None, str]:
    """The code and text of the reply behind an error: a main recipient's, if several."""
    if isinstance(error, smtplib.SMTPRecipientsRefused) and error.recipients:
        replies = error.recipients
        code, text = next((replies[a] for a in replies if a in main), next(iter(replies.values())))
        return code, _text(text)
    if isinstance(error, smtplib.SMTPResponseException):
        return error.smtp_code, _text(error.smtp_error)
    return None, ""


def _refused_account(error: smtplib.SMTPException, sender: str) -> AccountRefusedError | None:
    """An :class:`AccountRefusedError` if a reply in ``error`` refuses the account."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        replies = [(code, _text(text)) for code, text in error.recipients.values()]
    elif isinstance(error, smtplib.SMTPResponseException):
        replies = [(error.smtp_code, _text(error.smtp_error))]
    else:
        return None
    for code, text in replies:
        why = _account_refusal(code, text)
        if why is None and isinstance(error, smtplib.SMTPSenderRefused) and 500 <= code < 600 and code != 552:
            why = f"It refuses the sender address {sender}; check the From address."
        if why is not None:
            return AccountRefusedError(
                f"the server refused mail from this account ({code} {text}). {why}", smtp_code=code, smtp_error=text
            )
    return None


def _open_error(cfg: SMTPConfig, step: str, exc: BaseException) -> str:
    """Explain a failure in :meth:`SMTPTransport.open` by the step it happened in."""
    where = f"{cfg.host}:{cfg.port}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            f"login to {cfg.host} failed ({_reason(exc)}). For Gmail use an app password; "
            "some providers (notably Microsoft) no longer accept passwords for SMTP."
        )
    if isinstance(exc, ssl.SSLCertVerificationError):
        return (
            f"the TLS certificate of {cfg.host} could not be verified ({exc.verify_message or exc}). "
            "Check the host name, which must be one the certificate names."
        )
    if isinstance(exc, ssl.SSLError):
        if step == "connect":  # implicit TLS starts while connecting
            return (
                f"TLS with {where} failed ({exc.reason or exc}). If this port expects STARTTLS "
                "(usually 587), use --security starttls (security='starttls')."
            )
        return f"TLS with {where} failed after STARTTLS ({exc.reason or exc})."
    if step == "starttls":
        if isinstance(exc, smtplib.SMTPNotSupportedError):
            return (
                f"{where} does not offer STARTTLS, so the connection cannot be encrypted. For a relay "
                "on a network you trust, use --security none (security='none'); messages then travel unencrypted."
            )
        return f"{where} failed during STARTTLS ({_reason(exc)})."
    if step == "login":
        if isinstance(exc, smtplib.SMTPNotSupportedError):
            return (
                f"{where} does not offer a login (no AUTH). For a relay that needs none, use --no-auth (username=None)."
            )
        return f"login to {cfg.host} failed ({_reason(exc)})."
    if isinstance(exc, smtplib.SMTPConnectError):  # the greeting was not 220
        return f"{where} refused the connection ({_reason(exc)})."
    hint = "check the host name and port"
    if cfg.port == 465 and cfg.security != "ssl":
        hint = "port 465 usually expects implicit TLS, so use --security ssl (security='ssl')"
    return f"could not connect to {where} ({_reason(exc)}); {hint}."


class SMTPTransport:
    """Deliver over SMTP with TLS, automatic reconnection, and retries.

    Temporary failures (SMTP 4xx replies, including a 4xx refusal of a
    recipient, and dropped connections) are retried up to ``max_retries``
    times with exponential backoff, but only until the message data has been
    sent. A connection lost after that raises :class:`UncertainSendError`
    instead, because the server may already have the message. Permanent 5xx
    replies for a recipient raise :class:`PermanentSendError` so the run can
    continue with the next person. Authentication failures raise
    :class:`FatalSendError`.

    A connection lost between messages is opened again before the next one.
    Reconnecting that fails for a reason that may pass (no answer, a dropped
    connection, a 4xx greeting) counts as one of the ``max_retries``; when
    they run out, :meth:`send` raises :class:`FatalSendError`.

    When the server accepts the message for some recipients and refuses
    others, :meth:`send` returns the refusals. A temporary refusal of a main
    (``To``) recipient is retried for the temporarily refused addresses only,
    so the others do not get a second copy.

    Args:
        config: Server settings.
        max_retries: Retries for a temporary failure before the message data
            is sent.
        backoff: Base of the exponential delay between retries, in seconds.
        data_timeout: Seconds to wait for the server's reply after the
            message data (RFC 5321 section 4.5.3.2.6 recommends 10 minutes).
    """

    #: It sends mail; :func:`~email_merge_tool.mail_merge` uses it only with ``dry_run=False``.
    delivers = True

    def __init__(
        self, config: SMTPConfig, *, max_retries: int = 3, backoff: float = 2.0, data_timeout: float | None = 600.0
    ) -> None:
        self.config = config
        self.max_retries = max_retries
        self.backoff = backoff
        self.data_timeout = data_timeout
        #: True when the last send was interrupted (e.g. Ctrl-C) after the
        #: message was written, so it may have been delivered.
        self.unconfirmed = False
        self._smtp: smtplib.SMTP | None = None

    def open(self) -> None:
        """Connect, start TLS and log in.

        Raises:
            FatalSendError: a step failed. The message names the step
                (connecting, TLS or login) and what to try.
        """
        cfg = self.config
        context = ssl.create_default_context()
        smtp: smtplib.SMTP | None = None
        step = "connect"
        try:
            if cfg.security == "ssl":
                smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context)
            else:
                smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
                if cfg.security == "starttls":
                    step = "starttls"
                    smtp.starttls(context=context)
            if cfg.username:
                step = "login"
                smtp.login(cfg.username, cfg.password or "")
        except (OSError, smtplib.SMTPException) as exc:
            if smtp is not None:
                try:
                    smtp.close()
                except OSError:
                    pass
            raise FatalSendError(_open_error(cfg, step, exc)) from exc
        self._smtp = smtp

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except (OSError, smtplib.SMTPException):
                pass
            self._smtp = None

    def send(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        """Send one message.

        Returns:
            Recipients the server refused while accepting the message for the
            others, as ``{address: (code, reply)}``. The message *was* sent
            when this returns, but not to these addresses, which may include
            the ``To`` address when a ``Cc`` or ``Bcc`` was accepted.

        Raises:
            PermanentSendError: the server rejected the message, or temporary
                failures persisted through ``max_retries`` retries.
            UncertainSendError: the connection failed after the message data
                was sent; the message may have been delivered.
            AccountRefusedError: the reply refuses the account (a quota, a
                rate limit, a login or the sender address), so the run
                should stop.
            FatalSendError: reconnecting failed for good: a login or TLS
                failure, or ``max_retries`` failed attempts in a row.
        """
        attempt = 0
        self.unconfirmed = False
        sender, pending, main = _envelope(message)
        refused: dict[str, tuple[int, bytes]] = {}  # for good, while others were sent the message
        delivered = False  # the server has taken the message for at least one recipient
        error: Exception
        while True:
            phase: _DataPhase | None = None
            try:
                if self._smtp is None:
                    self.open()
                smtp = self._smtp
                assert smtp is not None
                payload, options = _wire(smtp, message, sender, pending)
                phase = _DataPhase(smtp, self.data_timeout)
                with phase:
                    result = smtp.sendmail(sender, pending, payload, options)
            except smtplib.SMTPRecipientsRefused as exc:  # nobody was sent the message this time
                if any(code == 421 for code, _ in exc.recipients.values()):
                    self.close()  # smtplib has already dropped the connection
                if account := _refused_account(exc, sender):
                    raise account from exc
                permanent = {address: reply for address, reply in exc.recipients.items() if not _temporary(reply)}
                if permanent.keys() & main or len(permanent) == len(exc.recipients):
                    if delivered:  # a retry for the main recipient, after the others were sent it
                        refused.update(exc.recipients)
                        return refused
                    code, text = _reply(exc, main)
                    raise PermanentSendError(_reason(exc), smtp_code=code, smtp_error=text) from exc
                refused.update(permanent)
                pending = [address for address in pending if address not in permanent]
                error = exc
            except smtplib.SMTPResponseException as exc:
                if exc.smtp_code == 421:
                    self.close()  # the server is closing the connection
                if account := _refused_account(exc, sender):
                    raise account from exc
                if 500 <= exc.smtp_code < 600:
                    raise PermanentSendError(
                        _reason(exc), smtp_code=exc.smtp_code, smtp_error=_text(exc.smtp_error)
                    ) from exc
                self.close()
                if phase is not None and phase.sent and not 400 <= exc.smtp_code < 500:
                    # Only a 4xx reply says the server did not take the message. A
                    # reply cut short, garbled or otherwise unexpected says nothing.
                    raise UncertainSendError(
                        f"unclear reply after the message was sent ({_reason(exc)}); "
                        "the server may have accepted it, so it was not sent again"
                    ) from exc
                error = exc
            except smtplib.SMTPNotSupportedError as exc:  # e.g. SMTPUTF8 needed but not offered
                raise PermanentSendError(str(exc)) from exc
            except OSError as exc:  # dropped connection, timeout, TLS failure (smtplib errors are OSErrors too)
                self._smtp = None  # reconnect on the next attempt
                if phase is not None and phase.sent:
                    raise UncertainSendError(
                        f"connection lost after the message was sent ({str(exc) or type(exc).__name__}); "
                        "the server may have accepted it, so it was not sent again"
                    ) from exc
                error = exc
            except MessageError as exc:  # a header the generator cannot write
                raise PermanentSendError(f"the message cannot be written: {exc}") from exc
            except FatalSendError as exc:  # from open(): reconnecting failed
                if not _temporary_open_failure(exc.__cause__):
                    raise
                error = exc
            except BaseException:  # Ctrl-C and the like: let the caller log it if it may have arrived
                self.unconfirmed = phase is not None and phase.sent
                if self._smtp is not None:
                    try:
                        self._smtp.close()  # the conversation is out of step; do not reuse it
                    except OSError:
                        pass
                    self._smtp = None
                raise
            else:
                delivered = True
                missed = {address: reply for address, reply in result.items() if address in main}
                if missed and (account := _refused_account(smtplib.SMTPRecipientsRefused(missed), sender)):
                    raise account  # the main recipient did not get it; a Cc or Bcc did
                retry = {address: reply for address, reply in result.items() if _temporary(reply)}
                if not retry.keys() & main:
                    refused.update(result)
                    return refused
                refused.update({address: reply for address, reply in result.items() if address not in retry})
                pending = list(retry)  # the others have it now
                error = smtplib.SMTPRecipientsRefused(retry)
            attempt += 1
            if attempt > self.max_retries:
                if isinstance(error, FatalSendError):  # the server stayed out of reach
                    raise FatalSendError(
                        f"lost the connection to {self.config.host}:{self.config.port} and could not "
                        f"reconnect in {self.max_retries} retries ({_reason(error.__cause__ or error)})"
                    ) from error
                reason = f"gave up after {self.max_retries} retries: {_reason(error)}"
                others = "; the other recipients were sent the message" if delivered else ""
                code, text = _reply(error, main)
                raise PermanentSendError(reason + others, smtp_code=code, smtp_error=text) from error
            delay = self.backoff**attempt
            logger.warning("temporary failure (%s); retrying in %.0fs", _reason(error), delay)
            time.sleep(delay)

    def __enter__(self) -> SMTPTransport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class DryRunTransport:
    """Collect messages instead of sending them; optionally save them as ``.eml``.

    ``.eml`` files open in most mail clients, which is the most faithful way to
    preview exactly what recipients will see. Each file holds the bytes that
    would go over SMTP, ``Bcc`` included, and is named after the message's
    number and its ``To`` address, shortened with a hash past 80 characters.

    Pass it as ``transport=`` to collect the messages or choose where they
    go; :func:`~email_merge_tool.mail_merge` treats every run with it as a
    dry run, since it sends nothing (``delivers`` is false).

    The directory keeps a list of the files a dry run wrote, in
    ``.email-merge-dry-run``. The next dry run into the same directory first
    deletes those files, so the directory shows the latest run only. Other
    files in it are never touched.

    Raises:
        FatalSendError: from ``open`` or ``send``, when the directory or a
            file cannot be written.
    """

    #: The file in ``output_dir`` that lists the ``.eml`` files the last run wrote.
    MANIFEST = ".email-merge-dry-run"

    #: It sends nothing, so :func:`~email_merge_tool.mail_merge` treats a run with it as a dry run.
    delivers = False

    def __init__(self, output_dir: str | Path | None = None) -> None:
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.messages: list[EmailMessage] = []
        self._manifest: TextIO | None = None

    def open(self) -> None:
        if self.output_dir is None:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            manifest = self.output_dir / self.MANIFEST
            if manifest.is_file():
                for name in manifest.read_text(encoding="utf-8").splitlines():
                    if _EML_NAME.fullmatch(name):  # a plain file name this class writes, never a path
                        (self.output_dir / name).unlink(missing_ok=True)
            self._manifest = manifest.open("w", encoding="utf-8")
        except OSError as exc:
            raise FatalSendError(f"cannot write the dry-run messages to {self.output_dir}: {exc}") from exc

    def send(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        self.messages.append(message)
        if self.output_dir is not None:
            if self._manifest is None:
                self.open()
            assert self._manifest is not None
            name = f"{len(self.messages):05d}_{_file_label(str(message['To']))}.eml"
            try:
                self._manifest.write(name + "\n")  # listed first, so a file is never left unlisted
                self._manifest.flush()
                (self.output_dir / name).write_bytes(wire_bytes(message, keep_bcc=True))
            except OSError as exc:
                raise FatalSendError(f"cannot write {self.output_dir / name}: {exc}") from exc
        return {}

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()
            self._manifest = None


# The names DryRunTransport gives its .eml files.
_EML_NAME = re.compile(r"\d{5,}_[A-Za-z0-9._@-]*\.eml")


def _file_label(to: str) -> str:
    """The ``To`` address as part of a file name: ASCII, at most 80 characters."""
    address = getaddresses([to])[0][1] or to
    label = "".join(c if c.isascii() and (c.isalnum() or c in "._@-") else "_" for c in address)
    if len(label) > 80:
        label = label[:71] + "_" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:8]
    return label
