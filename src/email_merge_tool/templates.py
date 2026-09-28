"""Loading and rendering message templates.

Placeholder syntax
------------------
* ``{field}`` is replaced with the recipient's value for ``field``. Field names
  are Python-style identifiers (letters, digits, underscore; not starting with
  a digit) and are matched **exactly** (case-sensitive).
* ``{{field}}`` produces a literal ``{field}`` in the output.
* Any other brace (for example CSS such as ``p { margin: 0 }``) is left alone,
  so HTML templates with inline styles work without escaping.

In HTML bodies every substituted value is HTML-escaped, so a recipient whose
name is ``<script>`` cannot inject markup. Header values (Subject, Reply-To,
...) are rejected if a substituted value contains a line break, which blocks
header injection.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

__all__ = [
    "Template",
    "RenderedMessage",
    "TemplateError",
    "load_template",
    "html_to_text",
    "placeholders",
    "fill",
]

_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_]\w*)\}\}|\{([A-Za-z_]\w*)\}")

#: Headers a template file may set in its header block.
ALLOWED_HEADERS = ("Subject", "From", "Reply-To", "Cc", "Bcc")
_ALLOWED_LOWER = {h.lower(): h for h in ALLOWED_HEADERS}


class TemplateError(ValueError):
    """Raised for malformed templates or values that cannot be rendered."""


def placeholders(text: str | None) -> set[str]:
    """Names of the ``{field}`` placeholders in ``text`` (escaped ``{{x}}`` excluded)."""
    if not text:
        return set()
    return {m.group(2) for m in _PLACEHOLDER.finditer(text) if m.group(2)}


def fill(text: str, values: Mapping[str, object], *, escape_html: bool = False) -> str:
    """Replace ``{field}`` placeholders in ``text`` with ``values``.

    Raises:
        TemplateError: if a placeholder has no entry in ``values``.
    """

    def repl(match: re.Match[str]) -> str:
        literal, name = match.group(1), match.group(2)
        if literal is not None:
            return "{" + literal + "}"
        if name not in values:
            raise TemplateError(f"no value for placeholder {{{name}}}")
        value = values[name]
        value = "" if value is None else str(value)
        return html.escape(value) if escape_html else value

    return _PLACEHOLDER.sub(repl, text)


@dataclass(frozen=True)
class RenderedMessage:
    """A template filled in for one recipient (not yet a MIME message)."""

    to: str
    subject: str
    text: str
    html: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Template:
    """A message template: a subject, a plain-text and/or HTML body, and headers.

    At least one of ``text`` or ``html`` is required. When only ``html`` is
    given, a plain-text alternative is generated automatically, because
    multipart/alternative messages are more accessible (screen readers,
    text-only clients) and are treated better by spam filters than HTML-only
    mail.

    Args:
        subject: Subject line; may contain placeholders.
        text: Plain-text body; may contain placeholders.
        html: HTML body; may contain placeholders (values are HTML-escaped).
        headers: Extra headers from :data:`ALLOWED_HEADERS` other than Subject
            (``From``, ``Reply-To``, ``Cc``, ``Bcc``); may contain placeholders.
        name: Label used for this template in logs and experiment assignments.
    """

    subject: str
    text: str | None = None
    html: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    name: str = "default"

    def __post_init__(self) -> None:
        if self.text is None and self.html is None:
            raise TemplateError("a template needs a text body, an HTML body, or both")
        if not self.subject or not self.subject.strip():
            raise TemplateError(f"template {self.name!r} has an empty subject")
        normalized: dict[str, str] = {}
        for key, value in self.headers.items():
            canonical = _ALLOWED_LOWER.get(key.lower())
            if canonical is None or canonical == "Subject":
                allowed = ", ".join(h for h in ALLOWED_HEADERS if h != "Subject")
                raise TemplateError(f"unsupported header {key!r}; allowed: {allowed}")
            normalized[canonical] = value
        object.__setattr__(self, "headers", normalized)

    @property
    def fields(self) -> set[str]:
        """All placeholder names used anywhere in this template."""
        found = placeholders(self.subject) | placeholders(self.text) | placeholders(self.html)
        for value in self.headers.values():
            found |= placeholders(value)
        return found

    def render(self, record: Mapping[str, object], *, to_field: str = "email") -> RenderedMessage:
        """Fill in the template for one recipient record.

        Raises:
            TemplateError: if a placeholder has no value, the recipient address
                is missing, or a header would contain a line break.
        """
        if to_field not in record or not str(record[to_field] or "").strip():
            raise TemplateError(f"record has no value in the address column {to_field!r}")
        to = str(record[to_field]).strip()
        subject = fill(self.subject, record, escape_html=False)
        headers = {k: fill(v, record, escape_html=False) for k, v in self.headers.items()}
        for name, value in [("To", to), ("Subject", subject), *headers.items()]:
            if "\r" in value or "\n" in value:
                raise TemplateError(f"{name} header would contain a line break (possible header injection)")
        html_body = fill(self.html, record, escape_html=True) if self.html is not None else None
        if self.text is not None:
            text_body = fill(self.text, record, escape_html=False)
        else:
            text_body = html_to_text(html_body or "")
        return RenderedMessage(to=to, subject=subject, text=text_body, html=html_body, headers=headers)


def _split_header_block(raw: str, source: str) -> tuple[dict[str, str], str]:
    """Split an optional leading ``Header: value`` block from the body.

    A header block is recognised only when the first line starts with one of
    :data:`ALLOWED_HEADERS`, so a body that begins with "Note: ..." is not
    misread. Inside a header block every line must be an allowed header, which
    turns typos such as ``Subjcet:`` into clear errors instead of silent body text.
    """
    lines = raw.splitlines(keepends=True)
    first = lines[0].split(":", 1)[0].strip().lower() if lines and ":" in lines[0] else ""
    if first not in _ALLOWED_LOWER:
        return {}, raw
    headers: dict[str, str] = {}
    for index, line in enumerate(lines):
        if not line.strip():
            return headers, "".join(lines[index + 1 :])
        key, sep, value = line.partition(":")
        canonical = _ALLOWED_LOWER.get(key.strip().lower())
        if not sep or canonical is None:
            raise TemplateError(
                f"{source}, line {index + 1}: expected one of "
                f"{', '.join(ALLOWED_HEADERS)} or a blank line before the body, got {line.strip()!r}"
            )
        headers[canonical] = value.strip()
    raise TemplateError(f"{source}: header block must be followed by a blank line and a body")


def load_template(
    path: str | Path,
    *,
    html_path: str | Path | None = None,
    subject: str | None = None,
    name: str | None = None,
) -> Template:
    """Load a template from a file.

    The file may start with a header block followed by a blank line::

        Subject: Your invitation, {first_name}
        Reply-To: study-team@example.org

        Dear {first_name}, ...

    Files ending in ``.html`` / ``.htm`` are treated as HTML bodies; anything
    else is plain text. Pass ``html_path`` to pair a plain-text template with
    an HTML version (headers are read from ``path``).

    Args:
        path: Template file (UTF-8).
        html_path: Optional HTML body to send alongside the text body.
        subject: Subject to use if the file has no ``Subject:`` header.
        name: Template label; defaults to the file name without extension.
    """
    path = Path(path)
    raw = _read(path)
    headers, body = _split_header_block(raw, str(path))
    subj = headers.pop("Subject", None) or subject
    if not subj:
        raise TemplateError(f"{path}: no subject; add a 'Subject:' line at the top or pass subject=")
    is_html = path.suffix.lower() in {".html", ".htm"}
    text_body, html_body = (None, body) if is_html else (body, None)
    if html_path is not None:
        if is_html:
            raise TemplateError("html_path was given but the main template is already HTML")
        html_headers, html_body = _split_header_block(_read(Path(html_path)), str(html_path))
        if html_headers:
            raise TemplateError(f"{html_path}: put headers in {path.name}, not in the HTML file")
    return Template(subject=subj, text=text_body, html=html_body, headers=headers, name=name or path.stem)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise TemplateError(f"template file not found: {path}") from None
    except UnicodeDecodeError as exc:
        raise TemplateError(f"{path} is not valid UTF-8: {exc}") from None


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            self._href.append(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            href = self._href.pop()
            if href and not href.startswith(("#", "mailto:")):
                self.parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(markup: str) -> str:
    """Produce a readable plain-text version of an HTML body.

    Links are kept as ``text (url)`` so the plain-text alternative remains usable.
    """
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts)
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
