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
- **Temporary** failures (4xx replies, dropped connections, timeouts) are
  retried up to 3 times with exponential backoff (2 s, 4 s, 8 s), reconnecting
  as needed.
- **Permanent** failures for one recipient (5xx) are recorded as `failed`, and
  the run continues.
- **Fatal** failures (can't connect, login rejected) stop the run with a clear
  message. Use `--resume` afterwards.
- If the server accepts a message for the main recipient but refuses a `Cc`,
  the message counts as `sent`, with the refusal noted in `detail`.

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

To deliver another way (an HTTP API, a queue), write a class with `open()`,
`send(message)` and `close()` methods and pass it as `transport`.
