"""Hub-side lease reaping on the heartbeat cycle (bead ac-fb137d8d).

`reap_expired_leases` used to be reachable only from `agentco pull`, which
makes recovery a function of someone asking for work. The hub is precisely the
node nobody pulls from: a bead whose remote worker lost power stays IN_PROGRESS
there forever — invisible to `ready()`, never retried, not even counted as
failed. `Orchestrator.cycle()` therefore reaps too, beside the supersede passes,
which solve the same class of problem: state that stopped being true and that
nothing else was ever going to correct.

These tests pin the *hub* side of that. What they must NOT do is make doctor's
lease checks redundant, so the last two assert the opposite direction: a lease
that expires between heartbeats is still an unreaped pathology doctor reports,
and the case reaping deliberately skips (a corrupt expiry) survives a cycle
untouched and still FAILs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import AgentConfig, Config
from agentco_harness.doctor import lease_pathologies
from agentco_harness.orchestrator import Orchestrator

WORKER = "frontsteps-worker"


def _soon() -> datetime:
    """A moment just after the seeded lease died.

    Deliberately relative to the wall clock rather than a frozen constant:
    `claim(ttl_seconds=0)` stamps an expiry from `datetime.now`, so a fixed
    `NOW` in the past would make the pathology look like a live lease and the
    test would pass or fail depending on the hour it ran.
    """
    return datetime.now(timezone.utc) + timedelta(minutes=1)


def _hub(tmp_path) -> Orchestrator:
    """A hub with one externally-executed worker declared.

    The worker is declared under `agents:` but has no in-process class, so a
    reclaimed bead is left for it rather than dispatched — which is the real
    deployed shape (the remote node claims over SSH) and keeps these tests
    about reaping rather than about execution.
    """
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.agents = {WORKER: AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


def _dead_lease(beads: Beads) -> str:
    """A bead handed to a worker that never came back."""
    task = beads.create(title="ado write", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)
    assert beads.get(task.id).status == TaskStatus.IN_PROGRESS
    return task.id


# ------------------------------------------------------------------ the fix


def test_a_cycle_reclaims_an_expired_lease_without_anyone_pulling(tmp_path, monkeypatch):
    """The hub heartbeat is the recovery path for a hub nobody pulls from."""
    monkeypatch.chdir(tmp_path)
    orch = _hub(tmp_path)
    task_id = _dead_lease(orch.beads)

    now = _soon()
    orch.cycle(now=now)

    back = orch.beads.get(task_id)
    assert back.status == TaskStatus.PENDING   # reclaimed, NOT failed
    assert back.leased_by is None
    assert back.lease_expires_at is None
    assert back.lease_attempt == 1             # the handout stays on the record
    assert task_id in {t.id for t in orch.beads.ready()}
    assert lease_pathologies(orch.beads.list(), now)["expired_unreaped"] == []


def test_the_cycle_says_what_it_reclaimed(tmp_path, monkeypatch, capsys):
    """Silent recovery is how a bead that churns forever stays invisible."""
    monkeypatch.chdir(tmp_path)
    orch = _hub(tmp_path)
    _dead_lease(orch.beads)

    orch.cycle(now=_soon())

    out = capsys.readouterr().out
    assert "reclaimed 1 bead(s) from expired leases" in out


def test_a_live_lease_survives_the_cycle(tmp_path, monkeypatch):
    """Reaping on the hub must never steal work from a worker that is working —
    that would hand the same bead to two machines at once, which is the exact
    failure the lease exists to prevent."""
    monkeypatch.chdir(tmp_path)
    orch = _hub(tmp_path)
    task = orch.beads.create(title="live", description="d", assigned_agent=WORKER)
    orch.beads.claim(task.id, WORKER, ttl_seconds=3600)

    orch.cycle(now=_soon())

    back = orch.beads.get(task.id)
    assert back.status == TaskStatus.IN_PROGRESS
    assert back.leased_by == WORKER


def test_backoff_never_hides_an_expired_lease_from_the_reaper(tmp_path, monkeypatch):
    """The gate that could have quietly undone this fix.

    An idle hub backs off, and a backed-off wake returns before the reaping
    pass runs. It does not matter here only because an IN_PROGRESS bead counts
    as ACTIONABLE, so the reset signal fires and the cycle runs in full. If
    `_reset_signal` ever stopped counting in-progress beads, a stuck lease
    would become unrecoverable on exactly the node this bead is about.
    """
    monkeypatch.chdir(tmp_path)
    orch = _hub(tmp_path)
    orch.config.backoff.enabled = True
    task_id = _dead_lease(orch.beads)
    now = _soon()
    # A heartbeat that says "not due for another week".
    (tmp_path / "heartbeat.json").write_text(
        json.dumps(
            {
                "cycle_completed_at": (now - timedelta(days=7)).isoformat(),
                "next_due_at": (now + timedelta(days=7)).isoformat(),
                "current_interval_s": 604800,
            }
        )
    )

    summary = orch.cycle(now=now)

    assert not summary.get("skipped")
    assert orch.beads.get(task_id).status == TaskStatus.PENDING


# ------------------------------------------------- doctor stays meaningful


def test_a_lease_that_expires_between_heartbeats_is_still_a_doctor_finding(tmp_path):
    """Reaping on the cycle narrows the window; it does not close it.

    A hub cycles hourly, so a lease that dies at :05 is unreaped for most of an
    hour. Weakening doctor's check because "the cycle handles it" would blind
    the one command an operator runs *during* an incident, which is precisely
    the window this pathology lives in.
    """
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    found = lease_pathologies(beads.list(), datetime.now(timezone.utc))
    assert [t.id for t in found["expired_unreaped"]] == [task.id]


def test_a_corrupt_expiry_survives_a_cycle_and_remains_doctors_alone(tmp_path, monkeypatch):
    """Reaping skips an unparseable expiry on purpose — the cycle inherits that
    restraint, so doctor remains the ONLY place this bead is ever mentioned."""
    monkeypatch.chdir(tmp_path)
    orch = _hub(tmp_path)
    task = orch.beads.create(title="t", description="d", assigned_agent=WORKER)
    orch.beads.claim(task.id, WORKER, ttl_seconds=3600)
    orch.beads.update(task.id, lease_expires_at="not-a-timestamp")

    now = _soon()
    orch.cycle(now=now)

    back = orch.beads.get(task.id)
    assert back.status == TaskStatus.IN_PROGRESS
    assert back.leased_by == WORKER
    found = lease_pathologies(orch.beads.list(), now)
    assert [t.id for t in found["corrupt_expiry"]] == [task.id]
