"""End-to-end portfolio test against a fake LM and frozen clock.

Scaffold a global instance + 2 companies + 1 project (under company A),
run heartbeats at every tier, then kill one company's cycle and assert the
global tier reports exactly that company as stale.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.beads import TaskStatus
from agentco_harness.children import ChildRef
from agentco_harness.config import AgentConfig, BackoffConfig, Config, LLMConfig
from agentco_harness.orchestrator import Orchestrator
from agentco_harness.recurring import RecurringDef

T0 = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _make_instance(base, name: str) -> Orchestrator:
    inst = base / name
    inst.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.instance = name
    config.tasks_path = str(inst / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    # This e2e pins the *fixed-interval* staleness path (expected_interval ×
    # grace): a dead company detected at 3h against a 2h allowance. Adaptive
    # backoff would let an idle company legitimately extend its own deadline via
    # next_due_at, which is a separate invariant covered in test_children.py
    # (test_backed_off_child_*). Disable backoff here so the two are tested
    # independently and this scenario stays deterministic.
    config.backoff = BackoffConfig(enabled=False)
    config.save(inst / "config.yaml")
    return Orchestrator(config)


def _link(parent: Orchestrator, child_name: str, child_dir) -> None:
    parent.children.path.parent.mkdir(parents=True, exist_ok=True)
    parent.children.add(
        ChildRef(name=child_name, path=str(child_dir), expected_interval="1h")
    )
    parent.recurring.add(
        RecurringDef(
            id=f"verify-{child_name}",
            title=f"Verify child instance: {child_name}",
            schedule={"every": "1h"},
            payload={"type": "verify_child", "child": child_name},
        )
    )


def _silence_triage(monkeypatch, *orchs):
    for orch in orchs:
        monkeypatch.setattr(
            orch,
            "_make_triage_lm",
            lambda: (_ for _ in ()).throw(RuntimeError("no LM in tests")),
        )


def test_portfolio_stale_company_detected_at_global_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    global_orch = _make_instance(tmp_path, "global")
    company_a = _make_instance(tmp_path, "company-a")
    company_b = _make_instance(tmp_path, "company-b")
    project = _make_instance(tmp_path / "company-a", "project-x")

    _link(global_orch, "company-a", tmp_path / "company-a")
    _link(global_orch, "company-b", tmp_path / "company-b")
    _link(company_a, "project-x", tmp_path / "company-a" / "project-x")
    _silence_triage(monkeypatch, global_orch, company_a, company_b, project)

    # --- Hour 1: every tier completes a cycle, bottom-up. All healthy. ---
    project.cycle(now=T0)
    company_a.cycle(now=T0 + timedelta(minutes=1))
    company_b.cycle(now=T0 + timedelta(minutes=1))
    global_summary = global_orch.cycle(now=T0 + timedelta(minutes=2))

    assert global_summary["errors"] == 0
    verify_results = [
        t
        for t in global_orch.beads.list(status=TaskStatus.DONE)
        if t.metadata.get("type") == "verify_child"
    ]
    assert {json.loads(t.result)["child"] for t in verify_results} == {
        "company-a",
        "company-b",
    }

    # --- Hour 4: company-b's daemon died after hour 1; everyone else ran. ---
    later = T0 + timedelta(hours=3, minutes=10)
    project.cycle(now=later)
    company_a.cycle(now=later + timedelta(minutes=1))
    # company_b does NOT cycle — its heartbeat is now ~3h stale (allowed 2h).
    global_orch.cycle(now=later + timedelta(minutes=2))

    failed = [
        t
        for t in global_orch.beads.list(status=TaskStatus.FAILED)
        if t.metadata.get("type") == "verify_child"
    ]
    assert len(failed) == 1
    failure = json.loads(failed[0].result)
    assert failure["child"] == "company-b"
    assert "stale" in failure["detail"]
    assert failure["staleness_seconds"] > 2 * 3600

    # company-a stayed healthy at the global tier this round.
    done_children = {
        json.loads(t.result)["child"]
        for t in global_orch.beads.list(status=TaskStatus.DONE)
        if t.metadata.get("type") == "verify_child"
    }
    assert "company-a" in done_children

    # --- Global status surfaces portfolio health (frozen clock). ---
    status = global_orch.status(now=later + timedelta(minutes=3))
    by_name = {c["child"]: c for c in status["children"]}
    assert by_name["company-b"]["level"] == "fail"
    assert by_name["company-a"]["level"] in ("ok", "warn")

    # The project tier is invisible to global — company-a verifies it.
    assert "project-x" not in by_name
    a_status = company_a.status(now=later + timedelta(minutes=3))
    assert {c["child"] for c in a_status["children"]} == {"project-x"}
