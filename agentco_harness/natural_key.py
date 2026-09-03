"""Natural keys — ONE uniqueness rule for every ingest path.

v1 grew roughly six mutually-incompatible idempotency mechanisms, one per
source, each invented at the moment that source was written:

* ``ado_radar``      — a scalar watermark plus a ``seen_pr_ids`` set
* ``chronicle``      — a watermark keyed by bead id
* ``emailfeed``      — a full ``beads.list()`` scan for a matching ``source_id``
* ``callouts``       — a quote-hash ``source_id`` plus its own pre-read map
* ``recurring``      — "is a previously-spawned bead still open?"
* ``feeds``          — per-source ``last_seen`` cursors

They disagree, they are each re-derived per source, and the weakest of them sat
on the highest-volume path. Duplicates got through: one measured node-day had
24 of 24 costed runs be duplicate RCA beads, and one finding fanned out to 95.

A **natural key** is the deterministic string that answers "is this the same
piece of work as something already filed?". Exactly one mechanism, enforced at
the one place every bead is born (``Beads.create``), so no source can opt out
by forgetting, and no future source has to invent a seventh mechanism.

Three derivation forms, in precedence order:

1. **explicit** — the caller states the key. Used verbatim (after
   normalisation); the caller owns its own namespace.
2. **external** ``ext|<source>|<source_id>`` — the bead MIRRORS something that
   exists outside AgentCo (an email, an ADO work item, a transcript callout).
   The external thing's identity is the bead's identity.
3. **generated** ``gen|<kind>|<subject>|<period>`` — the bead is GENERATED work
   with no external referent: a recurring ritual, a periodic sweep, a scheduled
   report. Identity is "this kind of work, about this subject, for this period".
   The period component is what makes a *nightly* job idempotent per night
   rather than idempotent forever.

A bead with no derivable key is unconstrained — exactly the v1 behaviour, so
adding this module changes nothing for ad-hoc manual beads.

**Forward-compatibility.** v2's ``resources`` table carries
``natural_key TEXT`` with ``CREATE UNIQUE INDEX ... WHERE natural_key IS NOT
NULL``. This module produces the values that column will hold, and stores them
under ``metadata.natural_key`` — the v1 carrier for a v2 column. Nothing here
needs to change when the store does.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


#: Where the key lives on a bead. ``metadata`` is the v1 carrier for what v2
#: models as a first-class indexed column.
NATURAL_KEY_FIELD = "natural_key"

#: Stamped by the backfill on the LATER members of a historical collision, so
#: the duplicates that predate enforcement stay queryable instead of merely
#: being counted once and forgotten.
DUPLICATE_OF_FIELD = "natural_key_duplicate_of"

#: Component separator. Escaped inside components so the key parses back
#: unambiguously — a key is a compound identity, not a display string.
SEPARATOR = "|"
_ESCAPED_SEPARATOR = "%7C"

#: Namespace prefixes for the two derived forms. Explicit keys carry no
#: prefix, which is what keeps a caller-owned key from ever colliding with a
#: derived one by accident.
EXTERNAL_PREFIX = "ext"
GENERATED_PREFIX = "gen"

#: Longest a single component may be before it is folded to a bounded, stable
#: digest form. Free-text subjects (bead titles) are the reason this exists.
MAX_COMPONENT_LEN = 160

#: Control characters are refused rather than stripped. This is the same defect
#: class as the ``--blocked-by 'ac-aaa\nac-bbb'`` incident (see
#: ``beads.TaskReferenceError``): a key with an embedded newline is a key that
#: will never match again, and a silently-stripped one is a key that matches
#: something it should not.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RUN = re.compile(r"\s+")


class NaturalKeyError(ValueError):
    """A natural key could not be derived from what the caller supplied.

    Raised at authoring time, which is the only cheap moment. A key that is
    wrong is worse than no key at all: it either collapses two distinct pieces
    of work into one (silently dropping the second) or fails to match the
    thing it was supposed to match (silently duplicating). Both are invisible
    at read time, so both are refused at write time.
    """


# --------------------------------------------------------------------------- #
# Component normalisation
# --------------------------------------------------------------------------- #

def normalize_component(name: str, value: object, *, fold_case: bool = False) -> str:
    """Normalise ONE component of a compound key.

    ``fold_case`` is deliberately opt-in and defaults to False. External ids
    are case-SIGNIFICANT (a Gmail ``Message-Id``, an ADO revision token), so
    lowercasing them would merge distinct records. Free-text subjects are not,
    so the generated form folds those.
    """
    if value is None:
        raise NaturalKeyError(f"natural key component {name!r} is None — nothing to key on")
    text = value if isinstance(value, str) else str(value)
    if _CONTROL_CHARS.search(text):
        raise NaturalKeyError(
            f"natural key component {name!r} contains a control character "
            f"({text!r}). A key with an embedded newline or NUL never matches "
            f"again — refusing rather than stripping, because a silently "
            f"repaired key is a silent duplicate."
        )
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if not text:
        raise NaturalKeyError(
            f"natural key component {name!r} is empty after normalisation — "
            f"an empty component would make every keyless bead of this shape "
            f"collide with every other."
        )
    if fold_case:
        text = text.casefold()
    if len(text) > MAX_COMPONENT_LEN:
        # Bounded but still injective in practice: the readable prefix keeps
        # the key greppable, the digest keeps it unique. Deterministic, so the
        # same subject always folds to the same key.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        text = f"{text[: MAX_COMPONENT_LEN - 13]}~{digest}"
    return text.replace(SEPARATOR, _ESCAPED_SEPARATOR)


def normalize_natural_key(value: object) -> str:
    """Normalise a caller-supplied (explicit) key.

    The separator is NOT escaped here: an explicit key is already a whole key,
    and a caller who writes ``ext|gmail|<id>`` means to target exactly that.
    """
    if value is None:
        raise NaturalKeyError("explicit natural key is None")
    text = value if isinstance(value, str) else str(value)
    if _CONTROL_CHARS.search(text):
        raise NaturalKeyError(
            f"explicit natural key contains a control character ({text!r}) — refused"
        )
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if not text:
        raise NaturalKeyError("explicit natural key is empty after normalisation")
    if len(text) > MAX_COMPONENT_LEN * 4:
        raise NaturalKeyError(
            f"explicit natural key is {len(text)} chars (max "
            f"{MAX_COMPONENT_LEN * 4}) — a key that long is a payload, not an "
            f"identity; hash it yourself so the hashing is yours to explain."
        )
    return text


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #

def external_key(source: object, source_id: object) -> str:
    """``ext|<source>|<source_id>`` — the bead mirrors an external record."""
    return SEPARATOR.join(
        (
            EXTERNAL_PREFIX,
            normalize_component("source", source, fold_case=True),
            normalize_component("source_id", source_id),
        )
    )


def generated_key(kind: object, subject: object, period: object) -> str:
    """``gen|<kind>|<subject>|<period>`` — generated work, per period.

    ``subject`` is case-folded because it is free text (a schedule id, a bead
    title). ``period`` is not: it is usually an ISO instant or a date, where
    case never varies and folding would only hide a malformed value.
    """
    return SEPARATOR.join(
        (
            GENERATED_PREFIX,
            normalize_component("kind", kind, fold_case=True),
            normalize_component("subject", subject, fold_case=True),
            normalize_component("period", period),
        )
    )


def derive_natural_key(
    *,
    explicit: object | None = None,
    source: object | None = None,
    source_id: object | None = None,
    kind: object | None = None,
    subject: object | None = None,
    period: object | None = None,
) -> str | None:
    """The one derivation. Returns ``None`` when nothing is keyable.

    Precedence: explicit > generated > external. ``explicit`` wins on purpose —
    a caller that states a key has more information than this function does,
    and the common case (a ``source``/``source_id`` pair that means something
    other than identity) needs an override that is obvious in the call.

    A PARTIALLY-supplied form is an error, never a silent downgrade. If a
    caller names a ``kind`` and a ``subject`` but forgets the ``period``, the
    honest answers are "key it forever" or "do not key it", and picking either
    one for them produces a bug they cannot see.
    """
    if explicit is not None:
        return normalize_natural_key(explicit)

    generated_parts = {"kind": kind, "subject": subject, "period": period}
    supplied = {k: v for k, v in generated_parts.items() if v is not None}
    if supplied:
        missing = sorted(set(generated_parts) - set(supplied))
        if missing:
            raise NaturalKeyError(
                f"generated natural key needs kind+subject+period; missing "
                f"{', '.join(missing)}. Supply all three, or none — a partial "
                f"key would either dedup work that should recur or fail to "
                f"dedup work that should not."
            )
        return generated_key(kind, subject, period)

    if source_id is not None:
        if source is None:
            raise NaturalKeyError(
                "source_id was given without source — an external id with no "
                "namespace collides across systems (an ADO id 4211 and a "
                "Transkriptor order 4211 are not the same work)."
            )
        return external_key(source, source_id)

    return None


def natural_key_of(task_or_dict: object) -> str | None:
    """Read the stored key off a ``Task`` or a raw decoded JSONL row."""
    metadata = (
        task_or_dict.get("metadata")
        if isinstance(task_or_dict, dict)
        else getattr(task_or_dict, "metadata", None)
    )
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(NATURAL_KEY_FIELD)
    return value if isinstance(value, str) and value else None


def derive_for_row(row: dict) -> str | None:
    """Best-effort key for an ALREADY-STORED row (used by the backfill).

    Only the external form is derivable after the fact: ``source``/``source_id``
    are stored on every bead, whereas a generated bead's period was never
    written down as such. Anything not derivable is reported, not guessed.
    """
    source = row.get("source")
    source_id = row.get("source_id")
    if not source or not source_id:
        return None
    try:
        return external_key(source, source_id)
    except NaturalKeyError:
        return None


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #

@dataclass
class BackfillReport:
    """What the backfill saw and what it would do (or did)."""

    path: str
    total_rows: int = 0
    unparseable_rows: int = 0
    already_keyed: int = 0
    keyed: int = 0
    not_derivable: int = 0
    #: derived key -> ids of every bead carrying it, for keys held by >1 bead.
    #: THIS is the measurement: each entry is a duplicate that the pre-existing
    #: per-source idempotency mechanisms let through.
    collisions: dict[str, list[str]] = field(default_factory=dict)
    applied: bool = False

    @property
    def colliding_keys(self) -> int:
        return len(self.collisions)

    @property
    def duplicate_beads(self) -> int:
        """Beads that would have been suppressed had the index existed."""
        return sum(len(ids) - 1 for ids in self.collisions.values())

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "total_rows": self.total_rows,
            "unparseable_rows": self.unparseable_rows,
            "already_keyed": self.already_keyed,
            "keyed": self.keyed,
            "not_derivable": self.not_derivable,
            "colliding_keys": self.colliding_keys,
            "duplicate_beads": self.duplicate_beads,
            "collisions": self.collisions,
            "applied": self.applied,
        }


def backfill_store(path: Path | str, *, apply: bool = False) -> BackfillReport:
    """Stamp ``metadata.natural_key`` onto every row where it is derivable.

    NON-DESTRUCTIVE by construction, and deliberately NOT routed through
    ``Task.from_json``/``Task.to_json``: that round-trip drops unknown
    top-level fields (``from_json`` filters to declared dataclass fields), so
    running it over a production store would silently delete any column a
    newer writer had added. This walks raw JSON dicts instead — unknown fields
    survive verbatim, and a line that will not parse is copied through byte for
    byte.

    Rows that ALREADY carry a key are left exactly as they are. Historical
    collisions are stamped on every member (history is not rewritten to look
    clean) and the later members additionally get
    ``metadata.natural_key_duplicate_of`` pointing at the first, so the
    duplicates that predate enforcement stay findable.

    ``apply=False`` (the default) computes the whole report and writes nothing.

    When applying, the read and the replace happen under the SAME advisory lock
    ``Beads`` takes (``<store>.lock``). Without it, a launchd node appending a
    bead between this function's read and its ``os.replace`` would have that
    bead silently deleted — a whole-file rewrite is the one operation on this
    store that can lose an append, and three of these stores have a scheduler
    writing to them.
    """
    path = Path(path)
    report = BackfillReport(path=str(path), applied=apply)
    if not path.exists():
        return report
    if apply:
        with _store_lock(path):
            return _backfill_locked(path, report)
    return _backfill_locked(path, report)


@contextmanager
def _store_lock(path: Path):
    """The same advisory lock ``Beads._locked`` takes, on the same file."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _backfill_locked(path: Path, report: BackfillReport) -> BackfillReport:
    apply = report.applied
    raw_lines = path.read_text().splitlines()

    # Pass 1 — decode, derive, and group by key so collisions are known before
    # anything is written. Grouping must happen first: which bead is "the
    # first" for a collision is a property of the whole file, not of one row.
    rows: list[tuple[str, dict | None, str | None]] = []
    by_key: dict[str, list[str]] = {}
    for raw in raw_lines:
        if not raw.strip():
            rows.append((raw, None, None))
            continue
        report.total_rows += 1
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("row is not a JSON object")
        except (ValueError, TypeError):
            report.unparseable_rows += 1
            rows.append((raw, None, None))
            continue

        existing = natural_key_of(decoded)
        key = existing or derive_for_row(decoded)
        if existing:
            report.already_keyed += 1
        elif key:
            report.keyed += 1
        else:
            report.not_derivable += 1
        if key:
            by_key.setdefault(key, []).append(str(decoded.get("id", "")))
        rows.append((raw, decoded, key))

    report.collisions = {k: ids for k, ids in by_key.items() if len(ids) > 1}

    if not apply:
        return report

    first_holder = {k: ids[0] for k, ids in report.collisions.items()}
    out: list[str] = []
    for raw, decoded, key in rows:
        if decoded is None or key is None:
            out.append(raw)
            continue
        metadata = decoded.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get(NATURAL_KEY_FIELD) == key and (
            key not in first_holder or DUPLICATE_OF_FIELD in metadata
        ):
            out.append(raw)
            continue
        metadata[NATURAL_KEY_FIELD] = key
        first = first_holder.get(key)
        if first and str(decoded.get("id", "")) != first:
            metadata[DUPLICATE_OF_FIELD] = first
        decoded["metadata"] = metadata
        out.append(json.dumps(decoded, ensure_ascii=False))

    _atomic_write_lines(path, out)
    return report


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    """Temp file in the SAME directory + fsync + ``os.replace``.

    Same posture as ``Beads._write_all``: a crash mid-backfill must leave the
    whole old file or the whole new one, never a truncated queue.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".natkey-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
