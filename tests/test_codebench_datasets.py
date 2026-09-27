"""codebench dataset loaders read what fetch_datasets.py wrote, and the
provenance/scope facts stay true: skip if the cached JSONL isn't there (a
fresh clone won't have it — evals/codebench/data is real data, not fixtures,
so it is not gitignored, but the test still shouldn't hard-fail a clone that
hasn't run the fetcher)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.datasets import (  # noqa: E402
    DATA_ROOT, HARDER_BUG_TYPES, Task, filter_harder, load_humanevalfix,
    load_quixbugs, stratified_sample,
)

_HAS_HEF = (DATA_ROOT / "humanevalfix" / "dataset.jsonl").exists()
_HAS_QB = (DATA_ROOT / "quixbugs" / "dataset.jsonl").exists()


@pytest.mark.skipif(not _HAS_HEF, reason="run fetch_datasets.py --source humanevalfix first")
def test_humanevalfix_shape():
    tasks = load_humanevalfix()
    assert len(tasks) == 164
    ids = {t.task_id for t in tasks}
    assert len(ids) == 164  # no duplicate task_ids
    t = next(t for t in tasks if t.task_id == "Python/0")
    assert t.entry_point == "has_close_elements"
    assert "def has_close_elements" in t.buggy_code
    assert t.visible_test_code and "from solution import has_close_elements" in t.visible_test_code
    assert "check(has_close_elements)" in t.hidden_test_code
    # visible is a strict subset of the assertions in hidden (fewer cases)
    assert t.hidden_test_code.count("assert") > t.visible_test_code.count("assert")


@pytest.mark.skipif(not _HAS_QB, reason="run fetch_datasets.py --source quixbugs first")
def test_quixbugs_shape():
    tasks = load_quixbugs()
    assert len(tasks) == 31  # flat subset only — graph-based 9 excluded, see PROVENANCE.md
    graph_based = {
        "breadth_first_search", "depth_first_search", "detect_cycle",
        "minimum_spanning_tree", "reverse_linked_list", "shortest_path_length",
        "shortest_path_lengths", "shortest_paths", "topological_ordering",
    }
    assert not ({t.entry_point for t in tasks} & graph_based)

    gcd = next(t for t in tasks if t.entry_point == "gcd")
    assert "def gcd(a, b):" in gcd.buggy_code
    assert gcd.hidden_test_code  # always non-empty for gcd (6 cases, split leaves >=1 hidden)
    assert "from solution import gcd" in gcd.hidden_test_code
    assert gcd.bug_type is None  # QuixBugs carries no bug_type taxonomy


@pytest.mark.skipif(not _HAS_HEF, reason="run fetch_datasets.py --source humanevalfix first")
def test_humanevalfix_bug_type_present_and_counted():
    tasks = load_humanevalfix()
    from collections import Counter
    counts = Counter(t.bug_type for t in tasks)
    assert set(counts) == {
        "value misuse", "missing logic", "excess logic",
        "operator misuse", "variable misuse", "function misuse",
    }
    assert counts == Counter({
        "value misuse": 44, "missing logic": 33, "excess logic": 31,
        "operator misuse": 25, "variable misuse": 23, "function misuse": 8,
    })


@pytest.mark.skipif(not (_HAS_HEF and _HAS_QB), reason="both datasets required")
def test_filter_harder_keeps_all_quixbugs_and_only_harder_humanevalfix():
    tasks = load_humanevalfix() + load_quixbugs()
    harder = filter_harder(tasks)
    quixbugs_kept = [t for t in harder if t.dataset == "quixbugs"]
    hef_kept = [t for t in harder if t.dataset == "humanevalfix"]
    assert len(quixbugs_kept) == 31  # every QuixBugs task passes through
    assert len(hef_kept) == 44 + 25 + 23 + 8  # value+operator+variable+function misuse
    assert all(t.bug_type in HARDER_BUG_TYPES for t in hef_kept)
    assert len(harder) == 31 + 100


@pytest.mark.skipif(not (_HAS_HEF and _HAS_QB), reason="both datasets required")
def test_task_ids_are_globally_unique_across_datasets():
    ids = {t.task_id for t in load_humanevalfix()} | {t.task_id for t in load_quixbugs()}
    assert len(ids) == 164 + 31


def _fake_tasks(dataset: str, n: int) -> list[Task]:
    return [
        Task(task_id=f"{dataset}/{i}", dataset=dataset, entry_point="f",
             buggy_code="", visible_test_code=None, hidden_test_code="", prompt="")
        for i in range(n)
    ]


def test_stratified_sample_is_deterministic_and_proportional():
    tasks = _fake_tasks("a", 164) + _fake_tasks("b", 31)
    s1 = stratified_sample(tasks, 60, seed=20260926)
    s2 = stratified_sample(tasks, 60, seed=20260926)
    assert [t.task_id for t in s1] == [t.task_id for t in s2]  # same seed -> identical selection
    assert len(s1) == 60
    counts = {"a": 0, "b": 0}
    for t in s1:
        counts[t.dataset] += 1
    # proportional to 164:31 within rounding — not exhausting the smaller group
    assert 45 <= counts["a"] <= 55
    assert 5 <= counts["b"] <= 15


def test_stratified_sample_different_seed_differs():
    tasks = _fake_tasks("a", 164) + _fake_tasks("b", 31)
    s1 = {t.task_id for t in stratified_sample(tasks, 60, seed=1)}
    s2 = {t.task_id for t in stratified_sample(tasks, 60, seed=2)}
    assert s1 != s2


def test_stratified_sample_n_at_least_total_returns_everything():
    tasks = _fake_tasks("a", 5) + _fake_tasks("b", 3)
    s = stratified_sample(tasks, 100, seed=1)
    assert len(s) == 8
