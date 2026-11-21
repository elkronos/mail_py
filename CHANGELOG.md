# Changelog

## Unreleased

### Added
- Templates may set `Sender`, provider and list headers such as `Feedback-ID`, `List-Id`, `Auto-Submitted` and `Precedence`, and any `X-` header, such as `X-SES-CONFIGURATION-SET` or `X-PM-Message-Stream`. `mail_merge(extra_headers=...)` and `email-merge --header 'Name: value'` add headers to every message. Placeholders are allowed, and ASCII values are written as given. Headers the tool writes itself (`To`, `Date`, `Message-ID`, `MIME-Version`, `Content-*`, `List-Unsubscribe`) are refused with what to use instead, and a misspelled header name gets a suggestion.
- A run stops at once when the server refuses the account rather than one message: a sending quota (Gmail's `5.4.5`, Microsoft's `SubmissionQuotaExceeded`), a `4.7.x` or `5.7.x` reply about a rate or limit, a login asked for while sending (530, 535), or a refused sender address. It raises the new `AccountRefusedError` (a `FatalSendError`), and the message that met the refusal is logged as `failed` so resume sends it. Before, the run tried every remaining recipient and logged each as failed.
- `mail_merge(max_consecutive_failures=5)` and `email-merge --max-failures N` stop a run after N failed messages in a row; refusals of unknown addresses or full mailboxes (`5.1.x`, `5.2.x`) do not count, and `0` turns the check off. After a stop mid-run, the CLI says to re-run with `--resume`.
- `PermanentSendError` and `AccountRefusedError` carry the server's reply as `smtp_code` and `smtp_error`.
- Every `FatalSendError` raised by `mail_merge` carries the partial `MergeReport` as `exc.report`, so a caller without a log can tell who was sent the message before the stop. The CLI prints how far a stopped run got. Sending without a log adds a warning to the report.
- New `TransportError` (a `FatalSendError`): an exception from a transport that is not one of the send errors stops the run with it, keeps the original as `__cause__`, and logs the message being sent as `uncertain`. Such exceptions used to escape unlogged and without the report.

### Fixed
- A CSV with an unbalanced double quote no longer merges the rows below it into one cell, which dropped those recipients and mailed their data to one person. Quoting is now strict, and a cell that has swallowed other rows is an error that names the line.
- Exclusion lists are validated entry by entry. A line such as `ann@example.org, responded`, a list of file names, records, or a DataFrame used to exclude nobody without any warning; files and records now work, and an entry that is not one valid address stops the run with its line number. A warning is shown when no excluded address is in the recipient data.
- Address validation, de-duplication, exclusion and resume now use the parser that builds the `To` header. Values such as `ann@example.org extra` validated with a key that differed from the mailbox the message went to, which bypassed exclusion lists and allowed duplicate sends. Overlong or deeply nested input no longer crashes the parser.
- A send that reuses a log already recording sent messages is refused unless it resumes, so repeating a command with the same log cannot email everyone again. Resuming from a log that does not exist, or whose addresses are all missing from the data, is an error. A run locks its log while sending, so two runs cannot use one log at once. A torn last line in the log no longer swallows the next record. Dry runs no longer create the log file.
- A connection lost, or a reply cut short or garbled, after the whole message was written is no longer retried, since the server may already have the message. The row is logged as `uncertain` and resume skips it; Ctrl-C while waiting for that reply logs it the same way. Failures before the message is fully written are still retried. The reply to the message content is awaited for up to 10 minutes (RFC 5321).
- Body lines that start with `From ` reach recipients unchanged. smtplib sent them as `>From `, which the `.eml` preview did not show. The tool now flattens each message itself, removing `Bcc` and choosing the envelope as before. Non-ASCII text in a message built elsewhere is declared with `BODY=8BITMIME`, or encoded as base64 for a server that does not offer 8BITMIME.
- Long `List-Unsubscribe` values, such as links with a token, are written as given instead of RFC 2047-encoded, which no mail client could use. The header folds only between URIs. A value that is not ASCII URIs in angle brackets fails validation for its row.
- Validation writes out every message exactly as it will be sent. A header that Python 3.10 to 3.12 fold into a line break without the following space, which ended the header block early on 3.10 and stopped a send partway on 3.11 and 3.12, now fails validation for its row.
- Dry-run `.eml` files are the bytes that go over SMTP, with CRLF line endings, and are UTF-8 when an address needs SMTPUTF8. Such an address used to stop the dry run with `UnicodeEncodeError`.
- A row whose `To` address the server refuses is logged as `failed`, and resume retries it, even when a `Cc` or `Bcc` on the row was accepted. It used to be logged as `sent`, so resume skipped that person for good. A temporary refusal of the `To` address is retried for the refused addresses only, so the others do not get a second copy.
- Temporary (4xx) refusals of a recipient, such as greylisting, are retried with backoff like other temporary failures, as the documentation said. They used to fail at once. After a 421 at that stage the next message no longer waits through a backoff.
- A connection lost mid-run that cannot be reopened at once is retried with backoff, like other temporary failures: a refused connection, a timeout or a `4xx` greeting uses one of the 3 retries. One failed reconnect used to end the run. A refused login or a TLS failure still stops it at once.
- Connection errors name the step that failed. A server without STARTTLS, a server without a login, a TLS handshake or certificate failure and a refused login each get their own message with the option to try (`--security none`, `--no-auth`, `--security starttls`); all of them used to read "could not connect". A connection that fails partway is closed. The documented port-25 relay recipe failed on relays without STARTTLS; `docs/cli.md` now shows both cases.
- Quoted-printable lines are at most 76 characters, as RFC 2045 requires; they were 78.
- Each text part's transfer encoding follows fixed rules instead of the standard library's guess. ASCII text is `7bit`, or quoted-printable when a line is longer than 76 characters, and is never base64; an ASCII newsletter with `=====` rules used to go out as base64, which rspamd penalizes. Other text is quoted-printable or base64, whichever is shorter. Bodies are never `8bit`.
- A non-ASCII subject is written as RFC 2047 encoded words by the tool, with the same bytes on every Python version. Python 3.10 to 3.12 dropped a space when folding long subjects with emoji or accented names, so recipients saw words run together.
- Attachments: `.eml` files go as `application/octet-stream` instead of a base64 `message/rfc822`, which RFC 2046 forbids. UTF-8 text files are labelled `charset="utf-8"`, so accented names in a CSV display correctly. Types come from a table in the tool, so `.docx` or `.xlsx` no longer arrive as `application/octet-stream` on machines without a system type table. ASCII file names are one plain `filename=` parameter at any length, and non-ASCII names add an RFC 2047 `name=`; older clients showed both kinds as "part1". A file name can no longer put a line break into a header. Two attachments with the same name produce a warning.
- The `From` of every message must be exactly one valid address. A `From` placeholder that rendered empty used to send with an empty `From` and a null envelope sender, and one with two addresses went out without the `Sender` header RFC 5322 then requires; both now fail validation for their row. A blank `Subject` fails too, and a template `From` that replaces a different `sender=` is reported as a warning.
- A `Message-ID` is 128 random bits at the `From` domain, converted to ASCII for an internationalized domain. It used to contain the process ID and could fall back to the machine's host name.
- The `Date` header is the time each message is sent, in UTC. It was the time the messages were built, before the first send, so at the default rate every message after about three hours carried a date old enough for SpamAssassin to add points (`DATE_IN_PAST_03_06`), and it showed the sender's UTC offset.
- A dry run into a directory deletes the `.eml` files the previous dry run wrote there, which it lists in `.email-merge-dry-run`, so the directory shows only the messages of the latest run. A reminder preview used to keep files for people who had since replied. Other files are never touched. File names use the address only, in ASCII, shortened with a hash past 80 characters, so a long address or display name no longer stops the dry run with "File name too long", and a file that cannot be written stops the run with an error message instead of a traceback.

### Changed
- With `--security none`, `--port` defaults to 25 instead of 587.
- `email-merge --send` requires `--log FILE`, or `--no-log` to send without one.
- New result and log status `uncertain`, new exception `UncertainSendError`, and `MergeReport.uncertain`. The CLI exits with 1 when any message is uncertain.
- Addresses with non-ASCII characters before the `@` are rejected at validation on every Python version. Python 3.15 had started accepting them, which sent them only through servers offering SMTPUTF8.
- `load_address_list` and `normalize_address` are exported from the package.
- The domain of an address must be letters, digits and hyphens, and invisible characters are rejected, so annotated entries such as `ann@example.org|2024` no longer count as addresses.
- `Issue` has an optional `where` label. The CLI preview shows the send log's full path, and `--resume` needs `--log`.
- Messages logged `uncertain` count toward `limit`. Exclusion lists may be UTF-16 text files, and missing values in a list are skipped.
- `Cc`, `Bcc`, `Reply-To` and `From` values follow the same address rules as recipients, and every header is checked to be writable before sending, so a bad header fails validation instead of stopping a run partway. `SMTPTransport(data_timeout=None)` waits without a limit.
- `min_interval` (`--rate`) spaces failed attempts too, so a run that fails from its first message no longer sends a burst.
- SMTP replies in `detail` read `ann@example.org (550 5.1.1 No such user)` instead of a Python dict of byte strings.
- The tests send through a local SMTP server (aiosmtpd), including STARTTLS and implicit TLS with a test certificate authority. The `dev` extra installs aiosmtpd, hypothesis and trustme.

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
