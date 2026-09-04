"""The runtime as a plane participant: signed pull, mirror, report, attest."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import Config
from agentco_harness.hub_client import (
    ACTOR_HEADER, HUB_KEY, SIGNATURE_HEADER, TIMESTAMP_HEADER,
    HubClient, HubRefusal, HubUnreachable, client_from_config, sign, signing_string,
)

SECRET = "s3cret"


class FakePlane(BaseHTTPRequestHandler):
    """Verifies signatures the way the plane does, serves one item, records reports."""
    items: list[dict] = []
    reports: list[tuple] = []
    attests: list[tuple] = []
    refuse_pull_with: dict | None = None

    def log_message(self, *a):  # quiet
        pass

    def _verify(self, method, body):
        actor = self.headers.get(ACTOR_HEADER); ts = self.headers.get(TIMESTAMP_HEADER)
        presented = self.headers.get(SIGNATURE_HEADER)
        expected = hmac.new(SECRET.encode(), f"{method}\n{self.path}\n{ts}\n{hashlib.sha256(body).hexdigest()}".encode(),
                            hashlib.sha256).hexdigest()
        return actor, hmac.compare_digest(expected, presented or "")

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        actor, ok = self._verify("GET", b"")
        if not ok:
            return self._send(401, {"state": "refused", "code": "unauthenticated", "message": "bad signature"})
        self._send(200, {"sops": []})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        actor, ok = self._verify("POST", body)
        if not ok:
            return self._send(401, {"state": "refused", "code": "unauthenticated", "message": "bad signature"})
        payload = json.loads(body or b"{}")
        if self.path == "/work/pull":
            if FakePlane.refuse_pull_with:
                return self._send(403, {"state": "refused", **FakePlane.refuse_pull_with})
            if FakePlane.items:
                item = FakePlane.items.pop(0)
                return self._send(200, {"state": "leased", "item": item, "attempt": item["lease_attempt"]})
            return self._send(200, {"state": "empty", "item": None})
        if self.path.endswith("/report"):
            FakePlane.reports.append((self.path.split("/")[2], actor, payload))
            return self._send(200, {"state": "reported", "status": payload["status"]})
        if self.path.endswith("/attest"):
            FakePlane.attests.append((self.path.split("/")[2], actor, payload))
            return self._send(200, {"state": "attested"})
        self._send(404, {"state": "refused", "code": "no_such_route", "message": self.path})


@pytest.fixture()
def plane():
    FakePlane.items, FakePlane.reports, FakePlane.attests, FakePlane.refuse_pull_with = [], [], [], None
    server = HTTPServer(("127.0.0.1", 0), FakePlane)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def plane_item(item_id="w-1", gate=None):
    """Shaped like the plane's WorkItem.to_json(): the gate is a TOP-LEVEL
    field (`verify`), and metadata carries the pin and the plan copy."""
    return {
        "id": item_id, "title": "3. implement", "description": "make it pass", "status": "in_progress",
        "lease_attempt": 2,
        "verify": gate or {"kind": "deterministic", "check": "true", "schema_version": 1},
        "metadata": {
            "sop_ref": {"asop_id": "feature-dev", "version": 3, "step": 3},
            "sop_plan": {"name": "implement", "role": "implementer", "purpose": "make it pass"},
        },
    }


# ----------------------------------------------------------------- signing

def test_signing_string_matches_the_planes_scheme_byte_for_byte():
    body = b'{"x":1}'
    assert signing_string("post", "/work/pull", "1700000000", body) == (
        "POST\n/work/pull\n1700000000\n" + hashlib.sha256(body).hexdigest())
    # a pinned vector: recompute independently, so a drift in either side shows
    expected = hmac.new(b"k", b"POST\n/p\n1\n" + hashlib.sha256(b"").hexdigest().encode(), hashlib.sha256).hexdigest()
    assert sign("k", "POST", "/p", "1", b"") == expected


def test_a_bad_secret_is_a_refusal_not_a_crash(plane):
    client = HubClient(url=plane, actor="claude", secret="wrong")
    with pytest.raises(HubRefusal) as e:
        client.probe()
    assert e.value.code == "unauthenticated" and e.value.http_status == 401


def test_no_plane_is_unreachable_not_refused():
    client = HubClient(url="http://127.0.0.1:9", actor="claude", secret=SECRET, timeout_s=1)
    with pytest.raises(HubUnreachable):
        client.probe()


# ----------------------------------------------------------------- pull + mirror

def test_pull_mirrors_the_item_with_its_pin_gate_and_provenance(plane, tmp_path):
    FakePlane.items = [plane_item()]
    beads = Beads(tmp_path / "tasks.jsonl")
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    mirrored = client.pull_and_mirror(beads)
    assert len(mirrored) == 1
    t = beads.get(mirrored[0].id)
    assert t.assigned_agent == "claude"
    assert t.metadata["sop_ref"] == {"asop_id": "feature-dev", "version": 3, "step": 3}
    assert t.metadata["verify"]["kind"] == "deterministic"          # the top-level gate was lifted into metadata
    assert t.metadata[HUB_KEY]["item_id"] == "w-1" and t.metadata[HUB_KEY]["attempt"] == 2
    assert t.status is TaskStatus.PENDING                          # the cycle executes it


def test_pulling_the_same_item_twice_mirrors_once(plane, tmp_path):
    """A plane re-issues a lease after expiry; the mirror is the same bead."""
    beads = Beads(tmp_path / "tasks.jsonl")
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    FakePlane.items = [plane_item()]
    a = client.pull_and_mirror(beads)
    FakePlane.items = [plane_item()]                               # same item id, new lease
    b = client.pull_and_mirror(beads)
    assert [t.id for t in a] == [t.id for t in b] and len(a) == 1
    assert len([t for t in beads.list() if t.metadata.get(HUB_KEY)]) == 1
    FakePlane.items = [plane_item(), plane_item()]                 # and within ONE pull
    c = client.pull_and_mirror(beads)
    assert [t.id for t in c] == [a[0].id]


def test_a_plane_refusal_on_pull_surfaces_its_code(plane, tmp_path):
    FakePlane.refuse_pull_with = {"code": "capability_mismatch", "message": "not for you", "remediation": "declare it"}
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    with pytest.raises(HubRefusal) as e:
        client.pull()
    assert e.value.code == "capability_mismatch" and e.value.remediation == "declare it"


# ----------------------------------------------------------------- report + attest

def test_completing_a_mirrored_bead_reports_done_with_an_attestation(plane, tmp_path):
    FakePlane.items = [plane_item()]
    beads = Beads(tmp_path / "tasks.jsonl")
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    t = client.pull_and_mirror(beads)[0]
    beads.claim(t.id, "claude"); done = beads.complete(t.id, result="implemented")
    assert done.status is TaskStatus.DONE                          # `true` passed the gate here
    receipt = client.report_back(beads, beads.get(t.id))
    assert receipt == {"state": "reported", "status": "done"}
    (item_id, actor, payload), = FakePlane.reports
    assert (item_id, actor, payload["status"], payload["attempt"], payload["result"]) == ("w-1", "claude", "done", 2, "implemented")
    att = payload["attestation"]
    assert att["check"] == "true" and att["exit_status"] == 0 and att["submitted_by"] == "claude"
    assert set(att) == {"check", "exit_status", "environment", "at", "submitted_by"}   # the contract's fields
    from asop.gates import validate_attestation
    validate_attestation(att, gate=beads.get(t.id).metadata["verify"], submitted_by="claude")  # the contract accepts it
    assert beads.get(t.id).metadata[HUB_KEY]["reported_status"] == "done"


def test_report_back_is_once_only(plane, tmp_path):
    FakePlane.items = [plane_item()]
    beads = Beads(tmp_path / "tasks.jsonl")
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    t = client.pull_and_mirror(beads)[0]
    beads.claim(t.id, "claude"); beads.complete(t.id, result="ok")
    assert client.report_back(beads, beads.get(t.id)) is not None
    assert client.report_back(beads, beads.get(t.id)) is None
    assert len(FakePlane.reports) == 1


def test_sync_reports_a_failed_mirror_the_hook_never_saw(plane, tmp_path):
    FakePlane.items = [plane_item("w-9")]
    beads = Beads(tmp_path / "tasks.jsonl")
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    t = client.pull_and_mirror(beads)[0]
    beads.claim(t.id, "claude"); beads.update(t.id, status=TaskStatus.FAILED, result="boom", verify_gate=False)
    receipts = client.sync(beads)
    assert len(receipts) == 1 and receipts[0]["status"] == "failed"
    (_, _, payload), = FakePlane.reports
    assert payload["status"] == "failed" and "attestation" not in payload
    assert client.sync(beads) == []                                 # nothing left unreported


def test_a_non_mirror_bead_is_never_reported(plane, tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    local = beads.create("local work", "d")
    beads.claim(local.id, "claude"); beads.complete(local.id, result="ok")
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    assert client.report_back(beads, beads.get(local.id)) is None
    assert client.sync(beads) == [] and FakePlane.reports == []


# ----------------------------------------------------------------- config + hook

def test_client_from_config_is_off_without_a_url_and_loud_without_a_secret(monkeypatch):
    config = Config()
    assert client_from_config(config) is None
    config.hub.url = "http://plane"
    monkeypatch.delenv("AGENTCO_HUB_SECRET", raising=False)
    with pytest.raises(HubRefusal) as e:
        client_from_config(config)
    assert e.value.code == "secret_required"
    monkeypatch.setenv("AGENTCO_HUB_SECRET", "x")
    assert client_from_config(config).actor == "harness"


def test_the_orchestrator_hook_reports_at_completion(plane, tmp_path, monkeypatch):
    from agentco_harness.orchestrator import Orchestrator
    FakePlane.items = [plane_item("w-5")]
    monkeypatch.setenv("AGENTCO_HUB_SECRET", SECRET)
    config = Config(); config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.hub.url = plane; config.hub.actor = "claude"
    beads = Beads(config.tasks_path)
    client = HubClient(url=plane, actor="claude", secret=SECRET)
    t = client.pull_and_mirror(beads)[0]
    orch = Orchestrator(config)
    orch.beads.claim(t.id, "claude"); orch.beads.complete(t.id, result="ok")
    orch._run_completion_hooks(orch.beads.get(t.id))
    assert [r[0] for r in FakePlane.reports] == ["w-5"]


def test_cli_status_pull_sync(plane, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from agentco_harness.cli import main
    FakePlane.items = [plane_item("w-7")]
    monkeypatch.setenv("AGENTCO_HUB_SECRET", SECRET)
    (tmp_path / "config.yaml").write_text(f"tasks_path: tasks.jsonl\nhub:\n  url: {plane}\n  actor: claude\n")
    cfg = str(tmp_path / "config.yaml"); runner = CliRunner()
    r = runner.invoke(main, ["-c", cfg, "hub", "status"]); assert r.exit_code == 0 and "OK" in r.output, r.output
    r = runner.invoke(main, ["-c", cfg, "hub", "pull"]); assert r.exit_code == 0 and "Mirrored 1" in r.output, r.output
    beads = Beads(tmp_path / "tasks.jsonl"); t = beads.list()[0]
    beads.claim(t.id, "claude"); beads.complete(t.id, result="ok")
    r = runner.invoke(main, ["-c", cfg, "hub", "sync"]); assert r.exit_code == 0 and "Reported 1" in r.output, r.output
