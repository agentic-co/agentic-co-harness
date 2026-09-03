"""Cross-machine lease protocol (bead ac-9cae7593).

The claim is a compare-and-set, the report is fenced on the lease attempt, and
an expired lease returns its bead to the ready set without failing it. These
are the invariants that let a second machine pull work out of this store over
SSH without two workers ever executing the same bead — or, worse, a late worker
overwriting the result of the one that replaced it.

Design of record: Plans/TwoMachineLifeos.md, "Dispatch: SSH pull + leases".
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    DEFAULT_LEASE_TTL_S,
    Beads,
    LeaseError,
    Task,
    TaskStatus,
)
from agentco_harness.cli import main

WORKER = "frontsteps-worker"


def _beads(tmp_path) -> Beads:
    return Beads(tmp_path / "tasks.jsonl")


# --------------------------------------------------------------- serialization


def test_lease_fields_round_trip_through_json(tmp_path):
    """to_json/from_json must be symmetric or a lease dies at the store boundary."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=600)

    reloaded = Task.from_json(beads.get(task.id).to_json())
    assert reloaded.leased_by == WORKER
    assert reloaded.lease_attempt == 1
    assert reloaded.lease_expires_at is not None
    # And the raw line really carries them — a field that only survives in
    # memory would look fine here and vanish across the SSH hop.
    raw = json.loads((tmp_path / "tasks.jsonl").read_text().strip().splitlines()[0])
    assert {"leased_by", "lease_attempt", "lease_expires_at"} <= set(raw)


def test_legacy_line_without_lease_fields_parses_free(tmp_path):
    """Every bead written before leases existed must read back as claimable."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    record = json.loads(beads.get(task.id).to_json())
    for key in ("leased_by", "lease_attempt", "lease_expires_at"):
        record.pop(key)
    (tmp_path / "tasks.jsonl").write_text(json.dumps(record) + "\n")

    legacy = Beads(tmp_path / "tasks.jsonl").get(task.id)
    assert legacy.leased_by is None
    assert legacy.lease_attempt == 0
    assert Beads(tmp_path / "tasks.jsonl").claim(task.id, WORKER) is not None


# ------------------------------------------------------------------------ CAS


def test_two_claimers_race_and_exactly_one_wins(tmp_path):
    """The whole reason the claim moved inside the lock.

    Sixteen threads, each with its own Beads handle (separate file descriptor,
    so they contend on the real flock the way a daemon and an SSH-invoked CLI
    do), all claim the same bead. Exactly one may come back with a task.
    """
    beads = _beads(tmp_path)
    task = beads.create(title="contested", description="d", assigned_agent=WORKER)

    winners: list[Task] = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def claimer(n: int):
        own = Beads(tmp_path / "tasks.jsonl")
        start.wait()
        got = own.claim(task.id, f"worker-{n}")
        if got is not None:
            with lock:
                winners.append(got)

    threads = [threading.Thread(target=claimer, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"{len(winners)} claimants all thought they won"
    stored = beads.get(task.id)
    assert stored.status == TaskStatus.IN_PROGRESS
    assert stored.leased_by == winners[0].leased_by
    # One handout, one increment — the fence must not be inflated by losers.
    assert stored.lease_attempt == 1
    assert len(beads._quarantined) == 0


def test_claim_refuses_a_bead_already_leased(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    assert beads.claim(task.id, "first") is not None
    assert beads.claim(task.id, "second") is None
    assert beads.get(task.id).leased_by == "first"


def test_claim_refuses_a_non_pending_bead(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.complete(task.id, result="already done")
    assert beads.claim(task.id, WORKER) is None
    assert beads.get(task.id).status == TaskStatus.DONE


def test_a_lost_claim_writes_nothing(tmp_path):
    """A refused CAS must leave the store byte-identical, not half-applied."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, "first")
    before = (tmp_path / "tasks.jsonl").read_bytes()

    assert beads.claim(task.id, "second") is None
    assert (tmp_path / "tasks.jsonl").read_bytes() == before


def test_claim_default_ttl_is_in_the_future(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    claimed = beads.claim(task.id, WORKER)
    expires = datetime.fromisoformat(claimed.lease_expires_at)
    assert expires > datetime.now(timezone.utc)
    assert expires <= datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_LEASE_TTL_S)


# --------------------------------------------------------------------- expiry


def test_expired_lease_is_reclaimable_and_not_failed(tmp_path):
    """Expiry is a statement about the worker, never about the work."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)  # dead on arrival

    reaped = beads.reap_expired_leases()
    assert [t.id for t in reaped] == [task.id]

    back = beads.get(task.id)
    assert back.status == TaskStatus.PENDING  # NOT failed
    assert back.leased_by is None
    assert back.lease_expires_at is None
    assert back.lease_attempt == 1  # the record of the handout survives
    assert task.id in {t.id for t in beads.ready()}

    second = beads.claim(task.id, "other-worker")
    assert second is not None
    assert second.lease_attempt == 2


def test_reaping_leaves_live_leases_and_terminal_beads_alone(tmp_path):
    beads = _beads(tmp_path)
    live = beads.create(title="live", description="d")
    finished = beads.create(title="finished", description="d")
    beads.claim(live.id, WORKER, ttl_seconds=3600)
    beads.claim(finished.id, WORKER, ttl_seconds=0)
    beads.report_result(
        finished.id, attempt=1, status=TaskStatus.DONE, result="ok"
    )

    assert beads.reap_expired_leases() == []
    assert beads.get(live.id).status == TaskStatus.IN_PROGRESS
    assert beads.get(finished.id).status == TaskStatus.DONE


def test_ready_hides_a_pending_bead_under_a_live_lease(tmp_path):
    """A bead can be PENDING and still owned; ready() must respect that."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER, ttl_seconds=3600)
    # Reverted to PENDING without releasing the lease (the shape a hand-edit or
    # a half-finished protocol step leaves behind).
    beads.update(task.id, status=TaskStatus.PENDING)

    assert task.id not in {t.id for t in beads.ready()}
    assert beads.claim(task.id, "someone-else") is None


def test_ready_returns_a_bead_whose_lease_has_expired(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER, ttl_seconds=0)
    beads.update(task.id, status=TaskStatus.PENDING)

    assert task.id in {t.id for t in beads.ready()}


# --------------------------------------------------------------------- fencing


def test_stale_attempt_is_rejected_loudly(tmp_path):
    """The attack this protocol exists to survive.

    Worker A claims, goes dark, is reaped. Worker B claims the same bead and
    finishes it. Worker A wakes and reports against attempt 1. Accepting that
    would overwrite B's real result with a stale one.
    """
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, "worker-a", ttl_seconds=0)
    beads.reap_expired_leases()
    beads.claim(task.id, "worker-b")
    beads.report_result(task.id, attempt=2, status=TaskStatus.DONE, result="B's work")

    with pytest.raises(LeaseError) as exc:
        beads.report_result(
            task.id, attempt=1, status=TaskStatus.FAILED, result="A's stale failure"
        )
    assert "attempt 1" in str(exc.value)

    survived = beads.get(task.id)
    assert survived.status == TaskStatus.DONE
    assert survived.result == "B's work"


def test_report_releases_the_lease_but_keeps_the_attempt(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER)
    done = beads.report_result(task.id, attempt=1, status=TaskStatus.DONE, result="ok")

    assert done.status == TaskStatus.DONE
    assert done.leased_by is None
    assert done.lease_expires_at is None
    assert done.lease_attempt == 1


def test_report_failed_records_the_failure(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER)
    failed = beads.report_result(
        task.id, attempt=1, status=TaskStatus.FAILED, result="boom"
    )
    assert failed.status == TaskStatus.FAILED
    assert failed.result == "boom"


def test_replaying_an_idempotency_key_is_a_no_op(tmp_path):
    """SSH can lose the response, so the worker must be able to send twice."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER)
    beads.report_result(
        task.id, attempt=1, status=TaskStatus.DONE, result="first", idempotency_key="k1"
    )
    first_updated_at = beads.get(task.id).updated_at

    # Same key, contradictory payload, and by now a stale attempt — all three
    # of which the dedup must swallow rather than write or raise.
    again = beads.report_result(
        task.id,
        attempt=1,
        status=TaskStatus.FAILED,
        result="second",
        idempotency_key="k1",
    )
    assert again.status == TaskStatus.DONE
    assert again.result == "first"
    assert beads.get(task.id).updated_at == first_updated_at


def test_report_refuses_a_non_terminal_status(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER)
    with pytest.raises(ValueError):
        beads.report_result(task.id, attempt=1, status=TaskStatus.PENDING)


def test_report_on_a_missing_bead_returns_none(tmp_path):
    assert (
        _beads(tmp_path).report_result(
            "ac-deadbeef", attempt=1, status=TaskStatus.DONE
        )
        is None
    )


def test_report_still_passes_through_the_verify_gate(tmp_path):
    """A remote worker must not be able to grade its own work either."""
    beads = _beads(tmp_path)
    task = beads.create(
        title="gated",
        description="d",
        metadata={"verify": {"class": "human", "check": "eyeball it"}},
    )
    beads.claim(task.id, WORKER)
    reported = beads.report_result(task.id, attempt=1, status=TaskStatus.DONE)
    assert reported.status == TaskStatus.AWAITING_VERIFY  # not DONE on its say-so


# ------------------------------------------------------------------------ CLI


def _init(runner, tmp_path):
    assert runner.invoke(main, ["init"]).exit_code == 0
    return Beads(tmp_path / "tasks.jsonl")


def test_pull_claims_up_to_max_and_leaves_the_rest(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    for i in range(5):
        beads.create(title=f"t{i}", description="d", assigned_agent=WORKER)

    result = runner.invoke(main, ["pull", "--agent", WORKER, "--max", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["count"] == 2
    assert len(payload["claimed"]) == 2
    assert all(c["lease_attempt"] == 1 for c in payload["claimed"])
    assert all(c["leased_by"] == WORKER for c in payload["claimed"])
    # The other three are untouched and still claimable by the next poll.
    assert len(beads.list(status=TaskStatus.PENDING)) == 3
    assert len(beads.ready(assigned_agent=WORKER)) == 3


def test_pull_only_takes_this_agents_lane(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    mine = beads.create(title="mine", description="d", assigned_agent=WORKER)
    theirs = beads.create(title="theirs", description="d", assigned_agent="other-lane")

    payload = json.loads(
        runner.invoke(main, ["pull", "--agent", WORKER]).stdout
    )
    assert [c["id"] for c in payload["claimed"]] == [mine.id]
    assert beads.get(theirs.id).status == TaskStatus.PENDING


def test_pull_is_empty_and_successful_when_there_is_no_work(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner, tmp_path)
    result = runner.invoke(main, ["pull", "--agent", WORKER])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["claimed"] == []


def test_pull_reaps_expired_leases_before_claiming(tmp_path, monkeypatch):
    """A worker that died mid-bead gets its own work back on the next poll.

    The first pull is what makes this the *steady-state* case: it stamps the
    pull ledger, so the second poll happens seconds later rather than after an
    unknown silence. That distinction matters since ac-48d8aba3 — a worker
    polling continuously has had no window in which a half-finished external
    write could have landed unobserved, so its own abandoned bead comes straight
    back with no reconcile ceremony. The offline case takes the other path and
    is covered in tests/test_lease_hardening.py.
    """
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    assert runner.invoke(main, ["pull", "--agent", WORKER]).exit_code == 0

    task = beads.create(title="abandoned", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER, ttl_seconds=0)

    payload = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    assert payload["reaped"] == [task.id]
    assert [c["id"] for c in payload["claimed"]] == [task.id]
    assert payload["claimed"][0]["lease_attempt"] == 2


def test_report_done_completes_the_bead(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    beads.create(title="t", description="d", assigned_agent=WORKER)

    pulled = json.loads(runner.invoke(main, ["pull", "--agent", WORKER]).stdout)
    bead = pulled["claimed"][0]

    result = runner.invoke(
        main,
        [
            "report",
            bead["id"],
            "--attempt",
            str(bead["lease_attempt"]),
            "--done",
            "--result",
            "shipped",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "done"

    stored = beads.get(bead["id"])
    assert stored.status == TaskStatus.DONE
    assert stored.result == "shipped"
    assert stored.leased_by is None


def test_report_with_a_stale_attempt_exits_two_and_writes_nothing(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="t", description="d", assigned_agent=WORKER)
    beads.claim(task.id, WORKER)

    result = runner.invoke(
        main, ["report", task.id, "--attempt", "99", "--done", "--result", "stale"]
    )
    assert result.exit_code == 2
    assert beads.get(task.id).status == TaskStatus.IN_PROGRESS
    assert beads.get(task.id).result is None


def test_report_requires_exactly_one_outcome(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    beads = _init(runner, tmp_path)
    task = beads.create(title="t", description="d")
    beads.claim(task.id, WORKER)

    assert runner.invoke(main, ["report", task.id, "--attempt", "1"]).exit_code == 1
    assert (
        runner.invoke(
            main, ["report", task.id, "--attempt", "1", "--done", "--failed"]
        ).exit_code
        == 1
    )
    assert beads.get(task.id).status == TaskStatus.IN_PROGRESS


def test_report_on_unknown_bead_exits_one(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init(runner, tmp_path)
    result = runner.invoke(
        main, ["report", "ac-deadbeef", "--attempt", "1", "--done"]
    )
    assert result.exit_code == 1
