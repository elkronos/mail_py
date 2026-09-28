# email-merge-tool

**Careful, reproducible mail merge for Python.** Write one template, point it at a
CSV or JSON file, and send each person a personalized message. The tool checks
everything before anything is sent, and it won't email anyone twice.

```bash
pip install git+https://github.com/elkronos/mail_py
email-merge -t invite.txt -d people.csv --from "Lab <lab@example.org>"   # dry run: nothing is sent
```

## Why this tool?

Plenty of Python code can send an email, and a few tools can do a basic mail
merge. This one is built for runs where mistakes are costly, such as research
participant invitations, course or HR announcements, and alumni and member
mailings:

| Problem | What email-merge-tool does |
| --- | --- |
| Row 900 has a typo'd address, and you find out after 899 emails went out | **Preflight validation** checks every row, every placeholder and every attachment first. Nothing is sent unless everything passes, and near-miss column names get suggestions. |
| You're not sure what recipients will actually see | **Dry run by default.** Preview messages in the terminal, or save them as `.eml` files that open in any mail client. |
| The connection drops or you hit a daily quota halfway through | An append-only **send log** plus `--resume` skips everyone already sent to (by address, not row number), so re-running never sends a duplicate. |
| You want to know which wording works better | **Randomized, stratified, reproducible assignment** of templates, with an exported assignment table and seed. See the [experiment walkthrough](tutorials/survey-experiment.md). |
| Reminders shouldn't go to people who already responded | Pass an **exclusion list** (`--exclude responded.txt`). |
| A name like `<b>Bob</b>` breaks your HTML, or a newline in a field injects a header | Values are **HTML-escaped** in HTML bodies, and **line breaks in headers are rejected**. |
| Gmail and Yahoo require unsubscribe headers from bulk senders | Built-in **`List-Unsubscribe`** and **RFC 8058 one-click** headers, plus a plain-text alternative for every HTML message. |

It has **no runtime dependencies** beyond the Python standard library (3.10+).

## Where to go next

- [Getting started](getting-started.md): install the tool and do a dry run in five minutes.
- Walkthroughs: [your first merge](tutorials/first-merge.md), an [HTML newsletter](tutorials/html-newsletter.md), and a [randomized survey invitation experiment](tutorials/survey-experiment.md).
- [Sending safely](guide/sending.md): Gmail app passwords, Microsoft 365, and custom SMTP servers.
- [How it compares](comparison.md) with other Python email tools, including when to use something else.
