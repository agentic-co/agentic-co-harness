"""CLI - Command line interface."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from .beads import gate_kind, verify_check_text
from .beads import (
    DEFAULT_LEASE_TTL_S,
    SOP_TEXT_KEYS,
    Beads,
    LeaseError,
    TaskPriority,
    TaskStatus,
)
from .config import Config
from .doctor import DEFAULT_RECONCILE_AFTER_H, unresolved_for_worker
from .cost import format_table, read_ledger, summarize
from .orchestrator import Orchestrator
from .routing_eval import (
    DEFAULT_MIN_ARM_SAMPLES,
    DEFAULT_MIN_SAMPLES,
    evaluate,
    format_report,
    portfolio_ledger,
    to_json,
)
from . import schedules as schedules_mod
from . import usage as usage_mod


@click.group()
@click.option("--config", "-c", default="config.yaml", help="Config file path")
@click.pass_context
def main(ctx, config: str):
    """AgentCo - Minimal agentic company structure."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@main.command()
@click.option("--company", is_flag=True, help="Create full company structure")
@click.option("--portfolio", is_flag=True, help="Also create recurring defs + children registry")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing config.yaml with a fresh default (destructive)",
)
@click.pass_context
def init(ctx, company: bool, portfolio: bool, force: bool):
    """Initialize AgentCo in current directory."""
    # Init is additive: an existing config.yaml is operator state, never ours to
    # rewrite. `--config` defaults to "config.yaml" in the CWD, and a node's own
    # runtime dir (.agentco/, the launchd WorkingDirectory) is exactly where an
    # operator lands — so a bare `agentco init` there used to hand a live node a
    # fresh default Config(), dropping `instance:` and every operator-declared
    # agent. Dropping an externally-executed agent (sommeliwhey's `box-scout`)
    # is not cosmetic: config.agents is the entire test in
    # Orchestrator._external_agent(), so the next cycle claims that agent's
    # beads and fails each with "Unknown agent" plus an RCA bead apiece.
    # 2026-08-04 closed the same hole in scaffold_agentco_runtime(); this is the
    # other door into the same file, and it stayed open.
    config_path = Path(ctx.obj["config_path"])
    if config_path.exists() and not force:
        config = Config.load(config_path)
        click.echo(f"Preserved existing {config_path} (use --force to overwrite)")
    else:
        config = Config()
        config.save(config_path)
        click.echo(f"Created {config_path}")
    click.echo(f"Created {config.tasks_path}")
    Beads(config.tasks_path)  # Create empty tasks file

    if portfolio:
        from .children import ChildRegistry
        from .recurring import Recurring

        Recurring(config.recurring_path)  # create empty recurring.jsonl
        registry = ChildRegistry(config.children_registry_path)
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.touch()
        click.echo(f"Created {config.recurring_path}")
        click.echo(f"Created {registry.path}")
        click.echo("Register children with: agentco link-child NAME PATH --interval 1h")

    if company:
        from .registry import Registry, scan_company
        from .scaffold import scaffold_agentco_runtime, scaffold_company

        base = Path.cwd()
        company_path = scaffold_company(base)
        click.echo(f"Created {company_path}")

        runtime_path = scaffold_agentco_runtime(base)
        click.echo(f"Created {runtime_path}")

        # Build initial registry
        registry = Registry(base / ".agentco" / "registry.json")
        count = scan_company(company_path, registry)
        click.echo(f"Registered {count} documents")


@main.command()
@click.pass_context
def observe(ctx):
    """Poll sources and create tasks."""
    config = Config.load(ctx.obj["config_path"])
    orchestrator = Orchestrator(config)
    tasks = orchestrator.observe()
    click.echo(f"Created {len(tasks)} tasks")


@main.command()
@click.option("--agent", "-a", help="Only work on tasks for this agent")
@click.option("--limit", "-n", default=10, help="Max tasks to process")
@click.pass_context
def work(ctx, agent: str | None, limit: int):
    """Execute ready tasks."""
    config = Config.load(ctx.obj["config_path"])
    orchestrator = Orchestrator(config)
    tasks = orchestrator.work(agent_name=agent, limit=limit)
    click.echo(f"Completed {len(tasks)} tasks")


@main.command()
@click.argument("signature")
@click.option(
    "--examples",
    "-e",
    default=None,
    help="Path to JSONL examples (default: data/examples/<signature>.jsonl)",
)
@click.option("--candidates", "-n", default=7, help="Number of candidates to evaluate")
def optimize(signature: str, examples: str | None, candidates: int):
    """Optimize a DSPy signature with MIPROv2."""
    from pathlib import Path

    from .optimize import SIGNATURES, optimize_signature

    if signature not in SIGNATURES:
        click.echo(f"Unknown signature: {signature}", err=True)
        click.echo(f"Available: {', '.join(SIGNATURES.keys())}", err=True)
        sys.exit(1)

    examples_path = Path(examples) if examples else Path(f"data/examples/{signature}.jsonl")
    if not examples_path.exists():
        click.echo(f"Examples not found: {examples_path}", err=True)
        sys.exit(1)

    click.echo(f"Optimizing {signature} with {candidates} candidates...")
    try:
        output = optimize_signature(signature, examples_path, num_candidates=candidates)
        click.echo(f"Saved optimized program to {output}")
    except Exception as e:
        click.echo(f"Optimization failed: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--observe-interval", default=300, help="Seconds between source polls")
@click.option("--work-interval", default=60, help="Seconds between work cycles")
@click.option("--cycle-interval", default=3600, help="Seconds between heartbeat cycles")
@click.pass_context
def daemon(ctx, observe_interval: int, work_interval: int, cycle_interval: int):
    """Run continuously in daemon mode."""
    config = Config.load(ctx.obj["config_path"])
    orchestrator = Orchestrator(config)
    try:
        orchestrator.daemon(
            observe_interval=observe_interval,
            work_interval=work_interval,
            cycle_interval=cycle_interval,
        )
    except KeyboardInterrupt:
        click.echo("\nShutting down...")


@main.command()
@click.option("--limit", "-n", default=50, help="Max tasks to execute this cycle")
@click.option(
    "--force",
    is_flag=True,
    help="Ignore adaptive backoff: run this cycle now and reset the interval to baseline.",
)
@click.pass_context
def cycle(ctx, limit: int, force: bool):
    """Run one heartbeat cycle: recurring generator → triage → execute.

    With adaptive backoff enabled, a wake that lands before the next due time
    with nothing active exits fast (skipped) instead of running. `--force`
    overrides that and also snaps the cadence back to baseline.
    """
    config = Config.load(ctx.obj["config_path"])
    orchestrator = Orchestrator(config)
    summary = orchestrator.cycle(limit=limit, force=force)
    click.echo(json.dumps(summary, indent=2))


@main.command()
@click.option("--agent", "-a", required=True, help="Worker/agent name to pull work for")
@click.option(
    "--max",
    "max_beads",
    default=3,
    show_default=True,
    help="Max beads to claim this poll (rate-limited drain after a long offline)",
)
@click.option(
    "--ttl",
    default=DEFAULT_LEASE_TTL_S,
    show_default=True,
    help="Lease length in seconds — how long the hub believes this claim",
)
@click.option(
    "--node",
    default=None,
    help="Registered child node this worker runs on. Its capabilities (from the "
    "children registry) are used as the claimant's manifest. Omit when pulling "
    "locally — the local config.yaml's capabilities are used instead.",
)
@click.option(
    "--reconcile",
    is_flag=True,
    help="List this worker's unresolved beads and claim NOTHING. Entered "
    "automatically after a long offline while such beads exist.",
)
@click.option(
    "--reconcile-after",
    default=DEFAULT_RECONCILE_AFTER_H,
    show_default=True,
    help="Hours of silence after which the reconcile guard arms itself",
)
@click.option(
    "--force",
    is_flag=True,
    help="Break-glass: claim even while the reconcile guard is armed",
)
@click.pass_context
def pull(
    ctx,
    agent: str,
    max_beads: int,
    ttl: int,
    node: str | None,
    reconcile: bool,
    reconcile_after: float,
    force: bool,
):
    """Claim this agent's ready beads and print them as JSON (ac-9cae7593).

    The hub half of the two-machine dispatch protocol: the MacBook worker's
    launchd job runs this over SSH, executes what comes back, and returns each
    outcome with `agentco report`. Pull, never push — the hub hands out work
    only when a worker asks, so a laptop that is asleep, offline or mid-reboot
    is simply a worker that has not asked yet, not a queue of lost dispatches.

    Claims are compare-and-set, so running this twice concurrently (a retried
    SSH call, two overlapping launchd ticks) cannot hand the same bead out
    twice: the loser sees the bead as leased and skips it.

    `--max` bounds the drain. A laptop that has been shut for a week would
    otherwise wake and claim the entire backlog at once, take leases on all of
    it, and hold every bead hostage to one fragile session.

    Expired leases are reaped first, so beads abandoned by a previous session
    of this same worker come back into its own ready set on the next poll
    rather than needing an operator.

    Lane routing (ac-39d4dbc8): the claim carries the claimant's declared
    capabilities, so a worker only takes beads whose `requires` its machine
    actually covers. Where that manifest is READ FROM depends on where this
    command runs, and the two cases are genuinely different:

    * **Locally** (no `--node`): the manifest is this node's own `config.yaml`.
      The process claiming and the machine executing are the same box, so its
      own declaration is the honest one.
    * **Over SSH on the hub** (`--node frontsteps`): the deployed shape — the
      MacBook's launchd job runs this ON THE HUB, so the local config is the
      *hub's* and would refuse precisely the beads the worker exists to take.
      The manifest is instead read from that child's registry entry, which is
      what the registry's capability tags are for.

    The manifest is never accepted as a raw flag value. `--node` names a
    registered child and the capabilities come from the registry, so a caller
    can only select among lanes an operator already wrote down — it cannot
    assert a new one. This still means the HUB's registry describes what a
    remote worker may claim, which is safe in the direction that matters:
    over-declaring hands a bead to a machine that then fails it, and cannot
    conjure the credential itself (Plans/TwoMachineLifeos.md, invariant 2 — the
    write PAT never leaves the MacBook).

    A bead the claimant cannot satisfy is reported in `refused` rather than
    dropped from the output. It stays PENDING and visible: the hub must keep
    seeing it (it belongs to some other lane), and a poll that silently skipped
    it forever would turn a misroute into nothing at all.

    RECONCILE BEFORE REPLAY (ac-48d8aba3). A worker returning from a long
    offline is the one caller that must not be trusted to just resume. Its
    leases have expired; the beads may have been reaped back to PENDING; and —
    the part that actually hurts — its half-finished work may ALREADY have
    landed externally. An ADO write is not undone by a lease expiring. Replaying
    it blind writes the work item twice.

    So before claiming, the hub asks whether this worker has anything
    unresolved, and if so hands back the list instead of new work:

        {"mode": "reconcile", "outstanding": [...], "contract": "..."}

    The **reconcile set** is every live bead with `lease_attempt > 0` that this
    worker either still holds (`leased_by`) or was the last to be handed
    (`assigned_agent`, lease since cleared by reaping). That is precisely the
    set for which "did my side-effect land?" is an open question — a bead never
    handed to this worker cannot have been half-done by it, and a terminal bead
    has an answer already.

    **The worker-side contract** (the ADO ground-truth check itself is out of
    scope here — this is the hub-side protocol support for it): for each listed
    bead, query the external system of record — for the FrontSteps lane, the ADO
    work item named by the bead's `external_id`/`external_url` metadata — and
    decide from THAT, never from local memory:

      * the write landed  → `agentco report <id> --attempt N --done   --result ...`
      * it did not land   → `agentco report <id> --attempt N --failed --result ...`
        (the bead returns through the normal ready path and is re-executed)
      * genuinely ambiguous → leave it and escalate to a human; the guard stays
        armed, which is the correct outcome for an unknown.

    `--attempt` is the `lease_attempt` shown in the reconcile output. Reporting
    still works on a bead that was reaped back to PENDING, because the fence
    checks the attempt counter and reaping does not bump it — only a fresh claim
    does. If someone else has since claimed it, the report is fenced out, which
    is also correct: that bead is no longer this worker's problem.

    **Why the guard arms on an unresolved set rather than purely on a clock.**
    A pure "> N hours since last pull" trigger clears itself the moment the
    worker polls again — so a worker that ignored the reconcile output and
    simply re-polled would be handed the very work the guard exists to withhold,
    and the protection would be advisory. Arming on the outstanding set instead
    means the guard clears when the beads are actually resolved, which is the
    real exit condition. The clock still decides when to ARM (a worker polling
    steadily every five minutes has not been offline and needs no ceremony); the
    set decides when to DISARM. `--reconcile` forces the mode on demand and
    `--force` is the documented break-glass override
    (Plans/BreakGlassFailover.md).

    Reconcile mode deliberately does NOT reap. Reaping releases leases whose
    external side-effects are exactly what is under question, handing those
    beads to another worker mid-investigation.

    Every pull — claiming, reconciling, or empty — is stamped into the pull
    ledger beside the children registry. That stamp is a remote node's only
    heartbeat: the hub is the sole observer of whether the MacBook is still
    talking, and `agentco doctor` reads it to alert on a lane that has gone
    quiet.

    Output is JSON on stdout and nothing else — this is a machine interface.
    """
    from .beads import CapabilityError
    from .children import ChildRegistry, PullLedger, pull_ledger_path

    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    if node is None:
        capabilities = list(config.capabilities)
    else:
        child = ChildRegistry(config.children_registry_path).get(node)
        if child is None:
            # Refused, not defaulted: falling back to the hub's own manifest on
            # a typo'd node name would silently claim work for the wrong lane.
            click.echo(
                f"unknown node {node!r} — not in {config.children_registry_path}. "
                f"Register it with `agentco link-child` first.",
                err=True,
            )
            sys.exit(1)
        capabilities = list(child.capabilities)

    ledger = PullLedger(pull_ledger_path(config.children_registry_path))
    ledger_key = node or agent
    prior = ledger.get(ledger_key)

    outstanding = unresolved_for_worker(beads.list(), agent)

    # The clock ARMS the guard; the outstanding set DISARMS it. A worker that has
    # never pulled is treated as silent-for-ever: its first contact after a
    # rebuild is exactly when a stale-lease replay would do damage.
    silent_h = None
    if prior and prior.get("last_pull_at"):
        last = datetime.fromisoformat(prior["last_pull_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        silent_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    was_silent = silent_h is None or silent_h > reconcile_after
    # The latch. Without it the guard would clear the instant the worker polled
    # again — its own reconcile poll refreshes `last_pull_at`, so the very next
    # request would look like a healthy steady-state worker and be handed the
    # work the guard just withheld. Once issued, reconcile stays issued until
    # the outstanding set is empty; that is what makes this a gate rather than
    # a suggestion.
    still_reconciling = bool(prior) and prior.get("last_mode") == "reconcile"
    armed = bool(outstanding) and (reconcile or was_silent or still_reconciling)

    if armed and not force:
        ledger.record(
            ledger_key,
            agent=agent,
            node=node,
            mode="reconcile",
            claimed=0,
            outstanding=len(outstanding),
        )
        click.echo(
            json.dumps(
                {
                    "agent": agent,
                    "node": node,
                    "mode": "reconcile",
                    "reason": (
                        "explicit --reconcile"
                        if reconcile
                        else (
                            "no prior pull recorded"
                            if silent_h is None
                            else f"last pull {silent_h:.1f}h ago "
                            f"(> --reconcile-after {reconcile_after}h)"
                            if was_silent
                            else "reconcile already issued and still unresolved"
                        )
                    ),
                    "claimed": [],
                    "count": 0,
                    "outstanding": [
                        {
                            "id": t.id,
                            "title": t.title,
                            "status": t.status.value,
                            "lease_attempt": t.lease_attempt,
                            "leased_by": t.leased_by,
                            "lease_expires_at": t.lease_expires_at,
                            "still_held": t.leased_by == agent,
                            "external_id": (t.metadata or {}).get("external_id"),
                            "external_url": (t.metadata or {}).get("external_url"),
                        }
                        for t in outstanding
                    ],
                    "contract": (
                        "Check the external system of record for each bead above "
                        "before claiming anything new. Then close each one with "
                        "`agentco report <id> --attempt <lease_attempt> "
                        "--done|--failed`. This mode repeats until the list is "
                        "empty; `--force` overrides it (break-glass only)."
                    ),
                },
                indent=2,
            )
        )
        return

    reaped = beads.reap_expired_leases()
    claimed = []
    refused: list[dict] = []
    for task in beads.ready(assigned_agent=agent):
        if len(claimed) >= max_beads:
            break
        try:
            got = beads.claim(
                task.id, agent, ttl_seconds=ttl, capabilities=capabilities
            )
        except CapabilityError:
            # Not a race and not fatal to the poll: this bead is simply not this
            # node's. Record what was missing so the misroute is legible, and
            # keep draining — one wrong-lane bead must not strand the rest.
            held = set(capabilities)
            refused.append(
                {"id": task.id, "missing": [r for r in task.requires if r not in held]}
            )
            continue
        if got is None:
            # Lost the CAS to a concurrent claimer. Expected under retry;
            # claim() already said so on stderr. Keep draining.
            continue
        claimed.append(got)

    ledger.record(
        ledger_key,
        agent=agent,
        node=node,
        mode="force" if (armed and force) else "claim",
        claimed=len(claimed),
        outstanding=len(outstanding),
    )

    click.echo(
        json.dumps(
            {
                "agent": agent,
                "node": node,
                "mode": "force" if (armed and force) else "claim",
                "capabilities": capabilities,
                "reaped": [t.id for t in reaped],
                "claimed": [json.loads(t.to_json()) for t in claimed],
                "refused": refused,
                "count": len(claimed),
            },
            indent=2,
        )
    )


@main.command()
@click.argument("task_id")
@click.option(
    "--attempt",
    type=int,
    required=True,
    help="The lease_attempt this result was produced under (the fence)",
)
@click.option("--done", "done", is_flag=True, help="Report success")
@click.option("--failed", "failed", is_flag=True, help="Report failure")
@click.option("--result", default=None, help="Result payload / error text")
@click.option(
    "--idempotency-key",
    default=None,
    help="Dedup key — replaying the same key is a no-op, not a second write",
)
@click.pass_context
def report(
    ctx,
    task_id: str,
    attempt: int,
    done: bool,
    failed: bool,
    result: str | None,
    idempotency_key: str | None,
):
    """Return a leased bead's outcome to the hub, fenced on --attempt.

    The other half of `agentco pull`. `--attempt` must be the lease_attempt the
    worker was handed; if the bead has since moved on (its lease expired, was
    reaped, and it was re-issued to someone else) this exits non-zero and
    writes NOTHING. That is the point: a worker coming back from a long sleep
    with an answer to a question the hub stopped asking must not overwrite the
    successor's real result.

    Exit codes are the contract for the SSH caller — 0 applied, 1 no such bead
    or bad arguments, 2 fenced out (stale attempt).
    """
    if done == failed:
        click.echo("error: pass exactly one of --done or --failed", err=True)
        sys.exit(1)

    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    try:
        task = beads.report_result(
            task_id,
            attempt=attempt,
            status=TaskStatus.DONE if done else TaskStatus.FAILED,
            result=result,
            idempotency_key=idempotency_key,
        )
    except LeaseError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)

    if task is None:
        click.echo(f"error: no such task {task_id}", err=True)
        sys.exit(1)

    # Echo the RESULTING status, never the requested one: a gated bead comes
    # back awaiting_verify or verify_failed, and the worker must be able to see
    # that its "done" did not mean done.
    click.echo(
        json.dumps(
            {
                "id": task.id,
                "status": task.status.value,
                "lease_attempt": task.lease_attempt,
                "leased_by": task.leased_by,
            },
            indent=2,
        )
    )


@main.command("rca")
@click.argument("task_id")
@click.option(
    "--error",
    "-e",
    default=None,
    help="Error text to seed the RCA with (default: the failed task's own result/error).",
)
@click.pass_context
def rca(ctx, task_id: str, error: str | None):
    """Manually kick off an RCA for an existing failed task.

    Creates the RCA root bead (phase=analyze) linked to TASK_ID via
    metadata.rca_for. Refuses to RCA a bead that is itself an RCA bead
    (source=='rca') — that would recurse."""
    from .rca import create_rca_task

    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)
    failed_task = beads.get(task_id)
    if failed_task is None:
        click.echo(f"No such task: {task_id}", err=True)
        sys.exit(1)
    if failed_task.source == "rca":
        click.echo(
            f"Refusing to create an RCA for an RCA bead ({task_id}, source='rca') — would recurse.",
            err=True,
        )
        sys.exit(1)
    err_text = error or failed_task.result or "(no error captured on the task)"
    root = create_rca_task(beads, failed_task, err_text)
    click.echo(json.dumps({"rca_root_id": root.id, "title": root.title, "phase": root.metadata.get("phase")}, indent=2))


@main.command("rca-check")
@click.argument("bead_id")
@click.option(
    "--store",
    default=None,
    help="Path to the node's tasks.jsonl (default: this node's store from "
    "config). The RCA verify gate passes it explicitly so the check reads the "
    "same store the bead lives in, wherever the completing process runs from.",
)
@click.pass_context
def rca_check(ctx, bead_id: str, store: str | None):
    """Exit 0 only if RCA bead BEAD_ID has a terminal action.

    This is the gate behind ``metadata.verify`` on every RCA analyze bead: an
    investigation that produced no fix bead, no applied change and no next
    phase is a no-op that billed full price and left the failure to recur on
    schedule, so it must not reach done. Exits 1 with the reason and the
    command to fix it when nothing followed the analysis.
    """
    from .rca import has_terminal_action

    if store:
        beads = Beads(store)
    else:
        config = Config.load(ctx.obj["config_path"])
        beads = Beads(config.tasks_path)
    bead = beads.get(bead_id)
    if bead is None:
        click.echo(f"No such task: {bead_id}", err=True)
        sys.exit(1)
    follow_up = has_terminal_action(beads, bead)
    if follow_up is not None:
        click.echo(f"terminal action found: {follow_up}")
        return
    click.echo(
        f"{bead_id} has no terminal action: no fix bead, no applied change "
        f"recorded, no next RCA phase. An analysis nobody acts on lets the "
        f"failure recur on schedule — file the fix bead first:\n"
        f'  agentco tasks create "<the fix>" -d "<root cause + the concrete '
        f'minimal change>" --parent {bead_id} -p 1\n'
        f"then complete this bead again (the gate re-runs).",
        err=True,
    )
    sys.exit(1)


def _link_child(
    config: Config,
    name: str,
    child_path: str,
    interval: str,
    notify: bool,
    priority: int = 2,
    force: bool = False,
) -> dict[str, str]:
    """Converge the registry entry AND its verify_child recurring def to the
    linked state — upsert, not create-only, so the two can never drift apart
    AND every half-broken state (crash between the two writes, hand-editing)
    is repairable by re-running this command, exactly as doctor prescribes.

    Returns per-resource outcomes ({"registry": ..., "verify_def": ...},
    each "created" | "updated" | "re-enabled" | "unchanged") so repair runs
    are auditable. Raises ValueError on a bad interval, or when the name is
    already linked to a different path and ``force`` is not set."""
    from .children import ChildRef, ChildRegistry
    from .recurring import Recurring, RecurringDef, parse_duration

    parse_duration(interval)  # validate loudly before touching either file
    registry = ChildRegistry(config.children_registry_path)
    recurring = Recurring(config.recurring_path)

    registry_outcome = registry.upsert(
        ChildRef(
            name=name, path=child_path, expected_interval=interval, notify=notify, priority=priority
        ),
        force=force,
    )

    def_id = f"verify-{name}"
    payload = {"type": "verify_child", "child": name}
    existing = recurring.get(def_id)
    if existing is None:
        recurring.add(
            RecurringDef(
                id=def_id,
                title=f"Verify child instance: {name}",
                schedule={"every": interval},
                payload=payload,
            )
        )
        def_outcome = "created"
    else:
        changes: dict = {}
        if not existing.enabled:
            changes["enabled"] = True
        if existing.schedule.get("every") != interval:
            changes["schedule"] = {"every": interval}
        if existing.payload != payload:
            changes["payload"] = payload
        if changes:
            recurring.update(def_id, **changes)
            parts = (["re-enabled"] if "enabled" in changes else []) + (
                ["updated"] if len(changes) > ("enabled" in changes) else []
            )
            def_outcome = "+".join(parts)
        else:
            def_outcome = "unchanged"
    return {"registry": registry_outcome, "verify_def": def_outcome}


@main.command("link-child")
@click.argument("name")
@click.argument("path", type=click.Path())
@click.option("--interval", default="1h", help="Expected heartbeat interval (e.g. 1h, 1d)")
@click.option("--notify/--no-notify", default=True, help="Notify externally when stale")
@click.option(
    "--priority",
    default=2,
    type=click.IntRange(0, 3),
    help="Company weight for `agentco me` (0=critical … 3=low, default 2)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Allow re-pointing an already-linked name at a different path",
)
@click.pass_context
def link_child(ctx, name: str, path: str, interval: str, notify: bool, priority: int, force: bool):
    """Register a child instance AND its verify_child recurring task.

    One command keeps the registry and the recurring defs in sync — they
    must never drift apart by hand-editing. Idempotent: re-running converges
    a drifted state (missing or disabled verify def, missing registry entry)
    back to fully linked, which is the repair doctor prescribes.
    """
    config = Config.load(ctx.obj["config_path"])

    child_path = str(Path(path).resolve())
    if not Path(child_path).is_dir():
        click.echo(f"WARNING: child path {child_path} does not exist yet", err=True)

    try:
        outcome = _link_child(config, name, child_path, interval, notify, priority, force=force)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"Linked child '{name}' at {child_path} "
        f"(every {interval}, notify={notify}, priority={priority}) "
        f"[registry: {outcome['registry']}, verify def: {outcome['verify_def']}]"
    )


@main.group()
def children():
    """The child registry — the hub's map of every node it watches."""


@children.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def children_list(ctx, as_json: bool):
    """List registered children with their host / capability tags.

    Tags decide routing (`--node` reads a child's manifest) and monitoring (a
    `host` makes the child's path unreadable from here), so they have to be
    visible without opening the JSONL.
    """
    from dataclasses import asdict

    from .children import ChildRegistry

    config = Config.load(ctx.obj["config_path"])
    registry = ChildRegistry(config.children_registry_path)
    rows = registry.list()

    if as_json:
        click.echo(json.dumps([asdict(c) for c in rows], indent=2, ensure_ascii=False))
        return

    if not rows:
        click.echo(f"No children registered ({config.children_registry_path}).")
        return

    for c in rows:
        # "local" and "none" are spelled out rather than left blank: an empty
        # column reads as a truncated line, while "no capabilities" is a fact
        # about routing that the operator needs to actually see.
        caps = ", ".join(c.capabilities) if c.capabilities else "none"
        click.echo(
            f"{c.name}  [{c.type}]  host={c.host or 'local'}  capabilities={caps}  "
            f"interval={c.expected_interval}  priority={c.priority}  path={c.path or '-'}"
        )


@children.command("set-tags")
@click.argument("name")
@click.option(
    "--host",
    default=None,
    help="Machine this node runs on. Setting it makes `path` a path ON THAT "
    "HOST, so this hub stops trying to read a heartbeat off it.",
)
@click.option("--clear-host", is_flag=True, help="Remove the host tag (node is local again)")
@click.option(
    "--capability",
    "capability",
    multiple=True,
    help="Capability token this node owns (repeatable). The flags given become "
    "the node's WHOLE capability set — this converges the row, it does not append.",
)
@click.option("--clear-capabilities", is_flag=True, help="Remove all capability tags")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def children_set_tags(
    ctx,
    name: str,
    host: str | None,
    clear_host: bool,
    capability: tuple[str, ...],
    clear_capabilities: bool,
    as_json: bool,
):
    """Set a registered child's `host` / `capabilities` tags (bead ac-ccd66c91).

    Both fields have existed on `ChildRef` since the capability manifests
    landed, but the only way to write them was to hand-edit a line of
    `children/registry.jsonl` — which `Plans/BreakGlassFailover.md` has to
    prescribe in prose, mid-incident, on the one path where a typo quarantines
    the row that names the lane. This is that edit, validated.

    Tags only: creating a child stays with `link-child`, which writes the
    registry row and its `verify_child` recurring def in the same breath. A
    name this command does not recognise is an error, never a new row — a
    child that exists only in the registry is a lane nothing staffs.

    The capability tag is a CACHE of the node's own manifest, never the
    authority: the claim gate reads the claimant's own config.yaml, so a tag
    here can mislead a human but can never widen a credential boundary.
    """
    from dataclasses import replace

    from .beads import normalize_capabilities
    from .children import ChildRegistry

    if host is not None and clear_host:
        click.echo("Error: --host and --clear-host are mutually exclusive", err=True)
        sys.exit(1)
    if capability and clear_capabilities:
        click.echo(
            "Error: --capability and --clear-capabilities are mutually exclusive",
            err=True,
        )
        sys.exit(1)
    if host is None and not clear_host and not capability and not clear_capabilities:
        click.echo(
            "Error: nothing to change — pass --host / --clear-host and/or "
            "--capability / --clear-capabilities",
            err=True,
        )
        sys.exit(1)

    config = Config.load(ctx.obj["config_path"])
    registry = ChildRegistry(config.children_registry_path)
    child = registry.get(name)
    if child is None:
        click.echo(
            f"Error: unknown child {name!r} — not in {config.children_registry_path}. "
            f"Register it with `agentco link-child {name} <path>` first (that writes "
            f"its verify_child def too).",
            err=True,
        )
        sys.exit(1)

    updated = child
    if clear_host:
        updated = replace(updated, host=None)
    elif host is not None:
        if not host.strip():
            click.echo("Error: --host cannot be empty (use --clear-host)", err=True)
            sys.exit(1)
        updated = replace(updated, host=host.strip())

    if clear_capabilities:
        updated = replace(updated, capabilities=[])
    elif capability:
        # Same normalizer, same STRICT posture the registry reader uses — a
        # token rejected here is exactly a token that would have quarantined
        # the row, surfaced at the keyboard instead of at the next cycle.
        try:
            tokens = normalize_capabilities(
                list(capability), field_name="capabilities", where=f"child {name!r}"
            )
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        updated = replace(updated, capabilities=tokens)

    try:
        outcome = registry.upsert(updated)
    except ValueError as e:  # path unchanged here, so this is belt-and-braces
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "child": updated.name,
                    "host": updated.host,
                    "capabilities": updated.capabilities,
                    "outcome": outcome,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    caps = ", ".join(updated.capabilities) if updated.capabilities else "none"
    click.echo(
        f"Tagged child '{updated.name}' (host={updated.host or 'local'}, "
        f"capabilities={caps}) [registry: {outcome}]"
    )


def _configure_instance(
    root: Path,
    name: str,
    parent_config: Config,
    with_company: bool,
    telegram_chat_id: str | None = None,
) -> list[str]:
    """Make ``root`` a fully configured, up-to-date AgentCo instance.

    Fresh folder → complete scaffold: config inheriting the parent's
    llm/triage/notify settings, task queue, recurring defs, children
    registry, and (optionally) the company/ docs tree + doc registry.

    Existing instance → non-destructive upgrade to the latest structure:
    only files an older version lacks are added (e.g. a pre-0.3.0 instance
    gains recurring.jsonl and children/registry.jsonl beside its queue).
    Existing config and queue are never rewritten.

    Returns the list of created artifacts (empty = already up to date).
    """
    from .beads import Beads
    from .children import ChildRegistry
    from .recurring import Recurring

    created: list[str] = []
    child_config_path = root / "config.yaml"

    if child_config_path.exists():
        child_config = Config.load(child_config_path)
    else:
        root.mkdir(parents=True, exist_ok=True)
        child_config = Config()
        child_config.instance = name
        # The portfolio's LLM/triage/notify settings propagate — a child
        # starts with the operator's real configuration, not stock defaults.
        # Copies, not references: per-company overrides below must never
        # leak back into the parent's in-memory config.
        from dataclasses import replace

        child_config.llm = replace(parent_config.llm)
        child_config.triage = replace(parent_config.triage)
        child_config.notify = replace(parent_config.notify)
        if telegram_chat_id:
            # Each company reports into its own Telegram group.
            child_config.notify.telegram_chat_id = telegram_chat_id
            child_config.notify.cycle_summary = True
        child_config.save(child_config_path)
        created.append(str(child_config_path))
        # Resolve relative tasks_path the same way Config.load does.
        child_config.tasks_path = str(root / child_config.tasks_path)

    # The fractal unit, beside the queue: anything missing is added so a
    # company can itself verify its own projects.
    queue_dir = Path(child_config.tasks_path).parent
    queue_path = Path(child_config.tasks_path)
    if not queue_path.exists():
        Beads(queue_path)
        created.append(str(queue_path))
    recurring_path = Path(child_config.recurring_path)
    if not recurring_path.exists():
        Recurring(recurring_path)
        created.append(str(recurring_path))
    children_path = Path(child_config.children_registry_path)
    if not children_path.exists():
        registry = ChildRegistry(children_path)
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.touch()
        created.append(str(children_path))

    if with_company and not (root / "company").is_dir():
        from .registry import Registry, scan_company
        from .scaffold import scaffold_company

        company_path = scaffold_company(root)
        doc_registry = Registry(root / ".agentco" / "registry.json")
        scan_company(company_path, doc_registry)
        created.append(str(company_path))

    return created


@main.command("add-company")
@click.argument("name")
@click.argument("path", type=click.Path(), required=False)
@click.option("--interval", default="1h", help="Expected heartbeat interval (e.g. 1h, 1d)")
@click.option("--notify/--no-notify", default=True, help="Notify externally when stale")
@click.option(
    "--company/--no-company",
    "with_company",
    default=True,
    help="Scaffold the company/ docs tree (default: yes)",
)
@click.option(
    "--telegram-chat-id",
    default=None,
    help="Telegram group/chat id for this company's cycle summaries (enables cycle_summary)",
)
@click.option(
    "--priority",
    default=2,
    type=click.IntRange(0, 3),
    help="Company weight for `agentco me` (0=critical … 3=low, default 2)",
)
@click.pass_context
def add_company(
    ctx,
    name: str,
    path: str | None,
    interval: str,
    notify: bool,
    with_company: bool,
    telegram_chat_id: str | None,
    priority: int,
):
    """Add a folder as a company root: configure a full AgentCo instance
    there and link it as a child of this instance.

    PATH defaults to ./NAME beside this instance's config. A folder without
    AgentCo gets a complete setup (inheriting this instance's llm/triage/
    notify config); an older instance is upgraded in place — missing files
    are added, existing config and queue are never touched.
    """
    config_path = Path(ctx.obj["config_path"]).resolve()
    config = Config.load(config_path)

    root = Path(path).resolve() if path else config_path.parent / name
    had_config = (root / "config.yaml").exists()

    created = _configure_instance(
        root, name, config, with_company, telegram_chat_id=telegram_chat_id
    )
    if not had_config:
        click.echo(f"Created company instance at {root}")
    elif created:
        click.echo(f"Upgraded existing instance at {root} to the latest structure")
    else:
        click.echo(f"Instance at {root} is already up to date — linking only")
    for item in created:
        click.echo(f"  + {item}")

    try:
        _link_child(config, name, str(root), interval, notify, priority)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"Linked company '{name}' at {root} "
        f"(every {interval}, notify={notify}, priority={priority})"
    )
    if telegram_chat_id:
        click.echo(f"Cycle summaries will post to Telegram chat {telegram_chat_id}.")
    else:
        click.echo(
            "\nSetup step — give this company its own Telegram channel:\n"
            f"  1. Create a Telegram group for '{name}' and add your bot to it\n"
            "  2. Send any message in the group, then grab its chat id (negative\n"
            "     number — e.g. from your bot's logs or @RawDataBot)\n"
            f"  3. Wire it up in {root / 'config.yaml'}:\n"
            "       notify:\n"
            "         telegram_chat_id: \"<group-chat-id>\"\n"
            "         cycle_summary: true\n"
            "     (or re-run add-company with --telegram-chat-id next time)\n"
            "  4. The bot token comes from $TELEGRAM_BOT_TOKEN in the daemon's env"
        )
    # Linking starts the parent's staleness clock immediately, and nothing here
    # installs a scheduler — so a node onboarded and left alone heartbeats once
    # (the cycle below), reads healthy for one interval, then fires a
    # verify_child alarm with nothing wrong inside it. That is the semijoias
    # incident (ac-67fbc23f): 6.9 hours of correct alarms on a node whose only
    # defect was that no launchd job existed. `agentco doctor` now names this as
    # BROKEN, but the cheapest place to close it is here, at onboarding.
    plist = f"com.{name}.agentco.plist"
    click.echo(
        f"\nREQUIRED — give '{name}' a scheduler, or it will go stale and alarm:\n"
        f"  1. Run one cycle now:  agentco -c {root / 'config.yaml'} cycle\n"
        f"  2. Install a LaunchAgent at ~/Library/LaunchAgents/{plist} running\n"
        f"     'agentco cycle' with WorkingDirectory={root}\n"
        f"     and StartInterval matching the {interval} cadence you just registered\n"
        f"  3. launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{plist}\n"
        f"  4. Confirm:  agentco doctor --class broken"
    )


@main.command()
@click.option(
    "--by",
    "group_by",
    default="agent",
    type=click.Choice(["agent", "model_used", "company", "task_type", "data_class", "requested_model"]),
    help="Dimension to aggregate by",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def cost(ctx, group_by: str, as_json: bool):
    """Cost and latency per completed bead — the model-routing evidence.

    The headline number is $/done (cost per COMPLETED bead), not cost per
    token: a model at half the per-token price that burns twice the tokens
    is not cheaper, and only a completion-denominated metric shows it.
    """
    config = Config.load(ctx.obj["config_path"])
    entries = read_ledger(config.tasks_path)
    rows = summarize(entries, group_by=group_by)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    click.echo(format_table(rows, group_by))


@main.command("usage")
@click.option(
    "--by",
    "group_by",
    default="day",
    type=click.Choice(list(usage_mod.GROUP_KEYS)),
    help="Dimension to aggregate by",
)
@click.option("--days", default=0, help="Only rows from the last N days (0 = all)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def usage_cmd(ctx, group_by: str, days: int, as_json: bool):
    """Token + cost telemetry — one row per metered model invocation.

    Reads `usage.jsonl` beside this node's task store. Every model-invoking
    execution path writes exactly one row, attributed to the bead, lane and
    node that caused it, so spend is answerable rather than inferred.

    A dash in the token or $ column means the route did not report the number —
    never that it was zero.
    """
    config = Config.load(ctx.obj["config_path"])
    rows = usage_mod.within(usage_mod.read_ledger(config.tasks_path), days or None)
    summary = usage_mod.summarize(rows, group_by=group_by)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "node": usage_mod.node_name(config.tasks_path),
                    "group_by": group_by,
                    "days": days or None,
                    "totals": usage_mod.totals(rows),
                    "groups": summary,
                },
                indent=2,
            )
        )
        return
    click.echo(usage_mod.format_table(summary, group_by))


@main.group("schedules")
def schedules_group():
    """The schedule registry and the expected-vs-observed audit.

    A recurring definition records INTENT and the bead store records EFFECTS;
    neither records FIRINGS, so "this schedule has not run for ten days" was
    not a question v1 could be asked. These commands answer it.
    """


@schedules_group.command("list")
@click.option("--all-nodes", is_flag=True, help="Include every registered local child node")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def schedules_list_cmd(ctx, all_nodes: bool, as_json: bool):
    """Every schedule this node (or the portfolio) owns."""
    config_path = ctx.obj["config_path"]
    if all_nodes:
        found = schedules_mod.portfolio_registry(config_path)
    else:
        found = schedules_mod.registry(Config.load(config_path))
    if as_json:
        click.echo(json.dumps([s.to_dict() for s in found], indent=2))
        return
    if not found:
        click.echo("No schedules defined.")
        return
    header = f"{'SCHEDULE':28} {'NODE':16} {'EVERY':6} {'ON':3} FIRES"
    click.echo(header)
    click.echo("-" * len(header))
    for s in found:
        click.echo(
            f"{s.id[:28]:28} {s.node[:16]:16} {(s.interval or s.cron or '-')[:6]:6} "
            f"{'yes' if s.enabled else 'no':3} {s.fires[:48]}"
        )


@schedules_group.command("audit")
@click.option("--days", "window_days", default=schedules_mod.DEFAULT_WINDOW_DAYS,
              show_default=True, help="Trailing window to audit, in days")
@click.option("--min-periods", "min_periods",
              default=schedules_mod.DEFAULT_MIN_SILENT_PERIODS, show_default=True,
              help="Expected periods with zero firings before a schedule is a finding")
@click.option("--all-nodes", is_flag=True, help="Audit every registered local child node too")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def schedules_audit_cmd(ctx, window_days: int, min_periods: int, all_nodes: bool, as_json: bool):
    """Expected firings versus observed — the silent-non-execution detector.

    Exits `4` (consequence class `liveness`) when any enabled schedule expected
    at least `--min-periods` firings in the window and produced ZERO. It cannot
    return a deployment or integrity code: a schedule that stopped firing must
    never be able to change the input of a consumer that gates deploys.

    Disabled schedules are excluded, not reported healthy. Observations come
    from `schedules.jsonl` plus firings reconstructed from the bead store, so
    the audit has a real answer on the day it ships.
    """
    config_path = ctx.obj["config_path"]
    if all_nodes:
        results = schedules_mod.audit_portfolio(
            config_path, window_days=window_days, min_silent_periods=min_periods
        )
    else:
        results = schedules_mod.audit_node(
            Config.load(config_path), window_days=window_days, min_silent_periods=min_periods
        )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "window_days": window_days,
                    "min_silent_periods": min_periods,
                    "consequence_class": schedules_mod.CONSEQUENCE_CLASS,
                    "silent": [r.schedule.id for r in results if r.silent],
                    "schedules": [r.to_dict() for r in results],
                },
                indent=2,
            )
        )
    else:
        click.echo(schedules_mod.format_audit(results))
    ctx.exit(schedules_mod.exit_code(results))


@main.command("sweep-stale")
@click.option("--dry-run", is_flag=True, help="Report what would close, change nothing")
@click.pass_context
def sweep_stale(ctx, dry_run: bool):
    """Close recurring failures that a later run of the same check disproved.

    A recurring bead is a sample, not a task. When a health check fails at 14:00 and
    passes at 15:00, the 14:00 failure stopped being actionable — but nothing closed
    it, so failures accumulated forever and sorted to the top of the queue as "needs
    you". This runs every heartbeat now; the command exists for the one-time backfill
    and for auditing with --dry-run.

    The most recent sample is never touched: if the last run failed, that is live news.
    """
    from .recurring import (
        _sampler_family,
        supersede_resolved_rcas,
        supersede_stale_failures,
    )

    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    if dry_run:
        # MUST use the same family helper the real sweep uses. An earlier version
        # reimplemented the grouping here, reported 0, and the real run then closed
        # 34 — a dry run that disagrees with the run is worse than no dry run.
        from collections import Counter

        from .beads import TaskStatus

        by_def: dict = {}
        for t in beads._read_all():
            family = _sampler_family(t)
            if family:
                by_def.setdefault(family, []).append(t)
        counts: Counter = Counter()
        for family, tasks in by_def.items():
            newest_ok = max(
                (t.created_at for t in tasks if t.status == TaskStatus.DONE), default=None
            )
            if newest_ok is None:
                continue
            counts[family] = sum(
                1 for t in tasks if t.status == TaskStatus.FAILED and t.created_at < newest_ok
            )
        total = sum(counts.values())
        for family, n in counts.most_common():
            if n:
                click.echo(f"  {n:4d}  {family}")
        click.echo(f"would supersede {total} stale failure(s)")
        return

    closed = supersede_stale_failures(beads)
    click.echo(f"superseded {len(closed)} stale sampler failure(s)")
    moot = supersede_resolved_rcas(beads)
    click.echo(f"closed {len(moot)} RCA(s) whose subject failure is resolved")


@main.group()
def eval():
    """Evidence built from production telemetry."""


@eval.command("routing")
@click.option(
    "--by",
    "group_by",
    default="task_type",
    type=click.Choice(["task_type", "agent", "company", "data_class"]),
    help="Dimension to compare models within",
)
@click.option(
    "--portfolio",
    is_flag=True,
    help="Walk every registered child read-only and pool their ledgers",
)
@click.option(
    "--min-samples",
    default=DEFAULT_MIN_SAMPLES,
    show_default=True,
    help="Runs a group needs before it may produce a recommendation",
)
@click.option(
    "--min-arm-samples",
    default=DEFAULT_MIN_ARM_SAMPLES,
    show_default=True,
    help="Runs a single model needs before it counts as a comparable arm",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def eval_routing(
    ctx, group_by: str, portfolio: bool, min_samples: int, min_arm_samples: int, as_json: bool
):
    """Which model to route each task type to, from real runs.

    Supersedes the synthetic harness in Plans/ModelRoutingEval.md: per-bead
    telemetry (ISC-122..129) turns every production cycle into an eval sample,
    so the evidence arrives free instead of being manufactured offline.

    A group only produces a recommendation once it has enough runs across at
    least two comparable models AND the outcomes actually discriminate. A
    group where every model succeeds equally is ranked on cost alone; one where
    everything fails is reported as a work problem, not a routing one. Thin
    evidence reports as INSUFFICIENT rather than guessing.

    Advisory only — this never edits config and never routes anything.
    """
    config_path = ctx.obj["config_path"]
    if portfolio:
        entries = portfolio_ledger(config_path)
    else:
        config = Config.load(config_path)
        entries = read_ledger(config.tasks_path)

    health, results = evaluate(
        entries,
        group_by=group_by,
        min_samples=min_samples,
        min_arm_samples=min_arm_samples,
    )
    if as_json:
        click.echo(json.dumps(to_json(health, results), indent=2))
        return
    click.echo(format_report(health, results, group_by, min_samples))


@main.command()
@click.option("--count", "-n", default=10, help="How many recent runs to show")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def runs(ctx, count: int, as_json: bool):
    """Show recent heartbeat cycle executions (newest first)."""
    config = Config.load(ctx.obj["config_path"])
    runs_path = Path(config.runs_path)
    if not runs_path.exists():
        click.echo("No runs recorded yet — run 'agentco cycle' first.")
        return

    records = []
    with open(runs_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a torn line; the log is append-only
    records = records[-count:][::-1]

    if as_json:
        click.echo(json.dumps(records, indent=2))
        return

    if not records:
        click.echo("No runs recorded yet.")
    for r in records:
        mark = "✅" if not r.get("errors") else "⚠️"
        click.echo(
            f"{mark} {r['at']}  [{r.get('instance', '?')}]  "
            f"done={r.get('executed', 0)} errors={r.get('errors', 0)} "
            f"spawned={r.get('spawned', 0)} open={r.get('open_after', 0)}"
        )
        for t in r.get("tasks", []):
            icon = "✅" if t.get("outcome") == "done" else "❌"
            line = f"    {icon} [{t['id']}] {t['title']} ({t.get('agent') or '-'})"
            if t.get("error"):
                line += f" — {t['error'][:100]}"
            click.echo(line)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def attention(ctx, as_json: bool):
    """Show what needs a human: failed tasks, blocked tasks, unhealthy children."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    failed = beads.list(status=TaskStatus.FAILED)
    blocked = beads.list(status=TaskStatus.BLOCKED)
    pending = beads.list(status=TaskStatus.PENDING)
    done_ids = {t.id for t in beads.list(status=TaskStatus.DONE)}
    waiting = [t for t in pending if any(b not in done_ids for b in t.blocked_by)]

    from .children import ChildRegistry, verify_child

    registry = ChildRegistry(config.children_registry_path)
    unhealthy = [
        r for r in (verify_child(c) for c in registry.list()) if r["level"] != "ok"
    ]

    if as_json:
        click.echo(
            json.dumps(
                {
                    "failed": [json.loads(t.to_json()) for t in failed],
                    "blocked": [json.loads(t.to_json()) for t in blocked + waiting],
                    "quarantined": len(beads._quarantined),
                    "children": unhealthy,
                },
                indent=2,
            )
        )
        return

    if not (failed or blocked or waiting or unhealthy or beads._quarantined):
        click.echo("Nothing needs attention. ✅")
        return

    if failed:
        click.echo(f"❌ {len(failed)} failed task(s) — retry with 'agentco tasks retry <id>':")
        for t in failed:
            err = (t.result or "")[:100]
            click.echo(f"  [{t.id}] {t.title} ({t.assigned_agent or '-'}) — {err}")
    if blocked or waiting:
        click.echo(f"🚧 {len(blocked) + len(waiting)} blocked/waiting task(s):")
        for t in blocked + waiting:
            click.echo(f"  [{t.id}] {t.title} — blocked_by: {', '.join(t.blocked_by) or '?'}")
    if beads._quarantined:
        click.echo(f"🗃️ {len(beads._quarantined)} quarantined queue line(s) — see 'agentco doctor'")
    for r in unhealthy:
        # A remote node is informational, not an alarm — it is reported here
        # only because this list shows every non-ok level, and its status
        # genuinely arrives through the mirror rather than a local heartbeat.
        icon = {"fail": "🚨", "remote": "🌐"}.get(r["level"], "⚠️")
        click.echo(f"{icon} child '{r['child']}': {r['detail']}")


@main.command("me")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--limit", "-n", default=0, help="Show only the top N items (0 = all)")
@click.pass_context
def me(ctx, as_json: bool, limit: int):
    """The human work queue: everything that depends on YOU, portfolio-wide.

    Walks this instance and every registered child recursively (read-only),
    collects approvals, failures, needs-input results, blocked tasks, and
    stale children, and ranks them by company priority × severity × age
    plus unblock leverage — how much machine work waits behind each item.
    """
    from .me import ranked

    items = ranked(ctx.obj["config_path"])
    if limit > 0:
        items = items[:limit]

    if as_json:
        click.echo(json.dumps([i.to_dict() for i in items], indent=2))
        return

    if not items:
        click.echo("Nothing depends on you right now. ✅")
        return

    icons = {
        "stale_child": "🚨",
        "failed": "❌",
        "human_assigned": "🧑",
        "needs_input": "🙋",
        "approval": "☑️",
        "blocked": "🚧",
        "verify_gate": "🛡️",
    }
    click.echo(f"You have {len(items)} item(s), highest leverage first:\n")
    for rank, i in enumerate(items, 1):
        age_h = i.age_seconds / 3600
        age = f"{age_h:.0f}h" if age_h < 48 else f"{age_h / 24:.0f}d"
        lev = f", unblocks {i.leverage}" if i.leverage else ""
        click.echo(f"{rank}. {icons[i.kind]} [{i.company}] {i.title}")
        click.echo(f"     {i.detail} ({age} old{lev})")
        # Temporal line only when the bead actually carries time data — an
        # untimed queue looks exactly as it did before tempo existed.
        if i.slack_hours is not None:
            if i.slack_hours < 0:
                mark = f"⏰ {abs(i.slack_hours):.1f}h PAST the point of no return"
            elif i.slack_hours < 24:
                mark = f"⏳ start within {i.slack_hours:.1f}h"
            else:
                mark = f"🕓 {i.slack_hours / 24:.1f}d of runway"
            click.echo(f"     {mark} — {i.why}")
        click.echo(f"     → {i.resolve}")


@main.command("tempo")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--hours-per-day", default=6.0, help="Human working hours per day")
@click.option("--horizon-days", default=14.0, help="Planning horizon")
@click.option(
    "--portfolio",
    is_flag=True,
    help="Check feasibility across this instance AND every registered child. "
    "The graph is per-company but your hours are not — per-node feasibility "
    "systematically understates the load.",
)
@click.pass_context
def tempo_cmd(
    ctx, as_json: bool, hours_per_day: float, horizon_days: float, portfolio: bool
):
    """Can you actually finish it? Feasibility over the deadline-bound queue.

    Runs the backward pass across the bead graph, then fits the deadline-bound
    work earliest-deadline-first. EDF is provably optimal for single-resource
    feasibility, so if this ordering cannot fit the work, no ordering can —
    the slip list is a fact, not a guess.

    Pinned commitments consume capacity before anything flexible is placed.
    Agent-owned beads are excluded: they scale horizontally.
    """
    from .beads import Beads
    from .config import Config
    from .tempo import expected_hours, feasibility, schedule

    if portfolio:
        from .me import portfolio_tasks

        tasks = portfolio_tasks(ctx.obj["config_path"])
    else:
        config = Config.load(ctx.obj["config_path"])
        tasks = Beads(config.tasks_path).list()
    result = feasibility(
        tasks, hours_per_day=hours_per_day, horizon_days=horizon_days
    )
    scheds = schedule(tasks)
    by_id = {t.id: t for t in tasks}

    if as_json:
        click.echo(
            json.dumps(
                {
                    "feasible": result.feasible,
                    "committed_hours": result.committed_hours,
                    "available_hours": result.available_hours,
                    "overload_ratio": round(result.overload_ratio, 3),
                    "slip": result.slip,
                    "cyclic": result.cyclic,
                },
                indent=2,
            )
        )
        return

    if result.cyclic:
        click.echo(
            f"🔁 {len(result.cyclic)} bead(s) are in a dependency cycle — "
            "no schedule exists for them until it is broken:"
        )
        for tid in result.cyclic:
            click.echo(f"     {tid}  {by_id[tid].title if tid in by_id else ''}")
        click.echo("")

    click.echo(
        f"{result.committed_hours:.1f}h committed against "
        f"{result.available_hours:.1f}h available "
        f"({result.overload_ratio * 100:.0f}% loaded)\n"
    )

    if result.feasible:
        click.echo("✅ Every deadline in the horizon is reachable at current pace.")
    else:
        click.echo(f"⚠️  {len(result.slip)} item(s) will miss their deadline:\n")
        for tid in result.slip:
            task = by_id.get(tid)
            sched = scheds.get(tid)
            if not task:
                continue
            due = sched.effective_due.strftime("%a %d %b %H:%M") if sched and sched.effective_due else "?"
            click.echo(f"   ❌ {task.title}")
            click.echo(f"      {expected_hours(task):.1f}h of work, due {due}")
        click.echo("\n   Move a deadline, cut the work, or delegate to an agent.")


# Recurring task definition commands
@main.group()
def recurring():
    """Recurring task definitions."""
    pass


@recurring.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def recurring_list(ctx, as_json: bool):
    """List recurring task definitions."""
    from .recurring import Recurring

    config = Config.load(ctx.obj["config_path"])
    store = Recurring(config.recurring_path)
    defs = store.list()

    if as_json:
        click.echo(json.dumps([json.loads(d.to_json()) for d in defs], indent=2))
    else:
        if not defs:
            click.echo("No recurring definitions.")
        for d in defs:
            state = "✅" if d.enabled else "⏸️"
            click.echo(
                f"{state} [{d.id}] {d.title} — every {d.schedule['every']} "
                f"(agent: {d.agent or '-'}, catch_up: {d.catch_up}, "
                f"last: {d.last_spawned or 'never'})"
            )
        if store._quarantined:
            click.echo(
                f"WARNING: {len(store._quarantined)} unparseable definition line(s) "
                f"quarantined — run 'agentco doctor' for details.",
                err=True,
            )


@recurring.command("add")
@click.argument("title")
@click.option("--id", "def_id", default=None, help="Definition ID (default: derived from title)")
@click.option("--every", required=True, help="Interval (e.g. 15m, 1h, 1d, 7d)")
@click.option("--agent", "-a", default=None, help="Agent (cs/pm/dev/devops/analyst/claude)")
@click.option("--prompt", default=None, help="Prompt for claude executor tasks")
@click.option("--catch-up", type=click.Choice(["latest", "all"]), default="latest")
@click.option("--timeout", type=int, default=None, help="Claude budget: seconds")
@click.option("--max-turns", type=int, default=None, help="Claude budget: max turns")
@click.pass_context
def recurring_add(ctx, title, def_id, every, agent, prompt, catch_up, timeout, max_turns):
    """Add a recurring task definition."""
    from .recurring import Recurring, RecurringDef

    config = Config.load(ctx.obj["config_path"])
    store = Recurring(config.recurring_path)

    def_id = def_id or "rec-" + "-".join(title.lower().split())[:40]
    payload = {}
    if prompt:
        payload["prompt"] = prompt
    budget = {}
    if timeout:
        budget["timeout"] = timeout
    if max_turns:
        budget["max_turns"] = max_turns

    try:
        store.add(
            RecurringDef(
                id=def_id,
                title=title,
                schedule={"every": every},
                agent=agent,
                payload=payload,
                catch_up=catch_up,
                budget=budget or None,
            )
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Added recurring definition: {def_id} (every {every})")


@main.command()
@click.pass_context
def status(ctx):
    """Show current status."""
    config = Config.load(ctx.obj["config_path"])
    orchestrator = Orchestrator(config)
    status = orchestrator.status()
    click.echo(json.dumps(status, indent=2))
    if not status.get("last_work_at"):
        click.echo("WARNING: no successful work cycle has ever been recorded.", err=True)
    for child in status.get("children", []):
        if child["level"] == "fail":
            click.echo(
                f"WARNING: child '{child['child']}' is unhealthy — {child['detail']}",
                err=True,
            )
    if status.get("quarantined"):
        click.echo(
            f"WARNING: {status['quarantined']} unparseable task line(s) quarantined "
            f"— run 'agentco doctor' for details.",
            err=True,
        )


@main.command()
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Don't write — exit 0 if PRIME.md is fresh, nonzero (with reasons) if stale",
)
@click.pass_context
def prime(ctx, check_only: bool):
    """Generate (or check) this node's PRIME.md context cache.

    Extractive pointers only — paths, entry points, commit subjects, one purpose
    line quoted from the repo. Stamped with git HEAD + source hashes, so
    staleness is content-based rather than a time window.
    """
    from . import prime as prime_mod

    config = Config.load(ctx.obj["config_path"])
    directory = prime_mod.node_dir(config)

    if check_only:
        try:
            fresh, reasons = prime_mod.check(directory)
        except prime_mod.PrimeError as e:
            click.echo(f"❌ {e}", err=True)
            sys.exit(1)
        if fresh:
            click.echo(f"✅ {directory / prime_mod.PRIME_FILENAME} is fresh")
            sys.exit(0)
        click.echo(f"⚠️  {directory / prime_mod.PRIME_FILENAME} is stale:", err=True)
        for reason in reasons:
            click.echo(f"   - {reason}", err=True)
        sys.exit(1)

    try:
        written = prime_mod.write(directory)
    except prime_mod.PrimeError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)
    click.echo(f"Wrote {written} ({written.stat().st_size} bytes)")


@main.command()
@click.option(
    "--class",
    "classes",
    multiple=True,
    type=click.Choice(["broken", "degraded", "info", "ok"]),
    help="Only PRINT findings of this consequence class (repeatable). "
         "Never changes the exit code — a filtered view cannot hide a broken node.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the classified report as JSON")
@click.pass_context
def doctor(ctx, classes: tuple[str, ...], as_json: bool):
    """Run preflight health checks, classified by consequence.

    Exit codes are derived from the classes present, not from a failure count:

    \b
      0  all clear (only ok/info findings)
      1  at least one BROKEN — something that should be working is not
      2  DEGRADED only — working, with reduced capability or an unverified
         assumption

    A single BROKEN can never be masked by advisory lines, by a DEGRADED with a
    numerically larger code, or by `--class`.
    """
    from .doctor import run_doctor

    sys.exit(run_doctor(ctx.obj["config_path"], classes=classes or None, as_json=as_json))


# Task management commands
@main.group()
def tasks():
    """Task management commands."""
    pass


@tasks.command("list")
@click.option("--status", "-s", type=click.Choice(["pending", "pending_approval", "in_progress", "blocked", "done", "failed", "skipped", "awaiting_verify", "verify_failed"]))
@click.option("--agent", "-a", help="Filter by assigned agent")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def tasks_list(ctx, status: str | None, agent: str | None, as_json: bool):
    """List tasks."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    status_enum = TaskStatus(status) if status else None
    tasks = beads.list(status=status_enum, assigned_agent=agent)

    if as_json:
        click.echo(json.dumps([json.loads(t.to_json()) for t in tasks], indent=2))
    else:
        for t in tasks:
            priority_mark = ["🔴", "🟠", "🟡", "🟢"][t.priority.value]
            status_mark = {
                TaskStatus.PENDING: "⏳",
                TaskStatus.PENDING_APPROVAL: "🔔",
                TaskStatus.IN_PROGRESS: "🔄",
                TaskStatus.BLOCKED: "🚧",
                TaskStatus.DONE: "✅",
                TaskStatus.FAILED: "❌",
                TaskStatus.SKIPPED: "⏭️",
                TaskStatus.AWAITING_VERIFY: "🔍",
                TaskStatus.VERIFY_FAILED: "🚨",
            }.get(t.status, "❓")
            click.echo(f"{status_mark} {priority_mark} [{t.id}] {t.title} → {t.assigned_agent or '?'}")


@tasks.command("backfill-natural-keys")
@click.option(
    "--store",
    "store_paths",
    multiple=True,
    type=click.Path(),
    help="Bead store to process. Repeat for several. Defaults to this node's "
    "tasks.jsonl.",
)
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    help="Actually write. Without it this is a dry run that reports what WOULD "
    "be stamped and nothing else.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def tasks_backfill_natural_keys(ctx, store_paths, do_apply: bool, as_json: bool):
    """Stamp metadata.natural_key onto historical beads, and COUNT the duplicates.

    Additive only: rows are walked as raw JSON dicts (never through
    Task.from_json, which drops unknown fields), a row that already carries a
    key is left alone, an unparseable row is copied through verbatim, and the
    write is atomic. No field is ever removed.

    The number that matters is `duplicate_beads`: beads that would not exist if
    the uniqueness index had been there. It is the measurement that justifies
    the index, so it is reported even in dry-run.
    """
    from .natural_key import backfill_store

    if store_paths:
        paths = [Path(p) for p in store_paths]
    else:
        paths = [Path(Config.load(ctx.obj["config_path"]).tasks_path)]

    reports = [backfill_store(p, apply=do_apply) for p in paths]

    if as_json:
        click.echo(json.dumps([r.to_dict() for r in reports], indent=2))
        return

    for r in reports:
        click.echo(f"\n{r.path}")
        click.echo(
            f"  rows={r.total_rows} already_keyed={r.already_keyed} "
            f"keyed={r.keyed} not_derivable={r.not_derivable} "
            f"unparseable={r.unparseable_rows}"
        )
        click.echo(
            f"  colliding_keys={r.colliding_keys} duplicate_beads={r.duplicate_beads}"
        )
        for key, ids in sorted(r.collisions.items(), key=lambda kv: -len(kv[1]))[:10]:
            click.echo(f"    {len(ids)}x {key}")
            click.echo(f"       kept: {ids[0]}  duplicates: {', '.join(ids[1:])}")
    total_dupes = sum(r.duplicate_beads for r in reports)
    click.echo(
        f"\n{'APPLIED' if do_apply else 'DRY RUN — nothing written'}: "
        f"{sum(r.keyed for r in reports)} bead(s) keyed, "
        f"{total_dupes} historical duplicate(s) revealed."
    )
    if not do_apply:
        click.echo("Re-run with --apply to write.")


@tasks.command("ready")
@click.option("--agent", "-a", help="Filter by assigned agent")
@click.pass_context
def tasks_ready(ctx, agent: str | None):
    """Show ready tasks (pending with no blockers)."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    tasks = beads.ready(assigned_agent=agent)
    for t in tasks:
        priority_mark = ["🔴", "🟠", "🟡", "🟢"][t.priority.value]
        click.echo(f"{priority_mark} [{t.id}] {t.title} → {t.assigned_agent or '?'}")


@tasks.command("create")
@click.argument("title")
@click.option("--description", "-d", default="", help="Task description")
@click.option("--priority", "-p", type=int, default=2, help="Priority 0-3 (0=critical)")
@click.option("--agent", "-a", help="Assigned agent")
@click.option(
    "--assign",
    default=None,
    help="Assign to a human executor, e.g. 'human:alice' (excluded from the "
    "agent queue, surfaced in `agentco me`)",
)
@click.option(
    "--task-class",
    type=click.Choice(["personal", "company", "agent", "ritual"]),
    default=None,
    help="Stored in metadata.task_class (not an isolation mechanism). Not purely "
    "an annotation: `agent` — work the DA owes itself — carries a budget floor "
    "of 1800s/120 turns when the bead has no explicit --timeout/--max-turns and "
    "no parent to inherit from (see orchestrator.TASK_CLASS_BUDGETS). `ritual` — "
    "machine-generated recurring rituals (StandUp/StandDown/etc, filed by "
    "~/Portfolio/rituals/run.sh) — distinguishes them from genuine ad-hoc "
    "manual agent work so they don't fall into the `agent`-class verify gate "
    "(ac-fcc95ca5) by default.",
)
@click.option(
    "--due",
    default=None,
    help="Deadline, ISO-8601 (e.g. 2026-08-08T17:00). Makes this a DUE bead the "
    "tempo backward pass ranks.",
)
@click.option(
    "--starts-at",
    default=None,
    help="Fixed start time, ISO-8601. Makes this a PIN bead (a commitment — "
    "consumes capacity, never re-ranked). Mutually exclusive with --due.",
)
@click.option(
    "--estimate",
    type=float,
    default=None,
    help="Most-likely effort in hours (PERT m)",
)
@click.option(
    "--estimate-range",
    nargs=2,
    type=float,
    default=None,
    help="Optimistic and pessimistic hours (PERT o p) — enables chain "
    "confidence, e.g. --estimate-range 1 6",
)
@click.option(
    "--blocked-by",
    multiple=True,
    help="Task id that must finish first. Repeat the flag for several — one id "
    "per flag. Each must be a real, existing ac-xxxxxxxx id; duplicates are "
    "deduplicated.",
)
@click.option(
    "--parent",
    "parent_id",
    default=None,
    help="Parent task id — makes this a sub-bead of a goal. Must already exist. "
    "Refused if it would exceed the decomposition depth limit.",
)
@click.option(
    "--verify",
    "verify_json",
    default=None,
    help="Verify payload as JSON, e.g. "
    "'{\"class\": \"deterministic\", \"check\": \"uv run pytest -q\"}'. "
    "Classes: deterministic (command must exit 0) | human (approval). "
    "The bead cannot reach done until this gate passes.",
)
@click.option(
    "--verify-check",
    default=None,
    help="Shorthand for the common case: a shell command, no JSON/quoting. "
    "Equivalent to --verify '{\"class\": \"deterministic\", \"check\": \"<cmd>\"}'. "
    "Use --verify directly for a human-class gate or cwd/timeout_s overrides. "
    "Mutually exclusive with --verify.",
)
@click.option(
    "--requires",
    multiple=True,
    help="Capability the executing NODE must declare, e.g. ado-write. Repeat "
    "the flag for several — one token per flag. Only a node whose config.yaml "
    "`capabilities:` covers all of them may claim this bead.",
)
# --- execution budget (ac-f698a0c3) ------------------------------------------
# The CLI had no budget surface at all, so every bead an agent filed was born
# with metadata={} and silently inherited executor.DEFAULT_TIMEOUT (600s). That
# killed both fix beads the @aidotengineer RCA filed (ac-83b2f89b, ac-7ea4b8a1)
# at exactly 600s: a bead that must read an RCA, find the code, edit it and run
# the suite is strictly MORE work than the 1800s analysis that spawned it. Same
# flags and same metadata.budget shape as `recurring add`, so the two intake
# paths budget a bead identically.
@click.option(
    "--timeout",
    type=int,
    default=None,
    help="Execution budget: wall-clock seconds for the subagent "
    "(metadata.budget.timeout). Defaults to 600s — raise it for anything that "
    "edits code and runs a test suite.",
)
@click.option(
    "--max-turns",
    type=int,
    default=None,
    help="Execution budget: max agent turns (metadata.budget.max_turns). Defaults to 50.",
)
# --- SOP block (ac-7ced1c85) -------------------------------------------------
# Named flags rather than one `--sop '{json}'`, deliberately breaking from the
# `--verify` precedent. --verify is JSON because its shape is VARIANT: a class
# enum, `check` xor `checks`, optional cwd/timeout. The SOP block is flat —
# four prose strings and a bounded list — so JSON buys no expressiveness and
# costs shell-quoting five sentences of prose, which is exactly the friction
# that makes an optional documentation field never get filled. The programmatic
# path is not the CLI anyway: planners call beads.create(metadata={"sop": ...})
# and hit validate_sop() directly.
@click.option(
    "--sop-purpose",
    default=None,
    help="SOP: why this work exists — the outcome it serves, not the steps.",
)
@click.option(
    "--sop-trigger",
    default=None,
    help="SOP: what fires this work (an event, a schedule, a condition).",
)
@click.option(
    "--sop-inputs",
    default=None,
    help="SOP: what the executor needs in hand before starting.",
)
@click.option(
    "--dod",
    "definition_of_done",
    default=None,
    help="SOP: definition of done — the bead's ISC in prose. Says what is TRUE "
    "when this is finished, not what was done. Pair with --verify when done is "
    "machine-checkable.",
)
@click.option(
    "--mistake",
    "mistakes",
    multiple=True,
    help="SOP: a known way this work goes wrong. Repeat for up to 3 — one per "
    "flag. This is the field that makes a handoff survive first contact; the "
    "cap of 3 forces the ones that actually bite.",
)
@click.pass_context
def tasks_create(
    ctx,
    title: str,
    description: str,
    priority: int,
    agent: str | None,
    assign: str | None,
    task_class: str | None,
    due: str | None,
    starts_at: str | None,
    estimate: float | None,
    estimate_range: tuple[float, float] | None,
    blocked_by: tuple[str, ...],
    parent_id: str | None,
    verify_json: str | None,
    verify_check: str | None,
    requires: tuple[str, ...],
    timeout: int | None,
    max_turns: int | None,
    sop_purpose: str | None,
    sop_trigger: str | None,
    sop_inputs: str | None,
    definition_of_done: str | None,
    mistakes: tuple[str, ...],
):
    """Create a task manually.

    Use --assign human:<name> to hand a task to a person: it never enters the
    agent dispatch loop and shows up in `agentco me`. A human-assigned task
    fires a best-effort assignment push (Telegram if configured).

    --parent, --blocked-by and --verify are the plan-to-beads intake: a planned
    goal decomposes into sub-beads that carry their own sequencing and their own
    definition of done, filed in one command each.

    The SOP flags (--sop-purpose/--sop-trigger/--sop-inputs, --dod, --mistake)
    make a bead delegation-ready: fill them on anything handed to an agent or
    another person, so the executor is not reconstructing intent from a title.
    """
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    if assign is not None:
        # Config honesty: a disabled humans section must refuse loudly, not
        # accept an assignment the operator believes is off.
        if not config.humans.enabled:
            click.echo(
                "humans.enabled is false — refusing to assign a task to a human. "
                "Set humans.enabled: true in config.yaml to use --assign.",
                err=True,
            )
            sys.exit(1)
        # Assignee grammar: only 'human:<name>' is recognized (Stage 1). An
        # unknown scheme is refused here rather than quarantined at dispatch.
        if not (assign.startswith("human:") and len(assign) > len("human:")):
            click.echo(
                f"Unrecognized assignee {assign!r} — only 'human:<name>' is "
                f"supported (e.g. --assign human:alice).",
                err=True,
            )
            sys.exit(1)

    # PIN vs DUE are different shapes of work: a bead may carry neither, but
    # never both — an appointment has no flexible "when" to rank.
    if due is not None and starts_at is not None:
        click.echo(
            "--due and --starts-at are mutually exclusive: a fixed-time "
            "commitment (PIN) has no flexible deadline to rank. Pick one.",
            err=True,
        )
        sys.exit(1)
    for label, stamp in (("--due", due), ("--starts-at", starts_at)):
        if stamp is not None:
            from datetime import datetime

            try:
                datetime.fromisoformat(stamp)
            except ValueError:
                click.echo(
                    f"{label} {stamp!r} is not ISO-8601 (e.g. 2026-08-08T17:00). "
                    f"Refusing: a silently unparseable deadline would rank as "
                    f"'no deadline', the opposite of what you asked for.",
                    err=True,
                )
                sys.exit(1)

    # A budget of zero or less is a bead that can never run. Refuse it here
    # rather than writing a metadata.budget the executor would turn into an
    # instant TimeoutExpired.
    for label, value in (("--timeout", timeout), ("--max-turns", max_turns)):
        if value is not None and value <= 0:
            click.echo(
                f"{label} must be a positive integer (got {value}). Task NOT created.",
                err=True,
            )
            sys.exit(1)

    # Default task_class to "agent" whenever -a/--agent is set and the caller
    # did not explicitly pass --task-class. This is what makes the verify
    # gate's hard block (ac-fcc95ca5) apply without depending on someone
    # remembering to type --task-class agent at filing time. An explicit
    # --task-class always wins (e.g. -a claude --task-class company stays
    # company) — this is a real "was it provided" check (task_class is None
    # only when --task-class was never passed; "agent" is not a falsy value
    # that could collide with an unset default).
    if task_class is None and agent:
        task_class = "agent"

    metadata = {}
    if task_class is not None:
        metadata["task_class"] = task_class

    budget = {}
    if timeout is not None:
        budget["timeout"] = timeout
    if max_turns is not None:
        budget["max_turns"] = max_turns
    if budget:
        metadata["budget"] = budget

    # Verify payload is parsed AND validated here, before anything is written:
    # a bead that looks gated but carries an unusable payload is worse than an
    # ungated one, because the gate would silently no-op at completion time.
    if verify_json is not None and verify_check is not None:
        click.echo(
            "--verify and --verify-check are mutually exclusive — pick one. "
            "Task NOT created.",
            err=True,
        )
        sys.exit(1)
    if verify_check is not None:
        # The friction --verify-check exists to remove: a bare shell command,
        # no JSON, no shell-quoting a brace. Anything the deterministic class
        # doesn't cover (human approval, an explicit cwd/timeout_s) still
        # needs --verify directly.
        verify_json = json.dumps({"class": "deterministic", "check": verify_check})
    if verify_json is not None:
        from .beads import VerifyContractError, validate_verify

        try:
            metadata["verify"] = validate_verify(json.loads(verify_json))
        except json.JSONDecodeError as e:
            click.echo(
                f"--verify is not valid JSON ({e}). Expected e.g. "
                f"'{{\"class\": \"deterministic\", \"check\": \"uv run pytest -q\"}}'. "
                f"Task NOT created.",
                err=True,
            )
            sys.exit(1)
        except VerifyContractError as e:
            click.echo(f"Invalid --verify payload: {e}. Task NOT created.", err=True)
            sys.exit(1)

    # Same posture again: assembled and validated before the first write. The
    # cap on --mistake is enforced in validate_sop rather than by click, so the
    # CLI and a planner writing metadata directly refuse the same shapes with
    # the same message.
    sop = {}
    for key, value in (
        ("purpose", sop_purpose),
        ("trigger", sop_trigger),
        ("inputs", sop_inputs),
        ("definition_of_done", definition_of_done),
    ):
        if value is not None:
            sop[key] = value
    if mistakes:
        sop["common_mistakes"] = list(mistakes)
    if sop:
        from .beads import SopContractError, validate_sop

        try:
            metadata["sop"] = validate_sop(sop)
        except SopContractError as e:
            click.echo(f"❌ {e}", err=True)
            click.echo("   Task NOT created.", err=True)
            sys.exit(1)

    # Same posture, same reason: validated BEFORE anything is written. A
    # malformed lane requirement that got quietly dropped would produce a bead
    # that looks restricted and is claimable anywhere — the exact hole the
    # manifest exists to close, wearing a green checkmark.
    from .beads import normalize_capabilities

    try:
        requirements = normalize_capabilities(list(requires), field_name="requires")
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        click.echo("   Task NOT created.", err=True)
        sys.exit(1)

    # Carry the human assignment on the FIRST append (create's assigned_to kwarg)
    # rather than create()-then-update(): a two-write sequence leaves a
    # cross-process window where the task exists as a plain PENDING agent task
    # and a concurrent cycle could grab it before the assignment lands.
    o, p = estimate_range if estimate_range else (None, None)
    from .beads import DepthLimitError, TaskReferenceError

    try:
        task = beads.create(
            title=title,
            description=description or title,
            priority=TaskPriority(priority),
            assigned_agent=agent,
            assigned_to=assign,
            source="manual",
            metadata=metadata or None,
            parent_id=parent_id,
            blocked_by=list(blocked_by) or None,
            due_at=due,
            starts_at=starts_at,
            estimate_hours=estimate,
            estimate_optimistic=o,
            estimate_pessimistic=p,
            requires=requirements or None,
        )
    except DepthLimitError as e:
        click.echo(f"❌ {e}", err=True)
        click.echo(
            "   A fix or follow-up belongs as a SIBLING of the deep bead, not a "
            "child of it.",
            err=True,
        )
        sys.exit(1)
    except TaskReferenceError as e:
        click.echo(f"❌ {e}", err=True)
        click.echo(
            "   Task NOT created. Pass each id as its OWN --blocked-by flag "
            "(--blocked-by ac-aaaaaaaa --blocked-by ac-bbbbbbbb); quoting two "
            "ids into one argument is how this goes wrong.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Created: {task.id}")
    if parent_id:
        click.echo(f"   parent: {parent_id}")
    if task.blocked_by:
        # Echo what was STORED, not what was typed: blockers are deduplicated on
        # the way in, and a confirmation that repeats the input would hide that.
        click.echo(f"   blocked by: {', '.join(task.blocked_by)}")
    if verify_json is not None:
        spec = metadata["verify"]
        click.echo(f"   verify: {gate_kind(spec)} — {verify_check_text(spec)}")
    if task.requires:
        # Echo what was STORED (deduplicated), same reason as blocked_by above.
        click.echo(f"   requires: {', '.join(task.requires)}")
    if sop:
        stored = metadata["sop"]
        filled = [k for k in SOP_TEXT_KEYS if k in stored]
        click.echo(
            f"   sop: {len(filled) + (1 if stored.get('common_mistakes') else 0)}"
            f"/5 field(s), {len(stored.get('common_mistakes') or [])} mistake(s)"
        )

    if assign is not None:
        from .humans import push_assignment

        push_assignment(config, task)


@tasks.command("update")
@click.argument("task_id")
@click.option("--due", default=None, help="Deadline, ISO-8601. '' clears it.")
@click.option(
    "--starts-at", default=None, help="Fixed start time, ISO-8601. '' clears it."
)
@click.option("--estimate", type=float, default=None, help="Most-likely hours (PERT m)")
@click.option(
    "--estimate-range",
    nargs=2,
    type=float,
    default=None,
    help="Optimistic and pessimistic hours (PERT o p)",
)
@click.option(
    "--blocked-by",
    multiple=True,
    help="Replace the blocker set with these id(s) — one id per flag. Refused "
    "if any id is malformed or unknown, or if it would close a dependency cycle.",
)
@click.option(
    "--clear-blocked-by",
    is_flag=True,
    help="Remove every blocker (the fix doctor suggests for a cycle on disk)",
)
@click.pass_context
def tasks_update(
    ctx,
    task_id: str,
    due: str | None,
    starts_at: str | None,
    estimate: float | None,
    estimate_range: tuple[float, float] | None,
    blocked_by: tuple[str, ...],
    clear_blocked_by: bool,
):
    """Update a task's temporal fields or blockers.

    Only touches what you pass — everything else is left alone. Cycle safety
    lives in Beads.update(), so no path through here can close a loop.
    """
    from .beads import DependencyCycleError, TaskReferenceError

    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    kwargs: dict = {}
    for key, stamp in (("due_at", due), ("starts_at", starts_at)):
        if stamp is not None:
            if stamp == "":
                kwargs[key] = None
            else:
                from datetime import datetime

                try:
                    datetime.fromisoformat(stamp)
                except ValueError:
                    click.echo(
                        f"{stamp!r} is not ISO-8601 — refusing: a silently "
                        f"unparseable deadline would rank as 'no deadline'.",
                        err=True,
                    )
                    sys.exit(1)
                kwargs[key] = stamp
    if estimate is not None:
        kwargs["estimate_hours"] = estimate
    if estimate_range is not None:
        kwargs["estimate_optimistic"] = estimate_range[0]
        kwargs["estimate_pessimistic"] = estimate_range[1]
    if clear_blocked_by:
        kwargs["blocked_by"] = []
    elif blocked_by:
        kwargs["blocked_by"] = list(blocked_by)

    if not kwargs:
        click.echo("Nothing to update — pass at least one option.", err=True)
        sys.exit(1)

    try:
        task = beads.update(task_id, **kwargs)
    except DependencyCycleError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)
    except TaskReferenceError as e:
        click.echo(f"❌ {e}", err=True)
        click.echo(
            f"   Nothing was written. If {task_id} already carries a bad "
            f"blocker from before this guard, clear it with: "
            f"agentco tasks update {task_id} --clear-blocked-by",
            err=True,
        )
        sys.exit(1)
    if task is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(f"Updated: {task.id}")


@tasks.command("show")
@click.argument("task_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Strict JSON only — omit the verify-gate summary (for `| jq` consumers)",
)
@click.pass_context
def tasks_show(ctx, task_id: str, as_json: bool):
    """Show task details, and — when the bead is gated — what `done` requires."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    task = beads.get(task_id)
    if not task:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)

    # JSON first and unchanged: this output is what a store-backed agent reads
    # to get its own context, and what operators pipe into jq.
    click.echo(json.dumps(json.loads(task.to_json()), indent=2))
    if as_json:
        return

    # SOP first: it is the orientation block. Someone reading a bead they did
    # not write needs why/what-fires-it/what-done-means before they need which
    # files to open or which command gates completion.
    sop = (task.metadata or {}).get("sop") or {}
    if sop:
        labels = {
            "purpose": "purpose",
            "trigger": "trigger",
            "inputs": "inputs",
            "definition_of_done": "done when",
        }
        click.echo("")
        click.echo("── SOP (delegation-ready block) ─────────────────────────")
        for key in SOP_TEXT_KEYS:
            if key in sop:
                click.echo(f"{labels[key]:>10}: {sop[key]}")
        mistakes = sop.get("common_mistakes") or []
        if mistakes:
            click.echo("  mistakes:")
            for mistake in mistakes:
                click.echo(f"    - {mistake}")
        else:
            # Named, not silently omitted. This is the one field with no other
            # home in LifeOS and the reason the block exists — its absence has
            # to be visible to the person about to pick the bead up.
            click.echo("  mistakes: (none recorded — add with --mistake)")

    refs = (task.metadata or {}).get("context_refs") or []
    if refs:
        from .beads import resolve_context_ref

        base_dir = Path(config.tasks_path).parent
        click.echo("")
        click.echo("── Context refs (pinned at plan time) ───────────────────")
        for ref in refs:
            path = str(ref.get("path", ""))
            resolved = resolve_context_ref(path, base_dir)
            mark = "" if resolved.exists() else "  ⚠️ not on disk yet"
            click.echo(f"- {path}{mark}")
            click.echo(f"    why: {ref.get('why', '')}")

    spec = (task.metadata or {}).get("verify")
    if not spec:
        return

    record = (task.metadata or {}).get("verify_result") or {}
    click.echo("")
    click.echo("── Verify gate ──────────────────────────────────────────")
    click.echo(f"class:  {gate_kind(spec)}")
    if spec.get("checks"):
        # Staged: the ladder is shown with per-stage verdicts, because "which
        # stage" is the whole reason to stage a gate. A stage after the failure
        # was never run and says so — it is not a pass and must not read as one.
        stages = list(spec["checks"])
        stages_run = record.get("stages_run")
        failed_index = (record.get("failed_stage") or {}).get("index")
        click.echo(f"checks: {len(stages)} stage(s), stop at first failure")
        for i, stage in enumerate(stages):
            if not record:
                mark = "·"
            elif failed_index is not None and i > failed_index:
                mark = "⏭ not run"
            elif failed_index is not None and i == failed_index:
                mark = "❌"
            elif stages_run is not None and i < stages_run:
                mark = "✅"
            else:
                mark = "·"
            click.echo(f"  {i + 1}. {stage}  {mark}")
    else:
        click.echo(f"check:  {spec.get('check')}")
    if spec.get("cwd"):
        click.echo(f"cwd:    {spec['cwd']}")
    if spec.get("timeout_s"):
        click.echo(f"timeout: {spec['timeout_s']}s")

    if task.status == TaskStatus.AWAITING_VERIFY:
        click.echo("status: 🔍 AWAITING APPROVAL — this bead is NOT done, and")
        click.echo("        nothing blocked by it is released.")
        click.echo(f"        agentco tasks approve-verify {task.id}")
        click.echo(f"        agentco tasks reject-verify {task.id} -m '<why>'")
    elif task.status == TaskStatus.VERIFY_FAILED:
        click.echo("status: 🚨 VERIFY FAILED — NOT done. Fix, then re-complete")
        click.echo(f"        (the check re-runs): agentco tasks complete {task.id}")
    elif task.status == TaskStatus.DONE:
        click.echo("status: ✅ gate passed")
    else:
        click.echo(
            f"status: {task.status.value} — completing this bead will run the "
            f"gate above; it cannot self-grade."
        )

    if record:
        click.echo(f"last run: {record.get('checked_at', '?')}")
        if record.get("failed_stage"):
            stage = record["failed_stage"]
            click.echo(
                f"failed at stage {stage.get('index', 0) + 1}"
                f"/{record.get('stages_total', '?')}: {stage.get('command')}"
            )
        if record.get("exit_code") is not None:
            click.echo(f"exit code: {record['exit_code']}")
        if record.get("timed_out"):
            click.echo("timed out: yes")
        tail = str(record.get("output_tail", ""))
        if tail:
            click.echo("output:")
            for line in tail.splitlines()[-20:]:
                click.echo(f"  {line}")
    if (task.metadata or {}).get("verify_approval"):
        approval = task.metadata["verify_approval"]
        click.echo(
            f"approved by {approval.get('approver')} at {approval.get('approved_at')}"
        )
    if (task.metadata or {}).get("verify_rejection"):
        rejection = task.metadata["verify_rejection"]
        click.echo(
            f"rejected by {rejection.get('approver')} at "
            f"{rejection.get('rejected_at')}: {rejection.get('reason')}"
        )


@tasks.command("retry")
@click.argument("task_id", required=False)
@click.option("--all-failed", is_flag=True, help="Retry every failed task")
@click.pass_context
def tasks_retry(ctx, task_id: str | None, all_failed: bool):
    """Reset a failed task to pending so the next cycle retries it."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    if not task_id and not all_failed:
        click.echo("Provide a TASK_ID or --all-failed.", err=True)
        sys.exit(1)

    targets = (
        beads.list(status=TaskStatus.FAILED)
        if all_failed
        else [t for t in [beads.get(task_id)] if t]
    )
    if not targets:
        click.echo("No matching failed task(s).", err=True)
        sys.exit(1)

    for t in targets:
        if t.status != TaskStatus.FAILED:
            click.echo(f"Skipping {t.id}: status is {t.status.value}, not failed")
            continue
        beads.update(t.id, status=TaskStatus.PENDING, result=None)
        click.echo(f"Retrying: {t.id} — {t.title}")


@tasks.command("complete")
@click.argument("task_id")
@click.option("--result", "-r", help="Result message")
@click.option(
    "--actual",
    type=float,
    default=None,
    help="Hours the work actually took. Feeds estimate calibration: after 3+ "
    "completions with both estimate and actual, every future schedule is "
    "corrected by your real median ratio.",
)
@click.pass_context
def tasks_complete(ctx, task_id: str, result: str | None, actual: float | None):
    """Mark a task as done."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    # Validation at the write boundary: if a result is supplied it MUST be a
    # valid TaskResult JSON (parseable, with a recognized `status`). A garbage
    # result would parse to None on the read side and silently bypass the feed
    # watermark guard — so we reject it here, before the task is marked DONE.
    # Completing WITHOUT --result stays allowed for ordinary tasks (backward
    # compat); the read-side None guard covers feed beads.
    if result is not None:
        from .beads import TaskResult

        try:
            TaskResult.from_str(result)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError) as e:
            click.echo(
                f"Invalid --result: not a valid TaskResult JSON ({e}). "
                f"Expected e.g. '{{\"status\": \"complete\", \"output\": \"...\"}}'. "
                f"Task {task_id} NOT completed.",
                err=True,
            )
            sys.exit(1)

    if actual is not None and actual <= 0:
        click.echo(
            f"--actual must be positive hours, got {actual}. Task NOT completed.",
            err=True,
        )
        sys.exit(1)

    from .beads import VerifyGateError

    try:
        task = beads.complete(task_id, result=result)
    except VerifyGateError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)
    if task is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)

    if actual is not None:
        # Recorded after complete() — a failed actual write must never
        # un-complete the task; the completion is the primary act.
        beads.update(task_id, actual_hours=actual)

    # A gated bead does NOT reach DONE by being completed. Say exactly where it
    # landed: "Completed" on a bead the gate just rejected is the lie this
    # whole mechanism exists to stop.
    record = (task.metadata or {}).get("verify_result") or {}
    if task.status == TaskStatus.AWAITING_VERIFY:
        check = ((task.metadata or {}).get("verify") or {}).get("check", "")
        click.echo(f"🔍 Awaiting verification: {task_id}")
        click.echo(f"   Approver must confirm: {check}")
        click.echo(f"   Then: agentco tasks approve-verify {task_id}")
    elif task.status == TaskStatus.VERIFY_FAILED:
        click.echo(f"🚨 VERIFY FAILED: {task_id} — NOT done", err=True)
        click.echo(f"   check: {record.get('check', '')}", err=True)
        tail = str(record.get("output_tail", ""))[-800:]
        if tail:
            click.echo(f"   output: {tail}", err=True)
        sys.exit(1)
    elif record:
        click.echo(f"Completed: {task_id} (verify passed: {record.get('check', '')})")
    else:
        click.echo(f"Completed: {task_id}")


@tasks.command("approve-verify")
@click.argument("task_id")
@click.option(
    "--approver",
    default=None,
    help="Who approved (defaults to $USER) — recorded in metadata.verify_approval",
)
@click.pass_context
def tasks_approve_verify(ctx, task_id: str, approver: str | None):
    """Approve a human-class verify gate: awaiting_verify → done.

    The only path that skips the gate, because here the person IS the gate.
    """
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    who = approver or os.environ.get("USER") or "unknown"
    try:
        task = beads.approve_verify(task_id, approver=who)
    except ValueError as e:
        click.echo(f"Cannot approve: {e}", err=True)
        sys.exit(1)
    if task is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(f"✅ Verified and completed: {task_id} (approved by {who})")


@tasks.command("reject-verify")
@click.argument("task_id")
@click.option("-m", "--message", "reason", default=None, help="Why it was rejected")
@click.option("--approver", default=None, help="Who rejected (defaults to $USER)")
@click.pass_context
def tasks_reject_verify(ctx, task_id: str, reason: str | None, approver: str | None):
    """Reject a human-class verify gate: awaiting_verify → verify_failed."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    who = approver or os.environ.get("USER") or "unknown"
    try:
        task = beads.reject_verify(task_id, approver=who, reason=reason)
    except ValueError as e:
        click.echo(f"Cannot reject: {e}", err=True)
        sys.exit(1)
    if task is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(f"🚨 Verify rejected: {task_id} — status is now verify_failed")


@tasks.command("decline")
@click.argument("task_id")
@click.option("--reason", default=None, help="Why the assignee declined (recorded in metadata)")
@click.pass_context
def tasks_decline(ctx, task_id: str, reason: str | None):
    """Decline a human-assigned task — returns it to the queue, unassigned.

    Clears assigned_to via the explicit human-approved path (the only sanctioned
    way past the human-lineage invariant), records the reason, and resets the
    task to pending. Decline exists so age-pressure never structurally
    incentivizes a false 'done'.
    """
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    from .humans import decline_task, TaskStateError

    try:
        task = decline_task(beads, task_id, reason)
    except TaskStateError as e:
        click.echo(f"Cannot decline: {e}", err=True)
        sys.exit(1)
    if task is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(f"Declined: {task_id} — returned to queue")


@tasks.command("snooze")
@click.argument("task_id")
@click.option("--for", "interval", required=True, help="How long to hide it (e.g. 2d, 4h, 30m)")
@click.pass_context
def tasks_snooze(ctx, task_id: str, interval: str):
    """Snooze a task — hide it from `agentco me` until the interval elapses."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)

    from .humans import snooze_task, TaskStateError

    try:
        task = snooze_task(beads, task_id, interval)
    except ValueError as e:
        click.echo(f"Invalid --for interval: {e}", err=True)
        sys.exit(1)
    except TaskStateError as e:
        click.echo(f"Cannot snooze: {e}", err=True)
        sys.exit(1)
    if task is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(f"Snoozed: {task_id} for {interval}")


# Link management commands
# Document management commands
@main.group()
def docs():
    """Document management commands."""
    pass


@docs.command("list")
@click.option("--area", "-a", help="Filter by area (strategy, product, engineering, ...)")
@click.option("--agent", help="Filter by creating agent")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def docs_list(area: str | None, agent: str | None, as_json: bool):
    """List company documents."""
    from .registry import Registry

    registry_path = Path.cwd() / ".agentco" / "registry.json"
    if not registry_path.exists():
        click.echo("No registry found. Run 'agentco init --company' first.", err=True)
        sys.exit(1)

    registry = Registry(registry_path)

    if area:
        entries = registry.list_by_area(area)
    elif agent:
        entries = registry.list_by_agent(agent)
    else:
        entries = registry.list_all()

    if as_json:
        click.echo(json.dumps([e.to_dict() for e in entries], indent=2))
    else:
        if not entries:
            click.echo("No documents found.")
        else:
            for e in entries:
                status_mark = {"draft": "📝", "active": "✅", "archived": "📦"}.get(
                    e.status, "❓"
                )
                click.echo(f"  {status_mark} {e.path} — {e.title} [{e.created_by}]")


@docs.command("show")
@click.argument("path")
def docs_show(path: str):
    """Show a document's content."""
    file_path = Path.cwd() / path
    if not file_path.is_file():
        # Try with company/ prefix
        file_path = Path.cwd() / "company" / path
    if not file_path.is_file():
        # Try adding .md
        file_path = Path.cwd() / "company" / f"{path}.md"
    if not file_path.is_file():
        click.echo(f"Document not found: {path}", err=True)
        sys.exit(1)

    click.echo(file_path.read_text())


@docs.command("search")
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def docs_search(query: str, as_json: bool):
    """Search documents by title, path, or tags."""
    from .registry import Registry

    registry_path = Path.cwd() / ".agentco" / "registry.json"
    if not registry_path.exists():
        click.echo("No registry found. Run 'agentco init --company' first.", err=True)
        sys.exit(1)

    registry = Registry(registry_path)
    results = registry.search(query)

    if as_json:
        click.echo(json.dumps([e.to_dict() for e in results], indent=2))
    else:
        if not results:
            click.echo(f"No documents matching '{query}'.")
        else:
            click.echo(f"Found {len(results)} result(s):")
            for e in results:
                click.echo(f"  {e.path} — {e.title}")


@main.group(invoke_without_command=True)
@click.pass_context
def approve(ctx):
    """Review and approve agent-proposed tasks before they run."""
    if ctx.invoked_subcommand is None:
        # Default: show the pending_approval list
        config = Config.load(ctx.obj["config_path"])
        beads = Beads(config.tasks_path)
        waiting = beads.pending_approval()
        if not waiting:
            click.echo("No tasks awaiting approval. ✅")
            return
        click.echo(f"🔔 {len(waiting)} task(s) awaiting your approval:\n")
        for t in waiting:
            parent_info = f" (child of {t.parent_id})" if t.parent_id else ""
            click.echo(f"  [{t.id}] {t.title}{parent_info}")
            click.echo(f"         agent: {t.assigned_agent or '?'} | {t.description[:80]}")
        click.echo(
            "\nRun 'agentco approve task <id>' to approve, "
            "'agentco approve all' to approve all, "
            "or 'agentco approve reject <id>' to skip."
        )


@approve.command("task")
@click.argument("task_id")
@click.pass_context
def approve_task(ctx, task_id: str):
    """Approve a single pending_approval task."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)
    try:
        task = beads.approve(task_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if not task:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(f"Approved: {task.id} — {task.title}")


@approve.command("all")
@click.pass_context
def approve_all(ctx):
    """Approve ALL pending_approval tasks."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)
    waiting = beads.pending_approval()
    if not waiting:
        click.echo("No tasks awaiting approval.")
        return
    for t in waiting:
        beads.approve(t.id)
        click.echo(f"Approved: {t.id} — {t.title}")
    click.echo(f"\nApproved {len(waiting)} task(s).")


@approve.command("reject")
@click.argument("task_id")
@click.pass_context
def approve_reject(ctx, task_id: str):
    """Reject (skip) a pending_approval task — marks it skipped, never runs."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)
    task = beads.get(task_id)
    if not task:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    if task.status != TaskStatus.PENDING_APPROVAL:
        click.echo(f"Task {task_id} is not pending_approval (status={task.status.value})", err=True)
        sys.exit(1)
    beads.update(task_id, status=TaskStatus.SKIPPED, result="Rejected by principal")
    click.echo(f"Rejected: {task_id} — {task.title}")


@approve.command("reject-all")
@click.pass_context
def approve_reject_all(ctx):
    """Reject ALL pending_approval tasks — marks them skipped."""
    config = Config.load(ctx.obj["config_path"])
    beads = Beads(config.tasks_path)
    waiting = beads.pending_approval()
    if not waiting:
        click.echo("No tasks awaiting approval.")
        return
    for t in waiting:
        beads.update(t.id, status=TaskStatus.SKIPPED, result="Rejected by principal")
        click.echo(f"Rejected: {t.id} — {t.title}")
    click.echo(f"\nRejected {len(waiting)} task(s).")


if __name__ == "__main__":
    main()
