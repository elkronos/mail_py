"""HTML + plain-text newsletter with a one-click unsubscribe header.

Note the second subscriber's name contains "<Admin>": it is HTML-escaped in the
HTML part, so it cannot inject markup.

Run from the repository root:  python examples/02_html_newsletter/run.py
"""

from pathlib import Path

from email_merge_tool import load_template, mail_merge

here = Path(__file__).parent

template = load_template(here / "newsletter.txt", html_path=here / "newsletter.html")
report = mail_merge(
    template,
    here / "subscribers.json",
    sender="Example Institute <news@example.org>",
    list_unsubscribe="<https://example.org/unsubscribe?id={id}>, <mailto:unsubscribe@example.org?subject={id}>",
    one_click_unsubscribe=True,  # RFC 8058; needs an https URI and DKIM signing by your provider
    output_dir=here / "outbox",
)

message = report.messages[1]
print(report.summary())
print("Content-Type:        ", message.get_content_type())
print("List-Unsubscribe:    ", message["List-Unsubscribe"])
print("List-Unsubscribe-Post:", message["List-Unsubscribe-Post"])
html = message.get_body(("html",)).get_content()
print("Escaped name in HTML:", "Bo &lt;Admin&gt;" in html)
