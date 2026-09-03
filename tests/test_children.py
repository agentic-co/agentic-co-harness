"""verify_child tests: missing / stale / fresh-with-errors / healthy.

Frozen clock injection throughout — no real time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentco_harness.children import ChildRef, ChildRegistry, verify_child

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _child_instance(tmp_path, name="acme", heartbeat: dict | None = None) -> ChildRef:
    inst = tmp_path / name
    inst.mkdir()
    (inst / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    if heartbeat is not None:
        (inst / "heartbeat.json").write_text(json.dumps(heartbeat))
    return ChildRef(name=name, path=str(inst), expected_interval="1h")


def _heartbeat(completed_at: datetime, errors: int = 0) -> dict:
    return {
        "instance": "acme",
        "cycle_completed_at": completed_at.isoformat(),
        "beads_open": 0,
        "beads_done_this_cycle": 1,
        "recurring_spawned_this_cycle": 0,
        "errors_this_cycle": errors,
        "version": "0.3.0",
    }


def test_missing_path_fails(tmp_path):
    child = ChildRef(name="ghost", path=str(tmp_path / "nope"), expected_interval="1h")
    result = verify_child(child, now=NOW)
    assert result["level"] == "fail"
    assert "does not exist" in result["detail"]


def test_missing_heartbeat_fails_never_completed(tmp_path):
    child = _child_instance(tmp_path, heartbeat=None)
    result = verify_child(child, now=NOW)
    assert result["level"] == "fail"
    assert "never completed a cycle" in result["detail"]


def test_stale_heartbeat_fails_with_exact_staleness(tmp_path):
    # expected 1h × grace 2.0 = 7200s allowed; 3h old = 10800s -> FAIL
    child = _child_instance(tmp_path, heartbeat=_heartbeat(NOW - timedelta(hours=3)))
    result = verify_child(child, now=NOW)
    assert result["level"] == "fail"
    assert result["staleness_seconds"] == 3 * 3600
    assert "10800s ago" in result["detail"]
    assert "allowed 7200s" in result["detail"]


def test_grace_multiplier_math_boundary(tmp_path):
    # 1h59m old with 1h interval × grace 2.0 -> still inside the window
    child = _child_instance(
        tmp_path, heartbeat=_heartbeat(NOW - timedelta(minutes=119))
    )
    assert verify_child(child, now=NOW)["ok"] is True
    # custom grace 1.5 -> 90min allowed -> 119min is stale
    assert verify_child(child, now=NOW, grace=1.5)["level"] == "fail"


def test_fresh_with_errors_warns_but_passes(tmp_path):
    child = _child_instance(
        tmp_path, heartbeat=_heartbeat(NOW - timedelta(minutes=30), errors=2)
    )
    result = verify_child(child, now=NOW)
    assert result["ok"] is True
    assert result["level"] == "warn"
    assert "2 error(s)" in result["detail"]


def test_healthy_child_ok(tmp_path):
    child = _child_instance(tmp_path, heartbeat=_heartbeat(NOW - timedelta(minutes=10)))
    result = verify_child(child, now=NOW)
    assert result == {
        "child": "acme",
        "ok": True,
        "level": "ok",
        "detail": result["detail"],
        "staleness_seconds": 600.0,
    }


def _backoff_heartbeat(completed_at: datetime, interval_s: float) -> dict:
    hb = _heartbeat(completed_at)
    hb["current_interval_s"] = interval_s
    hb["next_due_at"] = (completed_at + timedelta(seconds=interval_s)).isoformat()
    return hb


def test_backed_off_child_not_falsely_stale(tmp_path):
    """A legitimately idle child on a 4h backoff interval must NOT trip the
    parent's staleness alarm just because 4h > expected_interval×grace."""
    # Child last completed 4h ago, is on a 4h interval → next_due is NOW.
    # Deadline = next_due + 1.5×4h = NOW + 6h, so NOW is well inside → OK.
    hb = _backoff_heartbeat(NOW - timedelta(hours=4), interval_s=4 * 3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    result = verify_child(child, now=NOW)
    assert result["ok"] is True
    # The old fixed-interval rule (1h×2=2h allowed) would have called this stale.


def test_backed_off_child_past_its_own_deadline_fails(tmp_path):
    """Past next_due_at + due_grace×current_interval, a backed-off child is
    genuinely dead and must FAIL — the deadline is respected, not ignored."""
    # Completed 12h ago on a 4h interval: next_due = NOW-8h,
    # deadline = NOW-8h + 1.5×4h = NOW-2h → NOW is 2h past → FAIL.
    hb = _backoff_heartbeat(NOW - timedelta(hours=12), interval_s=4 * 3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    result = verify_child(child, now=NOW)
    assert result["level"] == "fail"
    assert "past its own deadline" in result["detail"]
    assert result["staleness_seconds"] == 12 * 3600


def test_unreadable_heartbeat_fails(tmp_path):
    child = _child_instance(tmp_path)
    (tmp_path / "acme" / "heartbeat.json").write_text("{broken")
    result = verify_child(child, now=NOW)
    assert result["level"] == "fail"
    assert "unreadable heartbeat" in result["detail"]


def test_heartbeat_resolves_through_child_config(tmp_path):
    """A child with a custom tasks_path keeps its heartbeat beside the queue."""
    inst = tmp_path / "custom"
    (inst / "data").mkdir(parents=True)
    (inst / "config.yaml").write_text("tasks_path: data/tasks.jsonl\n")
    (inst / "data" / "heartbeat.json").write_text(
        json.dumps(_heartbeat(NOW - timedelta(minutes=5)))
    )
    child = ChildRef(name="custom", path=str(inst), expected_interval="1h")
    assert verify_child(child, now=NOW)["level"] == "ok"


def test_registry_quarantines_bad_lines(tmp_path, capsys):
    path = tmp_path / "registry.jsonl"
    good = ChildRef(name="a", path="/tmp/a").to_json()
    path.write_text(good + "\n{nope\n" + '{"name": "b", "path": "/tmp/b", "expected_interval": "1w"}\n')
    registry = ChildRegistry(path)
    children = registry.list()
    out = capsys.readouterr().out
    assert [c.name for c in children] == ["a"]
    assert out.count("quarantined") == 2


def test_registry_rejects_duplicate_names(tmp_path):
    import pytest

    registry = ChildRegistry(tmp_path / "registry.jsonl")
    registry.add(ChildRef(name="a", path="/tmp/a"))
    with pytest.raises(ValueError):
        registry.add(ChildRef(name="a", path="/tmp/other"))


# --- non-beads children -----------------------------------------------------
#
# Regression cover for the silent monitoring hole (2026-07-18): 3 of 6 real
# children (sommeli/vault-only, frontsteps/ado-backed, personal/manual) were
# quarantined by a schema that required `path` and a parseable interval, while
# status() still returned a clean 3-child list — so the portfolio read as fully
# monitored with half of it unwatched. Rows below are the literal production
# registry lines.

VAULT_ONLY = json.dumps({
    "name": "sommeli", "path": "/Users/x/Code/sommeliwhey", "type": "vault-only",
    "vault_path": "1 - Projects/SommeliCo", "expected_interval": "manual", "notify": False,
})
ADO_BACKED = json.dumps({
    "name": "frontsteps", "type": "ado-backed", "ado_org": "frontsteps",
    "vault_path": "1 - Projects/Frontsteps", "notify": False,
})
MANUAL_BEADS = json.dumps({
    "name": "personal", "path": "/Users/x/Portfolio/personal", "type": "beads",
    "vault_path": "2 - Areas/Personal", "expected_interval": "manual", "notify": False,
})


def test_vault_only_child_is_not_quarantined():
    assert ChildRef.from_json(VAULT_ONLY).name == "sommeli"


def test_ado_backed_child_without_path_is_not_quarantined():
    child = ChildRef.from_json(ADO_BACKED)
    assert child.name == "frontsteps"
    assert child.path is None


def test_manual_interval_child_is_not_quarantined():
    assert ChildRef.from_json(MANUAL_BEADS).name == "personal"


def test_unverifiable_children_report_unverified_not_ok_or_fail():
    """An unknown must not be laundered into a green light, nor alarm forever."""
    for raw in (VAULT_ONLY, ADO_BACKED, MANUAL_BEADS):
        result = verify_child(ChildRef.from_json(raw))
        assert result["level"] == "unverified", raw
        assert result["ok"] is True, raw
        assert "not verifiable" in result["detail"]


def test_genuinely_malformed_interval_still_quarantines():
    """The guard must not become a blanket amnesty for bad rows."""
    bad = json.dumps({"name": "broken", "path": "/tmp/x", "expected_interval": "7 fortnights"})
    with pytest.raises(ValueError):
        ChildRef.from_json(bad)


def test_child_without_a_name_still_quarantines(tmp_path):
    """Asserted through the registry: that is where quarantine actually happens,
    and it catches TypeError (missing field) as well as ValueError."""
    registry = tmp_path / "registry.jsonl"
    registry.write_text(json.dumps({"path": "/tmp/x"}) + "\n")

    reg = ChildRegistry(registry)

    assert reg.list() == []
    assert len(reg._quarantined) == 1


def test_all_six_production_registry_rows_parse(tmp_path):
    """The real portfolio: 6 registered must yield 6 monitored, not 3."""
    registry = tmp_path / "registry.jsonl"
    registry.write_text("\n".join([
        json.dumps({"name": "recorro", "path": "/tmp/r", "expected_interval": "1h"}),
        json.dumps({"name": "feeds", "path": "/tmp/f", "expected_interval": "1d"}),
        json.dumps({"name": "m3bl", "path": "/tmp/m", "type": "beads", "expected_interval": "1h"}),
        VAULT_ONLY, ADO_BACKED, MANUAL_BEADS,
    ]) + "\n")

    reg = ChildRegistry(registry)
    children = reg.list()

    assert len(children) == 6
    assert reg._quarantined == []


# --- Host-outage awareness (ac-c5020eb8) ------------------------------------
# Parent and children are user LaunchAgents in one launchd domain. When that
# domain dies (logout / reboot-before-login / host sleep) nothing ticks, and
# the first cycle back saw EVERY child as stale → one full-price RCA bead per
# child, per outage, for children that were healthy and self-recovered.


def test_stale_child_warns_when_parent_is_equally_late(tmp_path):
    """Recorro 2026-08-31: child 6h31m past its deadline, parent 8h past its
    own. Same outage explains both → WARN, not FAIL."""
    hb = _backoff_heartbeat(NOW - timedelta(hours=9), interval_s=3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    result = verify_child(
        child, now=NOW, parent_next_due_at=NOW - timedelta(hours=8)
    )
    assert result["level"] == "warn"
    assert result["ok"] is True
    assert "host-level outage" in result["detail"]
    # The original verdict is preserved for the reader, not thrown away.
    assert "past its own deadline" in result["detail"]
    assert result["staleness_seconds"] == 9 * 3600


def test_stale_child_still_fails_when_parent_is_on_time(tmp_path):
    """No parent deadline passed, or a punctual parent → unchanged FAIL. This
    is the guard that the downgrade cannot silence a real dead child."""
    hb = _backoff_heartbeat(NOW - timedelta(hours=9), interval_s=3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    assert verify_child(child, now=NOW)["level"] == "fail"
    on_time = verify_child(
        child, now=NOW, parent_next_due_at=NOW + timedelta(minutes=30)
    )
    assert on_time["level"] == "fail"


def test_child_staler_than_the_outage_still_fails(tmp_path):
    """A child dead for days is not excused by an 8h host outage — its own
    lateness far exceeds the parent's plus one interval of slack."""
    hb = _backoff_heartbeat(NOW - timedelta(days=3), interval_s=3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    result = verify_child(
        child, now=NOW, parent_next_due_at=NOW - timedelta(hours=8)
    )
    assert result["level"] == "fail"
    assert "host-level outage" not in result["detail"]


def test_host_outage_downgrade_applies_to_the_classic_check(tmp_path):
    """Children on an older build (no next_due_at) get the same treatment."""
    child = _child_instance(tmp_path, heartbeat=_heartbeat(NOW - timedelta(hours=9)))
    assert verify_child(child, now=NOW)["level"] == "fail"
    result = verify_child(
        child, now=NOW, parent_next_due_at=NOW - timedelta(hours=8)
    )
    assert result["level"] == "warn"
    assert "host-level outage" in result["detail"]


def test_stale_child_warns_when_the_parent_only_just_recovered(tmp_path):
    """ache 2026-08-31, firing #2 (ac-af68be91).

    launchd brought the parent back at 07:00:08Z and the children at 07:46:45Z.
    On the parent's second cycle it is punctual again — `parent_next_due_at` is
    in the future — while ache is still carrying the same 9h outage. Without
    the parent's memory of its OWN outage this FAILs and files a full-price RCA
    bead for a child that recovers one tick later.
    """
    hb = _backoff_heartbeat(NOW - timedelta(hours=9), interval_s=3600)
    child = _child_instance(tmp_path, heartbeat=hb)

    punctual_parent = NOW + timedelta(minutes=14)
    assert (
        verify_child(child, now=NOW, parent_next_due_at=punctual_parent)["level"]
        == "fail"
    )

    result = verify_child(
        child,
        now=NOW,
        parent_next_due_at=punctual_parent,
        parent_recent_outage_s=9 * 3600,
    )
    assert result["level"] == "warn"
    assert result["ok"] is True
    assert "host-level outage" in result["detail"]


def test_recent_outage_memory_does_not_excuse_a_child_dead_for_days(tmp_path):
    """The second evidence source is bounded exactly like the first."""
    hb = _backoff_heartbeat(NOW - timedelta(days=3), interval_s=3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    result = verify_child(
        child,
        now=NOW,
        parent_next_due_at=NOW + timedelta(minutes=14),
        parent_recent_outage_s=9 * 3600,
    )
    assert result["level"] == "fail"
    assert "host-level outage" not in result["detail"]


def test_no_recent_outage_leaves_the_loud_path_untouched(tmp_path):
    """Zero / absent outage memory must not change any existing verdict."""
    hb = _backoff_heartbeat(NOW - timedelta(hours=9), interval_s=3600)
    child = _child_instance(tmp_path, heartbeat=hb)
    for value in (0.0, None):
        result = verify_child(
            child,
            now=NOW,
            parent_next_due_at=NOW + timedelta(minutes=30),
            parent_recent_outage_s=value,
        )
        assert result["level"] == "fail"
