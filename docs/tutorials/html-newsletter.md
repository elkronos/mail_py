# Walkthrough 2: an HTML newsletter

Files are in
[`examples/02_html_newsletter`](https://github.com/elkronos/mail_py/tree/main/examples/02_html_newsletter).
This walkthrough covers sending both HTML and plain text, CSS in templates,
HTML escaping, and unsubscribe headers.

## Pair a text template with an HTML version

Headers live in the text file:

```text
--8<-- "examples/02_html_newsletter/newsletter.txt"
```

The HTML file contains only the body:

```html
--8<-- "examples/02_html_newsletter/newsletter.html"
```

The CSS braces (`body { ... }`) are left alone. Only `{identifier}` with no
spaces counts as a placeholder, so you don't need to escape CSS.

The message is sent as `multipart/alternative`: mail clients show the HTML, and
screen readers, text-only clients and some spam filters use the text part.
If you only write HTML, a text version is generated automatically, with links
kept as `text (url)`. Writing your own text version gives better results.

## Unsubscribe headers

```python
--8<-- "examples/02_html_newsletter/run.py"
```

`list_unsubscribe` may contain placeholders, so each recipient gets their own
link. `one_click_unsubscribe=True` adds `List-Unsubscribe-Post:
List-Unsubscribe=One-Click` (RFC 8058). Gmail and Yahoo have required both
headers from bulk senders since 2024. Your web endpoint must accept a `POST` to
that URL and unsubscribe the person without further interaction. See
[Deliverability](../guide/deliverability.md).

## HTML escaping protects you from your data

The second subscriber's name is `Bo <Admin>`. In the HTML part it becomes
`Bo &lt;Admin&gt;`, so it displays as typed instead of being parsed as a tag.
The plain-text part and the subject keep the raw text, which is correct for
those contexts.

Escaping applies to *values*. The template itself is trusted, so you can write
any HTML you like in it.

## CLI equivalent

```bash
email-merge -t newsletter.txt --html newsletter.html -d subscribers.json \
    --from "Example Institute <news@example.org>" \
    --list-unsubscribe "<https://example.org/unsubscribe?id={id}>" --one-click
```
