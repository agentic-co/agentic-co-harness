"""The repo-tier episode loop — same shape as `codebench.agent.run_episode`
(tool-calling conversation, gate integration, caps, grading after the loop
ends), forked rather than shared for the same reason `repo_arms.py` forked
its gate: the tool contract differs (list_dir, run_tests(path=None) semantics)
enough that bending the single-file loop to also handle repos would have
meant threading an `is_repo` branch through most of its body. Caps here are
higher per the run brief (multi-file localisation takes more turns than a
single function): 20 turns, 40 tool calls by default.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_EVAL_DIR = Path(__file__).resolve().parents[2]
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.provider import DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_S, ChatResult, Endpoint, chat  # noqa: E402
from codebench.repobench.repo_arms import (  # noqa: E402
    TOOLS_SCHEMA, RepoAsopGate, system_prompt, user_prompt,
)
from codebench.repobench.repo_sandbox import RepoSandbox, wrap_argv  # noqa: E402
from codebench.repobench.repos import RepoSpec  # noqa: E402

DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_TOOL_CALLS = 40
DEFAULT_MAX_REFUSALS = 4
DEFAULT_TOOL_TIMEOUT_S = 20
DEFAULT_EPISODE_TIMEOUT_S = 600


@dataclass
class RepoEpisodeResult:
    task_id: str
    dataset: str  # always "repobench" — lets metrics.py's generic dict-based
    # aggregation (summarize_arm/paired_compare) run unmodified over a mix of
    # single-file and repo-tier episode files if anyone ever wants that.
    arm: str
    model: str
    mode: str  # always "ticket-only" — kept for the same reason as `dataset`
    status: str
    turns: int
    tool_calls: int
    called_finish: bool
    prompt_tokens: int
    completion_tokens: int
    wall_time_s: float
    hidden_pass: Optional[bool]
    repro_written: bool
    repro_validity: Optional[bool]
    false_done: bool
    localisation: bool  # repo-tier-only: did the final diff touch the mutated file
    regression_count: int  # newly-failing tests NOT in the task's original failing set
    remaining_expected_count: int  # of the task's original failures, how many still fail
    repo: str
    mutation_kind: str
    max_tokens: int  # what THIS episode called chat() with — logged per the 2026-09-26 finding
    any_truncated: bool  # any turn's response ended with finish_reason == "length"
    damaged_files: list[str]  # source files that shrank >20% vs their buggy start
    error: Optional[str] = None
    hidden_test_output: str = ""
    tool_call_log: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return dict(self.__dict__)


def _tool_message(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def run_repo_episode(
    task: dict,  # one row from mutants.jsonl
    arm: str,
    spec: RepoSpec,
    repo_snapshot_dir: Path,
    mutated_source: str,
    endpoint: Endpoint,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_refusals: int = DEFAULT_MAX_REFUSALS,
    tool_timeout_s: int = DEFAULT_TOOL_TIMEOUT_S,
    episode_timeout_s: int = DEFAULT_EPISODE_TIMEOUT_S,
    temperature: float = 0.2,
    request_timeout_s: int = DEFAULT_TIMEOUT_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> RepoEpisodeResult:
    sandbox = RepoSandbox(
        repo_snapshot_dir, spec, task["mutated_file"], mutated_source,
        tool_timeout_s=tool_timeout_s,
    )
    gate: Optional[RepoAsopGate] = None
    if arm in ("asop", "asop-nogate"):
        gate = RepoAsopGate(enforced=(arm == "asop"))

    messages: list[dict] = [
        {"role": "system", "content": system_prompt(arm, spec.name)},
        {"role": "user", "content": user_prompt(task["ticket"], spec.name)},
    ]

    turns = tool_calls = refusals = 0
    called_finish = False
    status: Optional[str] = None
    error: Optional[str] = None
    prompt_tokens = completion_tokens = 0
    any_truncated = False
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
                temperature=temperature, timeout=request_timeout_s, max_tokens=max_tokens,
            )
            turns += 1
            if not result.ok:
                status, error = "error", result.error
                break
            if result.finish_reason == "length":
                # The provider cut this response off at max_tokens — most
                # dangerous on a write_file/edit_file call, where it can
                # silently shorten the argument instead of erroring. Tracked
                # per episode rather than assumed away; see CODEBENCH.md.
                any_truncated = True

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
                continue

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
                    "list_dir": lambda a: sandbox.list_dir(a.get("path", ".")),
                    "read_file": lambda a: sandbox.read_file(a.get("path", "")),
                    "write_file": lambda a: sandbox.write_file(a.get("path", ""), a.get("content", "")),
                    "edit_file": lambda a: sandbox.edit_file(a.get("path", ""), a.get("old", ""), a.get("new", "")),
                    "run_tests": lambda a: sandbox.run_tests(a.get("path")),
                    "run_python": lambda a: sandbox.run_python(a.get("code", "")),
                }.get(name)
                if dispatch is None:
                    tr_output, tr_ok = f"unknown tool: {name}", False
                else:
                    tr = dispatch(args)
                    tr_output, tr_ok = tr.output, tr.ok
                    if gate is not None:
                        gate.observe(name, args, tr_ok)
                messages.append(_tool_message(tc.get("id", ""), tr_output))
                tool_call_log.append({
                    "name": name,
                    "args": {k: v for k, v in args.items() if k not in ("content", "old", "new")},
                    "ok": tr_ok,
                })
    finally:
        wall_time_s = time.monotonic() - started
        repro_path = sandbox.workdir / "repro_test.py"
        repro_written = repro_path.exists()

        hidden, failing_now = sandbox.run_hidden_test()
        original_failing = set(task.get("failing_node_ids_at_generation") or [])
        regression_count = len(set(failing_now) - original_failing)
        remaining_expected_count = len(set(failing_now) & original_failing)
        localisation = sandbox.localisation()
        damaged_files = sandbox.damaged_files()

        repro_validity: Optional[bool] = None
        if repro_written:
            # Reproduction validity: this repro test, run against the ORIGINAL
            # buggy file (must fail) and the FINAL file (must pass) — both in
            # isolated copies, mirroring the single-file tier's check.
            repro_content = repro_path.read_text()
            final_mutated_file_content = (sandbox.workdir / task["mutated_file"]).read_text()
            against_buggy = _run_repro_against(sandbox, repro_content, task["mutated_file"], mutated_source)
            against_final = _run_repro_against(sandbox, repro_content, task["mutated_file"], final_mutated_file_content)
            repro_validity = (not against_buggy) and against_final

        sandbox.cleanup()

    false_done = called_finish and not hidden.ok

    return RepoEpisodeResult(
        task_id=task["task_id"], dataset="repobench", arm=arm, model=endpoint.model,
        mode="ticket-only", status=status or "error", turns=turns, tool_calls=tool_calls,
        called_finish=called_finish, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        wall_time_s=wall_time_s, hidden_pass=hidden.ok, repro_written=repro_written,
        repro_validity=repro_validity, false_done=false_done, localisation=localisation,
        regression_count=regression_count, remaining_expected_count=remaining_expected_count,
        repo=task["repo"], mutation_kind=task["mutation_kind"],
        max_tokens=max_tokens, any_truncated=any_truncated, damaged_files=damaged_files, error=error,
        hidden_test_output=hidden.output, tool_call_log=tool_call_log,
    )


def _run_repro_against(sandbox: RepoSandbox, repro_content: str, mutated_file: str, file_content: str) -> bool:
    """True if `repro_content` PASSES when `mutated_file` holds `file_content`
    — run in an isolated temp copy of the CURRENT sandbox tree, never
    mutating it. Uses the sandbox's own pytest invocation machinery."""
    import shutil
    import subprocess
    import sys as _sys
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory(prefix="repobench_repro_") as tmp:
        tmp_path = _Path(tmp)
        shutil.copytree(sandbox.workdir, tmp_path, dirs_exist_ok=True)
        (tmp_path / mutated_file).write_text(file_content)
        (tmp_path / "repro_test.py").write_text(repro_content)
        try:
            proc = subprocess.run(
                wrap_argv([_sys.executable, "-m", "pytest", *sandbox.spec.pytest_args, "repro_test.py"]),
                cwd=tmp_path, env=sandbox.env(),
                capture_output=True, text=True, timeout=sandbox.tool_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0
