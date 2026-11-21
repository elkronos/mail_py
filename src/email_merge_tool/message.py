"""Turning a rendered template into a standards-compliant MIME message."""

from __future__ import annotations

import copy
import io
import mimetypes
import re
from collections.abc import Sequence
from email.errors import MessageError
from email.generator import BytesGenerator
from email.headerregistry import BaseHeader, HeaderRegistry
from email.message import EmailMessage
from email.policy import EmailPolicy, Policy
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

from .templates import RenderedMessage

__all__ = ["build_message", "needs_smtputf8", "wire_bytes"]

# RFC 5322 section 2.1.1: a header line, name included, may not exceed 998 characters.
_MAX_LINE = 998

# A line break inside a folded header that is not followed by folding
# whitespace, which would end the header or start a new one. Python 3.13
# refuses to write these; older versions write them.
_NEWLINE_WITHOUT_FWSP = re.compile(rb"\r\n[^ \t]|\r[^ \n\t]|\n[^ \t]")

# One RFC 2369 URI in angle brackets: printable ASCII other than < and >.
_UNSUBSCRIBE_URI = re.compile(r"\s*(<[!-;=?-~]+>)\s*(?:,|$)")


class _Verbatim:
    """Mixin for a header written exactly as given, folded only between its tokens.

    The email package RFC 2047-encodes an unstructured value whose words do
    not fit on a line, so a long List-Unsubscribe URL would reach mail clients
    as ``=?utf-8?q?...?=``, which none of them can use. Headers built with
    this mixin are ASCII, written as given, and folded only at the spaces
    between tokens, so unfolding restores the value.
    """

    name: str
    _tokens: tuple[str, ...]

    def fold(self, *, policy: Policy) -> str:
        limit = policy.max_line_length or _MAX_LINE
        lines = [f"{self.name}: {self._tokens[0]}"]
        for token in self._tokens[1:]:
            if len(lines[-1]) + 1 + len(token) <= limit:
                lines[-1] += " " + token
            else:
                lines.append(" " + token)
        return policy.linesep.join(lines) + policy.linesep


_REGISTRY = HeaderRegistry()
_VERBATIM_CLASSES: dict[str, type[BaseHeader]] = {}


def _verbatim_header(name: str, tokens: Sequence[str]) -> BaseHeader:
    """A header object for ``name`` whose value is ``tokens`` joined by spaces."""
    value = " ".join(tokens)
    if not value.isascii() or len(name) + 2 + len(value) > _MAX_LINE:
        raise ValueError(f"{name} must be ASCII and at most {_MAX_LINE - len(name) - 2} characters long")
    cls = _VERBATIM_CLASSES.get(name.lower())
    if cls is None:
        # The registry's class derives from the stdlib header class and
        # BaseHeader; listing those bases directly keeps the header picklable.
        bases = _REGISTRY[name].__bases__
        cls = _VERBATIM_CLASSES[name.lower()] = type(f"_Verbatim{bases[0].__name__}", (_Verbatim, *bases), {})
    header = cls(name, value)
    header._tokens = tuple(tokens)  # type: ignore[attr-defined]
    return header


def _unsubscribe_uris(value: str) -> list[str]:
    """Split a List-Unsubscribe value into its ``<URI>`` parts, or raise ValueError."""
    if "\r" in value or "\n" in value:
        raise ValueError("List-Unsubscribe value contains a line break")
    if not value.isascii():
        raise ValueError(f"List-Unsubscribe must be ASCII; percent-encode other characters in the URI: {value!r}")
    uris: list[str] = []
    position = 0
    while position < len(value):
        match = _UNSUBSCRIBE_URI.match(value, position)
        if match is None:
            raise ValueError(
                f"List-Unsubscribe must be one or more <URI> separated by commas, without spaces: {value!r}"
            )
        uris.append(match.group(1))
        position = match.end()
    if not uris:
        raise ValueError("List-Unsubscribe is empty")
    return uris


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
            header (RFC 2369), e.g. ``"<https://example.org/u?id=42>"``: one
            or more ASCII URIs in angle brackets, separated by commas. It is
            written as given (never RFC 2047-encoded), folded only between
            URIs, and may be up to 980 characters long.
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
        uris = _unsubscribe_uris(list_unsubscribe)
        msg["List-Unsubscribe"] = _verbatim_header("List-Unsubscribe", [u + "," for u in uris[:-1]] + uris[-1:])
        if one_click_unsubscribe:
            if not any(u.lower().startswith("<https:") for u in uris):
                raise ValueError("one-click unsubscribe (RFC 8058) requires an <https://...> List-Unsubscribe URI")
            msg["List-Unsubscribe-Post"] = _verbatim_header("List-Unsubscribe-Post", ["List-Unsubscribe=One-Click"])
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


def needs_smtputf8(message: EmailMessage) -> bool:
    """Whether an envelope address of ``message`` has non-ASCII characters.

    The envelope addresses are the ones smtplib takes from the headers:
    ``Sender`` (or ``From``), ``To``, ``Cc`` and ``Bcc``. Such a message can
    only be sent to a server that offers SMTPUTF8 (RFC 6531), and its headers
    are written as UTF-8 (RFC 6532) instead of RFC 2047 encoded words.
    """
    origin = "Sender" if "Sender" in message else "From"
    fields = [str(message[name]) for name in (origin, "To", "Cc", "Bcc") if message[name] is not None]
    return not all(address.isascii() for _, address in getaddresses(fields))


def wire_bytes(message: EmailMessage, *, keep_bcc: bool = False) -> bytes:
    """Serialize ``message`` as it is sent over SMTP.

    The result has CRLF line endings and no ``Bcc`` header (unless
    ``keep_bcc``), and body lines starting with "From " are left unchanged.
    Headers are UTF-8 when :func:`needs_smtputf8` is true, as smtplib writes
    them, and ASCII otherwise.

    Raises:
        email.errors.MessageError: a header folds into a line that does not
            start with whitespace, which would cut the header block short or
            add a header. Python 3.13 and later refuse to write these; this
            check applies on every version.
        Exception: anything else the standard library raises for a header it
            cannot write, such as ``UnicodeEncodeError``.
    """
    flat = copy.copy(message)  # deleting a header gives the copy its own header list
    if not keep_bcc:
        del flat["Bcc"]
        del flat["Resent-Bcc"]
    policy = message.policy.clone(linesep="\r\n")
    if isinstance(policy, EmailPolicy) and needs_smtputf8(message):
        policy = policy.clone(utf8=True)
    for part in flat.walk():
        for name, value in part.raw_items():
            folded = policy.fold_binary(name, value).removesuffix(b"\r\n")
            if _NEWLINE_WITHOUT_FWSP.search(folded):
                raise MessageError(f"{name}: the header folds into a line that does not start with a space: {folded!r}")
    buffer = io.BytesIO()
    BytesGenerator(buffer, mangle_from_=False, policy=policy).flatten(flat)
    return buffer.getvalue()
