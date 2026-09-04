"""Plan-to-beads intake: `tasks create --parent/--blocked-by/--verify` and the
gate rendering in `tasks show`.

A planned goal decomposes into sub-beads that each carry their own sequencing
and their own definition of done — these are the flags that let one command
file one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.cli import main


def _node(tmp_path: Path, monkeypatch) -> Beads:
    root = tmp_path / "node"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    monkeypatch.chdir(root)
    return Beads(root / "tasks.jsonl")


def test_create_roundtrips_parent_blocked_by_and_verify(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    goal = beads.create("goal: ship the thing", "d")
    sibling = beads.create("do the first part", "d")

    result = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "do the second part",
            "--parent",
            goal.id,
            "--blocked-by",
            sibling.id,
            "--verify",
            json.dumps({"class": "deterministic", "check": "uv run pytest -q"}),
        ],
    )
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "do the second part"][0]
    assert created.parent_id == goal.id
    assert created.blocked_by == [sibling.id]
    stored = created.metadata["verify"]
    assert stored["kind"] == "deterministic"
    assert stored["check"] == "uv run pytest -q" and stored["checks"] is None
    assert "verify: deterministic" in result.output


def test_create_accepts_repeated_blocked_by(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    b = beads.create("b", "d")

    result = runner.invoke(
        main, ["tasks", "create", "c", "--blocked-by", a.id, "--blocked-by", b.id]
    )
    assert result.exit_code == 0, result.output
    created = [t for t in beads.list() if t.title == "c"][0]
    assert created.blocked_by == [a.id, b.id]
    assert not beads.ready() or created.id not in {t.id for t in beads.ready()}


def test_create_normalizes_the_verify_payload_it_stores(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    result = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "gated",
            "--verify",
            json.dumps(
                {"class": "human", "check": "confirm sent", "timeout_s": 30, "cwd": "/tmp"}
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    spec = beads.list()[0].metadata["verify"]
    assert spec["kind"] == "human"
    assert spec["check"] == "confirm sent"
    assert spec["cwd"] == "/tmp" and spec["timeout_s"] == 30
    assert spec["schema_version"] == 1


def test_create_refuses_malformed_verify_json_and_writes_nothing(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)

    result = runner.invoke(main, ["tasks", "create", "x", "--verify", "{not json"])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output
    assert beads.list() == []


def test_create_refuses_an_invalid_verify_contract_and_writes_nothing(
    tmp_path, monkeypatch
):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)

    result = runner.invoke(
        main,
        ["tasks", "create", "x", "--verify", json.dumps({"class": "vibes", "check": "ok"})],
    )
    assert result.exit_code == 1
    assert "Invalid --verify payload" in result.output
    assert beads.list() == []


def test_create_refuses_a_parent_that_would_breach_the_depth_limit(
    tmp_path, monkeypatch
):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    root = beads.create("root", "d")
    d1 = beads.create("d1", "d", parent_id=root.id)
    d2 = beads.create("d2", "d", parent_id=d1.id)
    d3 = beads.create("d3", "d", parent_id=d2.id)

    result = runner.invoke(main, ["tasks", "create", "too deep", "--parent", d3.id])
    assert result.exit_code == 1
    assert "refusing to deepen the tree" in result.output
    assert "SIBLING" in result.output
    assert not [t for t in beads.list() if t.title == "too deep"]


def test_show_renders_the_gate_for_a_pending_bead(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    task = beads.create(
        "x",
        "d",
        metadata={
            "verify": {"class": "deterministic", "check": "make test", "timeout_s": 60}
        },
    )

    result = runner.invoke(main, ["tasks", "show", task.id])
    assert result.exit_code == 0, result.output
    assert "Verify gate" in result.output
    assert "check:  make test" in result.output
    assert "cannot self-grade" in result.output


def test_show_renders_awaiting_and_failed_states(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    gated = beads.create(
        "x", "d", metadata={"verify": {"class": "human", "check": "confirm"}}
    )
    beads.complete(gated.id)

    result = runner.invoke(main, ["tasks", "show", gated.id])
    assert "AWAITING APPROVAL" in result.output
    assert f"approve-verify {gated.id}" in result.output

    beads.reject_verify(gated.id, approver="mabidoli", reason="wrong recipient")
    result = runner.invoke(main, ["tasks", "show", gated.id])
    assert "VERIFY FAILED" in result.output
    assert "wrong recipient" in result.output


def test_show_stays_parseable_json_by_default_and_with_the_flag(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    plain = beads.create("ungated", "d")
    gated = beads.create(
        "gated", "d", metadata={"verify": {"class": "human", "check": "confirm"}}
    )

    # An ungated bead's output is byte-for-byte what it always was: pure JSON.
    result = runner.invoke(main, ["tasks", "show", plain.id])
    assert json.loads(result.output)["id"] == plain.id

    # A gated bead adds the human block — --json is the escape hatch for pipes.
    result = runner.invoke(main, ["tasks", "show", gated.id, "--json"])
    assert json.loads(result.output)["id"] == gated.id
