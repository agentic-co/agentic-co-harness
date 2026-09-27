"""RepoAsopGate: REPRODUCE/PROPOSE/VALIDATE for the repo tier, where VALIDATE
means both the agent's own repro test AND the whole suite pass. Pure unit
test — no sandbox, no model."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.repo_arms import RepoAsopGate  # noqa: E402


def test_refuses_finish_before_reproduce():
    gate = RepoAsopGate(enforced=True)
    outcome = gate.can_finish()
    assert not outcome.ok
    assert "REPRODUCE" in outcome.reason


def test_full_happy_path_requires_both_repro_and_whole_suite():
    gate = RepoAsopGate(enforced=True)
    gate.observe("write_file", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, False)  # fails on buggy code
    assert gate.repro_confirmed_failing
    assert not gate.can_finish().ok  # PROPOSE not done

    gate.observe("write_file", {"path": "pkg/mod.py"}, True)  # the actual fix
    assert gate.solution_modified_since_repro
    not_yet = gate.can_finish()
    assert not not_yet.ok
    assert "VALIDATE" in not_yet.reason

    gate.observe("run_tests", {"path": "repro_test.py"}, True)
    still_missing_suite = gate.can_finish()
    assert not still_missing_suite.ok
    assert "WHOLE suite" in still_missing_suite.reason

    gate.observe("run_tests", {}, True)  # run_tests() — path absent means None
    assert gate.can_finish().ok


def test_repro_passing_immediately_does_not_confirm_reproduction():
    gate = RepoAsopGate(enforced=True)
    gate.observe("write_file", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, True)  # passed — no bug caught
    assert not gate.repro_confirmed_failing
    assert not gate.can_finish().ok


def test_full_suite_passing_but_repro_not_rerun_still_blocks():
    gate = RepoAsopGate(enforced=True)
    gate.observe("write_file", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, False)
    gate.observe("write_file", {"path": "pkg/mod.py"}, True)
    gate.observe("run_tests", {}, True)  # whole suite happens to pass already
    still_missing_repro = gate.can_finish()
    assert not still_missing_repro.ok
    assert "repro_test.py" in still_missing_repro.reason


def test_editing_source_again_after_validation_requires_revalidation():
    gate = RepoAsopGate(enforced=True)
    gate.observe("write_file", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, False)
    gate.observe("write_file", {"path": "pkg/mod.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {}, True)
    assert gate.can_finish().ok

    gate.observe("write_file", {"path": "pkg/mod.py"}, True)  # tinkers more
    assert not gate.can_finish().ok


def test_edit_file_is_treated_the_same_as_write_file_for_propose():
    # edit_file is now the PRIMARY way to change an existing file (2026-09-26
    # fix); the gate must recognize it as a PROPOSE action exactly like
    # write_file always has been.
    gate = RepoAsopGate(enforced=True)
    gate.observe("write_file", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, False)
    gate.observe("edit_file", {"path": "pkg/mod.py"}, True)
    assert gate.solution_modified_since_repro
    gate.observe("run_tests", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {}, True)
    assert gate.can_finish().ok


def test_nogate_tracks_but_never_refuses():
    gate = RepoAsopGate(enforced=False)
    assert gate.can_finish().ok
    gate.observe("write_file", {"path": "repro_test.py"}, True)
    gate.observe("run_tests", {"path": "repro_test.py"}, True)  # passed immediately
    assert gate.can_finish().ok
    assert gate.would_pass_history == [False, False]
