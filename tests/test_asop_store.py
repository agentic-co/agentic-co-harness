"""The runtime's ASOP store and run filer, against ASOP.md §4, §5.1, §8."""

from __future__ import annotations

import json

import pytest
from asop import SopStatus
from asop.errors import Refusal

from agentco_harness.asop_store import AsopStore
from agentco_harness.beads import Beads, TaskStatus

DET = {"kind": "deterministic", "check": "uv run pytest -q"}
JUDGED = {"kind": "judged", "check": "criteria trace to tests", "rubric": "r1"}
HUMAN_GATE = {"kind": "human", "check": "sign off", "verifier": "dana",
              "max_park_seconds": 3600, "on_timeout": "escalate", "escalate_to": "dana"}


def feature_dev():
    return {
        "title": "Develop a feature", "task_type": "feature", "purpose": "requirement to code",
        "inputs": [{"name": "requirement"}, {"name": "repo"}],
        "roles": {"analyst": {"kind": "agent"}, "implementer": {"kind": "agent"}, "validator": {"kind": "agent"}},
        "constraints": [{"distinct": ["implementer", "validator"]}],
        "steps": [
            {"name": "validate-requirements", "role": "analyst", "purpose": "read", "gate": DET},
            {"name": "write-tests", "role": "implementer", "purpose": "tests", "gate": DET, "after": []},
            {"name": "implement", "role": "implementer", "purpose": "code", "gate": DET, "after": [1, 2]},
            {"name": "run-tests", "role": "implementer", "purpose": "prove", "gate": DET},
            {"name": "validate", "role": "validator", "purpose": "trace", "gate": JUDGED},
        ],
    }


BIND = {"analyst": "claude", "implementer": "forge", "validator": "claude"}
INPUTS = {"requirement": "REQ-1", "repo": "/tmp/repo"}


@pytest.fixture()
def store(tmp_path):
    return AsopStore(tmp_path / "asops.jsonl")


@pytest.fixture()
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


def active(store, asop_id="feature-dev"):
    rec = store.create(feature_dev(), author="m", author_kind="human", asop_id=asop_id)
    return store.activate(rec.asop_id, rec.version, by_kind="human")


# ----------------------------------------------------------------- lifecycle (§4)

def test_create_is_v1_draft_and_a_draft_cannot_run(store, beads):
    rec = store.create(feature_dev(), author="m", author_kind="human", asop_id="feature-dev")
    assert (rec.version, rec.status) == (1, SopStatus.DRAFT)
    with pytest.raises(Refusal) as e:
        store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads)
    assert e.value.code == "sop_refused" and "no active version" in e.value.message


def test_activate_then_revise_then_activate_supersedes(store):
    v1 = active(store)
    v2 = store.revise("feature-dev", {"purpose": "sharper"}, author="m", author_kind="human")
    assert (v2.version, v2.status) == (2, SopStatus.DRAFT)
    assert store.get("feature-dev").version == 1          # v2 is a draft; v1 still active
    store.activate("feature-dev", 2, by_kind="human")
    hist = store.history("feature-dev")
    assert [(r.version, r.status) for r in hist] == [(1, SopStatus.SUPERSEDED), (2, SopStatus.ACTIVE)]
    assert hist[0].superseded_by == 2
    assert store.get("feature-dev", 1) is not None         # pins stay resolvable forever


def test_retire_keeps_the_record_and_refuses_new_runs(store, beads):
    active(store)
    store.retire("feature-dev", by_kind="human")
    assert store.history("feature-dev")[0].status is SopStatus.RETIRED
    with pytest.raises(Refusal) as e:
        store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads)
    assert e.value.code == "sop_refused"


def test_retire_is_a_human_verb(store):
    """Decision 3, rule 4: `retire` is refused to an agent whatever it would do.

    Activation is NOT here any more — §8.1 makes it "human, or agent under
    policy", and the policy is what `test_revision_policy.py` covers.
    """
    store.create(feature_dev(), author="m", author_kind="human", asop_id="feature-dev")
    store.activate("feature-dev", 1, by_kind="human")
    with pytest.raises(Refusal) as e:
        store.retire("feature-dev", by_kind="agent")
    assert e.value.code == "revision_policy:human_only"


def test_a_malformed_body_is_sop_refused(store):
    body = feature_dev(); del body["steps"][0]["gate"]
    with pytest.raises(Refusal) as e:
        store.create(body, author="m", author_kind="human")
    assert e.value.code == "sop_refused" and "gate" in e.value.message


def test_an_unreadable_line_is_quarantined_not_dropped_and_blocks_revise(store, tmp_path):
    active(store)
    with open(store.path, "a") as f:
        f.write(json.dumps({"asop_id": "feature-dev", "version": 7, "title": "x", "status": "archived", "steps": []}) + "\n")
    assert store.get("feature-dev").version == 1
    assert len(store.quarantined) == 1
    with pytest.raises(Refusal) as e:
        store.revise("feature-dev", {"purpose": "p"}, author="m", author_kind="human")
    assert "cannot read" in e.value.message
    store.retire("feature-dev", by_kind="human")           # a rewrite carries the line
    assert sum(1 for l in store.path.read_text().splitlines() if '"archived"' in l) == 1


# ----------------------------------------------------------------- filing a run (§5.1)

def test_run_files_a_tree_pinned_per_step_with_after_as_blocked_by(store, beads):
    active(store)
    parent = store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads)
    assert parent.metadata["sop_ref"] == {"asop_id": "feature-dev", "version": 1}
    assert parent.metadata["run"] == {"inputs": INPUTS, "bindings": BIND}
    kids = sorted((t for t in beads.list() if t.parent_id == parent.id), key=lambda t: t.metadata["sop_ref"]["step"])
    assert [k.metadata["sop_ref"]["step"] for k in kids] == [1, 2, 3, 4, 5]
    ids = {k.metadata["sop_ref"]["step"]: k.id for k in kids}
    assert kids[0].blocked_by == [] and kids[1].blocked_by == []        # parallel start
    assert sorted(kids[2].blocked_by) == sorted([ids[1], ids[2]])       # explicit join
    assert kids[3].blocked_by == [ids[3]]                               # default: previous
    assert kids[4].blocked_by == [ids[4]]
    # each child carries a COPY of its step's text and its gate — the run supplied none
    assert kids[4].metadata["step"]["name"] == "validate"
    assert kids[4].metadata["verify"]["kind"] == "judged"
    assert kids[0].assigned_agent == "claude" and kids[2].assigned_agent == "forge"
    # the parent is held open by its children
    assert set(beads.get(parent.id).blocked_by) == set(ids.values())


def test_missing_inputs_refuse_before_anything_is_filed(store, beads):
    active(store)
    with pytest.raises(Refusal) as e:
        store.run("feature-dev", inputs={"repo": "/x"}, bindings=BIND, beads=beads)
    assert e.value.code == "inputs_missing" and "requirement" in e.value.message
    assert beads.list() == []


def test_unbound_role_refuses(store, beads):
    active(store)
    with pytest.raises(Refusal) as e:
        store.run("feature-dev", inputs=INPUTS, bindings={"analyst": "claude", "implementer": "forge"}, beads=beads)
    assert e.value.code == "role_unbound" and "validator" in e.value.message
    assert beads.list() == []


def test_a_distinct_constraint_refuses_one_actor_in_both_roles(store, beads):
    active(store)
    with pytest.raises(Refusal) as e:
        store.run("feature-dev", inputs=INPUTS, bindings={**BIND, "validator": "forge"}, beads=beads)
    assert e.value.code == "constraint_unsatisfiable"
    assert beads.list() == []


def test_a_human_role_files_a_human_assigned_bead(store, beads):
    body = feature_dev()
    body["roles"]["validator"] = {"kind": "human"}
    body["steps"][4]["gate"] = HUMAN_GATE
    rec = store.create(body, author="m", author_kind="human", asop_id="feature-dev")
    store.activate(rec.asop_id, 1, by_kind="human")
    parent = store.run("feature-dev", inputs=INPUTS, bindings={**BIND, "validator": "dana"}, beads=beads)
    last = max((t for t in beads.list() if t.parent_id == parent.id), key=lambda t: t.metadata["sop_ref"]["step"])
    assert last.assigned_to == "human:dana" and last.assigned_agent is None
    assert last.metadata["verify"]["kind"] == "human"


def test_an_explicit_version_must_exist_and_be_active(store, beads):
    active(store)
    with pytest.raises(Refusal) as e:
        store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads, version=9)
    assert e.value.code == "version_required"


# ----------------------------------------------------------------- nesting (§3.5)

def test_a_nested_step_files_the_inner_tree_pinned_to_the_inner_version(store, beads):
    inner = store.create({"title": "Release", "roles": {"releaser": {"kind": "agent"}},
                          "steps": [{"name": "tag", "role": "releaser", "purpose": "tag it", "gate": DET},
                                    {"name": "publish", "role": "releaser", "purpose": "push it", "gate": DET}]},
                         author="m", author_kind="human", asop_id="release")
    store.activate("release", 1, by_kind="human")
    outer = feature_dev()
    outer["steps"].append({"name": "release", "uses": {"asop_id": "release", "version": 1}})
    store.create(outer, author="m", author_kind="human", asop_id="feature-dev")
    store.activate("feature-dev", 1, by_kind="human")
    parent = store.run("feature-dev", inputs=INPUTS, bindings={**BIND, "releaser": "claude"}, beads=beads)
    kids = {t.metadata["sop_ref"].get("step"): t for t in beads.list() if t.parent_id == parent.id}
    container = kids[6]
    assert container.metadata["uses"] == {"asop_id": "release", "version": 1}
    inner_kids = sorted((t for t in beads.list() if t.parent_id == container.id), key=lambda t: t.metadata["sop_ref"]["step"])
    assert [t.metadata["sop_ref"] for t in inner_kids] == [
        {"asop_id": "release", "version": 1, "step": 1}, {"asop_id": "release", "version": 1, "step": 2}]
    assert inner_kids[1].blocked_by == [inner_kids[0].id]


def test_nesting_past_three_deep_is_decomposition_bound(store, beads):
    leaf = {"title": "Leaf", "roles": {"r": {"kind": "agent"}},
            "steps": [{"name": "do", "role": "r", "purpose": "p", "gate": DET}]}
    store.create(leaf, author="m", author_kind="human", asop_id="d3"); store.activate("d3", 1, by_kind="human")
    for outer_id, inner_id in (("d2", "d3"), ("d1", "d2"), ("d0", "d1")):
        store.create({"title": outer_id, "roles": {"r": {"kind": "agent"}},
                      "steps": [{"name": "nest", "uses": {"asop_id": inner_id, "version": 1}}]},
                     author="m", author_kind="human", asop_id=outer_id)
        store.activate(outer_id, 1, by_kind="human")
    with pytest.raises(Refusal) as e:
        store.run("d0", inputs={}, bindings={"r": "claude"}, beads=beads)
    assert e.value.code == "decomposition_bound"


# ----------------------------------------------------------------- drift (§2.1)

def test_drifted_reports_a_run_whose_procedure_moved_on(store, beads):
    active(store)
    parent = store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads)
    assert store.drifted(parent) is False
    store.revise("feature-dev", {"purpose": "new"}, author="m", author_kind="human")
    store.activate("feature-dev", 2, by_kind="human")
    assert store.drifted(beads.get(parent.id)) is True
    assert beads.get(parent.id).metadata["sop_ref"]["version"] == 1   # the pin did not move


# ----------------------------------------------------------------- CLI

def test_cli_create_activate_run(tmp_path, monkeypatch):
    import yaml
    from click.testing import CliRunner
    from agentco_harness.cli import main
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (tmp_path / "fd.yaml").write_text(yaml.safe_dump(feature_dev()))
    runner = CliRunner()
    cfg = str(tmp_path / "config.yaml")
    r = runner.invoke(main, ["-c", cfg, "sop", "create", str(tmp_path / "fd.yaml"), "--author", "m", "--id", "feature-dev"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["-c", cfg, "sop", "activate", "feature-dev", "1"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["-c", cfg, "sop", "run", "feature-dev", "--input", "requirement=R", "--input", "repo=/r",
                             "--bind", "analyst=claude", "--bind", "implementer=forge", "--bind", "validator=claude"])
    assert r.exit_code == 0, r.output
    assert "5 step bead(s)" in r.output
    r = runner.invoke(main, ["-c", cfg, "sop", "run", "feature-dev", "--input", "repo=/r",
                             "--bind", "analyst=claude", "--bind", "implementer=forge", "--bind", "validator=claude"])
    assert r.exit_code != 0 and "inputs_missing" in r.output


# ----------------------------------------------------------------- §5.5 auto-close (decision A, 2026-09-04)

OK = {"kind": "deterministic", "check": "true"}     # a gate the runtime can pass on its own


def _kids(beads, parent_id):
    return sorted((t for t in beads.list() if t.parent_id == parent_id),
                  key=lambda t: t.metadata["sop_ref"]["step"])


def passing_feature_dev():
    """The running example with every gate passable by the completing process.
    The runtime runs a deterministic check at complete(); `uv run pytest -q`
    in a temp dir exits 5, and a judged gate cannot be self-completed at all."""
    body = feature_dev()
    for st in body["steps"]:
        st["gate"] = OK
    return body


def test_the_parent_closes_when_the_last_step_lands_done(store, beads):
    store.create(passing_feature_dev(), author="m", author_kind="human", asop_id="feature-dev")
    store.activate("feature-dev", 1, by_kind="human")
    parent = store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads)
    kids = _kids(beads, parent.id)
    for k in kids[:-1]:
        beads.claim(k.id, k.assigned_agent); beads.complete(k.id, result="ok")
        assert beads.get(parent.id).status is not TaskStatus.DONE     # not yet
    beads.claim(kids[-1].id, kids[-1].assigned_agent); beads.complete(kids[-1].id, result="ok")
    closed = beads.get(parent.id)
    assert closed.status is TaskStatus.DONE
    assert closed.metadata["run_closed_at"]
    assert [r["step"] for r in closed.metadata["run_review"]["steps"]] == [1, 2, 3, 4, 5]


def test_a_verify_failed_sibling_holds_the_parent_open(store, beads):
    body = passing_feature_dev(); body["steps"][3]["gate"] = {"kind": "deterministic", "check": "false"}
    rec = store.create(body, author="m", author_kind="human", asop_id="feature-dev")
    store.activate("feature-dev", 1, by_kind="human")
    parent = store.run("feature-dev", inputs=INPUTS, bindings=BIND, beads=beads)
    kids = _kids(beads, parent.id)
    for k in kids:
        beads.claim(k.id, k.assigned_agent); beads.complete(k.id, result="ok")
    assert beads.get(kids[3].id).status is TaskStatus.VERIFY_FAILED     # `false` exits 1
    assert beads.get(parent.id).status is not TaskStatus.DONE


def test_a_nested_container_closes_and_propagates_upward(store, beads):
    inner = {"title": "Release", "roles": {"releaser": {"kind": "agent"}},
             "steps": [{"name": "tag", "role": "releaser", "purpose": "p", "gate": OK},
                       {"name": "publish", "role": "releaser", "purpose": "p", "gate": OK}]}
    store.create(inner, author="m", author_kind="human", asop_id="release"); store.activate("release", 1, by_kind="human")
    outer = {"title": "Ship", "roles": {"dev": {"kind": "agent"}},
             "steps": [{"name": "build", "role": "dev", "purpose": "p", "gate": OK},
                       {"name": "release", "uses": {"asop_id": "release", "version": 1}}]}
    store.create(outer, author="m", author_kind="human", asop_id="ship"); store.activate("ship", 1, by_kind="human")
    parent = store.run("ship", inputs={}, bindings={"dev": "claude", "releaser": "claude"}, beads=beads)
    build, container = _kids(beads, parent.id)
    beads.claim(build.id, "claude"); beads.complete(build.id, result="ok")
    for k in _kids(beads, container.id):
        beads.claim(k.id, "claude"); beads.complete(k.id, result="ok")
    assert beads.get(container.id).status is TaskStatus.DONE      # inner steps done → container done
    assert beads.get(parent.id).status is TaskStatus.DONE         # → and the run
