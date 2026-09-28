# Recipient data

## Accepted sources

| Source | Notes |
| --- | --- |
| `.csv` file | First row is the header. UTF-8, with or without a BOM (Excel's "CSV UTF-8" works). Blank lines are skipped. |
| `.json` file | A top-level array of objects: `[{"email": "...", ...}, ...]`. Nested objects or arrays are rejected. |
| list of dicts | e.g. rows you built in Python. |
| pandas `DataFrame` | Converted with `to_dict("records")`. `NaN` becomes an empty string. |

File extensions are case-insensitive. All values become strings, and missing
or `null` values become `""`. Column names have surrounding spaces removed.

If your file isn't UTF-8 (for example, an older Excel export in Windows-1252),
pass `load_recipients(path, encoding="cp1252")`, or better, re-save it as
UTF-8.

## What validation checks

Before any message is built or sent, every row is checked:

| Check | Severity |
| --- | --- |
| The address column exists | error |
| Every placeholder in every template (and the unsubscribe header, strata and variant columns) is a column | error, with a "did you mean" suggestion |
| Each address is present and syntactically valid | error |
| No address appears twice (case-insensitive, display names ignored) | error, unless `allow_duplicates=True` |
| Attachment files exist | error |
| Rows with an unquoted comma have more cells than the header | error when loading |
| A placeholder's value is empty | warning |

Errors raise `DataError`. Its `.issues` attribute lists every problem, not
just the first, so you can fix them all in one pass.

!!! note "What address validation can and can't do"
    The check is **syntactic**: one `@`, no spaces, a dotted domain, and
    acceptance by Python's RFC 5322 address parser. It catches the typos that
    matter in practice (`ann@gmail`, `ann gmail.com`, `ann@gmail,com`). It
    can't tell whether a mailbox exists. Only delivery (and bounces) can,
    and "verifying" addresses by probing mail servers is unreliable and
    frowned upon by providers.

## Address normalization

For de-duplication, exclusion lists and resume, addresses are compared after
removing any display name and lower-casing: `"Ann Lee" <Ann.Lee@Example.org>`
matches `ann.lee@example.org`. The email standard technically allows
case-sensitive local parts, but no mainstream provider uses them, and treating
two spellings as two people risks the worse error, a duplicate send.

Plus-addressing (`ann+news@`) and Gmail dots are **not** folded, because
these can be genuinely different mailboxes on other providers.
