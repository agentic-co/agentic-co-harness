"""The episode loop: one (task, arm) pair, one model, start to finish.

Drives the tool-calling conversation against `provider.chat`, dispatches tool
calls to a `Sandbox`, applies the `AsopGate` when the arm has one, and stops
on `finish()`, a gate-exhaustion forced stop, or a cap (turns/tool
calls/wall time). Grading (pass@1, reproduction validity) happens once, after
the loop ends, against the hidden test the agent never saw.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

_PARENT = Path(__file__).resolve().parents[1]
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from codebench.arms import TOOLS_SCHEMA, AsopGate, system_prompt, user_prompt  # noqa: E402
from codebench.datasets import Task  # noqa: E402
from codebench.provider import DEFAULT_TIMEOUT_S, ChatResult, Endpoint, chat  # noqa: E402
from codebench.sandbox import Sandbox  # noqa: E402

DEFAULT_MAX_TURNS = 8
DEFAULT_MAX_TOOL_CALLS = 20
DEFAULT_MAX_REFUSALS = 3
DEFAULT_TOOL_TIMEOUT_S = 10
DEFAULT_EPISODE_TIMEOUT_S = 240


@dataclass
class EpisodeResult:
    task_id: str
    dataset: str
    arm: str
    model: str
    mode: str  # "default" | "ticket-only"
    status: str  # finished | gate_exhausted | cutoff_turns | cutoff_tool_calls | cutoff_time | error
    turns: int
    tool_calls: int
    called_finish: bool
    prompt_tokens: int
    completion_tokens: int
    wall_time_s: float
    hidden_pass: Optional[bool]
    repro_written: bool
    repro_validity: Optional[bool]  # None if arm has no repro_test.py, else True/False
    false_done: bool
    error: Optional[str] = None
    final_solution_code: str = ""
    hidden_test_output: str = ""
    tool_call_log: list[dict] = field(default_factory=list)  # [{name, args, ok}] — no file bodies, kept small

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        # final_solution_code / hidden_test_output can be large; the JSONL writer
        # decides whether to keep them (see run_codebench.py --no-transcripts).
        return d


def _tool_message(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def run_episode(
    task: Task,
    arm: str,
    endpoint: Endpoint,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_refusals: int = DEFAULT_MAX_REFUSALS,
    tool_timeout_s: int = DEFAULT_TOOL_TIMEOUT_S,
    episode_timeout_s: int = DEFAULT_EPISODE_TIMEOUT_S,
    temperature: float = 0.2,
    request_timeout_s: int = DEFAULT_TIMEOUT_S,
    ticket_only: bool = False,
) -> EpisodeResult:
    # ticket-only: the agent gets the buggy code + the natural-language task
    # description, no visible test at all — the principal's real use case
    # (a ticket, not a test suite). Every downstream consumer of the task
    # (Sandbox, the prompt builders, AsopGate) already keys off
    # `visible_test_code` being falsy, so hiding it here is the whole change;
    # `task` itself (and its hidden_test_code/buggy_code) is untouched, so
    # grading is identical to default mode.
    effective_task = replace(task, visible_test_code=None) if ticket_only else task
    mode = "ticket-only" if ticket_only else "default"

    sandbox = Sandbox(effective_task, tool_timeout_s=tool_timeout_s)
    gate: Optional[AsopGate] = None
    if arm in ("asop", "asop-nogate"):
        gate = AsopGate(enforced=(arm == "asop"), has_visible_test=bool(effective_task.visible_test_code))

    messages: list[dict] = [
        {"role": "system", "content": system_prompt(arm, effective_task)},
        {"role": "user", "content": user_prompt(effective_task)},
    ]

    turns = 0
    tool_calls = 0
    refusals = 0
    called_finish = False
    status: Optional[str] = None
    error: Optional[str] = None
    prompt_tokens = 0
    completion_tokens = 0
    tool_call_log: list[dict] = []
    started = time.monotonic()

    try:
        while status is None:
            if turns >= max_turns:
                status = "cutoff_turns"
                break
            if time.monotonic() - started > episode_timeout_s:
                status = "cutoff_time"
                break

            result: ChatResult = chat(
                endpoint, messages, tools=TOOLS_SCHEMA,
                temperature=temperature, timeout=request_timeout_s,
            )
            turns += 1
            if not result.ok:
                status, error = "error", result.error
                break

            prompt_tokens += int(result.usage.get("prompt_tokens") or 0)
            completion_tokens += int(result.usage.get("completion_tokens") or 0)

            msg = result.message
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {}),
            })

            calls = msg.get("tool_calls") or []
            if not calls:
                continue  # plain-text turn, no tool call — burns a turn, loop continues

            for tc in calls:
                if tool_calls >= max_tool_calls:
                    status = "cutoff_tool_calls"
                    break
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls += 1

                if name == "finish":
                    called_finish = True
                    if gate is None:
                        ok, reason = True, ""
                    else:
                        outcome = gate.can_finish()
                        ok, reason = outcome.ok, outcome.reason
                    if ok:
                        messages.append(_tool_message(tc.get("id", ""), "finished."))
                        tool_call_log.append({"name": name, "args": args, "ok": True})
                        status = "finished"
                        break
                    refusals += 1
                    messages.append(_tool_message(tc.get("id", ""), f"finish refused: {reason}"))
                    tool_call_log.append({"name": name, "args": args, "ok": False, "reason": reason})
                    if refusals > max_refusals:
                        status = "gate_exhausted"
                        break
                    continue

                dispatch = {
                    "read_file": lambda a: sandbox.read_file(a.get("path", "")),
                    "write_file": lambda a: sandbox.write_file(a.get("path", ""), a.get("content", "")),
                    "run_tests": lambda a: sandbox.run_tests(a.get("path", "")),
                    "run_python": lambda a: sandbox.run_python(a.get("code", "")),
                }.get(name)
                if dispatch is None:
                    tr_output, tr_ok = f"unknown tool: {name}", False
                else:
                    tr = dispatch(args)
                    tr_output, tr_ok = tr.output, tr.ok
                    if gate is not None:
                        gate.observe(name, args, tr)
                messages.append(_tool_message(tc.get("id", ""), tr_output))
                tool_call_log.append({"name": name, "args": {k: v for k, v in args.items() if k != "content"}, "ok": tr_ok})
    finally:
        wall_time_s = time.monotonic() - started
        final_solution_code = (sandbox.workdir / "solution.py").read_text()
        repro_path = sandbox.workdir / "repro_test.py"
        repro_written = repro_path.exists()

        hidden = sandbox.run_hidden_test(final_solution_code)
        repro_validity: Optional[bool] = None
        if repro_written:
            against_original = sandbox.run_file_against("repro_test.py", sandbox.original_buggy_code)
            against_final = sandbox.run_file_against("repro_test.py", final_solution_code)
            repro_validity = (not against_original.ok) and against_final.ok

        sandbox.cleanup()

    false_done = called_finish and not hidden.ok

    return EpisodeResult(
        task_id=task.task_id,
        dataset=task.dataset,
        arm=arm,
        model=endpoint.model,
        mode=mode,
        status=status or "error",
        turns=turns,
        tool_calls=tool_calls,
        called_finish=called_finish,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        wall_time_s=wall_time_s,
        hidden_pass=hidden.ok,
        repro_written=repro_written,
        repro_validity=repro_validity,
        false_done=false_done,
        error=error,
        final_solution_code=final_solution_code,
        hidden_test_output=hidden.output,
        tool_call_log=tool_call_log,
    )
