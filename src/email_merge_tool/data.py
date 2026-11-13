"""Loading recipient data and validating it before anything is sent."""

from __future__ import annotations

import codecs
import csv
import difflib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from email import policy
from email.errors import NonASCIILocalPartDefect, ObsoleteHeaderDefect
from email.headerregistry import Address
from email.utils import getaddresses
from pathlib import Path

__all__ = [
    "Issue",
    "DataError",
    "load_recipients",
    "load_address_list",
    "normalize_address",
    "is_valid_address",
    "validate_recipients",
]

Record = dict[str, str]

# Pragmatic syntax check on the parsed address: a local part without spaces
# or quotes, and a domain made of hostname labels (letters and digits in any
# script, inner hyphens) with a non-numeric last label. This catches typos
# ("gmail,com", "ann@x.org|2024", "'ann@x.org'") but cannot tell whether a
# mailbox exists; only delivering to it can.
_LOCAL_PART = re.compile(r"[^\s@\"]+")
_DOMAIN_LABEL = re.compile(r"[^\W_](?:[^\W_]|-)*")

# Longer values are rejected before parsing: the stdlib address parser recurses
# on nested comments and is slow on long quote runs. Real addresses with a
# display name are far shorter.
_MAX_ADDRESS_LENGTH = 512
_MAX_HEADER_LENGTH = 4096

# One field of a CSV row that is an address on its own.
_LONE_ADDRESS = re.compile(r"<?[^\s@,;<>\"]+@[^\s@,;<>\"]+\.[^\s@,;<>\"]+>?")
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

_ADDRESS_LIST_SUFFIXES = {".txt", ".csv", ".json"}


class DataError(ValueError):
    """Raised when recipient data cannot be loaded or fails validation."""

    def __init__(self, message: str, issues: Sequence[Issue] = ()) -> None:
        super().__init__(message)
        self.issues = list(issues)


@dataclass(frozen=True)
class Issue:
    """A problem found during validation.

    Attributes:
        row: 1-based data row (header excluded), or ``None`` for dataset-level issues.
        severity: ``"error"`` blocks sending; ``"warning"`` is reported only.
        message: Human-readable description.
        where: What the issue is about when it is not a data row, e.g.
            ``"send log"``; defaults to ``"data"``.
    """

    row: int | None
    severity: str
    message: str
    where: str | None = None

    def __str__(self) -> str:
        where = f"row {self.row}" if self.row is not None else (self.where or "data")
        return f"{self.severity}: {where}: {self.message}"


def _is_missing(value: object) -> bool:
    """None, NaN, or a pandas missing-value marker (``pd.NA``, ``pd.NaT``)."""
    if value is None:
        return True
    if isinstance(value, float):
        return value != value  # NaN, e.g. an empty pandas cell
    return type(value).__name__ in {"NAType", "NaTType"}


def _stringify(value: object, where: str) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, (dict, list)):
        raise DataError(f"{where}: nested values are not supported ({type(value).__name__})")
    return str(value)


def _without_invisible(text: str) -> str:
    """Remove zero-width spaces, BOMs and other invisible format characters."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _clean(records: Iterable[Mapping[object, object]], source: str) -> list[Record]:
    cleaned: list[Record] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise DataError(f"{source}: row {index} is a {type(record).__name__}, expected an object/dict")
        cleaned.append(
            {str(k).strip(): _stringify(v, f"{source}: row {index}, field {k!r}") for k, v in record.items()}
        )
    return cleaned


def load_recipients(source: object, *, encoding: str = "utf-8-sig") -> list[Record]:
    """Load recipients as a list of ``{column: str}`` dicts.

    Accepts a path to a ``.csv`` or ``.json`` file, a list (or other iterable)
    of dicts, or a pandas ``DataFrame``. All values are converted to strings,
    missing values become ``""``, and column names are stripped of whitespace.

    The default encoding ``utf-8-sig`` also reads UTF-8 files saved by Excel,
    which begin with a byte-order mark that would otherwise corrupt the first
    column name.
    """
    if isinstance(source, (str, Path)):
        return _load_file(Path(source), encoding)
    if hasattr(source, "to_dict") and hasattr(source, "columns"):  # pandas.DataFrame
        return _clean(source.to_dict(orient="records"), "DataFrame")
    if isinstance(source, Iterable) and not isinstance(source, (bytes, Mapping)):
        return _clean(source, "data")
    raise DataError("recipients must be a CSV/JSON path, a list of dicts, or a pandas DataFrame")


def _load_file(path: Path, encoding: str) -> list[Record]:
    suffix = path.suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise DataError(f"{path}: unsupported file type {suffix!r}; use .csv or .json")
    try:
        with path.open(encoding=encoding, newline="") as handle:
            if suffix == ".json":
                data = json.load(handle)
                if not isinstance(data, list):
                    raise DataError(f"{path}: expected a JSON array of objects at the top level")
                return _clean(data, str(path))
            return _read_csv(handle, path)
    except FileNotFoundError:
        raise DataError(f"recipient file not found: {path}") from None
    except UnicodeDecodeError as exc:
        raise DataError(f"{path} is not valid {encoding}; re-save as UTF-8 or pass encoding=") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from None


def _read_csv(handle: Iterable[str], path: Path) -> list[Record]:
    # strict=True turns an unbalanced quote into an error. Without it, the csv
    # module reads every following line into one cell, so later recipients
    # vanish and their rows are mailed to the person whose cell was opened.
    reader = csv.reader(handle, strict=True)
    try:
        raw_header = next(reader)
    except StopIteration:
        raise DataError(f"{path}: the CSV file is empty") from None
    except csv.Error as exc:
        raise DataError(f"{path}: the header row cannot be read ({exc}); check it for a stray double quote") from None
    if any("\n" in h or "\r" in h for h in raw_header):
        raise DataError(f"{path}: a column name spans several lines; check the header row for a stray double quote")
    header = [h.strip() for h in raw_header]
    if not any(header):
        raise DataError(f"{path}: the CSV file is empty")
    duplicates = sorted({h for h in header if header.count(h) > 1})
    if duplicates:
        raise DataError(f"{path}: duplicate column name(s): {', '.join(duplicates)}")
    if "" in header:
        raise DataError(f"{path}: a column has an empty name")
    # Files written as "email, name" put a space after each comma; then a
    # space after a comma inside a cell is no sign of prose.
    spaced = any(h.startswith(" ") for h in raw_header[1:])
    rows: list[Record] = []
    while True:
        start = reader.line_num + 1  # after any blank lines skipped below
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            hint = (
                "text follows a closing double quote, such as a space before the comma"
                if "expected after" in str(exc)
                else "check that line for an unbalanced double quote"
            )
            raise DataError(f"{path}: the record starting on line {start} cannot be read ({exc}); {hint}") from None
        if not any(c.strip() for c in cells):
            continue  # blank line, or a row of empty cells
        number = len(rows) + 1  # the row number validation reports
        if len(cells) > len(header):  # usually an unquoted comma
            raise DataError(f"{path}: row {number} (line {start}) has more fields than the header (unquoted comma?)")
        cells += [""] * (len(header) - len(cells))
        for name, value in zip(header, cells, strict=True):
            if ("\n" in value or "\r" in value) and any(
                _looks_like_row(line, spaced) for line in _LINE_BREAK.split(value)
            ):
                raise DataError(
                    f"{path}: row {number} (line {start}), column {name!r} holds several lines, and one of them "
                    "looks like another recipient's row; check the quotes around that cell"
                )
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _looks_like_row(line: str, spaced: bool) -> bool:
    """Whether one line inside a CSV cell looks like a CSV row with an address.

    That is the sign of a stray double quote that swallowed the rows after it.
    A spreadsheet row has no space after its commas and has an address as one
    whole field; prose such as "Cc: a@x.org, b@x.org" has spaces after them.
    """
    if "," not in line or "@" not in line:
        return False
    fields = next(csv.reader([line]))
    if len(fields) < 2 or (not spaced and any(f.startswith(" ") for f in fields[1:])):
        return False
    return any("@" in f and _LONE_ADDRESS.fullmatch(f.strip()) for f in fields)


def load_address_list(source: object, *, to_field: str = "email") -> set[str]:
    """Load a set of normalized addresses, e.g. people who already responded.

    ``source`` may be:

    * a ``.txt`` file with one address per line (blank lines, ``#`` comment
      lines and trailing `` # comments`` are ignored, as are trailing commas
      and semicolons left by spreadsheet exports);
    * a ``.csv``/``.json`` file, or a pandas ``DataFrame``, with a
      ``to_field`` column;
    * an iterable whose items are addresses, records (dicts) with a
      ``to_field`` key, or paths to any of the files above.

    Every entry must be one valid address. An entry that is not (for example
    ``"ann@x.org, responded"``) raises :class:`DataError` naming its line,
    because a suppression entry that matches nobody silently suppresses nobody.
    """
    entries: list[tuple[str, str]] = []
    _collect_addresses(source, to_field, entries)
    keys: set[str] = set()
    problems: list[str] = []
    for where, value in entries:
        # Invisible characters copied along with an address are dropped here.
        # A recipient cell containing one fails validation instead, because
        # the message would be addressed with the character in it.
        address, reason = _parse_mailbox(_without_invisible(value))
        if address is None:
            problems.append(f"{where}: {value!r} is not a single valid address ({reason})")
        else:
            keys.add(address.addr_spec.lower())
    if problems:
        shown = problems[:20] + ([f"... and {len(problems) - 20} more"] if len(problems) > 20 else [])
        noun = "entry" if len(problems) == 1 else "entries"
        raise DataError(f"{len(problems)} address list {noun} would exclude nobody:\n  " + "\n  ".join(shown))
    return keys


def _collect_addresses(source: object, to_field: str, entries: list[tuple[str, str]]) -> None:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise DataError(f"address list not found: {source}")
        suffix = path.suffix.lower()
        if suffix == ".txt":
            for number, line in enumerate(_read_text(path).split("\n"), start=1):
                value = _without_invisible(line).strip()
                if not value or value.startswith("#"):
                    continue
                value = re.sub(r"\s+#.*$", "", value).rstrip(",;\t ")
                if value:
                    entries.append((f"{path}, line {number}", value))
            return
        if suffix not in {".csv", ".json"}:
            raise DataError(f"{path}: unsupported address list type {suffix!r}; use .txt, .csv or .json")
        _collect_records(load_recipients(path), to_field, str(path), entries)
        return
    if hasattr(source, "to_dict") and hasattr(source, "columns"):  # pandas.DataFrame
        _collect_records(load_recipients(source), to_field, "DataFrame", entries)
        return
    if isinstance(source, (bytes, Mapping)) or not isinstance(source, Iterable):
        raise DataError("an address list must be a file path, a DataFrame, or a list of addresses")
    for number, item in enumerate(source, start=1):
        if isinstance(item, Mapping):
            if to_field not in item:
                raise DataError(f"address list item {number} has no {to_field!r} key")
            value = _stringify(item[to_field], f"address list item {number}").strip()
            if value:
                entries.append((f"item {number}", value))
        elif isinstance(item, Path):
            _collect_addresses(item, to_field, entries)
        elif _is_missing(item):
            continue
        else:
            if isinstance(item, bytes):
                try:
                    value = item.decode("utf-8").strip()
                except UnicodeDecodeError:
                    raise DataError(f"address list item {number} is not UTF-8 text") from None
            else:
                value = str(item).strip()
            if not value:
                continue
            # A name like "optout@2024.csv" is both a file and a valid-looking
            # address; an existing file wins.
            if Path(value).suffix.lower() in _ADDRESS_LIST_SUFFIXES and (
                Path(value).is_file() or _parse_mailbox(value)[0] is None
            ):
                _collect_addresses(Path(value), to_field, entries)
            else:
                entries.append((f"item {number}", value))


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # Notepad "Unicode", PowerShell 5 ">"
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DataError(f"{path} is not valid UTF-8; re-save it as UTF-8") from None


def _collect_records(records: list[Record], to_field: str, label: str, entries: list[tuple[str, str]]) -> None:
    for number, record in enumerate(records, start=1):
        if to_field not in record:  # a JSON record can lack keys that others have
            hint = difflib.get_close_matches(to_field, record.keys(), n=1)
            suffix = f"; did you mean {hint[0]!r}?" if hint else ""
            where = f"{label}: no {to_field!r} column" if number == 1 else f"{label}, row {number}: no {to_field!r} key"
            raise DataError(where + suffix)
        value = record[to_field].strip()
        if value:
            entries.append((f"{label}, row {number}", value))


def _parse_mailbox(value: str) -> tuple[Address | None, str]:
    """Parse one mailbox exactly as the ``To`` header of an outgoing message is parsed.

    Returns the address and ``""``, or ``None`` and the reason it was rejected.
    Validation, de-duplication, exclusion and resume all use this one parser,
    so the key computed for a value is always the mailbox the message reaches.
    """
    text = value.strip()
    if not text:
        return None, "empty"
    if len(text) > _MAX_ADDRESS_LENGTH:
        return None, f"longer than {_MAX_ADDRESS_LENGTH} characters"
    if "\r" in text or "\n" in text:
        return None, "contains a line break"
    if "@" not in text:
        return None, "no @ sign"
    invisible = next((ch for ch in text if unicodedata.category(ch) == "Cf"), None)
    if invisible is not None:
        return None, f"contains an invisible character (U+{ord(invisible):04X})"
    try:
        header = policy.default.header_factory("To", text)
        groups, defects = header.groups, header.defects
        header.fold(policy=policy.default)  # smtplib writes it this way; deep comment nesting fails here
    except Exception:  # noqa: BLE001 - the stdlib parser raises AttributeError, RecursionError and others on odd input
        return None, "cannot be parsed"
    if any(isinstance(d, NonASCIILocalPartDefect) for d in defects):
        return None, "non-ASCII characters before the @ need SMTPUTF8, which is not supported"
    # An unquoted period in a display name ("Dr. Ann Smith <ann@x.org>") is
    # obsolete syntax that the email package quotes on output; the mailbox is
    # unaffected. Every other defect means the header would not say what the
    # cell says.
    if any(not isinstance(d, ObsoleteHeaderDefect) for d in defects):
        return None, "unexpected text or punctuation around the address"
    if len(groups) != 1 or len(groups[0].addresses) != 1:
        return None, "more than one address"
    if groups[0].display_name is not None:
        return None, "group syntax ('name: address;')"
    address = groups[0].addresses[0]
    problem = _address_problem(address)
    if problem:
        return None, problem
    try:
        delivered = [addr for _, addr in getaddresses([str(header)])]
    except Exception:  # noqa: BLE001
        return None, "cannot be parsed"
    if delivered != [address.addr_spec]:  # smtplib derives the envelope this way
        return None, "the delivery address would differ from the written address"
    return address, ""


def _address_problem(address: Address) -> str:
    """What is wrong with a parsed address, or ``""`` if nothing is."""
    local, domain = address.username, address.domain
    if not local or not _LOCAL_PART.fullmatch(local):
        return "not of the form name@domain.tld"
    if not local.isascii():  # newer Pythons no longer flag this as a defect
        return "non-ASCII characters before the @ need SMTPUTF8, which is not supported"
    if local[0] in "'`" or local[-1] in "'`":  # e.g. Excel's text prefix: 'ann@x.org
        return "a quote mark starts or ends the part before the @"
    labels = domain.split(".")
    if (
        len(labels) < 2
        or labels[-1].isdigit()
        or not all(_DOMAIN_LABEL.fullmatch(label) and not label.endswith("-") for label in labels)
    ):
        return "the part after the @ is not a domain name like example.org"
    return ""


def _valid_address_list(value: str) -> bool:
    """Whether a header value like "A <a@x.org>, b@y.org" is a clean list of valid addresses.

    The value is parsed as the message header will hold it, each parsed address
    must pass the same rules as a recipient address, and the delivery addresses
    smtplib derives from the header must be exactly those addresses.
    """
    if not value.strip() or len(value) > _MAX_HEADER_LENGTH or "\r" in value or "\n" in value:
        return False
    if any(unicodedata.category(ch) == "Cf" for ch in value):
        return False
    try:
        header = policy.default.header_factory("To", value)
        groups, defects = header.groups, header.defects
        addresses = [a for g in groups for a in g.addresses]
        delivered = [addr for _, addr in getaddresses([str(header)])]
        header.fold(policy=policy.default)
    except Exception:  # noqa: BLE001 - the stdlib parser raises several types on odd input
        return False
    if any(not isinstance(d, ObsoleteHeaderDefect) for d in defects):
        return False
    if not addresses or any(g.display_name is not None for g in groups):
        return False
    if any(_address_problem(a) for a in addresses):
        return False
    return delivered == [a.addr_spec for a in addresses]


def normalize_address(value: str) -> str:
    """Canonical form used for de-duplication, exclusion and resume.

    Strips a display name (``"Ann <ann@x.org>"`` -> ``ann@x.org``) and
    lower-cases. The RFC allows case-sensitive local parts, but essentially no
    provider uses them, and treating ``Ann@x.org`` and ``ann@x.org`` as
    different people is the more dangerous mistake (a duplicate send).

    For a valid address the result is the mailbox the message is delivered
    to. An invalid value is returned stripped and lower-cased; it never
    matches a valid address, and validation stops it from being sent.
    """
    address, _ = _parse_mailbox(value)
    if address is None:
        return value.strip().lower()
    return address.addr_spec.lower()


def is_valid_address(value: str) -> bool:
    """Syntax check for one address, optionally with a display name.

    The value is parsed the way the outgoing ``To`` header is, and is valid
    only if that finds exactly one mailbox with a dotted domain and nothing
    else: no trailing text, no group syntax, no second address.

    This is deliberately a *syntax* check only. It does not look up DNS or
    probe the mailbox; bounces are the only reliable signal of deliverability.
    """
    return _parse_mailbox(value)[0] is not None


def validate_recipients(
    records: Sequence[Mapping[str, str]],
    fields: Iterable[str],
    *,
    to_field: str = "email",
    allow_duplicates: bool = False,
) -> list[Issue]:
    """Check every record before sending anything.

    Checks performed:

    * the address column exists, and every address is present and well-formed;
    * no address appears twice (unless ``allow_duplicates``), since a duplicate
      row almost always means a person would be emailed twice;
    * every placeholder used by the template(s) is a column in the data, with a
      "did you mean" suggestion for near misses such as ``First_Name``;
    * empty values for placeholders are reported as warnings (the message
      would render with a blank, e.g. "Dear ,").
    """
    issues: list[Issue] = []
    if not records:
        return [Issue(None, "error", "no recipients to send to")]
    columns = set().union(*(r.keys() for r in records))
    needed = set(fields)
    if to_field not in columns:
        hint = difflib.get_close_matches(to_field, columns, n=1)
        suffix = f"; did you mean {hint[0]!r}?" if hint else ""
        issues.append(Issue(None, "error", f"address column {to_field!r} not found{suffix}"))
    for missing in sorted(needed - columns):
        hint = difflib.get_close_matches(missing, columns, n=1)
        suffix = f"; did you mean {hint[0]!r}?" if hint else ""
        issues.append(Issue(None, "error", f"template uses {{{missing}}} but there is no such column{suffix}"))
    if any(i.severity == "error" for i in issues):
        return issues

    seen: dict[str, int] = {}
    for row, record in enumerate(records, start=1):
        address = (record.get(to_field) or "").strip()
        if not address:
            issues.append(Issue(row, "error", f"empty {to_field!r}"))
            continue
        parsed, reason = _parse_mailbox(address)
        if parsed is None:
            issues.append(Issue(row, "error", f"invalid email address {address!r} ({reason})"))
            continue
        key = parsed.addr_spec.lower()
        if key in seen and not allow_duplicates:
            issues.append(Issue(row, "error", f"duplicate address {address!r} (first seen in row {seen[key]})"))
        seen.setdefault(key, row)
        for name in sorted(needed):
            if not (record.get(name) or "").strip():
                issues.append(Issue(row, "warning", f"empty value for {{{name}}}"))
    return issues
