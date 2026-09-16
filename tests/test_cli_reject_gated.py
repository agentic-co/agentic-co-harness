"""`approve reject` / `reject-all` / `decline --terminal` against a gated bead.

N9 closed these doors at the choke point (see `test_terminal_gate_policy.py`),
which is the part that matters for correctness. This file covers the part that
matters for USE: a refusal a person cannot act on is a refusal they route around.

The batch case carries a real contract and nothing tested it before: a gated
bead inside `reject-all` must be reported and skipped — never silently dropped,
and never allowed to abort the rest, because a bulk reject that dies halfway
leaves the operator not knowing which half happened.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.cli import main

GATE = json.dumps({"class": "judged", "check": "is this actually done?"})


def _node(tmp_path: Path, monkeypatch) -> Beads:
    root = tmp_path / "node"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    monkeypatch.chdir(root)
    return Beads(root / "tasks.jsonl")


def _awaiting_approval(beads: Beads, title: str, *, gated: bool) -> str:
    metadata = {"verify": json.loads(GATE)} if gated else None
    task = beads.create(title, "d", metadata=metadata)
    beads.update(task.id, status=TaskStatus.PENDING_APPROVAL)
    return task.id


def test_reject_refuses_a_gated_bead_and_names_the_way_out(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    task_id = _awaiting_approval(beads, "migrate the store", gated=True)

    result = runner.invoke(main, ["approve", "reject", task_id])

    assert result.exit_code == 1, result.output
    assert "Cannot reject" in result.output
    assert f"agentco tasks cancel {task_id}" in result.output
    assert beads.get(task_id).status is TaskStatus.PENDING_APPROVAL


def test_reject_still_works_on_an_ungated_bead(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    task_id = _awaiting_approval(beads, "tidy the scratch dir", gated=False)

    result = runner.invoke(main, ["approve", "reject", task_id])

    assert result.exit_code == 0, result.output
    assert beads.get(task_id).status is TaskStatus.SKIPPED


def test_reject_all_rejects_the_ungated_and_reports_the_gated(tmp_path, monkeypatch):
    """The batch contract: partial success is reported, not hidden.

    Exit code 1 with work done is deliberate. The alternative readings are both
    worse — exit 0 says "all rejected" when two were not, and aborting on the
    first refusal leaves the operator guessing where it stopped.
    """
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    plain_a = _awaiting_approval(beads, "tidy the scratch dir", gated=False)
    gated = _awaiting_approval(beads, "migrate the store", gated=True)
    plain_b = _awaiting_approval(beads, "archive old logs", gated=False)

    result = runner.invoke(main, ["approve", "reject-all"])

    assert result.exit_code == 1, result.output
    assert "Rejected 2 task(s)." in result.output
    assert "1 NOT rejected" in result.output
    assert f"agentco tasks cancel {gated}" in result.output

    assert beads.get(plain_a).status is TaskStatus.SKIPPED
    assert beads.get(plain_b).status is TaskStatus.SKIPPED
    assert beads.get(gated).status is TaskStatus.PENDING_APPROVAL


def test_reject_all_exits_zero_when_nothing_is_gated(tmp_path, monkeypatch):
    """The ordinary case must not inherit the partial-failure exit code."""
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    _awaiting_approval(beads, "tidy the scratch dir", gated=False)
    _awaiting_approval(beads, "archive old logs", gated=False)

    result = runner.invoke(main, ["approve", "reject-all"])

    assert result.exit_code == 0, result.output
    assert "Rejected 2 task(s)." in result.output
    assert "NOT rejected" not in result.output


def test_terminal_decline_refuses_and_offers_both_alternatives(tmp_path, monkeypatch):
    """N5's door, at the surface a person actually types.

    Two routes are offered because there are genuinely two intents: abandon it
    (cancel), or hand it back (plain decline). Offering only `cancel` would push
    someone toward a terminal status when they meant to put the bead down.
    """
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    task = beads.create("migrate the store", "d", metadata={"verify": json.loads(GATE)})
    beads.update(task.id, assigned_to="human:alex")

    result = runner.invoke(
        main, ["tasks", "decline", task.id, "--reason", "kill-dated", "--terminal"]
    )

    assert result.exit_code == 1, result.output
    assert "Cannot decline (terminal)" in result.output
    assert f"agentco tasks cancel {task.id}" in result.output
    assert f"agentco tasks decline {task.id}" in result.output
    assert beads.get(task.id).status is TaskStatus.PENDING
