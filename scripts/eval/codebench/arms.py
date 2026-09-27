"""The four arms: shared tool schema, per-arm system prompts, and the ASOP
gate state machine that turns REPRODUCE/VALIDATE into code-checked gates.

Design choice worth flagging: unlike the τ²/SOPBench ASOP engine
(`scripts/eval/asop_engine.py`), the procedure here is NOT revealed one step
at a time — the full 4-step text is in the system prompt from turn one, for
both `asop` and `asop-nogate`. A single-file bug-fix task is a much shorter
horizon than the multi-turn SOP conversations that engine targets, so
stepwise reveal wasn't worth building for v1. `asop` and `asop-nogate` get the
EXACT SAME prompt text — the only difference is whether `AsopGate` actually
intercepts tool calls, which is the whole point of running both.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_PARENT = Path(__file__).resolve().parents[1]
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from codebench.datasets import Task  # noqa: E402
from codebench.sandbox import ToolResult  # noqa: E402

ARMS = ("bare", "pva", "asop", "asop-nogate")

TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's full contents from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative path, e.g. 'solution.py'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite (or create) a file in the workspace with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative path, e.g. 'solution.py' or 'repro_test.py'"},
                    "content": {"type": "string", "description": "the full new file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run a test file that already exists in the workspace as `python3 <path>`. Reports pass/fail and any output.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "e.g. 'visible_test.py' or 'repro_test.py'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Run an ad hoc Python snippet in the workspace and see its stdout/stderr. Not saved as a file.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this when the task is complete. No further tool calls are processed after this.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _files_blurb(task: Task) -> str:
    lines = ["Files already in the workspace:", "- `solution.py` — contains the bug you must fix."]
    if task.visible_test_code:
        lines.append("- `visible_test.py` — example tests you may run for a sanity check (not the full grading suite).")
    return "\n".join(lines)


def _tools_blurb() -> str:
    return (
        "Tools available (call with these EXACT argument names):\n"
        "- read_file(path)\n"
        "- write_file(path, content)\n"
        "- run_tests(path)\n"
        "- run_python(code)\n"
        "- finish()  — ends the episode; call it only when you believe the task is done."
    )


def user_prompt(task: Task) -> str:
    return f"Task:\n{task.prompt}\n\n{_files_blurb(task)}"


def system_prompt(arm: str, task: Task) -> str:
    if arm == "bare":
        return (
            f"You are a software engineer fixing a bug in a single Python file.\n\n"
            f"{_tools_blurb()}\n\n"
            f"Fix the bug in `solution.py`. When you believe it is fixed, call finish()."
        )

    if arm == "pva":
        return (
            f"You are a software engineer fixing a bug in a single Python file.\n\n"
            f"{_tools_blurb()}\n\n"
            "Follow this loop:\n"
            "1. PLAN: before changing any code, state in 1-3 sentences what you believe "
            "the bug is and how you will confirm it.\n"
            "2. ACT: use write_file to change `solution.py`.\n"
            "3. VERIFY: use run_tests to check your fix (run `visible_test.py` if it "
            "exists). If verification fails, go back to PLAN.\n\n"
            "Only call finish() after a VERIFY step has actually run and passed.\n"
            "Note: this loop is guidance, not enforced by the system — no tool call will "
            "be blocked for skipping a step, but skipping VERIFY is how bugs ship."
        )

    if arm in ("asop", "asop-nogate"):
        visible_line = (
            "  b. Call run_tests(path=\"visible_test.py\") — must PASS.\n"
            if task.visible_test_code else ""
        )
        return (
            f"You are a software engineer fixing a bug in a single Python file.\n\n"
            f"{_tools_blurb()}\n\n"
            "You must follow this exact procedure, in order:\n\n"
            "STEP 1 — REPRODUCE:\n"
            "  a. Call write_file(path=\"repro_test.py\", content=<a short Python script "
            "that does `from solution import <entry_point>` and asserts the CORRECT "
            "expected behavior for at least one input>.\n"
            "  b. Call run_tests(path=\"repro_test.py\").\n"
            "  c. This run MUST FAIL. If it passes, your test does not actually "
            "reproduce the bug — rewrite repro_test.py and try again.\n\n"
            "STEP 2 — PROPOSE:\n"
            "  a. Call write_file(path=\"solution.py\", content=<your fix>).\n\n"
            "STEP 3 — VALIDATE:\n"
            "  a. Call run_tests(path=\"repro_test.py\") — must now PASS.\n"
            f"{visible_line}"
            "  c. Do not call finish() until every check in this step passes.\n\n"
            "STEP 4 — only after step 3 fully passes, call finish()."
        )

    raise ValueError(f"unknown arm: {arm!r}")


@dataclass
class GateOutcome:
    ok: bool
    reason: str = ""


@dataclass
class AsopGate:
    """Tracks REPRODUCE/PROPOSE/VALIDATE state from tool calls the agent
    already made, and (when `enforced`) is the sole authority on whether
    `finish()` is allowed to actually end the episode.

    `enforced=False` (the `asop-nogate` arm) tracks the exact same state —
    useful for a bonus "would the gate have passed" comparison — but
    `can_finish()` always returns True, so nothing is ever refused.
    """

    enforced: bool
    has_visible_test: bool
    repro_confirmed_failing: bool = False
    solution_modified_since_repro: bool = False
    last_repro_result: Optional[bool] = None
    last_visible_result: Optional[bool] = None
    would_pass_history: list[bool] = field(default_factory=list)

    def observe(self, tool_name: str, args: dict, result: ToolResult) -> None:
        path = args.get("path") if isinstance(args, dict) else None

        if tool_name == "write_file" and path == "solution.py":
            if self.repro_confirmed_failing:
                self.solution_modified_since_repro = True
            # Any edit invalidates the last validation result — re-run required.
            self.last_repro_result = None
            self.last_visible_result = None

        if tool_name == "write_file" and path == "repro_test.py":
            # Rewriting the reproduction means it has to re-earn "confirmed failing".
            self.repro_confirmed_failing = False
            self.solution_modified_since_repro = False

        if tool_name == "run_tests" and path == "repro_test.py":
            if not self.solution_modified_since_repro:
                # Still checking against the (still-buggy) original — this is the
                # REPRODUCE gate.
                self.repro_confirmed_failing = not result.ok
            else:
                # Checking after a fix attempt — this is (half of) the VALIDATE gate.
                self.last_repro_result = result.ok

        if tool_name == "run_tests" and path == "visible_test.py":
            self.last_visible_result = result.ok

    def can_finish(self) -> GateOutcome:
        if not self.repro_confirmed_failing:
            outcome = GateOutcome(False, (
                "STEP 1 (REPRODUCE) not satisfied: run_tests(path='repro_test.py') has "
                "not yet been shown to FAIL against the current solution.py."
            ))
        elif not self.solution_modified_since_repro:
            outcome = GateOutcome(False, (
                "STEP 2 (PROPOSE) not satisfied: solution.py has not been modified "
                "since the reproduction was confirmed failing."
            ))
        elif self.last_repro_result is not True:
            outcome = GateOutcome(False, (
                "STEP 3 (VALIDATE) not satisfied: repro_test.py must PASS against the "
                "current solution.py — call run_tests(path='repro_test.py') again."
            ))
        elif self.has_visible_test and self.last_visible_result is not True:
            outcome = GateOutcome(False, (
                "STEP 3 (VALIDATE) not satisfied: visible_test.py must also PASS — "
                "call run_tests(path='visible_test.py')."
            ))
        else:
            outcome = GateOutcome(True)

        self.would_pass_history.append(outcome.ok)
        if self.enforced:
            return outcome
        return GateOutcome(True)  # nogate: tracked, never enforced
