"""ac-3de1dd9d: a second classification step, after `Classifier.process` has
already created a plain bead, that asks one more question — is this actually
an instance of a procedure this node already knows?

Three outcomes, each recorded on the bead's own metadata as `asop_route`:

* **MATCH** — an ACTIVE ASOP fits, confidently. A run is filed and pinned to
  that ASOP's current version (`AsopStore.run`, the same path `sop run`
  uses), and the plain bead is left pointing at it rather than duplicating
  it — see "Design: MATCH and the plain bead" below. **Except** under
  `config.intake.require_approval` ("inverse shadow" mode): there, a MATCH
  is decided and recorded but never filed — `asop_route.held` is set, and
  `finalize_held_match` files it later, at `approve task` time, when a human
  says the classifier-created bead itself may run.
* **PLAIN** — no match. The bead is untouched beyond the `asop_route` tag;
  this is the safe default and what happens when the router is off, errors,
  or simply finds nothing.
* **CANDIDATE** — no match, but enough COMPLETED beads already look like this
  one that a human might want to draft a procedure for it. The bead stays
  plain; an entry is appended to `asop_candidates.jsonl` beside the ASOP
  store for a human to review. Activation is never automatic (ASOP.md
  §8.1 reserves it to a human, or an agent under the revision policy — a
  router guessing its way to a new procedure is neither of those).

## Design: MATCH and the plain bead

`AsopStore.run` always files a NEW parent bead plus one bead per step — it
has no notion of "reuse this existing bead as the parent", and teaching it
one would mean a run filed by `sop run` and a run filed by the router leave
the parent in different shapes for the same contract. So the router does not
try to make the classifier's bead double as the run's parent. Instead:

  1. the run is filed exactly as `sop run` would file it — a fresh parent,
     pinned to `(asop_id, version)`, with its own step tree;
  2. the classifier's original bead is annotated with `asop_route.run_id`
     pointing at that parent, then closed with `Beads.retire` — the existing
     verb for "administrative close of ungated work, → SKIPPED, never DONE".

SKIPPED, not DONE: the bead's own work was never done, it was redirected.
SKIPPED (not left PENDING) because a plain PENDING bead sitting next to a run
built to do the same thing is live, dispatchable duplicate work. `retire`
itself refuses a bead carrying `metadata.verify` — never a concern here,
since the classifier never sets one — and using it rather than a raw status
write keeps this module from opening a second door onto SKIPPED (D9/N9,
`tests/test_update_call_sites.py` and `tests/test_terminal_paths.py`).

If the run cannot actually be filed — a role the ASOP declares has no
configured binding, or a declared input this router cannot derive from the
bead — the match is still recorded (outcome stays "match", `run_error` says
why), and the bead is left exactly as the classifier produced it: PENDING,
dispatchable, unrouted. A partially-filed run is a worse failure mode than
one that never started, so filing is all-or-nothing.

## Never blocking bead creation

The bead this module receives already exists — `Classifier.process` created
it before the router ever sees it. Every path through `route()` either
leaves that bead as-is or annotates it; none of them can undo the create.
`observe()` wraps each event in its own try/except already, so an exception
raised anywhere in here costs one event, not the cycle.

## On LM failure: PLAIN outright, not a fallback to CANDIDATE

A broken router degrades to exactly what an OFF router looks like — PLAIN —
rather than falling through to the deterministic candidate check. The
candidate check is cheap and does not need the LM, so it would still "work",
but treating a broken match step as evidence for surfacing a NEW procedure
answers a question ("should a human consider building an ASOP for this?")
using a signal (repeated similar work) that had nothing to do with the
failure. Keeping the two independent means an LM outage never quietly starts
recommending procedures it was never asked about.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .asop_store import AsopStore
from .beads import Beads, Task, TaskStatus
from .config import AsopRouterConfig

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _title_similarity(a: str, b: str) -> float:
    """Token-Jaccard similarity of two titles. 0.0 if either is empty."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class RouteResult:
    """What the router decided, and what it did about it."""

    outcome: str  # "match" | "plain" | "candidate"
    reason: str = ""
    asop_id: Optional[str] = None
    version: Optional[int] = None
    confidence: Optional[float] = None
    run_id: Optional[str] = None
    run_error: Optional[str] = None
    similar_count: Optional[int] = None
    #: A MATCH found while `intake.require_approval` is on — filing is
    #: deferred to `finalize_held_match`, at `approve task` time. Never True
    #: alongside `run_id` or `run_error`: a held match has attempted nothing
    #: yet, one of those two means it has.
    held: bool = False

    def as_metadata(self) -> dict:
        """The `asop_route` value written to the bead. Omits unset fields
        rather than writing them as null — a MATCH's `run_error` and a
        PLAIN's `asop_id` are both simply absent, not present-and-empty."""
        out: dict = {"outcome": self.outcome, "reason": self.reason}
        for key in ("asop_id", "version", "confidence", "run_id", "run_error", "similar_count"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.held:
            out["held"] = True
        return out


def candidates_log_path(store: AsopStore) -> Path:
    """Where CANDIDATE detections are appended, beside the ASOP store itself."""
    return store.path.parent / "asop_candidates.jsonl"


def _active_asops(store: AsopStore) -> list:
    """Every ACTIVE version in the store — `store.list()` gives summary rows
    keyed by asop_id; this resolves each active one to its full record, which
    is what both the LM candidate list and `store.run` need (roles, inputs)."""
    return [
        store.get(row["asop_id"])
        for row in store.list()
        if row["active_version"] is not None
    ]


def _candidate_payload(active: list) -> str:
    return json.dumps(
        [
            {
                "asop_id": rec.asop_id,
                "title": rec.title,
                "task_type": rec.task_type,
                "purpose": rec.purpose,
            }
            for rec in active
        ]
    )


def default_predictor():
    """`dspy.ChainOfThought(RouteToAsop)`, built lazily — the same posture as
    `Classifier`: importing this module must not require the optional `lm`
    extra, only calling this does."""
    from . import _lm

    dspy = _lm.dspy("The ASOP router")
    from .signatures import RouteToAsop

    return dspy.ChainOfThought(RouteToAsop)


def _run_inputs(task: Task, declared: list[dict]) -> Optional[dict]:
    """Best-effort inputs for `store.run`, drawn only from what a bead always
    carries. Returns None if a declared input has no honest source here —
    filing with a guessed value is worse than not filing at all."""
    available = {
        "title": task.title,
        "description": task.description,
        "category": (task.metadata or {}).get("category"),
        "source": task.source,
        "source_id": task.source_id,
    }
    inputs: dict = {}
    for spec in declared:
        name = spec["name"]
        if name not in available or available[name] is None:
            return None
        inputs[name] = available[name]
    return inputs


def _run_bindings(roles: dict, configured: dict) -> Optional[dict]:
    """`configured` narrowed to the roles this ASOP actually declares, or None
    if any declared role has no configured actor."""
    bindings: dict = {}
    for role in roles:
        if role not in configured:
            return None
        bindings[role] = configured[role]
    return bindings


def _record_candidate(store: AsopStore, task: Task, similar_count: int) -> None:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "task_id": task.id,
        "title": task.title,
        "category": (task.metadata or {}).get("category"),
        "similar_count": similar_count,
    }
    path = candidates_log_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _similar_completed_count(beads: Beads, task: Task, threshold: float) -> int:
    category = (task.metadata or {}).get("category")
    done = beads.list(status=TaskStatus.DONE)
    return sum(
        1
        for t in done
        if t.id != task.id
        and (t.metadata or {}).get("category") == category
        and _title_similarity(t.title, task.title) >= threshold
    )


def _match(task: Task, active: list, predict, threshold: float) -> Optional[tuple]:
    """Ask the LM; return (record, confidence, reason) for a confident,
    validated match, or None. Raises on an LM failure — the caller decides
    what "on LM error" means, this function only knows how to ask."""
    predictor = predict or default_predictor()
    answer = predictor(
        category=(task.metadata or {}).get("category", ""),
        title=task.title,
        description=task.description,
        candidates=_candidate_payload(active),
    )
    matched_id = (getattr(answer, "matched_asop_id", "") or "").strip()
    if not matched_id:
        return None
    try:
        confidence = float(getattr(answer, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = getattr(answer, "reason", "") or ""
    record = next((r for r in active if r.asop_id == matched_id), None)
    if record is None:
        # The LM named an id that is not in the candidate list it was given.
        # Never trust a hallucinated id — treated identically to "no match".
        return None
    if confidence < threshold:
        return None
    return record, confidence, reason


def _file_match(result: RouteResult, record, task: Task, *, store: AsopStore, beads: Beads,
                 config: AsopRouterConfig) -> None:
    """Try to file the run a MATCH implies. Updates `result` in place with
    either `run_id` (filed; the classifier bead is moved to SKIPPED by the
    caller) or `run_error` (not filed; the bead is left exactly as it was)."""
    from asop.errors import Refusal

    inputs = _run_inputs(task, record.inputs)
    if inputs is None:
        result.run_error = "declared input(s) have no honest source on this bead — not filed"
        return
    bindings = _run_bindings(record.roles, config.bindings)
    if bindings is None:
        result.run_error = (
            "role(s) this procedure declares have no configured binding "
            "(asop_router.bindings) — not filed"
        )
        return
    try:
        parent = store.run(
            record.asop_id, inputs=inputs, bindings=bindings, beads=beads,
            version=record.version, title=f"{task.title} — {record.title}",
        )
    except Refusal as e:
        result.run_error = f"{e.code}: {e.message}"
        return
    result.run_id = parent.id


def route(
    task: Task,
    *,
    store: AsopStore,
    beads: Beads,
    config: AsopRouterConfig,
    predict: Optional[Callable[..., object]] = None,
    require_approval: bool = False,
) -> RouteResult:
    """Classify `task` against the node's ASOP library and act on the answer.

    Caller's contract: `task` already exists in `beads` — this is the SECOND
    classification step, run after `Classifier.process`. Never raises for an
    ordinary routing miss or LM failure; both degrade to PLAIN.

    `require_approval` mirrors `config.intake.require_approval` (the
    "inverse shadow" mode) — a MATCH is still decided and recorded here, it
    is simply never FILED here: see `finalize_held_match`, called at
    `approve task` time, for the other half of a held match's life.
    """
    result = _decide(task, store, beads, config, predict, require_approval)

    beads.annotate(task.id, {"asop_route": result.as_metadata()})
    if result.outcome == "match" and result.run_id:
        # `retire`, not a raw status write: it is exactly this shape — an
        # administrative close of UNGATED work to SKIPPED, never DONE — and
        # using the existing verb instead of a second door keeps this the
        # only door SKIPPED has outside `beads.py` itself (see D9/N9 in
        # tests/test_update_call_sites.py and test_terminal_paths.py).
        beads.retire(task.id, by="asop_router", reason=f"routed to run {result.run_id}")
    return result


def _decide(
    task: Task, store: AsopStore, beads: Beads, config: AsopRouterConfig, predict,
    require_approval: bool = False,
) -> RouteResult:
    """The outcome, with filing/logging already applied where a MATCH (not
    held) or a CANDIDATE was found. Split out from `route()` so the
    annotate + retire calls that follow every outcome live in exactly one
    place, rather than each branch below repeating them."""
    active = _active_asops(store)

    if active:
        try:
            hit = _match(task, active, predict, config.confidence_threshold)
        except Exception as exc:  # noqa: BLE001 — any LM failure degrades to PLAIN
            return RouteResult(outcome="plain", reason=f"router error, degraded to plain: {exc}")
        if hit is not None:
            record, confidence, reason = hit
            result = RouteResult(
                outcome="match", asop_id=record.asop_id, version=record.version,
                confidence=confidence, reason=reason,
            )
            if require_approval:
                # Decided, not filed: intake holds every bead for approval,
                # and a runnable run is exactly the thing that must not exist
                # before a human says so. `finalize_held_match` picks this up
                # by (asop_id, version) at `approve task` time.
                result.held = True
                result.reason = f"{reason} — held for approval (intake.require_approval)"
                return result
            _file_match(result, record, task, store=store, beads=beads, config=config)
            return result

    similar = _similar_completed_count(beads, task, config.candidate_similarity_threshold)
    if similar >= config.candidate_min_similar:
        _record_candidate(store, task, similar)
        return RouteResult(
            outcome="candidate", similar_count=similar,
            reason=f"{similar} completed bead(s) of category "
                   f"{(task.metadata or {}).get('category')!r} look like this one",
        )
    return RouteResult(outcome="plain", reason="no matching active procedure")


def finalize_held_match(
    task: Task, *, store: AsopStore, beads: Beads, config: AsopRouterConfig
) -> Optional[RouteResult]:
    """The other half of a HELD match's life: called at `approve task` time
    (`cli.py`'s `approve task` / `approve all`), before the ordinary
    `Beads.approve` promotion.

    Returns None when there is nothing to finalize — no held match on this
    bead, or its status is not PENDING_APPROVAL — and the caller falls
    through to `beads.approve()` exactly as if the router had never run.

    Otherwise files the run (same all-or-nothing rules `_file_match` always
    applies: an unbound role or an undeclared input refuses the run rather
    than guess) and returns the outcome. A caller sees `result.run_id` set
    when it filed — the bead is now SKIPPED, already retired here, and
    `beads.approve()` must NOT be called on it (it would refuse: the bead is
    no longer PENDING_APPROVAL). A caller sees `result.run_error` set when
    filing failed — the bead is untouched, still PENDING_APPROVAL, and an
    ordinary `beads.approve()` promotes it to PENDING same as any other bead
    (unrouted, exactly as if the match had never held).
    """
    if task.status != TaskStatus.PENDING_APPROVAL:
        return None
    held = (task.metadata or {}).get("asop_route") or {}
    if held.get("outcome") != "match" or not held.get("held"):
        return None

    asop_id, version = held.get("asop_id"), held.get("version")
    result = RouteResult(
        outcome="match", asop_id=asop_id, version=version,
        confidence=held.get("confidence"), reason=held.get("reason", ""),
    )
    record = store.get(asop_id, version)
    if record is None:
        result.run_error = f"{asop_id!r} v{version} no longer exists — not filed"
    else:
        _file_match(result, record, task, store=store, beads=beads, config=config)

    beads.annotate(task.id, {"asop_route": result.as_metadata()})
    if result.run_id:
        beads.retire(task.id, by="asop_router", reason=f"approved -> routed to run {result.run_id}")
    return result
