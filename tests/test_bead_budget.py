"""Execution budget: how a bead gets the wall-clock it needs (ac-f698a0c3).

The failure this file pins down had nothing to do with the idle watchdog its
bead was titled after. Two fix beads filed by the @aidotengineer RCA —
ac-83b2f89b ("Disarm the idle watchdog on store-backed beads") and
ac-7ea4b8a1 — both died at *exactly* 600s, `claude subagent timed out after
600s (budget exhausted)`. They ran in PROMPT mode, where the watchdog is never
armed at all; 600s is `executor.DEFAULT_TIMEOUT`, the fallback for a bead with
no `metadata.budget`.

They had no budget because they could not have one. The 1800s `RCA_BUDGET` fix
covered RCA *phase* beads, created in Python; the fix beads an analysis files
are created through `agentco tasks create`, which had no `--timeout` flag at
all. An agent following the RCA prompt to the letter filed a bead that was
guaranteed to die mid-edit.

Two halves, and the second is the one that holds:
  * the CLI can now express a budget (a)
  * a child bead with none INHERITS its parent's, so the budget survives an
    agent that forgets the flags (b)
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from agentco_harness.beads import Beads
from agentco_harness.cli import main
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.executor import DEFAULT_MAX_TURNS, DEFAULT_TIMEOUT
from agentco_harness.orchestrator import TASK_CLASS_BUDGETS, Orchestrator
from agentco_harness.rca import RCA_BUDGET, create_rca_task


def _node(tmp_path: Path, monkeypatch) -> Beads:
    root = tmp_path / "node"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    monkeypatch.chdir(root)
    return Beads(root / "tasks.jsonl")


def _orch(tmp_path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


# --------------------------------------------------------------------- (a)
# The CLI can express a budget at all.


def test_create_writes_the_budget_flags_into_metadata(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        main,
        ["tasks", "create", "edit the executor", "--timeout", "1800", "--max-turns", "120"],
    )
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "edit the executor"][0]
    assert created.metadata["budget"] == {"timeout": 1800, "max_turns": 120}


def test_each_budget_flag_stands_alone(tmp_path, monkeypatch):
    # Half a budget is legitimate: raise the wall clock, keep the default turns.
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["tasks", "create", "slow but simple", "--timeout", "1800"])
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "slow but simple"][0]
    assert created.metadata["budget"] == {"timeout": 1800}


def test_no_flags_leaves_the_bead_budgetless(tmp_path, monkeypatch):
    # The default is unchanged — this fix adds a surface, it does not silently
    # re-budget every bead in the system.
    beads = _node(tmp_path, monkeypatch)
    assert CliRunner().invoke(main, ["tasks", "create", "plain"]).exit_code == 0
    created = [t for t in beads.list() if t.title == "plain"][0]
    assert "budget" not in (created.metadata or {})


def test_a_nonpositive_budget_is_refused_rather_than_written(tmp_path, monkeypatch):
    # `--timeout 0` would be a bead that can never run: TimeoutExpired on the
    # first tick. Refuse at intake, where the operator can still see why.
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["tasks", "create", "doomed", "--timeout", "0"])
    assert result.exit_code == 1
    assert "must be a positive integer" in result.output
    assert [t for t in beads.list() if t.title == "doomed"] == []


# --------------------------------------------------------------------- (b)
# Inheritance — the half that holds when the agent forgets the flags.


def test_a_child_with_no_budget_inherits_its_parents(tmp_path):
    """The regression for ac-83b2f89b / ac-7ea4b8a1.

    A fix bead parented under an 1800s RCA analysis is not 600s of work
    because the command that filed it left the field blank.
    """
    orch = _orch(tmp_path)
    parent = orch.beads.create(
        "[RCA] something broke", "d", metadata={"budget": {"timeout": 1800, "max_turns": 120}}
    )
    fix = orch.beads.create("the fix", "d", parent_id=parent.id)

    assert orch._resolve_budget(fix) == (1800, 120)


def test_an_explicit_budget_beats_the_parents(tmp_path):
    orch = _orch(tmp_path)
    parent = orch.beads.create("goal", "d", metadata={"budget": {"timeout": 1800, "max_turns": 120}})
    child = orch.beads.create(
        "quick child", "d", parent_id=parent.id, metadata={"budget": {"timeout": 300}}
    )

    assert orch._resolve_budget(child) == (300, DEFAULT_MAX_TURNS)


def test_an_orphan_with_no_budget_still_gets_the_default(tmp_path):
    orch = _orch(tmp_path)
    task = orch.beads.create("top-level", "d")
    assert orch._resolve_budget(task) == (DEFAULT_TIMEOUT, DEFAULT_MAX_TURNS)


def test_a_budgetless_parent_does_not_invent_one(tmp_path):
    orch = _orch(tmp_path)
    parent = orch.beads.create("goal", "d")
    child = orch.beads.create("child", "d", parent_id=parent.id)
    assert orch._resolve_budget(child) == (DEFAULT_TIMEOUT, DEFAULT_MAX_TURNS)


def test_inheritance_stops_at_one_hop(tmp_path):
    """The budget a bead runs on must be readable from itself or its parent —
    never reconstructed by walking an arbitrary ancestry."""
    orch = _orch(tmp_path)
    grandparent = orch.beads.create("goal", "d", metadata={"budget": {"timeout": 1800}})
    parent = orch.beads.create("mid", "d", parent_id=grandparent.id)
    child = orch.beads.create("leaf", "d", parent_id=parent.id)

    assert orch._resolve_budget(child) == (DEFAULT_TIMEOUT, DEFAULT_MAX_TURNS)


# --- the two halves meet: an RCA's fix bead is budgeted either way ----------


def test_the_rca_prompt_tells_the_agent_to_budget_the_fix_bead(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Ingest youtube: @x", description="d")
    root = create_rca_task(beads, failed, error="boom")

    assert f"--timeout {RCA_BUDGET['timeout']}" in root.description
    assert f"--max-turns {RCA_BUDGET['max_turns']}" in root.description


def test_a_fix_bead_filed_without_the_flags_still_runs_on_the_rca_budget(tmp_path):
    """What actually happened, replayed: the agent files the fix with nothing
    but --parent. It must not land on 600s."""
    orch = _orch(tmp_path)
    failed = orch.beads.create(title="Ingest youtube: @x", description="d")
    root = create_rca_task(orch.beads, failed, error="boom")
    fix = orch.beads.create("Disarm the idle watchdog", "d", parent_id=root.id)

    assert orch._resolve_budget(fix) == (RCA_BUDGET["timeout"], RCA_BUDGET["max_turns"])


# --- the third source: a class floor for beads with no parent (ac-e0a43696) --
#
# Inheritance holds only for a bead that HAS a parent. The beads the DA files
# against itself under the "[Leeloo owes]" rule are roots — a title, a
# description and `--task-class agent`. Two of them died at exactly 600s on
# 2026-08-18 (ac-eb1d962a "Transcript-ingest liveness guard", ac-c32d5540
# "permanently-stuck transkriptor enrichment retry"), same signature as
# ac-83b2f89b/ac-7ea4b8a1 one fix earlier.


def test_an_agent_class_orphan_gets_the_class_floor_not_600s(tmp_path):
    """The regression for ac-eb1d962a / ac-c32d5540."""
    orch = _orch(tmp_path)
    owed = orch.beads.create(
        "[Leeloo owes] Transcript-ingest liveness guard",
        "d",
        metadata={"task_class": "agent"},
    )

    assert orch._resolve_budget(owed) == (
        TASK_CLASS_BUDGETS["agent"]["timeout"],
        TASK_CLASS_BUDGETS["agent"]["max_turns"],
    )
    assert orch._resolve_budget(owed)[0] != DEFAULT_TIMEOUT


def test_an_explicit_budget_beats_the_class_floor(tmp_path):
    """A floor is what happens when nobody chose — never an override of
    somebody who did."""
    orch = _orch(tmp_path)
    owed = orch.beads.create(
        "[Leeloo owes] a genuinely quick one",
        "d",
        metadata={"task_class": "agent", "budget": {"timeout": 300, "max_turns": 20}},
    )

    assert orch._resolve_budget(owed) == (300, 20)


def test_a_parents_budget_beats_the_class_floor(tmp_path):
    """Inheritance still runs first: the parent decomposed this work and knows
    how big it is, which the class name does not."""
    orch = _orch(tmp_path)
    parent = orch.beads.create("goal", "d", metadata={"budget": {"timeout": 3600, "max_turns": 200}})
    child = orch.beads.create(
        "owed sub-bead", "d", parent_id=parent.id, metadata={"task_class": "agent"}
    )

    assert orch._resolve_budget(child) == (3600, 200)


def test_other_task_classes_are_untouched(tmp_path):
    """Only `agent` has a floor. `personal` and `company` are human-owned
    annotations that say nothing about how long a subagent needs."""
    orch = _orch(tmp_path)
    for task_class in ("personal", "company"):
        task = orch.beads.create(task_class, "d", metadata={"task_class": task_class})
        assert orch._resolve_budget(task) == (DEFAULT_TIMEOUT, DEFAULT_MAX_TURNS)


def test_the_agent_class_budget_matches_the_rca_budget(tmp_path):
    """TASK_CLASS_BUDGETS duplicates RCA_BUDGET's numbers rather than importing
    them. Pin the duplication so the two cannot drift: an owed bead and an RCA
    fix bead are the same shape of work — read the code, edit it, run the
    suite — and must be sized the same."""
    assert TASK_CLASS_BUDGETS["agent"] == RCA_BUDGET


# --------------------------------------------------------------------- (d)
# The same orphan shape, one class over: rituals (ac-11290723, 2026-08-21).
#
# StandDown is filed by ~/Portfolio/rituals/run.sh as a root bead with
# `--task-class ritual` and no budget flags — orphan, budgetless, exactly what
# the class floor exists for, and simply never added to the table. It ran in
# 205s on 08-19 and 203s on 08-20; on 08-21 the weekly retro appended a new
# MANDATORY section to standdown.md at 17:02 and the 18:00 run hit the 600s
# wall, losing both deliverables (Telegram message, retro file) after ten
# minutes of real work.


def test_a_ritual_class_orphan_gets_the_class_floor_not_600s(tmp_path):
    """The regression for ac-11290723."""
    orch = _orch(tmp_path)
    standdown = orch.beads.create(
        "🌙 StandDown 2026-08-21",
        "Follow the ritual playbook ...",
        metadata={"task_class": "ritual"},
    )

    assert orch._resolve_budget(standdown) == (
        TASK_CLASS_BUDGETS["ritual"]["timeout"],
        TASK_CLASS_BUDGETS["ritual"]["max_turns"],
    )
    assert orch._resolve_budget(standdown)[0] != DEFAULT_TIMEOUT


def test_an_explicit_ritual_budget_beats_the_class_floor(tmp_path):
    """run.sh now passes per-ritual flags — the light hourly pulse must keep
    its tighter wall rather than being widened to the daily-ritual floor."""
    orch = _orch(tmp_path)
    hourly = orch.beads.create(
        "⏱️ Hourly 2026-08-21 15:00",
        "d",
        metadata={"task_class": "ritual", "budget": {"timeout": 600, "max_turns": 40}},
    )

    assert orch._resolve_budget(hourly) == (600, 40)


def test_the_ritual_class_budget_matches_the_agent_class_budget(tmp_path):
    """Pin the duplication. A daily ritual reads several sources, writes a file
    and sends a message; that is not less work than a fix bead, and the two
    numbers drifting apart would be an accident rather than a decision."""
    assert TASK_CLASS_BUDGETS["ritual"] == TASK_CLASS_BUDGETS["agent"]
