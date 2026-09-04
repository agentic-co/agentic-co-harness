"""Preflight health checks, classified by consequence.

`run_doctor` walks a series of checks against a config and the runtime
environment, recording one **finding** per check. Every finding declares a
*consequence class* — what is actually true of the system if this check is
red — and the exit code is derived from the classes present, never from an
undifferentiated failure counter.

    BROKEN    something that should be working is not: data loss, silent
              non-execution, a lane that cannot claim, a stale heartbeat.
    DEGRADED  working, but with reduced capability or an unverified
              assumption (a missing optional key, an unverified vendor term,
              a check that could not run).
    INFO      advisory. True, worth printing, gates nothing.

    exit 0 — all clear (only OK/INFO findings)
    exit 1 — at least one BROKEN
    exit 2 — DEGRADED present, no BROKEN

The precedence is stated as a table (``_EXIT_PRECEDENCE``) rather than a
``max()`` over codes precisely so that **a BROKEN can never be masked** — not
by twenty INFO lines, not by a DEGRADED with a numerically larger code, and
not by ``--class`` filtering, which changes what is *printed* and never what
is *returned*. Severity conflation is the defect this module exists to
remove: the previous aggregate mixed a missing optional API key with a
dependency cycle, so nothing could gate on doctor and operators learned to
skim it. Designed 2026-08-08, shipped after the 8h30m Recorro outage that a
consequence-classed check would have caught.

The marquee check is (c): an *enabled* source in config that has no
implementation in ``agentco_harness.sources.SOURCES``. That exact defect silently
killed the Recorro deployment — a source was promised in config, the loop
happily no-op'd it, and nobody noticed the observation had stopped. Doctor
turns that silence into a BROKEN.
"""

from __future__ import annotations

import json as _json_mod
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# --- consequence classes ----------------------------------------------------
#: A check that is healthy. Not a finding; printed so the operator can see the
#: check ran at all (a check nobody can see run is a check nobody trusts).
OK = "ok"
#: Advisory. True and worth printing; gates nothing.
INFO = "info"
#: Working with reduced capability, or an assumption this run could not verify.
DEGRADED = "degraded"
#: Something that should be working is not.
BROKEN = "broken"

#: Every class a finding may carry, most severe first.
CLASSES: tuple[str, ...] = (BROKEN, DEGRADED, INFO, OK)

# --- exit codes --------------------------------------------------------------
EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_DEGRADED = 2
#: Class-specific subscription code for `agentco schedules audit`, which
#: reports exactly one class and therefore cannot use the aggregate table.
#: Re-homed here from `schedules.py` so the whole class→code mapping lives in
#: one file; `schedules` imports it.
EXIT_LIVENESS = 4

#: Ordered highest-consequence-first. Iterated, never maximised: EXIT_BROKEN
#: (1) is numerically SMALLER than EXIT_DEGRADED (2), so a `max()` over codes
#: would silently let a degraded check outrank a broken one.
_EXIT_PRECEDENCE: tuple[tuple[str, int], ...] = (
    (BROKEN, EXIT_BROKEN),
    (DEGRADED, EXIT_DEGRADED),
)

#: JSON envelope version, for consumers that pin.
JSON_SCHEMA = "agentco_harness.doctor/1"


def exit_code_for(statuses) -> int:
    """Aggregate exit code for a set of finding classes.

    BROKEN wins over everything. DEGRADED wins over INFO/OK. INFO and OK are
    exit 0 — an advisory that could change a deployment gate is not advisory.
    """
    present = set(statuses)
    for cls, code in _EXIT_PRECEDENCE:
        if cls in present:
            return code
    return EXIT_OK


@dataclass(frozen=True)
class Finding:
    """One check's verdict: its class, its stable id, and what it said."""

    status: str
    check: str
    message: str

    def to_dict(self) -> dict:
        return {"class": self.status, "check": self.check, "message": self.message}

    def render(self) -> str:
        return f"[doctor] {self.status.upper()} ({self.check}): {self.message}"


class DoctorReport:
    """Ordered findings plus the class-derived verdict.

    The report holds ALL findings regardless of any display filter. Filtering
    happens at render time only, so `--class info` cannot turn a broken node
    green — the single most important property here.
    """

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, status: str, check: str, message: str) -> Finding:
        if status not in CLASSES:
            raise ValueError(f"unknown consequence class {status!r} (want one of {CLASSES})")
        finding = Finding(status=status, check=check, message=message)
        self.findings.append(finding)
        return finding

    def of_class(self, *statuses: str) -> list[Finding]:
        wanted = set(statuses)
        return [f for f in self.findings if f.status in wanted]

    def counts(self) -> dict[str, int]:
        return {cls: sum(1 for f in self.findings if f.status == cls) for cls in CLASSES}

    def exit_code(self) -> int:
        """Derived from every finding — never from the filtered view."""
        return exit_code_for(f.status for f in self.findings)

    def render(self, classes=None) -> str:
        selected = self.findings if not classes else self.of_class(*classes)
        lines = [f.render() for f in selected]
        counts = self.counts()
        summary = (
            f"[doctor] verdict: exit {self.exit_code()} — "
            + ", ".join(f"{counts[c]} {c}" for c in CLASSES)
        )
        if classes and counts[BROKEN] and BROKEN not in set(classes):
            # The filter hid a broken check. Say so, loudly, in the filtered
            # output itself: a human who filtered to `info` must not walk away
            # believing they saw everything that matters.
            summary += (
                f" (WARNING: {counts[BROKEN]} broken finding(s) hidden by "
                f"--class {','.join(sorted(classes))})"
            )
        lines.append(summary)
        return "\n".join(lines)

    def to_json(self, classes=None) -> str:
        selected = self.findings if not classes else self.of_class(*classes)
        return _json_mod.dumps(
            {
                "schema": JSON_SCHEMA,
                "exit_code": self.exit_code(),
                "counts": self.counts(),
                "filtered_to": sorted(classes) if classes else None,
                "findings": [f.to_dict() for f in selected],
            },
            indent=2,
        )


# Required third-party imports. Missing one of these is a hard FAIL — the
# system cannot function without them.
REQUIRED_IMPORTS = ("dspy", "yaml", "click")

# Map of LLM provider -> environment variable holding its API key. Providers
# not listed (ollama, lmstudio) run locally and need no key.
PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
LOCAL_PROVIDERS = {"ollama", "lmstudio"}

# Map of dispatchable agent name -> env var holding its key. Agents absent here
# (claude, and the store-backed paths) authenticate through their own CLI login
# rather than an env var, so there is nothing for doctor to probe.
AGENT_ENV = {
    "zai": "ZAI_API_KEY",
}

# How many times a bead may be handed out before the churn itself is the defect
# (ac-48d8aba3). `lease_attempt` is monotonic and never reset, so this counts
# every claim the bead has ever taken. Three is the point where "a laptop shut
# its lid twice" stops being the likely story.
LEASE_ATTEMPT_THRESHOLD = 3

# Hours of silence from a worker after which `agentco pull` arms the
# reconcile-before-replay guard (ac-48d8aba3). Six hours is longer than any
# normal poll gap and shorter than a closed-laptop overnight, which is the
# shortest absence that can hide a landed-but-unreported external write.
DEFAULT_RECONCILE_AFTER_H = 6.0


LAUNCH_AGENTS_DIR_ENV = "AGENTCO_LAUNCH_AGENTS_DIR"


def launch_agents_dir() -> Path:
    """Where this host keeps user LaunchAgents.

    Overridable so the scheduler check can be exercised against a fixture
    directory instead of the developer's real `~/Library/LaunchAgents`, which
    is shared machine state no test may depend on.
    """
    import os

    override = os.environ.get(LAUNCH_AGENTS_DIR_ENV)
    return Path(override) if override else Path.home() / "Library" / "LaunchAgents"


def launch_agent_jobs(agents_dir: Path) -> list[tuple[str, str]]:
    """(Label, raw plist text) for every parseable LaunchAgent in `agents_dir`.

    The raw text — not the parsed keys — is what the scheduler matcher searches,
    because the fleet's plists reference their node from three different places
    (`WorkingDirectory`, a wrapper script in `ProgramArguments`, a log path) and
    a key-by-key matcher would have to guess which. Unparseable files are
    skipped rather than reported: `~/Library/LaunchAgents` is shared with every
    other tool on the machine, and their malformed plists are not our finding.
    """
    jobs: list[tuple[str, str]] = []
    if not agents_dir.is_dir():
        return jobs
    import plistlib

    for path in sorted(agents_dir.glob("*.plist")):
        try:
            label = plistlib.loads(path.read_bytes()).get("Label", path.stem)
            jobs.append((str(label), path.read_text(errors="ignore")))
        except Exception:  # noqa: BLE001 — someone else's broken plist
            continue
    return jobs


def loaded_launchd_labels() -> set[str]:
    """Labels launchd currently has bootstrapped for this user.

    A plist sitting on disk unloaded produces exactly the silence a missing one
    does, so presence alone is not the assertion worth making.
    """
    import subprocess

    out = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=15
    )
    return {
        parts[2].strip()
        for parts in (line.split("\t") for line in out.stdout.splitlines()[1:])
        if len(parts) >= 3
    }


def scheduling_jobs_for(
    instance_dir: Path,
    jobs: list[tuple[str, str]],
    loaded: set[str],
) -> list[str]:
    """Loaded job labels that reference this AgentCo instance directory.

    Matches the instance dir and, when it is a `.agentco` subdir, its parent —
    the fleet legitimately schedules both shapes (`agentco cycle` with
    WorkingDirectory at the instance, or a wrapper script run from the repo
    root).
    """
    candidates = {str(instance_dir)}
    if instance_dir.name == ".agentco":
        candidates.add(str(instance_dir.parent))
    return sorted(
        label
        for label, text in jobs
        if label in loaded and any(c in text for c in candidates)
    )


def unresolved_for_worker(tasks, agent: str) -> list:
    """Beads this worker may have half-finished — the reconcile set.

    A live bead qualifies when it has been claimed at least once
    (``lease_attempt > 0``) AND this worker was the last party to hold it:
    either it still does (``leased_by``), or its lease was reaped and the
    routing preference still names it (``assigned_agent`` with the lease
    cleared).

    Both halves matter. Only checking ``leased_by`` misses the important case
    entirely — a worker offline long enough to matter is a worker whose leases
    have EXPIRED, and a reaped bead carries no holder at all. Only checking
    ``assigned_agent`` would sweep in beads that were merely routed to this
    worker and never handed over, which it cannot have side-effected.

    ``lease_attempt > 0`` is what separates "was actually dispatched once" from
    "is merely addressed to this worker", and reaping deliberately preserves
    that counter, so it survives exactly the event that clears the holder.

    Terminal beads are excluded: their answer is already recorded.
    """
    from .beads import TaskStatus

    terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED}
    return [
        t
        for t in tasks
        if t.status not in terminal
        and t.lease_attempt > 0
        and (
            t.leased_by == agent
            or (t.leased_by is None and t.assigned_agent == agent)
        )
    ]


def lease_pathologies(tasks, now, attempt_threshold: int = LEASE_ATTEMPT_THRESHOLD) -> dict:
    """Classify the three ways the cross-machine lease protocol goes wrong.

    Pure function over a task list — no config, no I/O — so the classification
    is testable directly and doctor's job is reduced to printing it.

    The three buckets are genuinely different failures, which is why they are
    not one count:

    * **expired_unreaped** — IN_PROGRESS past its expiry. `reap_expired_leases`
      returns it to PENDING, and runs from `agentco pull` (cli.py) AND from
      `Orchestrator.cycle` (ac-fb137d8d), so a hub nobody pulls from still
      recovers on its own heartbeat. That narrows the window; it does not close
      it. Between heartbeats the bead is stopped dead and holds its blockers,
      and an operator running doctor mid-incident is looking at exactly that
      window — so this stays a FAIL rather than deferring to the next cycle.
    * **corrupt_expiry** — leased with an expiry that does not parse, or with
      no expiry at all. `reap_expired_leases` SKIPS these on purpose ("left
      alone deliberately rather than reclaimed on a guess"), and `ready()`
      excludes them, so no automatic path can ever touch them again. They are
      unreachable until a human edits the store — which is exactly the class of
      thing that has to be surfaced somewhere, and this is the somewhere.
    * **churning** — `lease_attempt` over the threshold while still live. The
      bead IS moving, so nothing is stuck; it is being handed out, abandoned,
      handed out again. That burns a worker session per round and looks like
      ordinary throughput from every other angle.

    Only live beads are classified. A DONE bead that took five attempts is
    history: it finished, and re-reporting it forever would be a permanently
    red doctor, which trains an operator to stop reading doctor at all.
    """
    from .beads import TaskStatus, _parse_iso

    terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED}
    expired_unreaped, corrupt_expiry, churning = [], [], []

    for task in tasks:
        if task.status in terminal:
            continue
        if task.status == TaskStatus.IN_PROGRESS and task.leased_by:
            expires = _parse_iso(task.lease_expires_at)
            if expires is None:
                # Either unparseable or absent. Both land in the same hole:
                # reap cannot reason about the expiry, so it walks past.
                corrupt_expiry.append(task)
            elif not task.lease_active_at(now):
                expired_unreaped.append(task)
        if task.lease_attempt > attempt_threshold:
            churning.append(task)

    return {
        "expired_unreaped": expired_unreaped,
        "corrupt_expiry": corrupt_expiry,
        "churning": churning,
        "threshold": attempt_threshold,
    }


def collect(config_path: str) -> DoctorReport:
    """Run every check and return the classified report.

    Separated from `run_doctor` so callers that want the findings — the CLI's
    `--json`, Pulse, a scheduler — get structured data rather than parsed
    stdout. `run_doctor` is this function plus rendering plus the exit code.
    """
    report = DoctorReport()

    # The check id attached to every finding recorded from here until the next
    # `_sec` call. One mutable cell rather than an argument on sixty call sites:
    # the sections are strictly sequential, and a stable id per section is what
    # makes `--class` output and the JSON envelope subscribable.
    _section = ["startup"]

    def _sec(name: str) -> None:
        _section[0] = name

    def _ok(msg: str) -> None:
        report.add(OK, _section[0], msg)

    def _info(msg: str) -> None:
        report.add(INFO, _section[0], msg)

    # `_warn` and `_fail` are retained as the DEGRADED/BROKEN recorders so the
    # class of every historical call site is explicit at the site rather than
    # implied by a rename: a check reclassified in this backport calls
    # `_broken`/`_degraded`/`_info` directly and carries the reasoning inline.
    def _warn(msg: str) -> None:
        report.add(DEGRADED, _section[0], msg)

    _degraded = _warn

    def _fail(msg: str) -> None:
        report.add(BROKEN, _section[0], msg)

    _broken = _fail

    # (a) Python version >= 3.11
    _sec("python.version")
    if sys.version_info >= (3, 11):
        _ok(f"Python {sys.version_info.major}.{sys.version_info.minor} >= 3.11")
    else:
        _fail(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is too old "
            f"— AgentCo requires >= 3.11"
        )

    # (b) Required imports available
    _sec("imports.required")
    import importlib

    for mod in REQUIRED_IMPORTS:
        try:
            importlib.import_module(mod)
            _ok(f"import '{mod}' available")
        except ImportError as e:
            _fail(f"required import '{mod}' is missing ({e}) — install dependencies")

    # Config load drives the remaining checks.
    from .config import CONSUMED_AGENT_KEYS, DEFAULT_ENV_FILE, Config

    config = Config.load(config_path)

    # (d) Unconsumed agent-settings keys (WARN). Compare each agent's settings
    # keys against CONSUMED_AGENT_KEYS (minus 'model', which lives on its own
    # field, not in settings).
    _sec("config.unconsumed_keys")
    consumed_settings_keys = CONSUMED_AGENT_KEYS - {"model"}
    any_unconsumed = False
    for name, agent in config.agents.items():
        unconsumed = set(agent.settings) - consumed_settings_keys
        if unconsumed:
            any_unconsumed = True
            _warn(
                f"agent '{name}' has settings nothing consumes: "
                f"{', '.join(sorted(unconsumed))} "
                f"(consumed: {', '.join(sorted(consumed_settings_keys))})"
            )
    if not any_unconsumed:
        _ok("no unconsumed agent-settings keys")

    # (e) tasks.jsonl parses. Use the same tolerant approach as Beads._read_all:
    # each non-blank line must parse via Task.from_json; bad lines are reported
    # with line numbers.
    #
    # RECLASSIFIED to BROKEN in the consequence-class backport. A quarantined
    # line is not a cosmetic parse complaint: that bead is in the store, is not
    # in the queue, and will never be dispatched, retried, or reported on. That
    # is silent non-execution of work someone filed — the exact class this
    # module exists to make loud — and calling it a warning is how it stayed
    # invisible.
    _sec("store.tasks_parse")
    from .beads import Task

    tasks_path = Path(config.tasks_path)
    if not tasks_path.exists():
        _ok(
            f"tasks file {tasks_path} does not exist yet "
            f"(will be created on first use)"
        )
    else:
        bad_lines: list[int] = []
        total = 0
        with open(tasks_path) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    Task.from_json(line)
                except (ValueError, KeyError, TypeError):
                    bad_lines.append(lineno)
        if bad_lines:
            _broken(
                f"{len(bad_lines)} of {total} task line(s) in {tasks_path} "
                f"are unparseable (lines: "
                f"{', '.join(str(n) for n in bad_lines)}) "
                f"— they will be quarantined, not executed"
            )
        else:
            _ok(f"all {total} task line(s) in {tasks_path} parse cleanly")

        # (e2) dependency cycles. `update()` refuses to close one, but data
        # written before that guard existed — or hand-edited JSONL — can still
        # contain a loop. A cycle is a SILENT deadlock: every member waits on
        # another forever, so none ever satisfies ready(), nothing dispatches,
        # and nothing is stale or errored to signal it. Exactly the class of
        # failure doctor exists to make loud.
        _sec("store.dependency_cycles")
        from .beads import Beads, TaskStatus
        from .tempo import topo_order

        try:
            live = [
                t
                for t in Beads(tasks_path).list()
                if t.status
                not in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED)
            ]
            _, cyclic = topo_order(live)
            if cyclic:
                _fail(
                    f"{len(cyclic)} task(s) are in a dependency cycle and can "
                    f"NEVER become ready: {', '.join(cyclic)} — break one edge "
                    f"with `agentco tasks update <id> --clear-blocked-by`"
                )
            else:
                _ok("no dependency cycles in the bead graph")
        except Exception as e:  # noqa: BLE001 — doctor must never crash
            _warn(f"could not check for dependency cycles: {e}")

    # (f) company/ directory existence.
    #
    # RECLASSIFIED to BROKEN. Its own message says agent documents will be
    # DISCARDED — that is data loss on every document-producing bead, stated in
    # the warning text and then filed under the same class as an unverified
    # vendor term. Either the message is wrong or the class was; the message is
    # right.
    _sec("company.dir")
    company_dir = Path("company")
    if company_dir.is_dir():
        _ok("company/ directory exists")
    else:
        _broken(
            "company/ directory is missing — agent documents will be DISCARDED. "
            "Run 'agentco init --company' to create it."
        )

    # (g) recurring.jsonl validates: schema and parseable durations.
    #
    # RECLASSIFIED to BROKEN, for the same reason as (e): a quarantined
    # recurring definition is never scheduled, so a schedule the operator
    # believes is running is not running and nothing else says so.
    _sec("store.recurring_parse")
    from .recurring import Recurring

    recurring_path = Path(config.recurring_path)
    recurring_defs = []
    if not recurring_path.exists():
        _ok("no recurring.jsonl (no recurring tasks defined)")
    else:
        store = Recurring(recurring_path)
        recurring_defs = store.list()
        if store._quarantined:
            _broken(
                f"{len(store._quarantined)} recurring definition line(s) in "
                f"{recurring_path} are unparseable — they will be quarantined, "
                f"never scheduled"
            )
        else:
            _ok(f"all {len(recurring_defs)} recurring definition(s) parse cleanly")

    # (g2) Schedule liveness (backport 3, wired here by backport 4). A recurring
    # definition that parses is not a schedule that fires: F5 ran 10+ periods
    # silent while every other signal — the def parsed, the daemon heartbeated,
    # the queue drained — stayed green. `schedules.audit_node` joins the
    # declared registry against observed firings (ledger reservations plus
    # firings reconstructed from the bead store) over a trailing window.
    #
    # CLASS: BROKEN, and this is the whole point of wiring it in. A schedule
    # that expected N firings and produced ZERO is silent non-execution of
    # declared work — nothing raised, nothing failed, nothing is stale enough to
    # notice. `agentco schedules audit` keeps its own single-class exit
    # (EXIT_LIVENESS) for consumers that subscribe to liveness alone; here it
    # joins the aggregate as BROKEN, because a human running doctor is asking
    # "is anything not working" and the answer is yes.
    _sec("schedules.liveness")
    try:
        from . import schedules as schedules_mod

        audits = schedules_mod.audit_node(config)
        silent = [a for a in audits if a.silent]
        if not audits:
            _ok("no enabled schedules declared (nothing to expect)")
        elif silent:
            _broken(
                f"{len(silent)} of {len(audits)} declared schedule(s) have STOPPED "
                "FIRING: "
                + "; ".join(f"{a.schedule.id} ({a.reason})" for a in silent[:5])
                + (f" (+{len(silent) - 5} more)" if len(silent) > 5 else "")
                + " — the definition is enabled and parses, so nothing else "
                "reports this. See `agentco schedules audit`."
            )
        else:
            _ok(
                f"all {len(audits)} enabled schedule(s) firing on cadence "
                f"({sum(a.observed for a in audits)} observed firing(s) in window)"
            )
    except Exception as e:  # noqa: BLE001 — doctor must never crash the CLI
        _warn(f"schedule liveness check could not run: {e}")

    # (h) children registry ↔ recurring defs agree. Drift is a loud doctor
    # FAIL: a registered child with no verify_child def is silently
    # unmonitored — the portfolio-scale Recorro defect.
    _sec("children.registry_sync")
    from .children import ChildRegistry, child_heartbeat_path, verify_child as _verify

    registry = ChildRegistry(config.children_registry_path)
    children = registry.list()
    if not children and not any(
        d.payload.get("type") == "verify_child" for d in recurring_defs
    ):
        _ok("no children registered (leaf instance)")
    else:
        verify_targets = {
            d.payload.get("child")
            for d in recurring_defs
            if d.payload.get("type") == "verify_child" and d.enabled
        }
        # Only children whose liveness is actually observable need a verify def.
        # An ado-backed, vault-only, or `manual`-cadence child has no heartbeat
        # to poll, so demanding one would be a permanent FAIL for a correctly
        # configured registry — the mirror image of the bug that hid them.
        child_names = {c.name for c in children if c.verifiable}
        # Remote children are excluded from "not pollable" since ac-48d8aba3:
        # check (u) below polls them through the pull ledger, so listing them
        # here as unmonitored would contradict the check that now monitors them.
        unverifiable = [
            c.name for c in children if not c.verifiable and not c.is_remote
        ]
        if unverifiable:
            _ok(
                f"{len(unverifiable)} child(ren) present but not pollable "
                f"(no heartbeat by design): {', '.join(sorted(unverifiable))}"
            )
        unmonitored = child_names - verify_targets
        orphaned = verify_targets - child_names
        if unmonitored:
            _fail(
                f"registered child(ren) with NO verify_child recurring def: "
                f"{', '.join(sorted(unmonitored))} — they are silently unmonitored. "
                f"Re-link with 'agentco link-child'."
            )
        if orphaned:
            _fail(
                f"verify_child def(s) naming unregistered child(ren): "
                f"{', '.join(sorted(str(o) for o in orphaned))} — every cycle will "
                f"fail these beads. Registry and recurring defs have drifted."
            )
        if not unmonitored and not orphaned and children:
            _ok(f"registry ↔ recurring defs in sync for {len(children)} child(ren)")

        # (i) Each child path exists and looks like an AgentCo instance.
        #
        # RECLASSIFIED to BROKEN. All three branches describe a node that is
        # registered as monitored and is not being monitored: a path that does
        # not exist, a directory that is not an instance, or an instance whose
        # heartbeat has gone stale. A stale heartbeat is named explicitly in
        # the BROKEN definition — it is the portfolio-scale form of the Recorro
        # silence, and it looked exactly like a missing optional API key.
        _sec("children.instances")
        for child in children:
            if not child.verifiable:
                # No local instance dir to inspect (ado-backed / vault-only /
                # manual). Already reported above as present-but-not-pollable.
                continue
            child_dir = Path(child.path)
            if not child_dir.is_dir():
                _broken(
                    f"child '{child.name}' path {child_dir} does not exist "
                    f"— it is registered as monitored and cannot be verified"
                )
            elif not (child_dir / "config.yaml").exists():
                _broken(
                    f"child '{child.name}' at {child_dir} has no config.yaml "
                    f"— is it an AgentCo instance? Its liveness is unobservable."
                )
            else:
                result = _verify(child)
                if result["level"] == "fail":
                    _broken(f"child '{child.name}': {result['detail']}")
                else:
                    _ok(f"child '{child.name}': {result['detail']}")

        # (i2) Each pollable child has a SCHEDULER of its own.
        #
        # BROKEN. Check (h) proves the parent is watching the child; nothing
        # proved anything makes the child *cycle*. `agentco add-company` links
        # a child into this registry — which starts the staleness clock — but
        # installs no launchd job, so a freshly onboarded node is monitored and
        # unscheduled: it heartbeats once (the onboarding cycle), then goes
        # stale on schedule and fires a verify_child alarm that looks like a
        # defect in the child. That is the semijoias incident (ac-67fbc23f):
        # node created 2026-08-29 10:00 local, first and only cycle 10:04,
        # LaunchAgent hand-written 16:57 — a 6.9h gap with a correct alarm and
        # no cause inside the child. This check names the gap at onboarding
        # instead of six hours later, and it is the create-side twin of the
        # standing rule that migrating a node means migrating its scheduler.
        _sec("children.scheduler")
        schedulable = [c for c in children if c.verifiable and not c.is_remote]
        if not schedulable:
            _ok("no locally scheduled children to check")
        elif sys.platform != "darwin":
            _info(
                f"scheduler presence unchecked for {len(schedulable)} child(ren) "
                f"— launchd inspection is macOS-only on this host"
            )
        else:
            try:
                jobs = launch_agent_jobs(launch_agents_dir())
                loaded = loaded_launchd_labels()
                for child in schedulable:
                    instance_dir = Path(child.path)
                    labels = scheduling_jobs_for(instance_dir, jobs, loaded)
                    if labels:
                        _ok(
                            f"child '{child.name}' scheduled by "
                            f"{', '.join(labels[:3])}"
                        )
                    else:
                        _broken(
                            f"child '{child.name}' at {instance_dir} has NO loaded "
                            f"launchd job referencing it — it is registered as "
                            f"monitored every {child.expected_interval} but nothing "
                            f"makes it cycle, so its heartbeat WILL go stale. "
                            f"Install a LaunchAgent running 'agentco cycle' with "
                            f"WorkingDirectory={instance_dir}, then "
                            f"'launchctl bootstrap gui/$(id -u) <plist>'."
                        )
            except Exception as e:  # noqa: BLE001 — doctor must never crash the CLI
                _warn(f"child scheduler check could not run: {e}")

    # (j) Own heartbeat staleness.
    #
    # RECLASSIFIED to BROKEN. A heartbeat past its own backoff deadline means
    # the daemon is not cycling: nothing is being claimed, nothing dispatched,
    # nothing reaped, and every queued bead is sitting still. "A stale
    # heartbeat" is the canonical BROKEN example, and an unreadable heartbeat
    # is the same fact with the evidence destroyed.
    _sec("heartbeat.own")
    own_hb = Path(config.heartbeat_path)
    if own_hb.exists():
        import json as _json
        from datetime import datetime, timezone

        try:
            from datetime import timedelta

            hb = _json.loads(own_hb.read_text())
            completed = datetime.fromisoformat(hb["cycle_completed_at"])
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age = (now - completed).total_seconds()

            # Backoff-aware freshness: an idle instance that has legitimately
            # backed off will look "old" against a fixed 2h yardstick. Prefer the
            # instance's own next_due_at (+ 1.5× its current interval) when present.
            next_due_raw = hb.get("next_due_at")
            next_due = None
            if next_due_raw:
                try:
                    next_due = datetime.fromisoformat(next_due_raw)
                    if next_due.tzinfo is None:
                        next_due = next_due.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    next_due = None

            if next_due is not None:
                interval_s = float(hb.get("current_interval_s") or 3600)
                deadline = next_due + timedelta(seconds=1.5 * interval_s)
                if now > deadline:
                    _broken(
                        f"own heartbeat is {age / 3600:.1f}h old and past its own "
                        f"backoff deadline (next_due_at={next_due.isoformat()}) "
                        f"— is the daemon running?"
                    )
                else:
                    _ok(
                        f"own heartbeat is fresh ({age:.0f}s old; backed off, "
                        f"next cycle due {next_due.isoformat()})"
                    )
            elif age > 2 * 3600:
                _broken(
                    f"own heartbeat is {age / 3600:.1f}h old — is the daemon running?"
                )
            else:
                _ok(f"own heartbeat is fresh ({age:.0f}s old)")
        except (ValueError, KeyError, OSError) as e:
            _broken(f"own heartbeat at {own_hb} is unreadable ({e})")
    else:
        _ok("no heartbeat.json yet (no cycle has completed — expected before first run)")

    # (k) LLM provider API key present (WARN if missing). Skip local providers.
    _sec("llm.provider_key")
    provider = config.llm.default_provider
    if provider in LOCAL_PROVIDERS:
        _ok(f"LLM provider '{provider}' is local — no API key required")
    elif config.llm.api_key:
        _ok(f"LLM provider '{provider}' has an api_key configured")
    else:
        env_var = PROVIDER_ENV.get(provider)
        if env_var is None:
            _warn(
                f"LLM provider '{provider}' is unrecognized — "
                f"cannot verify credentials"
            )
        elif os.environ.get(env_var):
            _ok(f"LLM provider '{provider}' key present via ${env_var}")
        else:
            _warn(
                f"LLM provider '{provider}' has no api_key and ${env_var} is unset "
                f"— live calls will fail"
            )

    # (m) Adaptive cycle backoff. A malformed block is a hard FAIL — it is
    # "config parsed but unconsumed" made loud: the operator asked for a
    # backoff policy and the numbers do not parse, so the cycle silently ran at
    # baseline instead. Report the live interval from the heartbeat when present.
    _sec("backoff.config")
    backoff = config.backoff
    errs = backoff.validation_errors()
    if errs:
        _fail(
            f"backoff config is malformed: {'; '.join(errs)} — backoff is DISABLED "
            f"(every wake runs at baseline). Fix `backoff:` in {config_path}."
        )
    elif not backoff.enabled:
        _ok("backoff disabled — cycles run on every launchd wake (fixed cadence)")
    else:
        live = ""
        own_hb = Path(config.heartbeat_path)
        if own_hb.exists():
            import json as _json

            try:
                hb = _json.loads(own_hb.read_text())
                ci = hb.get("current_interval_s")
                nd = hb.get("next_due_at")
                if ci is not None:
                    live = f"; live interval {float(ci) / 3600:.2f}h, next due {nd}"
            except (ValueError, OSError):
                pass
        _ok(
            f"backoff enabled: base={backoff.base} factor={backoff.factor} "
            f"max={backoff.max}{live}"
        )

    # (n) codex agentic-CLI presence, gated on config actually routing to it.
    # Mirrors (c): only a *referenced* codex is required. An agent whose model
    # names the codex CLI (or a feeds ingest/curate agent set to codex) means a
    # bead will dispatch through `codex exec`; if the binary is not on PATH that
    # bead crashes mid-cycle instead of failing preflight — the same silent
    # class as the Recorro defect. If nothing routes to codex, there is nothing
    # to require, so the check stays silent.
    _sec("cli.codex_present")
    import shutil

    codex_refs: list[str] = []
    for name, agent in config.agents.items():
        candidates = [agent.model, *(str(v) for v in agent.settings.values())]
        if any(c and "codex" in str(c).lower() for c in candidates):
            codex_refs.append(f"agent '{name}'")
    if codex_refs:
        if shutil.which("codex"):
            _ok(
                f"codex CLI resolvable on PATH "
                f"(routed to by: {', '.join(sorted(codex_refs))})"
            )
        else:
            _fail(
                f"config routes to the codex agentic CLI "
                f"({', '.join(sorted(codex_refs))}) but 'codex' is not on PATH "
                f"— every codex-assigned bead will crash mid-cycle instead of "
                f"failing here. Install codex and put it on PATH."
            )

    # (o) agy agentic-CLI presence, gated on config actually routing to it.
    # Same contract as (n): only a *referenced* agy is required. An agent whose
    # model names the agy CLI (or a feeds ingest/curate agent set to agy /
    # antigravity) means a bead will dispatch through the Google Antigravity
    # CLI; if the binary is not on PATH that bead crashes mid-cycle instead of
    # failing preflight. If nothing routes to agy, the check stays silent.
    _sec("cli.agy_present")
    agy_refs: list[str] = []
    for name, agent in config.agents.items():
        candidates = [agent.model, *(str(v) for v in agent.settings.values())]
        if any(
            c and ("agy" in str(c).lower() or "antigravity" in str(c).lower())
            for c in candidates
        ):
            agy_refs.append(f"agent '{name}'")
    if agy_refs:
        if shutil.which("agy"):
            _ok(
                f"agy CLI resolvable on PATH "
                f"(routed to by: {', '.join(sorted(agy_refs))})"
            )
        else:
            _fail(
                f"config routes to the agy (Google Antigravity) CLI "
                f"({', '.join(sorted(agy_refs))}) but 'agy' is not on PATH "
                f"— every agy-assigned bead will crash mid-cycle instead of "
                f"failing here. Install agy and put it on PATH."
            )

    # (o2) A configured plane must name a local executor that can actually run
    # what it pulls. `hub.actor` is an identity ON THE PLANE — the label a run's
    # binding names when it means this node — and it is almost never a local
    # backend name ('harness-bigmac' is not 'claude'). A mirror stamped with it
    # is undispatchable by construction: every pulled bead fails the next cycle
    # with "Unknown agent" and spawns an RCA bead apiece, which is the
    # box-scout shape again, arriving over the network. The first --live
    # end-to-end run found it that way. Checked here, before a pull, because
    # the damage lands one cycle after the pull and the fix is in config.
    _sec("hub.executor_dispatchable")
    hub = getattr(config, "hub", None)
    if hub is not None and getattr(hub, "url", None):
        from .backends import executor_names

        name = getattr(hub, "executor", None)
        if not name:
            _fail(
                f"hub.url is set but hub.executor is not — pulled work would be "
                f"mirrored under the plane actor '{hub.actor}', which is an "
                f"identity on the plane, not a runner here. Every pulled bead "
                f"would fail with \"Unknown agent: {hub.actor}\" and spawn an RCA "
                f"bead. Set hub.executor to a registered backend "
                f"({', '.join(sorted(executor_names()))})."
            )
        elif name not in executor_names() and name not in config.agents:
            _fail(
                f"hub.executor is '{name}', which is neither a registered backend "
                f"({', '.join(sorted(executor_names()))}) nor declared under "
                f"agents: — every bead pulled from the plane would fail with "
                f"\"Unknown agent: {name}\"."
            )
        else:
            _ok(f"hub.executor '{name}' is dispatchable (plane actor: '{hub.actor}')")

    # (o3) A node holding procedures must declare who its humans are, or the
    # revision policy binds nobody who says otherwise.
    #
    # The store polices the kind it is HANDED. In-process that is honest — a
    # caller passing `agent` is bound by all four rules with no configuration.
    # At the CLI the kind is a flag, and where `AGENTCO_HUMANS` is undeclared
    # the flag stands, on the reasoning that a local `harness sop retire` has
    # no key to authenticate and there is an operator at the terminal.
    #
    # On THIS runtime that reasoning has a hole, because what sits at the
    # terminal is frequently not a person: the cycle dispatches headless agent
    # CLIs with shell access, and one of them can run `harness sop revise ...
    # --author-kind human` (or omit the flag, which defaults to human) and
    # revise a procedure carrying a `money` step. Verified 2026-09-04: with the
    # variable unset that revision is DRAFTED; with `AGENTCO_HUMANS` declared
    # the identical command is refused `revision_policy:protected`.
    #
    # Declaring the set is what turns the flag into a check, so a node with
    # procedures and no declaration is reported here rather than discovered
    # by a `money` step changing under somebody.
    _sec("asop.humans_declared")
    try:
        from .asop_store import AsopStore

        asops_path = Path(config.asops_path)
        has_procedures = asops_path.exists() and asops_path.stat().st_size > 0
    except Exception:  # noqa: BLE001 — a store we cannot read is check (q)'s business
        has_procedures = False
    if has_procedures:
        if os.environ.get("AGENTCO_HUMANS", "").strip():
            _ok("AGENTCO_HUMANS is declared, so the revision policy binds a caller's claimed kind")
        else:
            _fail(
                "this node holds ASOPs but AGENTCO_HUMANS is not declared — the "
                "revision policy cannot bind anyone who claims to be human, and "
                "`--author-kind` defaults to human. Any dispatched agent with a "
                "shell can revise or activate a procedure holding a protected "
                "(`money` / `irreversible`) step. Declare the people: "
                "AGENTCO_HUMANS=<comma-separated actors>."
            )

    # (p) codex CLI authentication, gated on config routing to codex and the
    # binary being present. Check (n) ensures `codex exec` can start, but a
    # fresh, expired, or machine-local session without auth.json still makes a
    # codex-assigned bead crash on an auth error mid-cycle. This is WARN rather
    # than FAIL because `codex login` recovers it interactively. As with (n),
    # an unreferenced codex stays silent and a missing binary has already been
    # reported by the presence check above.
    _sec("cli.codex_auth")
    if codex_refs:
        if shutil.which("codex"):
            codex_auth_path = os.path.expanduser("~/.codex/auth.json")
            if os.path.exists(codex_auth_path):
                _ok(
                    f"codex CLI authentication file exists at {codex_auth_path} "
                    f"(routed to by: {', '.join(sorted(codex_refs))})"
                )
            else:
                _warn(
                    f"codex CLI auth file is missing at {codex_auth_path} "
                    f"— codex is not logged in, so a codex-assigned bead will "
                    f"crash on an auth error mid-cycle. Run 'codex login'."
                )

    # (q) Egress policy artifact — the data-classification ceiling table that
    # gates cross-vendor dispatch. Missing is a WARN, not a FAIL: the native
    # (Anthropic) path stays available by design, and every non-native route
    # fails closed on its own. But an AGENT_ROUTE naming a route the artifact
    # doesn't define IS a FAIL — that agent can never dispatch, and finding out
    # at 01:00 is the whole failure mode this subsystem exists to prevent.
    _sec("egress.policy")
    try:
        from .egress import AGENT_ROUTE, PolicyUnavailable, load_routes, artifact_path

        try:
            routes = load_routes(
                artifact_path(config.egress.routes_path, store_dir=config.store_dir),
                store_dir=config.store_dir,
            )
        except PolicyUnavailable as e:
            _warn(
                f"egress policy artifact unavailable ({e}). Native (Anthropic) "
                f"dispatch is unaffected; every other vendor route will be "
                f"blocked until you run "
                f"`bun ~/.claude/LIFEOS/TOOLS/ExportInferenceRoutes.ts`."
            )
        else:
            orphans = {a: r for a, r in AGENT_ROUTE.items() if r not in routes}
            if orphans:
                _fail(
                    f"agent(s) mapped to routes absent from "
                    f"{artifact_path(config.egress.routes_path, store_dir=config.store_dir)}: "
                    + ", ".join(f"{a}->{r}" for a, r in sorted(orphans.items()))
                    + " — these beads can never dispatch. Re-export the artifact "
                      "or fix AGENT_ROUTE."
                )
            else:
                unverified = [r.name for r in routes.values() if not r.ceiling_verified]
                _ok(
                    f"egress policy loaded: {len(routes)} route(s), "
                    f"{len(AGENT_ROUTE)} agent(s) mapped"
                )
                if unverified:
                    _warn(
                        f"route(s) with UNVERIFIED vendor terms: {', '.join(sorted(unverified))} "
                        f"— their ceiling is degraded one step for unattended runs. "
                        f"Confirm the vendor's terms, then set ceilingVerified in models.ts."
                    )
    except Exception as e:  # noqa: BLE001 — doctor must never crash the CLI
        _warn(f"egress policy check could not run: {e}")

    # (r) Every agent name sitting in the live queue can actually be dispatched.
    # A bead is runnable if its agent is built-in, a special executor, or
    # declared in config (an externally-executed agent the cycle leaves alone).
    # Anything else fails with "Unknown agent: X" the moment a cycle claims it —
    # and each failure spawns an RCA bead, so one un-declared name costs two
    # beads apiece. That is the sommeliwhey box-scout incident (2026-07-22,
    # 07-29, 08-04): `agentco init --company` rewrote .agentco/config.yaml with
    # the scaffold placeholder, `box-scout` vanished from agents:, and the next
    # cycle burned 50 beads. Nothing detected the un-declaration until the queue
    # was already on fire. This check is that detector: it reads config, not the
    # damage, so it fires before the cycle rather than after.
    _sec("queue.dispatchability")
    try:
        from . import _lm
        from .beads import Beads, TaskStatus
        from .backends import executor_names
        SPECIAL_EXECUTORS = executor_names()

        # Through the seam, not `from .agents import AGENTS`: that import
        # raises on a base install, the blanket except below turned it into
        # a warning, and the one check built to catch a vanished agent before
        # a cycle burned beads on it was skipped on every run — silently.
        AGENTS = _lm.agent_names()

        live_statuses = {
            TaskStatus.PENDING,
            TaskStatus.PENDING_APPROVAL,
            TaskStatus.IN_PROGRESS,
            TaskStatus.BLOCKED,
        }
        # A bead with NO agent name is the mirror-image hole in this check: it
        # has nothing to validate, so the loop below skips it and the check
        # reports OK — which it did on two real box-scout work beads
        # (ac-6c5f1123, ac-d4a18e4d) that the cycle was mishandling every hour.
        # Dispatchable states only: a bead this check already forced to BLOCKED
        # is visible and no longer looping, so re-failing on it forever would
        # just be a second mute signal.
        dispatchable_statuses = {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}
        queued: dict[str, int] = {}
        unassigned: list[str] = []
        for task in Beads(config.tasks_path).list():
            if task.status not in live_statuses:
                continue
            if not task.assigned_agent:
                # assigned_to (a human executor) is a legitimate reason to carry
                # no agent name — those beads wait, visible, by design.
                if not task.assigned_to and task.status in dispatchable_statuses:
                    unassigned.append(task.id)
                continue
            queued[task.assigned_agent] = queued.get(task.assigned_agent, 0) + 1

        if unassigned:
            shown = ", ".join(unassigned[:5])
            more = f" (+{len(unassigned) - 5} more)" if len(unassigned) > 5 else ""
            _fail(
                f"{len(unassigned)} dispatchable bead(s) have no assigned_agent and "
                f"no assigned_to: {shown}{more} — the cycle counts each as an error "
                "every run without recording one, so `errors` stays permanently "
                "non-zero and any alert on it is dead. Assign an agent (or a "
                "human:<name> executor) to each."
            )

        undeclared = {
            name: count
            for name, count in queued.items()
            if name not in AGENTS
            and name not in SPECIAL_EXECUTORS
            and name not in config.agents
        }
        if undeclared:
            _fail(
                "queue holds bead(s) assigned to agent(s) that cannot dispatch: "
                + ", ".join(
                    f"'{name}' ({count} bead(s))"
                    for name, count in sorted(undeclared.items())
                )
                + " — the next cycle will fail every one with \"Unknown agent\" "
                "and spawn an RCA bead for each. Either declare the name under "
                "agents: (for an externally-executed worker) or fix the typo."
            )
        elif queued:
            external = sorted(
                name
                for name in queued
                if name not in AGENTS and name not in SPECIAL_EXECUTORS
            )
            msg = f"all {len(queued)} queued agent name(s) dispatchable"
            if external:
                msg += f" (externally-executed: {', '.join(external)})"
            _ok(msg)
    except Exception as e:  # noqa: BLE001 — doctor must never crash the CLI
        _warn(f"queue agent-dispatchability check could not run: {e}")

    # (t) Cross-machine leases (ac-48d8aba3). The two-machine protocol's failure
    # modes are all quiet ones: a stuck lease looks like work in progress, a
    # corrupt expiry looks like a claimed bead, and a churning bead looks like
    # throughput. None of them raises, none of them fails a cycle, and the ONLY
    # thing that reaps an expired lease is `agentco pull` — so on the hub, where
    # nobody pulls, they simply accumulate. Doctor is where they become visible.
    _sec("leases.health")
    try:
        from .beads import Beads

        from datetime import datetime as _dt, timezone as _tz

        live_tasks = Beads(config.tasks_path).list()
        path = lease_pathologies(live_tasks, _dt.now(_tz.utc))
        leased_live = [t for t in live_tasks if t.leased_by]

        def _ids(tasks: list) -> str:
            shown = ", ".join(t.id for t in tasks[:5])
            return shown + (f" (+{len(tasks) - 5} more)" if len(tasks) > 5 else "")

        if path["expired_unreaped"]:
            _fail(
                f"{len(path['expired_unreaped'])} bead(s) are IN_PROGRESS under an "
                f"EXPIRED lease that was never reaped: {_ids(path['expired_unreaped'])} "
                f"— each is stopped dead and still blocking whatever waits on it. "
                f"Nothing reaps outside `agentco pull`, so on a hub nobody pulls "
                f"from these never clear themselves. Fix: "
                f"`agentco pull --agent <worker>`, which reaps before it claims."
            )

        if path["corrupt_expiry"]:
            _fail(
                f"{len(path['corrupt_expiry'])} leased bead(s) carry an UNUSABLE "
                f"lease_expires_at: {_ids(path['corrupt_expiry'])} — reaping skips "
                f"these deliberately rather than reclaiming on a guess, and "
                f"ready() excludes them, so NO automatic path will ever touch "
                f"them again — `agentco pull` will not rescue these. A human "
                f"must clear the lease: `agentco report <id> --attempt "
                f"<lease_attempt> --failed --result 'stuck lease cleared'`, then "
                f"`agentco tasks retry <id>` to return it to pending."
            )

        if path["churning"]:
            detail = ", ".join(
                f"{t.id} (attempt {t.lease_attempt})" for t in path["churning"][:5]
            )
            more = (
                f" (+{len(path['churning']) - 5} more)"
                if len(path["churning"]) > 5
                else ""
            )
            # WARN, not FAIL, and the distinction is deliberate: a churning bead
            # is still moving, so the hub is not wedged. Making ordinary
            # lid-closing churn a red exit would leave doctor permanently
            # non-zero on a laptop lane — the same "train the operator to ignore
            # doctor" failure the routing-evidence check below avoids.
            _warn(
                f"{len(path['churning'])} live bead(s) have been handed out more "
                f"than {path['threshold']} times without completing: {detail}{more} "
                f"— each claim burns a worker session and the count only grows. "
                f"Check whether the bead is un-executable on the lane it keeps "
                f"being routed to, or whether that worker keeps dying mid-run."
            )

        if not any(
            path[k] for k in ("expired_unreaped", "corrupt_expiry", "churning")
        ):
            _ok(
                f"leases healthy: {len(leased_live)} live lease(s), none expired, "
                f"corrupt, or over {path['threshold']} attempts"
            )
    except Exception as e:  # noqa: BLE001 — doctor must never crash the CLI
        _warn(f"lease health check could not run: {e}")

    # (u) Remote node liveness (ac-48d8aba3). A registered child with `host` set
    # cannot be verified from a local heartbeat — verify_child says exactly that
    # and stops. That honesty leaves a hole: a MacBook whose launchd job died is
    # indistinguishable from a MacBook with nothing to do. The hub DOES see every
    # `agentco pull`, so the pull ledger is that node's heartbeat, and this check
    # is the same staleness judgement applied to it.
    _sec("children.remote_liveness")
    try:
        from .children import (
            ChildRegistry as _Registry,
            PullLedger,
            pull_ledger_path,
            verify_remote_child,
        )

        remote = [c for c in _Registry(config.children_registry_path).list() if c.is_remote]
        if remote:
            ledger = PullLedger(pull_ledger_path(config.children_registry_path))
            rows = ledger.load()
            healthy = 0
            for child in remote:
                result = verify_remote_child(child, rows.get(child.name))
                # RECLASSIFIED: a `fail` here is BROKEN. The old rationale —
                # "an alarm for a human, not a reason to refuse to start the
                # hub" — was reasoning about a boolean exit code, where BROKEN
                # meant "refuse to run". It no longer does: the class states
                # what is true, and what is true when a node stops pulling is
                # that a lane cannot claim, which is BROKEN by definition. A
                # `warn` (pulling, but slower than its cadence) is genuinely
                # reduced capability, so it stays DEGRADED.
                if result["level"] == "fail":
                    _broken(f"remote node '{child.name}': {result['detail']}")
                elif result["level"] == "warn":
                    _degraded(f"remote node '{child.name}': {result['detail']}")
                else:
                    healthy += 1
                    _ok(f"remote node '{child.name}': {result['detail']}")
            if healthy == len(remote):
                _ok(f"all {len(remote)} remote node(s) pulling on cadence")
    except Exception as e:  # noqa: BLE001 — doctor must never crash the CLI
        _warn(f"remote node liveness check could not run: {e}")

    # (s) Routing evidence. Advisory by construction: this reports what the cost
    # ledger can and cannot support, and notes where a configured model
    # disagrees with the evidence.
    #
    # RECLASSIFIED to INFO throughout — this is what INFO is for. A routing
    # preference is not a broken invariant and "you could be cheaper" is not
    # reduced capability; it is advice. Emitting it as DEGRADED made every
    # advisory line contribute to an exit code, which is precisely how a
    # gate-able signal gets diluted into noise nobody reads.
    _sec("routing.evidence")
    try:
        from .cost import read_ledger
        from .routing_eval import evaluate, recommendations

        entries = read_ledger(config.tasks_path)
        if not entries:
            _ok("routing evidence: no cost telemetry yet (recorded as beads execute)")
        else:
            health, results = evaluate(entries, group_by="task_type")
            recs = recommendations(results)

            if health.distinct_models < 2:
                _ok(
                    f"routing evidence: {health.total_runs} run(s) on "
                    f"{health.distinct_models} model — too few arms to compare, nothing to advise"
                )
            elif not health.can_compare_quality:
                _info(
                    f"routing evidence: {health.total_runs} run(s) across "
                    f"{health.distinct_models} models but EVERY run shares the same outcome — "
                    "quality cannot be compared, so any recommendation rests on cost alone. "
                    "Run `agentco eval routing` for the table."
                )
            else:
                _ok(
                    f"routing evidence: {health.total_runs} run(s), "
                    f"{health.distinct_models} models, outcomes vary — comparable"
                )

            # The disagreement check. Only meaningful where a task type maps
            # back to a configured model — the two entries here were the feeds
            # ingest and curate stages, which this runtime no longer ships. A
            # task type an extension introduces has no config field to compare
            # against, so the check reports evidence and advises on nothing
            # until something registers a mapping.
            configured: dict[str, str | None] = {}
            disagreements = 0
            for key, model in recs.items():
                if key == "(unset)":
                    continue
                current = configured.get(key)
                if current and current != model:
                    _info(
                        f"routing evidence: '{key}' is configured to {current} but the "
                        f"ledger favours {model} — advisory only, see `agentco eval routing`"
                    )
                    disagreements += 1
            if recs and "(unset)" in recs and len(recs) == 1:
                _info(
                    "routing evidence: every run has an empty task_type, so the table "
                    "cannot be mapped to a config field. Set metadata 'type' on beads to "
                    "make routing advice actionable."
                )
            elif recs and not disagreements:
                _ok(f"routing evidence: {len(recs)} recommendation(s), config agrees")
    except Exception as e:  # noqa: BLE001 — advisory checks never break doctor
        _warn(f"routing evidence check could not run: {e}")

    # (t) Usage telemetry (backport 2). A fleet that cannot be metered cannot
    # be safely scaled, so doctor reports what the meter actually knows AND —
    # the part that matters — what it does not: a model-invoking path that
    # executed without writing a usage row is invisible in the ledger by
    # construction, and can only be found by comparing the ledger against work
    # known to have run.
    #
    # CLASS: DEGRADED for the gap. A telemetry hole is exactly "working, but
    # with an unverified assumption" — the node executes fine, the spend
    # decision built on the ledger does not. It is never BROKEN (no work is
    # lost) and never INFO (a spend figure that silently under-counts is not
    # advice). The static UNMETERED_PATHS disclosure below is INFO: it is a
    # constant of this build, not a state change, and emitting a permanent
    # DEGRADED would make exit 2 the node's resting state.
    _sec("usage.telemetry")
    try:
        from . import usage as usage_mod
        from .beads import Beads, TaskStatus

        rows = usage_mod.read_ledger(config.tasks_path)
        if not rows:
            # An empty ledger means two different things and they are not the
            # same class. A node that has never dispatched anything simply has
            # nothing to meter (INFO). A node with agent-executed beads in its
            # history and zero usage rows has the gap in its strongest form —
            # every execution bypassed the meter (DEGRADED). Reporting both as
            # DEGRADED would put a fresh node permanently at exit 2, which is
            # how a class stops meaning anything.
            executed_any = any(
                task.assigned_agent not in (None, "", "human")
                and task.status in (TaskStatus.DONE, TaskStatus.FAILED)
                for task in Beads(config.tasks_path).list()
            )
            msg = (
                "usage telemetry: no rows in "
                f"{usage_mod.ledger_path(config.tasks_path)} — "
            )
            if executed_any:
                _degraded(
                    msg + "yet this node HAS agent-executed beads, so every one of "
                    "them ran unmetered. This node's burn rate is unknown. "
                    "See `agentco usage`."
                )
            else:
                _info(
                    msg + "no agent-executed bead has run yet, so there is nothing "
                    "to meter. Metering starts with the first model invocation."
                )
        else:
            t = usage_mod.totals(rows)
            cost = f"${t['cost_usd']:.4f}" if t["cost_usd"] is not None else "$— (unreported)"
            tokens = (
                f"{(t['input_tokens'] or 0) + (t['output_tokens'] or 0):,} tokens"
                if t["token_runs"]
                else "no token counts reported"
            )
            _ok(
                f"usage telemetry: {t['runs']} metered run(s) over {t['beads']} bead(s), "
                f"{tokens}, {cost} — {t['first_at']} .. {t['last_at']} (`agentco usage`)"
            )
            if t["unreported_cost_runs"]:
                _ok(
                    f"usage telemetry: {t['unreported_cost_runs']} of {t['runs']} run(s) "
                    "reported no price (subscription-billed or a route that does not "
                    "report cost) — recorded as unknown, never as $0"
                )

            # The gap check. Only beads executed SINCE metering began can be
            # expected to have a row; anything older ran before the meter
            # existed and warning about it would be noise, not signal.
            since = min(str(r.get("at") or "") for r in rows if r.get("at"))
            executed = []
            for task in Beads(config.tasks_path).list():
                if task.assigned_agent in (None, "", "human"):
                    continue
                if task.status not in (TaskStatus.DONE, TaskStatus.FAILED):
                    continue
                if str(task.updated_at or "") >= since:
                    executed.append(task.id)
            gaps = usage_mod.unmetered_beads(rows, executed)
            if gaps:
                _warn(
                    f"usage telemetry GAP: {len(gaps)} bead(s) executed on an agent "
                    f"route since metering began but produced NO usage row "
                    f"(e.g. {', '.join(gaps[:4])}) — an execution path is bypassing "
                    f"`usage.meter`, so its spend is invisible"
                )
            else:
                _ok(
                    f"usage telemetry: every agent-executed bead since {since[:10]} "
                    "has a usage row — no unmetered execution path detected"
                )
        # Named, not implied. An unmetered path the operator has never heard of
        # is worse than one on a list: the list is what makes the remaining
        # blind spot a decision rather than a surprise. INFO, not DEGRADED —
        # this line is true of every run of this build, and a finding that is
        # unconditionally present cannot carry information about THIS run.
        _info(
            f"usage telemetry: {len(usage_mod.UNMETERED_PATHS)} known model-invoking "
            "path(s) are NOT metered — "
            + "; ".join(f"{path} ({why})" for path, why in usage_mod.UNMETERED_PATHS)
        )
    except Exception as e:  # noqa: BLE001 — advisory check never breaks doctor
        _warn(f"usage telemetry check could not run: {e}")

    # PRIME cache freshness. WARN, never FAIL: a node with no PRIME.md is a
    # node that has not opted in, and a stale one still executes — it just
    # feeds agents a description of a repo that has moved. Rot surfaces in the
    # health check people already run rather than on a new surface nobody reads.
    _sec("prime.freshness")
    try:
        from . import prime as prime_mod

        prime_dir = prime_mod.node_dir(config)
        prime_file = prime_dir / prime_mod.PRIME_FILENAME
        if not prime_file.is_file():
            _ok(
                f"no {prime_mod.PRIME_FILENAME} cached (optional — "
                f"`agentco prime` creates one)"
            )
        else:
            fresh, reasons = prime_mod.check(prime_dir)
            if fresh:
                _ok(f"{prime_mod.PRIME_FILENAME} is fresh")
            else:
                _warn(
                    f"{prime_file} is stale ({'; '.join(reasons)}) — agents are "
                    f"being primed with context that no longer matches the repo. "
                    f"Run `agentco prime`."
                )
    except Exception as e:  # noqa: BLE001 — advisory check never breaks doctor
        _warn(f"PRIME freshness check could not run: {e}")

    return report


def run_doctor(
    config_path: str,
    *,
    classes=None,
    as_json: bool = False,
) -> int:
    """Run every check, print the report, return the class-derived exit code.

    ``classes`` filters what is PRINTED. It deliberately does not touch the
    return value: a scheduler that watches `--class degraded` still exits 1 the
    moment something is broken, and a human who filters to `info` is told, in
    the filtered output, how many broken findings they just hid.
    """
    report = collect(config_path)
    print(report.to_json(classes) if as_json else report.render(classes))
    return report.exit_code()
