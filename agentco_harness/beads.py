"""Beads - Git-backed task management.

Simple JSONL-based task queue. Each line is a task.
Git provides history, branching, and collaboration.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from .natural_key import (  # noqa: F401  (re-exported for callers of beads)
    NATURAL_KEY_FIELD,
    NaturalKeyError,
    derive_natural_key,
    natural_key_of,
)


# Structural guardrails against runaway agent decomposition. A single worked task
# must not be able to spawn an unbounded tree of deeper work (Recorro 2026-06-14).
# Raised 2026-08-05 (operator request) to support goal-level beads: a stated goal decomposes
# into as many sub-beads as the goal needs. Max leaves = MAX_SUBTASKS_PER_TASK **
# MAX_SUBTASK_DEPTH, so 7**3 = 343 (was 5**2 = 25).
#
# These remain BACKSTOPS, not budgets. They exist because a misbehaving agent that
# decomposes recursively is this codebase's founding defect class (Recorro 2026-06-14),
# and the real limit on a goal is human review capacity, not the cap. A goal needing
# more than ~15 leaves is usually two goals.
MAX_SUBTASK_DEPTH = 3  # max generations of children below a root (root = depth 0)
MAX_SUBTASKS_PER_TASK = 7  # max subtasks one task may spawn in a single execution

# The one shape a bead id may take, mirroring create(): "ac-" + 8 hex chars from
# uuid4().hex. Anchored and fully-fullmatched on purpose — `re.match` alone would
# happily accept the trailing-newline corruption this exists to stop.
TASK_ID_PATTERN = re.compile(r"^ac-[a-f0-9]{8}$")


class DepthLimitError(Exception):
    """Raised when creating a task would exceed MAX_SUBTASK_DEPTH."""


class DependencyCycleError(Exception):
    """Raised when a ``blocked_by`` edge would close a dependency cycle.

    An undetected cycle is a SILENT deadlock, which is the failure mode this
    codebase least tolerates: every task in the loop waits on another member
    forever, so none of them ever satisfies ``ready()``, none is dispatched,
    and ``me`` reports them as ordinarily "blocked" with no hint the state is
    unresolvable. Nothing is stale, nothing errors, nothing is late — the work
    simply never happens.

    The tree edge (``parent_id``) has been guarded by ``DepthLimitError``
    since decomposition shipped; this is the equivalent guard for the
    dependency edge. Prevention at write time is preferred over detection at
    read time because the offending edge is still in hand and can be named.

    We never auto-break a cycle: silently dropping an edge is an undisclosed
    data-loss decision, and the user cannot see what they lost.
    """


class TaskReferenceError(ValueError):
    """Raised when ``parent_id``/``blocked_by`` is malformed or names no task.

    The same silent-deadlock class as ``DependencyCycleError``, through a
    different door. Live incident 2026-08-07 (bead ac-694377f3):

        agentco tasks create --blocked-by 'ac-3f4dd6f2\\nac-b7063b2b'

    A shell mangled two ids into ONE argument carrying an embedded newline.
    That string was stored verbatim as a blocker, and since no task will ever
    have that id, the bead was blocked forever. Nothing was stale, nothing
    errored, ``ghost_blockers()`` had to be run by hand to see it — the work
    simply never happened. Exactly the failure mode this codebase least
    tolerates.

    Two checks, both at the WRITE boundary where the offending value is still
    in hand and can be named:

    * **format** — every id must match ``ac-<8 lowercase hex>``. Catches the
      newline corruption above, stray whitespace, and shell/JSON garbage.
    * **existence** — the referenced task must be parseable and present in the
      store at write time.

    Deliberately NOT enforced at the read boundary. ``ghost_blockers()``,
    ``_cycle_path()`` and ``tempo`` stay tolerant of dangling ids, because
    after this guard the only ways one reaches disk are a hand edit, a bad
    merge, or data written before the guard existed — and a read that crashes
    on legacy data hides the whole queue instead of degrading one bead.

    Subclasses ``ValueError`` so existing callers that catch ValueError at a
    write boundary keep working.
    """


class HumanLineageError(Exception):
    """Raised when an update would strip a human executor off a task.

    A task whose ``assigned_to`` carries a ``human:`` lineage can never be
    flipped human→agent (or human→None) by the planner, an auto-approve
    whitelist, or any routine code path — that transition is only legal on an
    explicit human-approved path that passes ``allow_human_reassign=True``.
    This is the code-level enforcement of the delegation-layer invariant, not
    a convention.
    """


class VerifyContractError(ValueError):
    """Raised when ``metadata.verify`` is present but malformed.

    Rejected at the WRITE boundary (create/update), exactly like TaskResult:
    a verify payload that cannot be understood is worse than none at all,
    because the bead LOOKS gated while the gate would silently no-op. The
    read side stays tolerant only in the sense that a legacy bead carrying no
    payload keeps legacy semantics.
    """


class DivergenceContractError(ValueError):
    """Raised when ``metadata.divergence`` is present but is not good/bad.

    The tag's whole value is that it ROUTES: `good` (the plan was wrong) feeds
    PRIME and the plan templates; `bad` (execution took a shortcut) feeds RCA.
    A free-text tag routes nowhere and quietly becomes a comment, so the value
    set is closed and enforced at the write boundary like every other contract
    in this module.
    """


class ContextRefsContractError(ValueError):
    """Raised when ``metadata.context_refs`` is present but malformed.

    Shape is enforced, EXISTENCE is not: a plan legitimately pins files a
    builder has not written yet, and refusing those would make the field
    unusable at exactly the moment it is most useful (plan time). A path that
    does not resolve is warned about at the write boundary and noted in the
    prompt at injection time — loud both times, fatal neither.
    """


class SopContractError(ValueError):
    """Raised when ``metadata.sop`` is present but malformed.

    Same write-boundary posture as every other contract in this module, for the
    same reason: a bead carrying a half-formed SOP block LOOKS delegation-ready
    in `tasks show` and in any future lister, while handing its executor an
    empty promise. The block's whole job is to survive first contact with
    someone who was not in the room when the work was scoped; a field that is
    present-but-blank fails that job while advertising success.
    """


class VerifyGateError(Exception):
    """Raised when a completion cannot be gated at all.

    v1 ships deterministic and human classes. A `judged` payload is refused
    here rather than passed through: silently treating an un-runnable gate as
    a pass is precisely the self-grading the gate exists to prevent.
    """


class LeaseError(Exception):
    """Raised when a lease precondition fails inside the store's locked region.

    Two distinct situations, deliberately one exception type because both mean
    the same thing to a caller — *the bead is not yours*:

    1. **Lost CAS** — ``claim()`` found the bead non-PENDING, or already held
       under an unexpired lease by someone else. ``claim()`` catches this and
       returns None: in a compare-and-set protocol a lost race is the protocol
       WORKING, not a failure, and a drain loop must simply move on.
    2. **Stale fence** — ``report_result()`` was handed a ``lease_attempt``
       that is not the bead's current one. This one propagates. A worker whose
       lease expired and was reaped, executed anyway, and came back to report
       is writing a result derived from an execution the hub already gave up
       on; accepting it would silently overwrite whatever the successor did.
       Rejected loudly, at the layer where the mismatch is detectable.
    """


class CapabilityError(LeaseError):
    """Raised when a claimant's manifest does not cover a bead's ``requires``.

    A subclass of ``LeaseError`` because it is the same sentence — *the bead is
    not yours* — but it is deliberately the one lease failure that PROPAGATES
    out of ``claim()`` instead of becoming a ``None``.

    The distinction is retryability. A lost CAS is a race, and racing is the
    protocol working: the bead is somebody's right now, will be free later, and
    a drain loop should shrug and move on. A capability miss is a MISROUTE —
    this worker can never satisfy this bead, no matter how many times it asks.
    Returning ``None`` for both would file a permanent configuration error under
    "normal contention", where it would be retried forever and read as nothing.

    This is also the gate that makes credential containment real: the
    write-scoped ADO PAT lives only on the MacBook, so a bead carrying
    ``requires: ["ado-write"]`` must be *unable* to take a lease on the hub —
    not merely unlikely to be scheduled there.
    """


# The verify payload's accepted classes. `deterministic` re-runs a command,
# `human` parks the bead for approval, `judged` is declared-but-unimplemented
# in v1 (see VerifyGateError).
VERIFY_CLASSES = frozenset({"deterministic", "judged", "human"})

# Keys a verify payload may carry. Unknown keys are rejected loudly: a typo'd
# `timeout` (instead of `timeout_s`) that silently defaults is a gate that
# behaves differently from what the author wrote down.
#
# `checks` is the STAGED form of `check` — an ordered list run stop-at-first-
# failure (lint → types → unit → integration). One opaque `&&`-chained command
# passes or fails as a unit and tells you nothing about WHERE it broke; staged
# checks name the stage, which is the difference between "verify failed" and
# "verify failed at stage 2 of 4: mypy". Exactly one of the two must be present.
VERIFY_KEYS = frozenset(
    {"class", "check", "checks", "cwd", "timeout_s", "rubric", "judge_route"}
)

DEFAULT_VERIFY_TIMEOUT_S = 120

# How long a claim is believed by default. Two hours is chosen against the
# execution budget, not plucked: `budget.timeout` defaults to executor's
# DEFAULT_TIMEOUT (600s) and the longest budget anything actually runs on is
# 1800s (RCA beads, feeds ingest), so 2h is 4x the longest legitimate
# in-process execution. A bead still IN_PROGRESS past it is stuck, not slow —
# which is exactly the condition reaping exists to recover.
DEFAULT_LEASE_TTL_S = 2 * 60 * 60


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None.

    Naive input is read as UTC — every writer in this module stamps UTC, and
    the alternative (raising, or comparing naive to aware) turns a cosmetic
    difference into a crash inside a locked region.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# The closed value set for ``metadata.divergence``. Two values because there are
# exactly two useful destinations: a wrong PLAN feeds PRIME/plan updates, a
# shortcut in EXECUTION feeds RCA.
DIVERGENCE_VALUES = frozenset({"good", "bad"})

# How much of a check's combined output is retained on the bead. Enough to
# read the failing assertion, bounded so a runaway check cannot bloat the JSONL.
VERIFY_OUTPUT_TAIL_CHARS = 2000

# Executors that run real subprocess LLM work and report their own completion
# (ac-fcc95ca5): "claude", "zai", "forge" are the three `_execute_*_task`
# branches in orchestrator._execute_cycle_task that hand a bead to a model and
# trust its account of what happened, matching `claim(task_id, agent, ...)`
# for each of those branches — `claim()` writes `agent` into `assigned_agent`,
# so this is checked against the field a real dispatch actually leaves behind.
# "planner" is deliberately excluded even though it is also a real subprocess:
# a planner bead's DONE claims a DECISION was written, not that task work
# happened, and there is no "evidence" shape to gate a decision on the way a
# claimed fix or claimed check can be gated. The system's own deterministic
# dispatch branches (verify_child, retro) claim under their OWN name
# ("verify_child", "retro"), never one of these three, so they fall out of
# this set without a separate exemption — keep it that way if either branch
# ever changes what it claims as.
SELF_REPORTING_EXECUTORS = frozenset({"claude", "zai", "forge"})


def validate_verify(payload: object) -> dict:
    """Validate + normalize a ``metadata.verify`` payload. Raise on garbage.

    Shape: ``{"class": "deterministic"|"judged"|"human", "check": str, ...}``
    or, for a staged gate, ``{"class": ..., "checks": [str, ...], ...}`` — plus
    optional ``cwd``/``timeout_s`` (which apply to EVERY stage) and
    `rubric`/`judge_route`, reserved for the judged class. Returns a normalized
    copy — callers store THAT, so what is on disk is what the gate will run.

    ``check`` and ``checks`` are mutually exclusive. Carrying both is refused
    rather than resolved by precedence: a bead that looks gated on four stages
    while only one runs is the self-grading this contract exists to stop.
    """
    if not isinstance(payload, dict):
        raise VerifyContractError(
            f"metadata.verify must be a JSON object, got {type(payload).__name__}"
        )
    unknown = set(payload) - VERIFY_KEYS
    if unknown:
        raise VerifyContractError(
            f"metadata.verify has unknown key(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(VERIFY_KEYS))})"
        )
    cls = payload.get("class")
    if cls not in VERIFY_CLASSES:
        raise VerifyContractError(
            f"metadata.verify['class'] must be one of "
            f"{sorted(VERIFY_CLASSES)}, got {cls!r}"
        )
    has_check = "check" in payload
    has_checks = "checks" in payload
    if has_check and has_checks:
        raise VerifyContractError(
            "metadata.verify carries BOTH 'check' and 'checks' — they are "
            "mutually exclusive. Use 'check' for one command, 'checks' for an "
            "ordered list of stages."
        )
    if not has_check and not has_checks:
        raise VerifyContractError(
            "metadata.verify needs either 'check' (one command) or 'checks' "
            "(an ordered list of stage commands)"
        )
    if has_checks:
        stages = payload["checks"]
        if isinstance(stages, str):
            # A bare string is iterable, so it would otherwise validate
            # character-by-character into a gate of one-letter commands.
            raise VerifyContractError(
                f"metadata.verify['checks'] must be a LIST of command strings, "
                f"got the string {stages!r} — pass [{stages!r}] for one stage, "
                f"or use 'check'."
            )
        if not isinstance(stages, (list, tuple)):
            raise VerifyContractError(
                f"metadata.verify['checks'] must be a list of command strings, "
                f"got {type(stages).__name__}"
            )
        if not stages:
            raise VerifyContractError(
                "metadata.verify['checks'] is empty — a gate with no stages "
                "passes everything, which is not a gate"
            )
        for i, stage in enumerate(stages):
            if not isinstance(stage, str) or not stage.strip():
                raise VerifyContractError(
                    f"metadata.verify['checks'][{i}] must be a non-empty "
                    f"string (the command to run), got {stage!r}"
                )
        return _finish_verify_normalization(
            {"class": cls, "checks": [str(s) for s in stages]}, payload
        )
    check = payload.get("check")
    if not isinstance(check, str) or not check.strip():
        raise VerifyContractError(
            "metadata.verify['check'] must be a non-empty string "
            "(the command to re-run, or — for the human class — what the "
            "approver must confirm)"
        )
    normalized: dict = {"class": cls, "check": check}
    return _finish_verify_normalization(normalized, payload)


def _finish_verify_normalization(normalized: dict, payload: dict) -> dict:
    """Copy the optional, shape-shared keys onto an already-typed payload."""
    cwd = payload.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.strip():
            raise VerifyContractError(
                f"metadata.verify['cwd'] must be a non-empty string, got {cwd!r}"
            )
        normalized["cwd"] = cwd
    timeout_s = payload.get("timeout_s")
    if timeout_s is not None:
        # bool is an int subclass; a True timeout is a bug, not a duration.
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
            raise VerifyContractError(
                f"metadata.verify['timeout_s'] must be a positive integer "
                f"(seconds), got {timeout_s!r}"
            )
        normalized["timeout_s"] = timeout_s
    for optional in ("rubric", "judge_route"):
        if optional in payload:
            normalized[optional] = payload[optional]
    return normalized


def verify_check_text(spec: dict | None) -> str:
    """One readable string for a verify payload, single-command or staged.

    The one place that knows how to flatten `check`/`checks`, so a renderer
    never has to branch and can never show a staged gate as if it had no check.
    """
    if not spec:
        return ""
    if spec.get("checks"):
        return " → ".join(str(s) for s in spec["checks"])
    return str(spec.get("check") or "")


def validate_context_refs(payload: object, base_dir: Path | str | None = None) -> list[dict]:
    """Validate + normalize ``metadata.context_refs``. Raise on a bad SHAPE only.

    Shape: ``[{"path": str, "why": str}, ...]`` — the two or three files this
    ONE bead actually needs, pinned when the plan was written, sitting below
    the node-wide PRIME.md. `why` is required for the same reason PRIME demands
    extractive pointers: a path with no reason is a file the executor has to
    open to find out whether it mattered.

    Existence is NOT required. A plan legitimately pins files a builder is
    about to create, and rejecting those would make the field useless at plan
    time — the moment it earns its keep. A path that does not resolve gets a
    stderr warning here and a "not found" note in the injected prompt, so it is
    loud twice and fatal never.
    """
    if isinstance(payload, dict) or not isinstance(payload, (list, tuple)):
        raise ContextRefsContractError(
            f"metadata.context_refs must be a list of "
            f"{{'path': ..., 'why': ...}} objects, got "
            f"{type(payload).__name__}"
        )
    normalized: list[dict] = []
    for i, ref in enumerate(payload):
        if not isinstance(ref, dict):
            raise ContextRefsContractError(
                f"metadata.context_refs[{i}] must be an object with 'path' and "
                f"'why', got {type(ref).__name__}"
            )
        unknown = set(ref) - {"path", "why"}
        if unknown:
            raise ContextRefsContractError(
                f"metadata.context_refs[{i}] has unknown key(s): "
                f"{', '.join(sorted(unknown))} (allowed: path, why)"
            )
        path = ref.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ContextRefsContractError(
                f"metadata.context_refs[{i}]['path'] must be a non-empty "
                f"string (repo-relative or absolute), got {path!r}"
            )
        why = ref.get("why")
        if not isinstance(why, str) or not why.strip():
            raise ContextRefsContractError(
                f"metadata.context_refs[{i}]['why'] must be a non-empty string "
                f"— say what the executor needs from {path!r}. A pointer with "
                f"no reason costs a file read to discover it did not matter."
            )
        normalized.append({"path": path, "why": why})
        if not resolve_context_ref(path, base_dir).exists():
            print(
                f"[beads] WARNING: context_refs[{i}] path {path!r} does not "
                f"exist yet (looked in {base_dir or Path.cwd()}) — stored "
                f"anyway; plans legitimately pin files that do not exist yet",
                file=sys.stderr,
            )
    return normalized


def resolve_context_ref(path: str, base_dir: Path | str | None = None) -> Path:
    """Where a context ref points: absolute as written, relative to the node."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return Path(base_dir or Path.cwd()) / candidate


def validate_divergence(value: object) -> str:
    """Validate a ``metadata.divergence`` tag. Raise on anything else."""
    # isinstance first: an unhashable value (a list, say) would raise TypeError
    # out of the set membership test instead of the contract error that names
    # the problem.
    if not isinstance(value, str) or value not in DIVERGENCE_VALUES:
        raise DivergenceContractError(
            f"metadata.divergence must be one of {sorted(DIVERGENCE_VALUES)}, "
            f"got {value!r} — 'good' means the PLAN was wrong (feed PRIME/plan "
            f"updates), 'bad' means EXECUTION took a shortcut (feed RCA). Omit "
            f"the key when neither has been judged."
        )
    return str(value)


# The SOP block's fields. `steps` is deliberately NOT among them: the bead's
# own `description` is the steps, and a second prose field competing for the
# same content produces beads where half the instructions live in one place and
# half in the other. The five kept fields are the ones `description` does not
# already answer — why it exists, what fires it, what it needs, what done means,
# and where people fall over.
#
# `definition_of_done` is the ISC in bead form. `common_mistakes` is the field
# with no existing home anywhere in LifeOS, and the reason this block exists:
# every other field describes the work, that one describes the FAILURE MODES,
# which is what a handoff actually breaks on.
SOP_KEYS = frozenset(
    {"purpose", "trigger", "inputs", "definition_of_done", "common_mistakes"}
)

# The four free-prose fields, kept in the order a reader wants them.
SOP_TEXT_KEYS = ("purpose", "trigger", "inputs", "definition_of_done")

# Why a cap at all, and why 3: the cap IS the discipline. An unbounded mistakes
# list becomes a wiki page, and a wiki page is not read at the moment of
# handoff. Forcing a ranking down to three is what keeps the field to the things
# that actually bite. The source note specifies three; this enforces it rather
# than trusting the author to stop.
MAX_SOP_MISTAKES = 3


def validate_sop(payload: object) -> dict:
    """Validate + normalize a ``metadata.sop`` block. Raise on garbage.

    Shape: ``{"purpose": str, "trigger": str, "inputs": str,
    "definition_of_done": str, "common_mistakes": [str, ...]}`` — every key
    optional, but the block as a whole may not be empty and no present key may
    be blank. Returns a normalized copy, so what is on disk is what a renderer
    will show.

    Partial blocks are legal on purpose. An SOP is filled in as the work is
    understood, and demanding all five fields at create time would mean the
    block gets skipped entirely at exactly the moment it is cheapest to start.
    What is refused is the *dishonest* shape: an empty block, or a field that is
    present and says nothing.
    """
    if not isinstance(payload, dict):
        raise SopContractError(
            f"metadata.sop must be a JSON object with any of "
            f"{sorted(SOP_KEYS)}, got {type(payload).__name__}"
        )
    unknown = set(payload) - SOP_KEYS
    if unknown:
        raise SopContractError(
            f"metadata.sop has unknown key(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(SOP_KEYS))}). Note there is no "
            f"'steps' field — the bead's description carries the steps."
        )
    if not payload:
        raise SopContractError(
            "metadata.sop is empty — a bead carrying an empty SOP block reads "
            "as delegation-ready and hands its executor nothing. Fill at least "
            "one field or omit the key."
        )

    out: dict = {}
    for key in SOP_TEXT_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise SopContractError(
                f"metadata.sop['{key}'] must be a non-empty string, got "
                f"{value!r} — a present-but-blank field claims this bead "
                f"answers a question it does not answer. Omit the key instead."
            )
        out[key] = value

    if "common_mistakes" in payload:
        mistakes = payload["common_mistakes"]
        if isinstance(mistakes, (str, dict)) or not isinstance(
            mistakes, (list, tuple)
        ):
            raise SopContractError(
                f"metadata.sop['common_mistakes'] must be a LIST of strings, "
                f"got {type(mistakes).__name__} — one entry per mistake, so "
                f"each can be read (and dropped) on its own."
            )
        if not mistakes:
            raise SopContractError(
                "metadata.sop['common_mistakes'] is empty — an empty list is "
                "the claim that this work has no known failure modes, which is "
                "the one claim a handoff should never make silently. Omit the "
                "key if none are known yet."
            )
        if len(mistakes) > MAX_SOP_MISTAKES:
            raise SopContractError(
                f"metadata.sop['common_mistakes'] carries {len(mistakes)} "
                f"entries; the cap is {MAX_SOP_MISTAKES}. The cap is the "
                f"discipline — an unbounded list is a wiki page, and a wiki "
                f"page is not read at handoff time. Keep the three that "
                f"actually bite."
            )
        normalized: list[str] = []
        for i, mistake in enumerate(mistakes):
            if not isinstance(mistake, str) or not mistake.strip():
                raise SopContractError(
                    f"metadata.sop['common_mistakes'][{i}] must be a non-empty "
                    f"string, got {mistake!r}"
                )
            normalized.append(mistake)
        out["common_mistakes"] = normalized

    return out


_CONTRACTED_METADATA_KEYS = ("verify", "divergence", "context_refs", "sop")


def _validated_metadata(
    metadata: dict | None, base_dir: Path | str | None = None
) -> dict | None:
    """Return metadata with its contracted payloads validated + normalized."""
    if not metadata:
        return metadata
    if not any(key in metadata for key in _CONTRACTED_METADATA_KEYS):
        return metadata
    out = dict(metadata)
    if "verify" in out:
        out["verify"] = validate_verify(out["verify"])
    if "divergence" in out:
        out["divergence"] = validate_divergence(out["divergence"])
    if "context_refs" in out:
        out["context_refs"] = validate_context_refs(out["context_refs"], base_dir)
    if "sop" in out:
        out["sop"] = validate_sop(out["sop"])
    return out


def validate_task_id(field: str, value: object) -> str:
    """Return ``value`` if it is a well-formed bead id, else raise.

    Nothing is normalized — no ``.strip()``, no lowercasing. A caller that
    passed ``'ac-3f4dd6f2\\nac-b7063b2b'`` did not mean one id with a newline in
    it, and quietly repairing the argument would hide the shell bug that
    produced it. Reject, name the value, let the caller fix the caller.
    """
    if not isinstance(value, str) or not TASK_ID_PATTERN.fullmatch(value):
        raise TaskReferenceError(
            f"{field} {value!r} is not a valid task id — expected the form "
            f"ac-<8 lowercase hex chars> (e.g. ac-3f4dd6f2). Refusing: a "
            f"malformed reference is stored verbatim, matches no task, and "
            f"blocks the bead forever with no diagnostics."
        )
    return value


def normalize_blockers(blocked_by: object) -> list[str]:
    """Validate every blocker id and return the deduplicated list.

    Duplicates are NORMALIZED AWAY, not rejected. ``blocked_by`` is a SET of
    preconditions — listing one twice expresses nothing a single entry does
    not, and ``ready()`` already treats it that way. It is also what the
    orchestrator's parent-gating merge already does
    (``list(dict.fromkeys(...))``), so this makes one convention out of two.
    Order is preserved: it is what `tasks show` and `me` display, and
    reordering a person's list is a change they did not ask for.
    """
    if blocked_by is None:
        return []
    if isinstance(blocked_by, str):
        # A bare string is iterable, so this would otherwise validate
        # character-by-character and produce a baffling error.
        raise TaskReferenceError(
            f"blocked_by must be a list of task ids, got the string "
            f"{blocked_by!r} — pass ['{blocked_by}'] if you meant one id."
        )
    return list(dict.fromkeys(validate_task_id("blocked_by", b) for b in blocked_by))


# One vocabulary, one spelling. A capability token is lowercase, starts with a
# letter or digit, and may contain `-`, `_` and `.` — enough for `ado-write`,
# `frontsteps-code`, `gpu.cuda`. The pattern exists because this is a MATCHING
# gate: `ADO-Write` in a manifest and `ado-write` on a bead do not match, and
# the failure would be a bead that silently never runs anywhere. Catching the
# spelling where it is typed is far cheaper than debugging it two machines away.
CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def normalize_capabilities(
    values: object,
    *,
    field_name: str = "capabilities",
    strict: bool = True,
    where: str = "",
) -> list[str]:
    """Validate and deduplicate a capability token list. **Fails closed.**

    Used for both halves of the routing gate — a node's declared
    ``capabilities`` and a bead's ``requires`` — because they are one
    vocabulary and must be normalized identically. A token that survives one
    side but not the other is a gate that silently never matches.

    ``strict`` picks the failure posture, and the two callers genuinely want
    different ones:

    * ``strict=True`` (bead ``requires``, registry rows) **raises**. This is
      authoring time. A typo'd requirement that is quietly dropped produces a
      bead which looks lane-restricted and is not — the exact hole the manifest
      exists to close, wearing a green checkmark.
    * ``strict=False`` (node ``config.yaml`` at load) **warns and drops**. A
      fat-fingered manifest must never take a daemon down mid-cycle. Dropping is
      the safe direction *because* it removes capability: the node ends up able
      to do less than it claimed, never more. A malformed block therefore grants
      NOTHING rather than being coerced into something plausible — coercing
      ``capabilities: ado-write`` (a bare string) into a list of characters, or
      into ``["ado-write"]``, would hand a node a lane it never wrote down.

    Order is preserved and duplicates are normalized away, matching
    ``normalize_blockers``: this is a SET of tokens, and reordering a person's
    list is a change they did not ask for.
    """
    suffix = f" in {where}" if where else ""

    def reject(problem: str, fix: str) -> list[str] | None:
        if strict:
            raise ValueError(f"{field_name}: {problem}{suffix}. {fix}")
        print(f"[config] WARNING: {field_name} {problem}{suffix} — {fix}")
        return None

    if values is None:
        return []
    if isinstance(values, str):
        # A bare string is iterable, so this would otherwise be validated
        # character by character and produce a baffling result.
        if reject(
            f"must be a list of tokens, got the string {values!r}",
            f"write it as a list: [{values}]. Treating it as EMPTY (no capabilities).",
        ) is None:
            return []
    if not isinstance(values, (list, tuple)):
        if reject(
            f"must be a list of tokens, got {type(values).__name__}",
            "Treating it as EMPTY (no capabilities).",
        ) is None:
            return []

    tokens: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            if reject(
                f"entry {raw!r} is not a string",
                "Dropping it.",
            ) is None:
                continue
        token = raw.strip()
        if not token:
            if reject("contains an empty token", "Dropping it.") is None:
                continue
        if not CAPABILITY_RE.match(token):
            if reject(
                f"token {token!r} is not a valid capability name",
                "Use lowercase letters, digits, '-', '_' or '.' "
                "(e.g. 'ado-write'). Dropping it.",
            ) is None:
                continue
        tokens.append(token)
    return list(dict.fromkeys(tokens))


@dataclass
class TaskResult:
    """Structured output written by agents to task.result.

    Agents write this as JSON to task.result via `agentco tasks complete --result`.
    Callers (Telegram handler, group-chat flow) parse it to get the deliverable
    and the pre-formed reply without touching raw stdout.
    """

    status: Literal["complete", "partial", "needs_input", "failed"]
    output: str
    reply: Optional[str] = None          # pre-formed Telegram/group-chat reply
    obsidian_note: Optional[str] = None  # Obsidian note path if result was saved there
    continuation_hint: Optional[str] = None  # what remains when status == "partial"
    error: Optional[str] = None          # error description when status == "failed"

    # The only accepted status values. A result whose status is outside this set
    # is not a TaskResult — from_str rejects it so bad data can't reach the store.
    VALID_STATUSES = frozenset({"complete", "partial", "needs_input", "failed"})

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None}, ensure_ascii=False)

    @classmethod
    def from_str(cls, s: str) -> "TaskResult":
        """Parse a JSON string into a TaskResult, validating the status value.

        Raises on garbage: json.JSONDecodeError (not JSON), TypeError (not an
        object / missing required field), KeyError, or ValueError (unknown
        `status`). Callers that want tolerance catch these and fall back.
        """
        d = json.loads(s)
        if not isinstance(d, dict):
            raise TypeError(f"TaskResult must be a JSON object, got {type(d).__name__}")
        known = {f.name for f in fields(cls)}
        tr = cls(**{k: v for k, v in d.items() if k in known})
        if tr.status not in cls.VALID_STATUSES:
            raise ValueError(
                f"invalid TaskResult status {tr.status!r} "
                f"(expected one of {sorted(cls.VALID_STATUSES)})"
            )
        return tr

    @classmethod
    def from_task(cls, task: "Task") -> "Optional[TaskResult]":
        """Parse task.result as TaskResult, or None if missing/not structured.

        Read-boundary tolerance: any parse/validation failure yields None so a
        malformed stored result never crashes a read. The write boundary
        (`tasks complete --result`) is where bad data is rejected loudly.
        """
        if not task.result:
            return None
        try:
            return cls.from_str(task.result)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return None


class TaskStatus(str, Enum):
    PENDING = "pending"
    PENDING_APPROVAL = "pending_approval"  # agent-created subtasks waiting for human approval
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    # --- verify gate (PPEV) -------------------------------------------------
    # A gated bead NEVER passes through DONE on its way to approval. The old
    # idiom (DONE + a needs_input result) marked work done *before* anyone had
    # confirmed it, which both lied to `ready()` — releasing downstream beads
    # against a blocker that was only provisionally finished — and aged the
    # gate silently off the human queue after 14 days.
    AWAITING_VERIFY = "awaiting_verify"  # human-class gate: work claimed, approval pending
    VERIFY_FAILED = "verify_failed"  # the gate ran and said no. Retryable, never done.


class TaskPriority(int, Enum):
    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3


@dataclass
class Task:
    """A single task in the queue."""

    id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    assigned_agent: Optional[str] = None
    # Human (or otherwise non-agent) executor discriminator. None → the agent
    # path is unchanged. "human:<name>" → a person owns this task; it is
    # excluded from ready() and never dispatched to a model. Additive field:
    # old JSONL lines without it parse fine (defaults to None).
    assigned_to: Optional[str] = None
    source: Optional[str] = None  # gmail, logs, feedback, etc.
    source_id: Optional[str] = None  # original event ID
    parent_id: Optional[str] = None
    blocked_by: list[str] = field(default_factory=list)
    # --- temporal layer (tempo.py) -----------------------------------------
    # Additive and optional: a bead with none of these behaves exactly as it
    # did before they existed, and old JSONL lines parse fine (defaults None).
    # Two shapes of work, deliberately distinct fields rather than one:
    #   starts_at  → PIN. A fixed commitment; the clock is the constraint and
    #                the only useful behaviour is a reminder. Never re-ranked.
    #   due_at     → DUE. A deliverable with a flexible *when*. This is what
    #                the backward pass ranks.
    # A bead may legitimately carry neither. It must never carry both.
    starts_at: Optional[str] = None  # ISO-8601, fixed-time commitment
    due_at: Optional[str] = None  # ISO-8601, deadline
    estimate_hours: Optional[float] = None  # most-likely effort (PERT 'm')
    estimate_optimistic: Optional[float] = None  # PERT 'o' — optional
    estimate_pessimistic: Optional[float] = None  # PERT 'p' — optional
    actual_hours: Optional[float] = None  # measured, feeds estimate calibration
    # --- cross-machine lease layer (ac-9cae7593) ----------------------------
    # Additive and optional: a bead that has never been leased carries the
    # defaults below and behaves exactly as it did before leases existed, and
    # old JSONL lines parse fine. Three fields, each earning its place:
    #   leased_by       → WHO holds it. None = free.
    #   lease_attempt   → the FENCE. Monotonic per bead, bumped on every
    #                     successful claim, never reset — not even by expiry
    #                     reaping. It is the permanent record of how many times
    #                     this bead has been handed out, which is why a reaped
    #                     lease does not fail the bead: the count already says
    #                     what happened. A result may only be reported against
    #                     the CURRENT attempt (see report_result).
    #   lease_expires_at → WHEN the hub stops believing the holder.
    #
    # ISO-8601 STRING, not a datetime, deliberately: `to_json` is `asdict` +
    # `json.dumps`, which cannot encode a datetime, and every other temporal
    # field on this dataclass (starts_at, due_at, created_at, updated_at) is
    # already an ISO string. A datetime here would need a custom encoder on the
    # write side and a parse on the read side — a second serialization
    # convention inside one record, for no gain. `lease_active_at()` does the
    # parsing where the comparison actually happens.
    leased_by: Optional[str] = None
    lease_attempt: int = 0
    lease_expires_at: Optional[str] = None  # ISO-8601 UTC
    # --- lane routing (ac-39d4dbc8) -----------------------------------------
    # What this bead NEEDS from whatever executes it, matched against the
    # claimant node's declared `capabilities`. Additive and optional in the same
    # way as the lease fields above: an empty list means "any worker", which is
    # every bead written before manifests existed.
    #
    # This pairs with `assigned_agent`, it does not duplicate it. `assigned_agent`
    # says WHO should do the work (a routing preference, freely reassignable);
    # `requires` says what the executing MACHINE must be able to do (a physical
    # constraint — the write-scoped ADO PAT exists on exactly one host). Only the
    # second one is a safety property, which is why only the second one is
    # enforced at claim time.
    requires: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    result: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Task":
        """Deserialize from JSON string.

        Unknown fields are ignored (forward compatibility). Unknown status or
        priority values raise ValueError — callers quarantine such lines
        rather than letting one bad record poison the whole queue.
        """
        d = json.loads(line)
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        d["status"] = TaskStatus(d["status"])
        d["priority"] = TaskPriority(d["priority"])
        return cls(**d)

    def lease_active_at(self, now: datetime) -> bool:
        """True when this bead is held under a lease that has NOT yet expired.

        An unparseable or missing ``lease_expires_at`` counts as NOT active.
        That direction is chosen on purpose: the failure mode of treating a
        corrupt expiry as "still leased" is a bead nobody can ever claim again,
        which is unrecoverable without hand-editing the store. The failure mode
        of treating it as free is a bead that gets re-handed-out — and the
        fence (``lease_attempt``) already makes that safe, because the stale
        holder's report is rejected.
        """
        if not self.leased_by or not self.lease_expires_at:
            return False
        expires = _parse_iso(self.lease_expires_at)
        if expires is None:
            return False
        return now < expires


def _content_key(kind: str, text: str) -> str:
    """Stable identity for a SYNTHESISED thread row — one derived from the
    bead's own fields rather than stored as its own record.

    Keyed by content because that is exactly what "the same row" means for
    these: a lifecycle row whose text is unchanged IS the row a client already
    has, no matter that ``system_events`` re-stamps its ``at`` with
    ``task.updated_at`` on every write. When the text does change
    (``in_progress`` → ``done``, or a new error on a failed bead) that is a
    genuinely new thing to say, and a new key is the right answer.
    """
    digest = hashlib.blake2s(text.encode("utf-8"), digest_size=4).hexdigest()
    return f"{kind}:{digest}"


def system_events(task: "Task") -> list[dict[str, str]]:
    """Chronological system rows interleaved into the chat (GitHub-issue style).

    Single source of truth for "what does this bead's lifecycle look like as
    a thread entry" — both the UI thread view (webui) and the chat-answering
    prompt (orchestrator.answer_pending_chat) read the bead through this same
    lens, so an agent answering a question sees exactly what the human sees on
    screen, never a narrower slice of it.

    Every row carries a ``key`` — see ``full_thread``. These rows are the
    reason it has to exist: the status row is stamped ``task.updated_at``, so
    it MOVES within the sorted thread on every single write to the bead.
    """
    events = [
        {
            "type": "system",
            "text": f"Created · {task.source or 'manual'}",
            "at": task.created_at,
            "key": _content_key("sys", f"Created · {task.source or 'manual'}"),
        }
    ]
    text = None
    if task.status == TaskStatus.PENDING_APPROVAL:
        text = "Waiting for your approval"
    elif task.status == TaskStatus.IN_PROGRESS:
        text = "Agent working"
    elif task.status == TaskStatus.FAILED:
        err = str(task.metadata.get("error", ""))[:140]
        text = f"Failed — {err or 'no error recorded'}"
    elif task.status == TaskStatus.DONE:
        text = "Done"
    if text is not None:
        events.append(
            {"type": "system", "text": text, "at": task.updated_at, "key": _content_key("sys", text)}
        )
    return events


def full_thread(task: "Task") -> list[dict]:
    """The whole conversation on a bead: lifecycle + human/agent chat + the
    stored result, chronological.

    Single source of truth for "the thread" so the UI and an answering agent
    can never see divergent views of the same bead.

    Every entry carries a stable ``key``. Consumers that track "what have I
    already delivered" MUST watermark on that key and never on a position in
    this list — the list is SORTED, and the lifecycle row from
    ``system_events`` is stamped with ``task.updated_at``, so it re-sorts to
    the tail on every write to the bead. A positional watermark over a
    re-sorted list silently swallows every new entry that sorts beneath a row
    that moved (the SSE regression of 2026-08-18: on any non-pending bead the
    stream emitted nothing but the status row, and chat replies never arrived
    live). Identity is stable under re-sorting; an index is not.

    Chat entries key off their index in ``metadata["chat"]``, which is
    append-only in both writers (webui.api_chat, orchestrator._append_chat_reply)
    and so is a stable identity — and, unlike content, it survives the same
    message being sent twice.
    """
    thread = system_events(task) + [
        {**m, "key": f"chat:{i}"} for i, m in enumerate(task.metadata.get("chat", []))
    ]
    tr = TaskResult.from_task(task)
    if tr is not None:
        text = tr.error or tr.output or ""
        if tr.continuation_hint:
            text += f"\n\n→ {tr.continuation_hint}"
        if text.strip():
            body = f"[{tr.status}] {text.strip()[:2000]}"
            thread.append(
                {
                    "type": "agent",
                    "text": body,
                    "at": task.updated_at,
                    "key": _content_key("result", body),
                }
            )
    thread.sort(key=lambda m: m.get("at", ""))
    return thread


def _cycle_path(
    task_id: str, proposed_blockers: list[str], tasks: list[Task]
) -> list[str]:
    """Return the offending chain if `task_id` gaining these blockers cycles.

    Edge direction: ``X.blocked_by = [B]`` means B must finish before X, i.e.
    an edge B → X. So X's *successors* are the tasks that list X in their own
    ``blocked_by``. Adding B → X closes a loop precisely when X can already
    reach B by walking successors.

    Returns the path (blocker → … → task_id) for the error message, or [] when
    the edge is safe. Naming the actual chain matters: "there is a cycle" is
    not actionable, "A → B → C → A" is.

    Dangling blocker ids are ignored rather than raised — consistent with
    ``ghost_blockers``, which reports them separately. A reference to a task
    that does not exist cannot participate in a cycle.

    Note ``create()`` needs no such guard: a brand-new id has no successors, so
    a newly created task is cycle-safe by construction. ``update()`` is the
    only path that can close a loop.
    """
    by_id = {t.id: t for t in tasks}
    successors: dict[str, list[str]] = {}
    for t in tasks:
        for blocker in t.blocked_by:
            successors.setdefault(blocker, []).append(t.id)

    for blocker in proposed_blockers:
        if blocker == task_id:
            return [task_id, task_id]  # self-block
        if blocker not in by_id:
            continue  # ghost — reported by ghost_blockers(), not a cycle
        # Walk forward from task_id; if we reach `blocker`, the new edge closes.
        parent: dict[str, str] = {}
        queue = [task_id]
        seen = {task_id}
        while queue:
            cur = queue.pop(0)
            if cur == blocker:
                chain = [blocker]
                node = blocker
                while node in parent:
                    node = parent[node]
                    chain.append(node)
                chain.reverse()
                return chain + [task_id]
            for succ in successors.get(cur, []):
                if succ not in seen:
                    seen.add(succ)
                    parent[succ] = cur
                    queue.append(succ)
    return []


class Beads:
    """Git-backed task queue."""

    def __init__(self, path: Path | str = "tasks.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        # Raw lines that failed to parse — preserved verbatim on rewrite so a
        # hand-edited or corrupt record is never silently dropped.
        self._quarantined: list[str] = []
        self._warned_lines: set[str] = set()

    @contextmanager
    def _locked(self):
        """Exclusive advisory lock guarding read-modify-write cycles."""
        with open(self._lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_all(self) -> list[Task]:
        """Read all tasks from file.

        Unparseable lines are quarantined with a loud warning instead of
        poisoning the whole queue; they survive rewrites verbatim.
        """
        tasks = []
        self._quarantined = []
        with open(self.path) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    tasks.append(Task.from_json(line))
                except (ValueError, KeyError, TypeError) as e:
                    self._quarantined.append(line)
                    if line not in self._warned_lines:
                        self._warned_lines.add(line)
                        print(
                            f"[beads] WARNING: quarantined unparseable task at "
                            f"{self.path}:{lineno} ({e}) — preserved, not executed",
                            file=sys.stderr,
                        )
        return tasks

    def _write_all(self, tasks: list[Task]) -> None:
        """Atomically write all tasks, preserving quarantined raw lines.

        Temp file in the SAME directory + flush + fsync + os.replace so a crash
        mid-write can never truncate or corrupt the live queue: the rename is
        atomic, leaving either the whole old file or the whole new one on disk.
        """
        directory = self.path.parent
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tasks-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for task in tasks:
                    f.write(task.to_json() + "\n")
                for raw in self._quarantined:
                    f.write(raw + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def create(
        self,
        title: str,
        description: str,
        priority: TaskPriority = TaskPriority.MEDIUM,
        assigned_agent: str | None = None,
        assigned_to: str | None = None,
        status: TaskStatus = TaskStatus.PENDING,
        source: str | None = None,
        source_id: str | None = None,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
        metadata: dict | None = None,
        starts_at: str | None = None,
        due_at: str | None = None,
        estimate_hours: float | None = None,
        estimate_optimistic: float | None = None,
        estimate_pessimistic: float | None = None,
        requires: list[str] | None = None,
        natural_key: str | None = None,
        natural_key_kind: str | None = None,
        natural_key_subject: str | None = None,
        natural_key_period: str | None = None,
    ) -> Task:
        """Create a new task.

        Raises DepthLimitError if parent_id would put the new task beyond
        MAX_SUBTASK_DEPTH — the hard floor that stops runaway decomposition,
        enforced here so no agent or future signature can bypass it.

        Raises TaskReferenceError if parent_id or any blocked_by entry is
        malformed or names a task the store does not contain. A brand-new id
        cannot be self-referenced and has no successors, so create() needs no
        cycle guard — but it very much needs this one (see the class docstring
        for the incident).

        ``assigned_to`` and ``status`` are additive, optional keywords so the
        FIRST JSONL append can already carry the final shape of the task — no
        create→update window where a human task is momentarily a plain agent
        PENDING task, and no PENDING→PENDING_APPROVAL flip where a proposal
        subtask is briefly dispatchable. Both default to the prior behaviour.

        ``requires`` is validated STRICTLY here (``ValueError`` on a malformed
        token). Authoring time is the only cheap moment to catch a typo in a
        lane requirement: a silently dropped one produces a bead that looks
        lane-restricted and is not, and a misspelled one produces a bead no
        manifest anywhere will ever match.

        **Natural-key uniqueness.** If a natural key is derivable — explicitly
        via ``natural_key``, as generated work via
        ``natural_key_kind``/``_subject``/``_period``, or (the common case,
        requiring no caller change at all) from ``source`` + ``source_id`` —
        then a bead already carrying that key makes this call a LOUD, NAMED
        NO-OP: nothing is appended, a ``DUPLICATE-SUPPRESSED`` line goes to
        stderr, and the EXISTING task is returned with
        ``.natural_key_conflict`` set True.

        Returning the existing bead rather than raising is the deliberate
        choice. Every ingest path in this repo already wanted exactly this and
        each hand-rolled its own version of it (``exists_source``, a
        ``beads.list()`` scan, a quote-hash map, an open-bead guard); raising
        would force all of them to grow a try/except to express the behaviour
        they already have. Silence was never an option — a suppressed duplicate
        that nobody announces is indistinguishable from a create that worked,
        which is how 24 of 24 costed runs in one node-day turned out to be
        duplicate RCA beads.

        A bead with no derivable key is unconstrained, exactly as before.
        ``NaturalKeyError`` (a ``ValueError``) is raised at authoring time for
        a malformed or partially-supplied key — before any I/O, because a bad
        key is bad regardless of what the store contains.
        """
        # Before any I/O: a key is a pure function of its inputs, and a
        # malformed one is wrong no matter what is on disk.
        key = derive_natural_key(
            explicit=natural_key,
            source=source,
            source_id=source_id,
            kind=natural_key_kind,
            subject=natural_key_subject,
            period=natural_key_period,
        )
        metadata = _validated_metadata(metadata, self.path.parent)
        if key is not None:
            metadata = dict(metadata or {})
            metadata[NATURAL_KEY_FIELD] = key
        # Format first, before any I/O: it needs no store, and a malformed id is
        # wrong regardless of what the store contains.
        if parent_id is not None:
            parent_id = validate_task_id("parent_id", parent_id)
        blockers = normalize_blockers(blocked_by)
        requirements = normalize_capabilities(requires, field_name="requires")

        task = Task(
            id=f"ac-{uuid.uuid4().hex[:8]}",
            title=title,
            description=description,
            status=status,
            priority=priority,
            assigned_agent=assigned_agent,
            assigned_to=assigned_to,
            source=source,
            source_id=source_id,
            parent_id=parent_id,
            blocked_by=blockers,
            metadata=metadata or {},
            starts_at=starts_at,
            due_at=due_at,
            estimate_hours=estimate_hours,
            estimate_optimistic=estimate_optimistic,
            estimate_pessimistic=estimate_pessimistic,
            requires=requirements,
        )
        with self._locked():
            # ONE read serves the uniqueness index, the existence check, and the
            # depth walk, and it happens under the lock so a concurrent writer
            # can neither delete the parent between validating it and appending
            # the child, nor append a second bead carrying the same natural key
            # between the lookup and the write. The lock IS the unique index.
            if key is not None or parent_id is not None or blockers:
                existing_tasks = self._read_all()
                if key is not None:
                    duplicate = next(
                        (t for t in existing_tasks if natural_key_of(t) == key), None
                    )
                    if duplicate is not None:
                        print(
                            f"[beads] DUPLICATE-SUPPRESSED natural_key={key!r} — "
                            f"{title!r} was NOT created; {duplicate.id} already "
                            f"holds this key. create() returned the existing bead.",
                            file=sys.stderr,
                        )
                        # Runtime-only marker (not a dataclass field, so it is
                        # never serialised): lets a caller distinguish "I filed
                        # this" from "this was already filed" without parsing
                        # stderr or re-reading the store.
                        duplicate.natural_key_conflict = True
                        return duplicate
                by_id = {t.id: t for t in existing_tasks}
                self._assert_references_exist(
                    by_id, parent_id=parent_id, blocked_by=blockers
                )
                if parent_id is not None:
                    child_depth = self._depth_in(parent_id, by_id) + 1
                    if child_depth > MAX_SUBTASK_DEPTH:
                        raise DepthLimitError(
                            f"task under {parent_id} would be depth {child_depth} "
                            f"(max {MAX_SUBTASK_DEPTH}) — refusing to deepen the tree"
                        )
            with open(self.path, "a") as f:
                f.write(task.to_json() + "\n")
        return task

    def _assert_references_exist(
        self,
        by_id: dict[str, "Task"],
        *,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
        task_id: str | None = None,
    ) -> None:
        """Raise TaskReferenceError for any reference not present in ``by_id``.

        ``by_id`` is the caller's ALREADY-READ view of the store, so this adds
        no I/O of its own. It comes from ``_read_all()``, which means a
        QUARANTINED line does not count as existing: an unparseable record is
        never dispatched, never completes, and so could never release whatever
        it blocks. Pointing at one is the same permanent deadlock as pointing
        at nothing, and it is refused with the same message.
        """
        if parent_id is not None:
            if parent_id == task_id:
                raise TaskReferenceError(
                    f"refusing to set parent_id on {task_id}: a task cannot be "
                    f"its own parent. That closes a tree cycle no walk can "
                    f"resolve and no human can read."
                )
            if parent_id not in by_id:
                raise TaskReferenceError(
                    f"parent_id {parent_id!r} does not exist in {self.path} — "
                    f"refusing to create an orphan whose depth cap and goal "
                    f"lineage cannot be computed."
                )
        for blocker in blocked_by or []:
            if blocker not in by_id:
                raise TaskReferenceError(
                    f"blocked_by references {blocker!r}, which does not exist "
                    f"in {self.path} — refusing: nothing will ever complete "
                    f"that id, so the bead would stay blocked forever. Check "
                    f"the id, or drop the blocker."
                )

    def _depth_of(self, task_id: str) -> int:
        """Generation of a task counting from its root (root = 0).

        Walks the parent_id chain. Cycle-safe and tolerant of orphaned
        parents (a missing parent simply ends the walk).
        """
        return self._depth_in(task_id, {t.id: t for t in self._read_all()})

    @staticmethod
    def _depth_in(task_id: str, tasks: dict[str, "Task"]) -> int:
        """``_depth_of`` against an already-read store view (no I/O)."""
        depth = 0
        seen: set[str] = set()
        cur: str | None = task_id
        while cur is not None and cur in tasks and cur not in seen:
            seen.add(cur)
            parent = tasks[cur].parent_id
            if parent is None:
                break
            depth += 1
            cur = parent
        return depth

    def get(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        for task in self._read_all():
            if task.id == task_id:
                return task
        return None

    def _run_one_stage(self, command: str, cwd: str, timeout: int) -> dict:
        """Run ONE command fresh and report `passed`/`exit_code`/`output_tail`."""
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "exit_code": None,
                "timed_out": True,
                "output_tail": f"check timed out after {timeout}s",
            }
        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            # An unrunnable check is a FAILED gate, never a pass: the claim is
            # unproven either way, and the reason is recorded verbatim.
            return {
                "passed": False,
                "exit_code": None,
                "timed_out": False,
                "output_tail": f"could not run check in {cwd}: {e}",
            }
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "timed_out": False,
            "output_tail": combined[-VERIFY_OUTPUT_TAIL_CHARS:],
        }

    def _run_deterministic_check(self, spec: dict) -> dict:
        """Re-run the check(s) FRESH and report what happened.

        Fresh is the whole point: the executor's own claim of success is not
        evidence, so the completing process runs the command itself. The
        default cwd is the store's directory — a node's beads live next to the
        node's config, so a check written as `uv run pytest -q` means "in this
        node", which is what an author writing the payload intends.

        A staged payload (``checks``) runs its stages IN ORDER and STOPS at the
        first failure — later stages are never run, which is the point: a
        type-check failure makes the integration suite's verdict meaningless,
        and running it anyway costs minutes and reports the wrong cause. The
        failing stage is named in ``failed_stage`` (0-based ``index``, its
        command, its output tail); ``stages_run``/``stages_total`` say how far
        the gate got. ``cwd``/``timeout_s`` apply per stage — the timeout is a
        per-command budget, not a budget for the whole ladder.

        The single-``check`` record shape is unchanged, so every existing
        reader (``tasks show``, the fallback result text, Chronicle) keeps
        working; ``check`` is also populated for a staged run (the failing
        command, or the arrow-joined ladder on a pass) for the same reason.
        """
        cwd = spec.get("cwd") or str(self.path.parent)
        timeout = int(spec.get("timeout_s", DEFAULT_VERIFY_TIMEOUT_S))
        record: dict = {
            "class": "deterministic",
            "cwd": cwd,
            "timeout_s": timeout,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        if "checks" not in spec:
            record["check"] = spec["check"]
            record.update(self._run_one_stage(spec["check"], cwd, timeout))
            return record

        stages: list[str] = list(spec["checks"])
        record["checks"] = stages
        record["stages_total"] = len(stages)
        for index, command in enumerate(stages):
            outcome = self._run_one_stage(command, cwd, timeout)
            if not outcome["passed"]:
                record.update(outcome)
                record["check"] = command
                record["stages_run"] = index + 1
                record["failed_stage"] = {
                    "index": index,
                    "command": command,
                    "exit_code": outcome["exit_code"],
                    "timed_out": outcome["timed_out"],
                    "output_tail": outcome["output_tail"],
                }
                return record
        record.update(
            passed=True,
            exit_code=0,
            timed_out=False,
            output_tail=f"all {len(stages)} stage(s) passed",
            check=" → ".join(stages),
            stages_run=len(stages),
        )
        return record

    def _classify_specless_done(self, task: Task, kwargs: dict, metadata: dict) -> str:
        """Classify a DONE with no ``metadata.verify`` payload (ac-fcc95ca5).

        Before this, EVERY spec-less DONE was legacy-untouched — the
        executor's word was the only evidence, and nothing on the record said
        so. Returns one of:

        * ``"none"``    — legacy, untouched. Not a self-reporting executor
          (``SELF_REPORTING_EXECUTORS`` — real subprocess LLM work, checked
          against the ``assigned_agent`` value the update is ABOUT to carry,
          since ``claim()`` writes it in the same ``update()`` call that can
          later carry the completion, and against ``metadata.executor`` for
          the rarer case a caller drives dispatch through that field
          instead), or is the system's OWN routine work — a recurring-spawned
          sample (``source == "recurring"``) or anything stamping
          ``metadata.spawned_by``, the same discriminator ``_sampler_family``
          in recurring.py already uses to know a bead is a periodic sample
          rather than filed work.
        * ``"await"``   — the live incident's EXACT shape:
          ``metadata.task_class == "agent"`` (the CLI's `--task-class agent`)
          on a self-reporting executor with no spec. Measured over the 30
          days before this fix, that shape closed 4 beads total — small
          enough that routing every one of them to AWAITING_VERIFY for human
          review is the right severity, not overkill.
        * ``"unverified"`` — every OTHER self-reporting-executor DONE with no
          spec (RCA analyses, manual/ritual triggers, feed enrichment — ~514
          in the same 30 days). Routing THAT volume to a human queue would
          flood it worse than the bug this closes, so the DONE stands, but
          tagged — see ``_mark_unverified_completion``.
        """
        assigned_agent = kwargs.get("assigned_agent", task.assigned_agent)
        executor = metadata.get("executor")
        if (
            assigned_agent not in SELF_REPORTING_EXECUTORS
            and executor not in SELF_REPORTING_EXECUTORS
        ):
            return "none"
        source = kwargs.get("source", task.source)
        if source == "recurring" or metadata.get("spawned_by"):
            return "none"
        if source == "rca":
            # RCA beads are code-generated with their own review flow, not
            # self-reported open-ended agent work — never hard-block them to
            # AWAITING_VERIFY regardless of task_class. Still tagged
            # "unverified" (not "none") so they stay spot-checkable: the
            # exemption is from the queue flood, not from the record saying
            # plainly that nobody checked.
            return "unverified"
        if metadata.get("task_class") == "agent":
            return "await"
        return "unverified"

    def _mark_unverified_completion(self, task: Task, kwargs: dict) -> dict:
        """Stamp a self-reported DONE as unverified — never silently DONE.

        The lighter of the two ac-fcc95ca5 responses (see
        ``_classify_specless_done``): the DONE stands, but the record now
        says plainly that nobody checked:
        ``metadata.verify_result = {"class": "unverified", "passed": None}``,
        the exact shape ``tasks show``/Chronicle already render for a real
        gate, so a bead that only an agent's word backs is grep-able and
        spot-checkable — it can never again look identical to a checked one.
        A bead that wants a real gate declares ``metadata.verify`` and gets
        the deterministic/human treatment in ``_apply_verify_gate`` instead.
        """
        metadata = dict(kwargs.get("metadata", task.metadata) or {})
        metadata["verify_result"] = {
            "class": "unverified",
            "passed": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "note": (
                "no metadata.verify spec — this DONE is the self-reporting "
                "executor's claim only, never re-checked"
            ),
        }
        kwargs["metadata"] = metadata
        return kwargs

    def _route_unverified_agent_bead_to_awaiting(self, task: Task, kwargs: dict) -> dict:
        """Refuse a bare DONE claim on an explicit agent-class bead (ac-fcc95ca5).

        Live incident, 2026-08-18: four beads filed ``--task-class agent -a
        claude``; the hourly cycle marked three DONE with no evidence any of
        the claimed work happened — one spot-checked and proved false
        (ac-eb1d962a: the guard it claimed to add was not in standup.md). The
        store carried that DONE exactly like a real one.

        This is the harder of the two ac-fcc95ca5 responses: for THIS class
        specifically the bead does not reach DONE without a spec — it lands
        AWAITING_VERIFY, the same state a human-class verify gate produces,
        so a human decides. Never call this directly; it is reached only
        through ``_classify_specless_done`` returning ``"await"``.
        """
        metadata = dict(kwargs.get("metadata", task.metadata) or {})
        kwargs["status"] = TaskStatus.AWAITING_VERIFY
        metadata["verify_result"] = {
            "class": "unverified",
            "check": None,
            "passed": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "output_tail": (
                "agent-class bead (metadata.task_class == 'agent') claimed "
                "DONE with no metadata.verify spec — routed to "
                "AWAITING_VERIFY for human review instead (ac-fcc95ca5)"
            ),
        }
        kwargs["metadata"] = metadata
        return kwargs

    def _apply_verify_gate(self, task: Task, spec: dict, kwargs: dict) -> dict:
        """Rewrite a DONE-bound update according to the bead's verify payload.

        Returns the kwargs the write should actually apply. Deliberately runs
        BEFORE the file lock is taken: a deterministic check can be a full test
        suite, and holding the store's exclusive lock across it would stall
        every other reader/writer of the node — and deadlock any check that
        itself shells out to `agentco`.
        """
        cls = spec["class"]
        if cls == "judged":
            raise VerifyGateError(
                f"refusing to complete {task.id}: judged gates are not "
                f"implemented in v1 (only 'deterministic' and 'human'). Change "
                f"metadata.verify['class'], or approve it as a human gate."
            )

        metadata = dict(kwargs.get("metadata", task.metadata) or {})
        if cls == "human":
            # A human gate never transitions to DONE from here — only
            # `approve_verify` can, and only a person can call that.
            kwargs["status"] = TaskStatus.AWAITING_VERIFY
            metadata["verify_result"] = {
                "class": "human",
                "check": verify_check_text(spec),
                "passed": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "output_tail": "awaiting human approval",
            }
            kwargs["metadata"] = metadata
            return kwargs

        record = self._run_deterministic_check(spec)
        metadata["verify_result"] = record
        kwargs["metadata"] = metadata
        if record["passed"]:
            kwargs["status"] = TaskStatus.DONE
        else:
            kwargs["status"] = TaskStatus.VERIFY_FAILED
            # v1 files no fix bead: it fails loudly, visibly, and stops. The
            # caller's own result (usually TaskResult JSON) is left intact so
            # downstream parsers keep working; the evidence lives in
            # metadata.verify_result, which `tasks show` renders. Only when
            # nothing else would explain the state do we write the tail to
            # `result`.
            if not kwargs.get("result") and not task.result:
                kwargs["result"] = (
                    f"verify failed: {record['check']}\n{record['output_tail']}"
                )
        return kwargs

    def update(
        self,
        task_id: str,
        allow_human_reassign: bool = False,
        verify_gate: bool = True,
        precheck=None,
        **kwargs,
    ) -> Task | None:
        """Update a task.

        Verify gate: any update that would set status DONE on a bead carrying
        ``metadata.verify`` is routed through that payload first — deterministic
        checks re-run here, human gates divert to AWAITING_VERIFY, judged gates
        refuse. This is THE choke point on purpose: `complete()`, the CLI, the
        orchestrator and the worked agent itself all reach DONE through here,
        so no executor can grade its own work. A bead with NO verify payload
        keeps legacy semantics UNLESS it is owned by a ``SELF_REPORTING_EXECUTORS``
        name and is not the system's own routine work — see
        ``_classify_specless_done`` (ac-fcc95ca5): an explicit agent-class
        bead (``metadata.task_class == "agent"`` — the live incident's exact
        shape) is refused outright and lands AWAITING_VERIFY; every other
        such DONE still lands, but carries
        ``metadata.verify_result = {"class": "unverified", ...}`` instead of
        looking identical to a checked completion.

        ``verify_gate=False`` is a deliberate, code-reviewable bypass — a
        caller asserting a human or equally-trustworthy piece of code, not the
        completing executor, already vouches for the result. ``approve_verify``
        is the human-approval instance; ``supersede_resolved_rcas`` (recurring.py)
        is the other — it closes an RCA on store-visible evidence that its
        subject already resolved, not on the RCA's own say-so. Both are
        call-site opt-ins, never the caller's default.

        Referential integrity: a ``parent_id``/``blocked_by`` update is
        validated for FORMAT and EXISTENCE (``TaskReferenceError``) before the
        cycle walk. The proposed set is checked WHOLE, not just the newly added
        edges — an update that merely carries a pre-existing ghost blocker
        forward is still writing that ghost, and this is the boundary that
        names it. ``--clear-blocked-by`` is the documented way out for a bead
        that already carries one.

        Human-lineage invariant: a task currently assigned to a ``human:``
        executor may not have that ``assigned_to`` cleared (→ None) or flipped
        to an agent assignment (any non-``human:`` value) unless the caller
        passes ``allow_human_reassign=True``. Only explicit, human-approved CLI
        paths (decline) set that flag — the planner, auto-approve, and every
        routine path leave it False, so they physically cannot route a person's
        task to a model. Reassigning to a different human is always allowed.

        ``precheck`` is the compare-and-set hook (ac-9cae7593). It is a
        callable invoked with the CURRENT task, inside the lock, before any
        field is written. It may raise to abort the whole update with NO write,
        and it may return a dict of additional field updates COMPUTED from the
        current state (``lease_attempt + 1`` is the motivating case). This is
        why the lease protocol did not need a second write path: read, check,
        derive and write all happen under the one ``flock`` that already guards
        every other read-modify-write here, so two machines racing for the same
        bead serialize on the same lock a daemon and the CLI already serialize
        on. A precheck that returns None simply contributes no extra fields.
        """
        if "metadata" in kwargs:
            kwargs["metadata"] = _validated_metadata(
                kwargs["metadata"], self.path.parent
            )
        if verify_gate and kwargs.get("status") == TaskStatus.DONE:
            current = self.get(task_id)
            if current is not None:
                metadata_for_check = kwargs.get("metadata", current.metadata) or {}
                spec = metadata_for_check.get("verify")
                if spec is not None:
                    kwargs = self._apply_verify_gate(current, spec, dict(kwargs))
                else:
                    action = self._classify_specless_done(current, kwargs, metadata_for_check)
                    if action == "await":
                        kwargs = self._route_unverified_agent_bead_to_awaiting(current, dict(kwargs))
                    elif action == "unverified":
                        kwargs = self._mark_unverified_completion(current, dict(kwargs))
        with self._locked():
            tasks = self._read_all()
            for i, task in enumerate(tasks):
                if task.id == task_id:
                    previous_status = task.status
                    # CAS gate FIRST: a caller that has already lost the race
                    # for this bead must not go on to run cycle walks, verify
                    # bookkeeping or reference checks on a state it does not
                    # own. Raising here leaves the store byte-identical.
                    if precheck is not None:
                        derived = precheck(task)
                        if derived:
                            kwargs = {**kwargs, **derived}
                    if "assigned_to" in kwargs and not allow_human_reassign:
                        current = task.assigned_to
                        new_value = kwargs["assigned_to"]
                        if isinstance(current, str) and current.startswith("human:"):
                            keeps_human = (
                                isinstance(new_value, str)
                                and new_value.startswith("human:")
                            )
                            if not keeps_human:
                                raise HumanLineageError(
                                    f"refusing to change assigned_to on {task_id} "
                                    f"from {current!r} to {new_value!r}: a human-"
                                    f"assigned task can only be re-routed on an "
                                    f"explicit human-approved path "
                                    f"(allow_human_reassign=True)"
                                )
                    # Referential integrity BEFORE the cycle walk: a malformed
                    # or ghost id cannot participate in a cycle, so the cycle
                    # check would wave it through (it ignores dangling ids by
                    # design) and the store would gain a permanent blocker.
                    # Reuses `tasks`, already read above for the cycle walk.
                    if "parent_id" in kwargs and kwargs["parent_id"] is not None:
                        kwargs["parent_id"] = validate_task_id(
                            "parent_id", kwargs["parent_id"]
                        )
                    if "blocked_by" in kwargs:
                        kwargs["blocked_by"] = normalize_blockers(kwargs["blocked_by"])
                    if "parent_id" in kwargs or "blocked_by" in kwargs:
                        self._assert_references_exist(
                            {t.id: t for t in tasks},
                            parent_id=kwargs.get("parent_id"),
                            blocked_by=kwargs.get("blocked_by"),
                            task_id=task_id,
                        )
                    if "blocked_by" in kwargs:
                        # A self-block IS well-formed and DOES exist, so it
                        # survives the checks above and is caught here, where
                        # DependencyCycleError names it precisely.
                        chain = _cycle_path(
                            task_id, list(kwargs["blocked_by"] or []), tasks
                        )
                        if chain:
                            raise DependencyCycleError(
                                f"refusing to set blocked_by on {task_id}: it "
                                f"would close a dependency cycle "
                                f"({' → '.join(chain)}). Nothing in that loop "
                                f"could ever become ready. Break the chain by "
                                f"removing one of those edges."
                            )
                    for key, value in kwargs.items():
                        if hasattr(task, key):
                            setattr(task, key, value)
                    was_done = previous_status == TaskStatus.DONE
                    task.updated_at = datetime.now(timezone.utc).isoformat()
                    tasks[i] = task
                    self._write_all(tasks)
                    if task.status == TaskStatus.DONE and not was_done:
                        self._on_goal_closed(task, tasks)
                    return task
        return None

    def _on_goal_closed(self, task: Task, tasks: list[Task]) -> None:
        """Write the System Review when a GOAL bead reaches DONE. Best-effort.

        Hooked here rather than in ``complete()`` for the same reason the verify
        gate lives here: every route to DONE — ``complete()``, the CLI, the
        orchestrator, ``approve_verify`` — passes through this one write, so no
        caller can close a goal without leaving the artifact behind. It fires
        on the TRANSITION only, so re-saving an already-done goal does not
        rewrite its review with a later timestamp.

        Failure is swallowed by ``write_goal_review`` (a warning on stderr): a
        goal that did the work is done whether or not its paperwork rendered.
        """
        try:
            from .review import is_goal, write_goal_review

            if not is_goal(task):
                return
            write_goal_review(self.path, task, tasks)
        except Exception as e:  # noqa: BLE001 — never let paperwork fail the work
            print(
                f"[review] WARNING: system-review hook failed for {task.id} ({e}) "
                f"— the goal is still DONE",
                file=sys.stderr,
            )

    def list(
        self,
        status: TaskStatus | None = None,
        assigned_agent: str | None = None,
        priority: TaskPriority | None = None,
    ) -> list[Task]:
        """List tasks with optional filters."""
        tasks = self._read_all()
        if status:
            tasks = [t for t in tasks if t.status == status]
        if assigned_agent:
            tasks = [t for t in tasks if t.assigned_agent == assigned_agent]
        if priority is not None:
            tasks = [t for t in tasks if t.priority == priority]
        return sorted(tasks, key=lambda t: (t.priority.value, t.created_at))

    def ready(self, assigned_agent: str | None = None) -> list[Task]:
        """Get tasks ready to be worked on (pending, no blockers).

        Excludes pending_approval (a different status) AND any task with
        ``assigned_to`` set — a human-owned (or otherwise non-agent) task
        NEVER enters the cycle dispatch loop. "They wait, visible" means never
        dispatched, not dispatched-then-short-circuited: the exclusion here is
        the primary safety, mirroring the pending_approval status exclusion.

        A blocker resolves ONLY at DONE. AWAITING_VERIFY and VERIFY_FAILED are
        deliberately not done: a bead whose gate has not passed is unproven, so
        everything sequenced behind it stays blocked. This is why gated work
        never routes through DONE on the way to approval — a momentary DONE
        would release the entire downstream chain against work that might yet
        be rejected.

        Also excludes any bead under an UNEXPIRED lease (ac-9cae7593). A claim
        normally moves the bead to IN_PROGRESS, which drops it from this list
        anyway — but "normally" is not an invariant across two machines, and a
        PENDING bead that still carries a live lease (reverted by hand, or
        parked mid-protocol) is genuinely somebody's. Once the lease expires it
        reappears here with no further action: the ready set is the recovery
        path, which is why expiry does not need to fail the bead.
        """
        tasks = self.list(status=TaskStatus.PENDING, assigned_agent=assigned_agent)
        done_ids = {t.id for t in self.list(status=TaskStatus.DONE)}
        now = datetime.now(timezone.utc)
        return [
            t
            for t in tasks
            if t.assigned_to is None
            and not t.lease_active_at(now)
            and all(b in done_ids for b in t.blocked_by)
        ]

    def approve(self, task_id: str) -> Task | None:
        """Approve a pending_approval task — promotes it to pending so the daemon picks it up.

        Clears the ``requires_approval`` metadata flag on the way through: the
        flag is the approval GATE, so a task that has passed the gate must no
        longer carry it. This keeps the dispatch-time defense-in-depth guard
        (which quarantines any PENDING task still flagged requires_approval)
        from ever blocking a legitimately approved task.
        """
        task = self.get(task_id)
        if task is None:
            return None
        if task.status != TaskStatus.PENDING_APPROVAL:
            raise ValueError(f"task {task_id} is not pending_approval (status={task.status.value})")
        metadata = dict(task.metadata)
        metadata.pop("requires_approval", None)
        return self.update(task_id, status=TaskStatus.PENDING, metadata=metadata)

    def pending_approval(self) -> list[Task]:
        """Return all tasks awaiting human approval."""
        return self.list(status=TaskStatus.PENDING_APPROVAL)

    def ghost_blockers(self) -> list[tuple[Task, list[str]]]:
        """Return pending tasks whose blocked_by IDs don't exist anywhere in the queue."""
        all_tasks = self._read_all()
        all_ids = {t.id for t in all_tasks}
        result = []
        for task in all_tasks:
            if task.status != TaskStatus.PENDING:
                continue
            ghosts = [b for b in task.blocked_by if b not in all_ids]
            if ghosts:
                result.append((task, ghosts))
        return result

    def claim(
        self,
        task_id: str,
        agent: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_S,
        capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> Task | None:
        """Compare-and-set claim of a task for an agent (ac-9cae7593).

        Succeeds only if, at the moment the store lock is held, the bead is
        PENDING **and** free (never leased, or leased under an expiry now in
        the past). On success it takes the lease: ``leased_by`` = agent,
        ``lease_attempt`` bumped, ``lease_expires_at`` = now + ttl.

        Returns None when the claim does not stick — either the bead does not
        exist, or someone else got there first. None is not an error here; it
        is the answer a drain loop is asking for. The reason is still printed
        to stderr, because a claim that keeps losing is the first symptom of
        two workers wrongly sharing a lane, and that must not be invisible.

        Before this was a CAS it was a bare status write with no owner check
        and no expiry: two claimants both "succeeded", both executed, and the
        second completion silently overwrote the first. On one machine that
        was latent (the daemon was the only claimant); with the MacBook worker
        pulling the same store over SSH it is the default outcome.

        NOTE for in-process callers (the orchestrator's agents): they already
        only claim beads that came out of ``ready()``, i.e. PENDING and
        unleased, so the CAS is satisfied by construction and their behaviour
        is unchanged. Their leases simply expire harmlessly after the work is
        done, since a terminal status is never reaped.

        ``capabilities`` is the claimant's manifest — what the node calling this
        can actually do (ac-39d4dbc8). It is matched against the bead's
        ``requires``, and a bead asking for something the claimant does not
        declare raises ``CapabilityError`` rather than returning None: see that
        class for why a misroute must not be filed as contention.

        **The gate fails closed.** ``capabilities=None`` is not "skip the
        check", it is "this claimant declares nothing" — identical to ``[]``.
        Any other reading would make the safe default the insecure one, and
        every caller that had not been updated yet would be a hole. Beads with
        an empty ``requires`` (i.e. every bead that existed before manifests)
        are unaffected: nothing is required, so nothing can be missing.

        Enforcement lives HERE, in the store's locked region, and not in
        ``ready()``. Visibility is deliberately not the gate — the hub must keep
        seeing FrontSteps work in its portfolio, because a lane that disappears
        from view is a lane nobody notices has stopped.
        """
        now = datetime.now(timezone.utc)
        held = frozenset(
            normalize_capabilities(
                capabilities,
                field_name="capabilities",
                strict=False,
                where=f"claim of {task_id} by {agent!r}",
            )
        )

        def cas(task: Task) -> dict:
            # Capability BEFORE the CAS checks, on purpose: between a permanent
            # problem and a transient one, report the permanent one. "This node
            # can never run this bead" is more useful — and more actionable —
            # than "someone else holds it right now", which may also be true.
            missing = [r for r in task.requires if r not in held]
            if missing:
                raise CapabilityError(
                    f"cannot claim {task_id} for {agent!r}: bead requires "
                    f"{', '.join(task.requires)} but this node declares "
                    f"{', '.join(sorted(held)) or '(none)'} — missing "
                    f"{', '.join(missing)}. Either run it on a node whose "
                    f"config.yaml declares those capabilities, or fix the "
                    f"bead's requires. Retrying here cannot help."
                )
            if task.status != TaskStatus.PENDING:
                raise LeaseError(
                    f"cannot claim {task_id} for {agent!r}: status is "
                    f"{task.status.value}, not pending"
                )
            if task.lease_active_at(now):
                raise LeaseError(
                    f"cannot claim {task_id} for {agent!r}: held by "
                    f"{task.leased_by!r} until {task.lease_expires_at} "
                    f"(attempt {task.lease_attempt})"
                )
            expires = now + timedelta(seconds=ttl_seconds)
            return {
                "leased_by": agent,
                "lease_attempt": task.lease_attempt + 1,
                "lease_expires_at": expires.isoformat(),
            }

        try:
            return self.update(
                task_id,
                status=TaskStatus.IN_PROGRESS,
                assigned_agent=agent,
                precheck=cas,
            )
        except CapabilityError as e:
            # Louder than a lost race, and it propagates. The precheck raised
            # before any field was written, so the store is byte-identical.
            print(f"[beads] claim REFUSED (capability): {e}", file=sys.stderr)
            raise
        except LeaseError as e:
            print(f"[beads] claim refused: {e}", file=sys.stderr)
            return None

    def report_result(
        self,
        task_id: str,
        attempt: int,
        status: TaskStatus,
        result: str | None = None,
        idempotency_key: str | None = None,
    ) -> Task | None:
        """Apply a leased worker's outcome, fenced on ``attempt``.

        The fence is the whole point. A worker that lost its lease — went to
        sleep, hit a network partition, was reaped as stuck — may still be
        running, and may still come back with an answer. By then the bead may
        have been handed to someone else and finished. Accepting the late
        report would overwrite a real result with one derived from an execution
        the hub already abandoned. So a mismatch raises ``LeaseError`` and
        writes nothing; the caller sees a non-zero exit, not a silent no-op.

        ``idempotency_key`` makes the honest retry safe. SSH is a lossy
        transport: the worker can apply a result and lose the response, and
        must be able to send it again. A repeat of an already-recorded key
        returns the stored task unchanged rather than re-running the verify
        gate or re-firing the goal-closed hook.

        Completion routes through ``update()``, so the verify gate still owns
        the DONE transition — a remote worker cannot grade its own work any
        more than a local agent can. That means the returned status may be
        AWAITING_VERIFY or VERIFY_FAILED rather than the one asked for, and
        callers must read it rather than assume.

        The lease is released either way (``leased_by``/``lease_expires_at``
        cleared) while ``lease_attempt`` is kept: the count is the history of
        how many times this bead was handed out, and nothing should erase it.
        """
        if status not in (TaskStatus.DONE, TaskStatus.FAILED):
            raise ValueError(
                f"report_result only applies terminal outcomes "
                f"(done/failed), got {status.value}"
            )

        current = self.get(task_id)
        if current is None:
            return None
        if idempotency_key:
            prior = (current.metadata or {}).get("lease_report") or {}
            if prior.get("idempotency_key") == idempotency_key:
                return current

        def fence(task: Task) -> dict:
            if task.lease_attempt != attempt:
                raise LeaseError(
                    f"refusing result for {task_id}: reported against lease "
                    f"attempt {attempt}, but the bead is on attempt "
                    f"{task.lease_attempt} (holder {task.leased_by!r}). The "
                    f"lease this result came from is no longer current — the "
                    f"work was superseded, not lost."
                )
            return {}

        metadata = dict(current.metadata or {})
        metadata["lease_report"] = {
            "attempt": attempt,
            "reported_by": current.leased_by,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "status": status.value,
            "idempotency_key": idempotency_key,
        }
        return self.update(
            task_id,
            status=status,
            result=result,
            metadata=metadata,
            leased_by=None,
            lease_expires_at=None,
            precheck=fence,
        )

    def reap_expired_leases(self, now: datetime | None = None) -> list[Task]:
        """Return IN_PROGRESS beads whose lease has expired to the ready set.

        Expiry does NOT fail the bead. A failure is a claim about the work; an
        expired lease is a claim about the WORKER, and the two are different
        facts. Marking it FAILED would burn a retry, spawn an RCA for a
        non-event, and make a laptop closing its lid look like a broken task.
        The bead goes back to PENDING with its lease cleared and its
        ``lease_attempt`` intact — the attempt counter is the record that this
        happened, and it is what fences the old holder out if it ever returns.

        Only IN_PROGRESS is reaped. A terminal bead's lease is history, and a
        PENDING bead with a dead lease is already visible to ``ready()``.

        Each reap is printed: a bead that keeps expiring is a worker that keeps
        dying, and that must surface rather than look like ordinary churn.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            # Callers hand this in from the cycle, where `now` is aware, and
            # from tests, where it usually is not. A naive value read as UTC is
            # the same convention every writer here uses; the alternative is a
            # TypeError deep inside a comparison that aborts the whole
            # heartbeat over a timezone.
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        reaped: list[Task] = []
        for task in self._read_all():
            if task.status != TaskStatus.IN_PROGRESS:
                continue
            if not task.leased_by or task.lease_active_at(now):
                continue
            expires = _parse_iso(task.lease_expires_at)
            if expires is None:
                # No usable expiry: not a lease this pass can reason about.
                # Left alone deliberately rather than reclaimed on a guess.
                continue
            updated = self.update(
                task_id=task.id,
                status=TaskStatus.PENDING,
                leased_by=None,
                lease_expires_at=None,
            )
            if updated is not None:
                print(
                    f"[beads] lease expired on {task.id} "
                    f"(holder {task.leased_by!r}, attempt {task.lease_attempt}, "
                    f"expired {task.lease_expires_at}) — returned to pending",
                    file=sys.stderr,
                )
                reaped.append(updated)
        return reaped

    def complete(self, task_id: str, result: str | None = None) -> Task | None:
        """Mark a task as done — subject to the verify gate.

        For a bead with no ``metadata.verify`` this is exactly what it always
        was. For a gated bead the returned task may come back
        AWAITING_VERIFY (human class) or VERIFY_FAILED (the check said no) —
        callers that need certainty must read the returned status, not assume
        DONE. Re-calling complete() on a VERIFY_FAILED bead RE-RUNS the check:
        that is the retry path (the other is `tasks update`/`update` back to
        PENDING to have an agent take another swing first).
        """
        return self.update(task_id, status=TaskStatus.DONE, result=result)

    def approve_verify(self, task_id: str, approver: str) -> Task | None:
        """Human approval of an AWAITING_VERIFY bead → DONE.

        The ONE sanctioned bypass of the gate, because here the human IS the
        gate. Refuses any other status loudly: approving a bead that never
        reached the gate would launder an ungated completion through the one
        door that skips the check.
        """
        task = self.get(task_id)
        if task is None:
            return None
        if task.status != TaskStatus.AWAITING_VERIFY:
            raise ValueError(
                f"task {task_id} is not awaiting_verify "
                f"(status={task.status.value}) — nothing to approve"
            )
        metadata = dict(task.metadata)
        metadata["verify_approval"] = {
            "approver": approver,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        result = dict(metadata.get("verify_result") or {})
        if result:
            result["passed"] = True
            result["output_tail"] = f"approved by {approver}"
            metadata["verify_result"] = result
        return self.update(
            task_id,
            status=TaskStatus.DONE,
            metadata=metadata,
            verify_gate=False,
        )

    def reject_verify(
        self, task_id: str, approver: str, reason: str | None = None
    ) -> Task | None:
        """Human rejection of an AWAITING_VERIFY bead → VERIFY_FAILED."""
        task = self.get(task_id)
        if task is None:
            return None
        if task.status != TaskStatus.AWAITING_VERIFY:
            raise ValueError(
                f"task {task_id} is not awaiting_verify "
                f"(status={task.status.value}) — nothing to reject"
            )
        metadata = dict(task.metadata)
        metadata["verify_rejection"] = {
            "approver": approver,
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason or "",
        }
        result = dict(metadata.get("verify_result") or {})
        result.update(
            passed=False,
            output_tail=f"rejected by {approver}: {reason or 'no reason given'}",
        )
        metadata["verify_result"] = result
        return self.update(
            task_id,
            status=TaskStatus.VERIFY_FAILED,
            metadata=metadata,
            verify_gate=False,
        )

    def awaiting_verify(self) -> list[Task]:
        """Beads parked at a human verify gate."""
        return self.list(status=TaskStatus.AWAITING_VERIFY)

    def fail(self, task_id: str, result: str | None = None) -> Task | None:
        """Mark a task as failed."""
        return self.update(task_id, status=TaskStatus.FAILED, result=result)

    def exists_source(self, source: str, source_id: str) -> bool:
        """Check if a task from this source already exists.

        Superseded by the natural-key index in ``create()``, which enforces the
        same thing at the one place that can actually guarantee it (under the
        write lock, for every source, with no way to forget the call). Kept
        because callers that want to skip EXPENSIVE work before creating —
        ``agents.IntakeAgent`` runs an LM classification first — still need to
        ask the question early.
        """
        for task in self._read_all():
            if task.source == source and task.source_id == source_id:
                return True
        return False

    def find_by_natural_key(self, key: str) -> Task | None:
        """The bead holding ``key``, or None.

        Returns the FIRST holder in file order. More than one holder can only
        exist for keys stamped by the backfill onto beads that predate
        enforcement — ``create()`` cannot produce a second one.
        """
        for task in self._read_all():
            if natural_key_of(task) == key:
                return task
        return None

    def natural_key_collisions(self) -> dict[str, list[str]]:
        """Stored keys held by more than one bead → the ids holding them.

        A non-empty result is history, not a live defect: enforcement is at
        create time, so every entry here predates the key being stamped. It is
        the measurement that says how much duplication the per-source
        idempotency mechanisms were letting through.
        """
        by_key: dict[str, list[str]] = {}
        for task in self._read_all():
            key = natural_key_of(task)
            if key:
                by_key.setdefault(key, []).append(task.id)
        return {k: ids for k, ids in by_key.items() if len(ids) > 1}
