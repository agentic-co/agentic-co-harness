"""Procedures pinned by content digest (hub ADR 0016), harness side."""

from __future__ import annotations

import json

import pytest
from asop.errors import Refusal
from asop.manifest import digest_of

from agentco_harness.asop_store import AsopStore, _document_digest
from agentco_harness.beads import Beads

GATE = {"kind": "deterministic", "check": "true"}


def body(title="Water the plants"):
    return {
        "title": title, "purpose": "keep the ferns alive",
        "inputs": [{"name": "room"}],
        "roles": {"gardener": {"kind": "agent"}},
        "steps": [{"name": "water", "role": "gardener", "purpose": "pour", "gate": GATE}],
    }


BIND = {"gardener": "bot-a"}
INPUTS = {"room": "atrium"}


@pytest.fixture()
def store(tmp_path):
    return AsopStore(tmp_path / "asops.jsonl")


@pytest.fixture()
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


def v1(store):
    rec = store.create(body(), author="m", author_kind="human", asop_id="ferns")
    return store.activate("ferns", 1, by_kind="human")


def lines(store):
    return [json.loads(x) for x in store.path.read_text().splitlines() if x.strip()]


def test_every_stored_version_carries_a_digest(store):
    v1(store)
    store.revise("ferns", {"purpose": "keep them alive and green"}, author="m", author_kind="human")
    rows = lines(store)
    assert all(r["digest"].startswith("sha256:") and len(r["digest"]) == 71 for r in rows)
    assert rows[0]["digest"] != rows[1]["digest"]


def test_digest_survives_a_status_rewrite(store):
    rec = v1(store)
    stored = lines(store)[0]["digest"]
    store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human")
    store.activate("ferns", 2, by_kind="human")     # rewrites the whole file
    assert lines(store)[0]["digest"] == stored == store.digest(rec)


def test_digest_is_stable_across_key_order(store):
    rec = v1(store)
    from asop.manifest import body_digest
    clean = body()
    shuffled = dict(reversed(list(clean.items())))
    shuffled["steps"] = [dict(reversed(list(s.items()))) for s in shuffled["steps"]]
    # the spec's validated-body digest agrees with the store's, whatever the key order
    assert body_digest(shuffled, asop_id="ferns") == body_digest(clean, asop_id="ferns") == _document_digest(rec)
    assert digest_of({"b": 1, "a": [2, {"d": 1, "c": 2}]}) == digest_of({"a": [2, {"c": 2, "d": 1}], "b": 1})


def test_run_records_the_digest_with_the_version(store, beads):
    rec = v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    ref = parent.metadata["sop_ref"]
    assert ref["asop_id"] == "ferns" and ref["version"] == 1
    assert ref["digest"] == store.digest(rec)


def test_tampered_stored_document_is_refused(store, beads):
    v1(store)
    rows = lines(store)
    rows[0]["steps"][0]["purpose"] = "pour acid"          # edited behind the store's back
    store.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    with pytest.raises(Refusal) as e:
        store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    assert e.value.code == "asop_digest_mismatch"
    assert beads.list() == [] if hasattr(beads, "list") else True


def test_tampered_document_is_refused_when_resolving_an_open_run(store, beads):
    v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    rows = lines(store)
    rows[0]["purpose"] = "something else entirely"
    store.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    with pytest.raises(Refusal) as e:
        store.resolve_pinned(parent)
    assert e.value.code == "asop_digest_mismatch"


def test_a_bead_pinned_to_other_bytes_is_refused(store, beads):
    v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    parent.metadata["sop_ref"]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(Refusal) as e:
        store.resolve_pinned(parent)
    assert e.value.code == "asop_digest_mismatch"


def test_a_store_with_no_digest_still_works(store, beads):
    v1(store)
    rows = lines(store)
    for r in rows:
        r.pop("digest")
    store.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    assert parent.metadata["sop_ref"]["digest"].startswith("sha256:")   # computed lazily
    assert store.resolve_pinned(parent).version == 1
    store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human")
    store.activate("ferns", 2, by_kind="human")
    assert store.get("ferns", 1) is not None


def test_activation_does_not_move_an_open_run(store, beads):
    v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human", compatible_with=[1])
    store.activate("ferns", 2, by_kind="human")
    assert store.get("ferns").version == 2
    assert store.resolve_pinned(parent).version == 1                 # no opt-in: stays
    assert beads.get(parent.id).metadata["sop_ref"]["version"] == 1


def test_compatible_with_without_opt_in_does_not_migrate(store, beads):
    v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human", compatible_with=[1])
    store.activate("ferns", 2, by_kind="human")
    assert store.resolve_pinned(parent, beads).version == 1


def test_opt_in_without_compatible_with_does_not_migrate(store, beads):
    v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    beads.update(parent.id, metadata={**parent.metadata, "asop_migrate_ok": True})
    store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human")
    store.activate("ferns", 2, by_kind="human")
    assert store.resolve_pinned(beads.get(parent.id), beads).version == 1


def test_compatible_with_plus_opt_in_migrates(store, beads):
    v1(store)
    parent = store.run("ferns", inputs=INPUTS, bindings=BIND, beads=beads)
    beads.update(parent.id, metadata={**parent.metadata, "asop_migrate_ok": True})
    store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human", compatible_with=[1])
    v2 = store.activate("ferns", 2, by_kind="human")
    moved = store.resolve_pinned(beads.get(parent.id), beads)
    assert moved.version == 2
    ref = beads.get(parent.id).metadata["sop_ref"]
    assert (ref["version"], ref["digest"], ref["migrated_from"]) == (2, store.digest(v2), 1)


def test_compatible_with_must_name_existing_versions(store):
    v1(store)
    with pytest.raises(Refusal):
        store.revise("ferns", {"purpose": "p2"}, author="m", author_kind="human", compatible_with=[7])
