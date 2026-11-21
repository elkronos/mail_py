# Sending safely

## Nothing is sent by default

- **Python:** `mail_merge(..., dry_run=True)` is the default. Sending requires
  both `dry_run=False` **and** a `transport`.
- **CLI:** sending requires `--send`, then typing `yes` at the confirmation
  prompt (`--yes` skips the prompt, for scripts).

## Providers

| `--service` | Host | Port and security | Login |
| --- | --- | --- | --- |
| `gmail` | smtp.gmail.com | 465, implicit TLS | Your address and an **app password** |
| `outlook` | smtp-mail.outlook.com | 587, STARTTLS | See the Microsoft note below |
| `office365` | smtp.office365.com | 587, STARTTLS | See the Microsoft note below |
| (custom) | `--host` | `--port`, `--security ssl\|starttls\|none` | `--username` |

TLS certificates are always verified (`ssl.create_default_context()`).
`--security none` is only for a local relay or test server, and the tool
refuses to send a password over it.

### Gmail

Google doesn't accept your normal password over SMTP. Turn on 2-Step
Verification, then create an **app password** (Google Account → Security →
App passwords) and use it as the SMTP password. Google Workspace
administrators can disable app passwords. If that applies to you, ask for an
SMTP relay. Personal Gmail accounts have a daily sending limit (on the order
of 500 recipients per day at the time of writing). Use `--limit` to spread a
large list over several days, with `--resume`.

### Microsoft (Outlook.com, Microsoft 365)

Microsoft has been retiring password-based ("basic") authentication for SMTP,
in favor of OAuth 2.0, which this tool doesn't implement yet. Depending on
your account type and tenant settings, logging in with a password may fail
with a 535 error. In that case, use your organization's SMTP relay or a
transactional email service. Check Microsoft's current documentation for your
account type.

### A transactional email service or relay

For more than a few hundred messages, a transactional service (Amazon SES,
Postmark, Mailgun, SendGrid and others) or an institutional relay gives better
deliverability and higher limits. All of them offer SMTP, so use `--host`,
`--port` and `--username`.

A relay that needs no login takes `--no-auth`. If it doesn't offer STARTTLS,
the run stops before sending anything; on a network you trust, add
`--security none` (the port then defaults to 25), and the messages travel
unencrypted.

## Passwords

- The CLI reads the password from the `EMAIL_MERGE_PASSWORD` environment
  variable, or prompts for it without echoing. It is deliberately **not** a
  command-line option, because options are visible to other users (`ps`) and
  saved in shell history.
- In Python, fetch the secret from wherever you keep secrets (for example
  `keyring`, or environment variables) and pass it to `SMTPConfig`.
  `repr(SMTPConfig)` masks the password, so it can't leak into logs.
- Never commit passwords, and use a separate app password you can revoke.

## Rate limiting and failures

- The CLI sends at most **20 messages per minute** by default (`--rate`). In
  Python, set `min_interval=` in seconds. Sending bursts is a common trigger
  for provider throttling.
- **Temporary** failures (4xx replies, including a 4xx refusal of a recipient
  such as greylisting, dropped connections, timeouts) are
  retried up to 3 times with exponential backoff (2 s, 4 s, 8 s), reconnecting
  as needed, but only until the whole message has been written to the
  server. If the connection drops after that, or the reply is cut short or
  garbled, the server may already have the message, so it is logged as
  `uncertain` and not sent again. Pressing Ctrl-C while waiting for that reply
  logs it as `uncertain` too. A clear 4xx reply means the server did not
  take the message, so it is retried. The tool waits up to 10 minutes for the
  server's reply to the message content (RFC 5321).
- **Permanent** failures for one recipient (5xx) are recorded as `failed`, and
  the run continues. A refusal that says the mailbox doesn't exist or is
  disabled (`5.1.x`, `5.2.1`, or `550 User unknown`) is recorded as `bounced`,
  and `--resume` doesn't mail that address again; see
  [Resume](resume.md#resume). Each result and log line carries the reply as
  `smtp_code` and `enhanced_status`.
- **Fatal** failures (can't connect, login rejected) stop the run with a clear
  message. Use `--resume` afterwards. The message names the step that failed
  (connecting, TLS or login) and what to try, such as `--security none` for
  a relay without STARTTLS or `--no-auth` for one without a login.
- A connection lost **mid-run** is opened again for the next attempt. A
  reconnect that fails for a reason that may pass (no answer, connection
  refused or reset, a `4xx` greeting) uses up one of the 3 retries, with the
  same backoff, so an outage of a few seconds doesn't end the run. When the
  retries run out, or the reconnect fails for good (a refused login, a TLS
  error), the run stops and the message being sent is logged as `failed`.
- **Account-level** refusals stop the run at once, because every remaining
  message would meet them too: a daily quota (Gmail's `550 5.4.5`,
  Microsoft's `SubmissionQuotaExceeded`), a `4.7.x` or `5.7.x` reply about a
  rate or limit, a login the server asks for while sending (530, 535), or a
  sender address it refuses. In Python this raises `AccountRefusedError`.
  The message that met the refusal is logged as `failed`, so `--resume`
  sends it once the cause is fixed.
- After **5 failed messages in a row** the run stops as well, since that
  usually means a problem with the account or server rather than five bad
  addresses. Refusals that name an unknown address or a full or disabled
  mailbox (enhanced status codes `5.1.x` and `5.2.x`) don't count, and a sent
  message starts the count again. Change the number with `--max-failures N`
  (`max_consecutive_failures=` in Python); `0` turns the check off.
- `--rate` spaces failed attempts too, so a run that fails from the first
  message doesn't send a burst.
- If the server accepts a message for the main recipient but refuses a `Cc`
  or `Bcc`, the message counts as `sent`, with the refusal noted in `detail`.
- If the server refuses the main (`To`) recipient, the row is `failed`, even
  when it accepted a `Cc` or `Bcc`. Those recipients were sent the message,
  and `--resume` sends it again to everyone on the row, so they get a second
  copy. A temporary (4xx) refusal of the main recipient is retried for the
  refused addresses only.

## What the server receives

Each message goes out as the dry run's `.eml` file shows it, except that the
`Bcc` header is removed (Bcc recipients still get the message). Lines end in
CRLF, as SMTP requires, and body lines that start with `From ` are sent
unchanged. Text parts are 7-bit data (see
[how messages are encoded](templates.md#how-messages-are-encoded)), so they
need no server extension. A message built elsewhere with 8-bit text is
declared with `BODY=8BITMIME` (RFC 6152), and a server that doesn't offer
8BITMIME gets those parts encoded as base64 instead.

## Message size

Attachments grow by about a third when they're encoded for email, so a 6 MB
PDF makes a message of about 8 MB. Validation measures every message as it
will be sent:

- A message larger than `--max-size` (`max_message_size=` in Python, default
  25 MB, a common provider limit) fails validation for its row, and the error
  names its attachments and their sizes. `--max-size 0` turns the check off.
- Messages over 10 MB get one warning, since many receiving servers refuse
  messages that size.
- Once connected, the server's own limit, the `SIZE` in its EHLO reply, is
  checked before the first message goes out. If any message of the run is
  larger, the run stops with nothing sent, and the error lists those rows.
  Without this check each oversized message would fail on its own, and a
  server that checks the size only after receiving the message would be sent
  the whole message every time.

For large files, a link to a shared drive is often the better choice.

## Sending from Python

```python
import os
from email_merge_tool import SMTPConfig, SMTPTransport, load_template, mail_merge

transport = SMTPTransport(SMTPConfig.from_service("gmail", "you@gmail.com", os.environ["APP_PASSWORD"]))
report = mail_merge(
    load_template("invite.txt"), "people.csv",
    sender="You <you@gmail.com>",
    transport=transport, dry_run=False,
    log_path="invite.jsonl", resume=True,   # safe to re-run
    min_interval=3,                          # 20 per minute
)
print(report.summary())
```

A transport is used only with `dry_run=False`. Passing one to a dry run adds
a warning that it isn't used. To choose where a dry run writes its `.eml`
files, pass `transport=DryRunTransport("outbox")` (or `output_dir="outbox"`);
a run with `DryRunTransport` is always a dry run and writes no log.

### When a run stops part-way

Every error that stops a run while sending is a `FatalSendError`, and its
`report` attribute holds the `MergeReport` up to that point: who was sent the
message, who failed, and the warnings. That holds with or without a log, so a
script can always say who was mailed:

```python
from email_merge_tool import FatalSendError

try:
    report = mail_merge(..., transport=transport, dry_run=False, log_path="invite.jsonl")
except FatalSendError as exc:
    print(f"stopped: {exc}")
    report = exc.report
print(report.summary())   # e.g. "sent: 212, failed: 1"
```

If the transport raises an exception that is not one of the send errors (for
example a `RuntimeError` from an HTTP client), the run stops with
`TransportError`, a `FatalSendError` whose `__cause__` is the original
exception. The message being sent is logged `uncertain`, because the transport
may have sent it before failing.

Sending without `log_path=` works, but adds a warning to `report.issues`:
without a log, a run that stops part-way cannot be resumed. The command line
asks for `--log FILE` or an explicit `--no-log`, and when a run stops it
prints how far it got, such as `Stopped after 214 of 600 recipient(s): sent:
212, failed: 1, skipped: 1`.

## Custom transports

To deliver another way, such as an HTTP email API or a queue, pass any object
with `open()`, `send(message)` and `close()` methods as `transport=`. The
`Transport` protocol in the [API reference](../api.md) describes it.
`mail_merge` calls `open()` once before the first message and `close()` once
at the end, also when the run stops with an error. `send(message)` gets one
`EmailMessage` and delivers it to the addresses in its `To`, `Cc` and `Bcc`
headers. `wire_bytes(message)` gives the bytes SMTP would transmit, without
the `Bcc` header.

What `send` returns or raises decides the recipient's status:

| `send` | Means | Status | The run |
| --- | --- | --- | --- |
| returns `None` (or `{}`) | delivered | `sent` | continues |
| returns `{address: (code, reply)}` | delivered, except to these recipients | `sent`; `failed` or `bounced` if the row's `To` is among them | continues |
| raises `PermanentSendError` | this message was not delivered | `failed`; `bounced` for a mailbox refusal | continues |
| raises `UncertainSendError` | the request went out but the answer was lost | `uncertain`, never sent again | continues |
| raises `FatalSendError` or `AccountRefusedError` | nothing more can be sent (a bad key, a quota) | `failed`, sent by `--resume` | stops |
| raises anything else | a bug or an unhandled case | `uncertain` | stops with `TransportError` |

Other return values, such as a provider's response object, are ignored, and
so are entries of a returned mapping that aren't `(code, reply)` pairs.
Pass a provider's bounce as `PermanentSendError(text, smtp_code=550,
smtp_error="5.1.1 ...")` to have it recorded as `bounced`. `mail_merge`
doesn't retry anything itself, so `send` should retry throttling and
connection errors that happened before the request went out. A transport that
only previews sets `delivers = False`, like `DryRunTransport`, and every run
with it is a dry run.

This transport posts each message to an HTTP API (the endpoint and fields are
placeholders) and maps each outcome to the table above. It is in the
repository as `examples/http_transport.py`, and the test suite runs it against
a local server.

```python
--8<-- "examples/http_transport.py"
```
