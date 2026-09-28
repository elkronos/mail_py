"""Wave 2: a reminder to everyone who has not responded yet.

Run from the repository root:  python examples/03_survey_experiment/reminder.py
"""

from pathlib import Path

from email_merge_tool import load_template, mail_merge

here = Path(__file__).parent

report = mail_merge(
    load_template(here / "reminder.txt"),
    here / "panel.csv",
    sender="Faculty Affairs <survey-team@example.edu>",
    exclude=here / "responded.txt",  # never remind people who already answered
    output_dir=here / "outbox_reminder",
)
print(report.summary())
for r in report.results:
    if r.status == "skipped":
        print(f"  skipped {r.to}: {r.detail}")
