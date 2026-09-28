# Changelog

## 0.2.0

A rewrite. Version 0.1 could not be imported and its placeholder substitution
never worked. See `docs/review.md` for the full list of problems fixed.

### Added
- `email-merge` command-line tool (`python -m email_merge_tool`), with dry run by default and a confirmation prompt before sending.
- Preflight validation of all recipients, placeholders and attachments before anything is sent, with "did you mean" suggestions.
- Template files with a header block (`Subject`, `From`, `Reply-To`, `Cc`, `Bcc`); text, HTML, or both.
- HTML escaping of merged values, and header-injection protection.
- `multipart/alternative` messages with an automatic plain-text part; `Date` and `Message-ID` headers.
- `List-Unsubscribe` and RFC 8058 one-click unsubscribe headers.
- Attachments, shared or per recipient.
- JSON Lines send log, `resume`, `limit`, and exclusion lists for reminder waves.
- Reproducible, stratified random assignment of templates (`seed`, `strata`, `weights`), with an assignment table export.
- SMTP transport with verified TLS, STARTTLS, retries with backoff, and reconnection; presets for Gmail, Outlook.com and Microsoft 365.
- Dry-run transport that writes `.eml` files.
- pandas DataFrame input; UTF-8 BOM handling for Excel CSV files.
- Documentation site (MkDocs), three walkthroughs, runnable examples, CI.

### Changed
- The package is now importable as `email_merge_tool` (src layout, `pyproject.toml`). Python 3.10+.
- `mail_merge()` has a new signature. Its first two arguments are templates and recipients, and sending requires `dry_run=False` and a `transport`.
- Outlook uses STARTTLS on port 587 (previously port 465, which does not work).
- The library no longer configures logging or writes `email_log.txt` on import.

### Removed
- `send_error_email` (a placeholder that only logged).

## 0.1.0

Initial version.
