"""The push outbox: everything the harness *says* leaves through one file.

Hub ADR 0013 splits the harness's traffic in two. Work moves by pull (pull,
renew, fenced leases). Every outbound statement (reports, attestations,
lifecycle edges, procedure proposals) moves by push: the harness appends one
line to ``outbox.jsonl`` and the hub's drainer, a separate local process that
holds the machine credential, signs and publishes it. The signature decides the
actor, never the body, which is why a payload that sets ``actor`` is refused.

WIRE FORMAT
-----------
Exactly the hub's L1 outbox (``docs/outbox.md`` and ``agentco/outbox.py`` in
the hub repo): one JSON object per line with ``line_id``, ``at``, ``verb``,
``payload`` and ``agent_label`` (null when absent), keys sorted. The format is
the contract, not this helper, so ``tests/test_outbox.py`` pins the field set
and the verb set against the hub's.

WHAT HAPPENS TO A PUSH, IN ORDER
--------------------------------
1. Shape validation, mirroring the hub's: known verb, object payload, no
   ``actor``, string label. A caller's bug fails here, whatever else is true.
2. Verb class gate (ADR 0013 s6). ``work`` is on by default; ``knowledge`` is
   off until the owner enables it. A verb with no class is refused: a new hub
   verb has to be classified here before this module will carry it.
3. Clean room (ADR 0013 s5, ADR 0015 s3). ``clean_room.assert_clean`` judges the
   payload AND the agent label before anything is written, because the outbox
   is a durable record on disk and content that must not leave must never be
   put there. Fail-closed: a missing or empty list file refuses the push, and a
   hit propagates as ``CleanRoomRefused``. Nothing is created on disk.
4. One locked read-append. Under the same sidecar lock the hub uses
   (``outbox.jsonl.lock``), existing lines are scanned for the new ``line_id``;
   a hit is a no-op. Otherwise the line is appended whole and fsynced.

IDEMPOTENCY
-----------
``line_id`` is ``ob_`` plus the first 16 hex digits of the sha256 of the
canonical JSON of ``[verb, payload, attempt_id or ""]``. It is never random, so
a replay after a crash writes the same id and is a no-op here, and on the hub
the id is the idempotency key that makes the retry free there too.

VERSION CONFLICTS (ADR 0013 s4)
-------------------------------
Not this module's job. A push that targets a stale version is refused by the
hub with a conflict code and the drainer re-reads before retrying; it never
overwrites. All the harness owes is the ability to say which version it saw, so
a payload may carry an ``expected_version`` key, which is passed through
untouched (and is part of the line's identity: the same report against a newer
version is a different statement).

NOT DONE HERE
-------------
Draining, signing and receipts (the hub's drainer owns them), and the wiring
from the orchestrator. "Work verbs are on once a hub is configured" is the
caller's check: this module only knows which classes the owner enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from agentco_harness import clean_room
from agentco_harness.filelock import lock_exclusive, unlock

# --- the hub's wire contract (mirrored; pinned by tests) --------------------

LINE_FIELDS = ("line_id", "at", "verb", "payload", "agent_label")

PUSH_VERBS: tuple[str, ...] = (
    "claim_scope",
    "release_scope",
    "snapshot",
    "work_report",
    "attest",
    "adjudicate",
    "sop_revise",
    "sop_activate",
)

MAX_LINE_BYTES = 64 * 1024

# --- verb classes (ADR 0013 s6) ---------------------------------------------

CLASS_WORK = "work"
CLASS_KNOWLEDGE = "knowledge"
CLASSES = (CLASS_WORK, CLASS_KNOWLEDGE)
#: Verbs the harness never sends, whatever is enabled. Not in CLASSES, so no
#: configuration can turn it on. Hub ADR 0015 s1: activation is the owner's act.
CLASS_NEVER = "never"

#: Statements about work the harness is already doing are ``work``; anything
#: that proposes a change to shared procedure or memory is ``knowledge``. A
#: future memory verb belongs in ``knowledge`` and must be added here before it
#: can be pushed.
VERB_CLASS: dict[str, str] = {
    "claim_scope": CLASS_WORK,
    "release_scope": CLASS_WORK,
    "snapshot": CLASS_WORK,
    "work_report": CLASS_WORK,
    "attest": CLASS_WORK,
    "adjudicate": CLASS_WORK,
    "sop_revise": CLASS_KNOWLEDGE,
    "sop_activate": CLASS_NEVER,
}

CODE_INVALID = "outbox_line_invalid"
CODE_CLASS_DISABLED = "outbox_class_disabled"
CODE_VERB_NEVER = "outbox_verb_never"

DEFAULT_PATH = ".agentco/outbox.jsonl"
CONSUMED_KEYS = {"enabled_classes", "path", "clean_room_path"}


class OutboxRefused(Exception):
    """A push the outbox will not write. ``code`` says why, the message how to fix it."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class OutboxConfig:
    """The ``outbox:`` section of config.yaml.

    ``enabled_classes``: which verb classes may be written. Default work only.
    ``path``: the outbox file; ``Config.load`` resolves a relative value against
    the config file's directory, like ``tasks_path``.
    ``clean_room_path``: the private list file; unset falls through to
    ``AGENTCO_CLEAN_ROOM`` and then ``clean-room.json`` beside the bead store.
    """

    enabled_classes: list[str] = field(default_factory=lambda: [CLASS_WORK])
    path: str = DEFAULT_PATH
    clean_room_path: Optional[str] = None

    @classmethod
    def from_mapping(cls, block: Mapping[str, Any], base: Path) -> "OutboxConfig":
        classes = block.get("enabled_classes", [CLASS_WORK])
        if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
            raise ValueError("`outbox.enabled_classes:` must be a list of class names")
        unknown = sorted(set(classes) - set(CLASSES))
        if unknown:
            # A typo here would silently leave a class off; refuse instead.
            raise ValueError(
                f"`outbox.enabled_classes:` has unknown class(es) {unknown}; known: {list(CLASSES)}"
            )

        def resolve(value: str) -> str:
            p = Path(value).expanduser()
            return str(p if p.is_absolute() else (base / p).resolve())

        crp = block.get("clean_room_path")
        return cls(
            enabled_classes=list(classes),
            path=resolve(str(block.get("path") or DEFAULT_PATH)),
            clean_room_path=resolve(str(crp)) if crp else None,
        )


@dataclass(frozen=True)
class PushResult:
    line: dict
    #: False when the same content was already in the file (a replay).
    written: bool


def derive_line_id(verb: str, payload: dict, attempt_id: Optional[str] = None) -> str:
    """Deterministic ``ob_`` id from content; never random."""
    canon = json.dumps([verb, payload, attempt_id or ""], sort_keys=True, separators=(",", ":"))
    return "ob_" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _invalid(message: str) -> OutboxRefused:
    return OutboxRefused(CODE_INVALID, message)


def _validate(verb: Any, payload: Any, agent_label: Any) -> None:
    if verb not in PUSH_VERBS:
        raise _invalid(
            f"verb must be one of {list(PUSH_VERBS)}, got {verb!r}. Any tool may push; "
            f"no tool may file: creating work needs an authenticated surface."
        )
    if not isinstance(payload, dict):
        raise _invalid(f"payload must be an object, got {type(payload).__name__}")
    if "actor" in payload:
        raise _invalid(
            "payload sets `actor`. Remove it: the drainer's signature decides the actor; "
            "a self-reported name belongs in `agent_label`."
        )
    if agent_label is not None and not isinstance(agent_label, str):
        raise _invalid(f"agent_label must be a string or absent, got {type(agent_label).__name__}")


class Outbox:
    def __init__(self, config: Optional[OutboxConfig] = None, *, store_dir: Path | str = "."):
        self.config = config or OutboxConfig()
        self.store_dir = Path(store_dir).expanduser()
        self.path = Path(self.config.path).expanduser()

    def push(
        self,
        verb: str,
        payload: dict,
        *,
        agent_label: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> PushResult:
        _validate(verb, payload, agent_label)
        label = agent_label.strip() if isinstance(agent_label, str) and agent_label.strip() else None

        klass = VERB_CLASS.get(verb)
        if klass == CLASS_NEVER:
            raise OutboxRefused(
                CODE_VERB_NEVER,
                f"{verb!r} is never sent by the harness: activation is the owner's act "
                f"(hub ADR 0015 s1). Activate on the hub instead.",
            )
        if klass is None or klass not in self.config.enabled_classes:
            raise OutboxRefused(
                CODE_CLASS_DISABLED,
                f"{verb!r} is a {klass or 'unclassified'} verb and the {klass or 'its'} class is not "
                f"enabled (enabled: {self.config.enabled_classes}). The owner opts in per class "
                f"with `outbox.enabled_classes`.",
            )

        # Fail-closed, and before the file or its lock exist.
        clean_room.assert_clean(
            {"payload": payload, "agent_label": label},
            path=self.config.clean_room_path,
            store_dir=self.store_dir,
        )

        line = {
            "line_id": derive_line_id(verb, payload, attempt_id),
            "at": datetime.now(timezone.utc).isoformat(),
            "verb": verb,
            "payload": payload,
            "agent_label": label,
        }
        encoded = json.dumps(line, sort_keys=True).encode("utf-8")
        if len(encoded) + 1 > MAX_LINE_BYTES:
            raise _invalid(
                f"line is {len(encoded) + 1} bytes, over the {MAX_LINE_BYTES} limit. "
                f"Push a pointer, not a body."
            )

        with self._locked():
            existing = self._find(line["line_id"])
            if existing is not None:
                return PushResult(existing, written=False)
            _append(self.path, encoded)
        return PushResult(line, written=True)

    # -- internals -----------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # Same sidecar name as the hub's Outbox, so a drainer and a pusher
        # exclude each other.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(self.path.suffix + ".lock"), "a+") as handle:
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

    def _find(self, line_id: str) -> Optional[dict]:
        """The stored line with this id, skipping anything unparseable (a torn tail)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        for chunk in raw.split(b"\n"):
            if line_id.encode() not in chunk:
                continue
            try:
                rec = json.loads(chunk.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("line_id") == line_id:
                return rec
        return None


def _append(path: Path, line: bytes) -> None:
    """Append one whole line, durably, even after a torn one.

    If the file does not end in a newline the previous writer died mid-line:
    write one first so the fragment stays its own (unparseable) line and the
    new record lands whole instead of gluing onto it.
    """
    data = line + b"\n"
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                data = b"\n" + data
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
