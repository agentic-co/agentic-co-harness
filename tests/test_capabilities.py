"""Capability manifests + lane routing (bead ac-39d4dbc8).

A node declares what it *can do* (`capabilities:` in its config.yaml); a bead
declares what it *needs* (`requires:`). Claiming is the gate — not visibility.
A bead stays visible in `ready()` everywhere so the portfolio never develops
blind spots, but only a worker whose manifest covers the bead's requirements
may take the lease.

The invariant this protects is credential containment: the write-scoped ADO PAT
lives only on the MacBook, so a bead carrying `requires: [ado-write]` must be
physically unable to execute on the hub. Every gate here therefore fails
CLOSED — an undeclared, unparseable or absent manifest grants nothing.

Design of record: Plans/TwoMachineLifeos.md, "Node & manifests" + "Invariants".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    Beads,
    CapabilityError,
    LeaseError,
    Task,
    TaskStatus,
    normalize_capabilities,
)
from agentco_harness.children import ChildRef, ChildRegistry, verify_child
from agentco_harness.cli import main
from agentco_harness.config import Config
from agentco_harness.me import collect

WORKER = "frontsteps-worker"
HUB = "hub"


def _beads(tmp_path) -> Beads:
    return Beads(tmp_path / "tasks.jsonl")


# ------------------------------------------------------------ the requires field


def test_requires_round_trips_through_json(tmp_path):
    """A requirement that only survives in memory is a gate that vanishes at the
    SSH hop — exactly where it is load-bearing."""
    beads = _beads(tmp_path)
    task = beads.create(
        title="write the AC back", description="d", requires=["ado-write"]
    )

    assert beads.get(task.id).requires == ["ado-write"]
    assert Task.from_json(beads.get(task.id).to_json()).requires == ["ado-write"]

    raw = json.loads((tmp_path / "tasks.jsonl").read_text().strip().splitlines()[0])
    assert raw["requires"] == ["ado-write"]


def test_legacy_line_without_requires_parses_unrestricted(tmp_path):
    """Every bead written before manifests existed must stay claimable."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d")
    record = json.loads(beads.get(task.id).to_json())
    record.pop("requires")
    (tmp_path / "tasks.jsonl").write_text(json.dumps(record) + "\n")

    legacy = Beads(tmp_path / "tasks.jsonl").get(task.id)
    assert legacy.requires == []
    assert Beads(tmp_path / "tasks.jsonl").claim(task.id, WORKER) is not None


def test_requires_dedupes_and_preserves_order(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(
        title="t",
        description="d",
        requires=["frontsteps-code", "ado-write", "frontsteps-code"],
    )
    assert task.requires == ["frontsteps-code", "ado-write"]


def test_requires_refuses_a_bare_string(tmp_path):
    """`requires="ado-write"` would otherwise validate character-by-character and
    produce a bead requiring 'a', 'd', 'o'… — claimable by nobody, for a reason
    no one could read."""
    beads = _beads(tmp_path)
    with pytest.raises(ValueError, match="list"):
        beads.create(title="t", description="d", requires="ado-write")


def test_requires_refuses_a_miscased_token(tmp_path):
    """One canonical spelling, enforced where it is typed. 'ADO-Write' would
    otherwise be a requirement no manifest ever matches — a bead that silently
    never runs anywhere."""
    beads = _beads(tmp_path)
    with pytest.raises(ValueError, match="lowercase"):
        beads.create(title="t", description="d", requires=["ADO-Write"])


def test_requires_refuses_an_empty_token(tmp_path):
    beads = _beads(tmp_path)
    with pytest.raises(ValueError):
        beads.create(title="t", description="d", requires=["ado-write", "  "])


# ------------------------------------------------------------------ claim gating


def test_claim_refused_when_worker_lacks_a_required_capability(tmp_path):
    """The core invariant: the hub cannot take a bead that needs the write PAT."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", requires=["ado-write"])

    with pytest.raises(CapabilityError, match="ado-write"):
        beads.claim(task.id, HUB, capabilities=["venture-keys"])

    after = beads.get(task.id)
    assert after.status == TaskStatus.PENDING
    assert after.leased_by is None
    assert after.lease_attempt == 0  # the refusal wrote NOTHING


def test_claim_refused_when_claimant_declares_no_capabilities(tmp_path):
    """Fail closed: an absent manifest grants nothing, it does not skip the gate."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", requires=["ado-write"])

    with pytest.raises(CapabilityError):
        beads.claim(task.id, HUB)  # capabilities defaults to None
    with pytest.raises(CapabilityError):
        beads.claim(task.id, HUB, capabilities=[])


def test_claim_succeeds_on_a_superset(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", requires=["ado-write"])

    got = beads.claim(
        task.id, WORKER, capabilities=["ado-write", "frontsteps-code", "extra"]
    )
    assert got is not None
    assert got.leased_by == WORKER
    assert got.status == TaskStatus.IN_PROGRESS


def test_claim_succeeds_on_an_exact_match(tmp_path):
    beads = _beads(tmp_path)
    task = beads.create(
        title="t", description="d", requires=["ado-write", "frontsteps-code"]
    )
    assert (
        beads.claim(task.id, WORKER, capabilities=["frontsteps-code", "ado-write"])
        is not None
    )


def test_claim_names_every_missing_capability(tmp_path):
    """Naming only the first missing one turns one fix into three round trips."""
    beads = _beads(tmp_path)
    task = beads.create(
        title="t", description="d", requires=["ado-write", "frontsteps-code"]
    )
    with pytest.raises(CapabilityError) as exc:
        beads.claim(task.id, HUB, capabilities=["venture-keys"])
    message = str(exc.value)
    assert "ado-write" in message and "frontsteps-code" in message


def test_bead_with_no_requirements_is_claimable_by_anyone(tmp_path):
    """The default must be exactly today's behaviour — an unrestricted bead."""
    beads = _beads(tmp_path)
    for caps in (None, [], ["ado-write"]):
        task = beads.create(title="t", description="d")
        assert beads.claim(task.id, WORKER, capabilities=caps) is not None


def test_capability_refusal_propagates_where_a_lost_race_returns_none(tmp_path):
    """A lost CAS is the protocol working — a drain loop moves on (None). A
    capability miss is a MISROUTE: retrying can never fix it, so it must not
    wear the same costume as a race."""
    beads = _beads(tmp_path)

    raced = beads.create(title="raced", description="d")
    beads.claim(raced.id, "first-worker")
    assert beads.claim(raced.id, "second-worker") is None  # lost CAS → None

    gated = beads.create(title="gated", description="d", requires=["ado-write"])
    with pytest.raises(CapabilityError):
        beads.claim(gated.id, "second-worker")


def test_capability_error_is_a_lease_error(tmp_path):
    """Callers that already handle the lease family keep working."""
    assert issubclass(CapabilityError, LeaseError)


def test_capability_is_checked_before_the_lease_race(tmp_path):
    """Between a permanent problem and a transient one, report the permanent one:
    'you can never run this' outranks 'someone else has it right now'."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", requires=["ado-write"])
    beads.claim(task.id, WORKER, capabilities=["ado-write"])  # now leased + in progress

    with pytest.raises(CapabilityError):
        beads.claim(task.id, HUB, capabilities=[])


def test_ready_is_not_filtered_by_capability(tmp_path):
    """Visibility is NOT the gate. The hub must still see FrontSteps work in its
    portfolio — hiding it is how a lane silently stops being watched."""
    beads = _beads(tmp_path)
    task = beads.create(title="t", description="d", requires=["ado-write"])
    assert [t.id for t in beads.ready()] == [task.id]


# --------------------------------------------------------------- node manifests


def _config_with(tmp_path: Path, body: str) -> Config:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n" + body)
    return Config.load(cfg)


def test_node_capabilities_default_to_empty(tmp_path):
    assert _config_with(tmp_path, "").capabilities == []


def test_node_capabilities_load_from_config_yaml(tmp_path):
    config = _config_with(tmp_path, "capabilities: [ado-write, frontsteps-code]\n")
    assert config.capabilities == ["ado-write", "frontsteps-code"]


def test_capabilities_is_a_known_top_level_key(tmp_path, capsys):
    _config_with(tmp_path, "capabilities: [ado-write]\n")
    assert "unknown top-level key" not in capsys.readouterr().out


def test_malformed_capabilities_block_fails_closed(tmp_path, capsys):
    """`capabilities: ado-write` (a string, not a list) must grant NOTHING and say
    so. Coercing it would silently hand a node a lane it never declared."""
    config = _config_with(tmp_path, "capabilities: ado-write\n")
    assert config.capabilities == []
    out = capsys.readouterr().out
    assert "capabilities" in out and "WARNING" in out


def test_bad_capability_token_is_dropped_not_fatal(tmp_path, capsys):
    """A fat-fingered token must never take a daemon down — but dropping it
    silently would leave a node believing it holds a lane it does not."""
    config = _config_with(tmp_path, "capabilities: [ado-write, 'BAD Token']\n")
    assert config.capabilities == ["ado-write"]
    assert "WARNING" in capsys.readouterr().out


def test_capabilities_survive_a_save_load_round_trip(tmp_path):
    config = _config_with(tmp_path, "capabilities: [ado-write]\n")
    out = tmp_path / "saved.yaml"
    config.save(out)
    assert Config.load(out).capabilities == ["ado-write"]


def test_save_omits_capabilities_when_a_node_declares_none(tmp_path):
    """A node that never declared a lane stays byte-identical to today's output."""
    import yaml

    config = _config_with(tmp_path, "")
    out = tmp_path / "saved.yaml"
    config.save(out)
    assert "capabilities" not in yaml.safe_load(out.read_text())


# ------------------------------------------------------------------- child tags


def test_childref_carries_host_and_capabilities(tmp_path):
    ref = ChildRef(
        name="frontsteps",
        path="/Users/x/Portfolio/frontsteps",
        host="macbook-pro.local",
        capabilities=["ado-write", "frontsteps-code"],
    )
    reloaded = ChildRef.from_json(ref.to_json())
    assert reloaded.host == "macbook-pro.local"
    assert reloaded.capabilities == ["ado-write", "frontsteps-code"]
    assert reloaded.is_remote


def test_local_child_is_not_remote(tmp_path):
    assert not ChildRef(name="sommeli", path=str(tmp_path)).is_remote


def test_legacy_child_row_without_the_new_tags_still_parses(tmp_path):
    ref = ChildRef.from_json(
        json.dumps({"name": "m3bl", "path": "/tmp/m3bl", "expected_interval": "1h"})
    )
    assert ref.host is None and ref.capabilities == []


def test_remote_child_reports_remote_not_fail(tmp_path):
    """A node on the other machine has no heartbeat this machine can read. Calling
    that 'fail' is a permanent false alarm; calling it 'ok' launders an unknown."""
    ref = ChildRef(
        name="frontsteps",
        path="/not/mounted/here",
        host="macbook-pro.local",
        expected_interval="1h",
    )
    assert not ref.verifiable
    result = verify_child(ref)
    assert result["level"] == "remote"
    assert result["ok"] is True
    assert "mirror" in result["detail"]
    assert "macbook-pro.local" in result["detail"]


def test_remote_child_with_a_stale_local_heartbeat_is_still_remote(tmp_path):
    """`host` is authoritative: a leftover directory of the same name on this
    machine must not be mistaken for the real node."""
    (tmp_path / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": "2020-01-01T00:00:00+00:00"})
    )
    ref = ChildRef(name="frontsteps", path=str(tmp_path), host="macbook-pro.local")
    assert verify_child(ref)["level"] == "remote"


def test_registry_quarantines_a_malformed_capability_row(tmp_path, capsys):
    reg_path = tmp_path / "registry.jsonl"
    reg_path.write_text(
        json.dumps({"name": "bad", "path": "/tmp/x", "capabilities": "ado-write"}) + "\n"
    )
    registry = ChildRegistry(reg_path)
    assert registry.list() == []
    assert "quarantined" in capsys.readouterr().out


# ------------------------------------------------------- me / children degrade


def _local_node(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (root / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": datetime.now(timezone.utc).isoformat()})
    )
    return root / "config.yaml"


def _register(parent: Path, entry: dict) -> None:
    reg = parent / "children" / "registry.jsonl"
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "a") as f:
        f.write(json.dumps(entry) + "\n")


def test_me_does_not_crash_or_alarm_on_a_remote_child(tmp_path, capsys):
    """The MacBook node is the first child whose path this machine cannot open.
    `me` must degrade to 'status via mirror', not raise and not cry wolf."""
    parent = tmp_path / "parent"
    cfg = _local_node(parent)
    _register(
        parent,
        {
            "name": "frontsteps",
            "path": "/Volumes/never-mounted/frontsteps",
            "host": "macbook-pro.local",
            "expected_interval": "1h",
            "notify": False,
            "capabilities": ["ado-write"],
        },
    )

    items = collect(str(cfg))  # must not raise

    assert [i for i in items if i.kind == "stale_child"] == []
    assert "invisible to this list" not in capsys.readouterr().err


def test_me_still_alarms_on_a_local_child_whose_path_vanished(tmp_path):
    """Degrading for remote must not blunt the real signal: a LOCAL child whose
    directory is gone is still a failure someone has to fix."""
    parent = tmp_path / "parent"
    cfg = _local_node(parent)
    _register(
        parent,
        {
            "name": "sommeli",
            "path": str(tmp_path / "gone"),
            "expected_interval": "1h",
            "notify": False,
        },
    )

    stale = [i for i in collect(str(cfg)) if i.kind == "stale_child"]
    assert len(stale) == 1
    assert stale[0].company == "sommeli"


def test_status_counts_a_remote_child_as_remote_not_verified(tmp_path):
    """A remote node must not be laundered into the locally-verified count."""
    from agentco_harness.orchestrator import Orchestrator

    parent = tmp_path / "parent"
    _local_node(parent)
    _register(
        parent,
        {
            "name": "frontsteps",
            "path": "/Volumes/never-mounted/frontsteps",
            "host": "macbook-pro.local",
            "notify": False,
        },
    )

    config = Config.load(parent / "config.yaml")
    status = Orchestrator(config).status()

    assert status["children_remote"] == 1
    assert status["children_unverified"] == 0
    remote = [c for c in status["children"] if c["child"] == "frontsteps"][0]
    assert remote["level"] == "remote"


# ------------------------------------------------------------------------- CLI


def _node(tmp_path: Path, capabilities: str = "") -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n" + capabilities)
    return cfg


def test_tasks_create_requires_flag_is_repeatable(tmp_path):
    cfg = _node(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(cfg),
            "tasks", "create", "write it back",
            "--requires", "ado-write",
            "--requires", "frontsteps-code",
        ],
    )
    assert result.exit_code == 0, result.output
    task = Beads(tmp_path / "tasks.jsonl").list()[0]
    assert task.requires == ["ado-write", "frontsteps-code"]
    assert "ado-write" in result.output


def test_tasks_create_refuses_a_malformed_requirement(tmp_path):
    cfg = _node(tmp_path)
    result = CliRunner().invoke(
        main,
        ["--config", str(cfg), "tasks", "create", "t", "--requires", "ADO Write"],
    )
    assert result.exit_code == 1
    assert Beads(tmp_path / "tasks.jsonl").list() == []


def test_pull_claims_only_what_this_node_can_do(tmp_path):
    """The pull CLI is where a node's manifest meets the hub's queue."""
    cfg = _node(tmp_path, "capabilities: [ado-write]\n")
    beads = Beads(tmp_path / "tasks.jsonl")
    ok = beads.create("ado work", "d", assigned_agent=WORKER, requires=["ado-write"])
    nope = beads.create("gpu work", "d", assigned_agent=WORKER, requires=["gpu"])
    plain = beads.create("plain", "d", assigned_agent=WORKER)

    result = CliRunner().invoke(main, ["--config", str(cfg), "pull", "--agent", WORKER])
    assert result.exit_code == 0, result.output
    # STDOUT specifically, not the combined stream: `pull` is a machine
    # interface read over SSH, so the loud refusal must land on stderr and leave
    # stdout parseable. A refusal that bled into stdout would break every caller.
    payload = json.loads(result.stdout)

    claimed = {t["id"] for t in payload["claimed"]}
    assert claimed == {ok.id, plain.id}
    assert beads.get(nope.id).status == TaskStatus.PENDING


def test_pull_reports_what_it_refused_rather_than_skipping_silently(tmp_path):
    """A bead this worker can never take must be VISIBLE in the poll output —
    silently skipping it forever is the failure class this project exists to
    kill."""
    cfg = _node(tmp_path, "capabilities: [ado-write]\n")
    beads = Beads(tmp_path / "tasks.jsonl")
    nope = beads.create("gpu work", "d", assigned_agent=WORKER, requires=["gpu"])

    result = CliRunner().invoke(main, ["--config", str(cfg), "pull", "--agent", WORKER])
    payload = json.loads(result.stdout)

    assert payload["refused"] == [{"id": nope.id, "missing": ["gpu"]}]
    assert payload["count"] == 0
    # …and the bead stays PENDING and visible: it belongs to some other lane.
    assert beads.get(nope.id).status == TaskStatus.PENDING


def test_pull_on_a_node_with_no_manifest_takes_only_unrestricted_beads(tmp_path):
    cfg = _node(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    beads.create("ado work", "d", assigned_agent=WORKER, requires=["ado-write"])
    plain = beads.create("plain", "d", assigned_agent=WORKER)

    payload = json.loads(
        CliRunner().invoke(main, ["--config", str(cfg), "pull", "--agent", WORKER]).stdout
    )
    assert [t["id"] for t in payload["claimed"]] == [plain.id]


def test_pull_node_uses_the_registered_child_manifest_not_the_hubs(tmp_path):
    """The DEPLOYED shape: the MacBook's launchd job runs `pull` on the HUB over
    SSH. Reading the local (hub) manifest there would refuse exactly the beads
    the worker exists to take — the lane would be dead on arrival."""
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "config.yaml").write_text(
        "tasks_path: tasks.jsonl\ncapabilities: [venture-keys]\n"
    )
    _register(
        hub,
        {
            "name": "frontsteps",
            "path": "/Volumes/never-mounted/frontsteps",
            "host": "macbook-pro.local",
            "notify": False,
            "capabilities": ["ado-write"],
        },
    )
    beads = Beads(hub / "tasks.jsonl")
    ado = beads.create("ado work", "d", assigned_agent=WORKER, requires=["ado-write"])

    cfg = str(hub / "config.yaml")
    # Without --node the hub's own manifest applies and the bead is refused…
    local = json.loads(
        CliRunner().invoke(main, ["--config", cfg, "pull", "--agent", WORKER]).stdout
    )
    assert local["claimed"] == []
    assert local["refused"] == [{"id": ado.id, "missing": ["ado-write"]}]

    # …with --node the registered worker's manifest applies and it claims.
    remote = json.loads(
        CliRunner()
        .invoke(main, ["--config", cfg, "pull", "--agent", WORKER, "--node", "frontsteps"])
        .stdout
    )
    assert [t["id"] for t in remote["claimed"]] == [ado.id]
    assert remote["capabilities"] == ["ado-write"]


def test_pull_refuses_an_unknown_node_rather_than_defaulting(tmp_path):
    """A typo'd node name must not silently fall back to the hub's manifest —
    that would claim another lane's work under the wrong identity."""
    cfg = _node(tmp_path, "capabilities: [venture-keys]\n")
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("t", "d", assigned_agent=WORKER)

    result = CliRunner().invoke(
        main, ["--config", str(cfg), "pull", "--agent", WORKER, "--node", "frontsteps"]
    )
    assert result.exit_code == 1
    assert beads.get(task.id).status == TaskStatus.PENDING


# ------------------------------------------------------------- in-process lane


def test_orchestrator_blocks_a_bead_this_node_cannot_satisfy(tmp_path):
    """In-process agents claim with the HUB's manifest. A misrouted bead must
    reach a terminal, visible state — leaving it PENDING would re-select it
    every cycle forever, failing silently by repetition."""
    from agentco_harness.orchestrator import Orchestrator

    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.capabilities = ["venture-keys"]
    beads = Beads(config.tasks_path)
    task = beads.create("ado work", "d", assigned_agent="dev", requires=["ado-write"])

    orch = Orchestrator(config)
    assert orch._execute_task(beads.get(task.id)) is False

    after = beads.get(task.id)
    assert after.status == TaskStatus.BLOCKED
    assert "ado-write" in (after.result or "")
    assert after.lease_attempt == 0


def test_orchestrator_runs_a_bead_its_manifest_covers(tmp_path, monkeypatch):
    """The gate must not become a wall: a covered bead still claims normally."""
    from agentco_harness.orchestrator import Orchestrator

    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.capabilities = ["ado-write"]
    beads = Beads(config.tasks_path)
    task = beads.create("ado work", "d", assigned_agent="dev", requires=["ado-write"])

    orch = Orchestrator(config)
    # Stop at the claim — executing the agent needs an LM, and the claim is the
    # only thing under test here.
    # Patched on `agents`, not `orchestrator`: the dispatch reaches it through
    # `_lm.agents()`, which resolves the attribute off the module at call time
    # so that the DSPy layer stays optional. Same boundary, one hop out.
    monkeypatch.setattr(
        "agentco_harness.agents.get_agent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reached dispatch")),
    )
    with pytest.raises(RuntimeError, match="reached dispatch"):
        orch._execute_task(beads.get(task.id))

    after = beads.get(task.id)
    assert after.leased_by == "dev"
    assert after.status == TaskStatus.IN_PROGRESS
