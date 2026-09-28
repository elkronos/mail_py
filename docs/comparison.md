# How it compares

There are good Python tools for sending email. This page describes where
email-merge-tool fits. The other tools' feature sets change over time, so check
their own documentation before relying on a detail here.

| Need | email-merge-tool | Alternatives |
| --- | --- | --- |
| Send one email from code | Works, but more than you need | The standard library's `smtplib` + `email`; `yagmail` (a simple Gmail-oriented client); `redmail` (HTML, Jinja templates, attachments, embedded images) |
| Simple CSV mail merge from the command line | Yes | [`mailmerge`](https://github.com/awdeorio/mailmerge) (Jinja2 templates, Markdown, attachments, dry run by default) |
| Validate the *whole* list before the first send, with "did you mean" hints | Yes | Usually you find out row by row |
| Resume after an interruption without duplicate sends, even if the file changed | Yes: an address-keyed, `fsync`-ed log | Commonly row-number based, or not provided |
| Randomized, stratified, reproducible A/B assignment with an exported table | Yes | Usually DIY with `random` or pandas, easy to get subtly wrong (unseeded, order-dependent, unbalanced) |
| Reminder waves that skip responders | Yes (`--exclude`) | DIY filtering |
| `List-Unsubscribe` / RFC 8058 one-click headers | Yes, with per-recipient values | Possible via custom headers in most libraries |
| HTML-escaping of merged values, and header-injection blocking | Yes, by default | Depends on the tool and its settings |
| Zero runtime dependencies | Yes | Most tools depend on Jinja2 or similar |

## When to use something else

email-merge-tool deliberately keeps the template language minimal and the
feature set focused. Choose another tool when you need:

- **Template logic** (conditionals, loops, filters): `mailmerge` or `redmail`,
  which use Jinja2.
- **OAuth2 login** (for example, Microsoft 365 tenants without password SMTP,
  or the Gmail API): `redmail` with a custom SMTP setup, or a provider SDK.
- **Inline images** embedded with `cid:` references, or charts in the message body: `redmail`.
- **Very large volumes** (tens of thousands of messages or more), bounce
  processing, open and click analytics, or list management: a transactional
  or marketing email service. You can still use this tool's
  `assign_variants` for the experimental design.

## Limitations to know about

- Messages are built in memory before sending, so every message is validated
  up front. With large per-recipient attachments and long lists, that uses a
  lot of memory.
- Bounces that arrive later by email (asynchronous bounces) aren't collected.
  Only rejections during the SMTP conversation are logged as `failed`.
- There is no built-in scheduling. Use cron or Task Scheduler with `--resume --limit`.
