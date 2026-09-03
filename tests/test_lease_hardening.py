"""Cross-machine lease HARDENING (bead ac-48d8aba3).

The lease protocol (ac-9cae7593) is correct in the cases it names. This suite
covers the cases it leaves quiet:

* a lease that expires on a hub nobody pulls from is never reaped, and looks
  exactly like work in progress;
* a lease whose expiry does not parse is skipped by reaping *on purpose*, so no
  automatic path will ever touch it again;
* a bead handed out over and over without finishing looks like throughput;
* a worker returning from a long offline replays work whose external
  side-effects may already have landed;
* a remote node that stopped pulling looks exactly like a remote node with
  nothing to do.

Every one of these is silent by construction, which is why each needs a test
that asserts something is SAID rather than that something works.

Design of record: Plans/TwoMachineLifeos.md; procedure: Plans/BreakGlassFailover.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml
from click.testing import CliRunner

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.children import (
    ChildRef,
    ChildRegistry,
    PullLedger,
    pull_ledger_path,
    verify_remote_child,
)
from agentco_harness.cli import main
from agentco_harness.doctor import (
    LEASE_ATTEMPT_THRESHOLD,
    lease_pathologies,
    run_doctor,
    unresolved_for_worker,
)

WORKER = "frontsteps-worker"
NODE = "frontsteps"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _init(runner, tmp_path) -> Beads:
    assert runner.invoke(main, ["init"]).exit_code == 0
    return Beads(tmp_path / "tasks.jsonl")


def _doctor_cfg(tmp_path) -> str:
    """A config whose only interesting property is the bead store.

    The worker is DECLARED under `agents:` — that is what an externally-executed
    worker looks like to this hub, and without it doctor's agent-dispatchability
    check fires first and masks whatever the lease checks were meant to say.
    """
    (tmp_path / "company").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "sources": {"logs": {"enabled": True}},
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
                "agents": {WORKER: {"role": "external worker"}},
            }
        )
    )
    return str(cfg)


# ------------------------------------------------------- pathology classifier


def test_expired_but_unreaped_lease_is_classified(tmp_path):
    """IN_PROGRESS past its expiry — the bead is stopped dead, not working."""
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    found = lease_pathologies(beads.list(), _now())
    assert [t.id for t in found["expired_unreaped"]] == [task.id]
    assert found["corrupt_expiry"] == []


def test_a_live_lease_is_not_a_pathology(tmp_path):
    """The check must not cry wolf on a worker that is simply working."""
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=3600)

    found = lease_pathologies(beads.list(), _now())
    assert found["expired_unreaped"] == []
    assert found["corrupt_expiry"] == []
    assert found["churning"] == []


def test_corrupt_lease_expiry_is_classified(tmp_path):
    """The case reap_expired_leases skips deliberately must surface here.

    reap walks past an unparseable expiry rather than reclaiming on a guess,
    and ready() excludes the bead too — so if doctor stays quiet, nothing in
    the system will ever mention this bead again.
    """
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=3600)
    beads.update(task.id, lease_expires_at="not-a-timestamp")

    found = lease_pathologies(beads.list(), _now())
    assert [t.id for t in found["corrupt_expiry"]] == [task.id]
    # And it is genuinely unreachable by the automatic path, which is the
    # reason it has to be reported.
    assert beads.reap_expired_leases() == []
    assert beads.ready(assigned_agent=WORKER) == []


def test_lease_held_with_no_expiry_at_all_is_corrupt(tmp_path):
    """A missing expiry lands in the same hole as an unparseable one."""
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=3600)
    beads.update(task.id, lease_expires_at=None)

    found = lease_pathologies(beads.list(), _now())
    assert [t.id for t in found["corrupt_expiry"]] == [task.id]
    assert beads.reap_expired_leases() == []


def test_churning_bead_over_the_attempt_threshold_is_classified(tmp_path):
    """Handed out repeatedly, never finishing — progress-shaped, but isn't."""
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    for _ in range(LEASE_ATTEMPT_THRESHOLD + 1):
        beads.claim(task.id, WORKER, ttl_seconds=0)
        beads.reap_expired_leases()

    found = lease_pathologies(beads.list(), _now())
    assert [t.id for t in found["churning"]] == [task.id]
    assert found["churning"][0].lease_attempt > LEASE_ATTEMPT_THRESHOLD


def test_a_completed_bead_that_churned_is_not_reported(tmp_path):
    """History is not a pathology — it finished, so there is nothing to do.

    Reporting it forever would make doctor permanently non-zero on a lane whose
    laptop closes its lid, which is how an operator learns to stop reading it.
    """
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    for _ in range(LEASE_ATTEMPT_THRESHOLD + 2):
        beads.claim(task.id, WORKER, ttl_seconds=0)
        beads.reap_expired_leases()
    beads.update(task.id, status=TaskStatus.DONE)

    assert lease_pathologies(beads.list(), _now())["churning"] == []


# ------------------------------------------------------------- doctor surface


def test_doctor_fails_on_an_expired_unreaped_lease(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "BROKEN (leases.health)" in out
    assert "EXPIRED lease" in out
    assert task.id in out


def test_doctor_fails_on_a_corrupt_lease_expiry(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=3600)
    beads.update(task.id, lease_expires_at="garbage")

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "UNUSABLE lease_expires_at" in out
    assert task.id in out


def test_doctor_warns_on_a_churning_bead(tmp_path, monkeypatch, capsys):
    """DEGRADED, not BROKEN: the bead is moving, so the hub is not wedged."""
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    for _ in range(LEASE_ATTEMPT_THRESHOLD + 1):
        beads.claim(task.id, WORKER, ttl_seconds=0)
        beads.reap_expired_leases()

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 2
    assert "DEGRADED (leases.health)" in out
    assert "handed out more than" in out
    assert task.id in out


def test_doctor_reports_healthy_leases_positively(tmp_path, monkeypatch, capsys):
    """A silent pass is not a pass — the report must say leases were checked."""
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=3600)

    # Exit 2, not 0: this node has an unmetered agent-executed bead, which is
    # DEGRADED. What matters here is that leases were checked and reported OK.
    assert run_doctor(cfg) in (0, 2)
    out = capsys.readouterr().out
    assert "OK (leases.health): leases healthy: 1 live lease(s)" in out
    assert "BROKEN" not in out


def test_the_recovery_doctor_prescribes_actually_clears_a_corrupt_lease(
    tmp_path, monkeypatch
):
    """Remediation advice is a claim about the CLI, so it gets tested like one.

    Doctor tells the operator to `report --failed` then `tasks retry`. If that
    path did not really exist — or did not really release the bead — the check
    would be worse than silence: it would name a defect and then send someone
    down a dead end.
    """
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="stuck", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=3600)
    beads.update(task.id, lease_expires_at="garbage")
    attempt = beads.get(task.id).lease_attempt

    # Nothing automatic rescues it — that is the premise of the FAIL.
    assert beads.reap_expired_leases() == []
    assert beads.ready(assigned_agent=WORKER) == []

    step1 = runner.invoke(
        main,
        [
            "report",
            task.id,
            "--attempt",
            str(attempt),
            "--failed",
            "--result",
            "stuck lease cleared",
        ],
    )
    assert step1.exit_code == 0, step1.output

    step2 = runner.invoke(main, ["tasks", "retry", task.id])
    assert step2.exit_code == 0, step2.output

    recovered = beads.get(task.id)
    assert recovered.status == TaskStatus.PENDING
    assert recovered.leased_by is None
    assert [t.id for t in beads.ready(assigned_agent=WORKER)] == [task.id]
    assert lease_pathologies(beads.list(), _now())["corrupt_expiry"] == []


# ------------------------------------------------------------- reconcile set


def test_unresolved_set_includes_a_reaped_bead_this_worker_held(tmp_path):
    """The important case: the lease is GONE, and the risk is not.

    A worker offline long enough to matter is a worker whose leases expired.
    Matching only on `leased_by` would find nothing at exactly the moment the
    check matters most.
    """
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)
    beads.reap_expired_leases()

    assert beads.get(task.id).leased_by is None
    assert [t.id for t in unresolved_for_worker(beads.list(), WORKER)] == [task.id]


def test_unresolved_set_excludes_a_bead_never_handed_out(tmp_path):
    """Routed-to is not the same as dispatched-to; only the latter can be half-done."""
    beads = Beads(tmp_path / "tasks.jsonl")
    beads.create(title="t", description="d", assigned_agent=WORKER)
    assert unresolved_for_worker(beads.list(), WORKER) == []


def test_unresolved_set_excludes_another_workers_bead(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent="other-worker")
    beads.claim(task.id, "other-worker", ttl_seconds=0)
    assert unresolved_for_worker(beads.list(), WORKER) == []


# --------------------------------------------------------- reconcile via CLI


def test_pull_reconcile_lists_without_claiming(tmp_path, monkeypatch):
    """--reconcile hands back the open questions and takes nothing new."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    held = beads.create(title="held", description="d", assigned_agent=WORKER)
    beads.claim(held.id, WORKER, ttl_seconds=0)
    fresh = beads.create(title="fresh", description="d", assigned_agent=WORKER)

    result = runner.invoke(main, ["pull", "--agent", WORKER, "--reconcile"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["mode"] == "reconcile"
    assert payload["claimed"] == []
    assert payload["count"] == 0
    assert [o["id"] for o in payload["outstanding"]] == [held.id]
    assert payload["outstanding"][0]["lease_attempt"] == 1
    assert "report" in payload["contract"]

    # Nothing moved: the untouched bead is still free, and the held one was NOT
    # reaped — releasing it mid-investigation is the thing this mode prevents.
    assert beads.get(fresh.id).status == TaskStatus.PENDING
    assert beads.get(fresh.id).lease_attempt == 0
    assert beads.get(held.id).status == TaskStatus.IN_PROGRESS
    assert beads.get(held.id).leased_by == WORKER


def test_pull_arms_reconcile_automatically_after_a_long_silence(tmp_path, monkeypatch):
    """The worker does not have to remember to ask."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    ledger = PullLedger(pull_ledger_path(tmp_path / "children" / "registry.jsonl"))
    ledger.record(
        WORKER,
        agent=WORKER,
        node=None,
        mode="claim",
        now=_now() - timedelta(hours=48),
    )

    payload = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert payload["mode"] == "reconcile"
    assert "48." in payload["reason"] or "reconcile-after" in payload["reason"]
    assert [o["id"] for o in payload["outstanding"]] == [task.id]


def test_a_steadily_polling_worker_is_never_held(tmp_path, monkeypatch):
    """Short-gap churn needs no ceremony — otherwise the guard is just friction."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    assert runner.invoke(main, ["pull", "--agent", WORKER]).exit_code == 0

    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    payload = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert payload["mode"] == "claim"
    assert payload["reaped"] == [task.id]
    assert [c["id"] for c in payload["claimed"]] == [task.id]


def test_a_worker_with_nothing_outstanding_pulls_normally_after_a_long_silence(
    tmp_path, monkeypatch
):
    """The guard arms on RISK, not on absence. No open beads, no ceremony."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    beads.create(title="t", description="d", assigned_agent=WORKER)

    payload = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert payload["mode"] == "claim"
    assert payload["count"] == 1


def test_the_guard_does_not_clear_by_merely_polling_again(tmp_path, monkeypatch):
    """A pure clock trigger would be advisory: re-poll and the guard is gone.

    This is the whole reason the guard disarms on the outstanding SET rather
    than on elapsed time.
    """
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    first = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    second = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert first["mode"] == "reconcile"
    assert second["mode"] == "reconcile"


def test_resolving_the_outstanding_bead_disarms_the_guard(tmp_path, monkeypatch):
    """The worker-side contract, end to end: report, then pull gets work.

    Note the bead is reported while it sits reaped-and-PENDING — the fence
    checks the attempt counter, which reaping preserves, so the recovery path
    the reconcile output instructs the worker to take actually works.
    """
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    stale = beads.create(title="stale", description="d", assigned_agent=WORKER)
    beads.claim(stale.id, WORKER, ttl_seconds=0)
    nxt = beads.create(title="next", description="d", assigned_agent=WORKER)

    held = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert held["mode"] == "reconcile"
    attempt = held["outstanding"][0]["lease_attempt"]

    done = runner.invoke(
        main,
        [
            "report",
            stale.id,
            "--attempt",
            str(attempt),
            "--done",
            "--result",
            "ADO says the write landed",
        ],
    )
    assert done.exit_code == 0, done.output

    after = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert after["mode"] == "claim"
    assert [c["id"] for c in after["claimed"]] == [nxt.id]


def test_force_overrides_the_armed_guard(tmp_path, monkeypatch):
    """Break-glass exists, and says so in the output rather than hiding."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    payload = json.loads(
        runner.invoke(main, ["pull", "--agent", WORKER, "--force"]).stdout
    )
    assert payload["mode"] == "force"
    assert payload["reaped"] == [task.id]
    assert [c["id"] for c in payload["claimed"]] == [task.id]


def test_reconcile_after_can_be_tightened(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    assert runner.invoke(main, ["pull", "--agent", WORKER]).exit_code == 0
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    # Zero-hour threshold: any gap at all counts as silence.
    payload = json.loads(
        runner.invoke(
            main, ["pull", "--agent", WORKER, "--reconcile-after", "0"]
        ).stdout
    )
    assert payload["mode"] == "reconcile"


# ------------------------------------------------------------- pull ledger


def test_every_pull_stamps_the_ledger(tmp_path, monkeypatch):
    """The stamp is a remote node's only heartbeat — an empty poll still counts."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner, tmp_path)
    assert runner.invoke(main, ["pull", "--agent", WORKER]).exit_code == 0

    ledger = PullLedger(pull_ledger_path(tmp_path / "children" / "registry.jsonl"))
    row = ledger.get(WORKER)
    assert row is not None
    assert row["agent"] == WORKER
    assert row["last_mode"] == "claim"
    assert row["pulls"] == 1


def test_a_reconcile_pull_still_counts_as_liveness(tmp_path, monkeypatch):
    """A worker held at the gate is emphatically alive; it must not read as dead."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    runner.invoke(main, ["pull", "--agent", WORKER])
    row = PullLedger(
        pull_ledger_path(tmp_path / "children" / "registry.jsonl")
    ).get(WORKER)
    assert row["last_mode"] == "reconcile"
    assert row["outstanding"] == 1
    assert row["last_reconcile_at"]


def test_a_corrupt_ledger_reads_empty_and_does_not_break_pull(tmp_path, monkeypatch):
    """Diagnostic bookkeeping must never take down the dispatch path."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    beads.create(title="t", description="d", assigned_agent=WORKER)

    path = pull_ledger_path(tmp_path / "children" / "registry.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    result = runner.invoke(main, ["pull", "--agent", WORKER])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["count"] == 1


# ------------------------------------------------------- remote node liveness


def _remote(interval: str = "1h") -> ChildRef:
    return ChildRef(
        name=NODE,
        path="/Users/somebody/Portfolio/frontsteps",
        expected_interval=interval,
        host="macbook-pro.local",
    )


def test_remote_node_that_never_pulled_fails(tmp_path):
    result = verify_remote_child(_remote(), None)
    assert result["level"] == "fail"
    assert "NEVER pulled" in result["detail"]


def test_remote_node_pulling_on_cadence_is_ok(tmp_path):
    entry = {"last_pull_at": _now().isoformat(), "last_mode": "claim", "last_claimed": 2}
    result = verify_remote_child(_remote(), entry)
    assert result["level"] == "ok"
    assert result["staleness_seconds"] < 60


def test_remote_node_past_its_interval_times_grace_fails(tmp_path):
    entry = {
        "last_pull_at": (_now() - timedelta(hours=5)).isoformat(),
        "last_mode": "claim",
    }
    result = verify_remote_child(_remote("1h"), entry)
    assert result["level"] == "fail"
    assert "has not pulled" in result["detail"]
    assert result["staleness_seconds"] > 3600


def test_remote_node_held_at_the_reconcile_gate_warns(tmp_path):
    """Alive but taking no work is its own state — not healthy, not dead."""
    entry = {
        "last_pull_at": _now().isoformat(),
        "last_mode": "reconcile",
        "outstanding": 3,
    }
    result = verify_remote_child(_remote(), entry)
    assert result["level"] == "warn"
    assert "RECONCILE GATE" in result["detail"]


def test_remote_node_with_manual_cadence_is_never_late(tmp_path):
    result = verify_remote_child(_remote("manual"), None)
    assert result["level"] == "unverified"
    assert result["ok"] is True


def test_verify_remote_child_refuses_a_local_child(tmp_path):
    """Wrong tool for a local node — its heartbeat is on this disk."""
    import pytest

    with pytest.raises(ValueError, match="local child"):
        verify_remote_child(ChildRef(name="local", path=str(tmp_path)), None)


def test_doctor_surfaces_a_stale_remote_node(tmp_path, monkeypatch, capsys):
    """The gap this closes: a dead launchd job used to look like an idle one."""
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)

    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    registry.add(_remote("1h"))
    ledger = PullLedger(pull_ledger_path(registry.path))
    ledger.record(
        NODE, agent=WORKER, node=NODE, mode="claim", now=_now() - timedelta(hours=9)
    )

    run_doctor(cfg)
    out = capsys.readouterr().out

    # 9h of silence on a 1h cadence is BROKEN, not degraded: that lane cannot
    # claim, which is exactly the "dead launchd job looks like an idle one"
    # failure this check exists to end.
    assert "BROKEN (children.remote_liveness)" in out
    assert NODE in out
    assert "has not pulled" in out


def test_doctor_is_quiet_about_a_healthy_remote_node(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)

    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    registry.add(_remote("1h"))
    PullLedger(pull_ledger_path(registry.path)).record(
        NODE, agent=WORKER, node=NODE, mode="claim", claimed=1
    )

    run_doctor(cfg)
    out = capsys.readouterr().out

    assert "remote node(s) pulling on cadence" in out
    assert "has not pulled" not in out


def test_a_remote_node_is_no_longer_listed_as_unpollable(tmp_path, monkeypatch, capsys):
    """It IS polled now — via the ledger. Saying otherwise contradicts check (u)."""
    monkeypatch.chdir(tmp_path)
    cfg = _doctor_cfg(tmp_path)
    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    registry.add(_remote("1h"))

    run_doctor(cfg)
    out = capsys.readouterr().out
    assert "not pollable" not in out
