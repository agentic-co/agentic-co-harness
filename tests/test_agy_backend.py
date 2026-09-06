"""The Antigravity backend: a real subprocess, and the failure that looks like success.

`agy --print` cannot prompt. A tool needing permission is auto-denied and the
process still exits ZERO, printing an explanation instead of doing the work.
That is the worst shape a failure can take — indistinguishable from a model
that simply had nothing to say — so the runner reads the output, not only the
exit code.
"""

from __future__ import annotations

import stat
from pathlib import Path

from agentco_harness import backends
from agentco_harness.executor import run_agy_task


def _fake_agy(tmp_path: Path, body: str) -> str:
    b = tmp_path / "agy"
    b.write_text("#!/bin/sh\n" + body)
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    return str(b)


def test_a_successful_run_returns_its_output(tmp_path):
    agy = _fake_agy(tmp_path, 'echo "patched slugify"\n')
    r = run_agy_task("do the thing", agy_bin=agy)
    assert r.success is True and "patched slugify" in r.output


def test_an_auto_denied_permission_is_a_failure_despite_exit_zero(tmp_path):
    """The whole reason this backend passes --dangerously-skip-permissions."""
    agy = _fake_agy(tmp_path, 'echo "a tool required a permission that headless mode cannot prompt for, so it was auto-denied"\nexit 0\n')
    r = run_agy_task("do the thing", agy_bin=agy)
    assert r.success is False
    assert r.exit_code == 0
    assert "auto-denied" in r.error


def test_permissions_are_skipped_because_print_mode_cannot_ask(tmp_path):
    agy = _fake_agy(tmp_path, 'echo "$@" \n')
    r = run_agy_task("x", agy_bin=agy)
    assert "--dangerously-skip-permissions" in r.output
    assert "--print" in r.output


def test_a_model_pin_reaches_the_command(tmp_path):
    agy = _fake_agy(tmp_path, 'echo "$@"\n')
    assert "--model" in run_agy_task("x", model="gemini-3.1-pro", agy_bin=agy).output


def test_a_nonzero_exit_is_loud(tmp_path):
    agy = _fake_agy(tmp_path, 'echo "boom" >&2\nexit 4\n')
    r = run_agy_task("x", agy_bin=agy)
    assert r.success is False and r.exit_code == 4 and "boom" in r.error


def test_a_missing_binary_says_what_to_do(tmp_path):
    r = run_agy_task("x", agy_bin=str(tmp_path / "nope"))
    assert r.success is False
    assert "not found on PATH" in r.error and "log in once" in r.error


def test_the_work_happens_in_the_beads_workdir(tmp_path):
    workdir = tmp_path / "repo"; workdir.mkdir()
    agy = _fake_agy(tmp_path, 'pwd -P\n')
    assert str(workdir.resolve()) in run_agy_task("x", agy_bin=agy, cwd=str(workdir)).output


def test_agy_is_agentic_and_carries_the_declared_google_ceiling():
    from agentco_harness import orchestrator  # noqa: F401  (registers it)
    from agentco_harness.egress import AGENT_ROUTE

    b = backends.resolve("agy")
    assert b is not None
    assert b.capabilities == frozenset()      # holds a shell and a tree
    assert b.route == "BELLOWS"
    assert AGENT_ROUTE["agy"] == "BELLOWS"    # registration declared it to the gate
