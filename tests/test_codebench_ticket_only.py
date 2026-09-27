"""Ticket-only mode: `run_episode(..., ticket_only=True)` builds an
`effective_task` with `visible_test_code=None` via `dataclasses.replace` and
passes THAT to the sandbox/prompts/gate — everywhere else already keys off
`visible_test_code` being falsy, so this is the whole mechanism. No network
needed: exercise the same building blocks agent.py composes directly."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.arms import AsopGate, system_prompt, user_prompt  # noqa: E402
from codebench.datasets import Task  # noqa: E402
from codebench.sandbox import Sandbox  # noqa: E402


def _task_with_visible() -> Task:
    return Task(
        task_id="t/0", dataset="test", entry_point="add",
        buggy_code="def add(a, b):\n    return a - b\n",
        visible_test_code="from solution import add\nassert add(1, 1) == 2\n",
        hidden_test_code="from solution import add\nassert add(2, 3) == 5\n",
        prompt="fix add()",
    )


def test_ticket_only_hides_visible_test_from_sandbox():
    task = _task_with_visible()
    effective = replace(task, visible_test_code=None)
    with Sandbox(effective) as sb:
        assert not (sb.workdir / "visible_test.py").exists()
    with Sandbox(task) as sb:  # default mode still writes it
        assert (sb.workdir / "visible_test.py").exists()


def test_ticket_only_prompt_drops_visible_test_mentions():
    task = _task_with_visible()
    effective = replace(task, visible_test_code=None)

    # bare/pva mention the file in the FILES blurb, which lives in user_prompt.
    assert "visible_test.py" in user_prompt(task)
    assert "visible_test.py" not in user_prompt(effective)

    # asop/asop-nogate additionally mention it as a STEP 3 VALIDATE sub-check
    # in the system prompt itself.
    for arm in ("asop", "asop-nogate"):
        assert "visible_test.py" in system_prompt(arm, task)
        assert "visible_test.py" not in system_prompt(arm, effective)


def test_ticket_only_gate_does_not_require_visible_test():
    task = _task_with_visible()
    effective = replace(task, visible_test_code=None)
    gate = AsopGate(enforced=True, has_visible_test=bool(effective.visible_test_code))
    assert gate.has_visible_test is False
    # walk REPRODUCE -> PROPOSE -> VALIDATE with no visible_test.py in play
    from codebench.sandbox import ToolResult
    gate.observe("write_file", {"path": "repro_test.py"}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(False, "fails as expected"))
    gate.observe("write_file", {"path": "solution.py"}, ToolResult(True, "wrote"))
    gate.observe("run_tests", {"path": "repro_test.py"}, ToolResult(True, "passes"))
    assert gate.can_finish().ok  # no visible_test check blocks it in ticket-only
