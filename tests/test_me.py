"""Tests for agentco_harness.me — the portfolio-wide human work queue."""

import json
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.cli import main
from agentco_harness.me import collect, ranked


def _write_config(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n")
    return cfg


def _register_child(parent: Path, name: str, child: Path, priority: int = 2) -> None:
    reg = parent / "children" / "registry.jsonl"
    reg.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "name": name,
        "path": str(child),
        "expected_interval": "1h",
        "notify": False,
        "priority": priority,
    }
    with open(reg, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _fresh_heartbeat(root: Path) -> None:
    (root / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": datetime.now(timezone.utc).isoformat()})
    )


def _portfolio(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Parent + two children: sommeli (priority 1, healthy), m3bl (priority 3, healthy)."""
    parent = tmp_path / "parent"
    sommeli = tmp_path / "sommeli"
    m3bl = tmp_path / "m3bl"
    for root in (parent, sommeli, m3bl):
        _write_config(root)
        _fresh_heartbeat(root)
    _register_child(parent, "sommeli", sommeli, priority=1)
    _register_child(parent, "m3bl", m3bl, priority=3)
    return parent, sommeli, m3bl


def test_me_collects_across_children(tmp_path):
    parent, sommeli, m3bl = _portfolio(tmp_path)

    pb = Beads(str(parent / "tasks.jsonl"))
    t = pb.create("parent approval", "x")
    pb.update(t.id, status=TaskStatus.PENDING_APPROVAL)

    sb = Beads(str(sommeli / "tasks.jsonl"))
    st = sb.create("sommeli failure", "x")
    sb.update(st.id, status=TaskStatus.FAILED)

    mb = Beads(str(m3bl / "tasks.jsonl"))
    mt = mb.create("m3bl failure", "x")
    mb.update(mt.id, status=TaskStatus.FAILED)

    items = collect(str(parent / "config.yaml"))
    companies = {i.company for i in items}
    assert {"sommeli", "m3bl"} <= companies
    kinds = {(i.company, i.kind) for i in items}
    assert ("sommeli", "failed") in kinds
    assert ("m3bl", "failed") in kinds
    assert any(i.kind == "approval" for i in items)


def test_company_priority_orders_list(tmp_path):
    parent, sommeli, m3bl = _portfolio(tmp_path)

    for root in (sommeli, m3bl):
        b = Beads(str(root / "tasks.jsonl"))
        t = b.create("failure", "x")
        b.update(t.id, status=TaskStatus.FAILED)

    items = ranked(str(parent / "config.yaml"))
    failed = [i for i in items if i.kind == "failed"]
    assert [i.company for i in failed] == ["sommeli", "m3bl"]
    assert failed[0].score > failed[1].score


def test_leverage_boosts_score(tmp_path):
    parent, sommeli, _ = _portfolio(tmp_path)
    b = Beads(str(sommeli / "tasks.jsonl"))

    keystone = b.create("keystone approval", "x")
    b.update(keystone.id, status=TaskStatus.PENDING_APPROVAL)
    b.create("dependent 1", "x", blocked_by=[keystone.id])
    b.create("dependent 2", "x", blocked_by=[keystone.id])

    loner = b.create("loner approval", "x")
    b.update(loner.id, status=TaskStatus.PENDING_APPROVAL)

    items = ranked(str(parent / "config.yaml"))
    approvals = {i.task_id: i for i in items if i.kind == "approval"}
    assert approvals[keystone.id].leverage == 2
    assert approvals[loner.id].leverage == 0
    assert approvals[keystone.id].score > approvals[loner.id].score


def test_blocked_on_open_dependency_reported(tmp_path):
    parent, sommeli, _ = _portfolio(tmp_path)
    b = Beads(str(sommeli / "tasks.jsonl"))
    gate = b.create("gate", "x")
    b.create("waiting task", "x", blocked_by=[gate.id])

    items = collect(str(parent / "config.yaml"))
    blocked = [i for i in items if i.kind == "blocked"]
    assert len(blocked) == 1
    assert gate.id in blocked[0].detail


def test_stale_child_reported_with_queue_leverage(tmp_path):
    parent, sommeli, _ = _portfolio(tmp_path)
    stale = tmp_path / "stale-co"
    _write_config(stale)  # no heartbeat.json → never completed a cycle
    b = Beads(str(stale / "tasks.jsonl"))
    b.create("stuck work 1", "x")
    b.create("stuck work 2", "x")
    _register_child(parent, "stale-co", stale, priority=0)

    items = ranked(str(parent / "config.yaml"))
    stale_items = [i for i in items if i.kind == "stale_child"]
    assert len(stale_items) == 1
    assert stale_items[0].company == "stale-co"
    assert stale_items[0].leverage == 2
    # priority-0 stale child with stuck work outranks everything else
    assert items[0].kind == "stale_child"


def test_registry_cycle_is_safe(tmp_path):
    parent, sommeli, _ = _portfolio(tmp_path)
    _register_child(sommeli, "parent-loop", parent, priority=2)

    items = collect(str(parent / "config.yaml"))  # must terminate
    assert isinstance(items, list)


def _register_pathless_child(parent: Path, name: str, ctype: str) -> None:
    """Register an ado-backed / manual child: no `path`, so no local config.yaml."""
    reg = parent / "children" / "registry.jsonl"
    reg.parent.mkdir(parents=True, exist_ok=True)
    entry = {"name": name, "type": ctype, "notify": False}
    with open(reg, "a") as f:
        f.write(json.dumps(entry) + "\n")


def test_pathless_child_does_not_crash(tmp_path):
    """A pathless child (ado-backed / manual) must not crash collect() and,
    being 'unverified' rather than failing, must not appear as a stale_child.
    Regression: collect() did Path(child.path)/config.yaml unconditionally,
    raising TypeError on child.path=None and breaking the whole human queue."""
    parent, sommeli, m3bl = _portfolio(tmp_path)
    _register_pathless_child(parent, "frontsteps", "ado-backed")

    items = collect(str(parent / "config.yaml"))  # must not raise
    assert isinstance(items, list)
    # ado-backed child is 'unverified' (nothing promised) — not human-owned noise
    assert not any(i.company == "frontsteps" for i in items)


def test_cli_me_json(tmp_path):
    parent, sommeli, _ = _portfolio(tmp_path)
    b = Beads(str(sommeli / "tasks.jsonl"))
    t = b.create("needs approval", "x")
    b.update(t.id, status=TaskStatus.PENDING_APPROVAL)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(parent / "config.yaml"), "me", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert any(i["kind"] == "approval" and i["task_id"] == t.id for i in payload)
    resolve = next(i["resolve"] for i in payload if i["task_id"] == t.id)
    assert "approve task" in resolve and t.id in resolve


def test_cli_me_empty(tmp_path):
    parent = tmp_path / "solo"
    _write_config(parent)
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(parent / "config.yaml"), "me"])
    assert result.exit_code == 0
    assert "Nothing depends on you" in result.output


# ------------------------------------------- a bead's own priority moves rank


def _mk(company_priority=2, task_priority=2, kind="human_assigned", age=0.0, leverage=0):
    from agentco_harness.me import MeItem, _score

    return _score(MeItem(
        company="c", company_priority=company_priority, kind=kind, task_id="t",
        title="t", detail="d", age_seconds=age, leverage=leverage,
        config_path="", resolve="", priority=task_priority,
    ))


def test_critical_bead_outranks_a_medium_one_in_the_same_company():
    # The whole point: on an undated queue, marking something CRITICAL must move it.
    assert _mk(task_priority=0) > _mk(task_priority=2)


def test_low_priority_bead_sinks_below_medium():
    assert _mk(task_priority=3) < _mk(task_priority=2)


def test_medium_priority_is_exactly_neutral():
    # MEDIUM is the default, so an untouched queue must rank bit-for-bit as before.
    from agentco_harness.me import _urgency

    assert _urgency(2) == 1.0


def test_company_weight_still_dominates_bead_priority():
    # A CRITICAL bead in a default-weight company must NOT outrank ordinary work in
    # a top-priority company — otherwise any bead could hijack the whole portfolio.
    critical_in_normal = _mk(company_priority=2, task_priority=0)
    medium_in_top = _mk(company_priority=0, task_priority=2)
    assert medium_in_top > critical_in_normal


def test_unknown_priority_is_neutral_not_a_crash():
    from agentco_harness.me import _urgency

    assert _urgency(99) == 1.0


def test_priority_is_read_from_the_bead(tmp_path):
    # End-to-end: the field must actually be populated from the task, not defaulted.
    from agentco_harness.beads import Beads, TaskPriority
    from agentco_harness.me import collect

    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create(title="urgent", description="d", priority=TaskPriority.CRITICAL)
    beads.update(t.id, assigned_to="human:mabidoli")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("instance: t\ntasks_path: tasks.jsonl\n")
    items = [i for i in collect(str(cfg)) if i.task_id == t.id]
    assert items and items[0].priority == 0
