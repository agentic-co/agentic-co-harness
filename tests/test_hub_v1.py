"""Runtime 1.0 connected-correctness (docs/v1-scope.md A1-A8, C3)."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentco_harness import connections
from agentco_harness.beads import Beads, TaskReferenceError, TaskStatus
from agentco_harness.config import Config
from agentco_harness.hub_client import (
    HUB_KEY, HubClient, HubRefusal, client_from_config, hub_item_refusal, intake,
)

SECRET = "s3cret"


def make_plane(hub_id: str):
    """A plane with its own generated id, items, and a record of every call."""

    class Plane(BaseHTTPRequestHandler):
        items: list = []
        calls: list = []

        def log_message(self, *a):
            pass

        def _ok(self, method, body):
            ts = self.headers.get("x-agentco-timestamp")
            expected = hmac.new(SECRET.encode(), f"{method}\n{self.path}\n{ts}\n{hashlib.sha256(body).hexdigest()}".encode(),
                                hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, self.headers.get("x-agentco-signature") or "")

        def _send(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            if not self._ok("GET", b""):
                return self._send(401, {"state": "refused", "code": "unauthenticated", "message": "x"})
            Plane.calls.append(("GET", self.path, None))
            self._send(200, {"state": "accepted", "hubId": hub_id, "label": "t"} if self.path == "/hub" else {"sops": []})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            if not self._ok("POST", body):
                return self._send(401, {"state": "refused", "code": "unauthenticated", "message": "x"})
            payload = json.loads(body or b"{}")
            Plane.calls.append(("POST", self.path, payload))
            if self.path == "/work/pull":
                if Plane.items:
                    item = Plane.items.pop(0)
                    return self._send(200, {"state": "leased", "item": item, "attempt": item["lease_attempt"]})
                return self._send(200, {"state": "empty", "item": None})
            self._send(200, {"state": "accepted"})

    server = HTTPServer(("127.0.0.1", 0), Plane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return Plane, f"http://127.0.0.1:{server.server_port}", server


@pytest.fixture()
def two_planes():
    a, url_a, sa = make_plane("hub-aaa")
    b, url_b, sb = make_plane("hub-bbb")
    yield (a, url_a), (b, url_b)
    sa.shutdown(); sb.shutdown()


@pytest.fixture()
def plane():
    p, url, srv = make_plane("hub-aaa")
    yield p, url
    srv.shutdown()


def item(item_id="w-1", attempt=1, gate=None, metadata=None):
    return {"id": item_id, "title": "t", "description": "d", "status": "in_progress",
            "lease_attempt": attempt,
            "verify": gate or {"kind": "deterministic", "check": "true", "schema_version": 1},
            "metadata": metadata or {}}


def reports(p):
    return [c for c in p.calls if c[1].endswith("/report")]


# ---- A1 ------------------------------------------------------------------

def test_a1_nothing_local_can_depend_on_hub_work(plane, tmp_path):
    p, url = plane
    p.items = [item()]
    beads = Beads(tmp_path / "tasks.jsonl")
    mirrored = HubClient(url=url, actor="a", secret=SECRET).pull_and_mirror(beads)[0]
    with pytest.raises(TaskReferenceError):
        beads.create("local", "d", blocked_by=[mirrored.id])
    with pytest.raises(TaskReferenceError):
        beads.create("local", "d", parent_id=mirrored.id)
    other = beads.create("other", "d")
    with pytest.raises(TaskReferenceError):
        beads.update(other.id, blocked_by=[mirrored.id])


# ---- A2 ------------------------------------------------------------------

def test_a2_two_hubs_same_item_id_are_two_beads_and_reports_go_only_home(two_planes, tmp_path):
    (pa, ua), (pb, ub) = two_planes
    pa.items, pb.items = [item("w-1")], [item("w-1")]
    beads = Beads(tmp_path / "tasks.jsonl")
    ca = HubClient(url=ua, actor="a", secret=SECRET)
    cb = HubClient(url=ub, actor="a", secret=SECRET)
    ta = ca.pull_and_mirror(beads)[0]
    tb = cb.pull_and_mirror(beads)[0]
    assert ta.id != tb.id and ta.metadata[HUB_KEY]["hub_id"] == "hub-aaa"
    beads.claim(ta.id, "a"); beads.complete(ta.id, result="ok")
    with pytest.raises(HubRefusal) as e:                      # hub B must not receive hub A's outcome
        cb.report_back(beads, beads.get(ta.id))
    assert e.value.code == "hub_of_origin_mismatch" and reports(pb) == []
    assert ca.report_back(beads, beads.get(ta.id)) is not None and len(reports(pa)) == 1
    # the sweep records the refusal for the foreign bead and keeps going
    beads.claim(tb.id, "a"); beads.complete(tb.id, result="ok")
    out = ca.sync(beads)
    assert [r["receipt"]["code"] for r in out] == ["hub_of_origin_mismatch"] and reports(pb) == []


def test_a2_hook_refuses_when_origin_hub_is_no_longer_configured(plane, tmp_path, capsys):
    p, url = plane
    p.items = [item()]
    beads = Beads(tmp_path / "tasks.jsonl")
    t = HubClient(url=url, actor="a", secret=SECRET).pull_and_mirror(beads)[0]
    beads.claim(t.id, "a"); beads.complete(t.id, result="ok")
    from agentco_harness import hub_client

    class Orch:
        config = Config(); beads = None
    Orch.beads = beads
    hub_client.completion_hook(Orch, beads.get(t.id))
    assert "not sent" in capsys.readouterr().out and reports(p) == []


# ---- A3 ------------------------------------------------------------------

def test_a3_a_relase_takes_the_new_attempt_and_a_stale_finish_is_never_sent(plane, tmp_path):
    p, url = plane
    beads = Beads(tmp_path / "tasks.jsonl")
    c = HubClient(url=url, actor="a", secret=SECRET)
    p.items = [item(attempt=1)]
    t = c.pull_and_mirror(beads)[0]
    p.items = [item(attempt=2)]
    t2 = c.pull_and_mirror(beads)[0]
    assert t2.id == t.id and beads.get(t.id).metadata[HUB_KEY]["attempt"] == 2
    beads.claim(t.id, "a"); beads.complete(t.id, result="ok")
    c.report_back(beads, beads.get(t.id))
    assert reports(p)[0][2]["attempt"] == 2
    # finished under attempt 2, then re-leased as 3 before the report went out
    p.items = [item("w-2", attempt=2)]
    t3 = c.pull_and_mirror(beads)[0]
    beads.claim(t3.id, "a"); beads.complete(t3.id, result="ok")
    p.items = [item("w-2", attempt=3)]
    c.pull_and_mirror(beads)
    with pytest.raises(HubRefusal) as e:
        c.report_back(beads, beads.get(t3.id))
    assert e.value.code == "stale_attempt" and len(reports(p)) == 1


# ---- A4 ------------------------------------------------------------------

def test_a4_a_long_run_renews_the_lease(plane, tmp_path):
    p, url = plane
    p.items = [item(attempt=4)]
    beads = Beads(tmp_path / "tasks.jsonl")
    c = HubClient(url=url, actor="a", secret=SECRET)
    t = c.pull_and_mirror(beads)[0]
    with c.keep_lease(beads.get(t.id), ttl_seconds=3, interval_s=0.05):
        time.sleep(0.4)
    renewals = [x for x in p.calls if x[1] == "/work/w-1/renew"]
    assert len(renewals) >= 2 and renewals[0][2] == {"attempt": 4, "ttlSeconds": 3}
    n = len(p.calls)
    time.sleep(0.2)
    assert len(p.calls) == n                                   # stops when the run ends


# ---- A5 ------------------------------------------------------------------

def _valid(att, gate):
    from asop.gates import validate_attestation
    validate_attestation(att, gate=gate, submitted_by="a")


def test_a5_one_check_and_several_checks_are_valid_under_the_spec(plane, tmp_path):
    p, url = plane
    c = HubClient(url=url, actor="a", secret=SECRET)
    beads = Beads(tmp_path / "tasks.jsonl")
    gate = {"kind": "deterministic", "checks": ["true", "true"], "schema_version": 1}
    p.items = [item(gate=gate)]
    t = c.pull_and_mirror(beads)[0]
    beads.claim(t.id, "a"); done = beads.complete(t.id, result="ok")
    assert done.status is TaskStatus.DONE
    atts = c.attestations_for(beads.get(t.id))
    assert [a["stage"] for a in atts] == [0, 1]
    for a in atts:
        _valid(a, beads.get(t.id).metadata["verify"])
    c.report_back(beads, beads.get(t.id))
    assert reports(p)[0][2]["attestation"]["stage"] == 0
    assert [x[1] for x in p.calls if x[1].endswith("/attest")] == ["/work/w-1/attest"]


def test_a5_a_judged_gate_reports_done_without_an_attestation(plane, tmp_path):
    p, url = plane
    c = HubClient(url=url, actor="a", secret=SECRET)
    beads = Beads(tmp_path / "tasks.jsonl")
    p.items = [item(gate={"kind": "judged", "check": "review it", "schema_version": 1})]
    t = c.pull_and_mirror(beads)[0]
    beads.claim(t.id, "a"); parked = beads.complete(t.id, result="ok")
    assert parked.status is TaskStatus.AWAITING_VERIFY          # parks locally...
    c.report_back(beads, beads.get(t.id))                       # ...and the hub is told
    (_, _, payload), = reports(p)
    assert payload["status"] == "done" and "attestation" not in payload


# ---- A7 ------------------------------------------------------------------

class _Orch:
    def __init__(self, cfg, beads):
        self.config, self.beads, self.node_capabilities = cfg, beads, []


def _cfg(tmp_path, url, **hub):
    c = Config()
    c.tasks_path = str(tmp_path / "tasks.jsonl")
    c.hub.url, c.hub.actor = url, "a"
    for k, v in hub.items():
        setattr(c.hub, k, v)
    return c


def test_a7_intake_is_off_by_default_and_bounded_when_on(plane, tmp_path, monkeypatch):
    p, url = plane
    monkeypatch.setenv("AGENTCO_HUB_SECRET", SECRET)
    p.items = [item(f"w-{i}") for i in range(5)]
    beads = Beads(tmp_path / "tasks.jsonl")
    assert intake(_Orch(_cfg(tmp_path, url), beads)) == [] and len(p.items) == 5
    got = intake(_Orch(_cfg(tmp_path, url, intake=True, intake_max_open=2), beads))
    assert len(got) == 2 and len(p.items) == 3
    assert intake(_Orch(_cfg(tmp_path, url, intake=True, intake_max_open=2), beads)) == []   # full


# ---- A8 ------------------------------------------------------------------

def test_a8_workdir_must_sit_under_configured_roots(plane, tmp_path):
    p, url = plane
    root = tmp_path / "repos"; (root / "x").mkdir(parents=True)
    p.items = [item(gate={"kind": "deterministic", "check": "true", "cwd": str(root / "x"), "schema_version": 1}),
               item("w-2", gate={"kind": "deterministic", "check": "true", "cwd": "/etc", "schema_version": 1})]
    beads = Beads(tmp_path / "tasks.jsonl")
    good, bad = HubClient(url=url, actor="a", secret=SECRET).pull_and_mirror(beads)
    cfg = _cfg(tmp_path, url, workdir_roots=[str(root)])
    assert hub_item_refusal(cfg, beads.get(good.id)) is None
    assert hub_item_refusal(cfg, beads.get(bad.id))[0] == "workdir_outside_roots"
    assert hub_item_refusal(_cfg(tmp_path, url), beads.get(bad.id)) is None      # opt-in
    local = beads.create("local", "d", metadata={"workdir": "/etc"})
    assert hub_item_refusal(cfg, local) is None                                   # only the plane's items


# ---- C3 ------------------------------------------------------------------

def test_c3_the_hub_credential_comes_from_identity_not_the_environment(plane, tmp_path, monkeypatch):
    p, url = plane
    monkeypatch.setenv("AGENTCO_CONNECTIONS_DIR", str(tmp_path / "conns"))
    monkeypatch.setenv("AGENTCO_HUB_SECRET", "machine-wide-secret")
    from agentco_harness.config import IdentityConfig
    cfg = _cfg(tmp_path, url)
    cfg.identity = IdentityConfig(name="acme")
    with pytest.raises(HubRefusal) as e:                       # declared identity, no hub credential
        client_from_config(cfg)
    assert e.value.code == "identity_unavailable"
    connections.save(connections.Connection(name="acme-hub", kind="hub", base_url=url, actor="a"), SECRET)
    cfg.identity = IdentityConfig(name="acme", hub="acme-hub")
    assert client_from_config(cfg).secret == SECRET            # not the machine-wide one
    client_from_config(cfg).probe()                            # and the plane accepts it
    cfg.hub.actor = "someone-else"                             # bound to one actor
    with pytest.raises(HubRefusal) as e:
        client_from_config(cfg)
    assert e.value.code == "identity_hub_mismatch"
    cfg2 = _cfg(tmp_path, url)                                 # no identity: unchanged behaviour
    assert client_from_config(cfg2).secret == "machine-wide-secret"


def test_c3_hub_connection_validates_live(plane, tmp_path, monkeypatch):
    p, url = plane
    monkeypatch.setenv("AGENTCO_CONNECTIONS_DIR", str(tmp_path / "conns"))
    ok = connections.validate(connections.Connection(name="h", kind="hub", base_url=url, actor="a"), secret=SECRET)
    assert ok.ok
    bad = connections.validate(connections.Connection(name="h", kind="hub", base_url=url, actor="a"), secret="wrong")
    assert not bad.ok and "unauthenticated" in bad.detail
