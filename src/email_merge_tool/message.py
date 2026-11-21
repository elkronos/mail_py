"""Turning a rendered template into a standards-compliant MIME message."""

from __future__ import annotations

import base64
import copy
import functools
import io
import mimetypes
import quopri
import re
import unicodedata
import urllib.parse
from collections.abc import Sequence
from email.errors import MessageError
from email.generator import BytesGenerator
from email.headerregistry import BaseHeader, HeaderRegistry
from email.message import EmailMessage, Message
from email.policy import EmailPolicy, Policy
from email.policy import default as _default_policy
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Any

from .templates import RenderedMessage

__all__ = ["POLICY", "attachment_name", "attachment_type", "build_message", "needs_smtputf8", "wire_bytes"]

# RFC 5322 section 2.1.1: a header line, name included, may not exceed 998 characters.
_MAX_LINE = 998

# RFC 2045 section 6.7: quoted-printable lines are at most 76 characters, and
# RFC 2047 section 2: so are header lines that hold encoded words.
_LINE_LENGTH = 76

#: The policy messages are built with: the standard ``email.policy.default``
#: with 76-character lines, so quoted-printable and base64 bodies and folded
#: headers meet RFC 2045 and RFC 2047 on every Python version.
POLICY = _default_policy.clone(max_line_length=_LINE_LENGTH)

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
    if not value.isascii() or any(len(name) + 2 + len(token) > _MAX_LINE for token in tokens):
        raise ValueError(f"{name} must be ASCII, with no part longer than {_MAX_LINE - len(name) - 2} characters")
    cls = _VERBATIM_CLASSES.get(name.lower())
    if cls is None:
        # The registry's class derives from the stdlib header class and
        # BaseHeader; listing those bases directly keeps the header picklable.
        bases = _REGISTRY[name].__bases__
        cls = _VERBATIM_CLASSES[name.lower()] = type(f"_Verbatim{bases[0].__name__}", (_Verbatim, *bases), {})
    header = cls(name, value)
    header._tokens = tuple(tokens)  # type: ignore[attr-defined]
    return header


# Attachment types by file extension, so a file gets the same type on every
# machine and Python version. Other extensions fall back to the standard
# library's built-in table (never the system's mime.types), then to
# application/octet-stream.
_ATTACHMENT_TYPES = {
    ".7z": "application/x-7z-compressed",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # message/rfc822 may not be base64-encoded (RFC 2046 section 5.2.1), and
    # rewriting the attached message to 7bit would change it.
    ".eml": "application/octet-stream",
    ".epub": "application/epub+zip",
    ".gif": "image/gif",
    ".gz": "application/gzip",
    ".heic": "image/heic",
    ".htm": "text/html",
    ".html": "text/html",
    ".ics": "text/calendar",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".m4a": "audio/mp4",
    ".md": "text/markdown",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rar": "application/vnd.rar",
    ".rtf": "application/rtf",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webp": "image/webp",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
    ".zip": "application/zip",
}

# RFC 2231 attribute characters that need no percent-encoding.
_ATTR_CHARS = "!#$&+-.^_`|~"


def attachment_name(path: str | Path) -> str:
    """The file name an attachment is sent under.

    It is the file's own name with control characters, quotes, backslashes and
    slashes replaced by ``_``, so a name cannot break the header it is written
    in or point into a directory when the recipient saves it.
    """
    name = Path(path).name
    return "".join("_" if unicodedata.category(c) in ("Cc", "Cf") or c in '"\\/' else c for c in name)


@functools.cache
def _builtin_types() -> mimetypes.MimeTypes:
    return mimetypes.MimeTypes()  # the standard library's own table, without the system's files


def attachment_type(name: str) -> str:
    """The MIME type an attachment named ``name`` is sent as."""
    suffix = Path(name).suffix.lower()
    if suffix in _ATTACHMENT_TYPES:
        return _ATTACHMENT_TYPES[suffix]
    ctype, encoding = _builtin_types().guess_type(name)
    if ctype is None or encoding is not None:  # unknown, or compressed (e.g. .tar.bz2)
        return "application/octet-stream"
    return ctype


def _attach(msg: EmailMessage, data: bytes, name: str) -> None:
    """Attach ``data`` as ``name``, with plain parameters when the name allows.

    The standard library writes a long or non-ASCII file name only in RFC 2231
    pieces (``filename*0*=``), which older clients show as "part1". An ASCII
    name is written as a plain quoted ``filename=`` and ``name=``. Any other
    name is a single ``filename*=utf-8''...`` (RFC 2231) plus an RFC 2047
    ``name=`` on the Content-Type, which is what Thunderbird sends.
    """
    ctype = attachment_type(name)
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(data, maintype=maintype, subtype=subtype)
    *_, part = msg.iter_parts()
    params = [f"{ctype};"]
    if maintype == "text":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            params.append('charset="utf-8";')
    if name.isascii() and name.isprintable() and "=?" not in name:
        params.append(f'name="{name}"')
        filename = f'filename="{name}"'
    else:
        words = []
        encoded = name.encode("utf-8")
        position = 0
        while position < len(encoded):
            end = min(position + 45, len(encoded))
            while end < len(encoded) and encoded[end] & 0xC0 == 0x80:  # not inside a character
                end -= 1
            words.append(f"=?utf-8?b?{base64.b64encode(encoded[position:end]).decode('ascii')}?=")
            position = end
        params.append(f'name="{" ".join(words)}"')
        filename = "filename*=utf-8''" + urllib.parse.quote(name, safe=_ATTR_CHARS)
    part.replace_header("Content-Type", _verbatim_header("Content-Type", params))
    part.replace_header("Content-Disposition", _verbatim_header("Content-Disposition", ["attachment;", filename]))


def _encoded_subject(subject: str) -> BaseHeader:
    """A non-ASCII Subject as RFC 2047 base64 encoded words, folded between words.

    Python 3.10 to 3.12 drop a space when they fold a long subject that mixes
    ASCII words with encoded ones (emoji, accented names), and recipients see
    "openZoë". Encoding the whole subject here gives the same bytes on every
    version: each encoded word holds whole characters, the whitespace between
    encoded words is not part of the text (RFC 2047 section 6.2), and every
    line stays within 76 characters.
    """
    words: list[str] = []
    room = _LINE_LENGTH - len("Subject: ")
    chunk = b""
    for char in subject:
        data = char.encode("utf-8")
        if chunk and 12 + 4 * -(-(len(chunk) + len(data)) // 3) > room:  # =?utf-8?b?...?= around the base64
            words.append(f"=?utf-8?b?{base64.b64encode(chunk).decode('ascii')}?=")
            chunk, room = b"", _LINE_LENGTH - 1
        chunk += data
    words.append(f"=?utf-8?b?{base64.b64encode(chunk).decode('ascii')}?=")
    return _verbatim_header("Subject", words)


def _transfer_encoding(text: str) -> str:
    """The Content-Transfer-Encoding for a UTF-8 text part.

    ASCII text is 7bit when no line exceeds 76 characters and quoted-printable
    otherwise, never base64: filters such as rspamd penalize base64 text that
    did not need it. Other text is quoted-printable or base64, whichever is
    shorter. Bodies are never 8bit, so they need no 8BITMIME and survive relays
    that convert 8-bit mail (which breaks DKIM signatures).
    """
    data = text.encode("utf-8")
    if data.isascii():
        short = all(len(line) <= _LINE_LENGTH for line in data.splitlines())
        return "7bit" if short and b"\0" not in data else "quoted-printable"
    return "quoted-printable" if len(quopri.encodestring(data)) <= len(base64.encodebytes(data)) else "base64"


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
    ``multipart/alternative`` HTML part. It uses :data:`POLICY`. Text parts
    are UTF-8 in ``7bit``, quoted-printable or base64 (never ``8bit``), and a
    non-ASCII subject is written as RFC 2047 encoded words, so the bytes are
    the same on every Python version.

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
        attachments: Files to attach. The MIME type comes from the file
            extension (see :func:`attachment_type`), and text files that are
            valid UTF-8 are labelled ``charset="utf-8"``.
    """
    msg = EmailMessage(policy=POLICY)
    from_value = rendered.headers.get("From") or sender
    msg["From"] = from_value
    msg["To"] = rendered.to
    for name in ("Cc", "Bcc", "Reply-To"):
        if rendered.headers.get(name):
            msg[name] = rendered.headers[name]
    msg["Subject"] = rendered.subject if rendered.subject.isascii() else _encoded_subject(rendered.subject)
    msg["Date"] = formatdate(localtime=True)
    domain = parseaddr(from_value)[1].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    if list_unsubscribe:
        uris = _unsubscribe_uris(list_unsubscribe)
        if len("List-Unsubscribe: ") + len(", ".join(uris)) > _MAX_LINE:
            raise ValueError(f"List-Unsubscribe is longer than the {_MAX_LINE - 18} characters a header line allows")
        msg["List-Unsubscribe"] = _verbatim_header("List-Unsubscribe", [u + "," for u in uris[:-1]] + uris[-1:])
        if one_click_unsubscribe:
            if not any(u.lower().startswith("<https:") for u in uris):
                raise ValueError("one-click unsubscribe (RFC 8058) requires an <https://...> List-Unsubscribe URI")
            msg["List-Unsubscribe-Post"] = _verbatim_header("List-Unsubscribe-Post", ["List-Unsubscribe=One-Click"])
    elif one_click_unsubscribe:
        raise ValueError("one_click_unsubscribe requires list_unsubscribe")
    msg.set_content(rendered.text, cte=_transfer_encoding(rendered.text))
    if rendered.html is not None:
        msg.add_alternative(rendered.html, subtype="html", cte=_transfer_encoding(rendered.html))
    for item in attachments:
        _attach(msg, Path(item).read_bytes(), attachment_name(item))
    return msg


def needs_smtputf8(message: Message) -> bool:
    """Whether an envelope address of ``message`` has non-ASCII characters.

    The envelope addresses are the ones smtplib takes from the headers:
    ``Sender`` (or ``From``), ``To``, ``Cc`` and ``Bcc``. Such a message can
    only be sent to a server that offers SMTPUTF8 (RFC 6531), and its headers
    are written as UTF-8 (RFC 6532) instead of RFC 2047 encoded words.
    """
    origin = "Sender" if "Sender" in message else "From"
    fields = [str(message[name]) for name in (origin, "To", "Cc", "Bcc") if message[name] is not None]
    return not all(address.isascii() for _, address in getaddresses(fields))


def wire_bytes(message: Message, *, keep_bcc: bool = False, utf8: bool | None = None, seven_bit: bool = False) -> bytes:
    """Serialize ``message`` as it is sent over SMTP.

    The result has CRLF line endings and no ``Bcc`` header, and body lines
    starting with "From " are left unchanged (smtplib's ``send_message``
    writes them as ">From "). Preflight, the SMTP transport and the dry run's
    ``.eml`` files all use this function, so validation checks the bytes that
    are sent.

    Args:
        message: The message.
        keep_bcc: Keep the ``Bcc`` header, for a preview.
        utf8: Write headers as UTF-8 (RFC 6532), for a server that offers
            SMTPUTF8. ``None`` writes UTF-8 when :func:`needs_smtputf8` is
            true and ASCII otherwise.
        seven_bit: Re-encode 8-bit text parts as base64 or quoted-printable,
            for a server that does not offer 8BITMIME.

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
    if utf8 is None:
        utf8 = isinstance(message.policy, EmailPolicy) and needs_smtputf8(message)
    overrides: dict[str, Any] = {"linesep": "\r\n"}
    if utf8:
        overrides["utf8"] = True
    if seven_bit:
        overrides["cte_type"] = "7bit"
    policy = message.policy.clone(**overrides)
    for part in flat.walk():
        for name, value in part.raw_items():
            folded = policy.fold_binary(name, value).removesuffix(b"\r\n")
            if _NEWLINE_WITHOUT_FWSP.search(folded):
                raise MessageError(f"{name}: the header folds into a line that does not start with a space: {folded!r}")
    buffer = io.BytesIO()
    BytesGenerator(buffer, mangle_from_=False, policy=policy).flatten(flat)
    return buffer.getvalue()
