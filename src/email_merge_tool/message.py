"""Turning a rendered template into a standards-compliant MIME message."""

from __future__ import annotations

import mimetypes
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from .templates import RenderedMessage

__all__ = ["build_message"]


def build_message(
    rendered: RenderedMessage,
    *,
    sender: str,
    list_unsubscribe: str | None = None,
    one_click_unsubscribe: bool = False,
    attachments: Sequence[str | Path] = (),
) -> EmailMessage:
    """Build an :class:`email.message.EmailMessage` ready to send.

    The message always has ``Date`` and ``Message-ID`` headers (RFC 5322),
    a plain-text part, and, when the template has HTML, a
    ``multipart/alternative`` HTML part. Non-ASCII subjects and names are
    encoded correctly by the standard library.

    Args:
        rendered: Output of :meth:`Template.render`.
        sender: From address, e.g. ``"Study Team <team@example.org>"``. A
            ``From`` header in the template overrides it.
        list_unsubscribe: Already-rendered value for the ``List-Unsubscribe``
            header (RFC 2369), e.g. ``"<https://example.org/u?id=42>"``.
        one_click_unsubscribe: Also add ``List-Unsubscribe-Post:
            List-Unsubscribe=One-Click`` (RFC 8058). Only valid when
            ``list_unsubscribe`` contains an ``https:`` URI, and it only works
            if your provider DKIM-signs the message.
        attachments: Files to attach. The MIME type is guessed from the file
            extension (``application/octet-stream`` if unknown).
    """
    msg = EmailMessage()
    from_value = rendered.headers.get("From") or sender
    msg["From"] = from_value
    msg["To"] = rendered.to
    for name in ("Cc", "Bcc", "Reply-To"):
        if rendered.headers.get(name):
            msg[name] = rendered.headers[name]
    msg["Subject"] = rendered.subject
    msg["Date"] = formatdate(localtime=True)
    domain = parseaddr(from_value)[1].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    if list_unsubscribe:
        if "\r" in list_unsubscribe or "\n" in list_unsubscribe:
            raise ValueError("List-Unsubscribe value contains a line break")
        msg["List-Unsubscribe"] = list_unsubscribe
        if one_click_unsubscribe:
            if "<https:" not in list_unsubscribe.replace(" ", "").lower():
                raise ValueError("one-click unsubscribe (RFC 8058) requires an <https://...> List-Unsubscribe URI")
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    elif one_click_unsubscribe:
        raise ValueError("one_click_unsubscribe requires list_unsubscribe")
    msg.set_content(rendered.text)
    if rendered.html is not None:
        msg.add_alternative(rendered.html, subtype="html")
    for item in attachments:
        path = Path(item)
        ctype, encoding = mimetypes.guess_type(path.name)
        if ctype is None or encoding is not None:  # unknown, or compressed (e.g. .tar.gz)
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg
