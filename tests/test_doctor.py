"""Doctor preflight checks, classified by consequence.

Exit codes here are class-derived: 0 all clear, 1 BROKEN present, 2 DEGRADED
only. Per-class behaviour and the "an aggregate cannot mask a BROKEN" property
live in `test_doctor_classes.py`; this file covers the individual checks.
"""

from __future__ import annotations

import json

import pytest
import yaml

from agentco_harness.doctor import run_doctor


@pytest.fixture(autouse=True)
def _hermetic_host(tmp_path, monkeypatch):
    """Cut doctor off from this machine's global state.

    Two checks read paths outside the node under test: the transkriptor ledger
    (`TRANSKRIPTOR_ROOT`, default ~/Feeds) and the egress route artifact
    (`LIFEOS_INFERENCE_ROUTES`, default ~/.claude). Before consequence classes
    they only ever produced WARN lines, which no assertion looked at, so the
    leak was invisible. Now a developer's stale PRIME cache or unverified
    vendor term would set exit 2 and fail every "healthy" test on their machine
    and pass on CI. Isolate both.
    """
    monkeypatch.setenv("TRANSKRIPTOR_ROOT", str(tmp_path / "_no_transkriptor"))
    artifact = tmp_path / "_inference-routes.json"
    from agentco_harness.egress import AGENT_ROUTE

    artifact.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "routes": {
                    name: {
                        "vendor": "test",
                        "model": "test-model",
                        "ceiling": "CONFIDENTIAL",
                        "ceilingUnsupervised": "INTERNAL",
                        "ceilingVerified": True,
                    }
                    for name in sorted(set(AGENT_ROUTE.values()))
                },
            }
        )
    )
    monkeypatch.setenv("LIFEOS_INFERENCE_ROUTES", str(artifact))

    # The child-scheduler check reads this host's real `~/Library/LaunchAgents`
    # and asks launchd what is loaded — machine state no test may depend on, in
    # either direction: on a developer's Mac every tmp_path child would read as
    # unscheduled (BROKEN), and on CI the check would silently never run. Point
    # it at an empty fixture dir and stub the loaded set; the check has its own
    # tests below that populate both deliberately.
    agents = tmp_path / "_launch_agents"
    agents.mkdir()
    monkeypatch.setenv("AGENTCO_LAUNCH_AGENTS_DIR", str(agents))
    monkeypatch.setattr("agentco_harness.doctor.loaded_launchd_labels", lambda: {"fixture"})


def test_doctor_healthy_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # company/ present so (f) is OK; local provider so (g) needs no key.
    (tmp_path / "company").mkdir()

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "sources": {"logs": {"enabled": True}},
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 0
    assert "BROKEN" not in out
    assert "DEGRADED" not in out


def _healthy_cfg(tmp_path) -> str:
    (tmp_path / "company").mkdir(exist_ok=True)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
            }
        )
    )
    return str(cfg_file)


def test_doctor_fails_on_unmonitored_child(tmp_path, monkeypatch, capsys):
    """Registry ↔ recurring drift: a registered child with no verify_child def."""
    from agentco_harness.children import ChildRef, ChildRegistry

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    registry.path.parent.mkdir(parents=True)
    registry.add(ChildRef(name="orphan-co", path=str(tmp_path)))

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "orphan-co" in out
    assert "NO verify_child" in out


def test_doctor_fails_on_orphaned_verify_def(tmp_path, monkeypatch, capsys):
    """Drift the other way: a verify_child def naming an unregistered child."""
    from agentco_harness.recurring import Recurring, RecurringDef

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    recurring = Recurring(tmp_path / "recurring.jsonl")
    recurring.add(
        RecurringDef(
            id="verify-ghost",
            title="Verify child instance: ghost",
            schedule={"every": "1h"},
            payload={"type": "verify_child", "child": "ghost"},
        )
    )

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "ghost" in out
    assert "drifted" in out


def test_doctor_in_sync_children_pass(tmp_path, monkeypatch, capsys):
    from agentco_harness.children import ChildRef, ChildRegistry
    from agentco_harness.recurring import Recurring, RecurringDef

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)

    child_dir = tmp_path / "kid"
    child_dir.mkdir()
    (child_dir / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    # A child that has never completed a cycle is BROKEN (see
    # test_doctor_broken_when_child_never_completed_a_cycle). This test is
    # about registry<->recurring agreement, so give it a live heartbeat.
    from datetime import datetime, timezone

    (child_dir / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": datetime.now(timezone.utc).isoformat()})
    )

    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    registry.path.parent.mkdir(parents=True)
    registry.add(ChildRef(name="kid", path=str(child_dir)))
    Recurring(tmp_path / "recurring.jsonl").add(
        RecurringDef(
            id="verify-kid",
            title="Verify child instance: kid",
            schedule={"every": "1h"},
            payload={"type": "verify_child", "child": "kid"},
        )
    )
    # A monitored child with no scheduler is its own BROKEN finding
    # (test_doctor_broken_when_child_has_no_scheduler). Give this one a job so
    # the exit code still measures registry<->recurring agreement.
    _write_launch_agent(tmp_path, "com.test.kid", child_dir, monkeypatch)

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 0
    assert "in sync for 1 child(ren)" in out


def _write_launch_agent(tmp_path, label: str, working_dir, monkeypatch=None) -> None:
    """Put a LaunchAgent for `working_dir` in the fixture dir.

    Passing `monkeypatch` also reports the job as bootstrapped. Writing the
    file without that leaves it present-but-unloaded, which is the state
    test_doctor_broken_when_scheduler_plist_present_but_not_loaded pins.
    """
    import plistlib

    agents = tmp_path / "_launch_agents"
    (agents / f"{label}.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["agentco", "cycle"],
                "WorkingDirectory": str(working_dir),
                "StartInterval": 3600,
            }
        )
    )
    if monkeypatch is not None:
        monkeypatch.setattr(
            "agentco_harness.doctor.loaded_launchd_labels", lambda: {"fixture", label}
        )


def _child_with_live_heartbeat(tmp_path, name: str = "kid"):
    """A registered, verify-def'd child whose heartbeat is fresh."""
    from datetime import datetime, timezone

    from agentco_harness.children import ChildRef, ChildRegistry
    from agentco_harness.recurring import Recurring, RecurringDef

    child_dir = tmp_path / name
    child_dir.mkdir()
    (child_dir / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (child_dir / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": datetime.now(timezone.utc).isoformat()})
    )
    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.add(ChildRef(name=name, path=str(child_dir)))
    Recurring(tmp_path / "recurring.jsonl").add(
        RecurringDef(
            id=f"verify-{name}",
            title=f"Verify child instance: {name}",
            schedule={"every": "1h"},
            payload={"type": "verify_child", "child": name},
        )
    )
    return child_dir


def test_doctor_broken_when_child_has_no_scheduler(tmp_path, monkeypatch, capsys):
    """The semijoias defect (ac-67fbc23f): monitored, healthy, unscheduled.

    `agentco add-company` links a child — starting the parent's staleness clock
    — without installing a launchd job. The node cycles once during onboarding,
    reads healthy for one interval, then goes stale with nothing wrong inside
    it. Doctor must name the missing scheduler while the heartbeat is still
    fresh, not leave the verify_child alarm to report it hours later.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    _child_with_live_heartbeat(tmp_path)

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "NO loaded launchd job" in out
    assert "kid" in out


def test_doctor_ok_when_child_has_a_loaded_scheduler(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    child_dir = _child_with_live_heartbeat(tmp_path)
    _write_launch_agent(tmp_path, "com.test.kid", child_dir, monkeypatch)

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 0
    assert "child 'kid' scheduled by com.test.kid" in out


def test_doctor_broken_when_scheduler_plist_present_but_not_loaded(
    tmp_path, monkeypatch, capsys
):
    """On disk is not the same as bootstrapped.

    An unloaded plist produces exactly the silence a missing one does, so the
    check must key on launchd's loaded set rather than on the file existing.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    child_dir = _child_with_live_heartbeat(tmp_path)
    _write_launch_agent(tmp_path, "com.test.unloaded", child_dir)
    monkeypatch.setattr("agentco_harness.doctor.loaded_launchd_labels", lambda: set())

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "NO loaded launchd job" in out


def test_scheduler_matcher_accepts_a_job_pointed_at_the_repo_root(tmp_path):
    """Recorro's shape: the plist runs a wrapper script from the repo root
    while the instance lives in `<repo>/.agentco`. Both are the same node."""
    from agentco_harness.doctor import scheduling_jobs_for

    repo = tmp_path / "repo"
    instance = repo / ".agentco"
    instance.mkdir(parents=True)
    jobs = [("com.test.wrapper", f"<string>{repo}/scripts/daemon.sh</string>")]

    assert scheduling_jobs_for(instance, jobs, {"com.test.wrapper"}) == [
        "com.test.wrapper"
    ]
    assert scheduling_jobs_for(tmp_path / "other" / ".agentco", jobs, {"com.test.wrapper"}) == []


def test_doctor_warns_on_bad_recurring_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    (tmp_path / "recurring.jsonl").write_text("{broken\n")

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    # RECLASSIFIED: a quarantined recurring def is never scheduled, so a
    # schedule the operator believes is running is not running.
    assert code == 1
    assert "BROKEN (store.recurring_parse)" in out
    assert "unparseable" in out


def test_unpollable_children_do_not_fail_the_verify_def_check(tmp_path, capsys):
    """The mirror-image bug: once non-beads children became visible, doctor
    demanded a verify_child def for children that have no heartbeat to poll."""
    (tmp_path / "children").mkdir()
    (tmp_path / "children" / "registry.jsonl").write_text("\n".join([
        json.dumps({"name": "sommeli", "path": "/tmp/s", "type": "vault-only",
                    "expected_interval": "manual"}),
        json.dumps({"name": "frontsteps", "type": "ado-backed"}),
    ]) + "\n")
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")

    run_doctor(str(tmp_path / "config.yaml"))

    out = capsys.readouterr().out
    assert "NO verify_child recurring def" not in out
    assert "not pollable" in out


def test_doctor_fails_when_codex_routed_but_binary_missing(
    tmp_path, monkeypatch, capsys
):
    """An agent routed to the codex CLI with codex not on PATH is a hard FAIL."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir()
    monkeypatch.setattr("shutil.which", lambda _name: None)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
                "agents": {"forge": {"model": "codex"}},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 1
    assert "BROKEN (cli.codex_present)" in out
    assert "codex" in out


def test_doctor_no_codex_reference_stays_silent(tmp_path, monkeypatch, capsys):
    """No config path routes to codex -> the check emits nothing and never FAILs,
    even when the binary is absent."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir()
    monkeypatch.setattr("shutil.which", lambda _name: None)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 0
    # Check that no doctor line mentions codex (the path may contain "codex" from
    # the test name, but doctor output should not).
    assert "codex CLI" not in out
    assert "routes to the codex" not in out


def test_doctor_warns_when_codex_routed_and_present_but_unauthenticated(
    tmp_path, monkeypatch, capsys
):
    """A resolvable but logged-out codex CLI is recoverable with codex login."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir()
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda path: str(tmp_path / ".codex" / "auth.json")
        if path == "~/.codex/auth.json"
        else path,
    )

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
                "agents": {"forge": {"model": "codex"}},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    # An unauthenticated but present CLI is reduced capability, not breakage:
    # `codex login` recovers it interactively.
    assert code == 2
    assert "DEGRADED (cli.codex_auth)" in out
    assert "codex login" in out
    assert "BROKEN" not in out


def test_doctor_oks_when_codex_routed_present_and_authenticated(
    tmp_path, monkeypatch, capsys
):
    """A resolvable codex CLI with a local auth file passes the auth check."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir()
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    auth_path = tmp_path / ".codex" / "auth.json"
    auth_path.parent.mkdir()
    auth_path.write_text("{}")
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda path: str(auth_path) if path == "~/.codex/auth.json" else path,
    )

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
                "agents": {"forge": {"model": "codex"}},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 0
    assert "FAIL" not in out
    assert "codex CLI authentication file exists" in out


def test_doctor_fails_when_agy_routed_but_binary_missing(
    tmp_path, monkeypatch, capsys
):
    """An agent routed to the agy CLI with agy not on PATH is a hard FAIL."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir()
    monkeypatch.setattr("shutil.which", lambda _name: None)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
                "agents": {"bellows": {"model": "agy"}},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 1
    assert "BROKEN (cli.agy_present)" in out
    assert "agy" in out


def test_doctor_no_agy_reference_stays_silent(tmp_path, monkeypatch, capsys):
    """No config path routes to agy -> the check emits nothing and never FAILs,
    even when the binary is absent."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir()
    monkeypatch.setattr("shutil.which", lambda _name: None)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
            }
        )
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 0
    assert "agy CLI" not in out
    assert "routes to the agy" not in out


def test_doctor_fails_on_queued_undeclared_agent(tmp_path, monkeypatch, capsys):
    """The box-scout detector: a live bead whose agent cannot dispatch.

    Regression for 2026-07-22 / 07-29 / 08-04 (sommeliwhey). `box-scout` is
    externally executed and only reachable because config declares it. When
    `agentco init --company` rewrote .agentco/config.yaml with the scaffold
    placeholder, that declaration vanished and the next cycle failed 50 beads
    with "Unknown agent: box-scout", spawning 50 RCA beads. Doctor must catch
    the un-declaration from config, before any cycle touches the queue.
    """
    from agentco_harness.beads import Beads

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    Beads(tmp_path / "tasks.jsonl").create(
        title="box-scout: Darklab (darklab)",
        description="crawl",
        assigned_agent="box-scout",
    )

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "box-scout" in out
    assert "Unknown agent" in out


def test_doctor_accepts_declared_external_agent(tmp_path, monkeypatch, capsys):
    """Declaring the agent is what makes the bead legal — same queue, no FAIL."""
    from agentco_harness.beads import Beads

    monkeypatch.chdir(tmp_path)
    (tmp_path / "company").mkdir(exist_ok=True)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "agents": {"box-scout": {}},
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
            }
        )
    )
    Beads(tmp_path / "tasks.jsonl").create(
        title="box-scout: Darklab (darklab)",
        description="crawl",
        assigned_agent="box-scout",
    )

    code = run_doctor(str(cfg_file))
    out = capsys.readouterr().out

    assert code == 0
    assert "externally-executed: box-scout" in out


def test_doctor_ignores_terminal_beads_of_undeclared_agent(tmp_path, monkeypatch, capsys):
    """Only live beads can be claimed — a done/failed one is history, not a risk."""
    from agentco_harness.beads import Beads

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="old sweep", description="x", assigned_agent="box-scout")
    beads.fail(task.id, result="Unknown agent: box-scout")

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    # No BROKEN: the terminal bead is history. Exit 2 comes from the usage
    # telemetry gap this bead legitimately creates (it executed on an agent
    # route and metered nothing) — a DEGRADED, which is the point of the split.
    assert code == 2
    assert "BROKEN" not in out
    assert "queue holds bead(s)" not in out


def test_special_executors_cover_run_task_branches():
    """Dispatch and the dispatchability guard must agree on every name.

    A name that dispatch executes but the guard omits is read as externally-
    executed, so its beads are filtered out of every cycle and sit pending
    forever — the mirror image of the box-scout failure. This used to be
    kept in lockstep by hand, and this test read the dispatch source for
    `assigned_agent == "..."` branches to check. The executor backend seam
    replaced the branches with one registry that both sides read, so the
    hazard is gone rather than guarded: assert there are no literal branches
    left to drift, that dispatch resolves through the registry, and that the
    guard sees everything the registry holds.
    """
    import inspect
    import re

    from agentco_harness import backends
    from agentco_harness.orchestrator import SPECIAL_EXECUTORS, Orchestrator

    source = inspect.getsource(Orchestrator._execute_cycle_task)
    literal_branches = re.findall(r'task\.assigned_agent == "([\w-]+)"', source)
    assert not literal_branches, (
        f"_execute_cycle_task dispatches {literal_branches} by literal name again — "
        f"register them as backends instead, or the guard will drift"
    )
    assert "backends.resolve(" in source
    assert backends.executor_names() <= set(SPECIAL_EXECUTORS)
    assert {"planner", "claude", "zai", "forge"} <= set(SPECIAL_EXECUTORS)


def test_doctor_fails_on_pending_bead_with_no_agent(tmp_path, monkeypatch, capsys):
    """The mirror-image hole in check (r): a bead with no name to validate.

    Regression for 2026-08-04 (sommeliwhey). Check (r) skipped any bead whose
    assigned_agent was null, so it reported OK on two real box-scout work beads
    (ac-6c5f1123, ac-d4a18e4d) that the cycle mishandled every hour — and on the
    24 unassigned beads that had been counting as errors since 2026-07-24
    without recording one.
    """
    from agentco_harness.beads import Beads

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    Beads(tmp_path / "tasks.jsonl").create(
        title="box-scout: Nutts+ (nutts)",
        description="crawl",
    )

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 1
    assert "no assigned_agent" in out


def test_doctor_ignores_human_assigned_bead_with_no_agent(tmp_path, monkeypatch, capsys):
    """A human-owned bead legitimately carries no agent name — it waits, visible."""
    from agentco_harness.beads import Beads

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    Beads(tmp_path / "tasks.jsonl").create(
        title="Approve the LGPD LIA",
        description="d",
        assigned_to="human:mabidoli",
    )

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 0
    assert "no assigned_agent" not in out


def _tk_ledger(tmp_path, monkeypatch, note_exists: bool):
    """A transkriptor root whose ledger claims one written note."""
    root = tmp_path / "tk"
    (root).mkdir(parents=True)
    monkeypatch.setenv("TRANSKRIPTOR_ROOT", str(root))
    note = tmp_path / "vault" / "2026-08-17-meeting.md"
    if note_exists:
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# note\n")
    (root / "ledger.jsonl").write_text(
        json.dumps({"order_id": "OID1", "stage": "complete", "at": "2026-08-17T10:00:00+00:00"}) + "\n"
        + json.dumps({"order_id": "OID1", "stage": "noted", "at": "2026-08-17T11:00:00+00:00",
                      "path": str(note)}) + "\n"
        + json.dumps({"order_id": "OID1", "stage": "tasked", "at": "2026-08-17T12:00:00+00:00"}) + "\n"
    )


def _minimal_cfg(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "sources": {"logs": {"enabled": True}},
                "llm": {"default_provider": "lmstudio", "default_model": "local"},
            }
        )
    )
    (tmp_path / "company").mkdir(exist_ok=True)
    return cfg_file


# ------------------------------------------------------- usage telemetry (t)


def _write_usage(tmp_path, rows):
    from agentco_harness import usage

    path = usage.ledger_path(tmp_path / "tasks.jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _usage_row(**kw):
    row = {
        "schema": "agentco_harness.usage/1",
        "at": "2026-08-25T10:00:00+00:00",
        "bead_id": "ac-metered",
        "lane": "cycle",
        "node": "acme",
        "executor": "claude",
        "route": "anthropic",
        "model_used": "claude-sonnet-5",
        "exit_status": "ok",
        "duration_seconds": 12.0,
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.5,
    }
    row.update(kw)
    return row


def test_doctor_reports_usage_telemetry_instead_of_none_yet(tmp_path, monkeypatch, capsys):
    """The old line said 'no cost telemetry yet' forever. Now it reports."""
    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    _write_usage(tmp_path, [_usage_row()])

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    assert code == 0
    assert "usage telemetry: 1 metered run(s)" in out
    assert "120 tokens" in out
    assert "$0.5000" in out


def test_doctor_warns_when_no_usage_rows_exist(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = run_doctor(_healthy_cfg(tmp_path))
    out = capsys.readouterr().out

    # No agent-executed bead has run, so an empty ledger is INFO, not a gap.
    assert code == 0
    assert "INFO (usage.telemetry): usage telemetry: no rows in" in out
    assert "nothing to meter" in out


def test_doctor_detects_an_execution_that_produced_no_usage_row(tmp_path, monkeypatch, capsys):
    """THE gap: a bead that ran on an agent route and metered nothing."""
    from agentco_harness.beads import Beads, TaskStatus

    monkeypatch.chdir(tmp_path)
    cfg = _healthy_cfg(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    metered = beads.create("metered work", "d", assigned_agent="claude")
    unmetered = beads.create("unmetered work", "d", assigned_agent="claude")
    for task in (metered, unmetered):
        beads.update(task.id, status=TaskStatus.DONE)
    _write_usage(tmp_path, [_usage_row(bead_id=metered.id, at="2020-01-01T00:00:00+00:00")])

    code = run_doctor(cfg)
    out = capsys.readouterr().out

    # The gap degrades a spend decision; it never breaks the node.
    assert code == 2
    assert "DEGRADED (usage.telemetry): usage telemetry GAP" in out
    assert unmetered.id in out
    assert metered.id not in out.split("GAP")[1].split("\n")[0]


def test_doctor_names_the_paths_that_are_still_unmetered(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_doctor(_healthy_cfg(tmp_path))
    out = capsys.readouterr().out
    assert "known model-invoking path(s) are NOT metered" in out
    assert "transkriptor" in out
