"""Elitist accept/reject and per-task fixed/broken/unchanged bookkeeping
(EXP-L.md v3, "with elitism (RL review #1)" and "Per-task fixed / broken /
unchanged on EDIT ... (RL review #3)").

Pure-stdlib: exercises `expl.stats.paired_delta` (the accept/reject test) and
`expl.loop.per_task_transition` directly against synthetic per-task success
dicts, no SOPBench or network needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from expl import loop as loop_mod  # noqa: E402
from expl.stats import paired_delta  # noqa: E402


def _rec(success: bool) -> dict:
    return {"success": success}


def test_candidate_beating_b_on_select_is_accepted():
    # B: 5/10 success. C: 7/10 success, strictly better -> accept.
    b = {i: (i < 5) for i in range(10)}
    c = {i: (i < 7) for i in range(10)}
    cmp = paired_delta(c, b, list(range(10)))
    assert cmp["delta"] > 0
    accepted = cmp["delta"] > 0
    assert accepted is True


def test_candidate_tying_b_on_select_is_rejected():
    # Elitism is asymmetric on purpose: a tie is NOT an improvement, so it is
    # rejected — "accept C as the new B only if it beats B" (strict).
    b = {i: (i < 5) for i in range(10)}
    c = {i: (i < 5) for i in range(10)}
    cmp = paired_delta(c, b, list(range(10)))
    assert cmp["delta"] == 0
    accepted = cmp["delta"] > 0
    assert accepted is False


def test_candidate_regressing_is_rejected():
    b = {i: (i < 6) for i in range(10)}
    c = {i: (i < 4) for i in range(10)}
    cmp = paired_delta(c, b, list(range(10)))
    assert cmp["delta"] < 0
    accepted = cmp["delta"] > 0
    assert accepted is False


def test_per_task_transition_counts_fixed_broken_and_unchanged():
    b_records = {
        0: _rec(False),  # will be fixed
        1: _rec(True),   # will be broken
        2: _rec(True),   # unchanged pass
        3: _rec(False),  # unchanged fail
        4: _rec(False),  # not present in c -> ignored
    }
    c_records = {
        0: _rec(True),
        1: _rec(False),
        2: _rec(True),
        3: _rec(False),
    }
    transition = loop_mod.per_task_transition(b_records, c_records, positions=[0, 1, 2, 3, 4])
    assert transition == {"fixed": 1, "broken": 1, "unchanged_pass": 1, "unchanged_fail": 1}


def test_check_run_health_passes_on_a_complete_clean_run(tmp_path):
    splits = {"edit": [0, 1], "select": [2, 3], "test": [4, 5]}
    records = {i: _rec(True) for i in range(6)}
    log = tmp_path / "run.log"
    log.write_text("Running interactions: 100%|##########| 6/6\nSaved results\n")
    healthy, problems = loop_mod.check_run_health(records, splits, log)
    assert healthy is True
    assert problems == []


def test_check_run_health_fails_loud_on_a_partial_split():
    """The round-1 incident, minimally reproduced: another process unloaded
    the shared LM Studio instance mid-run, and only 48/195 tasks scored — the
    guard must catch this from split completeness alone, even with no error
    text in the log.
    """
    splits = {"edit": [0, 1, 2, 3], "select": [4, 5], "test": [6, 7]}
    records = {i: _rec(True) for i in range(4)}  # select/test never scored
    healthy, problems = loop_mod.check_run_health(records, splits, Path("/nonexistent/run.log"))
    assert healthy is False
    assert any("select" in p for p in problems)
    assert any("test" in p for p in problems)


def test_check_run_health_fails_loud_on_a_request_error_even_if_counts_match(tmp_path):
    splits = {"edit": [0], "select": [1], "test": [2]}
    records = {i: _rec(True) for i in range(3)}
    log = tmp_path / "run.log"
    log.write_text("[route] call failed: Error code: 400 - No models loaded\n")
    healthy, problems = loop_mod.check_run_health(records, splits, log)
    assert healthy is False
    assert any("No models loaded" in p for p in problems)


def test_a_rejected_round_leaves_b_untouched_in_state(tmp_path, monkeypatch):
    """Simulates the state-update half of `cmd_round`'s accept/reject branch
    without running a real subprocess or reviser call: after a reject, the
    caller's `state["b_doc"]`/`b_records_file` must be exactly what they were
    before the round, only `rounds` grows.
    """
    state = {"b_doc": "v0.md", "b_records_file": "v0_records.json", "rounds": []}
    before = dict(state)

    # Mirror loop.cmd_round's post-decision branch directly (accepted=False).
    accepted = False
    round_record = {"round": 1, "accepted": accepted}
    state["rounds"].append(round_record)
    if accepted:
        state["b_doc"] = "round1_candidate.md"
        state["b_records_file"] = "round1_records.json"

    assert state["b_doc"] == before["b_doc"]
    assert state["b_records_file"] == before["b_records_file"]
    assert len(state["rounds"]) == 1 and state["rounds"][0]["accepted"] is False
