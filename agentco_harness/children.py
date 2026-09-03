"""Child instance registry and heartbeat verification.

Every AgentCo instance may have children — other AgentCo instances whose
liveness it verifies on its own heartbeat. The registry lives at
`children/registry.jsonl` beside the task queue; one line per child.

`verify_child` is a pure code path — no LLM call. It reads the child's
`heartbeat.json` and judges freshness. A crashed or wedged child never
updates its heartbeat, so staleness IS the failure signal: absence of
freshness is exactly what the parent checks.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .beads import normalize_capabilities
from .recurring import parse_duration

HEARTBEAT_FILENAME = "heartbeat.json"
# Where the hub records each remote worker's last `agentco pull` (ac-48d8aba3).
# Beside the children registry, because it describes the same population.
PULLS_FILENAME = "pulls.json"
DEFAULT_GRACE = 2.0
# Extra slack past a backoff-aware child's own `next_due_at` before we call it
# dead — the child promised to complete a cycle by next_due_at; we allow one
# and a half more of its *current* intervals for a late/slow wake before FAIL.
DEFAULT_DUE_GRACE = 1.5
# Slack allowed on top of the PARENT's own overdue window before a stale child
# is judged independently broken rather than collaterally dead. One interval:
# after the host comes back, the child's first launchd tick lands up to one
# full interval later, so it is legitimately staler than the parent by that
# much and no more.
HOST_OUTAGE_SLACK_INTERVALS = 1.0
# How long the parent's memory of its OWN outage stays admissible as evidence,
# in multiples of its interval. launchd coalesces every missed StartInterval
# job into one wake, but not into one *instant*: on 2026-08-31 the parent came
# back at 07:00:08Z and its children at 07:46:45Z, 46 minutes later. In that
# window the parent is punctual again (its overdue count is 0) while the
# children still carry the outage, so `parent_next_due_at` alone stops
# explaining them and every child FAILs on the parent's second cycle back.
OUTAGE_EVIDENCE_WINDOW_INTERVALS = 2.0


def _parent_overdue_seconds(
    now: datetime, parent_next_due_at: datetime | None
) -> float:
    """Seconds the PARENT is past its own published deadline; 0 when on time.

    A parent running on schedule reports 0, which disables every host-outage
    downgrade below — the normal path keeps failing loudly, unchanged.
    """
    if parent_next_due_at is None:
        return 0.0
    if parent_next_due_at.tzinfo is None:
        parent_next_due_at = parent_next_due_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parent_next_due_at).total_seconds())


def _downgrade_for_host_outage(
    failure: dict,
    child_overdue: float,
    parent_overdue: float,
    interval_s: float,
) -> dict:
    """Turn a stale-child FAIL into a WARN when the parent was down too.

    Parent and children are user LaunchAgents in the same launchd domain: when
    that domain goes away (logout, reboot before login, host sleep), NOTHING
    ticks. The first cycle after the outage then sees every child stale and
    reports each as `fail` — one full-price RCA bead per child, per outage,
    for children that are healthy and self-recover on their next tick.

    The parent's own lateness is the evidence. If the child is stale by no more
    than the parent is — plus one interval of slack for its own first tick back
    — the same outage explains both, and this is a WARN, not a fault. A child
    staler than that has an independent problem and still FAILs.
    """
    if parent_overdue <= 0:
        return failure
    if child_overdue > parent_overdue + HOST_OUTAGE_SLACK_INTERVALS * interval_s:
        return failure
    return {
        **failure,
        "ok": True,
        "level": "warn",
        "detail": (
            f"stale, but explained by a host-level outage: this parent was "
            f"itself out for {parent_overdue:.0f}s, and the "
            f"child is only {child_overdue:.0f}s past its own — same launchd "
            f"domain, so neither could tick. Not an independent child fault; "
            f"expect self-recovery on its next interval. "
            f"(original: {failure['detail']})"
        ),
    }


# An interval of "manual" means the child has NO automatic cadence — nobody
# schedules it, so staleness is not a failure signal and an alarm would be
# permanent noise. It is a first-class registry value, not a malformed
# duration: `agentco add-company` writes it for vault-only and personal nodes.
MANUAL_INTERVAL = "manual"

# Child kinds whose liveness this parent cannot observe from a heartbeat file.
# `ado-backed` children live in Azure DevOps and have no local instance dir at
# all; `vault-only` children are Obsidian folders with no cycle to run. Both
# belong in the registry (for `agentco me` routing and portfolio display) but
# have nothing to verify.
UNVERIFIABLE_TYPES = {"ado-backed", "vault-only"}


@dataclass
class ChildRef:
    """One registered child instance.

    `path` and `expected_interval` are optional because the registry legitimately
    holds children this parent cannot poll: an `ado-backed` child has no local
    path, and a `manual` child has no cadence. Requiring them quarantined three
    of six real children — sommeli, frontsteps and personal vanished from
    verification while `status()` still reported a clean list, so the portfolio
    read as fully monitored when half of it was unwatched. A registry row must
    be able to say "present but not pollable" instead of being dropped for it.
    """

    name: str
    path: str | None = None
    expected_interval: str | None = "1h"
    notify: bool = True
    priority: int = 2  # company weight for `agentco me`: 0=critical … 3=low (mirrors TaskPriority)
    type: str = "beads"
    vault_path: str | None = None
    # --- node tags (ac-39d4dbc8) --------------------------------------------
    # `host` names the machine this child actually runs on. None (the default)
    # means "here" — every child that existed before two-machine LifeOS. A set
    # host makes `path` a path ON THAT HOST, which this machine cannot open:
    # the registry's founding assumption was that every child is locally
    # mountable, and the MacBook node is the first one that is not.
    #
    # `capabilities` mirrors that node's own manifest so the hub can SEE which
    # lane a child owns without reaching across the network. It is a cached
    # description, never the authority — the claim gate reads the claimant's own
    # config.yaml, so a stale tag here can mislead a human but can never grant
    # a lane. Keeping the authority local to the claimant is what stops a
    # registry edit on one machine from widening a credential boundary on another.
    host: str | None = None
    capabilities: list[str] = field(default_factory=list)

    @property
    def is_remote(self) -> bool:
        """Whether this child lives on another machine."""
        return bool(self.host)

    @property
    def verifiable(self) -> bool:
        """Whether liveness can be judged from a heartbeat at all."""
        return (
            self.type not in UNVERIFIABLE_TYPES
            and not self.is_remote
            and bool(self.path)
            and self.expected_interval != MANUAL_INTERVAL
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "ChildRef":
        d = json.loads(line)
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        obj = cls(**d)
        if not obj.name:
            raise ValueError("child requires a name")
        # STRICT here (unlike a node's own config.yaml, which warns and drops):
        # a malformed row quarantines loudly and is preserved verbatim, which is
        # this registry's whole failure posture. A silently emptied capability
        # tag would make a remote lane look unowned in `me` and `status`.
        obj.capabilities = normalize_capabilities(
            obj.capabilities, field_name="capabilities", where=f"child {obj.name!r}"
        )
        # A verifiable child MUST carry a usable path and a real duration — those
        # are still validated at read so a genuinely malformed row quarantines.
        # An unverifiable one is exempt by design, not by accident.
        if obj.type not in UNVERIFIABLE_TYPES and obj.path:
            if obj.expected_interval and obj.expected_interval != MANUAL_INTERVAL:
                parse_duration(obj.expected_interval)
        return obj


class ChildRegistry:
    """JSONL registry of child instances. Quarantines bad lines loudly."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._quarantined: list[str] = []
        self._warned_lines: set[str] = set()

    def exists(self) -> bool:
        return self.path.exists()

    def list(self) -> list[ChildRef]:
        children = []
        self._quarantined = []
        if not self.path.exists():
            return children
        with open(self.path) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    children.append(ChildRef.from_json(line))
                except (ValueError, KeyError, TypeError) as e:
                    self._quarantined.append(line)
                    if line not in self._warned_lines:
                        self._warned_lines.add(line)
                        print(
                            f"[children] WARNING: quarantined unparseable child at "
                            f"{self.path}:{lineno} ({e}) — preserved, not verified"
                        )
        return children

    def get(self, name: str) -> ChildRef | None:
        for c in self.list():
            if c.name == name:
                return c
        return None

    def add(self, child: ChildRef) -> ChildRef:
        if self.get(child.name):
            raise ValueError(f"child {child.name!r} is already registered")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(child.to_json() + "\n")
        return child

    def upsert(self, child: ChildRef, force: bool = False) -> str:
        """Converge the registry to contain exactly this child entry.

        Returns the outcome: "created" | "updated" | "unchanged". Refuses to
        re-point an existing name at a *different* path unless ``force`` —
        silently re-pointing would orphan the real instance.
        """
        existing = self.get(child.name)
        if existing is None:
            self.add(child)
            return "created"
        if existing.path != child.path and not force:
            raise ValueError(
                f"child {child.name!r} is already linked to {existing.path}; "
                f"refusing to re-point to {child.path} (use --force to override)"
            )
        if asdict(existing) == asdict(child):
            return "unchanged"
        children = [child if c.name == child.name else c for c in self.list()]
        self._write_all(children)
        return "updated"

    def _write_all(self, children: list[ChildRef]) -> None:
        """Atomically rewrite, preserving quarantined raw lines.

        Same crash-safety pattern as Recurring._write_all: temp file in the
        same directory + fsync + os.replace, so a crash mid-write leaves
        either the whole old file or the whole new one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".children-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for c in children:
                    f.write(c.to_json() + "\n")
                for raw in self._quarantined:
                    f.write(raw + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


class PullLedger:
    """When each remote worker last spoke to this hub (bead ac-48d8aba3).

    A remote node is the one child whose liveness this parent cannot read off a
    disk: `verify_child` answers "remote — status via mirror" and stops, which
    is honest but unmonitored. A MacBook whose launchd job died looks exactly
    like a MacBook with nothing to do, forever.

    The missing fact is not on the remote machine at all — it is here. The hub
    is the only party that observes every `agentco pull`, so the pull IS the
    remote node's heartbeat, and this is where it gets written down. That keeps
    invariant 1 of Plans/TwoMachineLifeos.md intact: the remote party still
    interacts only through commands, never file writes into a synced tree.

    Shape is a single JSON object keyed by node, atomically replaced — NOT the
    JSONL append this repo uses for queues. A queue's value is its history; this
    file's value is one current row per worker, and a poll every five minutes
    would grow an append-only log by ~300 lines a day to answer a question that
    only ever concerns the newest one. `heartbeat.json` is the precedent, and
    this is the same kind of record for a worker that is not local.

    Keyed on the node name when the pull names one, else the agent name. The
    hub's own scheduler is what a plain agent-keyed row describes; a `--node`
    row is what doctor matches against the children registry.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict:
        """Every recorded row. An unreadable ledger reads as empty, loudly.

        Never raises: this file is diagnostic bookkeeping, and a corrupt one
        must not take down the dispatch path that writes it. The warning is the
        signal — a hub that cannot read its own pull ledger has lost remote
        staleness alerting, and silence there is the failure mode this class
        exists to remove.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # stderr, not stdout: `agentco pull` promises JSON and nothing else
            # on stdout, and a warning printed there would corrupt the machine
            # interface the remote worker parses.
            print(
                f"[children] WARNING: unreadable pull ledger {self.path} ({e})",
                file=sys.stderr,
            )
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict | None:
        entry = self.load().get(key)
        return entry if isinstance(entry, dict) else None

    def record(
        self,
        key: str,
        agent: str,
        node: str | None,
        mode: str,
        claimed: int = 0,
        outstanding: int = 0,
        now: datetime | None = None,
    ) -> dict:
        """Stamp a pull. Returns the row written.

        A reconcile poll is recorded exactly like a claiming one: the question
        this ledger answers is "is the worker alive and talking", and a worker
        held at the reconcile gate is emphatically alive. Recording only
        successful claims would make a correctly-guarded worker look dead.
        """
        now = now or datetime.now(timezone.utc)
        data = self.load()
        prior = data.get(key) if isinstance(data.get(key), dict) else {}
        row = {
            "agent": agent,
            "node": node,
            "last_pull_at": now.isoformat(),
            "last_mode": mode,
            "last_claimed": claimed,
            "outstanding": outstanding,
            "pulls": int(prior.get("pulls") or 0) + 1,
        }
        if mode == "reconcile":
            row["last_reconcile_at"] = now.isoformat()
        elif prior.get("last_reconcile_at"):
            row["last_reconcile_at"] = prior["last_reconcile_at"]
        data[key] = row
        self._write(data)
        return row

    def _write(self, data: dict) -> None:
        """Atomic rewrite — same temp+fsync+replace contract as the registry."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".pulls-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def pull_ledger_path(registry_path: Path | str) -> Path:
    """The pull ledger sits beside the children registry it describes."""
    return Path(registry_path).parent / PULLS_FILENAME


def verify_remote_child(
    child: ChildRef,
    entry: dict | None,
    now: datetime | None = None,
    grace: float = DEFAULT_GRACE,
) -> dict:
    """Judge a REMOTE child's liveness from its pull record (ac-48d8aba3).

    Deliberately the same return contract as `verify_child`
    ({"child", "ok", "level", "detail", "staleness_seconds"}) so every existing
    consumer — doctor's reporting loop, `notify_stale`, the bead record a
    verify_child def writes — works on it unchanged. A remote node is not a new
    kind of thing needing new plumbing; it is a child whose heartbeat arrives
    over SSH instead of off a local disk, and the only honest difference is
    where the timestamp comes from.

    `entry` is that node's row from `PullLedger`, or None if it has never
    pulled. Never-pulled is a FAIL for the same reason a missing local
    heartbeat is: the node is registered, so something was promised, and
    nothing has ever arrived. Doctor renders a child-level fail as WARN, so
    this reports loudly without gating a hub whose laptop is simply not set up
    yet.
    """
    now = now or datetime.now(timezone.utc)

    if not child.is_remote:
        raise ValueError(
            f"verify_remote_child called on local child {child.name!r} — "
            f"use verify_child(); a local node's heartbeat is on this disk"
        )

    # A remote node with no cadence promised nothing, so it cannot be late.
    # Same exemption `verifiable` grants a local `manual` child.
    if not child.expected_interval or child.expected_interval == MANUAL_INTERVAL:
        return {
            "child": child.name,
            "ok": True,
            "level": "unverified",
            "detail": (
                f"remote node on {child.host} with interval "
                f"'{MANUAL_INTERVAL}' — no automatic cadence to be late for"
            ),
            "staleness_seconds": None,
        }

    if not entry or not entry.get("last_pull_at"):
        return {
            "child": child.name,
            "ok": False,
            "level": "fail",
            "detail": (
                f"remote node on {child.host} has NEVER pulled — no worker on "
                f"that host has ever run `agentco pull --node {child.name}`. "
                f"Its lane is registered but unstaffed: beads routed to it will "
                f"sit PENDING forever. Check the launchd job and SSH key on "
                f"{child.host}."
            ),
            "staleness_seconds": None,
        }

    last = datetime.fromisoformat(entry["last_pull_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    staleness = (now - last).total_seconds()
    allowed = parse_duration(child.expected_interval).total_seconds() * grace

    if staleness > allowed:
        return {
            "child": child.name,
            "ok": False,
            "level": "fail",
            "detail": (
                f"remote node on {child.host} has not pulled in "
                f"{staleness:.0f}s, allowed {allowed:.0f}s "
                f"(expected_interval={child.expected_interval} × grace={grace}); "
                f"last pull {entry['last_pull_at']} in mode "
                f"{entry.get('last_mode', '?')!r}. Its lane is stalled — beads "
                f"requiring it are not being executed by anyone."
            ),
            "staleness_seconds": staleness,
        }

    if entry.get("last_mode") == "reconcile":
        return {
            "child": child.name,
            "ok": True,
            "level": "warn",
            "detail": (
                f"remote node on {child.host} is pulling (last {staleness:.0f}s "
                f"ago) but is HELD AT THE RECONCILE GATE with "
                f"{entry.get('outstanding', 0)} bead(s) outstanding — it is "
                f"claiming no new work until those are resolved"
            ),
            "staleness_seconds": staleness,
        }

    return {
        "child": child.name,
        "ok": True,
        "level": "ok",
        "detail": (
            f"remote node on {child.host} healthy — last pull {staleness:.0f}s "
            f"ago, {entry.get('last_claimed', 0)} bead(s) claimed"
        ),
        "staleness_seconds": staleness,
    }


def child_heartbeat_path(instance_dir: Path | str) -> Path:
    """Locate a child instance's heartbeat file.

    The heartbeat is written beside the child's task queue. We resolve it
    through the child's own config.yaml (which resolves a relative
    tasks_path against the config's directory); a child without config.yaml
    falls back to `<instance_dir>/heartbeat.json`.
    """
    instance_dir = Path(instance_dir)
    config_path = instance_dir / "config.yaml"
    if config_path.exists():
        from .config import Config

        config = Config.load(config_path)
        return Path(config.tasks_path).parent / HEARTBEAT_FILENAME
    return instance_dir / HEARTBEAT_FILENAME


def verify_child(
    child: ChildRef,
    now: datetime | None = None,
    grace: float = DEFAULT_GRACE,
    due_grace: float = DEFAULT_DUE_GRACE,
    parent_next_due_at: datetime | None = None,
    parent_recent_outage_s: float = 0.0,
) -> dict:
    """Verify one child's liveness. Pure code — no LLM call.

    Returns {"child", "ok", "level", "detail", "staleness_seconds"} where
    level is "ok" | "warn" | "fail". Failures are loud at the tier where
    someone is looking: the caller writes this into its own bead record.

    Backoff-aware: if the child publishes `next_due_at` (adaptive backoff), the
    parent judges freshness against *that* deadline plus `due_grace × current
    interval`, not against a fixed `expected_interval`. This is what stops a
    legitimately backed-off (idle) child from tripping a false staleness alarm.
    A child on an older build without `next_due_at` falls back to the classic
    `expected_interval × grace` check unchanged.

    Outage-aware: pass `parent_next_due_at` (the caller's OWN deadline from its
    previous heartbeat) and a stale child is downgraded fail → warn whenever
    the parent is itself that late — a host-level outage killed both, and the
    child is not independently broken. Omit it and behaviour is unchanged.

    `parent_recent_outage_s` carries the same evidence one step further, and is
    what makes the downgrade survive the parent's own recovery. The parent is
    only *visibly* late on its first cycle back; launchd then restarts the
    children up to tens of minutes after that, so on the parent's second cycle
    `parent_next_due_at` is fresh again while the children are still stale.
    Passing how long the parent was itself out keeps that window explained.
    Whichever evidence is larger wins; both default to "no outage", which
    leaves the normal loud-failure path untouched.
    """
    now = now or datetime.now(timezone.utc)
    parent_overdue = max(
        _parent_overdue_seconds(now, parent_next_due_at),
        max(0.0, float(parent_recent_outage_s or 0.0)),
    )

    # A child with no observable heartbeat reports "unverified", NOT "ok" and
    # NOT "fail". Calling it ok would launder an unknown into a green light —
    # the same false all-clear that hid three children for a week. Calling it
    # fail would alarm forever on something nobody schedules.
    # A REMOTE child is checked first and answered distinctly, because both of
    # the obvious answers are wrong. "fail" (what the path-does-not-exist branch
    # below would say) is a permanent false alarm — nothing is broken, the disk
    # is simply on another machine. "ok" would launder an unknown into a green
    # light, the same false all-clear that once hid three children for a week.
    # Its own level says what is actually true: this parent cannot observe it
    # from here, and its status arrives through the mirrored bead state instead.
    #
    # `host` is authoritative over `path` on purpose: a leftover directory of
    # the same name on this machine must never be mistaken for the real node —
    # that would report a stale local copy as the live one.
    if child.is_remote:
        return {
            "child": child.name,
            "ok": True,
            "level": "remote",
            "detail": (
                f"remote node on {child.host} — status via mirror, not a local "
                f"heartbeat (path {child.path!r} is on that host)"
            ),
            "staleness_seconds": None,
        }

    if not child.verifiable:
        if child.type in UNVERIFIABLE_TYPES:
            why = f"type '{child.type}' has no local heartbeat to poll"
        elif not child.path:
            why = "no path registered"
        else:
            why = f"interval is '{MANUAL_INTERVAL}' — no automatic cadence to be late for"
        return {
            "child": child.name,
            "ok": True,          # not a failure — nothing was promised
            "level": "unverified",
            "detail": f"not verifiable: {why}",
            "staleness_seconds": None,
        }

    instance_dir = Path(child.path)

    if not instance_dir.is_dir():
        return {
            "child": child.name,
            "ok": False,
            "level": "fail",
            "detail": f"child path {instance_dir} does not exist",
            "staleness_seconds": None,
        }

    hb_path = child_heartbeat_path(instance_dir)
    if not hb_path.exists():
        return {
            "child": child.name,
            "ok": False,
            "level": "fail",
            "detail": (
                f"no heartbeat at {hb_path} — child has never completed a cycle"
            ),
            "staleness_seconds": None,
        }

    try:
        hb = json.loads(hb_path.read_text())
        completed_at = datetime.fromisoformat(hb["cycle_completed_at"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
        return {
            "child": child.name,
            "ok": False,
            "level": "fail",
            "detail": f"unreadable heartbeat at {hb_path} ({e})",
            "staleness_seconds": None,
        }
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)

    staleness = (now - completed_at).total_seconds()

    # Prefer the child's published next_due_at (backoff-aware) over the fixed
    # expected_interval; fall back to the classic check when it is absent.
    next_due_raw = hb.get("next_due_at")
    next_due_at = None
    if next_due_raw:
        try:
            next_due_at = datetime.fromisoformat(next_due_raw)
            if next_due_at.tzinfo is None:
                next_due_at = next_due_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            next_due_at = None

    if next_due_at is not None:
        try:
            interval_s = float(hb.get("current_interval_s") or 0) or (
                parse_duration(child.expected_interval).total_seconds()
            )
        except (ValueError, TypeError):
            interval_s = parse_duration(child.expected_interval).total_seconds()
        deadline = next_due_at + timedelta(seconds=due_grace * interval_s)
        if now > deadline:
            overdue = (now - deadline).total_seconds()
            return _downgrade_for_host_outage(
                {
                    "child": child.name,
                    "ok": False,
                    "level": "fail",
                    "detail": (
                        f"heartbeat is stale: {overdue:.0f}s past its own deadline "
                        f"(next_due_at={next_due_at.isoformat()} + due_grace={due_grace} "
                        f"× current_interval={interval_s:.0f}s); last cycle {staleness:.0f}s ago"
                    ),
                    "staleness_seconds": staleness,
                },
                child_overdue=overdue,
                parent_overdue=parent_overdue,
                interval_s=interval_s,
            )
    else:
        expected_s = parse_duration(child.expected_interval).total_seconds()
        allowed = expected_s * grace
        if staleness > allowed:
            return _downgrade_for_host_outage(
                {
                    "child": child.name,
                    "ok": False,
                    "level": "fail",
                    "detail": (
                        f"heartbeat is stale: last successful cycle {staleness:.0f}s ago, "
                        f"allowed {allowed:.0f}s (expected_interval={child.expected_interval} "
                        f"× grace={grace})"
                    ),
                    "staleness_seconds": staleness,
                },
                child_overdue=staleness - allowed,
                parent_overdue=parent_overdue,
                interval_s=expected_s,
            )

    errors = hb.get("errors_this_cycle", 0)
    if errors:
        return {
            "child": child.name,
            "ok": True,
            "level": "warn",
            "detail": f"heartbeat fresh but last cycle recorded {errors} error(s)",
            "staleness_seconds": staleness,
        }

    return {
        "child": child.name,
        "ok": True,
        "level": "ok",
        "detail": f"healthy — last cycle {staleness:.0f}s ago",
        "staleness_seconds": staleness,
    }


def notify_stale(child: ChildRef, detail: str, url: str, timeout: float = 5.0) -> bool:
    """Best-effort external notification for a failed child verification.

    Notification failure logs a WARNING but never fails the cycle —
    a broken notify channel must not mask the verification result itself.
    """
    payload = json.dumps(
        {"message": f"AgentCo: child '{child.name}' failed verification — {detail}"}
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        print(f"[children] WARNING: notify failed for child '{child.name}' ({e})")
        return False
