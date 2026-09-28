# Experiments: methodology

Randomly assigning people to different message versions is the most reliable
way to learn what works, whether that's a subject line, a salutation, a
sender, or the length of an appeal. This tool implements the design side
carefully, so that the analysis side is straightforward. The
[experiment walkthrough](../tutorials/survey-experiment.md) shows it end to end.

## How assignment works

`assign_variants` (used by `mail_merge(..., seed=...)` and `--seed`) does:

1. **Stratify:** group recipients by the values of the `strata` columns.
2. **Order units reproducibly:** within each stratum, sort recipients by
   `SHA-256(seed | address)`. This makes the assignment independent of the row
   order in your file.
3. **Fix arm sizes:** compute each arm's size from `weights` with
   largest-remainder rounding. Leftover slots (when the stratum size isn't
   divisible) go to arms chosen at random, not always the first arm.
4. **Shuffle:** shuffle the list of arm labels with a PRNG seeded by
   `seed | stratum`, and deal them to the ordered units.

The result is **complete randomization within strata** (a blocked design):
every arm's size in every stratum is within one of its target, and every
assignment is equally likely given those sizes.

### Why not flip a coin for each person?

Independent coin flips (Bernoulli assignment) are also random, but the arm
sizes vary by chance. With 40 people, a 25/15 split isn't unusual, which costs
statistical power and invites doubts about balance. Fixing the arm sizes
avoids both problems (Gerber & Green, 2012, ch. 3–4).

### Why stratify?

If a characteristic strongly predicts the outcome (for example, past
participation predicts survey response), randomizing within its levels
guarantees the arms are balanced on it. That removes a source of chance
imbalance and makes the estimate more precise (Gerber & Green, 2012, §4.4).
Stratify on a few important, pre-treatment variables, not on everything: each
stratum should contain at least as many people as there are arms, ideally many
more.

### Reproducibility and auditability

- The seed can be an integer or a string, and `--seed 2024` equals `seed=2024`.
- The same seed, address set, stratum values, arm names and weights always
  give the same assignment on any machine. This relies only on SHA-256 and
  Python's string-seeded `random.Random`, which is stable across Python
  versions and unaffected by `PYTHONHASHSEED`.
- `--assignments file.csv` writes `address, variant, <strata...>, seed`, and
  the send log records the variant for every message sent.
- **Record the seed before sending**, ideally in a pre-registration (for
  example on OSF). A seed chosen after looking at several candidate assignments
  defeats the purpose.

The PRNG is Mersenne Twister, which is appropriate for experimental
assignment but not for secrets. Assignments are predictable to anyone who
knows the seed, so don't publish the seed before the study if participants
could exploit knowing their arm.

## Designing a good message experiment

- **Change one thing** between arms (or use a factorial design and analyze it
  as one). If the personalized version is also shorter, you can't tell which
  change mattered.
- **Define the outcome in advance**: responded within *N* days, clicked, or
  replied. Open tracking is unreliable (privacy proxies such as Apple Mail
  Privacy Protection pre-fetch images), so prefer outcomes you observe
  directly, like survey completion.
- **Keep units independent.** If colleagues forward messages to each other,
  arms contaminate each other. Consider randomizing by team (put the team ID in
  a column, send per team) if that's a concern.
- **Get ethics or IRB approval** that covers the experiment, where required.
  Experiments on communications with people are human-subjects research in
  many settings.

## Sample size

A quick normal-approximation formula for comparing two proportions *p₁*
and *p₂* with equal arms, two-sided α = 0.05 and 80% power:

*n per arm ≈ 7.85 × [p₁(1−p₁) + p₂(1−p₂)] / (p₁ − p₂)²*

| Baseline rate | Detectable lift | n per arm |
| --- | --- | --- |
| 40% | +5 points (to 45%) | ≈ 1,530 |
| 40% | +10 points (to 50%) | ≈ 385 |
| 10% | +3 points (to 13%) | ≈ 1,770 |

Small lists can only detect large effects. If yours is small, run the
experiment anyway only if a noisy answer is still useful, and report the
confidence interval rather than a significance verdict. Stratification
typically makes these numbers somewhat conservative.

## Analysis

Join the assignment table to your outcome data by address, and compare rates
between arms. `examples/03_survey_experiment/analyze.py` computes the
difference in proportions with Newcombe's (1998) hybrid score interval, which
is more accurate than the simple Wald interval in small samples. For
stratified designs, a regression of the outcome on the arm with stratum fixed
effects (or a stratified estimator) is standard (Gerber & Green, 2012).
Analyze everyone as randomized (intention to treat), including people whose
message bounced.

## References

- Dillman, D. A., Smyth, J. D., & Christian, L. M. (2014). *Internet, Phone, Mail, and Mixed-Mode Surveys: The Tailored Design Method* (4th ed.). Wiley.
- Gerber, A. S., & Green, D. P. (2012). *Field Experiments: Design, Analysis, and Interpretation*. W. W. Norton.
- Heerwegh, D. (2005). Effects of personal salutations in e-mail invitations to participate in a web survey. *Public Opinion Quarterly, 69*(4), 588–598.
- Joinson, A. N., & Reips, U.-D. (2007). Personalized salutation, power of sender and response rates to Web-based surveys. *Computers in Human Behavior, 23*(3), 1372–1383.
- Kohavi, R., Tang, D., & Xu, Y. (2020). *Trustworthy Online Controlled Experiments: A Practical Guide to A/B Testing*. Cambridge University Press.
- Newcombe, R. G. (1998). Interval estimation for the difference between independent proportions: comparison of eleven methods. *Statistics in Medicine, 17*(8), 873–890.
