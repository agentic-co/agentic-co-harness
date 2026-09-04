"""The Hub client — the runtime as an L2 participant of a coordination plane.

Decision 8 (ASOP.md §11, 2026-09-04): when a plane is configured, **the plane
owns the queue**. A run is filed on the plane with bindings; this runtime
pulls the step beads its bindings name, executes them with its own backends,
and reports and attests back. It never files a run on the plane unprompted
and never publishes anything a person did not ask for. The local ASOP store
stays the standalone path.

Three verbs, one shape each, all over signed HTTP (the plane's boring
option): `pull` claims one ready item the actor may run, with a fenced
lease; `report` returns a terminal outcome under that fence; `attest`
submits the proof-of-execution record for a gated item. A pulled item is
**mirrored** as a local bead carrying everything the plane sent — the pin,
the step text, the gate — plus where it came from, so the cycle executes
it exactly as it executes a local step, and the mirror is what reports.

Reporting happens twice over, on purpose. A completion hook reports at the
moment a mirrored bead lands DONE, so the plane learns "at that moment"
(§5.5). `sync` walks mirrored beads whose outcome has not been reported —
DONE from a route that bypassed the hook, FAILED, or a crash between the
two — and reports them. The hook is the fast path; the sweep is the honest
one, and both write the same receipt so neither reports twice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from asop import gates as _asop_gates

from .beads import Beads, Task, TaskStatus

ACTOR_HEADER = "x-agentco-actor"
TIMESTAMP_HEADER = "x-agentco-timestamp"
SIGNATURE_HEADER = "x-agentco-signature"

#: The provenance a mirrored bead carries. Its presence is what makes a bead
#: "the plane's": the hook and the sweep both key on it.
HUB_KEY = "hub"


def signing_string(method: str, path: str, timestamp: str, body: bytes) -> str:
    """Byte-for-byte the plane's `agentco.auth.signing_string`. A second
    hand-written copy is how a scheme drifts, so the test pins a vector."""
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{digest}"


def sign(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), signing_string(method, path, timestamp, body).encode(), hashlib.sha256).hexdigest()


class HubRefusal(Exception):
    """The plane refused, with its own code — surfaced, never swallowed."""

    def __init__(self, code: str, message: str, remediation: str = "", http_status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.remediation, self.http_status = code, message, remediation, http_status


class HubUnreachable(Exception):
    """No plane answered. Distinct from a refusal: nothing was decided."""


@dataclass
class HubClient:
    url: str
    actor: str
    secret: str
    timeout_s: int = 30

    # ------------------------------------------------------------ transport

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        timestamp = str(int(time.time()))
        headers = {
            ACTOR_HEADER: self.actor,
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign(self.secret, method, path, timestamp, body),
            "content-type": "application/json",
        }
        req = urllib.request.Request(self.url.rstrip("/") + path, data=body if payload is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise HubUnreachable(f"{method} {self.url}{path}: {e}") from e
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HubRefusal("bad_json", f"plane answered {status} with a non-JSON body", http_status=status)
        if status >= 400 or (isinstance(data, dict) and data.get("state") == "refused"):
            raise HubRefusal(
                str(data.get("code", f"http_{status}")), str(data.get("message", raw[:200])),
                str(data.get("remediation", "")), http_status=status,
            )
        return data

    # ------------------------------------------------------------ verbs

    def pull(self, *, capabilities: Optional[list[str]] = None, ttl_seconds: Optional[int] = None) -> dict:
        payload: dict = {}
        if capabilities is not None:
            payload["capabilities"] = list(capabilities)
        if ttl_seconds is not None:
            payload["ttlSeconds"] = int(ttl_seconds)
        return self._request("POST", "/work/pull", payload)

    def report(self, item_id: str, attempt: int, status: str, *, result: Optional[str] = None,
               attestation: Optional[dict] = None) -> dict:
        payload: dict = {"status": status, "attempt": int(attempt)}
        if result is not None:
            payload["result"] = result
        if attestation is not None:
            payload["attestation"] = attestation
        return self._request("POST", f"/work/{item_id}/report", payload)

    def attest(self, item_id: str, attestation: dict) -> dict:
        return self._request("POST", f"/work/{item_id}/attest", {"attestation": attestation})

    def probe(self) -> dict:
        """An authenticated read that costs the plane nothing — `harness hub status`."""
        return self._request("GET", "/sops")

    # ------------------------------------------------------------ mirroring

    @staticmethod
    def natural_key(item_id: str) -> str:
        return f"hub:{item_id}"

    def mirror(self, beads: Beads, leased: dict) -> Task:
        """A pulled item as a local bead the cycle can execute.

        Everything the plane sent rides along in metadata — `sop_ref`, the
        step copy, `verify` — so the gate runs here exactly as it would for
        a local step. `hub` records where it came from and the fence it was
        leased under. Idempotent on the plane's item id: pulling twice
        returns the same bead rather than filing a second one.
        """
        item = leased["item"]
        existing = beads.find_by_natural_key(self.natural_key(item["id"]))
        if existing is not None:
            return existing
        metadata = dict(item.get("metadata") or {})
        metadata[HUB_KEY] = {
            "url": self.url, "item_id": item["id"], "attempt": leased.get("attempt"),
            "actor": self.actor, "pulled_at": datetime.now(timezone.utc).isoformat(),
        }
        return beads.create(
            title=item.get("title") or item["id"],
            description=item.get("description") or "",
            assigned_agent=self.actor,
            metadata=metadata,
            natural_key=self.natural_key(item["id"]),
        )

    def pull_and_mirror(self, beads: Beads, *, capabilities: Optional[list[str]] = None,
                        ttl_seconds: Optional[int] = None, limit: int = 20) -> list[Task]:
        """Pull until the plane says empty, or `limit`. Never more than it can run."""
        out: list[Task] = []
        seen: set[str] = set()
        for _ in range(max(1, limit)):
            leased = self.pull(capabilities=capabilities, ttl_seconds=ttl_seconds)
            if leased.get("state") != "leased" or not leased.get("item"):
                break
            bead = self.mirror(beads, leased)
            if bead.id not in seen:            # a re-issued lease is the same bead
                seen.add(bead.id)
                out.append(bead)
        return out

    # ------------------------------------------------------------ reporting back

    def attestation_for(self, task: Task) -> Optional[dict]:
        """The contract's attestation record from the runtime's own gate run.

        Only a deterministic gate this process ran produces one: its
        `verify_result` is the check identity and exit status the plane
        verifies as a claim (§5.3). Judged and human gates are answered on
        the plane by the party the gate names, never by the executor.
        """
        spec = (task.metadata or {}).get("verify") or {}
        record = (task.metadata or {}).get("verify_result") or {}
        if not spec or not record:
            return None
        check = spec.get("checks") or spec.get("check")
        exit_status = 0 if record.get("passed") else record.get("exit_code")
        if exit_status is None:
            exit_status = 1
        return {
            "check": check,
            "exit_status": int(exit_status),
            # A fingerprint of where the check ran, as one string: the contract
            # asks for enough to identify the machine or runtime, so an
            # attestation nobody can reproduce on can at least be disputed.
            "environment": (
                f"host={socket.gethostname()} platform={platform.platform()} "
                f"python={platform.python_version()} cwd={record.get('cwd')} actor={self.actor}"
            ),
            "at": record.get("checked_at") or datetime.now(timezone.utc).isoformat(),
            "submitted_by": self.actor,
        }

    def report_back(self, beads: Beads, task: Task) -> Optional[dict]:
        """Report a mirrored bead's terminal outcome once. Returns the receipt."""
        hub = (task.metadata or {}).get(HUB_KEY)
        if not hub or hub.get("reported_at"):
            return None
        if task.status is TaskStatus.DONE:
            status = "done"
        elif task.status is TaskStatus.FAILED:
            status = "failed"
        else:
            return None
        attestation = self.attestation_for(task) if status == "done" else None
        receipt = self.report(hub["item_id"], int(hub.get("attempt") or 0), status,
                              result=task.result, attestation=attestation)
        stamped = dict(task.metadata or {})
        stamped[HUB_KEY] = {**hub, "reported_at": datetime.now(timezone.utc).isoformat(),
                            "reported_status": status, "plane_state": receipt.get("state")}
        beads.update(task.id, metadata=stamped, verify_gate=False)
        return receipt

    def sync(self, beads: Beads) -> list[dict]:
        """Report every mirrored bead with an unreported terminal outcome."""
        receipts = []
        for t in beads.list():
            hub = (t.metadata or {}).get(HUB_KEY)
            if hub and not hub.get("reported_at") and t.status in (TaskStatus.DONE, TaskStatus.FAILED):
                r = self.report_back(beads, t)
                if r is not None:
                    receipts.append({"bead": t.id, "item": hub["item_id"], "status": t.status.value, "receipt": r})
        return receipts


def client_from_config(config) -> Optional[HubClient]:
    """The configured client, or None when no plane is configured."""
    hub = getattr(config, "hub", None)
    if hub is None or not hub.url:
        return None
    secret = os.environ.get(hub.secret_env, "")
    if not secret:
        raise HubRefusal(
            "secret_required",
            f"hub.url is set but {hub.secret_env} is not in the environment",
            f"export {hub.secret_env}=<the actor's shared secret>",
        )
    return HubClient(url=hub.url, actor=hub.actor, secret=secret, timeout_s=hub.timeout_s)


def completion_hook(orch, task: Task) -> None:
    """Registered with the orchestrator: report a mirrored bead the moment it lands DONE."""
    if not (task.metadata or {}).get(HUB_KEY):
        return
    client = client_from_config(orch.config)
    if client is None:
        return
    try:
        client.report_back(orch.beads, task)
    except (HubRefusal, HubUnreachable) as e:
        # The sweep will retry; the bead is done here regardless (§9: a
        # plane is advisory and never blocks a harness).
        print(f"[hub] report for {task.id} deferred to sync: {e}")
