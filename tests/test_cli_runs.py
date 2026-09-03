"""CLI tests for runs, attention, and tasks retry."""

from __future__ import annotations

import json

from click.testing import CliRunner

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.cli import main


def _init(runner):
    assert runner.invoke(main, ["init"]).exit_code == 0


def test_runs_renders_newest_first(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)

    records = [
        {
            "at": f"2026-06-11T0{i}:00:00+00:00",
            "instance": "x",
            "spawned": 0,
            "executed": i,
            "errors": 0,
            "open_after": 0,
            "tasks": [{"id": f"ac-{i}", "title": f"task {i}", "agent": "claude", "outcome": "done"}],
        }
        for i in range(3)
    ]
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    result = runner.invoke(main, ["runs", "-n", "2"])
    assert result.exit_code == 0, result.output
    # Newest (02:00) first, only 2 shown.
    assert result.output.index("T02:") < result.output.index("T01:")
    assert "T00:" not in result.output  # oldest run cut by -n 2
    assert "task 2" in result.output


def test_runs_without_log(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)
    result = runner.invoke(main, ["runs"])
    assert result.exit_code == 0
    assert "No runs recorded" in result.output


def test_attention_lists_failed_and_blocked(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)

    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="broken thing", description="x", assigned_agent="dev")
    beads.fail(failed.id, result="AuthenticationError: missing key")
    blocker = beads.create(title="prereq", description="x")
    beads.create(title="waiting thing", description="x", blocked_by=[blocker.id])

    result = runner.invoke(main, ["attention"])
    assert result.exit_code == 0, result.output
    assert "broken thing" in result.output
    assert "missing key" in result.output
    assert "waiting thing" in result.output
    assert "tasks retry" in result.output


def test_attention_clean_queue(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)
    result = runner.invoke(main, ["attention"])
    assert "Nothing needs attention" in result.output


def test_tasks_retry_resets_failed_to_pending(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)

    beads = Beads(tmp_path / "tasks.jsonl")
    t1 = beads.create(title="a", description="x")
    t2 = beads.create(title="b", description="x")
    beads.fail(t1.id, result="err1")
    beads.fail(t2.id, result="err2")
    done = beads.create(title="c", description="x")
    beads.complete(done.id)

    result = runner.invoke(main, ["tasks", "retry", t1.id])
    assert result.exit_code == 0, result.output
    assert beads.get(t1.id).status == TaskStatus.PENDING
    assert beads.get(t1.id).result is None
    assert beads.get(t2.id).status == TaskStatus.FAILED

    result = runner.invoke(main, ["tasks", "retry", "--all-failed"])
    assert result.exit_code == 0, result.output
    assert beads.get(t2.id).status == TaskStatus.PENDING
    assert beads.get(done.id).status == TaskStatus.DONE  # untouched


def test_tasks_complete_rejects_garbage_result(tmp_path, monkeypatch):
    """`tasks complete --result <garbage>` must fail loudly at the write
    boundary and leave the task NOT done — garbage would parse to None on the
    read side and silently bypass the feed watermark guard."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)

    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create(title="ingest", description="x")

    result = runner.invoke(main, ["tasks", "complete", t.id, "--result", "not json at all"])
    assert result.exit_code != 0
    assert "Invalid --result" in result.output
    assert beads.get(t.id).status != TaskStatus.DONE  # stays not-DONE

    # A structurally-valid JSON with a bogus status value is also rejected.
    bad_status = '{"status": "bogus", "output": "x"}'
    result = runner.invoke(main, ["tasks", "complete", t.id, "--result", bad_status])
    assert result.exit_code != 0
    assert beads.get(t.id).status != TaskStatus.DONE


def test_tasks_complete_accepts_valid_result(tmp_path, monkeypatch):
    """A valid TaskResult JSON completes the task at the write boundary."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)

    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create(title="ingest", description="x")

    good = '{"status": "complete", "output": "2 notes"}'
    result = runner.invoke(main, ["tasks", "complete", t.id, "--result", good])
    assert result.exit_code == 0, result.output
    assert beads.get(t.id).status == TaskStatus.DONE


def test_tasks_complete_without_result_still_allowed(tmp_path, monkeypatch):
    """Backward compat: completing an ordinary task WITHOUT --result stays valid."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner)

    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create(title="ordinary", description="x")

    result = runner.invoke(main, ["tasks", "complete", t.id])
    assert result.exit_code == 0, result.output
    assert beads.get(t.id).status == TaskStatus.DONE
