"""Quick start: validate, preview and save messages without sending anything.

Run from the repository root:  python examples/01_quickstart/run.py
"""

from pathlib import Path

from email_merge_tool import load_template, mail_merge

here = Path(__file__).parent

report = mail_merge(
    load_template(here / "invite.txt"),
    here / "people.csv",
    sender="Open House <openhouse@example.org>",
    output_dir=here / "outbox",  # each message saved as a .eml file
)

print(report.summary())
first = report.messages[0]
print("To:     ", first["To"])
print("Subject:", first["Subject"])
print(first.get_body(("plain",)).get_content())

# To actually send, pass a transport and dry_run=False, e.g.:
#   from email_merge_tool import SMTPConfig, SMTPTransport
#   transport = SMTPTransport(SMTPConfig.from_service("gmail", "you@gmail.com", app_password))
#   mail_merge(..., transport=transport, dry_run=False, log_path="sent.jsonl")
