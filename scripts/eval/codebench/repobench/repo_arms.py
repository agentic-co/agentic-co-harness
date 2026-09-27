"""Repo-tier arms: same bare/pva/asop/asop-nogate shape as the single-file
tier, adapted for a whole repo — an extra `list_dir` tool, `run_tests` takes
an optional path (None = the repo's own full suite), and there is NEVER a
visible test (this tier IS the ticket-only condition, always: the principal's
real use case is a ticket, not a test file).

`RepoAsopGate` below is a SMALL, DELIBERATE fork of `codebench.arms.AsopGate`
rather than a reuse — that class's VALIDATE step keys off a literal
`path == "visible_test.py"` string, and this tier's second half of VALIDATE
is "the whole suite passes" (`run_tests()` with no path at all, i.e.
`path is None`), which is a different signal, not a different VALUE of the
same one. Forking ~40 lines to get the condition right beat bending the
original class's string-keyed check to also mean "no path given."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ARMS = ("bare", "pva", "asop", "asop-nogate")

TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories (one level) at a path in the repo. Directories are shown with a trailing '/'.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative path, '.' for repo root"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's full contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a NEW file, or fully replace an existing SHORT file (<= 200 lines). "
                "Refused on a longer existing file — use edit_file for those, so you only send "
                "the lines that actually change."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Change part of an EXISTING file by exact string replacement. `old` must match "
                "the file's current content EXACTLY (whitespace and indentation included) and "
                "must be unique in the file — include enough surrounding context that it only "
                "matches once. This is how you change existing source files; write_file is for "
                "NEW files only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "the exact text to replace, copied from read_file's output"},
                    "new": {"type": "string", "description": "the text to replace it with"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the test suite with pytest. Omit `path` to run the repo's ENTIRE test "
                "suite. Give `path` to run just one file or nodeid (e.g. a test file you wrote)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "optional — a specific test file or nodeid"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Run an ad hoc Python snippet in the repo root and see its output. Not saved as a file.",
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

_TOOLS_BLURB = (
    "Tools available (call with these EXACT argument names):\n"
    "- list_dir(path='.') — one level, directories shown with a trailing '/'\n"
    "- read_file(path)\n"
    "- edit_file(path, old, new) — change part of an EXISTING file by exact string "
    "replacement. `old` must match the file's CURRENT content exactly (copy it from "
    "read_file's output — whitespace and indentation included) and must be unique in the "
    "file. THIS is how you change existing source files.\n"
    "- write_file(path, content) — for a NEW file only, or fully replacing an existing file "
    "of <= 200 lines. Refused on a longer existing file (use edit_file instead) — resending a "
    "whole large file risks the response being cut off before the file ends.\n"
    "- run_tests(path=None) — path omitted runs the WHOLE suite; a path runs just that file/nodeid\n"
    "- run_python(code)\n"
    "- finish() — ends the episode; call it only when you believe the task is done.\n\n"
    "IMPORTANT: this repo's tests run through pytest. A test file you write yourself must "
    "define a real test function (e.g. `def test_repro():` with an `assert` inside it) — a "
    "bare top-level `assert` with no enclosing `def test_...():` is not collected as a test "
    "by pytest and will not run."
)


def user_prompt(ticket: str, repo_name: str) -> str:
    return (
        f"You have been handed a bug ticket for the `{repo_name}` Python codebase (a real, "
        f"multi-file repository — you have not been told which file or function is involved):\n\n"
        f"{ticket}\n\n"
        "Explore the repository yourself to find the affected code."
    )


def system_prompt(arm: str, repo_name: str) -> str:
    if arm == "bare":
        return (
            f"You are a software engineer fixing a bug in the `{repo_name}` repository, given "
            f"only a ticket describing the symptom (no test file, no pointer to which file or "
            f"function is affected).\n\n{_TOOLS_BLURB}\n\n"
            "Find and fix the bug. When you believe it is fixed, call finish()."
        )

    if arm == "pva":
        return (
            f"You are a software engineer fixing a bug in the `{repo_name}` repository, given "
            f"only a ticket describing the symptom.\n\n{_TOOLS_BLURB}\n\n"
            "Follow this loop:\n"
            "1. PLAN: explore the repo (list_dir/read_file) and state in 2-4 sentences where "
            "you believe the bug is and why, before changing any code.\n"
            "2. ACT: use edit_file(path, old, new) to change the existing source (write_file "
            "is for NEW files only).\n"
            "3. VERIFY: use run_tests (the whole suite) to check your fix. If it fails, go "
            "back to PLAN.\n\n"
            "Only call finish() after a VERIFY step has actually run and the whole suite passed.\n"
            "Note: this loop is guidance, not enforced by the system — no tool call will be "
            "blocked for skipping a step."
        )

    if arm in ("asop", "asop-nogate"):
        return (
            f"You are a software engineer fixing a bug in the `{repo_name}` repository, given "
            f"only a ticket describing the symptom.\n\n{_TOOLS_BLURB}\n\n"
            "You must follow this exact procedure, in order:\n\n"
            "STEP 1 — REPRODUCE:\n"
            "  a. Explore the repo (list_dir/read_file) to find the affected area.\n"
            "  b. Call write_file(path=\"repro_test.py\", content=<a pytest test file with a "
            "single `def test_repro():` function that asserts the CORRECT expected behavior "
            "described in the ticket>.\n"
            "  c. Call run_tests(path=\"repro_test.py\").\n"
            "  d. This run MUST FAIL. If it passes, your test does not actually reproduce the "
            "bug — rewrite repro_test.py and try again.\n\n"
            "STEP 2 — PROPOSE:\n"
            "  a. Call edit_file(path=<the source file>, old=<the exact buggy lines, copied "
            "from read_file's output>, new=<your fix>) to change the source. Only use "
            "write_file here if the fix genuinely requires a NEW file.\n\n"
            "STEP 3 — VALIDATE:\n"
            "  a. Call run_tests(path=\"repro_test.py\") — must now PASS.\n"
            "  b. Call run_tests() with no path — the WHOLE suite must PASS (this also catches "
            "any new failures your fix introduced elsewhere in the codebase).\n"
            "  c. Do not call finish() until every check in this step passes.\n\n"
            "STEP 4 — only after step 3 fully passes, call finish()."
        )

    raise ValueError(f"unknown arm: {arm!r}")


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    reason: str = ""


@dataclass
class RepoAsopGate:
    """REPRODUCE/PROPOSE/VALIDATE for the repo tier. VALIDATE here is two
    checks: the agent's own repro_test.py passes, AND the whole suite passes
    (`run_tests()` with no path) — the second one is what makes a
    regression the agent's own fix introduced elsewhere block `finish()`
    too, not just leave it for hidden grading to discover after the fact.
    """

    enforced: bool
    repro_confirmed_failing: bool = False
    solution_modified_since_repro: bool = False
    last_repro_result: Optional[bool] = None
    last_full_suite_result: Optional[bool] = None
    would_pass_history: list[bool] = field(default_factory=list)

    def observe(self, tool_name: str, args: dict, ok: bool) -> None:
        path = args.get("path") if isinstance(args, dict) else None

        # write_file and edit_file are equivalent PROPOSE actions — either
        # is "the agent changed a file," and the gate doesn't care which
        # tool did it, only what path was touched.
        if tool_name in ("write_file", "edit_file") and path == "repro_test.py":
            self.repro_confirmed_failing = False
            self.solution_modified_since_repro = False
            return

        if tool_name in ("write_file", "edit_file") and path not in (None, "repro_test.py"):
            # Any OTHER file change (source or otherwise) counts as a
            # PROPOSE attempt once REPRODUCE has been confirmed — and
            # invalidates the last VALIDATE results, forcing a re-check.
            if self.repro_confirmed_failing:
                self.solution_modified_since_repro = True
            self.last_repro_result = None
            self.last_full_suite_result = None
            return

        if tool_name == "run_tests" and path == "repro_test.py":
            if not self.solution_modified_since_repro:
                self.repro_confirmed_failing = not ok
            else:
                self.last_repro_result = ok
            return

        if tool_name == "run_tests" and path is None:
            self.last_full_suite_result = ok

    def can_finish(self) -> GateOutcome:
        if not self.repro_confirmed_failing:
            outcome = GateOutcome(False, (
                "STEP 1 (REPRODUCE) not satisfied: run_tests(path='repro_test.py') has not "
                "yet been shown to FAIL against the current code."
            ))
        elif not self.solution_modified_since_repro:
            outcome = GateOutcome(False, (
                "STEP 2 (PROPOSE) not satisfied: no source file has been edited since the "
                "reproduction was confirmed failing."
            ))
        elif self.last_repro_result is not True:
            outcome = GateOutcome(False, (
                "STEP 3 (VALIDATE) not satisfied: repro_test.py must PASS against the current "
                "code — call run_tests(path='repro_test.py') again."
            ))
        elif self.last_full_suite_result is not True:
            outcome = GateOutcome(False, (
                "STEP 3 (VALIDATE) not satisfied: the WHOLE suite must pass — call run_tests() "
                "with no path."
            ))
        else:
            outcome = GateOutcome(True)

        self.would_pass_history.append(outcome.ok)
        if self.enforced:
            return outcome
        return GateOutcome(True)  # nogate: tracked, never enforced
