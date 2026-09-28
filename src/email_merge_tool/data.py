"""Loading recipient data and validating it before anything is sent."""

from __future__ import annotations

import csv
import difflib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.headerregistry import Address
from email.utils import getaddresses, parseaddr
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

# Pragmatic syntax check: one "@", no whitespace, and a dotted domain.
# This catches typos (missing @, "gmail,com", trailing text) but cannot tell
# whether a mailbox exists; only delivering to it can.
_ADDR = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


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
    """

    row: int | None
    severity: str
    message: str

    def __str__(self) -> str:
        where = f"row {self.row}" if self.row is not None else "data"
        return f"{self.severity}: {where}: {self.message}"


def _stringify(value: object, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise DataError(f"{where}: nested values are not supported ({type(value).__name__})")
    if isinstance(value, float) and value != value:  # NaN, e.g. an empty pandas cell
        return ""
    return str(value)


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
    reader = csv.DictReader(handle)
    header = [h.strip() for h in (reader.fieldnames or [])]
    if not header:
        raise DataError(f"{path}: the CSV file is empty")
    duplicates = sorted({h for h in header if header.count(h) > 1})
    if duplicates:
        raise DataError(f"{path}: duplicate column name(s): {', '.join(duplicates)}")
    if "" in header:
        raise DataError(f"{path}: a column has an empty name")
    rows: list[Record] = []
    for index, row in enumerate(reader, start=1):
        if None in row:  # more cells than header columns: usually an unquoted comma
            raise DataError(f"{path}: row {index} has more fields than the header (unquoted comma?)")
        if not any((v or "").strip() for v in row.values()):
            continue  # skip blank lines
        rows.append({k.strip(): (v or "") for k, v in row.items()})
    return rows


def load_address_list(source: object, *, to_field: str = "email") -> set[str]:
    """Load a set of normalized addresses, e.g. people who already responded.

    ``source`` may be an iterable of addresses, a ``.csv``/``.json`` file with a
    ``to_field`` column, or a ``.txt`` file with one address per line.
    """
    if isinstance(source, (str, Path)) and not Path(source).is_file():
        raise DataError(f"address list not found: {source}")
    if isinstance(source, (str, Path)) and Path(source).suffix.lower() == ".txt":
        lines = Path(source).read_text(encoding="utf-8-sig").splitlines()
        return {normalize_address(line) for line in lines if line.strip() and not line.startswith("#")}
    if isinstance(source, (str, Path)):
        records = load_recipients(source)
        if records and to_field not in records[0]:
            raise DataError(f"{source}: no {to_field!r} column")
        return {normalize_address(r[to_field]) for r in records if r.get(to_field, "").strip()}
    return {normalize_address(str(a)) for a in source if str(a).strip()}  # type: ignore[union-attr]


def normalize_address(value: str) -> str:
    """Canonical form used for de-duplication, exclusion and resume.

    Strips a display name (``"Ann <ann@x.org>"`` -> ``ann@x.org``) and
    lower-cases. The RFC allows case-sensitive local parts, but essentially no
    provider uses them, and treating ``Ann@x.org`` and ``ann@x.org`` as
    different people is the more dangerous mistake (a duplicate send).
    """
    _, addr = parseaddr(value.strip())
    return (addr or value).strip().lower()


def is_valid_address(value: str) -> bool:
    """Syntax check for an address (optionally with a display name).

    This is deliberately a *syntax* check only. It does not look up DNS or
    probe the mailbox; bounces are the only reliable signal of deliverability.
    """
    parsed = getaddresses([value.strip()])
    if len(parsed) != 1:  # "a@x.org, b@x.org" is two people, not one
        return False
    addr = parsed[0][1]
    if not addr or not _ADDR.match(addr):
        return False
    try:
        Address(addr_spec=addr)
    except (ValueError, IndexError):
        return False
    return True


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
        if not is_valid_address(address):
            issues.append(Issue(row, "error", f"invalid email address {address!r}"))
            continue
        key = normalize_address(address)
        if key in seen and not allow_duplicates:
            issues.append(Issue(row, "error", f"duplicate address {address!r} (first seen in row {seen[key]})"))
        seen.setdefault(key, row)
        for name in sorted(needed):
            if not (record.get(name) or "").strip():
                issues.append(Issue(row, "warning", f"empty value for {{{name}}}"))
    return issues
