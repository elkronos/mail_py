"""Wave 1: randomized, stratified invitation experiment.

Half of each department x prior-participation stratum gets a generic
invitation, half a personalized one. The seed and the assignment table are
saved so the analysis can be reproduced exactly.

Run from the repository root:  python examples/03_survey_experiment/wave1.py
"""

from pathlib import Path

from email_merge_tool import load_template, mail_merge

here = Path(__file__).parent
SEED = 20240915  # pre-register this value before sending

report = mail_merge(
    [load_template(here / "control.txt"), load_template(here / "personalized.txt")],
    here / "panel.csv",
    sender="Faculty Affairs <survey-team@example.edu>",
    seed=SEED,
    strata=["department", "prior_participant"],
    assignments_path=here / "assignments.csv",
    output_dir=here / "outbox_wave1",
    # For a real send add: transport=..., dry_run=False, log_path=here / "wave1.jsonl"
)

counts = {}
for a in report.assignments:
    key = (a.stratum, a.variant)
    counts[key] = counts.get(key, 0) + 1
print(report.summary())
for (stratum, variant), n in sorted(counts.items()):
    print(f"{' / '.join(stratum):<22} {variant:<13} {n}")
print("Assignment table written to", here / "assignments.csv")
