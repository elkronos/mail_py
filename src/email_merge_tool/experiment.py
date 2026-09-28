"""Reproducible random assignment of recipients to message variants.

Randomized message experiments ("A/B tests") are the standard design for
learning which wording, subject line, or sender works better, and have a long
history in survey methodology (see the documentation's *Experiments* page for
references). This module provides what is needed to run them defensibly:

* **Reproducibility**: the assignment is a pure function of the seed, the set
  of recipient addresses and the stratum values. Re-ordering the input file
  does not change who gets what.
* **Balance**: within each stratum, arm sizes differ by at most one from their
  target allocation (complete randomization with fixed arm sizes, rather than a
  coin flip per person, which can leave arms noticeably unbalanced in small
  samples).
* **Stratification** (blocking) on one or more columns, so that, for example,
  each department or each prior-response group is split evenly across arms.
* **An auditable record**: :func:`write_assignments` exports who was assigned to
  which arm, with the seed, for the analysis stage.
"""

from __future__ import annotations

import csv
import hashlib
import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .data import normalize_address

__all__ = ["Assignment", "assign_variants", "write_assignments"]


@dataclass(frozen=True)
class Assignment:
    """The arm assigned to one recipient."""

    address: str
    variant: str
    stratum: tuple[str, ...]


def _target_counts(n: int, variants: Sequence[str], weights: Sequence[float], rng: random.Random) -> list[int]:
    total = float(sum(weights))
    exact = [n * w / total for w in weights]
    counts = [math.floor(x) for x in exact]
    remainder = n - sum(counts)
    # Largest-remainder rounding with random tie-breaking, so no arm is
    # systematically favoured when n is not divisible by the number of arms.
    order = sorted(range(len(variants)), key=lambda i: (-(exact[i] - counts[i]), rng.random()))
    for i in order[:remainder]:
        counts[i] += 1
    return counts


def assign_variants(
    records: Sequence[Mapping[str, str]],
    variants: Sequence[str],
    *,
    seed: int | str,
    to_field: str = "email",
    strata: str | Sequence[str] | None = None,
    weights: Sequence[float] | None = None,
) -> list[Assignment]:
    """Randomly assign each record to one of ``variants``.

    Args:
        records: Recipient records (each needs ``to_field``).
        variants: Arm labels, e.g. template names ``["control", "personalized"]``.
        seed: Any int or string. Record it: it is what makes the design reproducible.
        to_field: Column holding the address (used as the stable unit ID).
        strata: Column name(s) to block on. Randomization is done separately
            within each combination of values.
        weights: Relative allocation, e.g. ``[2, 1]`` for 2:1. Defaults to equal.

    Returns:
        One :class:`Assignment` per record, in the same order as ``records``.
    """
    if len(variants) < 2:
        raise ValueError("an experiment needs at least two variants")
    if len(set(variants)) != len(variants):
        raise ValueError("variant names must be unique")
    weights = list(weights) if weights is not None else [1.0] * len(variants)
    if len(weights) != len(variants) or any(w <= 0 for w in weights):
        raise ValueError("weights must be positive and match the number of variants")
    strata_cols = [strata] if isinstance(strata, str) else list(strata or [])

    groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        missing = [c for c in strata_cols if c not in record]
        if missing:
            raise ValueError(f"record {index + 1} lacks stratification column(s) {missing}")
        groups[tuple(record[c] for c in strata_cols)].append(index)

    def unit_key(i: int) -> str:
        # Sort units by a seeded hash of their address so the result does not
        # depend on the order of rows in the input file.
        address = normalize_address(records[i][to_field])
        return hashlib.sha256(f"{seed}|{address}".encode()).hexdigest()

    result: list[Assignment | None] = [None] * len(records)
    for stratum in sorted(groups):
        members = sorted(groups[stratum], key=unit_key)
        rng = random.Random(  # noqa: S311 (reproducibility, not security)
            f"{seed}|{'|'.join(stratum)}"
        )
        counts = _target_counts(len(members), variants, weights, rng)
        labels = [v for v, c in zip(variants, counts, strict=True) for _ in range(c)]
        rng.shuffle(labels)
        for i, label in zip(members, labels, strict=True):
            result[i] = Assignment(normalize_address(records[i][to_field]), label, stratum)
    return [a for a in result if a is not None]


def write_assignments(
    path: str | Path,
    assignments: Sequence[Assignment],
    *,
    seed: int | str,
    strata: str | Sequence[str] | None = None,
) -> None:
    """Write assignments to CSV (address, variant, stratum columns, seed) for analysis."""
    strata_cols = [strata] if isinstance(strata, str) else list(strata or [])
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["address", "variant", *strata_cols, "seed"])
        for a in assignments:
            writer.writerow([a.address, a.variant, *a.stratum, seed])
