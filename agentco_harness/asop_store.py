"""The local ASOP store, and filing a run from one — the runtime's half of
ASOP v3 (`packages/asop/ASOP.md`, ratified 2026-09-04).

A harness runs standalone against this store with the same contract a plane
speaks: the same records (`asop.ASOP`, `asop.Step`), the same verbs, the
same refusal codes, the same pin. Nothing here imports a plane.

Persistence is one JSONL line per ASOP version, append-only for `create`
and rewritten atomically for everything that changes a status — the same
lock-then-temp-then-`os.replace` discipline the recurring store uses, and
for the same reason: a crash mid-write must never truncate the file that
every pinned run resolves its procedure from. A line this build cannot
model is quarantined and reported, never dropped and never executed.

`run` is the verb that turns a procedure into work (ASOP.md §5.1). It files
a tree of beads: a parent pinned to `(asop_id, version)` carrying the run's
inputs and bindings, and one child per step pinned to `(asop_id, version,
step)`, `blocked_by` the beads of the steps it is `after`, carrying a COPY
of its step's text and its gate. A caller supplies inputs and bindings, and
nothing else — in particular no gate. Bindings are the harness's knowledge:
which agent, route or person fills each role, for this run. The store
refuses a run it cannot file honestly, with the contract's own codes.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from asop import ASOP, Step, SopStatus, validate_asop
from asop.errors import Refusal
from asop.revision import (
    RevisionPolicyError,
    check_asop_revision,
    protected_tags_from_env,
    require_human,
)
from asop.sop import SopContractError

from .beads import Beads, DepthLimitError, Task

HUMAN = "human"

#: What a caller may be, for the verbs the contract reserves to people.
AUTHOR_KINDS = ("human", "agent")


def _refuse(code: str, message: str, remediation: str) -> None:
    raise Refusal(code=code, message=message, remediation=remediation)


def _declared_kind(verb: str, kind: str) -> str:
    """The caller's declared trust domain, or a refusal naming the verb.

    Every policed verb funnels through here so an undeclared or misspelled
    kind is a refusal rather than a `ValueError` from inside the policy — the
    policy's own type check is a programming-error guard, not a caller-facing
    one, and a runtime that let `by_kind="huamn"` through to it would turn a
    typo into a 500 on a path that was refusing correctly.
    """
    if kind not in AUTHOR_KINDS:
        _refuse(
            "sop_refused",
            f"`{verb}` needs a declared caller: {kind!r} is not one of {AUTHOR_KINDS}",
            "Declare who is asking — 'human' or 'agent'. Who is human is the "
            "operator's declaration (AGENTCO_HUMANS), never the caller's word.",
        )
    return kind


def _policed(exc: RevisionPolicyError) -> None:
    """The contract's policy error, said in the contract's refusal vocabulary.

    `revision_policy:<rule>` is what ASOP.md §8.1 and §10 promise a caller,
    and it is what the plane returns for the same refusal from the same
    function — so a harness and a plane refusing the same revision say the
    same thing, and a client can branch on the code either way.
    """
    _refuse(
        f"revision_policy:{exc.rule}",
        str(exc),
        "Have a person do this, or change the revision so the rule does not "
        "fire. Who is human is declared by the operator (AGENTCO_HUMANS), "
        "never inferred — an undeclared registry polices everyone.",
    )


class AsopStore:
    """The versioned procedure store. One file, one lock, every version kept.

    `revise`, `activate` and `retire` are policed by the shared revision
    policy (`asop.revision`, ASOP.md §6.4) — the same four rules, from the
    same implementation, that the coordination plane enforces. A human passes
    all of them; an agent is bound by every one.
    """

    def __init__(self, path: Path | str, protected_tags: Optional[Sequence[str]] = None):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.quarantined: list[str] = []
        # The defaults (`money`, `irreversible`) plus whatever this
        # installation adds through `AGENTCO_PROTECTED_TAGS` — the same
        # variable, with the same name and the same add-only semantics, the
        # plane reads. Read once here, as the plane reads it once per
        # library, so a procedure cannot be protected halfway through a run.
        self.protected_tags = (
            frozenset(protected_tags) if protected_tags is not None
            else protected_tags_from_env()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # ------------------------------------------------------------ persistence

    @contextmanager
    def _locked(self):
        with open(self._lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_all(self) -> list[ASOP]:
        out: list[ASOP] = []
        quarantined: list[str] = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(ASOP.from_json(line))
                except (ValueError, TypeError, KeyError):
                    quarantined.append(line)
        self.quarantined = quarantined
        return out

    def _write_all(self, records: list[ASOP]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".asops-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for r in records:
                    f.write(r.to_json() + "\n")
                for line in self.quarantined:      # carried, never dropped
                    f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ reads

    def history(self, asop_id: str) -> list[ASOP]:
        return sorted((r for r in self._read_all() if r.asop_id == asop_id), key=lambda r: r.version)

    def get(self, asop_id: str, version: Optional[int] = None) -> Optional[ASOP]:
        """A version, or the active one. None when there is neither."""
        versions = self.history(asop_id)
        if version is not None:
            return next((r for r in versions if r.version == version), None)
        return next((r for r in versions if r.status is SopStatus.ACTIVE), None)

    def list(self) -> list[dict]:
        """One row per asop_id: the active version if any, and the status."""
        by_id: dict[str, list[ASOP]] = {}
        for r in self._read_all():
            by_id.setdefault(r.asop_id, []).append(r)
        rows = []
        for asop_id, versions in sorted(by_id.items()):
            versions.sort(key=lambda r: r.version)
            active = next((r for r in versions if r.status is SopStatus.ACTIVE), None)
            latest = versions[-1]
            rows.append({
                "asop_id": asop_id,
                "title": (active or latest).title,
                "task_type": (active or latest).task_type,
                "active_version": active.version if active else None,
                "latest_version": latest.version,
                "latest_status": latest.status.value,
            })
        return rows

    # ------------------------------------------------------------ authoring

    @staticmethod
    def _body(record: ASOP) -> dict:
        """The validatable body of a version — identity and store fields off."""
        return {
            "title": record.title,
            **({"task_type": record.task_type} if record.task_type else {}),
            **({"purpose": record.purpose} if record.purpose else {}),
            **({"trigger": record.trigger} if record.trigger else {}),
            "inputs": list(record.inputs),
            "roles": dict(record.roles),
            "constraints": list(record.constraints),
            "steps": [
                {k: v for k, v in s.__dict__.items() if v not in (None, [], {}) and k != "step"}
                for s in record.steps
            ],
            **({"proposals": list(record.proposals)} if record.proposals else {}),
        }

    def create(self, body: dict, *, author: str, author_kind: str, asop_id: Optional[str] = None) -> ASOP:
        """A new procedure at v1, DRAFT."""
        if author_kind not in AUTHOR_KINDS:
            _refuse("sop_refused", f"author_kind must be one of {AUTHOR_KINDS}", "Declare the author.")
        body = dict(body)
        explicit = body.pop("asop_id", None) or asop_id
        try:
            clean = validate_asop(body)
        except SopContractError as e:
            _refuse("sop_refused", str(e), "Fix the procedure and create it again.")
        with self._locked():
            records = self._read_all()
            new_id = explicit or f"asop-{secrets.token_hex(4)}"
            if any(r.asop_id == new_id for r in records):
                _refuse("sop_refused", f"ASOP {new_id!r} already exists", "Revise it instead of creating it twice.")
            record = ASOP(
                asop_id=new_id, version=1, status=SopStatus.DRAFT,
                author=author, author_kind=author_kind,
                steps=[Step(**s) for s in clean.pop("steps")], **clean,
            )
            with open(self.path, "a") as f:
                f.write(record.to_json() + "\n")
        return record

    def revise(self, asop_id: str, changes: dict, *, author: str, author_kind: str,
               from_version: Optional[int] = None) -> ASOP:
        """A new DRAFT version from an existing one plus a change set.

        Never edits a version. `steps`, if given, replace the sequence
        wholesale — a step list is one thing, not a set of patches.

        **The revision policy runs here, before anything is written** (§6.4).
        An agent is bound by all four rules and a refusal leaves the file
        byte-identical; a human passes unconditionally.
        """
        _declared_kind("sop_revise", author_kind)
        with self._locked():
            records = self._read_all()
            if self.quarantined and any(json.loads(q).get("asop_id") == asop_id for q in self.quarantined if q.startswith("{")):
                _refuse("sop_refused",
                        f"ASOP {asop_id!r} has a version this build cannot read; revising would reissue its number",
                        "Read the quarantined line with a build that can, or repair it, before revising.")
            versions = sorted((r for r in records if r.asop_id == asop_id), key=lambda r: r.version)
            if not versions:
                _refuse("sop_refused", f"no ASOP {asop_id!r}", "Create it first.")
            source = versions[-1] if from_version is None else next((r for r in versions if r.version == from_version), None)
            if source is None:
                _refuse("version_required", f"ASOP {asop_id!r} has no version {from_version}", "Name a version from `history`.")
            body = self._body(source)
            body.update(changes)
            try:
                clean = validate_asop(body)
            except SopContractError as e:
                _refuse("sop_refused", str(e), "Fix the revision and try again.")
            record = ASOP(
                asop_id=asop_id, version=versions[-1].version + 1, status=SopStatus.DRAFT,
                author=author, author_kind=author_kind,
                steps=[Step(**s) for s in clean.pop("steps")], **clean,
            )
            # Measured against the LATEST version, not against `from_version`.
            # Branching from an older version is how an agent would otherwise
            # undo a human without ever proposing the undo: revise from the
            # version before the human's edit and the diff never shows it.
            try:
                check_asop_revision(
                    history=versions, baseline=versions[-1], proposed=record,
                    reviser_kind=author_kind, protected_tags=self.protected_tags,
                    action="revise",
                )
            except RevisionPolicyError as e:
                _policed(e)
            with open(self.path, "a") as f:
                f.write(record.to_json() + "\n")
        return record

    def activate(self, asop_id: str, version: int, *, by_kind: str) -> ASOP:
        """DRAFT → ACTIVE; the previously active version → SUPERSEDED.

        Human, **or agent under policy** (ASOP.md §8.1). Activation is policed
        exactly as a revision is, or the policy has a door beside it: an agent
        forbidden from re-adding a step a human removed could otherwise
        re-activate the version from before the human removed it.
        """
        _declared_kind("sop_activate", by_kind)
        with self._locked():
            records = self._read_all()
            target = next((r for r in records if r.asop_id == asop_id and r.version == version), None)
            if target is None:
                _refuse("version_required", f"no ASOP {asop_id!r} version {version}", "Name a version from `history`.")
            if target.status is not SopStatus.DRAFT:
                _refuse("sop_refused", f"ASOP {asop_id!r} v{version} is {target.status.value}, not a draft",
                        "Only a draft activates. Revise to get a new draft.")
            versions = sorted((r for r in records if r.asop_id == asop_id), key=lambda r: r.version)
            active = next((r for r in versions if r.status is SopStatus.ACTIVE), None)
            try:
                check_asop_revision(
                    history=versions, baseline=active or versions[-1], proposed=target,
                    reviser_kind=by_kind, protected_tags=self.protected_tags,
                    action="activate",
                    # Nothing has ever been active, so there is no prior
                    # version to measure this one against and every
                    # differential rule is vacuous. The policy switches to an
                    # absolute check: an agent may not put a human step into
                    # service at all.
                    first_activation=active is None,
                )
            except RevisionPolicyError as e:
                _policed(e)
            out: list[ASOP] = []
            for r in records:
                if r.asop_id == asop_id and r.status is SopStatus.ACTIVE:
                    r = replace(r, status=SopStatus.SUPERSEDED, superseded_by=version)
                if r.asop_id == asop_id and r.version == version:
                    r = replace(r, status=SopStatus.ACTIVE)
                    target = r
                out.append(r)
            self._write_all(out)
        return target

    def retire(self, asop_id: str, *, by_kind: str) -> ASOP:
        """ACTIVE → RETIRED. No successor; runs in flight finish; none new.

        Human-only (rule 4, ASOP.md §8.1). Not a diff — there is no proposed
        version to compare — so it is refused to an agent whatever it would
        produce: an agent that learns a procedure is slow and withdraws it is
        doing exactly what the loop asks of it, and the procedure it withdrew
        may have been the one that keeps a payment run from sending money
        that cannot be recalled.
        """
        _declared_kind("sop_retire", by_kind)
        try:
            require_human(
                by_kind, "sop_retire",
                because=(
                    "Withdrawing a procedure ends it with no successor, and an agent "
                    "that finds a step expensive would be the party deciding nobody "
                    "follows it any more."
                ),
            )
        except RevisionPolicyError as e:
            _policed(e)
        with self._locked():
            records = self._read_all()
            active = next((r for r in records if r.asop_id == asop_id and r.status is SopStatus.ACTIVE), None)
            if active is None:
                _refuse("sop_refused", f"ASOP {asop_id!r} has no active version to retire", "Nothing to do.")
            out = [replace(r, status=SopStatus.RETIRED) if r is active else r for r in records]
            self._write_all(out)
        return replace(active, status=SopStatus.RETIRED)

    # ------------------------------------------------------------ running

    def run(self, asop_id: str, *, inputs: dict, bindings: dict, beads: Beads,
            version: Optional[int] = None, title: Optional[str] = None) -> Task:
        """File a run: a parent bead and one bead per step (ASOP.md §5.1).

        Returns the parent. Refuses, with the contract's codes, a version that
        is not active, a missing declared input, an unbound role, or bindings
        that put one actor in two roles a `distinct` constraint separates.
        """
        record = self.get(asop_id, version)
        if record is None:
            versions = self.history(asop_id)
            if not versions:
                _refuse("sop_refused", f"no ASOP {asop_id!r}", "Create and activate it first.")
            if version is not None:
                _refuse("version_required", f"no ASOP {asop_id!r} version {version}", "Name a version from `history`.")
            latest = versions[-1]
            _refuse("sop_refused",
                    f"ASOP {asop_id!r} has no active version — v{latest.version} is a {latest.status.value}",
                    "Activate a version first. Work filed from an unactivated procedure hands "
                    "somebody a half-written instruction with the authority of a published one.")
        if record.status is not SopStatus.ACTIVE:
            _refuse("sop_refused", f"ASOP {asop_id!r} v{record.version} is {record.status.value}, not active",
                    "Only an active version files runs.")
        self._check_inputs(record, inputs)
        self._check_bindings(record, bindings)

        parent = beads.create(
            title=title or f"{record.title} — run",
            description=record.purpose or record.title,
            metadata={"sop_ref": record.ref, "run": {"inputs": dict(inputs), "bindings": dict(bindings)}},
        )
        self._file_steps(record, parent, bindings, beads)
        return beads.get(parent.id)

    @staticmethod
    def _check_inputs(record: ASOP, inputs: dict) -> None:
        missing = [i["name"] for i in record.inputs if i["name"] not in inputs]
        if missing:
            _refuse("inputs_missing",
                    f"run of {record.asop_id!r} v{record.version} is missing input(s): {', '.join(missing)}",
                    "Supply every input the procedure declares, by name.")

    @staticmethod
    def _check_bindings(record: ASOP, bindings: dict) -> None:
        unbound = [r for r in record.roles if r not in bindings or not str(bindings[r]).strip()]
        if unbound:
            _refuse("role_unbound",
                    f"run of {record.asop_id!r} v{record.version} has no binding for role(s): {', '.join(unbound)}",
                    "Bind every role the procedure declares to an agent, route or person.")
        for c in record.constraints:
            roles = c["distinct"]
            actors = [bindings[r] for r in roles]
            if len(set(actors)) != len(actors):
                _refuse("constraint_unsatisfiable",
                        f"roles {roles} must be distinct but share a binding: {actors}",
                        "Bind those roles to different actors — the separation is the point.")
        # by construction: a judged step's route is distinct from the routes of what it judges
        by_index = {s.step: s for s in record.steps}
        for s in record.steps:
            if s.uses or not s.gate or s.gate.get("kind") != "judged":
                continue
            for ref in s.after:
                prev = by_index[ref]
                if not prev.uses and bindings[prev.role] == bindings[s.role]:
                    _refuse("constraint_unsatisfiable",
                            f"step {s.step} judges step {ref} but both are bound to {bindings[s.role]!r}",
                            "Bind the judging role to a different actor than the one it judges.")

    def _file_steps(self, record: ASOP, parent: Task, bindings: dict, beads: Beads) -> None:
        """File `record`'s steps under `parent`, then hold `parent` open on them.

        The plane writes a child into its parent's `blocked_by` in the same
        lock that files it. This runtime's `create` does not, so the store
        does it here, once every child exists: the parent is `blocked_by`
        all of them, and cannot close while any is open (ASOP.md §5.1).
        """
        ids: dict[int, str] = {}
        for s in record.steps:
            blocked_by = [ids[a] for a in s.after]
            if s.uses:
                inner = self.get(s.uses["asop_id"], s.uses["version"])
                if inner is None:
                    _refuse("sop_refused",
                            f"step {s.step} uses {s.uses['asop_id']!r} v{s.uses['version']}, which does not exist",
                            "Create that version, or point the step at one that exists.")
                self._check_bindings(inner, bindings)
                try:
                    container = beads.create(
                        title=f"{s.name}", description=inner.purpose or inner.title,
                        parent_id=parent.id, blocked_by=blocked_by,
                        metadata={"sop_ref": record.step_ref(s.step), "uses": inner.ref},
                    )
                except DepthLimitError as e:
                    _refuse("decomposition_bound", str(e), "Flatten the nesting; the bound is three deep.")
                self._file_steps(inner, container, bindings, beads)
                ids[s.step] = container.id
                continue
            role_kind = record.roles[s.role]["kind"]
            actor = bindings[s.role]
            kwargs = {"assigned_to": f"human:{actor}"} if role_kind == HUMAN else {"assigned_agent": actor}
            step_copy = {k: v for k, v in s.__dict__.items() if k in (
                "name", "role", "purpose", "entry_check", "inputs", "definition_of_done",
                "validation", "write_back", "common_mistakes", "tags") and v not in (None, [], {})}
            try:
                bead = beads.create(
                    title=f"{s.step}. {s.name}",
                    description=s.purpose or s.name,
                    parent_id=parent.id, blocked_by=blocked_by,
                    metadata={"sop_ref": record.step_ref(s.step), "step": step_copy, "verify": dict(s.gate)},
                    **kwargs,
                )
            except DepthLimitError as e:
                _refuse("decomposition_bound", str(e), "Flatten the nesting; the bound is three deep.")
            ids[s.step] = bead.id
        beads.update(parent.id, blocked_by=sorted(ids.values()))

    def drifted(self, task: Task) -> bool:
        """Has the procedure this bead pins moved on? (ASOP.md §2.1)"""
        ref = (task.metadata or {}).get("sop_ref") or {}
        if not ref:
            return False
        active = self.get(ref["asop_id"])
        return active is None or active.version != ref["version"]
