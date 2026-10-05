"""Metadata-only audit export — `agentic-co audit export` / `audit verify`.

WHY THIS EXISTS
---------------
A harness validated on a raw machine (no hub, no LifeOS, its own Claude Code)
has to be able to ship back evidence of what it did, to be reviewed somewhere
else. The node's ledgers are that evidence, but they also hold bead text,
prompts, model output, gate commands and paths. This module produces a bundle
that carries the first and none of the second.

DENY BY DEFAULT
---------------
Every exported row is rebuilt from ``POLICY`` below: a field is copied only if
the table says ``keep`` (or names a nested shape to reduce it by). Anything
else — a field the table denies, or one it has never heard of — is dropped and
COUNTED BY NAME in the manifest's redaction report, so a writer that gains a
field shows up as "dropped: N" rather than leaking. `tests/test_audit_export.py`
fails when a ledger writer emits a field the table has not decided.

Kept values are scalars (or lists of scalars) only. A kept string that is long
or carries a path under the user's home / the node directory is replaced by
its sha256 — a kept field is an identifier, and an identifier is never either.

READ-ONLY
---------
Ledgers are read with plain ``open(..., "rb")``; no store class is constructed
(their constructors create missing files) and no lock is taken. ``doctor`` is
not read-only on a sparse node — it creates an empty ``tasks.jsonl`` /
``recurring.jsonl`` when absent — so it runs against a throwaway shadow of the
node: real directories in a temp dir, every existing entry symlinked, the
config file copied. Anything doctor creates lands in the shadow.

NULL, NEVER ZERO
----------------
Values are copied, never defaulted: a null token count stays null. A ledger
that does not exist reports ``rows_read: null``, not 0, and a filter that was
not applied reports its counts as null.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

SCHEMA = "agentic-co-audit-v1"
TIER = "metadata"
MANIFEST = "manifest.json"
DEFAULT_OUT = "~/agentic-co-audit"

#: A kept string longer than this is hashed: identifiers are short.
MAX_KEPT_STRING = 256

KEEP = "keep"
DENY = "deny"


def _nest(shape: str) -> str:
    return f"nest:{shape}"


# --------------------------------------------------------------------------- #
# THE TABLE. shape -> field -> (decision, one-line justification).
# Field names are the writers' real keys (usage.record_usage, cost.record_run,
# Orchestrator._log_run/_write_cycle_heartbeat/_heartbeat, schedules.reserve/
# observe, RecurringDef, asop.ASOP/Step, beads.Task and the metadata it gets).
# --------------------------------------------------------------------------- #
POLICY: dict[str, dict[str, tuple[str, str]]] = {
    "usage": {  # usage.jsonl — usage.record_usage (the meter() row)
        "schema": (KEEP, "row format version"),
        "at": (KEEP, "event time; the --since axis"),
        "bead_id": (KEEP, "joins the row to beads.jsonl"),
        "lane": (KEEP, "which pipeline dispatched the run"),
        "node": (KEEP, "node name (basename of the store dir)"),
        "company": (KEEP, "company label the bead declared"),
        "task_type": (KEEP, "routing-evaluation group key"),
        "data_class": (KEEP, "egress classification label"),
        "executor": (KEEP, "executor adapter name"),
        "route": (KEEP, "egress route name"),
        "model_requested": (KEEP, "model asked for"),
        "model_used": (KEEP, "model that answered"),
        "duration_seconds": (KEEP, "latency"),
        "exit_status": (KEEP, "ok/failed/timeout classification"),
        "exit_code": (KEEP, "process exit code"),
        "input_tokens": (KEEP, "spend; null when the route does not report it"),
        "output_tokens": (KEEP, "spend; null when unreported"),
        "cache_read_tokens": (KEEP, "spend; null when unreported"),
        "cache_creation_tokens": (KEEP, "spend; null when unreported"),
        "total_tokens": (KEEP, "spend; null when neither side reported"),
        "num_turns": (KEEP, "agent turns; null when unreported"),
        "cost_usd": (KEEP, "price; null when the route reports none"),
        "error": (DENY, "free text from the CLI/exception: can carry prompt, output or paths"),
        "extra": (DENY, "free-form attribution dict an extension fills; contents unknown"),
    },
    "costs": {  # costs.jsonl — cost.record_run
        "at": (KEEP, "event time; the --since axis"),
        "task_id": (KEEP, "joins the row to beads.jsonl"),
        "agent": (KEEP, "agent the bead ran on"),
        "company": (KEEP, "company label"),
        "data_class": (KEEP, "egress classification label"),
        "task_type": (KEEP, "routing-evaluation group key"),
        "requested_model": (KEEP, "model asked for"),
        "model_used": (KEEP, "model that answered"),
        "success": (KEEP, "completion flag — the cost-per-completed-bead denominator"),
        "truncated": (KEEP, "output-truncation flag"),
        "cost_usd": (KEEP, "price; null when the envelope carried none"),
        "input_tokens": (KEEP, "spend; null when unreported"),
        "output_tokens": (KEEP, "spend; null when unreported"),
        "num_turns": (KEEP, "agent turns"),
        "duration_seconds": (KEEP, "latency"),
    },
    "runs": {  # runs.jsonl — Orchestrator._log_run(**summary)
        "at": (KEEP, "cycle time; the --since axis"),
        "instance": (KEEP, "instance name (directory basename by default)"),
        "spawned": (KEEP, "recurring beads spawned this cycle"),
        "executed": (KEEP, "beads completed this cycle"),
        "errors": (KEEP, "beads failed this cycle"),
        "open_after": (KEEP, "ready beads left after the cycle"),
        "undispatchable": (KEEP, "beads stalled on an undeclared agent"),
        "chat_replies": (KEEP, "chat threads answered"),
        "current_interval_s": (KEEP, "adaptive-backoff interval"),
        "next_due_at": (KEEP, "adaptive-backoff next wake"),
        "tasks": (_nest("runs.task"), "per-bead outcomes, reduced by runs.task"),
    },
    "runs.task": {
        "id": (KEEP, "bead id"),
        "agent": (KEEP, "agent it ran on"),
        "outcome": (KEEP, "done/failed"),
        "title": (DENY, "bead text"),
        "error": (DENY, "failure text / bead result excerpt: model output"),
    },
    "heartbeat": {  # heartbeat.json — Orchestrator._write_cycle_heartbeat
        "instance": (KEEP, "instance name"),
        "cycle_completed_at": (KEEP, "last completed cycle — staleness is the failure signal"),
        "beads_open": (KEEP, "open bead count"),
        "beads_done_this_cycle": (KEEP, "count"),
        "recurring_spawned_this_cycle": (KEEP, "count"),
        "errors_this_cycle": (KEEP, "count"),
        "version": (KEEP, "harness version that wrote it"),
        "current_interval_s": (KEEP, "adaptive-backoff interval"),
        "next_due_at": (KEEP, "adaptive-backoff next wake"),
        "last_outage_gap_s": (KEEP, "host-outage evidence"),
        "last_outage_ended_at": (KEEP, "host-outage evidence"),
    },
    "node_state": {  # .agentco-heartbeat.json — Orchestrator._heartbeat(**fields)
        "last_work_at": (KEEP, "timestamp"),
        "last_work_completed": (KEEP, "count"),
        "last_observe_at": (KEEP, "timestamp"),
        "last_wake_at": (KEEP, "timestamp of a backoff-skipped wake"),
    },
    "schedules": {  # schedules.jsonl — schedules.reserve / schedules.observe
        "schema": (KEEP, "row format version"),
        "type": (KEEP, "reservation/observation discriminator"),
        "key": (KEEP, "uniqueness key (schedule id + period)"),
        "kind": (KEEP, "reservation namespace"),
        "subject": (KEEP, "schedule id (recurring def id)"),
        "period": (KEEP, "period fired"),
        "at": (KEEP, "event time; the --since axis"),
        "produced": (KEEP, "beads produced by the firing"),
        "bead_ids": (KEEP, "ids of the beads produced"),
        "detail": (DENY, "free text"),
    },
    "recurring": {  # recurring.jsonl — RecurringDef.to_json
        "id": (KEEP, "definition id; joins schedules.subject"),
        "title": (DENY, "bead text"),
        "schedule": (_nest("recurring.schedule"), "cadence"),
        "agent": (KEEP, "agent the spawned bead is assigned to"),
        "payload": (DENY, "bead template: description, prompt, metadata"),
        "last_spawned": (KEEP, "timestamp"),
        "created_at": (KEEP, "timestamp"),
        "enabled": (KEEP, "flag"),
        "catch_up": (KEEP, "latest/all policy"),
        "budget": (_nest("recurring.budget"), "executor budget"),
    },
    "recurring.schedule": {
        "every": (KEEP, "interval duration string"),
    },
    "recurring.budget": {
        "timeout": (KEEP, "seconds"),
        "max_turns": (KEEP, "count"),
    },
    "asops": {  # asops.jsonl — asop.ASOP.to_json
        "asop_id": (KEEP, "procedure id"),
        "version": (KEEP, "procedure version"),
        "status": (KEEP, "draft/active/retired"),
        "task_type": (KEEP, "routing key"),
        "author_kind": (KEEP, "trust domain of the author (human/agent)"),
        "superseded_by": (KEEP, "version that replaced this one"),
        "created_at": (KEEP, "timestamp"),
        "steps": (_nest("asops.step"), "step structure, reduced by asops.step"),
        "title": (DENY, "procedure text"),
        "purpose": (DENY, "procedure text"),
        "trigger": (DENY, "procedure text"),
        "inputs": (DENY, "input declarations: names and descriptions"),
        "roles": (DENY, "dict keyed by author-chosen role names"),
        "constraints": (DENY, "separation-of-duty declarations over role names"),
        "author": (DENY, "a person's or agent's name; author_kind carries the trust domain"),
        "proposals": (DENY, "free text"),
    },
    "asops.step": {
        "step": (KEEP, "step number"),
        "role": (KEEP, "role name the step binds"),
        "after": (KEEP, "step ordering"),
        "tags": (KEEP, "policy labels (protected-tag policy reads them)"),
        "gate": (_nest("gate"), "gate kind and timing only"),
        "uses": (_nest("ref"), "pinned sub-procedure reference"),
        "name": (DENY, "step text"),
        "purpose": (DENY, "step text"),
        "entry_check": (DENY, "step text"),
        "inputs": (DENY, "step text"),
        "definition_of_done": (DENY, "step text"),
        "validation": (DENY, "step text"),
        "write_back": (DENY, "step text"),
        "common_mistakes": (DENY, "step text"),
        "proposals": (DENY, "free text"),
    },
    "gate": {  # asop.gates.GATE_FIELDS — a bead's metadata.verify / a step's gate
        "class": (KEEP, "gate class (deterministic/human/judged)"),
        "kind": (KEEP, "gate kind"),
        "timeout_s": (KEEP, "seconds"),
        "max_park_seconds": (KEEP, "seconds"),
        "on_timeout": (KEEP, "timeout policy"),
        "judge_route": (KEEP, "route name"),
        "verifier": (KEEP, "declared verifier name"),
        "escalate_to": (KEEP, "declared escalation target name"),
        "check": (DENY, "shell command: can carry paths and secrets"),
        "checks": (DENY, "shell commands"),
        "cwd": (DENY, "path"),
        "rubric": (DENY, "free text"),
    },
    "ref": {  # ASOP.ref / ASOP.step_ref
        "asop_id": (KEEP, "procedure id"),
        "version": (KEEP, "procedure version"),
        "step": (KEEP, "step number"),
    },
    "beads": {  # tasks.jsonl — beads.Task.to_json
        "id": (KEEP, "bead id"),
        "status": (KEEP, "lifecycle status"),
        "priority": (KEEP, "0-3"),
        "assigned_agent": (KEEP, "agent"),
        "assigned_to": (KEEP, "assignee (human:<name>)"),
        "source": (KEEP, "intake source system name"),
        "parent_id": (KEEP, "tree edge"),
        "blocked_by": (KEEP, "dependency edges"),
        "starts_at": (KEEP, "timestamp"),
        "due_at": (KEEP, "timestamp"),
        "estimate_hours": (KEEP, "number"),
        "estimate_optimistic": (KEEP, "number"),
        "estimate_pessimistic": (KEEP, "number"),
        "actual_hours": (KEEP, "number"),
        "leased_by": (KEEP, "lease holder (worker name)"),
        "lease_attempt": (KEEP, "lease fence counter"),
        "lease_expires_at": (KEEP, "timestamp"),
        "requires": (KEEP, "capability tags"),
        "created_at": (KEEP, "timestamp"),
        "updated_at": (KEEP, "timestamp; the --since axis for beads"),
        "metadata": (_nest("beads.metadata"), "reduced by beads.metadata; unknown keys dropped"),
        "title": (DENY, "bead text"),
        "description": (DENY, "bead text / prompt"),
        "result": (DENY, "model output"),
        "source_id": (DENY, "external event id (message id, URL)"),
    },
    "beads.metadata": {
        "task_class": (KEEP, "agent/human class"),
        "type": (KEEP, "bead kind (e.g. verify_child)"),
        "spawned_by": (KEEP, "recurring def id that spawned it"),
        "data_class": (KEEP, "egress classification label"),
        "fix_for": (KEEP, "bead id this fixes"),
        "rca_for": (KEEP, "bead id this RCA is about"),
        "chat_pending": (KEEP, "flag"),
        "chat_in_flight_at": (KEEP, "timestamp"),
        "run_steps_done_at": (KEEP, "timestamp"),
        "run_closed_at": (KEEP, "timestamp"),
        "verify": (_nest("gate"), "gate kind and timing only"),
        "verify_result": (_nest("verify_result"), "gate outcome without command or output"),
        "verify_approval": (_nest("verify_approval"), "who approved and when"),
        "verify_rejection": (_nest("verify_rejection"), "who rejected and when"),
        "dispatch_refusal": (_nest("dispatch_refusal"), "refusal code and time"),
        "sop_ref": (_nest("ref"), "pinned procedure reference"),
        "uses": (_nest("ref"), "pinned sub-procedure reference"),
        "asop_route": (_nest("asop_route"), "router decision without its reasoning"),
        "chat": (DENY, "conversation text"),
        "error": (DENY, "failure text"),
        "run": (DENY, "run inputs and bindings: values"),
        "step": (DENY, "copy of step text"),
        "sop": (DENY, "procedure text"),
        "context_refs": (DENY, "paths and URLs"),
        "divergence": (DENY, "free text"),
    },
    "verify_result": {
        "class": (KEEP, "gate class"),
        "passed": (KEEP, "true/false/null (null = nobody checked)"),
        "checked_at": (KEEP, "timestamp"),
        "exit_code": (KEEP, "check exit code"),
        "timed_out": (KEEP, "flag"),
        "timeout_s": (KEEP, "seconds"),
        "stages_total": (KEEP, "count"),
        "stages_run": (KEEP, "count"),
        "failed_stage": (_nest("failed_stage"), "which stage failed, without its command"),
        "check": (DENY, "shell command"),
        "checks": (DENY, "shell commands"),
        "cwd": (DENY, "path"),
        "output_tail": (DENY, "command output / approver reason"),
        "note": (DENY, "free text"),
    },
    "failed_stage": {
        "index": (KEEP, "0-based stage index"),
        "exit_code": (KEEP, "exit code"),
        "timed_out": (KEEP, "flag"),
        "command": (DENY, "shell command"),
        "output_tail": (DENY, "command output"),
    },
    "verify_approval": {
        "approver": (KEEP, "declared verifier name"),
        "approved_at": (KEEP, "timestamp"),
        "verdict": (DENY, "contains the free-text reason"),
    },
    "verify_rejection": {
        "approver": (KEEP, "declared verifier name"),
        "rejected_at": (KEEP, "timestamp"),
        "reason": (DENY, "free text"),
    },
    "dispatch_refusal": {  # Beads.refuse_dispatch
        "code": (KEEP, "machine refusal code"),
        "at": (KEEP, "timestamp"),
        "message": (DENY, "prose; interpolates bead/route detail"),
        "remediation": (DENY, "prose"),
    },
    "asop_route": {  # asop_router.RouteResult.as_metadata
        "outcome": (KEEP, "match/plain/candidate"),
        "asop_id": (KEEP, "procedure id"),
        "version": (KEEP, "procedure version"),
        "confidence": (KEEP, "router confidence"),
        "run_id": (KEEP, "filed run id"),
        "similar_count": (KEEP, "count"),
        "held": (KEEP, "flag"),
        "reason": (DENY, "LM reasoning text"),
        "run_error": (DENY, "error text"),
    },
}

#: What is read, from where (relative to the store dir), into which bundle
#: path, by which shape, filtered on which field. ``None`` = a state snapshot,
#: exported whole regardless of --since.
@dataclass(frozen=True)
class _Ledger:
    source: str
    bundle: str
    shape: str
    since_field: Optional[str]
    single_object: bool = False
    sort: Optional[Callable[[dict], tuple]] = None


LEDGERS: tuple[_Ledger, ...] = (
    _Ledger("runs.jsonl", "ledgers/runs.jsonl", "runs", "at"),
    _Ledger("usage.jsonl", "ledgers/usage.jsonl", "usage", "at"),
    _Ledger("costs.jsonl", "ledgers/costs.jsonl", "costs", "at"),
    _Ledger("schedules.jsonl", "ledgers/schedules.jsonl", "schedules", "at"),
    _Ledger("recurring.jsonl", "ledgers/recurring.jsonl", "recurring", None,
            sort=lambda r: (str(r.get("id", "")),)),
    _Ledger("asops.jsonl", "ledgers/asops.jsonl", "asops", None,
            sort=lambda r: (str(r.get("asop_id", "")),
                            r.get("version") if isinstance(r.get("version"), int) else -1)),
    _Ledger("heartbeat.json", "ledgers/heartbeat.json", "heartbeat", None, single_object=True),
    _Ledger(".agentco-heartbeat.json", "ledgers/node-state.json", "node_state", None,
            single_object=True),
)

BEADS_SOURCE = "tasks.jsonl"
BEADS_BUNDLE = "beads.jsonl"
REFUSALS_BUNDLE = "refusals.jsonl"

#: What the bundle can and cannot say about refusals. Stated in the manifest so
#: a reviewer does not read an empty refusals file as "nothing was refused".
REFUSAL_COVERAGE = {
    "dispatch_refusal": (
        "exported (code + at) from bead metadata; current state only — a "
        "refusal cleared by clear_dispatch_refusal leaves no record"
    ),
    "egress": (
        "not persisted with its egress:* code: the cycle path records only "
        "dispatch_refusal.code='egress_denied'; the chat-reply path records "
        "nothing structured"
    ),
    "command_floor": (
        "not persisted: command_floor.check_command is not called from any "
        "execution path in this package"
    ),
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")
_VERSION = re.compile(r"\d+(?:\.\d+)+[0-9A-Za-z.+\-]*")


class AuditError(Exception):
    """A usage error the CLI reports and exits 2 on."""


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(key: object) -> str:
    return key if isinstance(key, str) and _IDENT.match(key) else "<non-identifier>"


@dataclass
class _FileReport:
    dropped: Counter = field(default_factory=Counter)
    paths_hashed: int = 0
    oversize_hashed: int = 0

    def as_dict(self, corrupt_lines: Optional[int]) -> dict:
        return {
            "dropped": dict(sorted(self.dropped.items())),
            "corrupt_lines": corrupt_lines,
            "paths_hashed": self.paths_hashed,
            "oversize_hashed": self.oversize_hashed,
        }


class _Reducer:
    """Rebuilds a row from POLICY; everything else is dropped and counted."""

    def __init__(self, scrub_roots: list[str], report: _FileReport):
        self.roots = scrub_roots
        self.report = report

    def _string(self, s: str) -> str:
        if s.startswith("~") or any(root in s for root in self.roots):
            self.report.paths_hashed += 1
            return "sha256:" + _sha256(s.encode("utf-8"))
        if len(s) > MAX_KEPT_STRING:
            self.report.oversize_hashed += 1
            return "sha256:" + _sha256(s.encode("utf-8"))
        return s

    def _scalar(self, v):
        if v is None or isinstance(v, (bool, int, float)):
            return True, v
        if isinstance(v, str):
            return True, self._string(v)
        if isinstance(v, list):
            out = []
            for item in v:
                ok, cleaned = self._scalar(item)
                if not ok or isinstance(item, list):
                    return False, None
                out.append(cleaned)
            return True, out
        return False, None

    def reduce(self, row: dict, shape: str, prefix: str = "") -> dict:
        policy = POLICY[shape]
        out: dict = {}
        for key in sorted(row, key=str):
            name = prefix + _safe_name(key)
            decision = policy.get(key) if isinstance(key, str) else None
            if decision is None or decision[0] == DENY:
                self.report.dropped[name] += 1
                continue
            value = row[key]
            if decision[0] == KEEP:
                ok, cleaned = self._scalar(value)
                if ok:
                    out[key] = cleaned
                else:
                    self.report.dropped[name + "(non-scalar)"] += 1
                continue
            sub = decision[0][len("nest:"):]
            if value is None:
                out[key] = None
            elif isinstance(value, dict):
                out[key] = self.reduce(value, sub, name + ".")
            elif isinstance(value, list):
                items = []
                for item in value:
                    if isinstance(item, dict):
                        items.append(self.reduce(item, sub, name + "[]."))
                    else:
                        self.report.dropped[name + "[](non-object)"] += 1
                out[key] = items
            else:
                self.report.dropped[name + "(non-object)"] += 1
        return out


# --------------------------------------------------------------------------- #
# Reading (plain file reads; no store class, no lock)
# --------------------------------------------------------------------------- #


def _read_rows(path: Path, validate: Optional[Callable[[str], object]] = None,
               single_object: bool = False) -> tuple[list[dict], int]:
    """(well-formed rows, corrupt line count). Corrupt content is never kept."""
    data = path.read_bytes()
    if single_object:
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return [], 1
        return ([obj], 0) if isinstance(obj, dict) else ([], 1)
    rows: list[dict] = []
    corrupt = 0
    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            text = line.decode("utf-8")
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
            if validate is not None:
                validate(text)
        except Exception:  # noqa: BLE001 — any unreadable line is quarantined, never exported
            corrupt += 1
            continue
        rows.append(obj)
    return rows, corrupt


def _validator(source: str) -> Optional[Callable[[str], object]]:
    """The real store's own parser, so 'corrupt' means what it means there."""
    if source == BEADS_SOURCE:
        from .beads import Task

        return Task.from_json
    if source == "recurring.jsonl":
        from .recurring import RecurringDef

        return RecurringDef.from_json
    if source == "asops.jsonl":
        from asop import ASOP

        return ASOP.from_json
    return None


def parse_since(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    parsed = _parse_ts(value)
    if parsed is None:
        raise AuditError(f"--since {value!r} is not an ISO-8601 date or timestamp")
    return parsed


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _since_split(rows: list[dict], fld: str, since: Optional[datetime]):
    """(kept, excluded, unparseable_kept). An unreadable timestamp is KEPT —
    the usage.within convention: a defect in a row is not evidence the event
    did not happen."""
    if since is None:
        return rows, None, None
    kept, excluded, unparseable = [], 0, 0
    for row in rows:
        ts = _parse_ts(row.get(fld))
        if ts is None:
            unparseable += 1
            kept.append(row)
        elif ts >= since:
            kept.append(row)
        else:
            excluded += 1
    return kept, excluded, unparseable


# --------------------------------------------------------------------------- #
# Node resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Node:
    root: Path  # the directory the operator named
    config_file: Optional[Path]
    store_dir: Path
    tasks_file: Path


def resolve_node(node: Optional[str], config_path: Optional[str] = None) -> Node:
    if node is not None:
        root = Path(node).expanduser().absolute()
        if not root.is_dir():
            raise AuditError(f"--node {node!r} is not a directory")
        candidates = [root / "config.yaml", root / ".agentco" / "config.yaml"]
    else:
        cfg = Path(config_path or "config.yaml").expanduser().absolute()
        root = cfg.parent
        candidates = [cfg]
    config_file = next((c for c in candidates if c.is_file()), None)
    tasks_file = root / BEADS_SOURCE
    if config_file is not None:
        try:
            from .config import Config

            tasks_file = Path(Config.load(config_file).tasks_path)
        except Exception:  # noqa: BLE001 — an unloadable config still exports its hash
            tasks_file = config_file.parent / BEADS_SOURCE
    return Node(root=root, config_file=config_file, store_dir=tasks_file.parent,
                tasks_file=tasks_file)


# --------------------------------------------------------------------------- #
# Environment, doctor
# --------------------------------------------------------------------------- #


def probe_tool(name: str) -> dict:
    """Presence and a bare version token — never the binary's path."""
    exe = shutil.which(name)
    if not exe:
        return {"present": False, "version": None}
    version = None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        match = _VERSION.search((proc.stdout or "") + " " + (proc.stderr or ""))
        version = match.group(0)[:40] if match else None
    except (OSError, subprocess.SubprocessError):
        version = None
    return {"present": True, "version": version}


def _dist_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001
        return None


def _environment(node: Node, tool_probe: Callable[[str], dict]) -> dict:
    from . import __version__

    config: dict = {"present": False, "name": None, "sha256": None, "bytes": None}
    if node.config_file is not None:
        data = node.config_file.read_bytes()
        config = {"present": True, "name": node.config_file.name,
                  "sha256": _sha256(data), "bytes": len(data)}
    return {
        "harness_version": __version__,
        "package_version": _dist_version("agentco-harness"),
        "asop_spec_version": _dist_version("asop-spec"),
        "python": {"version": platform.python_version(),
                   "implementation": platform.python_implementation()},
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine()},
        "tools": {name: tool_probe(name) for name in ("claude", "uv")},
        "config": config,
    }


@contextmanager
def _shadow(node: Node) -> Iterator[Path]:
    """A throwaway mirror of the config dir: real dirs down to the store,
    every other existing entry symlinked, the config file copied (Config.load
    resolves symlinks, so a linked config would point doctor back at the
    real node). Doctor's touch-if-missing writes land here."""
    base = node.config_file.parent if node.config_file else node.root
    base = base.absolute()
    store = node.store_dir.absolute()
    chain = {base}
    probe = store
    while probe != base and base in probe.parents:
        chain.add(probe)
        probe = probe.parent
    with tempfile.TemporaryDirectory(prefix="agentic-co-audit-doctor-") as tmp:
        root = Path(tmp) / "node"

        def mirror(src: Path, dst: Path) -> None:
            dst.mkdir()
            for entry in sorted(src.iterdir()):
                target = dst / entry.name
                if node.config_file and entry.absolute() == node.config_file.absolute():
                    shutil.copy2(entry, target)
                elif entry.absolute() in chain and entry.is_dir() and not entry.is_symlink():
                    mirror(entry, target)
                else:
                    os.symlink(entry.absolute(), target)

        mirror(base, root)
        yield root


def _doctor(node: Node) -> tuple[dict, _FileReport]:
    report = _FileReport()
    base = (node.config_file.parent if node.config_file else node.root).absolute()
    if base != node.store_dir.absolute() and base not in node.store_dir.absolute().parents:
        return {"status": "skipped", "reason": "store_outside_config_dir"}, report
    from . import doctor as doctor_mod
    from .config import Config

    prev = os.getcwd()
    try:
        with _shadow(node) as shadow:
            config_arg = str(shadow / (node.config_file.name if node.config_file else "config.yaml"))
            os.chdir(shadow)
            try:
                sink = io.StringIO()
                with redirect_stdout(sink), redirect_stderr(sink):
                    tasks = Path(Config.load(config_arg).tasks_path)
                    if not tasks.is_absolute():
                        tasks = shadow / tasks
                    here = shadow.resolve()
                    if tasks.parent.resolve() != here and here not in tasks.parent.resolve().parents:
                        return {"status": "skipped", "reason": "tasks_path_outside_node"}, report
                    result = doctor_mod.collect(config_arg)
            finally:
                os.chdir(prev)  # leave the shadow before it is deleted
    except Exception as e:  # noqa: BLE001 — the export must survive a broken doctor
        return {"status": "error", "error_type": type(e).__name__}, report
    findings = []
    for f in result.findings:
        findings.append({"class": f.status, "check": f.check})
        report.dropped["findings[].message"] += 1
    return {"status": "ok", "exit_code": result.exit_code(), "counts": result.counts(),
            "findings": findings}, report


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


@dataclass
class ExportResult:
    bundle_dir: Path
    tarball: Path
    manifest: dict


def _jsonl(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=True,
                              separators=(",", ":")) + "\n" for r in rows).encode("ascii")


def _json(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def _count_rows(rel: str, data: bytes) -> int:
    if rel.endswith(".jsonl"):
        return sum(1 for line in data.split(b"\n") if line.strip())
    return 1 if data.strip() else 0


def _scrub_roots(node: Node) -> list[str]:
    roots = {str(Path.home())}
    for p in (node.root, node.store_dir):
        roots.update({str(p.absolute()), str(p.resolve())})
    return sorted(r for r in roots if r and r != os.sep and len(r) > 1)


def _bundle_name(node: Node, now: datetime) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", node.root.name or "node")[:64]
    return f"agentic-co-audit-{name}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def export(
    node: Node,
    *,
    out_dir: Path,
    since: Optional[datetime] = None,
    now: Optional[datetime] = None,
    tool_probe: Optional[Callable[[str], dict]] = None,
) -> ExportResult:
    """Write a metadata-only bundle (directory + .tar.gz) and return it."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    out_dir = Path(out_dir).expanduser().absolute()
    bundle_dir = out_dir / _bundle_name(node, now)
    for inside in (node.store_dir, node.root):
        if inside.resolve() == bundle_dir.resolve() or inside.resolve() in bundle_dir.resolve().parents:
            raise AuditError(
                f"refusing to write the bundle inside the node ({inside}) — the export "
                f"is read-only against the node; pass --out elsewhere"
            )
    if bundle_dir.exists():
        raise AuditError(f"{bundle_dir} already exists")

    roots = _scrub_roots(node)
    files: dict[str, bytes] = {}
    redaction: dict[str, dict] = {}
    sources: dict[str, dict] = {}

    def absent(source: str) -> None:
        sources[source] = {"present": False, "bundle_path": None, "rows_read": None,
                           "corrupt_lines": None, "exported": None,
                           "excluded_by_since": None, "since_unparseable_kept": None}

    def ledger(spec: _Ledger, path: Path) -> Optional[list[dict]]:
        if not path.is_file():
            absent(spec.source)
            return None
        raw, corrupt = _read_rows(path, _validator(spec.source), spec.single_object)
        kept, excluded, unparseable = (
            _since_split(raw, spec.since_field, since) if spec.since_field else (raw, None, None)
        )
        rep = _FileReport()
        reducer = _Reducer(roots, rep)
        rows = [reducer.reduce(r, spec.shape) for r in kept]
        if spec.sort:
            rows.sort(key=spec.sort)
        if spec.single_object:
            files[spec.bundle] = _json(rows[0]) if rows else b""
        else:
            files[spec.bundle] = _jsonl(rows)
        redaction[spec.bundle] = rep.as_dict(corrupt)
        sources[spec.source] = {"present": True, "bundle_path": spec.bundle,
                                "rows_read": len(raw) + corrupt, "corrupt_lines": corrupt,
                                "exported": len(rows), "excluded_by_since": excluded,
                                "since_unparseable_kept": unparseable}
        return raw

    for spec in LEDGERS:
        ledger(spec, node.store_dir / spec.source)

    beads_spec = _Ledger(BEADS_SOURCE, BEADS_BUNDLE, "beads", "updated_at",
                         sort=lambda r: (str(r.get("id", "")),))
    raw_beads = ledger(beads_spec, node.tasks_file)
    if raw_beads is not None:
        rep = _FileReport()
        reducer = _Reducer(roots, rep)
        refusals = []
        for bead in raw_beads:
            meta = bead.get("metadata")
            record = meta.get("dispatch_refusal") if isinstance(meta, dict) else None
            if not isinstance(record, dict):
                continue
            row = reducer.reduce(record, "dispatch_refusal")
            row["bead_id"] = reducer.reduce({"id": bead.get("id")}, "beads").get("id")
            row["kind"] = "dispatch_refusal"
            refusals.append(row)
        refusals, _, _ = _since_split(refusals, "at", since)
        refusals.sort(key=lambda r: (str(r.get("at") or ""), str(r.get("bead_id") or "")))
        files[REFUSALS_BUNDLE] = _jsonl(refusals)
        redaction[REFUSALS_BUNDLE] = rep.as_dict(None)

    files["environment.json"] = _json(_environment(node, tool_probe or probe_tool))
    doctor, doctor_rep = _doctor(node)
    files["doctor.json"] = _json(doctor)
    redaction["doctor.json"] = doctor_rep.as_dict(None)

    from . import __version__

    manifest = {
        "schema": SCHEMA,
        "tier": TIER,
        "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "harness_version": __version__,
        "since": since.astimezone(timezone.utc).isoformat() if since else None,
        "node": node.root.name,
        "files": [
            {"path": rel, "sha256": _sha256(data), "bytes": len(data),
             "rows": _count_rows(rel, data)}
            for rel, data in sorted(files.items())
        ],
        "sources": dict(sorted(sources.items())),
        "redaction": dict(sorted(redaction.items())),
        "refusal_coverage": REFUSAL_COVERAGE,
    }
    files[MANIFEST] = _json(manifest)

    bundle_dir.mkdir(parents=True)
    for rel, data in sorted(files.items()):
        target = bundle_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    tarball = out_dir / (bundle_dir.name + ".tar.gz")
    _write_tarball(tarball, bundle_dir.name, files, int(now.timestamp()))
    return ExportResult(bundle_dir=bundle_dir, tarball=tarball, manifest=manifest)


def _write_tarball(path: Path, top: str, files: dict[str, bytes], mtime: int) -> None:
    """Sorted members, normalised owners and times — same input, same archive."""
    with open(path, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=mtime) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel, data in sorted(files.items()):
                info = tarfile.TarInfo(f"{top}/{rel}")
                info.size = len(data)
                info.mtime = mtime
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #


def _bundle_files(bundle: Path) -> tuple[dict[str, bytes], list[str]]:
    problems: list[str] = []
    files: dict[str, bytes] = {}
    if bundle.is_dir():
        for p in sorted(bundle.rglob("*")):
            rel = p.relative_to(bundle).as_posix()
            if p.is_symlink():
                problems.append(f"{rel}: symlink in bundle")
            elif p.is_file():
                files[rel] = p.read_bytes()
        return files, problems
    try:
        with tarfile.open(bundle, mode="r:gz") as tar:
            for m in tar.getmembers():
                if m.isdir():
                    continue
                parts = m.name.split("/")
                if not m.isfile() or m.name.startswith("/") or ".." in parts or len(parts) < 2:
                    problems.append(f"{m.name}: unexpected archive member")
                    continue
                extracted = tar.extractfile(m)
                files["/".join(parts[1:])] = extracted.read() if extracted else b""
    except (OSError, tarfile.TarError, EOFError) as e:
        problems.append(f"unreadable archive ({type(e).__name__})")
    return files, problems


def verify(bundle: Path) -> tuple[bool, list[str]]:
    """Recompute every hash, byte count and row count. (ok, problems)."""
    bundle = Path(bundle).expanduser()
    if not bundle.exists():
        return False, [f"{bundle} does not exist"]
    files, problems = _bundle_files(bundle)
    raw = files.pop(MANIFEST, None)
    if raw is None:
        return False, problems + ["manifest.json missing"]
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False, problems + ["manifest.json is not valid JSON"]
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        return False, problems + [f"manifest schema is not {SCHEMA}"]
    listed = set()
    for entry in manifest.get("files") or []:
        rel = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(rel, str):
            problems.append("malformed file entry in manifest")
            continue
        listed.add(rel)
        data = files.get(rel)
        if data is None:
            problems.append(f"{rel}: missing")
            continue
        if _sha256(data) != entry.get("sha256"):
            problems.append(f"{rel}: sha256 mismatch")
        if len(data) != entry.get("bytes"):
            problems.append(f"{rel}: byte count mismatch")
        if _count_rows(rel, data) != entry.get("rows"):
            problems.append(f"{rel}: row count mismatch")
    for rel in sorted(set(files) - listed):
        problems.append(f"{rel}: not listed in manifest")
    return not problems, problems


__all__ = [
    "AuditError", "DENY", "KEEP", "LEDGERS", "POLICY", "REFUSAL_COVERAGE", "SCHEMA",
    "ExportResult", "Node", "export", "parse_since", "probe_tool", "resolve_node", "verify",
]
