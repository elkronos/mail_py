# Changelog

## Unreleased

### Fixed
- A CSV with an unbalanced double quote no longer merges the rows below it into one cell, which dropped those recipients and mailed their data to one person. Quoting is now strict, and a cell that has swallowed other rows is an error that names the line.
- Exclusion lists are validated entry by entry. A line such as `ann@example.org, responded`, a list of file names, records, or a DataFrame used to exclude nobody without any warning; files and records now work, and an entry that is not one valid address stops the run with its line number. A warning is shown when no excluded address is in the recipient data.
- Address validation, de-duplication, exclusion and resume now use the parser that builds the `To` header. Values such as `ann@example.org extra` validated with a key that differed from the mailbox the message went to, which bypassed exclusion lists and allowed duplicate sends. Overlong or deeply nested input no longer crashes the parser.
- A send that reuses a log already recording sent messages is refused unless it resumes, so repeating a command with the same log cannot email everyone again. Resuming from a log that does not exist, or whose addresses are all missing from the data, is an error. A run locks its log while sending, so two runs cannot use one log at once. A torn last line in the log no longer swallows the next record. Dry runs no longer create the log file.
- A connection lost, or a reply cut short or garbled, after the whole message was written is no longer retried, since the server may already have the message. The row is logged as `uncertain` and resume skips it; Ctrl-C while waiting for that reply logs it the same way. Failures before the message is fully written are still retried. The reply to the message content is awaited for up to 10 minutes (RFC 5321).

### Changed
- `email-merge --send` requires `--log FILE`, or `--no-log` to send without one.
- New result and log status `uncertain`, new exception `UncertainSendError`, and `MergeReport.uncertain`. The CLI exits with 1 when any message is uncertain.
- Addresses with non-ASCII characters before the `@` are rejected at validation on every Python version. Python 3.15 had started accepting them, which sent them only through servers offering SMTPUTF8.
- `load_address_list` and `normalize_address` are exported from the package.
- The domain of an address must be letters, digits and hyphens, and invisible characters are rejected, so annotated entries such as `ann@example.org|2024` no longer count as addresses.
- `Issue` has an optional `where` label. The CLI preview shows the send log's full path, and `--resume` needs `--log`.
- Messages logged `uncertain` count toward `limit`. Exclusion lists may be UTF-16 text files, and missing values in a list are skipped.
- `Cc`, `Bcc`, `Reply-To` and `From` values follow the same address rules as recipients, and every header is checked to be writable before sending, so a bad header fails validation instead of stopping a run partway. `SMTPTransport(data_timeout=None)` waits without a limit.

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
