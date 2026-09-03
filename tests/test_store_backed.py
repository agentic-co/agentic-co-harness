"""Store-backed executor path: TaskResult round-trips, run_store_backed_task
prompt/env/truncation behavior, and orchestrator routing — including the
critical "agent finished but never wrote the result back" failure.
"""

from __future__ import annotations

import stat
from pathlib import Path

import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.beads import TaskResult, TaskStatus
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.executor import ExecResult, run_claude_task, run_store_backed_task
from agentco_harness.orchestrator import Orchestrator


def _fake_claude(tmp_path: Path, script_body: str) -> str:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n" + script_body)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


# --------------------------------------------------------------- TaskResult

def test_taskresult_roundtrips_through_json():
    tr = TaskResult(status="complete", output="did the thing", reply="hi there")
    back = TaskResult.from_str(tr.to_json())
    assert back == tr


def test_taskresult_omits_none_fields_in_json():
    tr = TaskResult(status="complete", output="x")
    j = tr.to_json()
    assert "reply" not in j and "obsidian_note" not in j and "error" not in j


def test_taskresult_from_str_ignores_unknown_fields():
    tr = TaskResult.from_str('{"status": "complete", "output": "x", "future_field": 1}')
    assert tr.status == "complete" and tr.output == "x"


def test_taskresult_from_task_returns_none_when_unstructured():
    class _T:
        result = "just a plain stdout blob, not JSON"

    assert TaskResult.from_task(_T()) is None


def test_taskresult_from_task_returns_none_when_empty():
    class _T:
        result = None

    assert TaskResult.from_task(_T()) is None


def test_taskresult_from_task_parses_structured_result():
    class _T:
        result = '{"status": "partial", "output": "halfway", "continuation_hint": "finish step 2"}'

    tr = TaskResult.from_task(_T())
    assert tr.status == "partial" and tr.continuation_hint == "finish step 2"


# ----------------------------------------------------- run_store_backed_task

def test_store_backed_prompt_carries_task_id_and_config(tmp_path):
    # `cat` echoes the stdin prompt to stdout so we can inspect what the agent received.
    claude = _fake_claude(tmp_path, "cat\n")
    result = run_store_backed_task(
        "task-123", config_path="/some/where/config.yaml", claude_bin=claude
    )
    assert result.success is True
    assert "task-123" in result.output
    assert "--config /some/where/config.yaml" in result.output
    # The agent is instructed to write its result back before finishing.
    assert "tasks complete task-123" in result.output


def test_model_flag_passed_when_set(tmp_path):
    claude = _fake_claude(tmp_path, 'echo "$@"\n')  # echo args to stdout
    result = run_store_backed_task("t", claude_bin=claude, model="haiku")
    assert "--model haiku" in result.output


def test_no_model_flag_when_unset(tmp_path):
    claude = _fake_claude(tmp_path, 'echo "$@"\n')
    result = run_store_backed_task("t", claude_bin=claude)
    assert "--model" not in result.output  # inherit the claude CLI default


def test_store_backed_prompt_omits_config_flag_when_none(tmp_path):
    claude = _fake_claude(tmp_path, "cat\n")
    result = run_store_backed_task("task-9", config_path=None, claude_bin=claude)
    assert "--config" not in result.output
    assert "tasks show task-9" in result.output


def test_store_backed_strips_billing_and_nested_session_env(tmp_path, monkeypatch):
    for key in ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(key, "leak")
    claude = _fake_claude(
        tmp_path,
        'echo "C=[${CLAUDECODE}] K=[${ANTHROPIC_API_KEY}] T=[${ANTHROPIC_AUTH_TOKEN}]"\n',
    )
    result = run_store_backed_task("t", claude_bin=claude)
    assert "C=[] K=[] T=[]" in result.output


def test_truncation_at_max_tokens_fails_loudly(tmp_path):
    claude = _fake_claude(tmp_path, 'echo \'{"stop_reason": "max_tokens"}\'\n')
    result = run_claude_task("x", claude_bin=claude)
    assert result.success is False
    assert result.truncated is True
    assert "truncated" in result.error


# -------------------------------------------------- orchestrator routing

def _orch(tmp_path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


def test_store_backed_completes_when_agent_writes_result_to_store(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    task = orch.beads.create(
        title="distill", description="x", assigned_agent="claude",
        metadata={"store_backed": True},
    )

    def fake(task_id, config_path, timeout, max_turns, model=None):
        # Simulate the subagent writing its result back via the store.
        orch.beads.complete(task_id, result=TaskResult(status="complete", output="done").to_json())
        return ExecResult(True, "ignored stdout", None, 0, 0.1)

    monkeypatch.setattr(orchestrator_mod, "run_store_backed_task", fake)
    assert orch._execute_claude_task(task) is True
    assert orch.beads.get(task.id).status == TaskStatus.DONE


def test_store_backed_fails_when_agent_never_writes_back(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    task = orch.beads.create(
        title="distill", description="x", assigned_agent="claude",
        metadata={"store_backed": True},
    )

    # Subprocess "succeeds" but the agent forgot to complete the task in the store.
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task",
        lambda task_id, config_path, timeout, max_turns, model=None: ExecResult(True, "", None, 0, 0.1),
    )
    assert orch._execute_claude_task(task) is False
    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.FAILED
    assert "result missing from store" in (refreshed.result or "")


def test_non_store_backed_still_uses_stdout_path(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    task = orch.beads.create(title="plain", description="x", assigned_agent="claude")

    monkeypatch.setattr(
        orchestrator_mod, "run_claude_task",
        lambda prompt, timeout, max_turns, model=None: ExecResult(True, '{"ok": true}', None, 0, 0.1),
    )
    assert orch._execute_claude_task(task) is True
    assert '{"ok": true}' in orch.beads.get(task.id).result
