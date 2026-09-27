"""Sandbox: file tools stay inside the workspace, the hidden test is never
written where the agent's tools can reach it, and grading runs in an isolated
copy so the agent's own workdir is never mutated by grading."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.datasets import Task  # noqa: E402
from codebench.sandbox import Sandbox  # noqa: E402


def _task(buggy="def add(a, b):\n    return a - b\n", visible=None, hidden=None) -> Task:
    return Task(
        task_id="t/0", dataset="test", entry_point="add",
        buggy_code=buggy,
        visible_test_code=visible or "from solution import add\nassert add(1, 1) == 2\n",
        hidden_test_code=hidden or "from solution import add\nassert add(2, 3) == 5\nassert add(0, 0) == 0\n",
        prompt="fix add()",
    )


def test_read_write_roundtrip():
    with Sandbox(_task()) as sb:
        assert "return a - b" in sb.read_file("solution.py").output
        r = sb.write_file("solution.py", "def add(a, b):\n    return a + b\n")
        assert r.ok
        assert "return a + b" in sb.read_file("solution.py").output


def test_path_escape_refused():
    with Sandbox(_task()) as sb:
        assert not sb.read_file("../../etc/passwd").ok
        assert not sb.write_file("/etc/passwd", "pwned").ok
        assert not sb.write_file("../outside.py", "x = 1").ok


def test_oversize_write_refused():
    with Sandbox(_task()) as sb:
        r = sb.write_file("big.py", "x = 1\n" * 20_000)
        assert not r.ok


def test_run_tests_fails_on_buggy_then_passes_after_fix():
    with Sandbox(_task()) as sb:
        r = sb.run_tests("visible_test.py")
        assert not r.ok  # a - b gives add(1,1) == 0, visible test wants 2
        sb.write_file("solution.py", "def add(a, b):\n    return a + b\n")
        r2 = sb.run_tests("visible_test.py")
        assert r2.ok


def test_run_python_executes_and_times_out():
    with Sandbox(_task(), tool_timeout_s=1) as sb:
        ok = sb.run_python("print(1 + 1)")
        assert ok.ok and "2" in ok.output
        slow = sb.run_python("import time; time.sleep(5)")
        assert not slow.ok
        assert "timed out" in slow.output


def test_hidden_test_never_written_to_workspace():
    with Sandbox(_task()) as sb:
        assert not (sb.workdir / "hidden_test.py").exists()
        # even after grading runs
        sb.run_hidden_test()
        assert not (sb.workdir / "hidden_test.py").exists()


def test_grade_pass_and_fail():
    with Sandbox(_task()) as sb:
        assert not sb.run_hidden_test().ok  # buggy solution fails hidden test
        fixed = "def add(a, b):\n    return a + b\n"
        assert sb.run_hidden_test(fixed).ok
        # grading must not mutate the agent's own solution.py
        assert "return a - b" in sb.read_file("solution.py").output


def test_run_file_against_isolated_solution():
    with Sandbox(_task()) as sb:
        sb.write_file("repro_test.py", "from solution import add\nassert add(1, 1) == 3\n")
        buggy_result = sb.run_file_against("repro_test.py", sb.original_buggy_code)
        assert not buggy_result.ok  # a - b gives add(1,1) == 0, not 3
        fixed_result = sb.run_file_against("repro_test.py", "def add(a, b):\n    return a + b + 1\n")
        assert fixed_result.ok  # contrived "fix" that happens to satisfy the repro test's == 3
