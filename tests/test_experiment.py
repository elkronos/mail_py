import csv
import random
from collections import Counter

import pytest

from email_merge_tool import assign_variants, write_assignments


def make(n, strata=("a", "b")):
    return [{"email": f"p{i}@example.org", "grp": strata[i % len(strata)]} for i in range(n)]


def test_reproducible_and_order_independent():
    rows = make(50)
    first = {a.address: a.variant for a in assign_variants(rows, ["A", "B"], seed=7)}
    shuffled = rows[:]
    random.Random(1).shuffle(shuffled)
    second = {a.address: a.variant for a in assign_variants(shuffled, ["A", "B"], seed=7)}
    assert first == second
    other = {a.address: a.variant for a in assign_variants(rows, ["A", "B"], seed=8)}
    assert other != first


def test_int_and_str_seed_agree():
    rows = make(10)
    assert assign_variants(rows, ["A", "B"], seed=5) == assign_variants(rows, ["A", "B"], seed="5")


@pytest.mark.parametrize("n", [1, 2, 3, 7, 101])
def test_balance_within_one(n):
    counts = Counter(a.variant for a in assign_variants(make(n, ("x",)), ["A", "B", "C"], seed=1))
    sizes = [counts.get(v, 0) for v in "ABC"]
    assert sum(sizes) == n and max(sizes) - min(sizes) <= 1


def test_stratified_balance():
    rows = make(40, ("a", "a", "a", "b"))  # 30 in a, 10 in b
    result = assign_variants(rows, ["A", "B"], seed=3, strata="grp")
    by = Counter((a.stratum[0], a.variant) for a in result)
    assert by[("a", "A")] == by[("a", "B")] == 15
    assert by[("b", "A")] == by[("b", "B")] == 5


def test_weights():
    counts = Counter(a.variant for a in assign_variants(make(30, ("x",)), ["A", "B"], seed=1, weights=[2, 1]))
    assert counts == {"A": 20, "B": 10}


def test_randomness_is_not_degenerate():
    # Across seeds, a given person should land in each arm sometimes.
    rows = make(20, ("x",))
    arms = {assign_variants(rows, ["A", "B"], seed=s)[0].variant for s in range(20)}
    assert arms == {"A", "B"}


def test_bad_arguments():
    with pytest.raises(ValueError):
        assign_variants(make(4), ["A"], seed=1)
    with pytest.raises(ValueError):
        assign_variants(make(4), ["A", "A"], seed=1)
    with pytest.raises(ValueError):
        assign_variants(make(4), ["A", "B"], seed=1, weights=[1, 0])
    with pytest.raises(ValueError, match="stratification"):
        assign_variants(make(4), ["A", "B"], seed=1, strata="nope")


def test_write_assignments(tmp_path):
    result = assign_variants(make(4), ["A", "B"], seed=9, strata="grp")
    path = tmp_path / "a.csv"
    write_assignments(path, result, seed=9, strata="grp")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows[0].keys() == {"address", "variant", "grp", "seed"}
    assert len(rows) == 4 and rows[0]["seed"] == "9"
