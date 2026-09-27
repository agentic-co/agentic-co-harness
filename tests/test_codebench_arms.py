"""AsopGate: the state machine that turns REPRODUCE/PROPOSE/VALIDATE into a
code-checked gate on `finish()` — enforced for `asop`, tracked-but-never-
enforced for `asop-nogate`. Uses `ToolResult` directly (no model, no
network) so this is a pure unit test of the gate logic."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.arms import AsopGate  # noqa: E402
from codebench.sandbox import ToolResult  # noqa: E402


def test_enforced_refuses_finish_before_reproduce():
    gate = AsopGate(enforced=True, has_visible_test=False)
    outcome = gate.can_finish()
    assert not outcome.ok
    assert "REPRODUCE" in outcome.reason


def test_full_happy_path_unlocks_finish():
    gate = AsopGate(enforced=True, has_visible_test=True)
    # STEP 1: repro test written, then run against still-buggy code -> fails
    gate.observe("write_file", {"path": "repro_test.py", "content": "..."}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(False, "AssertionError"))
    assert gate.repro_confirmed_failing
    assert not gate.can_finish().ok  # PROPOSE not done yet

    # STEP 2: fix written
    gate.observe("write_file", {"path": "solution.py", "content": "fixed"}, ToolResult(True, "wrote"))
    assert gate.solution_modified_since_repro
    not_yet = gate.can_finish()
    assert not not_yet.ok
    assert "VALIDATE" in not_yet.reason

    # STEP 3a: repro now passes
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(True, "ok"))
    still_missing_visible = gate.can_finish()
    assert not still_missing_visible.ok
    assert "visible_test" in still_missing_visible.reason

    # STEP 3b: visible test passes too
    gate.observe("run_tests", {"path": "visible_test.py"}, ToolResult(True, "ok"))
    assert gate.can_finish().ok


def test_repro_that_passes_immediately_does_not_confirm_reproduction():
    gate = AsopGate(enforced=True, has_visible_test=False)
    gate.observe("write_file", {"path": "repro_test.py"}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(True, "passed — no bug caught"))
    assert not gate.repro_confirmed_failing
    assert not gate.can_finish().ok


def test_editing_solution_after_validation_requires_revalidation():
    gate = AsopGate(enforced=True, has_visible_test=False)
    gate.observe("write_file", {"path": "repro_test.py"}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(False, "fails as expected"))
    gate.observe("write_file", {"path": "solution.py"}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(True, "passes"))
    assert gate.can_finish().ok

    # agent tinkers further — must re-validate before finish is allowed again
    gate.observe("write_file", {"path": "solution.py"}, ToolResult(True, "wrote again"))
    assert not gate.can_finish().ok


def test_nogate_tracks_but_never_refuses():
    gate = AsopGate(enforced=False, has_visible_test=False)
    assert gate.can_finish().ok  # never enforced, even on turn zero
    gate.observe("write_file", {"path": "repro_test.py"}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(True, "passed immediately"))
    assert gate.can_finish().ok
    # but the underlying tracking is identical to the enforced arm's — the
    # "would it have passed" history is available for analysis even though
    # nothing was ever blocked.
    assert gate.would_pass_history == [False, False]
