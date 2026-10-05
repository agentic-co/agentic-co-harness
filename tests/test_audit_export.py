"""`agentic-co audit export` / `audit verify` — metadata-only, read-only, deny-by-default.

The load-bearing test is the canary: a node whose beads, ledgers and config carry
a unique string in every text-bearing field must produce a bundle (directory AND
tarball) in which that string appears nowhere. The second is the writer-field
test: a ledger writer that gains a field the policy table has not decided fails
here, so new fields are decided rather than leaked or silently dropped.
"""

from __future__ import annotations

import ast
import dataclasses
import gzip
import hashlib
import io
import json
import os
import socket
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import agentco_harness
from agentco_harness import audit, cost, schedules, usage
from agentco_harness.beads import Task, TaskStatus
from agentco_harness.cli import main
from agentco_harness.recurring import RecurringDef

CANARY = "QZXCANARY7731"
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 9, 1, tzinfo=timezone.utc)
NEW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _no_tools(name: str) -> dict:
    return {"present": False, "version": None}


@pytest.fixture(autouse=True)
def _stub_tool_probe(monkeypatch):
    """No subprocess to the host's claude/uv from the suite."""
    monkeypatch.setattr(audit, "probe_tool", _no_tools)


def _seed(node: Path) -> Path:
    node.mkdir(parents=True, exist_ok=True)
    (node / "config.yaml").write_text(
        "tasks_path: tasks.jsonl\n"
        "agents:\n  dev:\n    model: test-model\n"
        f"    description: {CANARY} config value\n"
    )
    tasks = node / "tasks.jsonl"
    leaky = {
        "task_class": "agent",
        "chat": [{"author": "human:alice", "text": f"{CANARY} chat"}],
        "error": f"{CANARY} error",
        "run": {"inputs": {"x": CANARY}, "bindings": {"implementer": "dev"}},
        "zz_unknown_key": CANARY,
        "verify": {"class": "deterministic", "check": f"echo {CANARY}", "cwd": str(node)},
        "verify_result": {
            "class": "deterministic", "passed": False, "exit_code": 1, "timed_out": False,
            "check": f"echo {CANARY}", "cwd": str(node), "output_tail": CANARY,
            "checked_at": OLD.isoformat(),
            "failed_stage": {"index": 0, "command": CANARY, "exit_code": 1,
                             "timed_out": False, "output_tail": CANARY},
        },
        "verify_rejection": {"approver": "alice", "rejected_at": OLD.isoformat(),
                             "reason": CANARY},
    }
    beads = [
        Task(id="ac-00000001", title=f"{CANARY} title", description=f"{CANARY} desc",
             status=TaskStatus.VERIFY_FAILED, assigned_agent="dev",
             source=str(node / "inbox" / "mail.eml"), source_id=f"<{CANARY}@mail>",
             result=f"{CANARY} model output", created_at=OLD.isoformat(),
             updated_at=OLD.isoformat(), metadata=leaky),
        Task(id="ac-00000002", title=f"{CANARY} two", description=CANARY,
             assigned_agent="zai", blocked_by=["ac-00000001"],
             created_at=NEW.isoformat(), updated_at=NEW.isoformat(),
             metadata={"dispatch_refusal": {"code": "egress_denied", "message": CANARY,
                                            "remediation": CANARY, "at": NEW.isoformat()}}),
    ]
    lines = [b.to_json() for b in beads]
    # A line the real store quarantines (unknown status) — counted, never exported.
    lines.append(json.dumps({"id": "ac-bad", "title": CANARY, "description": CANARY,
                             "status": "weird", "priority": 2}))
    tasks.write_text("\n".join(lines) + "\n")

    for at, tokens in ((OLD, 10), (NEW, None)):
        usage.record_usage(
            usage.Attribution(bead_id="ac-00000001", lane="cycle", tasks_path=str(tasks),
                              extra={"note": CANARY}),
            executor="claude", route="NATIVE", model_used="test-model",
            input_tokens=tokens, error=f"{CANARY} boom", now=at,
        )
    with open(node / "usage.jsonl", "a") as fh:
        fh.write('{"at": "' + CANARY + '", broken\n')

    cost.record_run(tasks, task_id="ac-00000001", agent="dev", now=OLD,
                    exec_result=SimpleNamespace(model_used="test-model", success=False,
                                                truncated=False, cost_usd=None,
                                                input_tokens=None, output_tokens=None,
                                                num_turns=None, duration_seconds=1.5))
    (node / "runs.jsonl").write_text(json.dumps({
        "at": OLD.isoformat(), "instance": "node", "spawned": 0, "executed": 0,
        "errors": 1, "open_after": 0,
        "tasks": [{"id": "ac-00000001", "title": CANARY, "agent": "dev",
                   "outcome": "failed", "error": CANARY}],
    }) + "\n")
    schedules.reserve(tasks, "daily", "2026-09-01", now=OLD, detail=CANARY)
    schedules.observe(tasks, "daily", "2026-09-01", produced=1,
                      bead_ids=["ac-00000001"], detail=CANARY, now=OLD)
    (node / "recurring.jsonl").write_text(RecurringDef(
        id="daily", title=CANARY, schedule={"every": "1d"}, agent="dev",
        payload={"description": CANARY}).to_json() + "\n")
    from agentco_harness.asop_store import AsopStore

    AsopStore(node / "asops.jsonl").create(
        {"title": f"{CANARY} sop", "purpose": CANARY,
         "roles": {"implementer": {"kind": "agent"}},
         "steps": [{"step": 1, "name": CANARY, "role": "implementer", "purpose": CANARY,
                    "definition_of_done": CANARY,
                    "gate": {"kind": "deterministic", "check": f"echo {CANARY}"}}]},
        author="alice", author_kind="human",
    )
    (node / "heartbeat.json").write_text(json.dumps({
        "instance": "node", "cycle_completed_at": OLD.isoformat(), "beads_open": 1,
        "beads_done_this_cycle": 0, "recurring_spawned_this_cycle": 0,
        "errors_this_cycle": 1, "version": "0.5.0"}))
    (node / ".agentco-heartbeat.json").write_text(json.dumps({"last_work_at": OLD.isoformat()}))
    return node


def _export(node: Path, out: Path, **kw) -> audit.ExportResult:
    return audit.export(audit.resolve_node(str(node)), out_dir=out, now=NOW,
                        tool_probe=_no_tools, **kw)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns, digest)
    return out


# --------------------------------------------------------------------------- #


def test_canary_appears_nowhere_in_the_bundle_or_tarball(tmp_path):
    node = _seed(tmp_path / "node")
    result = _export(node, tmp_path / "out")

    blobs = {p.as_posix(): p.read_bytes() for p in result.bundle_dir.rglob("*") if p.is_file()}
    raw = result.tarball.read_bytes()
    blobs["<tar stream>"] = gzip.decompress(raw)
    with tarfile.open(result.tarball, "r:gz") as tar:
        for member in tar.getmembers():
            assert CANARY not in member.name
            if member.isfile():
                blobs[f"<tar>{member.name}"] = tar.extractfile(member).read()
    assert len(blobs) > 10
    for name, data in blobs.items():
        assert CANARY.encode() not in data, f"canary leaked into {name}"
        for path in {str(node), str(node.resolve())}:
            assert path.encode() not in data, f"node path leaked into {name}"


def test_reduced_rows_keep_metadata_and_count_what_they_drop(tmp_path):
    node = _seed(tmp_path / "node")
    result = _export(node, tmp_path / "out")
    beads = {b["id"]: b for b in _rows(result.bundle_dir / "beads.jsonl")}

    assert list(beads) == ["ac-00000001", "ac-00000002"]  # sorted, quarantined line absent
    one = beads["ac-00000001"]
    assert one["status"] == "verify_failed" and one["assigned_agent"] == "dev"
    assert "title" not in one and "description" not in one and "result" not in one
    assert one["source"].startswith("sha256:")  # a path under the node: hash only
    assert one["metadata"]["verify_result"] == {
        "class": "deterministic", "passed": False, "exit_code": 1, "timed_out": False,
        "checked_at": OLD.isoformat(),
        "failed_stage": {"index": 0, "exit_code": 1, "timed_out": False},
    }
    assert one["metadata"]["verify"] == {"class": "deterministic"}
    assert beads["ac-00000002"]["blocked_by"] == ["ac-00000001"]

    dropped = result.manifest["redaction"]["beads.jsonl"]["dropped"]
    assert dropped["title"] == 2 and dropped["description"] == 2
    assert dropped["metadata.zz_unknown_key"] == 1  # unknown -> dropped and named
    assert dropped["metadata.verify_result.output_tail"] == 1

    refusals = _rows(result.bundle_dir / "refusals.jsonl")
    assert refusals == [{"at": NEW.isoformat(), "bead_id": "ac-00000002",
                         "code": "egress_denied", "kind": "dispatch_refusal"}]
    assert set(result.manifest["refusal_coverage"]) == {"dispatch_refusal", "egress",
                                                          "command_floor"}
    runs = _rows(result.bundle_dir / "ledgers" / "runs.jsonl")
    assert runs[0]["tasks"] == [{"agent": "dev", "id": "ac-00000001", "outcome": "failed"}]

    doctor = json.loads((result.bundle_dir / "doctor.json").read_text())
    assert doctor["status"] == "ok" and doctor["findings"]
    assert all(set(f) == {"class", "check"} for f in doctor["findings"])
    env = json.loads((result.bundle_dir / "environment.json").read_text())
    assert env["harness_version"] == agentco_harness.__version__
    assert env["config"]["sha256"] == hashlib.sha256((node / "config.yaml").read_bytes()).hexdigest()
    assert env["tools"] == {"claude": _no_tools("claude"), "uv": _no_tools("uv")}


def test_null_is_never_written_as_zero(tmp_path):
    node = _seed(tmp_path / "node")
    result = _export(node, tmp_path / "out")
    rows = _rows(result.bundle_dir / "ledgers" / "usage.jsonl")
    assert [r["input_tokens"] for r in rows] == [10, None]
    assert all(r["cost_usd"] is None for r in rows)
    assert _rows(result.bundle_dir / "ledgers" / "costs.jsonl")[0]["cost_usd"] is None
    src = result.manifest["sources"]["usage.jsonl"]
    assert src["excluded_by_since"] is None and src["since_unparseable_kept"] is None


def test_roundtrip_export_then_verify_via_cli(tmp_path):
    node = _seed(tmp_path / "node")
    out = tmp_path / "out"
    runner = CliRunner()
    res = runner.invoke(main, ["--config", str(node / "config.yaml"),
                               "audit", "export", "--out", str(out)])
    assert res.exit_code == 0, res.output
    bundles = [p for p in out.iterdir() if p.is_dir()]
    tarballs = list(out.glob("*.tar.gz"))
    assert len(bundles) == 1 and len(tarballs) == 1
    assert tarballs[0].name == bundles[0].name + ".tar.gz"
    manifest = json.loads((bundles[0] / "manifest.json").read_text())
    assert manifest["schema"] == "agentic-co-audit-v1" and manifest["tier"] == "metadata"
    assert {f["path"] for f in manifest["files"]} >= {
        "beads.jsonl", "refusals.jsonl", "doctor.json", "environment.json",
        "ledgers/usage.jsonl", "ledgers/runs.jsonl", "ledgers/costs.jsonl",
        "ledgers/schedules.jsonl", "ledgers/recurring.jsonl", "ledgers/asops.jsonl",
        "ledgers/heartbeat.json", "ledgers/node-state.json"}
    for target in (bundles[0], tarballs[0]):
        res = runner.invoke(main, ["audit", "verify", str(target)])
        assert res.exit_code == 0, res.output


def test_tampering_fails_verify(tmp_path):
    node = _seed(tmp_path / "node")
    result = _export(node, tmp_path / "out")
    assert audit.verify(result.bundle_dir) == (True, [])

    target = result.bundle_dir / "ledgers" / "usage.jsonl"
    target.write_bytes(target.read_bytes().replace(b'"cycle"', b'"cyclX"'))
    ok, problems = audit.verify(result.bundle_dir)
    assert not ok and any("usage.jsonl: sha256 mismatch" in p for p in problems)
    res = CliRunner().invoke(main, ["audit", "verify", str(result.bundle_dir)])
    assert res.exit_code == 1

    clean = _export(node, tmp_path / "out2")
    (clean.bundle_dir / "smuggled.txt").write_text("extra")
    ok, problems = audit.verify(clean.bundle_dir)
    assert not ok and any("not listed" in p for p in problems)

    # A tarball whose member was edited fails too.
    edited = tmp_path / "edited.tar.gz"
    with tarfile.open(clean.tarball, "r:gz") as src, tarfile.open(edited, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name.endswith("beads.jsonl"):
                data = data.replace(b"ac-00000002", b"ac-99999999")
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    assert audit.verify(edited)[0] is False


def test_corrupt_ledger_lines_are_counted_not_exported(tmp_path):
    node = _seed(tmp_path / "node")
    (node / "heartbeat.json").write_text("{not json " + CANARY)
    result = _export(node, tmp_path / "out")
    sources = result.manifest["sources"]
    assert sources["usage.jsonl"]["corrupt_lines"] == 1
    assert sources["usage.jsonl"]["exported"] == 2
    assert sources["tasks.jsonl"]["corrupt_lines"] == 1  # the store's own quarantine rule
    assert sources["heartbeat.json"]["corrupt_lines"] == 1
    assert result.manifest["redaction"]["ledgers/usage.jsonl"]["corrupt_lines"] == 1
    assert (result.bundle_dir / "ledgers" / "heartbeat.json").read_bytes() == b""
    assert audit.verify(result.bundle_dir)[0]


def test_since_filters_events_and_keeps_unreadable_timestamps(tmp_path):
    node = _seed(tmp_path / "node")
    with open(node / "usage.jsonl", "a") as fh:
        fh.write(json.dumps({"at": "not-a-date", "bead_id": "ac-00000002"}) + "\n")
    result = _export(node, tmp_path / "out", since=audit.parse_since("2026-10-01"))

    usage_rows = _rows(result.bundle_dir / "ledgers" / "usage.jsonl")
    assert [r["at"] for r in usage_rows] == [NEW.isoformat(), "not-a-date"]
    src = result.manifest["sources"]["usage.jsonl"]
    assert src["excluded_by_since"] == 1 and src["since_unparseable_kept"] == 1
    assert [b["id"] for b in _rows(result.bundle_dir / "beads.jsonl")] == ["ac-00000002"]
    assert _rows(result.bundle_dir / "ledgers" / "runs.jsonl") == []
    # State snapshots are exported whole.
    assert len(_rows(result.bundle_dir / "ledgers" / "recurring.jsonl")) == 1
    assert result.manifest["since"] == "2026-10-01T00:00:00+00:00"
    with pytest.raises(audit.AuditError):
        audit.parse_since("yesterday")


def test_empty_node_exports_and_verifies(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _export(empty, tmp_path / "out")
    assert audit.verify(result.bundle_dir) == (True, [])
    assert audit.verify(result.tarball) == (True, [])
    assert {f["path"] for f in result.manifest["files"]} == {"doctor.json", "environment.json"}
    for src in result.manifest["sources"].values():
        assert src["present"] is False and src["rows_read"] is None  # null, never 0
    assert list(empty.iterdir()) == []  # doctor's touch-if-missing landed in the shadow


def test_export_is_read_only_against_the_node(tmp_path):
    sparse = tmp_path / "sparse"  # no recurring.jsonl: doctor would create one
    sparse.mkdir()
    (sparse / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (sparse / "tasks.jsonl").write_text(Task(id="ac-1", title="t", description="d").to_json() + "\n")
    seeded = _seed(tmp_path / "node")
    for node in (sparse, seeded):
        before = _snapshot(node)
        _export(node, tmp_path / f"out-{node.name}")
        assert _snapshot(node) == before


def test_bundle_inside_the_node_is_refused(tmp_path):
    node = _seed(tmp_path / "node")
    with pytest.raises(audit.AuditError):
        _export(node, node / "bundles")


def test_output_is_deterministic(tmp_path):
    node = _seed(tmp_path / "node")
    a = _export(node, tmp_path / "a")
    b = _export(node, tmp_path / "b")
    assert a.tarball.read_bytes() == b.tarball.read_bytes()


def test_export_needs_no_network_and_no_hub(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("audit export attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    node = _seed(tmp_path / "node")
    assert audit.verify(_export(node, tmp_path / "out").bundle_dir)[0]


# --------------------------------------------------------------------------- #
# Every field a ledger writer emits must be DECIDED (kept or denied) in POLICY.
# --------------------------------------------------------------------------- #

_PKG = Path(agentco_harness.__file__).parent


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"writer {name} not found — update this test with the writer")


def _const_keys(d: ast.Dict) -> set[str]:
    return {k.value for k in d.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _keys(fn: ast.AST, var: str | None = None) -> set[str]:
    """Literal keys written into dict `var` in `fn` (every dict when var is None)."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if var is None:
            if isinstance(node, ast.Dict):
                out |= _const_keys(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == var and isinstance(node.value, ast.Dict):
                    out |= _const_keys(node.value)
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == var and isinstance(t.slice, ast.Constant)):
                    out.add(t.slice.value)
    return out


def _writer_fields(tmp_path: Path) -> dict[str, set[str]]:
    from asop import ASOP, Step
    from asop import gates

    tasks = tmp_path / "w" / "tasks.jsonl"
    tasks.parent.mkdir()
    row = usage.record_usage(
        usage.Attribution(bead_id="b", lane="l", tasks_path=str(tasks), extra={"k": 1}),
        executor="e", route="r", error="x")
    cost.record_run(tasks, task_id="b", agent="a", exec_result=SimpleNamespace())
    reserved = schedules.reserve(tasks, "s", "p", detail="d")
    observed = schedules.observe(tasks, "s", "p", detail="d")

    orch = ast.parse((_PKG / "orchestrator.py").read_text())
    beads_src = ast.parse((_PKG / "beads.py").read_text())
    node_state = {
        kw.arg
        for n in ast.walk(orch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_heartbeat"
        for kw in n.keywords if kw.arg
    }
    found = {
        "usage": set(row),
        "costs": set(cost.read_ledger(tasks)[0]),
        "schedules": set(reserved) | set(observed),
        "recurring": set(json.loads(RecurringDef(id="i", title="t", schedule={"every": "1d"}).to_json())),
        "beads": set(json.loads(Task(id="i", title="t", description="d").to_json())),
        "asops": {f.name for f in dataclasses.fields(ASOP)},
        "asops.step": {f.name for f in dataclasses.fields(Step)},
        "gate": set(gates.GATE_FIELDS),
        "heartbeat": _keys(_func(orch, "_write_cycle_heartbeat"), "payload")
        | _keys(_func(orch, "_outage_evidence")),
        "runs": _keys(_func(orch, "_log_run"), "record") | _keys(_func(orch, "cycle"), "summary"),
        "runs.task": _keys(_func(orch, "cycle"), "outcome"),
        "node_state": node_state,
        "dispatch_refusal": _keys(_func(beads_src, "refuse_dispatch")),
    }
    # The extraction must not be vacuous.
    assert {"cycle_completed_at", "last_outage_gap_s"} <= found["heartbeat"]
    assert {"tasks", "open_after"} <= found["runs"] and {"id", "outcome"} <= found["runs.task"]
    assert {"last_work_at", "last_observe_at"} <= found["node_state"]
    assert {"code", "message"} <= found["dispatch_refusal"]
    return found


def test_every_ledger_writer_field_is_decided(tmp_path):
    for shape, fields in _writer_fields(tmp_path).items():
        undecided = sorted(fields - set(audit.POLICY[shape]))
        assert not undecided, (
            f"{shape}: writer emits field(s) {undecided} that audit.POLICY neither keeps "
            f"nor denies — decide each one (with a justification) in agentco_harness/audit.py"
        )


def test_policy_table_is_well_formed():
    for shape, table in audit.POLICY.items():
        for name, (decision, why) in table.items():
            assert why.strip(), f"{shape}.{name} has no justification"
            assert decision in (audit.KEEP, audit.DENY) or (
                decision.startswith("nest:") and decision[5:] in audit.POLICY
            ), f"{shape}.{name}: bad decision {decision!r}"
    for spec in audit.LEDGERS:
        assert spec.shape in audit.POLICY
