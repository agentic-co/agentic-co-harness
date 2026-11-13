"""Procedure proposals: the harness half of `sop propose` (hub ADR 0015 s1).

A lesson that says "this procedure should change" leaves as ONE outbox line,
verb `sop_revise`, in the exact payload the hub's drainer reads
(`agentco/outbox.py`: `payload["sopId"]`, `payload["changes"]`, optional
`payload["title"]`). It is a proposal: nothing here activates anything, and the
`knowledge` class (off by default) and the clean room still gate the push.

Every name below is fabricated. The real clean-room lists are private
per-partition configuration and never enter this repository.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agentco_harness import clean_room, proposals
from agentco_harness.clean_room import CleanRoomRefused
from agentco_harness.outbox import CLASS_KNOWLEDGE, CLASS_WORK, Outbox, OutboxConfig, OutboxRefused
from agentco_harness.proposals import (
    SOP_BODY_KEYS,
    ProposalInvalid,
    proposal_from_lesson,
    propose_revision,
)

# The hub's `SOP_BODY_KEYS` (agentco/app.py, main), spelled out. The hub's
# strict wire refuses any other key in a revision body, so a `changes` key
# outside this set would be a 400 after the drainer had already signed it.
HUB_SOP_BODY_KEYS = (
    "purpose", "trigger",
    "task_type", "inputs", "roles", "constraints", "steps", "proposals",
    "outcomes", "outputs", "handoffs", "on_escalate", "requires", "entry_gates", "rework",
)

CHANGES = {"purpose": "Ship a reviewed release without a second manual approval step."}
PROVENANCE = {"kind": "bead", "ref": "ac-0badf00d"}


@pytest.fixture
def store(tmp_path):
    (tmp_path / clean_room.LISTS_NAME).write_text(json.dumps({
        "schemaVersion": 1,
        "internal_names": ["Quillfeather"],
        "never_leave": ["tangerine-ledger"],
        "people": ["Orsolya Brannigan"],
        "internal_domains": ["intra.quillfeather.example"],
    }))
    return tmp_path


def make(store, classes=(CLASS_WORK, CLASS_KNOWLEDGE)) -> Outbox:
    cfg = OutboxConfig(path=str(store / "outbox.jsonl"), enabled_classes=list(classes))
    return Outbox(cfg, store_dir=store)


def lines(box: Outbox) -> list[dict]:
    if not box.path.exists():
        return []
    return [json.loads(l) for l in box.path.read_text().splitlines() if l.strip()]


# ------------------------------------------------------- the hub's payload


def test_body_keys_match_the_hubs():
    assert SOP_BODY_KEYS == HUB_SOP_BODY_KEYS


def test_payload_carries_exactly_what_the_hubs_sop_revise_reads(store):
    box = make(store)
    res = propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE,
                           title="Release without the second approval", agent_label="harness-a")
    (line,) = lines(box)
    assert res.written and line["verb"] == "sop_revise"
    p = line["payload"]
    # The drainer does: registry.sop_revise(payload["sopId"], **payload["changes"], title=payload["title"])
    assert p["sopId"] == "asop-1a2b3c4d"
    assert p["changes"] == CHANGES
    assert p["title"] == "Release without the second approval"
    assert set(p) == {"sopId", "changes", "title", "provenance"}
    assert line["agent_label"] == "harness-a"


def test_title_is_omitted_when_not_given(store):
    box = make(store)
    propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert "title" not in lines(box)[0]["payload"]


def test_expected_version_rides_through_when_given(store):
    box = make(store)
    propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE,
                     expected_version=3)
    assert lines(box)[0]["payload"]["expected_version"] == 3


def test_changes_outside_the_hubs_body_keys_are_refused_before_the_outbox(store):
    box = make(store)
    for bad in ({"status": "active"}, {"author": "x"}, {"author_kind": "human"},
                {"title": "t"}, {"version": 9}):
        with pytest.raises(ProposalInvalid):
            propose_revision(box, sop_id="asop-1a2b3c4d", changes=bad, provenance=PROVENANCE)
    assert lines(box) == []


@pytest.mark.parametrize("sop_id", ["", "  ", None, 7])
def test_sop_id_must_be_a_nonempty_string(store, sop_id):
    with pytest.raises(ProposalInvalid):
        propose_revision(make(store), sop_id=sop_id, changes=CHANGES, provenance=PROVENANCE)


def test_an_empty_change_is_not_a_proposal(store):
    with pytest.raises(ProposalInvalid):
        propose_revision(make(store), sop_id="asop-1a2b3c4d", changes={}, provenance=PROVENANCE)


# ------------------------------------------------------------- provenance


def test_provenance_is_the_kind_and_ref_pointer_shape_of_the_hubs_memory(store):
    box = make(store)
    propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert lines(box)[0]["payload"]["provenance"] == {"kind": "bead", "ref": "ac-0badf00d"}


@pytest.mark.parametrize("prov", [
    None, {}, "ac-0badf00d", {"kind": "bead"}, {"ref": "ac-1"},
    {"kind": "", "ref": "ac-1"}, {"kind": "bead", "ref": "  "},
    {"kind": "bead", "ref": "ac-1", "extra": "x"}, {"kind": "k" * 65, "ref": "ac-1"},
    {"kind": "bead", "ref": "r" * 2049},
])
def test_missing_or_malformed_provenance_is_refused_and_writes_nothing(store, prov):
    box = make(store)
    with pytest.raises(ProposalInvalid):
        propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=prov)
    assert lines(box) == []


# ------------------------------------------------------- the two gates


def test_knowledge_class_disabled_refuses_and_writes_nothing(store):
    box = make(store, classes=(CLASS_WORK,))
    with pytest.raises(OutboxRefused) as ei:
        propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert ei.value.code == "outbox_class_disabled"
    assert not box.path.exists()


def test_default_outbox_config_refuses(store):
    box = Outbox(OutboxConfig(path=str(store / "outbox.jsonl")), store_dir=store)
    with pytest.raises(OutboxRefused):
        propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert not box.path.exists()


@pytest.mark.parametrize("where", ["changes", "title", "provenance", "label"])
def test_a_fabricated_internal_name_writes_nothing(store, where):
    box = make(store)
    kw = dict(sop_id="asop-1a2b3c4d", changes=dict(CHANGES), provenance=dict(PROVENANCE),
              title=None, agent_label=None)
    leak = "Quillfeather"
    if where == "changes":
        kw["changes"]["purpose"] = f"Stop asking {leak} for sign-off."
    elif where == "title":
        kw["title"] = f"{leak} release"
    elif where == "provenance":
        kw["provenance"]["ref"] = f"{leak}/ac-1"
    else:
        kw["agent_label"] = f"{leak}-worker"
    with pytest.raises(CleanRoomRefused):
        propose_revision(box, **kw)
    assert not box.path.exists()


def test_missing_clean_room_list_is_fail_closed(tmp_path):
    box = make(tmp_path)  # no list file in the store
    with pytest.raises(clean_room.CleanRoomUnavailable):
        propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert not box.path.exists()


# ------------------------------------------- never activates; replay is free


def test_replay_is_a_noop(store):
    box = make(store)
    a = propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    b = propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert a.written and not b.written and len(lines(box)) == 1


def test_a_proposal_never_pushes_sop_activate(store):
    box = make(store)
    propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert [l["verb"] for l in lines(box)] == ["sop_revise"]


def test_there_is_no_activate_path_in_the_module():
    """Not a convention: the module cannot name the verb. It is parsed, so a
    docstring explaining the rule does not trip it, but a call would."""
    tree = ast.parse(Path(proposals.__file__).read_text())
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    docstrings = {ast.get_docstring(n) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
    code_strings = {s for s in literals if s not in docstrings}
    assert not any("activate" in s for s in code_strings)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not any("activate" in n for n in names)


def test_every_push_in_the_module_is_sop_revise(store, monkeypatch):
    seen = []
    box = make(store)
    real = box.push
    monkeypatch.setattr(box, "push", lambda verb, payload, **kw: (seen.append(verb), real(verb, payload, **kw))[1])
    propose_revision(box, sop_id="asop-1a2b3c4d", changes=CHANGES, provenance=PROVENANCE)
    assert seen == ["sop_revise"]


# ------------------------------------------------------- lesson adapter

LESSON = {
    "id": "l-7",
    "type": "proposal",
    "target": "asop:asop-1a2b3c4d#2",
    "summary": "Step 2 asks for an approval nobody can give at night.",
    "changes": CHANGES,
    "source": {"bead": "ac-0badf00d"},
}


def test_adapter_maps_a_proposal_lesson_to_propose_revision_arguments():
    args = proposal_from_lesson(LESSON)
    assert args["sop_id"] == "asop-1a2b3c4d"
    assert args["changes"] == CHANGES
    assert args["provenance"] == {"kind": "bead", "ref": "ac-0badf00d#l-7"}
    assert set(args) == {"sop_id", "changes", "provenance", "title"}


def test_adapter_output_feeds_propose_revision_directly(store):
    box = make(store)
    propose_revision(box, **proposal_from_lesson(LESSON))
    (line,) = lines(box)
    assert line["payload"]["sopId"] == "asop-1a2b3c4d"
    assert line["payload"]["provenance"]["ref"] == "ac-0badf00d#l-7"


def test_adapter_accepts_a_run_source_and_a_bare_target():
    args = proposal_from_lesson({**LESSON, "target": "asop:asop-9z", "source": {"run": "run-5"}})
    assert args["sop_id"] == "asop-9z"
    assert args["provenance"] == {"kind": "run", "ref": "run-5#l-7"}


@pytest.mark.parametrize("patch", [
    {"type": "defect"}, {"type": "fact"}, {"type": "noise"},
    {"target": "tool:scripts/x.sh"}, {"target": "garbage"}, {"target": None},
    {"changes": None}, {"changes": {}}, {"source": {}}, {"source": None},
    {"source": {"unrelated": "x"}},
])
def test_adapter_refuses_what_is_not_a_complete_procedure_proposal(patch):
    with pytest.raises(ProposalInvalid):
        proposal_from_lesson({**LESSON, **patch})
