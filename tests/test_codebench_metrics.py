"""metrics: arm summaries and exact McNemar over synthetic episode records —
no model, no sandbox, just the aggregation math."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.metrics import exact_mcnemar, paired_compare, summarize_arm  # noqa: E402


def _ep(task_id, arm, hidden_pass, **overrides):
    base = dict(
        task_id=task_id, dataset="test", arm=arm, model="m", status="finished",
        turns=3, tool_calls=5, called_finish=True, prompt_tokens=100,
        completion_tokens=50, wall_time_s=1.5, hidden_pass=hidden_pass,
        repro_written=False, repro_validity=None, false_done=(not hidden_pass and True),
        error=None,
    )
    base.update(overrides)
    return base


def test_summarize_arm_basic_rates():
    episodes = [
        _ep("a", "bare", True),
        _ep("b", "bare", False),
        _ep("c", "bare", True),
        _ep("d", "bare", False),
    ]
    s = summarize_arm(episodes)
    assert s.n == 4
    assert s.pass_at_1 == 0.5
    assert s.false_done_rate == 0.5
    assert s.finished_rate == 1.0


def test_summarize_arm_repro_validity():
    episodes = [
        _ep("a", "asop", True, repro_written=True, repro_validity=True),
        _ep("b", "asop", True, repro_written=True, repro_validity=False),
        _ep("c", "asop", False, repro_written=False, repro_validity=None),
    ]
    s = summarize_arm(episodes)
    assert s.repro_written_rate == 2 / 3
    assert s.repro_validity_rate == 0.5  # 1 of 2 that wrote one were valid


def test_exact_mcnemar_symmetric_gives_p_one():
    assert exact_mcnemar(0, 0) == 1.0
    assert exact_mcnemar(3, 3) == 1.0


def test_exact_mcnemar_lopsided_is_significant():
    # 10 pairs where A passes and B fails, 0 the other way — a strong signal.
    p = exact_mcnemar(10, 0)
    assert p < 0.01


def test_paired_compare_counts_and_drops():
    a = [_ep("1", "asop", True), _ep("2", "asop", False), _ep("3", "asop", True)]
    b = [_ep("1", "bare", False), _ep("2", "bare", False), _ep("4", "bare", True)]
    cmp = paired_compare(a, b)
    assert cmp["n_pairs"] == 2  # only task_id 1 and 2 are shared
    assert cmp["dropped_task_ids"] == ["3", "4"]
    assert cmp["a_only"] == 1  # task 1: a passed, b failed
    assert cmp["both_fail"] == 1  # task 2: both failed
    assert 0.0 <= cmp["p_value_exact_mcnemar"] <= 1.0
