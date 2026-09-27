"""Tests for task-subset runs (`--task-ids`) and the stratified pilot sampler.

A subset rides on the same positional-placeholder mechanism as `--shard`, so
the properties pinned are the same ones: a shard runs exactly selected ∩ owned,
the merge demands one real result per SELECTED position and none elsewhere,
and the sampler is deterministic and honours its allocation rule. Synthetic
data only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "eval"))

import sopbench_sample as SS  # noqa: E402
import sopbench_shard as S  # noqa: E402


def _real(goal: str) -> dict:
    return {"setup": {"assistant_agent": {"model": "m"}}, "task": {"user_goal": goal},
            "interactions": [{"interaction": []}]}


def test_subset_composes_with_shard_as_intersection():
    subset = frozenset({1, 2, 5})
    runs = {i for i, e in enumerate(S.seed_results([], 6, 1, 2, subset))
            if e == {"interactions": []}}
    assert runs == {1, 5}  # odd positions (shard 1/2) that are selected
    runs0 = {i for i, e in enumerate(S.seed_results([], 6, 0, 2, subset))
             if e == {"interactions": []}}
    assert runs0 == {2}


def test_subset_alone_is_shard_zero_of_one():
    subset = frozenset({0, 3})
    seeded = S.seed_results([], 5, 0, 1, subset)
    assert [e == {"interactions": []} for e in seeded] == [True, False, False, True, False]


def test_merge_with_subset_checks_only_selected_positions():
    subset = frozenset({0, 3})
    s0 = S.seed_results([], 5, 0, 2, subset)
    s1 = S.seed_results([], 5, 1, 2, subset)
    s0[0], s1[3] = _real("g0"), _real("g3")
    merged = S.merge_results([s0, s1], subset=subset)
    assert merged[0]["task"]["user_goal"] == "g0" and merged[3]["task"]["user_goal"] == "g3"
    # non-selected positions are empty slots the scorer skips
    assert all(merged[i]["interactions"] == [] for i in (1, 2, 4))


def test_merge_with_subset_refuses_a_missing_selected_position():
    subset = frozenset({0, 3})
    s0 = S.seed_results([], 5, 0, 1, subset)
    s0[0] = _real("g0")
    with pytest.raises(SystemExit, match="position 3"):
        S.merge_results([s0], subset=subset)


def test_merge_with_subset_refuses_a_real_result_outside_it():
    s0 = [_real("g0"), _real("g1")]
    with pytest.raises(SystemExit, match="outside the subset"):
        S.merge_results([s0], subset=frozenset({0}))


def test_load_subset_validates(tmp_path):
    good = tmp_path / "ids.json"
    good.write_text("[3, 1, 2]")
    assert S.load_subset(good) == frozenset({1, 2, 3})
    assert S.load_subset(None) is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"ids": [1]}')
    with pytest.raises(SystemExit):
        S.load_subset(bad)


# ── the sampler ─────────────────────────────────────────────────────────────

TASKS = {
    "a": [{"action_should_succeed": True}] * 30 + [{"action_should_succeed": False}] * 10,
    "b": [{"action_should_succeed": True}] * 1,
    "c": [{"action_should_succeed": False}] * 3,
}


def test_strata_are_positional_in_run_simulation_order():
    strata = SS.strata_of(TASKS)
    assert strata[("a", True)] == list(range(30))
    assert strata[("a", False)] == list(range(30, 40))
    assert strata[("b", True)] == [40]
    assert strata[("c", False)] == [41, 42, 43]


def test_allocation_floor_then_largest_remainder_and_caps():
    sizes = {k: len(v) for k, v in SS.strata_of(TASKS).items()}
    alloc = SS.allocate(sizes, 12)
    assert sum(alloc.values()) == 12
    assert alloc[("b", True)] == 1  # floor is min(2, size)
    assert all(alloc[k] <= sizes[k] for k in sizes)
    assert all(alloc[k] >= min(2, sizes[k]) for k in sizes)
    assert alloc[("a", True)] > alloc[("a", False)]  # remainder follows size


def test_allocation_cannot_exceed_population():
    sizes = {("x", True): 1, ("y", False): 2}
    assert SS.allocate(sizes, 10) == {("x", True): 1, ("y", False): 2}


def test_sample_is_deterministic_and_seed_sensitive():
    a, _ = SS.sample(TASKS, 12, 20260924)
    b, _ = SS.sample(TASKS, 12, 20260924)
    c, _ = SS.sample(TASKS, 12, 1)
    assert a == b and len(a) == 12 == len(set(a)) and a == sorted(a)
    assert a != c


def test_committed_hotel_pilot_is_a_valid_subset():
    ids = json.loads((_ROOT / "evals" / "sopbench-bank-asop" / "hotel-pilot-40.json").read_text())
    assert len(ids) == 40 == len(set(ids)) and ids == sorted(ids)
    assert 0 <= ids[0] and ids[-1] < 195
