# Templates

## File format

```text
Subject: Your results, {first_name}
From: Study Team <team@example.org>
Reply-To: help@example.org
Cc: {advisor_email}

Dear {first_name},
...
```

- The optional **header block** comes first and ends with a blank line.
  Allowed headers are `Subject`, `From`, `Reply-To`, `Cc` and `Bcc`
  (case-insensitive). Any other line inside the block is an error. This catches
  typos such as `Subjcet:`, which would otherwise silently become body text.
- The header block is only recognized if the **first line** is one of those
  headers, so a body starting with `Note: ...` is safe.
- `Subject` is required, either in the file or via `load_template(..., subject=...)`.
- Files are read as UTF-8, and a byte-order mark is ignored.
- Files ending in `.html` or `.htm` are HTML bodies. Everything else is plain text.
- `To` always comes from the data (the `email` column, or `--to-field`).
- `Bcc` recipients get the message but the header is removed before sending, as
  the SMTP standard requires. They are not hidden from the send log.

## Placeholders

| You write | You get |
| --- | --- |
| `{first_name}` | the value of the `first_name` column |
| `{{first_name}}` | the literal text `{first_name}` |
| `p { margin: 0 }`, `a{color:red}` | unchanged (not placeholders) |

Placeholder names follow Python identifier rules (`[A-Za-z_][A-Za-z0-9_]*`)
and are **case-sensitive**. If you write `{First_Name}` and the column is
`first_name`, validation fails with a "did you mean" suggestion rather than
guessing.

Empty values render as empty strings, and validation warns about them.
Numbers from JSON are converted with `str()`. Format them in your data if you
need a specific style (`"1,250.00"`).

There is deliberately **no logic** (conditionals, loops) in templates. If you
need a different paragraph for some recipients, put the finished text in a
column (`{custom_paragraph}`) or use different templates with `variant_field`.
This keeps templates readable by non-programmers and makes every message
fully predictable in a dry run.

## Safety rules applied at render time

- **HTML bodies:** every substituted value is HTML-escaped (`<`, `>`, `&`,
  quotes). The template's own markup is not touched.
- **Headers:** if a substituted value would put a line break into `Subject`,
  `To`, `From`, `Reply-To`, `Cc`, `Bcc` or `List-Unsubscribe`, that recipient
  fails validation. This blocks header injection (such as a "name" containing
  `\nBcc: someone@else`).
- **Non-ASCII:** subjects, names and bodies in any language are encoded per
  RFC 2047 and RFC 6532 by the standard library, and verified by a round-trip test.

## Text and HTML together

```python
from email_merge_tool import load_template
t = load_template("letter.txt", html_path="letter.html")   # headers go in letter.txt
```

On the CLI, use `-t letter.txt --html letter.html`. With several templates, give
`--html` once per `-t`, in the same order.

An HTML-only template gets an automatic plain-text alternative, which is better
than nothing, but a hand-written text version reads better.

## Templates in code

```python
from email_merge_tool import Template

t = Template(
    subject="Welcome, {first_name}",
    text="Hi {first_name},\n...",
    html="<p>Hi {first_name},</p>...",       # optional
    headers={"Reply-To": "help@example.org"},  # optional
    name="welcome",                            # label used in logs and experiments
)
t.fields            # {'first_name'}
t.render({"email": "a@example.org", "first_name": "Ann"})
```
