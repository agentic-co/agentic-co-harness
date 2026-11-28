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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from asop import gates as _asop_gates

from .beads import Beads, Task, TaskStatus, gate_kind

ACTOR_HEADER = "x-agentco-actor"
TIMESTAMP_HEADER = "x-agentco-timestamp"
SIGNATURE_HEADER = "x-agentco-signature"

#: The provenance a mirrored bead carries. Its presence is what makes a bead
#: "the plane's": the hook and the sweep both key on it.
HUB_KEY = "hub"


def canonical_query(query: Optional[str]) -> str:
    """Byte-for-byte the plane's `agentco.auth.canonical_query`: parse, sort,
    re-encode, so a proxy reordering parameters cannot break a signature while
    the values stay bound. "" for no query."""
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query.lstrip("?"), keep_blank_values=True)
    return "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(pairs)
    )


def signing_string(method: str, path: str, timestamp: str, body: bytes,
                   query: Optional[str] = None) -> str:
    """Byte-for-byte the plane's `agentco.auth.signing_string`. A second
    hand-written copy is how a scheme drifts, so the test pins a vector. The
    query is its own line only when there is one, as on the plane."""
    digest = hashlib.sha256(body or b"").hexdigest()
    base = f"{method.upper()}\n{path}\n{timestamp}\n{digest}"
    canonical = canonical_query(query)
    return f"{base}\n{canonical}" if canonical else base


def sign(secret: str, method: str, path: str, timestamp: str, body: bytes,
         query: Optional[str] = None) -> str:
    return hmac.new(secret.encode(), signing_string(method, path, timestamp, body, query).encode(),
                    hashlib.sha256).hexdigest()


class HubRefusal(Exception):
    """The plane refused, with its own code — surfaced, never swallowed."""

    def __init__(self, code: str, message: str, remediation: str = "", http_status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.remediation, self.http_status = code, message, remediation, http_status


class HubUnreachable(Exception):
    """No plane answered. Distinct from a refusal: nothing was decided."""


class _NoSchemeDowngrade(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves the scheme the operator authorised.

    `config._require_authenticated_plane` checks `hub.url` at load. That check
    guards a STRING; the capability lives at the socket, and urllib follows
    redirects across schemes — its own handler permits http, https and ftp
    without comment. So an https plane answering 302 with an http Location
    gets this runtime to reconnect in plaintext, and the response is then
    parsed and its `verify` lifted onto a bead whose gate runs through a shell.
    The config check is bypassed at runtime by a header.

    Found by a second security review after the config check landed, and it is
    the same shape that review named elsewhere: the guard on the path somebody
    traced, the capability on the sibling nobody did.

    A redirect WITHIN https is left alone — that is ordinary, and the
    authentication the design assumes still holds.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlparse(req.full_url).scheme
        new = urllib.parse.urlparse(newurl).scheme
        if old == "https" and new != "https":
            raise urllib.error.HTTPError(
                newurl, code,
                f"plane redirected {old} -> {new or 'no scheme'}; refusing. "
                f"Responses from the plane are not authenticated, so a "
                f"downgraded connection is one an on-path attacker can answer.",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class HubClient:
    url: str
    actor: str
    secret: str
    timeout_s: int = 30
    #: The LOCAL backend a mirrored bead is stamped for. `actor` is who this
    #: node is ON THE PLANE; `executor` is what runs the work HERE. They are
    #: different names for a reason — see `HubConfig.executor`.
    executor: str | None = None
    _origin: Optional[str] = field(default=None, init=False, repr=False, compare=False)

    # ------------------------------------------------------------ transport

    def _request(self, method: str, path: str, payload: Optional[dict] = None,
                 query: Optional[str] = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        timestamp = str(int(time.time()))
        headers = {
            ACTOR_HEADER: self.actor,
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign(self.secret, method, path, timestamp, body, query),
            "content-type": "application/json",
        }
        target = self.url.rstrip("/") + path + (f"?{query}" if query else "")
        req = urllib.request.Request(target, data=body if payload is not None else None,
                                     method=method, headers=headers)
        try:
            opener = urllib.request.build_opener(_NoSchemeDowngrade)
            with opener.open(req, timeout=self.timeout_s) as resp:
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

    def renew(self, item_id: str, attempt: int, ttl_seconds: Optional[int] = None) -> dict:
        """Extend the lease this node holds, fenced on `attempt`. The heartbeat of
        a long step; never moves the fence (hub `POST /work/{id}/renew`)."""
        payload: dict = {"attempt": int(attempt)}
        if ttl_seconds is not None:
            payload["ttlSeconds"] = int(ttl_seconds)
        return self._request("POST", f"/work/{item_id}/renew", payload)

    def origin_id(self) -> str:
        """The id this hub generated for itself (`GET /hub`), cached.

        Item ids are only unique within one hub, so the id of the HUB is what
        makes `(origin, item)` a key. A plane too old to answer `/hub` falls
        back to its url, which is stable for as long as the config is.
        """
        if self._origin is None:
            try:
                uid = str(self._request("GET", "/hub").get("hubId") or "")
            except HubRefusal as e:
                if e.http_status not in (404, 405):
                    raise
                uid = ""
            self._origin = uid or f"url:{self.url.rstrip('/')}"
        return self._origin

    def owns(self, hub_meta: dict) -> bool:
        """Is this the hub the bead was pulled from? Beads pulled before origin
        ids were recorded are matched by url."""
        recorded = (hub_meta or {}).get("hub_id")
        if recorded:
            return recorded == self.origin_id()
        return str((hub_meta or {}).get("url", "")).rstrip("/") == self.url.rstrip("/")

    def attest(self, item_id: str, attestation: dict) -> dict:
        return self._request("POST", f"/work/{item_id}/attest", {"attestation": attestation})

    def probe(self) -> dict:
        """An authenticated read that costs the plane nothing — `harness hub status`."""
        return self._request("GET", "/sops")

    # ------------------------------------------------------------ mirroring

    def natural_key(self, item_id: str) -> str:
        """`hub:<the hub's generated id>:<item id>` — two hubs can both have a w-1."""
        return f"hub:{self.origin_id()}:{item_id}"

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
            return self._refresh_fence(beads, existing, leased)
        metadata = dict(item.get("metadata") or {})
        # Where the work happens. The gate declares the directory it is proved
        # in, so that is the directory the work is done in — an executor left
        # in the node's own runtime dir would be writing beside the bead store
        # instead of in the repository the run named. Only mirrored beads get
        # this: a local bead's cwd is unchanged unless it sets `workdir` itself.
        workdir = (item.get("verify") or metadata.get("verify") or {}).get("cwd")
        if workdir and "workdir" not in metadata:
            metadata["workdir"] = workdir
        # The plane keeps the gate as a top-level field of the work item, not
        # in metadata; this runtime keeps it in metadata.verify. Lift it, or
        # the mirror executes ungated and the plane refuses the report for
        # want of an attestation — found by the first end-to-end run.
        if item.get("verify") and "verify" not in metadata:
            metadata["verify"] = dict(item["verify"])
        metadata[HUB_KEY] = {
            "url": self.url, "hub_id": self.origin_id(), "item_id": item["id"],
            "attempt": leased.get("attempt"),
            "actor": self.actor, "pulled_at": datetime.now(timezone.utc).isoformat(),
        }
        return beads.create(
            title=item.get("title") or item["id"],
            description=item.get("description") or self.brief(metadata),
            # The LOCAL executor, not the plane actor. A bead stamped with the
            # actor is undispatchable by construction (§7: the actor is an
            # identity, not a runner); the actor is preserved in metadata.hub.
            assigned_agent=self.executor or self.actor,
            metadata=metadata,
            natural_key=self.natural_key(item["id"]),
        )

    def _refresh_fence(self, beads: Beads, existing: Task, leased: dict) -> Task:
        """A re-lease of an item already mirrored: take the NEW attempt.

        The plane re-issues a lease after expiry under a higher attempt, and
        only a report under that attempt is accepted. Keeping the old one sent
        every later report under a fence the plane had already moved past. A
        bead that already finished under the old attempt did its work for a
        lease the plane no longer honours, so it is marked `stale_attempt` and
        its outcome is refused at report time rather than sent under a fence it
        did not run under.
        """
        hub = dict((existing.metadata or {}).get(HUB_KEY) or {})
        fresh = leased.get("attempt")
        if fresh is None or hub.get("attempt") == fresh:
            return existing
        update = {**hub, "attempt": fresh, "previous_attempt": hub.get("attempt"),
                  "leased_at": datetime.now(timezone.utc).isoformat()}
        if existing.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.AWAITING_VERIFY) \
                and not hub.get("reported_at"):
            update["stale_attempt"] = True
        beads.annotate(existing.id, {HUB_KEY: update})
        return beads.get(existing.id) or existing

    @staticmethod
    def brief(metadata: dict) -> str:
        """The step's own words, as the brief its executor is handed.

        The plane copies the procedure's text onto every step it files
        (`sop_plan`, ASOP.md §5.2) and states the gate that will judge it.
        A mirror that dropped them handed the executor a bare step name —
        "write-tests", no purpose, no definition of done, no repository —
        which is not a brief anyone could act on. Rendering them here is what
        makes a pulled step executable by a model rather than only by a script
        that already knew the answer.
        """
        plan = dict(metadata.get("sop_plan") or {})
        if not plan:
            return ""
        lines: list[str] = []
        if plan.get("title") and plan.get("name"):
            role = f" (role: {plan['role']})" if plan.get("role") else ""
            lines.append(f"Procedure: {plan['title']} — step '{plan['name']}'{role}.")
        for key, label in (("purpose", "Purpose"), ("entry_check", "Entry check"),
                           ("inputs", "Inputs"), ("definition_of_done", "Definition of done"),
                           ("validation", "Validation"), ("write_back", "Write back")):
            value = plan.get(key)
            if value:
                lines.append(f"{label}: {value}")
        for mistake in plan.get("common_mistakes") or []:
            lines.append(f"Common mistake to avoid: {mistake}")
        gate = metadata.get("verify") or {}
        check = gate.get("check") or (gate.get("checks") or [None])[0]
        if check:
            lines.append(f"The gate that decides this step is {gate.get('kind', 'deterministic')}: {check}")
        if metadata.get("workdir"):
            lines.append(f"Work in {metadata['workdir']} — that is where the gate runs.")
        return "\n".join(lines)

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

    def attestations_for(self, task: Task) -> list[dict]:
        """The contract's attestation records from the runtime's own gate run.

        Only a deterministic gate this process ran produces any: its
        `verify_result` is the check identity and exit status the plane
        verifies as a claim (§5.3). Judged and human gates are answered on the
        plane by the party the gate names, never by the executor, and a plane
        refuses an attestation offered with their report — so they yield none.

        A staged gate (`checks`) takes ONE record per rung, each carrying its
        own command and `stage` index (`asop.gates.validate_attestation`); the
        old single record with the whole list as `check` was invalid. A run
        that stopped at a failing rung attests the rungs it ran, no more.
        """
        spec = (task.metadata or {}).get("verify") or {}
        record = (task.metadata or {}).get("verify_result") or {}
        if not spec or not record or (gate_kind(spec) or "deterministic") != "deterministic":
            return []
        environment = (
            f"host={socket.gethostname()} platform={platform.platform()} "
            f"python={platform.python_version()} cwd={record.get('cwd')} actor={self.actor}"
        )
        at = record.get("checked_at") or datetime.now(timezone.utc).isoformat()

        def one(check: str, exit_status, stage: Optional[int]) -> dict:
            att = {
                "check": check,
                "exit_status": int(1 if exit_status is None else exit_status),
                # A fingerprint of where the check ran, as one string: the
                # contract asks for enough to identify the machine or runtime.
                "environment": environment,
                "at": at,
                "submitted_by": self.actor,
            }
            if stage is not None:
                att["stage"] = stage
            return att

        staged = spec.get("checks")
        if not staged:
            return [one(spec.get("check"), 0 if record.get("passed") else record.get("exit_code"), None)]
        ran = int(record.get("stages_run") or len(staged))
        failed = (record.get("failed_stage") or {}).get("index")
        out = []
        for i in range(min(ran, len(staged))):
            code = (record.get("failed_stage") or {}).get("exit_code") if i == failed else 0
            out.append(one(staged[i], code, i))
        return out

    def attestation_for(self, task: Task) -> Optional[dict]:
        """The first attestation record (the whole record for a single check)."""
        found = self.attestations_for(task)
        return found[0] if found else None

    def report_back(self, beads: Beads, task: Task) -> Optional[dict]:
        """Report a mirrored bead's terminal outcome once. Returns the receipt.

        Refused, with a code and never sent anywhere else, when this client is
        not the hub the item came from, or when the bead ran under a lease the
        plane has since re-issued.
        """
        hub = (task.metadata or {}).get(HUB_KEY)
        if not hub or hub.get("reported_at"):
            return None
        spec = (task.metadata or {}).get("verify") or {}
        parked = (task.status is TaskStatus.AWAITING_VERIFY
                  and gate_kind(spec) in ("judged", "human"))
        if task.status is TaskStatus.DONE or parked:
            # A judged or human gate parks here, and "done" is what it reports:
            # the plane derives AWAITING_VERIFY itself (hub ADR 0012 §3) and
            # refuses an attestation from the executor on those gates.
            status = "done"
        elif task.status is TaskStatus.FAILED:
            status = "failed"
        else:
            return None
        if not self.owns(hub):
            raise HubRefusal(
                "hub_of_origin_mismatch",
                f"{task.id} was pulled from {hub.get('url')!r}, which is not the hub this "
                f"node is configured for ({self.url!r}); the outcome is not sent",
                "Restore the original hub in config to report it, or leave the item to its lease expiry.",
            )
        if hub.get("stale_attempt"):
            raise HubRefusal(
                "stale_attempt",
                f"{task.id} finished under attempt {hub.get('previous_attempt')}, but the hub "
                f"has since re-leased the item as attempt {hub.get('attempt')}; the outcome is not sent",
                "The new lease needs its own run.",
            )
        attestations = self.attestations_for(task) if status == "done" else []
        receipt = self.report(hub["item_id"], int(hub.get("attempt") or 0), status,
                              result=task.result,
                              attestation=attestations[0] if attestations else None)
        for extra in attestations[1:]:
            self.attest(hub["item_id"], extra)      # the remaining rungs of a staged gate
        # `annotate`, not `update(verify_gate=False)`. This only ever wrote
        # metadata, and the gate only fires on a DONE transition — so the
        # bypass flag here never suppressed anything. It read like authority
        # and was noise, which is the worse of the two.
        beads.annotate(task.id, {HUB_KEY: {
            **hub,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "reported_status": status,
            "plane_state": receipt.get("state"),
        }})
        return receipt

    def sync(self, beads: Beads) -> list[dict]:
        """Report every mirrored bead with an unreported terminal outcome.

        A refusal on one bead (wrong origin, stale attempt) is recorded as that
        bead's receipt and the sweep goes on; it must not hide the others.
        """
        receipts = []
        for t in beads.list():
            hub = (t.metadata or {}).get(HUB_KEY)
            if not hub or hub.get("reported_at") or t.status not in _REPORTABLE:
                continue
            try:
                r = self.report_back(beads, t)
            except HubRefusal as e:
                r = {"state": "refused", "code": e.code, "message": e.message}
            if r is not None:
                receipts.append({"bead": t.id, "item": hub["item_id"], "status": t.status.value, "receipt": r})
        return receipts

    # ------------------------------------------------------------ lease

    @contextmanager
    def keep_lease(self, task: Task, ttl_seconds: int, *, interval_s: Optional[float] = None):
        """Renew the lease on a mirrored bead for as long as the block runs.

        A run longer than the lease TTL otherwise loses the item to another
        worker mid-run. Renews every third of the TTL; a failed renewal is
        recorded and does not interrupt the run (the plane's fence decides what
        the final report is worth).
        """
        hub = (task.metadata or {}).get(HUB_KEY) or {}
        if not hub or not hub.get("item_id") or hub.get("attempt") is None or not self.owns(hub):
            yield
            return
        stop = threading.Event()
        every = interval_s if interval_s is not None else max(1.0, ttl_seconds / 3)

        def beat():
            while not stop.wait(every):
                try:
                    self.renew(hub["item_id"], int(hub["attempt"]), ttl_seconds)
                except (HubRefusal, HubUnreachable) as e:
                    print(f"[hub] lease renewal for {task.id} failed: {e}")

        thread = threading.Thread(target=beat, name=f"hub-lease-{task.id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)


_REPORTABLE = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.AWAITING_VERIFY)


# ---------------------------------------------------------------- trust (A8)

def hub_item_refusal(config, task: Task) -> Optional[tuple[str, str, str]]:
    """What the plane sent, checked against what this node allows.

    Returns `(code, message, remediation)` or None. Run before the executor
    starts: the plane's metadata sets the working directory, and a node that
    never said which directories the plane may name should not discover that
    from a stray `workdir: /`.

    `hub.workdir_roots` lists the directories a pulled item may run under.
    Unset means unrestricted, so a node keeps working until it opts in (doctor
    says so). Checks are not restricted here: ADR 0014's check library does not
    exist yet, so there is nothing to name them against (docs/v1-scope.md, A8).
    """
    if not (task.metadata or {}).get(HUB_KEY):
        return None
    roots = list(getattr(getattr(config, "hub", None), "workdir_roots", None) or [])
    raw = (task.metadata or {}).get("workdir")
    if not roots or not raw:
        return None
    from pathlib import Path
    try:
        workdir = Path(str(raw)).expanduser().resolve()
        resolved = [Path(r).expanduser().resolve() for r in roots]
    except OSError as e:
        return ("workdir_unresolvable", f"workdir {raw!r} could not be resolved: {e}",
                "Point workdir at a real directory.")
    if not any(workdir == r or r in workdir.parents for r in resolved):
        return ("workdir_outside_roots",
                f"the plane asked for workdir {workdir}, which is not under any of hub.workdir_roots",
                "Add the directory to hub.workdir_roots if this node should run work there.")
    return None


# ---------------------------------------------------------------- intake (A7)

def intake(orch) -> list[Task]:
    """Pull up to the node's free capacity at the start of a cycle.

    Off unless `hub.intake` is true. Capacity is `hub.intake_max_open` minus the
    hub items this node already holds unfinished, so a slow node does not
    accumulate leases it cannot renew. A plane that is down or refuses is
    logged and the cycle goes on: a plane is advisory (§9).
    """
    cfg = getattr(orch.config, "hub", None)
    if cfg is None or not cfg.url or not getattr(cfg, "intake", False):
        return []
    try:
        client = client_from_config(orch.config)
    except HubRefusal as e:
        print(f"[hub] intake skipped: {e}")
        return []
    if client is None:
        return []
    held = sum(1 for t in orch.beads.list()
               if (t.metadata or {}).get(HUB_KEY) and not (t.metadata or {}).get(HUB_KEY, {}).get("reported_at")
               and t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS))
    free = int(getattr(cfg, "intake_max_open", 3)) - held
    if free <= 0:
        return []
    try:
        return client.pull_and_mirror(orch.beads, capabilities=orch.node_capabilities or None,
                                      ttl_seconds=cfg.lease_ttl_s, limit=free)
    except (HubRefusal, HubUnreachable) as e:
        print(f"[hub] intake failed: {e}")
        return []


def _identity_secret(config, hub) -> str:
    """The hub credential from the instance's identity, never from the environment.

    A node that declares `identity:` takes every credential from the connections
    it names; the machine-wide env var is exactly the thing two instances on one
    box would share. The connection is bound to one plane and one actor, so it
    cannot be pointed at another.
    """
    from . import connections

    name = config.identity.hub
    if not name:
        raise HubRefusal(
            "identity_unavailable",
            "hub.url is set and `identity:` is declared, but identity.hub is not — the hub "
            "credential would fall back to a machine-wide environment variable",
            "Create a hub connection (`agentic-co connect add hub --name ... --base-url ... "
            "--actor ... --secret-stdin`) and set identity.hub to its name.",
        )
    try:
        conn = connections.load(name)
        if conn.kind != "hub":
            raise connections.ConnectionError_(f"{name!r} is {conn.kind}, expected hub")
        secret = connections.read_secret(conn)
    except connections.ConnectionError_ as e:
        raise HubRefusal("identity_unavailable", f"identity.hub: {e}", "") from None
    if conn.base_url.rstrip("/") != str(hub.url).rstrip("/") or conn.actor != hub.actor:
        raise HubRefusal(
            "identity_hub_mismatch",
            f"hub connection {name!r} is for {conn.base_url} as {conn.actor!r}, but this "
            f"instance's hub is {hub.url} as {hub.actor!r}",
            "Point identity.hub at the connection made for this hub and actor.",
        )
    if not secret:
        raise HubRefusal("identity_unavailable", f"hub connection {name!r} has no secret", "")
    return secret


def client_from_config(config) -> Optional[HubClient]:
    """The configured client, or None when no plane is configured."""
    hub = getattr(config, "hub", None)
    if hub is None or not hub.url:
        return None
    if getattr(config, "identity", None) is not None:
        secret = _identity_secret(config, hub)
    else:
        secret = os.environ.get(hub.secret_env, "")
    if not secret:
        raise HubRefusal(
            "secret_required",
            f"hub.url is set but {hub.secret_env} is not in the environment",
            f"export {hub.secret_env}=<the actor's shared secret>",
        )
    return HubClient(url=hub.url, actor=hub.actor, secret=secret, timeout_s=hub.timeout_s,
                     executor=hub.executor)


def completion_hook(orch, task: Task) -> None:
    """Registered with the orchestrator: report a mirrored bead the moment it lands DONE."""
    if not (task.metadata or {}).get(HUB_KEY):
        return
    client = client_from_config(orch.config)
    if client is None:
        print(f"[hub] {task.id} was pulled from a hub but none is configured now; "
              f"hub_of_origin_unconfigured — the outcome is not sent")
        return
    try:
        client.report_back(orch.beads, task)
    except (HubRefusal, HubUnreachable) as e:
        # The sweep will retry; the bead is done here regardless (§9: a
        # plane is advisory and never blocks a harness).
        print(f"[hub] report for {task.id} deferred to sync: {e}")
