"""Natural keys: derivation, enforcement at create, and the backfill.

Every test here fails without ``agentco/natural_key.py`` and the ``create()``
enforcement — either by ImportError, or (for the enforcement cases) by the
store ending up with two beads where it should hold one.
"""

from __future__ import annotations

import json

import pytest

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.natural_key import (
    DUPLICATE_OF_FIELD,
    MAX_COMPONENT_LEN,
    NATURAL_KEY_FIELD,
    NaturalKeyError,
    backfill_store,
    derive_for_row,
    derive_natural_key,
    external_key,
    generated_key,
    natural_key_of,
    normalize_component,
)


def _store(tmp_path) -> Beads:
    return Beads(tmp_path / "tasks.jsonl")


def _lines(beads: Beads) -> list[str]:
    return [line for line in beads.path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #

def test_external_form_is_namespaced_and_deterministic():
    assert external_key("email", "CADn=abc@mail") == "ext|email|CADn=abc@mail"
    assert external_key("email", "CADn=abc@mail") == external_key("email", "CADn=abc@mail")


def test_source_is_case_folded_but_the_external_id_is_not():
    # A Gmail Message-Id and an ADO revision token are case-SIGNIFICANT; folding
    # them would merge two distinct records into one bead.
    assert external_key("Email", "x") == external_key("email", "x")
    assert external_key("email", "ABC") != external_key("email", "abc")


def test_the_separator_is_escaped_inside_components():
    # Without escaping, ("a|b", "c") and ("a", "b|c") would be the same key.
    assert external_key("a|b", "c") != external_key("a", "b|c")


def test_generated_form_carries_the_period():
    a = generated_key("recurring", "standup", "2026-08-25")
    b = generated_key("recurring", "standup", "2026-08-26")
    assert a != b
    assert a.startswith("gen|recurring|standup|")


def test_a_partially_supplied_generated_key_is_refused_not_downgraded():
    # "Key it forever" and "do not key it" are both wrong answers to guess.
    with pytest.raises(NaturalKeyError, match="period"):
        derive_natural_key(kind="recurring", subject="standup")


def test_source_id_without_source_is_refused():
    with pytest.raises(NaturalKeyError, match="without source"):
        derive_natural_key(source_id="4211")


def test_control_characters_are_refused_not_stripped():
    # The `--blocked-by 'ac-aaa\nac-bbb'` defect class: a repaired key is a
    # silent duplicate, a stripped one is a silent merge.
    with pytest.raises(NaturalKeyError, match="control character"):
        normalize_component("source_id", "ac-aaaaaaaa\nac-bbbbbbbb")


def test_empty_after_normalisation_is_refused():
    with pytest.raises(NaturalKeyError, match="empty"):
        normalize_component("subject", "   ")


def test_long_components_fold_to_a_bounded_but_still_distinct_key():
    long_a = "x" * 400
    long_b = "x" * 399 + "y"
    key_a = generated_key("retro", long_a, "2026-08-25")
    key_b = generated_key("retro", long_b, "2026-08-25")
    assert key_a != key_b
    assert len(key_a) < MAX_COMPONENT_LEN + 40
    assert key_a == generated_key("retro", long_a, "2026-08-25")  # deterministic


def test_precedence_explicit_beats_generated_beats_external():
    assert derive_natural_key(explicit="mine", source="s", source_id="i") == "mine"
    assert derive_natural_key(
        source="rca", source_id="rca-for:x:cycle1", kind="rca", subject="x", period="c1"
    ).startswith("gen|")


def test_nothing_keyable_returns_none():
    assert derive_natural_key() is None
    assert derive_natural_key(source="manual") is None


# --------------------------------------------------------------------------- #
# Enforcement at create()
# --------------------------------------------------------------------------- #

def test_duplicate_create_is_a_loud_no_op_returning_the_existing_bead(tmp_path, capsys):
    beads = _store(tmp_path)
    first = beads.create("Email A", "body", source="email", source_id="msg-1")
    capsys.readouterr()

    second = beads.create("Email A (again)", "body", source="email", source_id="msg-1")

    assert second.id == first.id
    assert second.title == "Email A"  # the EXISTING bead, not the attempted one
    assert getattr(second, "natural_key_conflict", False) is True
    assert len(_lines(beads)) == 1

    err = capsys.readouterr().err
    assert "DUPLICATE-SUPPRESSED" in err
    assert "ext|email|msg-1" in err
    assert first.id in err


def test_the_key_is_stored_on_the_bead(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("t", "d", source="email", source_id="msg-1")
    assert task.metadata[NATURAL_KEY_FIELD] == "ext|email|msg-1"
    assert natural_key_of(beads.get(task.id)) == "ext|email|msg-1"


def test_distinct_work_is_unaffected(tmp_path):
    beads = _store(tmp_path)
    a = beads.create("A", "d", source="email", source_id="msg-1")
    b = beads.create("B", "d", source="email", source_id="msg-2")
    c = beads.create("C", "d", source="ado", source_id="msg-1")  # same id, other system
    assert len({a.id, b.id, c.id}) == 3
    assert len(_lines(beads)) == 3


def test_unkeyed_beads_may_still_repeat(tmp_path):
    # Ad-hoc manual beads carry no source_id and are unconstrained, exactly as
    # before this existed. Two identical `agentco tasks create` calls are two
    # pieces of work, and nothing here may change that.
    beads = _store(tmp_path)
    a = beads.create("Same title", "d", source="manual")
    b = beads.create("Same title", "d", source="manual")
    assert a.id != b.id
    assert natural_key_of(a) is None


def test_explicit_key_dedups_across_different_sources(tmp_path):
    beads = _store(tmp_path)
    a = beads.create("A", "d", source="email", source_id="1", natural_key="incident:42")
    b = beads.create("B", "d", source="ado", source_id="99", natural_key="incident:42")
    assert b.id == a.id
    assert len(_lines(beads)) == 1


def test_generated_key_dedups_per_period_and_not_across_periods(tmp_path):
    beads = _store(tmp_path)
    first = beads.create(
        "StandUp", "d",
        natural_key_kind="ritual", natural_key_subject="standup",
        natural_key_period="2026-08-25",
    )
    same = beads.create(
        "StandUp", "d",
        natural_key_kind="ritual", natural_key_subject="standup",
        natural_key_period="2026-08-25",
    )
    tomorrow = beads.create(
        "StandUp", "d",
        natural_key_kind="ritual", natural_key_subject="standup",
        natural_key_period="2026-08-26",
    )
    assert same.id == first.id
    assert tomorrow.id != first.id
    assert len(_lines(beads)) == 2


def test_a_malformed_key_is_refused_before_anything_is_written(tmp_path):
    beads = _store(tmp_path)
    with pytest.raises(NaturalKeyError):
        beads.create("t", "d", source="email", source_id="msg\n1")
    assert _lines(beads) == []


def test_find_by_natural_key_and_collisions(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("t", "d", source="email", source_id="msg-1")
    assert beads.find_by_natural_key("ext|email|msg-1").id == task.id
    assert beads.find_by_natural_key("ext|email|nope") is None
    # create() cannot produce a collision, so a fresh store has none.
    assert beads.natural_key_collisions() == {}


def test_suppression_does_not_disturb_the_existing_bead(tmp_path):
    beads = _store(tmp_path)
    first = beads.create("t", "d", source="email", source_id="msg-1")
    beads.complete(first.id)
    again = beads.create("t", "d", source="email", source_id="msg-1")
    assert again.status is TaskStatus.DONE  # not resurrected, not re-opened
    assert len(_lines(beads)) == 1


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #

def _write_rows(path, rows: list[dict | str]) -> None:
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n"
    )


def _row(task_id: str, **over) -> dict:
    row = {
        "id": task_id,
        "title": "t",
        "description": "d",
        "status": "pending",
        "priority": 2,
        "source": "email",
        "source_id": "msg-1",
        "metadata": {},
    }
    row.update(over)
    return row


def test_backfill_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001")])
    before = path.read_text()

    report = backfill_store(path)

    assert report.keyed == 1
    assert report.applied is False
    assert path.read_text() == before


def test_backfill_stamps_the_key(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001"), _row("ac-00000002", source_id="msg-2")])

    backfill_store(path, apply=True)

    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [natural_key_of(r) for r in rows] == ["ext|email|msg-1", "ext|email|msg-2"]


def test_backfill_is_idempotent(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001")])
    backfill_store(path, apply=True)
    after_first = path.read_text()

    second = backfill_store(path, apply=True)

    assert second.keyed == 0
    assert second.already_keyed == 1
    assert path.read_text() == after_first


def test_backfill_reveals_and_marks_historical_collisions(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(
        path,
        [
            _row("ac-00000001"),
            _row("ac-00000002"),  # same source_id — a duplicate that got through
            _row("ac-00000003"),  # and a third
            _row("ac-00000009", source_id="msg-9"),
        ],
    )

    report = backfill_store(path, apply=True)

    assert report.colliding_keys == 1
    assert report.duplicate_beads == 2  # 3 beads, 1 legitimate
    assert report.collisions["ext|email|msg-1"] == [
        "ac-00000001",
        "ac-00000002",
        "ac-00000003",
    ]

    rows = {
        json.loads(line)["id"]: json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    }
    assert DUPLICATE_OF_FIELD not in rows["ac-00000001"]["metadata"]
    assert rows["ac-00000002"]["metadata"][DUPLICATE_OF_FIELD] == "ac-00000001"
    assert rows["ac-00000003"]["metadata"][DUPLICATE_OF_FIELD] == "ac-00000001"


def test_backfill_preserves_unknown_top_level_fields(tmp_path):
    # Task.from_json filters to declared dataclass fields, so a round trip
    # through it would DELETE any column a newer writer added. The backfill must
    # not do that to production data.
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001", future_column={"kept": True})])

    backfill_store(path, apply=True)

    row = json.loads(path.read_text().splitlines()[0])
    assert row["future_column"] == {"kept": True}
    assert row["metadata"][NATURAL_KEY_FIELD] == "ext|email|msg-1"


def test_backfill_preserves_unparseable_lines_verbatim(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001"), "{not json at all", _row("ac-2", source_id="m2")])

    report = backfill_store(path, apply=True)

    assert report.unparseable_rows == 1
    assert "{not json at all" in path.read_text().splitlines()


def test_backfill_leaves_an_existing_key_alone(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(
        path,
        [_row("ac-00000001", metadata={NATURAL_KEY_FIELD: "hand-written-key"})],
    )

    report = backfill_store(path, apply=True)

    assert report.already_keyed == 1 and report.keyed == 0
    assert natural_key_of(json.loads(path.read_text().splitlines()[0])) == "hand-written-key"


def test_backfill_counts_rows_it_cannot_key(tmp_path):
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001", source="manual", source_id=None)])

    report = backfill_store(path)

    assert report.not_derivable == 1 and report.keyed == 0


def test_backfill_holds_the_same_lock_beads_writes_under(tmp_path):
    """A whole-file rewrite is the one operation here that can LOSE an append.

    Three of the live stores have a launchd node writing to them. If the
    backfill read, then a node appended, then the backfill replaced, that bead
    would be gone with no trace.
    """
    import fcntl

    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001")])
    lock_path = path.with_suffix(path.suffix + ".lock")

    observed = {}
    real_replace = __import__("os").replace

    def spy_replace(src, dst):
        # While the rewrite is in flight, an outside writer must NOT be able to
        # take the lock.
        with open(lock_path, "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed["held"] = False
                fcntl.flock(f, fcntl.LOCK_UN)
            except BlockingIOError:
                observed["held"] = True
        return real_replace(src, dst)

    import agentco_harness.natural_key as nk

    orig = nk.os.replace
    nk.os.replace = spy_replace
    try:
        backfill_store(path, apply=True)
    finally:
        nk.os.replace = orig

    assert observed.get("held") is True


def test_derive_for_row_needs_both_halves():
    assert derive_for_row({"source": "email", "source_id": "m"}) == "ext|email|m"
    assert derive_for_row({"source": "manual"}) is None
    assert derive_for_row({"source_id": "m"}) is None


def test_backfill_on_a_missing_store_is_a_no_op(tmp_path):
    report = backfill_store(tmp_path / "nope.jsonl")
    assert report.total_rows == 0 and not (tmp_path / "nope.jsonl").exists()


def test_create_dedups_against_a_backfilled_bead(tmp_path):
    """The point of the backfill: history participates in the index."""
    path = tmp_path / "tasks.jsonl"
    _write_rows(path, [_row("ac-00000001")])
    backfill_store(path, apply=True)

    beads = Beads(path)
    again = beads.create("t", "d", source="email", source_id="msg-1")

    assert again.id == "ac-00000001"
    assert len(_lines(beads)) == 1


# --------------------------------------------------------------------------- #
# The two source paths whose source_id was NOT an identity
# --------------------------------------------------------------------------- #

def test_rca_recurrence_gets_its_own_key(tmp_path):
    """`rca-for:X:cycle1` is the same string for an investigation and for the
    one the SAME bead earns after that root closed and the symptom came back."""
    from agentco_harness.rca import _create_analyze_bead

    beads = _store(tmp_path)
    kwargs = dict(
        failed_title="box-scout: MAGRINHA",
        error="Unknown agent: box-scout",
        reproduce="r",
        affected="a",
        fix_plan_seed="f",
        rca_for="ac-524e1ad1",
        root_id=None,
        cycle=1,
        parent_id=None,
    )
    first = _create_analyze_bead(beads, **kwargs)
    folded = _create_analyze_bead(beads, **kwargs)
    recurrence = _create_analyze_bead(beads, **kwargs, recurred_after=first.id)

    assert folded.id == first.id  # same incident, same epoch → suppressed
    assert recurrence.id != first.id  # new epoch → a real second investigation
