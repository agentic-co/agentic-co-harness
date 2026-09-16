"""Tests for `scripts/eval/paired.py` — the arm comparison that publishes p-values.

These exist because this tool now produces the numbers the project quotes, and the
two mistakes it was written to prevent are both mistakes that LOOK like results:

  * pairing over cells one arm could not score (how p = 0.039 happened); and
  * a sign test that is off by a tail (how a p-value becomes publishable noise).

The sign-test cases are checked against values that can be worked out by hand from
the binomial, so a regression in `sign_test` cannot hide behind plausibility.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PAIRED = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "paired.py"


def _load():
    # Loaded by path and registered before exec: the suite's existing convention
    # for scripts/eval modules (see test_asop_agent.py) and required, or the
    # dataclasses in the module's own imports blow up.
    spec = importlib.util.spec_from_file_location("paired", _PAIRED)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["paired"] = mod
    spec.loader.exec_module(mod)
    return mod


paired = _load()


def _write_run(tmp_path: Path, name: str, cells: dict[tuple[str, int], float],
               last_user_says: dict[tuple[str, int], str] | None = None) -> Path:
    """Materialise a minimal results.json: the fields `load` actually reads."""
    last_user_says = last_user_says or {}
    sims = []
    for (task, trial), reward in cells.items():
        messages = []
        if (task, trial) in last_user_says:
            messages = [{"role": "user", "content": last_user_says[(task, trial)]}]
        sims.append({
            "task_id": task,
            "trial": trial,
            "reward_info": {"reward": reward},
            "messages": messages,
        })
    run = tmp_path / name
    run.mkdir()
    (run / "results.json").write_text(json.dumps({"simulations": sims}))
    return run


# ── the sign test ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("wins,losses,expected", [
    (0, 0, 1.0),      # no discordant pairs: nothing to test
    (1, 1, 1.0),      # perfectly split
    (8, 1, 0.0390625),  # the original arm B vs C figure, exactly
    (7, 1, 0.0703125),  # the same comparison after symmetric exclusion
    (5, 0, 0.0625),   # 2 * (1/32) -- cannot clear 0.05 at n=5, whatever happens
    (10, 0, 2 / 1024),
])
def test_sign_test_exact_values(wins, losses, expected):
    assert paired.sign_test(wins, losses) == pytest.approx(expected)


def test_sign_test_is_symmetric():
    """Direction must not change the p-value; only the reported winner does."""
    for a, b in [(8, 1), (7, 2), (10, 3)]:
        assert paired.sign_test(a, b) == pytest.approx(paired.sign_test(b, a))


def test_sign_test_never_exceeds_one():
    """The doubling in a two-sided test can overshoot at even splits if unclamped."""
    for n in range(0, 12):
        for w in range(0, n + 1):
            assert 0.0 <= paired.sign_test(w, n - w) <= 1.0


# ── cell loading and exclusion ───────────────────────────────────────────────

def test_unscoreable_reward_is_dropped_not_zeroed(tmp_path):
    """A missing reward is absence of evidence, and must not score as a failure."""
    run = _write_run(tmp_path, "a", {("1", 0): 1.0, ("2", 0): 0.0})
    results = json.loads((run / "results.json").read_text())
    results["simulations"].append(
        {"task_id": "3", "trial": 0, "reward_info": {"reward": None}, "messages": []}
    )
    (run / "results.json").write_text(json.dumps(results))

    cells, _ = paired.load(run)
    assert ("3", 0) not in cells
    assert len(cells) == 2


def test_consent_stop_fault_is_detected(tmp_path):
    """FAULT 2: the user consents and stops in one turn, so the agent never acted."""
    run = _write_run(
        tmp_path, "a",
        {("1", 0): 0.0},
        {("1", 0): "Yes, please go ahead and book all three. ###STOP###"},
    )
    _, faulted = paired.load(run)
    assert ("1", 0) in faulted


def test_a_refusal_is_not_a_consent_stop(tmp_path):
    """The narrow pattern matters: a decline must not be excluded as a fault."""
    run = _write_run(
        tmp_path, "a",
        {("1", 0): 0.0},
        {("1", 0): "No, I can't proceed with that upgrade. ###STOP###"},
    )
    _, faulted = paired.load(run)
    assert faulted == set()


def test_fault_in_either_arm_drops_the_cell_from_both(tmp_path, capsys):
    """THE regression guard: per-arm exclusion is what produced 18/26 vs 18/24.

    Arm A scores the faulted cell 1.0 and arm B 0.0. Counted per-arm, A keeps a win
    B never had a chance at. Symmetrically excluded, the cell vanishes and the two
    arms tie -- which is the truth the evidence supports.
    """
    cells_a = {("1", 0): 1.0, ("2", 0): 1.0, ("3", 0): 1.0}
    cells_b = {("1", 0): 0.0, ("2", 0): 1.0, ("3", 0): 1.0}
    a = _write_run(tmp_path, "a", cells_a)
    b = _write_run(
        tmp_path, "b", cells_b,
        {("1", 0): "Yes, go ahead. ###STOP###"},  # faulted in B only
    )

    paired.compare("A", a, "B", b)
    out = capsys.readouterr().out
    assert "matched cells      2" in out
    assert "consent-stop fault dropped from both: 1" in out
    assert "delta              +0.000" in out


def test_disjoint_task_sets_report_rather_than_divide_by_zero(tmp_path, capsys):
    a = _write_run(tmp_path, "a", {("1", 0): 1.0})
    b = _write_run(tmp_path, "b", {("9", 0): 1.0})
    paired.compare("A", a, "B", b)
    assert "NO MATCHED CELLS" in capsys.readouterr().out


def test_low_discordant_count_is_flagged(tmp_path, capsys):
    """Fewer than 6 discordant pairs cannot reach p<0.05; say so beside the p."""
    a = _write_run(tmp_path, "a", {("1", 0): 1.0, ("2", 0): 1.0, ("3", 0): 1.0})
    b = _write_run(tmp_path, "b", {("1", 0): 0.0, ("2", 0): 1.0, ("3", 0): 1.0})
    paired.compare("A", a, "B", b)
    assert "p cannot reach 0.05" in capsys.readouterr().out
