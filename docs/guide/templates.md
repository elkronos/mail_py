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
  Allowed headers are `Subject`, the address headers `From`, `Sender`,
  `Reply-To`, `Cc` and `Bcc`, the
  [provider and list headers](#provider-and-custom-headers) below, and any
  header whose name starts with `X-` (names are case-insensitive). Any other
  line inside the block is an error. This catches typos such as `Subjcet:`,
  which would otherwise silently become body text, and the error suggests the
  header you probably meant.
- The header block is only recognized if the **first line** is one of those
  headers, so a body starting with `Note: ...` is safe.
- `Subject` is required, either in the file or via `load_template(..., subject=...)`.
- Files are read as UTF-8, and a byte-order mark is ignored.
- Files ending in `.html` or `.htm` are HTML bodies. Everything else is plain text.
- `To` always comes from the data (the `email` column, or `--to-field`).
- `From` in the template replaces `sender=` (`--from`), and validation warns
  when the two differ. Whichever is used must be exactly one valid address for
  every row: a `From: {owner}` placeholder whose cell is empty (with no
  `sender=` to fall back on) or that holds two addresses fails validation.
- The `Subject` must not be blank once its placeholders are filled in.
- `Bcc` recipients get the message but the header is removed before sending, as
  the SMTP standard requires. They are not hidden from the send log.
- `Sender` names the mailbox that sends when `From` is someone else,
  such as a department address you send on behalf of. It must be one valid
  address. The SMTP transport also uses it as the envelope sender, so bounces
  go to it.

## Provider and custom headers

Sending services read their own headers to route mail, group statistics and
handle feedback loops. A template can set them in its header block:

```text
Subject: Spring survey
Feedback-ID: {campaign}:lab:survey
X-SES-CONFIGURATION-SET: spring-2026
X-PM-Message-Stream: broadcast
X-Campaign: {campaign}

Dear {first_name},
...
```

Besides any `X-` header, these are allowed: `Feedback-ID` (Gmail's feedback
loop), `List-Id`, `List-Archive`, `List-Help`, `List-Owner`, `List-Post`,
`List-Subscribe`, `Auto-Submitted`, `Precedence`, `Organization`, `Keywords`
and `Comments`. Values may contain placeholders and follow the same line-break
check as other headers; a header whose value is empty for a row is left out of
that message. ASCII values are written exactly as given, folded only at
spaces, so long tokens reach the provider intact.

To add the same headers to every template, pass
`mail_merge(..., extra_headers={"X-Campaign": "spring"})` or repeat
`--header "X-Campaign: spring"` on the command line. A header may be set in
the templates or there, not both.

The tool writes some headers itself and refuses them in templates:
`To` (from the address column), `Date` (set when each message is sent),
`Message-ID`, `MIME-Version`, every `Content-*` header, `List-Unsubscribe`
and `List-Unsubscribe-Post` (use `list_unsubscribe=` and `--one-click`), and
`Return-Path` (set by the receiving server).

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
  RFC 2047 and RFC 6532, and verified by a round-trip test. See
  [how messages are encoded](#how-messages-are-encoded).

## How messages are encoded

The tool chooses each encoding itself instead of leaving it to the standard
library, whose choices differ between Python versions.

- **Lines** are at most 76 characters in quoted-printable and base64 bodies
  (RFC 2045) and in folded headers.
- **Text and HTML bodies** are UTF-8. ASCII text is sent as `7bit` when no
  line is longer than 76 characters, and as quoted-printable otherwise. It is
  never base64, which spam filters penalize for text that did not need it.
  Text with other characters is quoted-printable or base64, whichever is
  shorter: quoted-printable for most European languages, base64 for most other
  scripts. Bodies are never `8bit`, so they reach servers without 8BITMIME
  unchanged and keep their DKIM signature through relays.
- **A non-ASCII subject** is written as RFC 2047 base64 encoded words, folded
  between words. Python 3.10 to 3.12 drop a space when they fold a long subject
  that mixes emoji or accented words with plain ones, so recipients would see
  `openZoë`; the tool's own encoding gives the same bytes on every version.
  ASCII subjects are written as they are.
- **List-Unsubscribe** is written exactly as given; see
  [deliverability](deliverability.md).

## Attachments

`attachments=[...]` (`--attach FILE`) adds files to every message, and
`attachment_field="column"` (`--attachment-field COLUMN`) adds each row's own
files. Paths in that column are separated by `;` and may contain placeholders.

- **Type:** from the file extension, using a table built into the tool, so a
  file gets the same type on every machine and Python version (`.pdf`,
  `.docx`, `.xlsx`, `.ics`, images, archives and so on). Unknown extensions are
  sent as `application/octet-stream`, and so are `.eml` files, so a forwarded
  message arrives byte for byte.
- **Text files** such as `.csv`, `.txt` and `.ics` that are valid UTF-8 are
  labelled `charset="utf-8"`, so accented characters display correctly.
- **File name:** the file's own name, with control characters, quotes,
  backslashes and slashes replaced by `_`. An ASCII name is written as one
  plain `filename="..."` parameter at any length. Other names are written as
  RFC 2231 `filename*=utf-8''...`, with an RFC 2047 `name=` on the
  Content-Type for older clients.
- **Duplicate names:** two attachments with the same name (ignoring case) in
  one message produce a warning, since mail clients then rename one of them or
  save one over the other.

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
