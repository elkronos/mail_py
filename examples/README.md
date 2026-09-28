# Examples

Every example runs **offline**: it performs a dry run and writes `.eml` files you
can open in any mail client. Nothing is sent. Run them from the repository root
after `pip install -e .`:

| Folder | Shows |
| --- | --- |
| [`01_quickstart`](01_quickstart) | One text template, a CSV, a dry run (Python and CLI). |
| [`02_html_newsletter`](02_html_newsletter) | Text + HTML alternative, inline CSS, per-recipient unsubscribe link. |
| [`03_survey_experiment`](03_survey_experiment) | A randomized, stratified invitation experiment, a reminder wave that excludes responders, and the analysis. |

```bash
python examples/01_quickstart/run.py
python examples/02_html_newsletter/run.py
python examples/03_survey_experiment/wave1.py
python examples/03_survey_experiment/reminder.py
python examples/03_survey_experiment/analyze.py
```

The walkthroughs on the documentation site explain each step:
https://elkronos.github.io/mail_py/
