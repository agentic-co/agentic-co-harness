"""RCA (root cause analysis) loop — automated analyze -> verify_plan -> execute
-> validate chain, driven entirely by beads.

When a task fails, ``create_rca_task`` spawns the RCA root bead (the analyze
phase). Each phase bead is worked by an agent (assigned_agent="claude"); on
completion the agent — or any caller — invokes ``advance_rca`` with the
bead and an outcome dict, which reads the bead's ``metadata.phase`` +
``metadata.rca_cycle`` and creates the correct next-phase bead:

    analyze -> verify_plan -> execute (pending_approval) -> validate
        ^                                                       |
        +------------------ not sound / not fixed ---------------+
                      (rca_cycle += 1, capped at MAX_RCA_CYCLES)

All phase beads share one ``metadata.rca_root`` (the ORIGINAL analyze bead's
id) and parent off it directly (``parent_id=root_id``) rather than chaining
depth-first — this keeps every RCA tree at exactly depth 1 under its root
regardless of how many cycles it takes, well inside beads.py's
MAX_SUBTASK_DEPTH guardrail. The root bead itself is created with
parent_id=None (a fresh top-level bead) rather than parented under the
failed task, so an already-nested failed task can never push an RCA chain
past the depth cap — the link back to the failed task lives in
``metadata.rca_for``, not in the parent_id chain.

Once ``rca_cycle`` would exceed MAX_RCA_CYCLES, the loop stops and
``escalate_rca`` hands the whole cycle history to a human instead of
spawning another analyze bead.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

from .beads import Beads, Task, TaskPriority, TaskStatus

MAX_RCA_CYCLES = 3

#: Who an exhausted RCA loop escalates to. Set from `humans.escalate_to` in
#: config.yaml (the Orchestrator does this at startup); `$AGENTCO_ESCALATE_TO`
#: overrides for one-off CLI runs. The `human:` prefix is what keeps the bead
#: out of the agent queue (see Beads.ready()).
DEFAULT_ESCALATION_ASSIGNEE = "human:operator"


def escalation_assignee() -> str:
    return os.environ.get("AGENTCO_ESCALATE_TO") or DEFAULT_ESCALATION_ASSIGNEE

# RCA beads carried no budget, so they inherited executor.DEFAULT_TIMEOUT (600s)
# while the work they analyze routinely runs far longer — feeds ingest beads get
# 1800s from config. An analysis bead is strictly MORE work than the bead it
# analyzes: it must read the error, reproduce it, inspect the affected code and
# draft a fix plan. Giving it a third of the budget guaranteed the timeouts we
# saw ("[RCA] Verify child instance: feeds" and several ingest RCAs died at
# exactly 600s), which then counted as fresh failures. Budget the analysis for
# the analysis, not for a default nobody chose.
RCA_BUDGET = {"timeout": 1800, "max_turns": 120}

# The same budget, spelled as CLI flags, for the fix bead the analysis files.
# The 1800s fix stopped one hop short: RCA PHASE beads got a real budget while
# the fix beads they file — the ones that actually edit code and run the suite —
# were still created through `agentco tasks create`, which had no budget flag at
# all, so they were born metadata={} and died at DEFAULT_TIMEOUT. Both fix beads
# from the @aidotengineer RCA (ac-83b2f89b, ac-7ea4b8a1) failed that way at
# exactly 600s. Budget the fix like the analysis; it is not less work.
_FIX_BUDGET_FLAGS = f"--timeout {RCA_BUDGET['timeout']} --max-turns {RCA_BUDGET['max_turns']}"

_PHASES = ("analyze", "verify_plan", "execute", "validate")
_PHASE_INDEX = {p: i for i, p in enumerate(_PHASES)}

_PHASE_LABEL = {
    "analyze": "[RCA]",
    "verify_plan": "[RCA verify-plan]",
    "execute": "[RCA execute]",
    "validate": "[RCA validate]",
}

_RCA_DESCRIPTION_TEMPLATE = """\
**Error**
{error}

**How to reproduce**
{reproduce}

**What's affected**
{affected}

**Root cause**
(fill in during analysis — run the RootCauseAnalysis or Rca skill to reason \
through this systematically: search the codebase, review recent history, \
identify the affected components, and state the root cause plainly.)

**Fix plan**
{fix_plan}

**Terminal action — REQUIRED (this bead cannot close on analysis alone)**
The deliverable is a landed fix or a filed fix bead, never the analysis by \
itself. This bead runs headless: nobody is there to answer "say the word and I \
will apply it", so an RCA that ends on that question is a no-op that bills full \
price and lets the failure recur on schedule (2026-08-09 and 2026-08-10, same \
failure, two full-price analyses, zero files changed). Before completing, do ONE \
of:

  1. Apply the minimal fix yourself, then record it and close the record:
       agentco tasks create "<what you changed>" -d "<the change + the evidence it works>" --parent {bead_id} -p 1
       agentco tasks complete <new bead id> --result '{{"status": "complete", "output": "<evidence>"}}'
  2. File the fix bead for a later run to apply, and put its id in your result:
       agentco tasks create "<the fix>" -d "<root cause + the concrete minimal change>" --parent {bead_id} -p 1 {fix_budget}

     Keep the budget flags. A fix bead filed without them runs on the 600s
     default and dies mid-edit, which is how this very failure was produced.

Never end by asking an absent human for permission — filing the bead IS the ask, \
and it keeps the failure visibly open in `agentco me` instead of showing as done. \
A deterministic gate re-checks this at completion: with no linked follow-up bead, \
this bead lands in `verify_failed`, never `done`.
"""

# Placeholder used while formatting the description BEFORE the bead exists —
# `_create_analyze_bead` rewrites the description with the real id immediately
# after create, in the same update that stamps the verify gate.
_BEAD_ID_PLACEHOLDER = "<this bead's id>"


def _describe(
    *, error: str, reproduce: str, affected: str, fix_plan: str, bead_id: str
) -> str:
    return _RCA_DESCRIPTION_TEMPLATE.format(
        error=error,
        reproduce=reproduce,
        affected=affected,
        fix_plan=fix_plan,
        bead_id=bead_id,
        fix_budget=_FIX_BUDGET_FLAGS,
    )


def terminal_action_verify(store_path: Path | str, bead_id: str) -> dict:
    """The PPEV verify payload that makes the terminal action non-optional.

    The prompt block above is advice; this is enforcement. `agentco rca-check`
    exits non-zero while the bead has no follow-up bead, so completing on
    analysis alone routes through `_apply_verify_gate` to VERIFY_FAILED instead
    of DONE — visible in `agentco me`, and NOT re-queued (verify_failed is not
    a ready state), so a stuck RCA costs nothing further.

    ``--store`` is passed explicitly rather than relying on the check's cwd
    config: the gate must resolve the same store the bead lives in even when
    the completing process runs from somewhere else.
    """
    return {
        "class": "deterministic",
        "check": f"agentco rca-check {bead_id} --store {shlex.quote(str(store_path))}",
        "timeout_s": 60,
    }


def has_terminal_action(beads: Beads, bead: Task) -> str | None:
    """The id of the bead that IS this RCA bead's terminal action, or None.

    Anything that carries the investigation forward counts, because the defect
    being prevented is an RCA that leaves NOTHING behind:

      * an explicit fix bead — ``metadata.rca_fix_for``, or simply parented
        under this bead / its RCA root (what `agentco tasks create --parent`
        produces, since the CLI has no --metadata flag);
      * a later phase of the same cycle (verify_plan/execute/validate), or any
        later cycle — i.e. ``advance_rca`` was called;
      * the human escalation bead.

    Only store-visible state is consulted, never the result text: the gate runs
    BEFORE the completing write lands, so a bead id named only in ``--result``
    is not yet readable. File the bead, then name it in the result.
    """
    root_id = bead.metadata.get("rca_root") or bead.id
    cycle = int(bead.metadata.get("rca_cycle", 1) or 1)
    phase_index = _PHASE_INDEX.get(bead.metadata.get("phase"), -1)
    for task in beads.list():
        if task.id == bead.id:
            continue
        metadata = task.metadata or {}
        if metadata.get("rca_fix_for") in (bead.id, root_id):
            return task.id
        if task.parent_id in (bead.id, root_id) and task.source != "rca":
            return task.id
        if metadata.get("rca_root") != root_id:
            continue
        if metadata.get("escalated_after_cycles"):
            return task.id
        task_cycle = int(metadata.get("rca_cycle", 0) or 0)
        task_phase = _PHASE_INDEX.get(metadata.get("phase"), -1)
        if task_cycle > cycle or (task_cycle == cycle and task_phase > phase_index):
            return task.id
    return None


def _title_for(phase: str, failed_title: str) -> str:
    return f"{_PHASE_LABEL[phase]} {failed_title}"


def _create_analyze_bead(
    beads: Beads,
    *,
    failed_title: str,
    error: str,
    reproduce: str,
    affected: str,
    fix_plan_seed: str,
    rca_for: str,
    root_id: str | None,
    cycle: int,
    parent_id: str | None,
    recurred_after: str | None = None,
) -> Task:
    """Shared constructor for an analyze-phase bead — used both for the RCA
    root (root_id=None, cycle=1) and for a re-spawned analyze bead on a
    subsequent cycle (root_id=the original root, parent_id=root_id)."""
    description = _describe(
        error=error,
        reproduce=reproduce,
        affected=affected,
        fix_plan=fix_plan_seed,
        bead_id=_BEAD_ID_PLACEHOLDER,
    )
    metadata = {
        "rca_for": rca_for,
        "rca_root": root_id,
        "rca_cycle": cycle,
        "phase": "analyze",
        "rca_failed_title": failed_title,
        "rca_error": error,
        "budget": RCA_BUDGET,
    }
    if recurred_after:
        # The symptom came back after an investigation closed. Say so on the
        # bead, so the analyst starts from "the last fix did not hold / a second
        # vector exists" instead of re-deriving the closed root's finding.
        metadata["rca_recurrence_of"] = recurred_after
    task = beads.create(
        title=_title_for("analyze", failed_title),
        description=description,
        priority=TaskPriority.HIGH,
        assigned_agent="claude",
        source="rca",
        source_id=f"rca-for:{rca_for}:cycle{cycle}",
        parent_id=parent_id,
        metadata=metadata,
        # An RCA bead is GENERATED work, not a mirror of an external record, so
        # it is keyed (kind, subject, period) rather than by source_id — and the
        # period must carry the RECURRENCE, not just the cycle. `source_id`
        # alone says "the RCA for bead X, cycle 1", which is the same string for
        # the investigation opened on 07-29 and the one the SAME bead earns on
        # 08-04 after that root closed and the symptom came back through a
        # different vector (sommeliwhey MAGRINHA). Folding those two together is
        # exactly the failure `find_closed_rca_root` exists to prevent, so the
        # id of the closed root it recurred after IS the epoch boundary.
        natural_key_kind="rca",
        natural_key_subject=rca_for or failed_title,
        natural_key_period=(
            f"cycle{cycle}@after:{recurred_after}" if recurred_after else f"cycle{cycle}"
        ),
    )
    # One post-create write, because both things it stamps need the bead's own
    # id: the self-referential rca_root (root beads only) and the verify gate
    # that forbids closing this analysis without a terminal action. The
    # description is rewritten in the same write so the id in its instructions
    # is the real one rather than the placeholder.
    patched = {**task.metadata, "verify": terminal_action_verify(beads.path, task.id)}
    if root_id is None:
        # This bead IS the root — point rca_root at itself so every later
        # phase bead in the chain can read metadata.rca_root uniformly,
        # including this very bead.
        patched["rca_root"] = task.id
    return beads.update(
        task.id,
        description=_describe(
            error=error,
            reproduce=reproduce,
            affected=affected,
            fix_plan=fix_plan_seed,
            bead_id=task.id,
        ),
        metadata=patched,
    )


_OPEN_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.PENDING_APPROVAL,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
    }
)


def _source_id_for(failed_task_id: str, cycle: int) -> str:
    return f"rca-for:{failed_task_id}:cycle{cycle}"


def find_existing_rca_root(
    beads: Beads, failed_task: Task, error: str, cycle: int = 1
) -> Task | None:
    """The OPEN RCA root that already covers this failure, or None.

    Two keys, because the sommeliwhey box-scout incidents (2026-07-22, 07-29,
    08-04) produced duplicate RCA roots by two independent routes — 72 beads
    across 41 distinct titles for a single finding, 100% of that node's spend
    on 2026-08-04:

    1. ``source_id`` — this exact failed bead already has a root.
       ``agentco tasks retry --all-failed`` (the standard queue repair) resets
       failed beads to pending, so the same bead re-fails and spawned a SECOND
       root for it; 31 of those 41 titles carry two beads by exactly this path.
       Keying on the bead ID survives the retry.
    2. ``metadata.rca_error`` — one config defect failed 30 beads in a single
       cycle with byte-identical error strings, and each spawned its own root.
       Folding the rest into the first root collapses 30 roots into 1.

    Key 1 is the stronger key, so a source_id hit wins over an error hit.

    BOTH keys only match OPEN roots. A closed root is a finished investigation,
    not a bucket for next month's incident — and closed roots do not stay
    correct: on 2026-08-04 the MAGRINHA bead re-failed with the same string as
    on 07-29, but through a different vector (``init`` clobbering config vs. the
    missing ``_external_agent`` guard). Under an any-state key 1 that recurrence
    would have folded into the DONE 07-29 root and the real cause would never
    have been investigated. Recurrence after a close is a finding in itself;
    ``create_rca_task`` records it as ``metadata.rca_recurrence_of``.
    """
    source_id = _source_id_for(failed_task.id, cycle)
    by_source: Task | None = None
    by_error: Task | None = None
    for task in beads.list():
        if task.source != "rca" or task.metadata.get("phase") != "analyze":
            continue
        if task.status not in _OPEN_STATUSES:
            continue
        if by_source is None and task.source_id == source_id:
            by_source = task
        if by_error is None and task.metadata.get("rca_error") == error:
            by_error = task
    return by_source or by_error


def find_closed_rca_root(beads: Beads, failed_task: Task, cycle: int = 1) -> Task | None:
    """The most recent CLOSED RCA root for this exact bead, or None.

    Used only to mark a fresh root as a recurrence — the signal that a symptom
    came back after its investigation closed, which is what stayed invisible
    across the three box-scout incidents.
    """
    source_id = _source_id_for(failed_task.id, cycle)
    closed = [
        task
        for task in beads.list()
        if task.source == "rca"
        and task.metadata.get("phase") == "analyze"
        and task.source_id == source_id
        and task.status not in _OPEN_STATUSES
    ]
    return closed[-1] if closed else None


def create_rca_task(
    beads: Beads,
    failed_task: Task,
    error: str,
    cycle: int = 1,
    reproduce: str | None = None,
    affected: str | None = None,
    candidate_fix_plan: str | None = None,
) -> Task:
    """Create the RCA root bead (phase=analyze, cycle=1 by default) for a
    failed task, or return the existing root that already covers it.

    The root bead IS the analyze-phase bead — the analyze agent fills in the
    Root cause / Fix plan sections of its own description rather than a
    separate bead being created for step 1. ``reproduce`` / ``affected`` /
    ``candidate_fix_plan`` let a caller seed richer context than the bare
    error text (used for hand-seeded RCAs where the shape of the bug is
    already partly understood); all three default to a generic placeholder
    pointing the analyze agent at the RCA skills.

    Idempotent per ``find_existing_rca_root``: N beads failing the same way
    produce ONE investigation, not N. When a failure is folded into an existing
    root, its bead ID is appended to that root's ``metadata.rca_also_failed``
    so the analysis still sees its true blast radius.
    """
    existing = find_existing_rca_root(beads, failed_task, error, cycle=cycle)
    if existing is not None:
        if existing.source_id != _source_id_for(failed_task.id, cycle):
            also = list(existing.metadata.get("rca_also_failed") or [])
            if failed_task.id not in also:
                also.append(failed_task.id)
                existing = beads.update(
                    existing.id,
                    metadata={**existing.metadata, "rca_also_failed": also},
                )
        return existing
    prior = find_closed_rca_root(beads, failed_task, cycle=cycle)
    return _create_analyze_bead(
        beads,
        failed_title=failed_task.title,
        error=error,
        reproduce=reproduce
        or (
            f"This bead already had an RCA ({prior.id}) that closed; the same "
            "failure came back. Start by asking whether that fix regressed or "
            "whether this is a second vector to the same symptom — do not "
            "assume the closed root cause still applies."
            if prior is not None
            else "(not yet determined — reproduce the failure and document the exact "
            "steps here before proposing a fix)"
        ),
        affected=affected or "(not yet determined — what else touches this code path?)",
        fix_plan_seed=candidate_fix_plan
        or "(fill in during analysis — the concrete, minimal change that addresses the root cause)",
        rca_for=failed_task.id,
        root_id=None,
        cycle=cycle,
        parent_id=None,
        recurred_after=prior.id if prior is not None else None,
    )


def _loop_or_escalate(
    beads: Beads, *, root_id: str, rca_for: str, cycle: int, reason: str
) -> Task:
    """Either spawn the next analyze cycle or, past MAX_RCA_CYCLES, escalate
    to a human with the full cycle history."""
    root = beads.get(root_id)
    failed_title = (root.metadata.get("rca_failed_title") if root else None) or "RCA"
    error = (root.metadata.get("rca_error") if root else None) or reason
    next_cycle = cycle + 1
    if next_cycle > MAX_RCA_CYCLES:
        history = collect_rca_history(beads, root_id)
        return escalate_rca(beads, root or Task(id=root_id, title=failed_title, description=""), history)
    return _create_analyze_bead(
        beads,
        failed_title=failed_title,
        error=error,
        reproduce=f"Prior cycle {cycle} did not resolve it: {reason}",
        affected="(re-assess — did the prior cycle's fix change what's affected?)",
        fix_plan_seed="(fill in during analysis — the prior fix plan did not hold; propose a different one)",
        rca_for=rca_for,
        root_id=root_id,
        cycle=next_cycle,
        parent_id=root_id,
    )


def advance_rca(beads: Beads, bead: Task, outcome: dict) -> Task:
    """Advance one RCA phase bead to the next, per its metadata.phase and
    metadata.rca_cycle. Callable both by an agent finishing a phase and
    programmatically (e.g. from tests or other code paths).

    ``outcome`` shape depends on the CURRENT phase of ``bead``:
      analyze      -> {"root_cause": str, "fix_plan": str}
      verify_plan  -> {"sound": bool, "reason": str}
      execute      -> {"notes": str}                        (always advances)
      validate     -> {"fixed": bool, "notes": str}

    Returns the newly created next-phase bead, EXCEPT:
      - validate with fixed=True returns the updated (DONE) root bead.
      - a loop that exceeds MAX_RCA_CYCLES returns the escalation bead
        (see escalate_rca).
    """
    phase = bead.metadata.get("phase")
    if phase not in _PHASES:
        raise ValueError(f"bead {bead.id} has unknown/missing RCA phase {phase!r}")

    root_id = bead.metadata.get("rca_root") or bead.id
    rca_for = bead.metadata.get("rca_for")
    cycle = int(bead.metadata.get("rca_cycle", 1))
    failed_title = bead.metadata.get("rca_failed_title") or bead.title

    if phase == "analyze":
        root_cause = outcome.get("root_cause", "")
        fix_plan = outcome.get("fix_plan", "")
        # Record the analysis on the analyze bead itself so the chain (and
        # escalate_rca's history walk) can read it back later.
        beads.update(
            bead.id, metadata={**bead.metadata, "root_cause": root_cause, "fix_plan": fix_plan}
        )
        return beads.create(
            title=_title_for("verify_plan", failed_title),
            description=(
                f"**Root cause (cycle {cycle}):** {root_cause}\n\n"
                f"**Proposed fix plan:** {fix_plan}\n\n"
                f"Verify this plan is sound, safe, and minimal before it is approved "
                f"for execution. A human still approves the execute step regardless — "
                f"this check is what decides whether that step gets created at all."
            ),
            priority=TaskPriority.HIGH,
            assigned_agent="claude",
            source="rca",
            parent_id=root_id,
            metadata={
                "rca_for": rca_for,
                "rca_root": root_id,
                "rca_cycle": cycle,
                "phase": "verify_plan",
                "rca_failed_title": failed_title,
                "fix_plan": fix_plan,
                "root_cause": root_cause,
                "budget": RCA_BUDGET,
            },
        )

    if phase == "verify_plan":
        sound = bool(outcome.get("sound"))
        reason = outcome.get("reason", "")
        if sound:
            fix_plan = bead.metadata.get("fix_plan", "")
            return beads.create(
                title=_title_for("execute", failed_title),
                description=(
                    f"**Approved fix plan (cycle {cycle}):** {fix_plan}\n\n"
                    f"**Verify-plan notes:** {reason}\n\n"
                    f"Apply the fix, then create the validate bead "
                    f"(advance_rca with outcome={{'notes': ...}})."
                ),
                priority=TaskPriority.HIGH,
                assigned_agent="claude",
                source="rca",
                status=TaskStatus.PENDING_APPROVAL,
                parent_id=root_id,
                metadata={
                    "rca_for": rca_for,
                    "rca_root": root_id,
                    "rca_cycle": cycle,
                    "phase": "execute",
                    "rca_failed_title": failed_title,
                    "fix_plan": fix_plan,
                    "budget": RCA_BUDGET,
                },
            )
        return _loop_or_escalate(
            beads,
            root_id=root_id,
            rca_for=rca_for,
            cycle=cycle,
            reason=f"verify_plan (cycle {cycle}) rejected the fix plan: {reason}",
        )

    if phase == "execute":
        notes = outcome.get("notes", "")
        return beads.create(
            title=_title_for("validate", failed_title),
            description=(
                f"**Fix applied (cycle {cycle}):** {notes}\n\n"
                f"Re-run the original failed task (or a reproduction of it) to "
                f"confirm the fix actually worked."
            ),
            priority=TaskPriority.HIGH,
            assigned_agent="claude",
            source="rca",
            parent_id=root_id,
            metadata={
                "rca_for": rca_for,
                "rca_root": root_id,
                "rca_cycle": cycle,
                "phase": "validate",
                "rca_failed_title": failed_title,
                "budget": RCA_BUDGET,
            },
        )

    # phase == "validate"
    fixed = bool(outcome.get("fixed"))
    notes = outcome.get("notes", "")
    if fixed:
        return beads.update(
            root_id,
            status=TaskStatus.DONE,
            result=f"RCA resolved after {cycle} cycle(s). {notes}".strip(),
        )
    return _loop_or_escalate(
        beads,
        root_id=root_id,
        rca_for=rca_for,
        cycle=cycle,
        reason=f"validate (cycle {cycle}) found the fix did not work: {notes}",
    )


def collect_rca_history(beads: Beads, root_id: str) -> list[dict]:
    """All beads belonging to one RCA tree (metadata.rca_root == root_id),
    ordered by cycle then phase, reduced to the fields that matter for a
    human reading the history: cycle, phase, root_cause, fix_plan, notes."""
    chain = [t for t in beads.list() if t.metadata.get("rca_root") == root_id]
    chain.sort(
        key=lambda t: (
            int(t.metadata.get("rca_cycle", 1)),
            _PHASE_INDEX.get(t.metadata.get("phase"), len(_PHASES)),
        )
    )
    history = []
    for t in chain:
        history.append(
            {
                "bead_id": t.id,
                "cycle": t.metadata.get("rca_cycle"),
                "phase": t.metadata.get("phase"),
                "root_cause": t.metadata.get("root_cause"),
                "fix_plan": t.metadata.get("fix_plan"),
                "notes": t.result,
            }
        )
    return history


def escalate_rca(beads: Beads, root: Task, history: list[dict]) -> Task:
    """Create the human-escalation bead once MAX_RCA_CYCLES is exhausted
    without a validated fix. Does not touch the root bead's own status — the
    escalation bead is the visible next step; the root stays inspectable
    with its full chain of children."""
    failed_title = root.metadata.get("rca_failed_title") or root.title
    rca_for = root.metadata.get("rca_for")
    lines = [
        f"**RCA for:** {failed_title} (failed task: {rca_for or '?'})",
        f"**Escalated after {MAX_RCA_CYCLES} cycle(s) without a validated fix.**",
        "",
        "**Original error**",
        root.metadata.get("rca_error") or "(not recorded)",
        "",
        "**Full RCA history (all cycles' causes + attempted fixes)**",
    ]
    for h in history:
        lines.append(f"\n--- Cycle {h.get('cycle')} · {h.get('phase')} ({h.get('bead_id')}) ---")
        if h.get("root_cause"):
            lines.append(f"Root cause: {h['root_cause']}")
        if h.get("fix_plan"):
            lines.append(f"Fix plan: {h['fix_plan']}")
        if h.get("notes"):
            lines.append(f"Notes/result: {h['notes']}")
    description = "\n".join(lines)
    return beads.create(
        title=f"[RCA ESCALATED] {failed_title}",
        description=description,
        priority=TaskPriority.CRITICAL,
        assigned_to=escalation_assignee(),
        source="rca",
        parent_id=root.id,
        metadata={
            "rca_for": rca_for,
            "rca_root": root.id,
            "escalated_after_cycles": MAX_RCA_CYCLES,
        },
    )
