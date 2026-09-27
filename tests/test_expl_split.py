"""EXP-L split determinism (EXP-L.md v3, "Split (changed)").

Pure-stdlib: no SOPBench checkout needed, so this runs in the normal suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from expl import split as split_mod  # noqa: E402


def _synthetic_refs(n_goals=6, per_goal=(1, 2, 7, 23, 40, 99)):
    """A skewed set of strata sizes deliberately including singletons — the
    case a fixed-ratio-per-stratum split cannot land on exact global totals
    for, which is why `stratified_split` uses a running-remainder instead.
    """
    refs = []
    pos = 0
    total = sum(per_goal)
    for i, n in enumerate(per_goal):
        goal = f"goal_{i}"
        for j in range(n):
            refs.append(split_mod.TaskRef(pos, goal, action_should_succeed=(j % 2 == 0)))
            pos += 1
    assert pos == total
    return refs, total


def test_split_partitions_every_task_exactly_once():
    refs, total = _synthetic_refs()
    targets = {"edit": 68, "select": 43, "test": 61}  # sums to 172, matches total below
    assert sum(targets.values()) == total
    out = split_mod.stratified_split(refs, seed=20260926, targets=targets)
    all_positions = out["edit"] + out["select"] + out["test"]
    assert sorted(all_positions) == list(range(total))
    assert len(all_positions) == len(set(all_positions))


def test_split_hits_exact_targets_despite_singleton_strata():
    refs, _ = _synthetic_refs()
    targets = {"edit": 68, "select": 43, "test": 61}
    out = split_mod.stratified_split(refs, seed=20260926, targets=targets)
    assert {k: len(v) for k, v in out.items()} == targets


def test_split_is_deterministic_for_same_seed():
    refs, _ = _synthetic_refs()
    targets = {"edit": 68, "select": 43, "test": 61}
    a = split_mod.stratified_split(refs, seed=20260926, targets=targets)
    b = split_mod.stratified_split(refs, seed=20260926, targets=targets)
    assert a == b


def test_split_changes_with_a_different_seed():
    refs, _ = _synthetic_refs()
    targets = {"edit": 68, "select": 43, "test": 61}
    a = split_mod.stratified_split(refs, seed=20260926, targets=targets)
    b = split_mod.stratified_split(refs, seed=1, targets=targets)
    assert a != b


def test_split_rejects_targets_that_dont_sum_to_the_task_count():
    refs, _ = _synthetic_refs()
    try:
        split_mod.stratified_split(refs, seed=20260926, targets={"edit": 1, "select": 1, "test": 1})
    except ValueError:
        return
    raise AssertionError("expected ValueError for mismatched targets")


def test_hotel_195_matches_pre_registered_80_50_65(tmp_path):
    """The exact pre-registered split, run against a synthetic stand-in for
    hotel's real shape (10 goals, 195 tasks) — the real tasks.json is not
    needed for this property; only the goal/count shape matters.
    """
    sizes = [42, 4, 1, 99, 17, 7, 9, 2, 13, 1]  # hotel_tasks.json's own per-goal counts
    assert sum(sizes) == 195
    refs = []
    pos = 0
    for i, n in enumerate(sizes):
        for j in range(n):
            refs.append(split_mod.TaskRef(pos, f"goal_{i}", action_should_succeed=(j % 3 != 0)))
            pos += 1
    out = split_mod.stratified_split(refs, seed=split_mod.SEED)
    assert {k: len(v) for k, v in out.items()} == split_mod.DEFAULT_TARGETS
