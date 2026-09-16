"""C1-coding's scoring layer — the part that decides whether it is an experiment.

`ai-tasks/unified/phase-2.md` warns that the obvious version of this experiment
is self-confirming and has been corrected twice. These tests pin the three
corrections that keep it honest, so a later simplification cannot quietly undo
them:

1. **The gate and ground truth must be independent.** `gate()` delegates to the
   official SWE-bench grader, so the grader cannot also be the thing under
   study. The public gate sees PASS_TO_PASS (present at `base_commit`); ground
   truth is the hidden FAIL_TO_PASS that arrive in `test_patch`.
2. **Recall is reported per defect class, never pooled** — pooled accuracy moves
   with prevalence, and this plan criticised that error and then committed it.
3. **A run that exercised no requirement-violating patch is not a result.** It
   could not have produced a refuting outcome, so it must not be reportable as
   one. That is asserted here as a behaviour, not left to a reader's care.

Everything here runs offline. The Docker-backed verdicts are inputs to this
layer, not part of it — which is the point: the logic that turns verdicts into a
claim is provable without spending two hours to find out it was wrong.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


swebench = _load("swebench_eval", REPO / "scripts" / "eval" / "swebench.py")

GOLD = (
    "diff --git a/src/thing.py b/src/thing.py\n"
    "--- a/src/thing.py\n"
    "+++ b/src/thing.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-return None\n"
    "+return value\n"
)
ENTRY = {"instance_id": "acme__widget-1", "patch": GOLD}


def _trial(cls: str, public: bool, hidden: bool, n: int = 1) -> list[dict]:
    return [
        {"instance_id": f"i{i}", "patch_class": cls,
         "public_passed": public, "hidden_resolved": hidden}
        for i in range(n)
    ]


# ------------------------------------------------------- the four outcome cells


@pytest.mark.parametrize("public,hidden,expected", [
    (True, True, "OK"),
    (False, False, "CAUGHT"),
    (False, True, "FALSE-ALARM"),
    (True, False, "MISSED"),
])
def test_every_outcome_cell_is_named(public, hidden, expected):
    """`MISSED` is the only cell that bears on the claim.

    Public gate said yes, hidden acceptance tests said the requirement is not
    met. A design that cannot produce this cell cannot refute "passed therefore
    correct", which is the whole reason the third patch class exists.
    """
    assert swebench.classify(public, hidden) == expected


# ------------------------------------------------------------ the three classes


def test_the_silent_patch_is_deliberately_dull():
    """A no-op comment on a file the gold patch touches.

    It compiles, changes nothing, leaves every pre-existing test green, and does
    not fix the issue. If the gate cannot catch the DULLEST possible non-fix,
    nothing is learned by making the non-fix subtler — so the test pins the
    dullness rather than treating it as a placeholder to improve later.
    """
    patch = swebench.synthesize_patch(ENTRY, swebench.SILENT)
    assert "src/thing.py" in patch
    assert "no-op" in patch
    assert "return value" not in patch, "the silent patch must not contain the fix"


def test_the_broken_patch_fails_at_import():
    """Chosen to break the PUBLIC tests identically across repos.

    A per-repo semantic break would have to be reasoned about per instance, and
    every instance-specific judgement is somewhere the experiment can be tuned
    toward the answer it wants.
    """
    patch = swebench.synthesize_patch(ENTRY, swebench.BROKEN)
    assert "raise RuntimeError" in patch


def test_the_correct_patch_is_the_gold_patch_unmodified():
    assert swebench.synthesize_patch(ENTRY, swebench.CORRECT) == GOLD


def test_an_instance_whose_gold_patch_touches_no_file_is_refused():
    """Loudly, rather than producing an empty patch that scores as a non-fix.

    An empty patch in the CORRECT class would silently become a second silent
    patch, and the correct class would start looking like the gate had failed.
    """
    with pytest.raises(ValueError, match="touches no file"):
        swebench.synthesize_patch({"instance_id": "x", "patch": "no diff here"},
                                  swebench.SILENT)


# ----------------------------------------------------------------- the scoring


def test_recall_is_reported_per_class_and_never_pooled():
    trials = (
        _trial(swebench.CORRECT, public=True, hidden=True, n=5)
        + _trial(swebench.BROKEN, public=False, hidden=False, n=5)
        + _trial(swebench.SILENT, public=True, hidden=False, n=5)
    )
    by_class = swebench.score_trials(trials)

    assert by_class[swebench.BROKEN]["recall"] == 1.0, "the easy class"
    assert by_class[swebench.SILENT]["recall"] == 0.0, (
        "the public gate accepted every requirement-violating patch — the "
        "refuting outcome, and it must show as 0.0 rather than being averaged away"
    )
    assert by_class[swebench.CORRECT]["recall"] is None, (
        "no defective patch in this class, so recall is UNDEFINED — reporting "
        "0.0 or 1.0 here makes a class that was never exercised look like one "
        "that passed"
    )
    # And there is no pooled number to quote by accident.
    assert "recall" not in by_class


def test_a_refuting_result_is_stated_as_one():
    trials = _trial(swebench.SILENT, public=True, hidden=False, n=3)
    out = swebench.format_score(swebench.score_trials(trials))
    assert "3 MISSED" in out
    assert "refuting outcome" in out


def test_a_clean_result_is_reported_as_a_pilot_not_a_proof():
    """phase-2.md item 7: treat any clean result as a pilot bounded to one
    executor and one repo. Written into the output so the caveat travels with
    the number instead of living in a plan file nobody re-reads."""
    trials = _trial(swebench.SILENT, public=False, hidden=False, n=4)
    out = swebench.format_score(swebench.score_trials(trials))
    assert "PILOT" in out


def test_a_run_with_no_requirement_violating_patch_refuses_to_be_a_result():
    """The tautology guard, as behaviour rather than as a warning in prose.

    CORRECT + BROKEN alone gives a perfect-looking table that proves nothing:
    it contains no patch that could have been wrongly accepted. The output says
    so in capitals and `score` exits non-zero, so it cannot end a pipeline green.
    """
    trials = (
        _trial(swebench.CORRECT, public=True, hidden=True, n=10)
        + _trial(swebench.BROKEN, public=False, hidden=False, n=10)
    )
    by_class = swebench.score_trials(trials)
    out = swebench.format_score(by_class)
    assert "NOT AN EXPERIMENT YET" in out
    silent = by_class.get(swebench.SILENT)
    assert not (silent and silent["defective"]), "score must exit non-zero here"


def test_the_liveness_caveat_travels_with_every_report():
    """It is the caveat this project has lost track of most often."""
    out = swebench.format_score(swebench.score_trials(_trial(swebench.SILENT, True, False)))
    assert "LIVENESS" in out and "CORRECTNESS" in out


# -------------------------------------------------------- the leakage boundary


def test_the_gold_patch_is_kept_beside_the_manifest_not_in_a_workdir():
    """`prepare` stores `patch` for the CORRECT class. That is the same boundary
    `test_patch` already sits on — next to the manifest, never near the workdir
    the agent is pointed at. This test exists so the reason is findable from the
    test suite and not only from a comment."""
    src = (REPO / "scripts" / "eval" / "swebench.py").read_text()
    prepare_src = src[src.index("def prepare("):src.index("def gate(")]
    assert '"patch": row["patch"]' in prepare_src
    assert "workdir" in prepare_src
    # The workdir is a copy of the repo at base_commit. Nothing that writes the
    # answer into it may appear here.
    assert 'workdir / "patch' not in prepare_src
