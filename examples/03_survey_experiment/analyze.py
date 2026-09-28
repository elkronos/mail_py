"""Compare wave-1 response rates between arms.

Joins the assignment table written by wave1.py with the list of responders and
reports each arm's response rate, the difference, and a 95% confidence
interval (Newcombe's hybrid score interval, which behaves well for small
samples and rates near 0 or 1). Standard library only.

This toy panel has 24 people, so the interval is very wide: that is the
honest answer, and a reminder to plan sample sizes before sending.

Run from the repository root (after wave1.py):  python examples/03_survey_experiment/analyze.py
"""

import csv
import math
from pathlib import Path

here = Path(__file__).parent
Z = 1.959963984540054  # 97.5th percentile of the standard normal


def wilson(successes: int, n: int) -> tuple[float, float]:
    p = successes / n
    centre = (p + Z * Z / (2 * n)) / (1 + Z * Z / n)
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / (1 + Z * Z / n)
    return centre - half, centre + half


def newcombe(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float, float]:
    """Difference p1 - p2 with Newcombe's (1998) method 10 interval."""
    p1, p2 = x1 / n1, x2 / n2
    l1, u1 = wilson(x1, n1)
    l2, u2 = wilson(x2, n2)
    d = p1 - p2
    return d, d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


lines = (here / "responded.txt").read_text(encoding="utf-8").splitlines()
responded = {line.strip().lower() for line in lines if line.strip() and not line.startswith("#")}
with (here / "assignments.csv").open(encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))

arms: dict[str, list[int]] = {}
for row in rows:
    arms.setdefault(row["variant"], []).append(row["address"] in responded)

for arm, outcomes in sorted(arms.items()):
    print(f"{arm:<13} responded {sum(outcomes)}/{len(outcomes)} = {sum(outcomes) / len(outcomes):.0%}")

p, c = arms["personalized"], arms["control"]
d, low, high = newcombe(sum(p), len(p), sum(c), len(c))
print(f"personalized - control: {d:+.1%} (95% CI {low:+.1%} to {high:+.1%}), seed {rows[0]['seed']}")
