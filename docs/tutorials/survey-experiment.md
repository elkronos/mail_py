# Walkthrough 3: a survey invitation experiment

This walkthrough follows a full research cycle: design, randomize, send,
remind, and analyze. It uses
[`examples/03_survey_experiment`](https://github.com/elkronos/mail_py/tree/main/examples/03_survey_experiment).

**Question:** does a personalized invitation ("Dear Ann") get more survey
responses than a generic one ("Dear colleague")? This question has been studied
in survey methodology, with personalization often raising response rates
modestly (Heerwegh, 2005; Joinson & Reips, 2007). Effects vary by population
and sender, so it's worth testing in *your* population. See
[Experiments](../guide/experiments.md) for the methodology and references.

## 1. Design before you send

- **Arms:** `control.txt` (generic salutation and subject) and
  `personalized.txt` (first name in both). Only the salutation and subject
  differ. Keep everything else identical, so a difference in response can only
  come from what you changed.
- **Unit:** the person (address).
- **Outcome:** responded within 7 days, yes or no.
- **Strata (blocks):** `department` × `prior_participant`. Past participants
  respond at very different rates, so randomizing *within* these groups
  guarantees both arms get the same mix of them.
- **Seed:** `20240915`. Write it into your pre-registration or analysis plan
  *before* sending.

```text
--8<-- "examples/03_survey_experiment/personalized.txt"
```

## 2. Randomize and send wave 1

```python
--8<-- "examples/03_survey_experiment/wave1.py"
```

The output shows exact balance, two per arm in each of the six strata:

```text
dry_run: 24
Biology / no           control       2
Biology / no           personalized  2
...
Physics / yes          personalized  2
```

On the command line:

```bash
email-merge -t control.txt -t personalized.txt -d panel.csv \
    --from "Faculty Affairs <survey-team@example.edu>" \
    --seed 20240915 --strata department --strata prior_participant \
    --assignments assignments.csv --log wave1.jsonl --send --service gmail
```

**Reproducibility guarantee:** the same seed, the same set of addresses and
the same stratum values always give the same assignment, *even if the CSV is
re-sorted*. Anyone can re-run the assignment to verify it. The seed may be an
integer or a string, and `2024` and `"2024"` give the same result.

## 3. Remind non-responders

The survey tool exports a list of respondents to `responded.txt`. Following the
Tailored Design Method's advice on multiple contacts (Dillman et al., 2014),
send a reminder to everyone else:

```python
--8<-- "examples/03_survey_experiment/reminder.py"
```

The 8 respondents are skipped. Everyone gets the *same* reminder here, so the
experiment compares invitation wording only. To test reminder wording too,
randomize again with a different seed, but analyze that as a separate
experiment.

## 4. Analyze

`analyze.py` joins `assignments.csv` with the responder list and compares
response rates, with a Newcombe (1998) confidence interval for the difference:

```text
control       responded 5/12 = 42%
personalized  responded 3/12 = 25%
personalized - control: -16.7% (95% CI -47.6% to +19.3%), seed 20240915
```

With 24 people the interval spans nearly ±50 percentage points, so the data
are compatible with a large benefit, no effect, or a large harm. **This is the
expected result for a toy sample, not a finding.** Detecting a 5-point
difference between rates around 40% at 80% power needs roughly 1,500 people
per arm. Do the sample-size calculation before sending (see
[Experiments](../guide/experiments.md#sample-size)).

## Checklist

- [ ] Arms differ only in what you are testing.
- [ ] Seed, strata, arms and outcome recorded *before* sending.
- [ ] `--assignments` table and send log kept with the data.
- [ ] Sample size is large enough for the smallest effect you care about.
- [ ] Ethics or IRB approval covers the contact protocol, including reminders and unsubscribe.
