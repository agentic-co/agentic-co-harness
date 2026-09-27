"""Tests for `scripts/eval/judge_paired_bootstrap.py`.

This tool produces the Δ and the P(Δ≤0) that a published finding rests on, so
the cases below are the ones where a bug would look like a result:

  * a delta computed over a **shifted id set** (the two arms must refuse to
    pair at all rather than silently compare different decisions);
  * a **guard that does not fire** when the revision buys FPR by refusing less
    of everything — the failure mode the whole experiment is designed around;
  * point estimates that disagree with hand arithmetic on a tiny fixture.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "judge_paired_bootstrap.py"


def _load():
    spec = importlib.util.spec_from_file_location("judge_paired_bootstrap", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["judge_paired_bootstrap"] = mod
    spec.loader.exec_module(mod)
    return mod


jpb = _load()


def _arm(tmp_path: Path, name: str, verdicts: dict[str, tuple[str, bool | None]]) -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({
        "summary": {},
        "decisions": [{"id": i, "truth": t, "passed": v} for i, (t, v) in verdicts.items()],
    }))
    return p


def test_point_estimates_match_hand_arithmetic(tmp_path):
    # 2 violated, 2 satisfied. A refuses everything: TPR 1.0, FPR 1.0, lift 0.
    # B refuses only the violated pair: TPR 1.0, FPR 0.0, lift +1.0.
    v = {"n1": ("not_held", False), "n2": ("not_held", False),
         "h1": ("held", False), "h2": ("held", False)}
    w = dict(v, h1=("held", True), h2=("held", True))
    res = jpb.compare(_arm(tmp_path, "a", v), _arm(tmp_path, "b", w), "t", reps=200, seed=1)
    assert res["a"] == {"TPR": 1.0, "FPR": 1.0, "lift": 0.0, "unscored": 0}
    assert res["b"] == {"TPR": 1.0, "FPR": 0.0, "lift": 1.0, "unscored": 0}
    assert res["delta"]["lift"]["point"] == pytest.approx(1.0)
    assert res["delta"]["FPR"]["point"] == pytest.approx(-1.0)
    assert res["guard"]["TPR_held_at_point"] is True
    assert res["guard"]["TPR_drop_significant"] is False
    assert res["guard"]["indiscriminate_ratio"] == pytest.approx(0.0)


def test_guard_fires_when_the_revision_refuses_less_of_everything(tmp_path):
    """The string-match-ALL-in-a-new-hat case: B passes everything.

    FPR improves, which flatters the revision, but TPR collapses by the same
    amount. `indiscriminate_ratio` near 1.0 is the signature and the guard must
    report TPR_held False.
    """
    v = {"n1": ("not_held", False), "n2": ("not_held", False),
         "h1": ("held", False), "h2": ("held", False)}
    w = {k: (t, True) for k, (t, _) in v.items()}
    res = jpb.compare(_arm(tmp_path, "a", v), _arm(tmp_path, "b", w), "t", reps=200, seed=1)
    assert res["delta"]["TPR"]["point"] == pytest.approx(-1.0)
    assert res["delta"]["FPR"]["point"] == pytest.approx(-1.0)
    assert res["delta"]["lift"]["point"] == pytest.approx(0.0)  # lift hides it entirely
    assert res["guard"]["TPR_held_at_point"] is False
    assert res["guard"]["TPR_drop_significant"] is True
    assert res["guard"]["indiscriminate_ratio"] == pytest.approx(1.0)


def test_refuses_to_pair_a_shifted_id_set(tmp_path):
    a = _arm(tmp_path, "a", {"n1": ("not_held", False), "h1": ("held", True)})
    b = _arm(tmp_path, "b", {"n2": ("not_held", False), "h1": ("held", True)})
    with pytest.raises(SystemExit, match="same decisions"):
        jpb.compare(a, b, "t", reps=10, seed=1)


def test_refuses_to_pair_when_ground_truth_disagrees(tmp_path):
    a = _arm(tmp_path, "a", {"n1": ("not_held", False), "h1": ("held", True)})
    b = _arm(tmp_path, "b", {"n1": ("held", False), "h1": ("held", True)})
    with pytest.raises(SystemExit, match="ground truth"):
        jpb.compare(a, b, "t", reps=10, seed=1)


def test_unscored_decisions_are_counted_not_silently_dropped(tmp_path):
    v = {"n1": ("not_held", False), "n2": ("not_held", None),
         "h1": ("held", True), "h2": ("held", True)}
    res = jpb.compare(_arm(tmp_path, "a", v), _arm(tmp_path, "b", v), "t", reps=50, seed=1)
    assert res["a"]["unscored"] == 1
    assert res["a"]["TPR"] == pytest.approx(1.0)  # scored over the 1 usable violation


def test_bootstrap_is_deterministic_under_a_fixed_seed(tmp_path):
    v = {f"n{i}": ("not_held", i % 3 != 0) for i in range(20)}
    v.update({f"h{i}": ("held", i % 4 != 0) for i in range(20)})
    w = {k: (t, True if t == "held" else p) for k, (t, p) in v.items()}
    a, b = _arm(tmp_path, "a", v), _arm(tmp_path, "b", w)
    assert (jpb.compare(a, b, "t", reps=500, seed=7)["delta"]
            == jpb.compare(a, b, "t", reps=500, seed=7)["delta"])
