# email-merge-tool

[![Tests](https://github.com/elkronos/mail_py/actions/workflows/ci.yml/badge.svg)](https://github.com/elkronos/mail_py/actions/workflows/ci.yml)
[![Docs](https://github.com/elkronos/mail_py/actions/workflows/docs.yml/badge.svg)](https://elkronos.github.io/mail_py/)

**Careful, reproducible mail merge for Python.** Write one template, point it at
a CSV or JSON file, and send each person a personalized message. The tool
validates everything before anything is sent, never emails anyone twice, and
supports randomized message experiments.

**Documentation and walkthroughs: <https://elkronos.github.io/mail_py/>**

## Highlights

- **Dry run by default.** Preview messages, or save them as `.eml` files, before sending.
- **Preflight validation** of every row, placeholder and attachment, with "did you mean" hints. Nothing is sent if anything is wrong.
- **Crash-safe resume.** An address-keyed send log means re-running never double-sends.
- **Randomized experiments.** Reproducible, stratified assignment of message variants, with an exported assignment table.
- **Reminder waves** that skip people who already responded.
- **Safe by default.** HTML-escaped values, header-injection blocking, verified TLS, and no passwords on the command line.
- **Deliverability built in.** Plain-text alternatives, `List-Unsubscribe` and RFC 8058 one-click headers, and rate limiting.
- Gmail, Outlook and Microsoft 365, or any SMTP server. **No dependencies** beyond the standard library (Python 3.10+).

## Install

```bash
pip install git+https://github.com/elkronos/mail_py
```

## Quick start

`invite.txt`:

```text
Subject: See you on {date}, {first_name}!
Reply-To: events@example.org

Dear {first_name},

You're invited to our open house on {date}.
```

`people.csv`:

```text
email,first_name,date
ann@example.org,Ann,12 March
bo@example.org,Bo,12 March
```

Preview (nothing is sent):

```bash
email-merge -t invite.txt -d people.csv --from "Events <events@example.org>"
```

Send, with a log so an interrupted run can be resumed:

```bash
export EMAIL_MERGE_PASSWORD='your-gmail-app-password'
email-merge -t invite.txt -d people.csv --from you@gmail.com --service gmail --send --log invite.jsonl
```

The same in Python:

```python
from email_merge_tool import SMTPConfig, SMTPTransport, load_template, mail_merge

template = load_template("invite.txt")
report = mail_merge(template, "people.csv", sender="Events <events@example.org>")  # dry run
print(report.messages[0])

transport = SMTPTransport(SMTPConfig.from_service("gmail", "you@gmail.com", app_password))
report = mail_merge(template, "people.csv", sender="you@gmail.com",
                    transport=transport, dry_run=False, log_path="invite.jsonl", resume=True)
print(report.summary())
```

A randomized, stratified experiment comparing two invitations:

```bash
email-merge -t generic.txt -t personalized.txt -d panel.csv --from lab@example.org \
    --seed 20240915 --strata department --assignments assignments.csv
```

## Examples

[`examples/`](examples) has three runnable, offline projects: a quick start,
an HTML newsletter with unsubscribe headers, and a complete survey experiment
(randomize, remind, analyze).

## Development

```bash
pip install -e ".[dev,docs]"
pytest               # tests
ruff check . && ruff format --check .
mkdocs serve         # documentation at http://127.0.0.1:8000
```

The [code review](https://elkronos.github.io/mail_py/review/) documents the
problems found in v0.1 and how v0.2 fixes them.

## License

MIT. See [LICENSE](LICENSE).
