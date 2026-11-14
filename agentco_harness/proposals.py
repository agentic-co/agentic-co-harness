"""Procedure proposals: the harness half of `sop propose` (hub ADR 0015 s1).

A lesson that says "this procedure should change" leaves the harness as one
outbox line and nothing else. This module builds that line's payload and hands
it to `Outbox.push`, which owns every gate: the `knowledge` verb class (off
until the owner enables it) and the clean room, both before anything touches
disk.

THE PAYLOAD (what the hub's drainer reads)
------------------------------------------
Verb `sop_revise`. The hub's drainer (`agentco/outbox.py`, hub repo) calls
``registry.sop_revise(payload["sopId"], **payload["changes"], title=payload["title"])``
and the hub's strict wire (`agentco/app.py`) refuses any `changes` key outside
its `SOP_BODY_KEYS`, so those keys are validated here, before a line exists:

    sopId        required, the procedure being revised
    changes      required object, keys drawn from SOP_BODY_KEYS only
    title        optional
    provenance   {kind, ref}, REQUIRED by this module (ADR 0006 s3); the hub's
                 drainer does not read it, it rides in the durable line
    expected_version   optional, passed through untouched (ADR 0013 s4)

A revision is a proposal by construction: the hub drafts the next version and
the owner decides. This module has no way to say more than that, on purpose.

NOT DONE HERE
-------------
Wiring from `lesson_triage` or the orchestrator, and the `memory submit` half
of ADR 0015. `proposal_from_lesson` is the seam a caller will use.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from asop.chain import CHAIN_FIELDS
from asop.sop import ASOP_TEXT_FIELDS

from agentco_harness.outbox import Outbox, OutboxRefused, PushResult

#: The hub's `SOP_BODY_KEYS`, derived the same way it derives them (from the
#: installed contract), and pinned to a literal copy in the tests.
SOP_BODY_KEYS: tuple[str, ...] = (
    *ASOP_TEXT_FIELDS, "task_type", "inputs", "roles", "constraints", "steps", "proposals",
    *CHAIN_FIELDS,
)

CODE_PROPOSAL_INVALID = "proposal_invalid"

# The hub's memory provenance bounds (agentco/memory.py): kind 64, ref 2048.
MAX_KIND = 64
MAX_REF = 2048

# `lesson_triage.TARGET`, procedure half: asop:<id>[#<step>].
_ASOP_TARGET = re.compile(r"^asop:([a-z0-9][a-z0-9-]*)(#[0-9]+)?$")
# Which key of a lesson's `source` names where it came from, and what kind of
# pointer that makes it. First match wins.
_SOURCE_KINDS = (("bead", "bead"), ("bead_id", "bead"), ("run", "run"), ("run_id", "run"))


class ProposalInvalid(OutboxRefused, ValueError):
    """A proposal that is malformed. Refused before the outbox sees it."""

    def __init__(self, message: str):
        OutboxRefused.__init__(self, CODE_PROPOSAL_INVALID, message)


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProposalInvalid(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ProposalInvalid(f"{name} is {len(value)} chars, over the {limit} limit")
    return value


def _provenance(prov: Any) -> dict:
    if not isinstance(prov, Mapping) or set(prov) != {"kind", "ref"}:
        raise ProposalInvalid(
            "provenance is required and is exactly {kind, ref}: where this proposal came from, "
            "as a pointer to a bead or run (ADR 0006 s3)"
        )
    return {"kind": _text(prov["kind"], "provenance.kind", MAX_KIND),
            "ref": _text(prov["ref"], "provenance.ref", MAX_REF)}


def propose_revision(
    outbox: Outbox,
    *,
    sop_id: str,
    changes: Mapping[str, Any],
    provenance: Mapping[str, str],
    title: Optional[str] = None,
    expected_version: Any = None,
    agent_label: Optional[str] = None,
    attempt_id: Optional[str] = None,
) -> PushResult:
    """Push one procedure-change proposal. Raises `ProposalInvalid` for a
    malformed one, `OutboxRefused` if the knowledge class is off, and
    `CleanRoomRefused` / `CleanRoomUnavailable` from the clean room; in every
    refusal nothing is written."""
    _text(sop_id, "sop_id", MAX_REF)
    if not isinstance(changes, Mapping) or not changes:
        raise ProposalInvalid("changes must be a non-empty object")
    stray = sorted(set(changes) - set(SOP_BODY_KEYS))
    if stray:
        raise ProposalInvalid(
            f"changes has key(s) {stray} outside the hub's revision body {list(SOP_BODY_KEYS)}. "
            f"The title goes in `title`; status, version and author are not a proposer's to say."
        )
    payload: dict[str, Any] = {
        "sopId": sop_id,
        "changes": dict(changes),
        "provenance": _provenance(provenance),
    }
    if title is not None:
        payload["title"] = _text(title, "title", MAX_REF)
    if expected_version is not None:
        payload["expected_version"] = expected_version
    return outbox.push("sop_revise", payload, agent_label=agent_label, attempt_id=attempt_id)


def proposal_from_lesson(lesson: Mapping[str, Any]) -> dict:
    """Map a triaged lesson of type "proposal" to `propose_revision` arguments
    (minus the outbox). Shape of the lesson:

        {id, type: "proposal", target: "asop:<id>[#<step>]",
         changes: {...}, source: {bead|run: <id>}}

    The provenance pointer is the bead or run the lesson came from, plus the
    lesson id, so a reviewer can walk from the proposal back to its evidence."""
    if not isinstance(lesson, Mapping) or lesson.get("type") != "proposal":
        raise ProposalInvalid(f"only a lesson of type 'proposal' becomes a proposal, got {lesson.get('type') if isinstance(lesson, Mapping) else lesson!r}")
    lid = _text(lesson.get("id"), "lesson.id", MAX_REF)
    m = _ASOP_TARGET.match(str(lesson.get("target") or ""))
    if not m:
        raise ProposalInvalid(
            f"lesson.target {lesson.get('target')!r} must be 'asop:<id>[#<step>]'; a tool is not a procedure"
        )
    source = lesson.get("source")
    if not isinstance(source, Mapping):
        raise ProposalInvalid("lesson.source must name the bead or run it came from")
    for key, kind in _SOURCE_KINDS:
        if isinstance(source.get(key), str) and source[key].strip():
            ref = f"{source[key]}#{lid}"
            break
    else:
        raise ProposalInvalid(f"lesson.source names none of {[k for k, _ in _SOURCE_KINDS]}")
    changes = lesson.get("changes")
    if not isinstance(changes, Mapping) or not changes:
        raise ProposalInvalid("lesson.changes must be the non-empty change being proposed")
    return {
        "sop_id": m.group(1),
        "changes": dict(changes),
        "provenance": {"kind": kind, "ref": ref},
        "title": None,
    }
