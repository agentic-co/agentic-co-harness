"""`approve task` / `approve all` finalizing a HELD ASOP match, end to end
through the CLI — the moment `intake.require_approval` was designed around:
a human's approval is what actually files the run.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml
from click.testing import CliRunner

from agentco_harness.asop_router import route
from agentco_harness.asop_store import AsopStore
from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.cli import main
from agentco_harness.config import AsopRouterConfig


def widget_asop(role="worker"):
    return {
        "title": "Handle a widget request", "task_type": "widget", "purpose": "handle it",
        "inputs": [{"name": "title"}], "roles": {role: {"kind": "agent"}},
        "steps": [{"name": "do-it", "role": role, "purpose": "do the work",
                   "gate": {"kind": "deterministic", "check": "true"}}],
    }


def _stub(matched_asop_id="", confidence=0.0, reason=""):
    def predict(**kwargs):
        return SimpleNamespace(matched_asop_id=matched_asop_id, confidence=confidence, reason=reason)
    return predict


def _node(tmp_path: Path, monkeypatch, *, bindings: dict) -> tuple[Beads, AsopStore]:
    root = tmp_path / "node"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump({
        "tasks_path": "tasks.jsonl",
        "asop_router": {"enabled": True, "bindings": bindings},
    }))
    monkeypatch.chdir(root)
    beads = Beads(root / "tasks.jsonl")
    store = AsopStore(root / "asops.jsonl")
    rec = store.create(widget_asop(), author="m", author_kind="human", asop_id="widget-flow")
    store.activate(rec.asop_id, rec.version, by_kind="human")
    return beads, store


def _held_bead(beads: Beads, store: AsopStore, cfg: AsopRouterConfig) -> str:
    task = beads.create(title="widget broke", description="d", status=TaskStatus.PENDING_APPROVAL,
                         metadata={"category": "widget", "requires_approval": True})
    route(task, store=store, beads=beads, config=cfg, require_approval=True,
          predict=_stub("widget-flow", 0.9, "fits"))
    return task.id


def test_approve_task_files_the_held_run(tmp_path, monkeypatch):
    beads, store = _node(tmp_path, monkeypatch, bindings={"worker": "claude"})
    task_id = _held_bead(beads, store, AsopRouterConfig(bindings={"worker": "claude"}))

    result = CliRunner().invoke(main, ["approve", "task", task_id])

    assert result.exit_code == 0, result.output
    assert "routed to run" in result.output
    refreshed = beads.get(task_id)
    assert refreshed.status == TaskStatus.SKIPPED
    run_id = refreshed.metadata["asop_route"]["run_id"]
    assert [t for t in beads.list() if t.parent_id == run_id]


def test_approve_task_without_a_binding_still_promotes_and_says_why(tmp_path, monkeypatch):
    beads, store = _node(tmp_path, monkeypatch, bindings={})  # no binding for 'worker'
    task_id = _held_bead(beads, store, AsopRouterConfig(bindings={}))

    result = CliRunner().invoke(main, ["approve", "task", task_id])

    assert result.exit_code == 0, result.output
    assert "could not be filed" in result.output
    assert beads.get(task_id).status == TaskStatus.PENDING  # ordinary promotion, unrouted


def test_approve_task_on_a_plain_bead_is_unchanged(tmp_path, monkeypatch):
    beads, store = _node(tmp_path, monkeypatch, bindings={"worker": "claude"})
    task = beads.create(title="one-off", description="d", status=TaskStatus.PENDING_APPROVAL)

    result = CliRunner().invoke(main, ["approve", "task", task.id])

    assert result.exit_code == 0, result.output
    assert "routed to run" not in result.output
    assert beads.get(task.id).status == TaskStatus.PENDING


def test_approve_task_reports_not_found(tmp_path, monkeypatch):
    _node(tmp_path, monkeypatch, bindings={"worker": "claude"})

    result = CliRunner().invoke(main, ["approve", "task", "ac-doesnotexist"])

    assert result.exit_code == 1
    assert "Task not found" in result.output


def test_approve_all_files_a_held_run_alongside_a_plain_one(tmp_path, monkeypatch):
    beads, store = _node(tmp_path, monkeypatch, bindings={"worker": "claude"})
    held_id = _held_bead(beads, store, AsopRouterConfig(bindings={"worker": "claude"}))
    plain = beads.create(title="one-off", description="d", status=TaskStatus.PENDING_APPROVAL)

    result = CliRunner().invoke(main, ["approve", "all"])

    assert result.exit_code == 0, result.output
    assert "Approved 2 task(s)" in result.output
    assert beads.get(held_id).status == TaskStatus.SKIPPED
    assert beads.get(plain.id).status == TaskStatus.PENDING
