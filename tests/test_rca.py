"""RCA loop (agentco/rca.py): analyze -> verify_plan -> execute -> validate,
driven by beads linked through metadata (rca_for / rca_root / rca_cycle /
phase) and parent_id (every phase bead parents off the RCA root directly).

Invariants under test:
  * create_rca_task makes a root bead with the right metadata/phase (a).
  * advance_rca walks the full chain analyze -> verify_plan -> execute
    -> validate, with each phase bead correctly linked to the root (b).
  * a validate outcome of "not fixed" loops back to a fresh analyze bead
    with rca_cycle incremented (c).
  * exceeding MAX_RCA_CYCLES escalates to a human bead instead of looping
    again (d).
  * the orchestrator's failure hook creates an RCA for a normal failed task
    but never for a bead that is itself an RCA bead — no RCA-of-RCA (e).

No network — beads are constructed directly against a tmp_path JSONL file.
"""

from __future__ import annotations

from agentco_harness.beads import Beads, TaskPriority, TaskStatus
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.orchestrator import Orchestrator
from agentco_harness.rca import (
    MAX_RCA_CYCLES,
    _BEAD_ID_PLACEHOLDER,
    advance_rca,
    create_rca_task,
    has_terminal_action,
)


def _orch(tmp_path) -> Orchestrator:
    """A minimal orchestrator against a local-model config — no network,
    no API keys required (same convention as tests/test_backoff.py)."""
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


# --------------------------------------------------------------------- (a)


def test_create_rca_task_creates_root_with_correct_metadata(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(
        title="Ingest youtube: http://youtube.com/@danmartell",
        description="ingest a channel",
    )
    error = "claude binary 'claude' not found on PATH — cannot execute task"
    beads.fail(failed.id, result=error)
    failed = beads.get(failed.id)

    root = create_rca_task(beads, failed, error)

    assert root.title == "[RCA] Ingest youtube: http://youtube.com/@danmartell"
    assert root.priority == TaskPriority.HIGH
    assert root.source == "rca"
    assert root.assigned_agent == "claude"
    assert root.status == TaskStatus.PENDING
    assert root.parent_id is None  # root is top-level; rca_for carries the link
    assert root.metadata["phase"] == "analyze"
    assert root.metadata["rca_cycle"] == 1
    assert root.metadata["rca_for"] == failed.id
    assert root.metadata["rca_root"] == root.id
    assert error in root.description
    assert "Root cause" in root.description
    assert "Fix plan" in root.description
    assert "How to reproduce" in root.description
    assert "What's affected" in root.description


def test_create_rca_task_accepts_seeded_context(tmp_path):
    """Manual/rich seeding (used for hand-created RCAs) lands in the description."""
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Some failed task", description="d")

    root = create_rca_task(
        beads,
        failed,
        "boom",
        reproduce="run X from an env without Y on PATH",
        affected="every task that shells out to Y",
        candidate_fix_plan="resolve Y to an absolute path",
    )

    assert "run X from an env without Y on PATH" in root.description
    assert "every task that shells out to Y" in root.description
    assert "resolve Y to an absolute path" in root.description


# --------------------------------------------------------------------- (b)


def test_advance_rca_walks_full_chain(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Ingest youtube: foo", description="d")
    root = create_rca_task(beads, failed, "claude binary not found on PATH")

    verify_plan = advance_rca(
        beads, root, {"root_cause": "bare claude_bin", "fix_plan": "resolve via shutil.which"}
    )
    assert verify_plan.metadata["phase"] == "verify_plan"
    assert verify_plan.parent_id == root.id
    assert verify_plan.metadata["rca_root"] == root.id
    assert verify_plan.metadata["rca_for"] == failed.id
    assert verify_plan.metadata["rca_cycle"] == 1
    assert verify_plan.source == "rca"
    assert verify_plan.status == TaskStatus.PENDING  # no approval gate at this phase

    execute = advance_rca(beads, verify_plan, {"sound": True, "reason": "minimal and safe"})
    assert execute.metadata["phase"] == "execute"
    assert execute.parent_id == root.id
    assert execute.metadata["rca_root"] == root.id
    assert execute.status == TaskStatus.PENDING_APPROVAL  # human approves before code changes

    validate = advance_rca(beads, execute, {"notes": "applied shutil.which fix"})
    assert validate.metadata["phase"] == "validate"
    assert validate.parent_id == root.id
    assert validate.metadata["rca_root"] == root.id

    done_root = advance_rca(beads, validate, {"fixed": True, "notes": "re-ran ingest, it worked"})
    assert done_root.id == root.id
    assert done_root.status == TaskStatus.DONE
    assert "resolved" in (done_root.result or "").lower()
    assert "re-ran ingest" in (done_root.result or "")


def test_verify_plan_not_sound_loops_back_to_analyze(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="X", description="d")
    root = create_rca_task(beads, failed, "err")

    verify_plan = advance_rca(beads, root, {"root_cause": "rc", "fix_plan": "fp"})
    next_analyze = advance_rca(beads, verify_plan, {"sound": False, "reason": "too risky"})

    assert next_analyze.metadata["phase"] == "analyze"
    assert next_analyze.metadata["rca_cycle"] == 2
    assert next_analyze.metadata["rca_root"] == root.id
    assert next_analyze.parent_id == root.id
    assert next_analyze.id != root.id


# --------------------------------------------------------------------- (c)


def test_validate_not_fixed_increments_cycle(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="X", description="d")
    root = create_rca_task(beads, failed, "err")

    verify_plan = advance_rca(beads, root, {"root_cause": "rc", "fix_plan": "fp"})
    execute = advance_rca(beads, verify_plan, {"sound": True, "reason": "ok"})
    validate = advance_rca(beads, execute, {"notes": "applied"})

    next_analyze = advance_rca(beads, validate, {"fixed": False, "notes": "still broken"})

    assert next_analyze.metadata["phase"] == "analyze"
    assert next_analyze.metadata["rca_cycle"] == 2
    assert next_analyze.metadata["rca_root"] == root.id
    assert next_analyze.metadata["rca_for"] == failed.id
    assert next_analyze.parent_id == root.id
    assert next_analyze.id != root.id

    # The original root is untouched (still open, not DONE) — the chain
    # continues via the new analyze bead, not by mutating the root's phase.
    root_after = beads.get(root.id)
    assert root_after.status == TaskStatus.PENDING


# --------------------------------------------------------------------- (d)


def test_exceeding_max_cycles_escalates_to_human(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Ingest youtube: foo", description="d")
    bead = create_rca_task(beads, failed, "claude binary not found on PATH")
    root_id = bead.id

    for cycle in range(1, MAX_RCA_CYCLES + 1):
        verify_plan = advance_rca(beads, bead, {"root_cause": "rc", "fix_plan": "fp"})
        execute = advance_rca(beads, verify_plan, {"sound": True, "reason": "ok"})
        validate = advance_rca(beads, execute, {"notes": "applied"})
        bead = advance_rca(beads, validate, {"fixed": False, "notes": f"still broken (cycle {cycle})"})

    # After MAX_RCA_CYCLES failed validations, the loop does NOT spawn a 4th
    # analyze bead — it escalates to a human instead.
    assert bead.title.startswith("[RCA ESCALATED]")
    assert bead.assigned_to == "human:operator"
    assert bead.priority == TaskPriority.CRITICAL
    assert bead.source == "rca"
    assert bead.metadata["rca_root"] == root_id
    assert bead.metadata["rca_for"] == failed.id
    assert f"{MAX_RCA_CYCLES} cycle(s)" in bead.description
    assert "Cycle 1" in bead.description
    assert f"Cycle {MAX_RCA_CYCLES}" in bead.description

    all_tasks = beads.list()
    analyze_beads = [t for t in all_tasks if t.metadata.get("phase") == "analyze"]
    assert len(analyze_beads) == MAX_RCA_CYCLES  # exactly cycles 1..MAX, no 4th

    escalation_beads = [t for t in all_tasks if t.assigned_to == "human:operator"]
    assert len(escalation_beads) == 1


# --------------------------------------------------------------------- (e)


def test_orchestrator_fail_creates_rca_for_normal_task(tmp_path):
    orch = _orch(tmp_path)
    task = orch.beads.create(
        title="Ingest youtube: http://youtube.com/@danmartell",
        description="d",
        assigned_agent="claude",
    )
    orch.beads.claim(task.id, "claude")

    orch._fail_with_rca(task, "claude binary 'claude' not found on PATH — cannot execute task")

    failed = orch.beads.get(task.id)
    assert failed.status == TaskStatus.FAILED

    rca_beads = [t for t in orch.beads.list() if t.metadata.get("rca_for") == task.id]
    assert len(rca_beads) == 1
    assert rca_beads[0].source == "rca"
    assert rca_beads[0].metadata["phase"] == "analyze"


def test_orchestrator_does_not_rca_an_rca_bead(tmp_path):
    """Guard against RCA-of-RCA: a failed bead whose own source is 'rca'
    (an RCA phase bead) must not spawn another RCA chain."""
    orch = _orch(tmp_path)
    original_failed = orch.beads.create(title="Ingest youtube: foo", description="d")
    rca_bead = create_rca_task(orch.beads, original_failed, "boom")

    orch._fail_with_rca(rca_bead, "the analyze agent itself crashed mid-run")

    failed = orch.beads.get(rca_bead.id)
    assert failed.status == TaskStatus.FAILED

    rca_of_rca = [t for t in orch.beads.list() if t.metadata.get("rca_for") == rca_bead.id]
    assert rca_of_rca == []


def test_every_rca_phase_bead_carries_a_real_budget(tmp_path):
    """RCA beads inherited DEFAULT_TIMEOUT (600s) and died mid-analysis.

    An analysis bead is strictly more work than the bead it analyzes, so it
    must not run on a smaller budget than the work it is analyzing.
    """
    from agentco_harness.executor import DEFAULT_TIMEOUT
    from agentco_harness.rca import RCA_BUDGET, advance_rca, create_rca_task

    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Ingest youtube: @x", description="d")

    root = create_rca_task(beads, failed, error="boom")
    assert root.metadata["budget"] == RCA_BUDGET

    verify = advance_rca(beads, root, {"root_cause": "rc", "fix_plan": "fp"})
    execute = advance_rca(beads, verify, {"sound": True})
    validate = advance_rca(beads, execute, {"notes": "applied"})

    for bead in (verify, execute, validate):
        assert bead.metadata.get("budget") == RCA_BUDGET, bead.metadata.get("phase")
        assert bead.metadata["budget"]["timeout"] > DEFAULT_TIMEOUT


# --- RCA dedup (2026-08-04, sommeliwhey box-scout) -------------------------
#
# One config defect produced 72 [RCA] beads across 41 distinct titles for a
# SINGLE finding, and on 2026-08-04 those duplicates were 100% of the node's
# spend ($42.44 / 24 costed runs, 24 of 24 an [RCA] box-scout bead). Two
# independent routes made the duplicates; both are covered below.


def test_same_error_in_one_batch_yields_one_rca_root(tmp_path):
    """Fan-out route: N beads failing identically must open ONE investigation.

    The 2026-08-04 incident failed 30 beads in a single cycle with byte-identical
    "Unknown agent: box-scout" strings, and _fail_with_rca spawned a root for
    every one of them. The 2nd..Nth failure now folds into the first root.
    """
    orch = _orch(tmp_path)
    error = "Unknown agent: box-scout"

    failed = [
        orch.beads.create(title=f"box-scout: BRAND{i} (brand{i})", description="crawl")
        for i in range(5)
    ]
    for task in failed:
        orch._fail_with_rca(task, error)

    roots = [t for t in orch.beads.list() if t.title.startswith("[RCA]")]
    assert len(roots) == 1, [t.title for t in roots]

    # The single root still knows every bead the defect took down.
    root = roots[0]
    assert root.metadata["rca_for"] == failed[0].id
    assert root.metadata["rca_also_failed"] == [t.id for t in failed[1:]]

    # Every failed bead is still individually FAILED — dedup is about the
    # investigation, not about hiding failures.
    for task in failed:
        assert orch.beads.get(task.id).status == TaskStatus.FAILED


def test_retried_bead_reuses_its_open_rca_root(tmp_path):
    """Retry route: `tasks retry --all-failed` must not double the RCA count.

    The standard queue repair resets failed beads to pending; the same incident
    re-fails the same brand while the investigation is still open. Keyed on the
    failed bead's ID, the second failure finds the first root — which is why 31
    of 41 box-scout RCA titles carried two beads each.
    """
    orch = _orch(tmp_path)
    error = "Unknown agent: box-scout"

    task = orch.beads.create(title="box-scout: LUCKAU (luckau)", description="crawl")
    first = orch._fail_with_rca(task, error)
    root_ids = {t.id for t in orch.beads.list() if t.title.startswith("[RCA]")}
    assert len(root_ids) == 1

    # Retry the bead and fail it the same way again, root still open.
    root_id = next(iter(root_ids))
    orch.beads.update(task.id, status=TaskStatus.PENDING, result=None)
    orch._fail_with_rca(orch.beads.get(task.id), error)

    roots = [t for t in orch.beads.list() if t.title.startswith("[RCA]")]
    assert len(roots) == 1, [t.id for t in roots]
    assert roots[0].id == root_id
    assert first is not None


def test_same_bead_refailing_after_its_rca_closed_opens_a_new_investigation(tmp_path):
    """A closed root must not absorb the SAME bead's next incident either.

    This is the sommeliwhey 08-04 case: MAGRINHA re-failed with the byte-identical
    "Unknown agent: box-scout" string it carried on 07-29, but through a different
    vector (`init` clobbering config, not the missing _external_agent guard).
    Keying on source_id in ANY state folded it into the DONE 07-29 root, so the
    real cause would never have been investigated.
    """
    orch = _orch(tmp_path)
    error = "Unknown agent: box-scout"

    task = orch.beads.create(title="box-scout: MAGRINHA (magrinha)", description="crawl")
    orch._fail_with_rca(task, error)
    first_root = next(t for t in orch.beads.list() if t.title.startswith("[RCA]"))
    orch.beads.update(first_root.id, status=TaskStatus.DONE, result="analyzed")

    orch.beads.update(task.id, status=TaskStatus.PENDING, result=None)
    orch._fail_with_rca(orch.beads.get(task.id), error)

    roots = [t for t in orch.beads.list() if t.title.startswith("[RCA]")]
    assert len(roots) == 2, [t.id for t in roots]
    fresh = next(t for t in roots if t.id != first_root.id)
    # The recurrence is labelled as one, so the analyst does not re-derive the
    # closed root's finding.
    assert fresh.metadata["rca_recurrence_of"] == first_root.id
    assert first_root.id in fresh.description


def test_recurrence_batch_still_collapses_to_one_new_root(tmp_path):
    """Re-opening on a closed root must not re-open N of them.

    30 previously-RCA'd beads failing together in a new incident produce ONE new
    investigation (the 2nd..Nth fold in by error), not 30.
    """
    orch = _orch(tmp_path)
    error = "Unknown agent: box-scout"

    failed = [
        orch.beads.create(title=f"box-scout: BRAND{i} (brand{i})", description="crawl")
        for i in range(4)
    ]
    for task in failed:
        orch._fail_with_rca(task, error)
        root = next(
            t
            for t in orch.beads.list()
            if t.title.startswith("[RCA]") and t.metadata.get("rca_for") == task.id
        )
        orch.beads.update(root.id, status=TaskStatus.DONE, result="analyzed")
        orch.beads.update(task.id, status=TaskStatus.PENDING, result=None)
    closed_ids = {t.id for t in orch.beads.list() if t.title.startswith("[RCA]")}
    assert len(closed_ids) == 4

    for task in failed:
        orch._fail_with_rca(orch.beads.get(task.id), error)

    fresh = [
        t
        for t in orch.beads.list()
        if t.title.startswith("[RCA]") and t.id not in closed_ids
    ]
    assert len(fresh) == 1, [t.id for t in fresh]
    assert fresh[0].metadata["rca_also_failed"] == [t.id for t in failed[1:]]


def test_a_different_error_still_gets_its_own_rca(tmp_path):
    """Dedup must not swallow genuinely distinct failures."""
    orch = _orch(tmp_path)

    a = orch.beads.create(title="box-scout: A (a)", description="crawl")
    b = orch.beads.create(title="box-scout: B (b)", description="crawl")
    orch._fail_with_rca(a, "Unknown agent: box-scout")
    orch._fail_with_rca(b, "connection refused: postgres:5432")

    roots = [t for t in orch.beads.list() if t.title.startswith("[RCA]")]
    assert len(roots) == 2, [t.title for t in roots]


def test_closed_rca_does_not_absorb_a_fresh_recurrence(tmp_path):
    """A CLOSED root is not a place to hide next month's incident.

    Only OPEN roots absorb same-error failures; a new bead failing the same way
    after the investigation closed deserves its own investigation.
    """
    orch = _orch(tmp_path)
    error = "Unknown agent: box-scout"

    a = orch.beads.create(title="box-scout: A (a)", description="crawl")
    orch._fail_with_rca(a, error)
    root = next(t for t in orch.beads.list() if t.title.startswith("[RCA]"))
    orch.beads.update(root.id, status=TaskStatus.DONE, result="fixed")

    b = orch.beads.create(title="box-scout: B (b)", description="crawl")
    orch._fail_with_rca(b, error)

    roots = [t for t in orch.beads.list() if t.title.startswith("[RCA]")]
    assert len(roots) == 2, [t.id for t in roots]


# --- terminal-action gate (2026-08-11, ac-d82a660f) ------------------------
#
# Two RCAs (2026-08-09, 2026-08-10) diagnosed the SAME nightly failure
# correctly, each ended with "say the word and I will apply it", each closed
# `done` having changed no file and filed no bead — and nobody was there to
# say the word, because the RCA runs headless at 04:17 UTC. An RCA that closes
# on analysis alone is a no-op that bills full price and lets the failure
# recur on schedule. These tests pin the prompt block AND the gate that makes
# it non-optional.


def test_analyze_bead_carries_the_terminal_action_gate(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Ingest youtube @aidotengineer", description="d")
    root = create_rca_task(beads, failed, "timed out after 900s")

    verify = root.metadata["verify"]
    assert verify["class"] == "deterministic"
    assert "rca-check" in verify["check"]
    assert root.id in verify["check"]
    assert str(beads.path) in verify["check"]

    # The instructions name THIS bead's real id, not a placeholder — the agent
    # can paste the fix-bead command verbatim.
    assert "Terminal action" in root.description
    assert f"--parent {root.id}" in root.description
    assert _BEAD_ID_PLACEHOLDER not in root.description


def test_has_terminal_action_is_none_until_something_follows(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="X", description="d")
    root = create_rca_task(beads, failed, "err")

    assert has_terminal_action(beads, root) is None

    fix = beads.create(title="Raise the ingest timeout", description="d", parent_id=root.id)
    assert has_terminal_action(beads, beads.get(root.id)) == fix.id


def test_advancing_the_loop_is_itself_a_terminal_action(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="X", description="d")
    root = create_rca_task(beads, failed, "err")

    verify_plan = advance_rca(beads, root, {"root_cause": "rc", "fix_plan": "fp"})

    assert has_terminal_action(beads, beads.get(root.id)) == verify_plan.id


def test_a_later_cycle_needs_its_own_terminal_action(tmp_path):
    """Cycle 1's phase beads do not discharge cycle 2's obligation.

    Otherwise the second analysis inherits the first one's evidence and can
    close empty — exactly the pattern this gate exists to stop.
    """
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="X", description="d")
    root = create_rca_task(beads, failed, "err")
    verify_plan = advance_rca(beads, root, {"root_cause": "rc", "fix_plan": "fp"})
    cycle2 = advance_rca(beads, verify_plan, {"sound": False, "reason": "too risky"})

    assert cycle2.metadata["rca_cycle"] == 2
    assert has_terminal_action(beads, cycle2) is None


def test_closing_on_analysis_alone_lands_in_verify_failed(tmp_path):
    """The whole point: `complete` on an empty-handed analysis is NOT done."""
    beads = Beads(tmp_path / "tasks.jsonl")
    failed = beads.create(title="Ingest youtube @aidotengineer", description="d")
    root = create_rca_task(beads, failed, "timed out after 900s")

    blocked = beads.complete(
        root.id,
        result='{"status": "complete", "output": "diagnosed it; say the word and I will apply the fix"}',
    )
    assert blocked.status == TaskStatus.VERIFY_FAILED
    assert blocked.metadata["verify_result"]["passed"] is False
    assert "no terminal action" in blocked.metadata["verify_result"]["output_tail"]

    # File the fix bead and the same completion goes through — the gate
    # re-runs on every complete(), so this is the documented recovery path.
    beads.create(
        title="Raise the ingest watchdog budget past 900s",
        description="the concrete minimal change",
        parent_id=root.id,
    )
    now_done = beads.complete(root.id, result='{"status": "complete", "output": "fix bead filed"}')
    assert now_done.status == TaskStatus.DONE
