"""Ways to deliver messages: real SMTP, or a dry run that writes ``.eml`` files."""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

__all__ = [
    "PRESETS",
    "SMTPConfig",
    "Transport",
    "SMTPTransport",
    "DryRunTransport",
    "PermanentSendError",
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


class SMTPTransport:
    """Deliver over SMTP with TLS, automatic reconnection, and retries.

    Temporary failures (SMTP 4xx replies, dropped connections) are retried up
    to ``max_retries`` times with exponential backoff. Permanent 5xx replies
    for a recipient raise :class:`PermanentSendError` so the run can continue
    with the next person. Authentication failures raise :class:`FatalSendError`.
    """

    def __init__(self, config: SMTPConfig, *, max_retries: int = 3, backoff: float = 2.0) -> None:
        self.config = config
        self.max_retries = max_retries
        self.backoff = backoff
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
        """
        attempt = 0
        while True:
            try:
                if self._smtp is None:
                    self.open()
                assert self._smtp is not None
                return self._smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                raise PermanentSendError(f"recipient refused: {exc.recipients}") from exc
            except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError) as exc:
                self._smtp = None  # reconnect on the next attempt
                error: Exception = exc
            except smtplib.SMTPResponseException as exc:
                if 500 <= exc.smtp_code < 600:
                    raise PermanentSendError(f"{exc.smtp_code} {exc.smtp_error!r}") from exc
                self.close()  # e.g. 421: the server is closing the connection
                error = exc
            attempt += 1
            if attempt > self.max_retries:
                raise PermanentSendError(f"gave up after {self.max_retries} retries: {error}") from error
            delay = self.backoff**attempt
            logger.warning("temporary failure (%s); retrying in %.0fs", error, delay)
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
