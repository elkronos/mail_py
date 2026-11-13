"""Ways to deliver messages: real SMTP, or a dry run that writes ``.eml`` files."""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.errors import MessageError
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "PRESETS",
    "SMTPConfig",
    "Transport",
    "SMTPTransport",
    "DryRunTransport",
    "PermanentSendError",
    "UncertainSendError",
    "FatalSendError",
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


class PermanentSendError(Exception):
    """This one message cannot be delivered (e.g. 5xx: recipient rejected)."""


class UncertainSendError(PermanentSendError):
    """The connection failed after the message data was sent.

    The server may already have accepted and queued the message, so it is not
    retried: a retry here is how one person ends up with several copies.
    """


class FatalSendError(Exception):
    """The run cannot continue (e.g. authentication failed)."""


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


class Transport(Protocol):
    """Anything that can deliver an :class:`EmailMessage`."""

    def open(self) -> None: ...

    def send(self, message: EmailMessage) -> object: ...

    def close(self) -> None: ...


class _DataPhase:
    """Track whether the whole message has been written to an SMTP connection.

    ``smtplib``'s ``send_message`` calls the connection's ``data()`` method
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


class SMTPTransport:
    """Deliver over SMTP with TLS, automatic reconnection, and retries.

    Temporary failures (SMTP 4xx replies, dropped connections) are retried up
    to ``max_retries`` times with exponential backoff, but only until the
    message data has been sent. A connection lost after that raises
    :class:`UncertainSendError` instead, because the server may already have
    the message. Permanent 5xx replies for a recipient raise
    :class:`PermanentSendError` so the run can continue with the next person.
    Authentication failures raise :class:`FatalSendError`.

    Args:
        config: Server settings.
        max_retries: Retries for a temporary failure before the message data
            is sent.
        backoff: Base of the exponential delay between retries, in seconds.
        data_timeout: Seconds to wait for the server's reply after the
            message data (RFC 5321 section 4.5.3.2.6 recommends 10 minutes).
    """

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
        cfg = self.config
        context = ssl.create_default_context()
        try:
            if cfg.security == "ssl":
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context)
            else:
                smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
                if cfg.security == "starttls":
                    smtp.starttls(context=context)
            if cfg.username:
                smtp.login(cfg.username, cfg.password or "")
        except smtplib.SMTPAuthenticationError as exc:
            raise FatalSendError(
                f"login to {cfg.host} failed ({exc.smtp_code}). For Gmail use an app password; "
                "some providers (notably Microsoft) no longer accept passwords for SMTP."
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise FatalSendError(f"could not connect to {cfg.host}:{cfg.port}: {exc}") from exc
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
            others (e.g. a bad Cc). The message *was* sent when this returns.

        Raises:
            PermanentSendError: the server rejected the message, or temporary
                failures persisted through ``max_retries`` retries.
            UncertainSendError: the connection failed after the message data
                was sent; the message may have been delivered.
        """
        attempt = 0
        self.unconfirmed = False
        while True:
            phase: _DataPhase | None = None
            try:
                if self._smtp is None:
                    self.open()
                assert self._smtp is not None
                phase = _DataPhase(self._smtp, self.data_timeout)
                with phase:
                    return self._smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                raise PermanentSendError(f"recipient refused: {exc.recipients}") from exc
            except smtplib.SMTPResponseException as exc:
                if 500 <= exc.smtp_code < 600:
                    raise PermanentSendError(f"{exc.smtp_code} {exc.smtp_error!r}") from exc
                self.close()  # e.g. 421: the server is closing the connection
                if phase is not None and phase.sent and not 400 <= exc.smtp_code < 500:
                    # Only a 4xx reply says the server did not take the message. A
                    # reply cut short, garbled or otherwise unexpected says nothing.
                    raise UncertainSendError(
                        f"unclear reply after the message was sent ({exc.smtp_code} {exc.smtp_error!r}); "
                        "the server may have accepted it, so it was not sent again"
                    ) from exc
                error: Exception = exc
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
            except BaseException:  # Ctrl-C and the like: let the caller log it if it may have arrived
                self.unconfirmed = phase is not None and phase.sent
                if self._smtp is not None:
                    try:
                        self._smtp.close()  # the conversation is out of step; do not reuse it
                    except OSError:
                        pass
                    self._smtp = None
                raise
            attempt += 1
            if attempt > self.max_retries:
                raise PermanentSendError(
                    f"gave up after {self.max_retries} retries: {str(error) or type(error).__name__}"
                ) from error
            delay = self.backoff**attempt
            logger.warning("temporary failure (%s); retrying in %.0fs", str(error) or type(error).__name__, delay)
            time.sleep(delay)

    def __enter__(self) -> SMTPTransport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class DryRunTransport:
    """Collect messages instead of sending them; optionally save them as ``.eml``.

    ``.eml`` files open in most mail clients, which is the most faithful way to
    preview exactly what recipients will see.
    """

    def __init__(self, output_dir: str | Path | None = None) -> None:
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.messages: list[EmailMessage] = []

    def open(self) -> None:
        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def send(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        self.messages.append(message)
        if self.output_dir is not None:
            safe = "".join(c if c.isalnum() or c in "._@-" else "_" for c in str(message["To"]))
            path = self.output_dir / f"{len(self.messages):05d}_{safe}.eml"
            path.write_bytes(message.as_bytes())
        return {}

    def close(self) -> None:
        pass
