"""The revision policy, enforced by the runtime's store (ASOP.md §6.4, §8.1).

Before this file the runtime made `activate` human-only outright, with a
comment saying the contract allows an agent to activate "under policy" and
the policy was not ported. It is now: `asop.revision` holds the four rules,
the coordination plane calls the same functions, and `AsopStore` refuses with
the same `revision_policy:<rule>` codes.

What each test pins is the RULE, not the wording. Every refusal here is
asserted on `Refusal.code`, because the code is what a caller branches on and
the message is prose that may improve.

The four, and where each is exercised below:

1. **protected** — a `money` or `irreversible` step freezes the whole
   procedure against agents, revision and activation alike, deletion
   included. Adding or removing such a tag is itself refused.
2. **ratchet** — a human step never becomes an agent step at an agent's hand,
   and deleting it is the same demotion. On a FIRST activation the ratchet is
   absolute: baseline and target are the same version, so a differential
   check would be vacuous.
3. **no-undo** — an agent may not move a field back to a state a human moved
   it away from, until a human moves it back.
4. **human_only** — `retire` is a human verb whatever it would produce.

A human passes all four. That asymmetry is the point of every "…_a_human_may_"
test here: these rules are about trust domains, not about what a change says.
"""

from __future__ import annotations

import pytest
from asop import SopStatus
from asop.errors import Refusal

from agentco_harness.asop_store import AsopStore

DET = {"kind": "deterministic", "check": "uv run pytest -q"}
HUMAN_GATE = {"kind": "human", "check": "the owner signs off", "verifier": "dana",
              "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}


@pytest.fixture()
def store(tmp_path):
    return AsopStore(tmp_path / "asops.jsonl")


def payment(steps=None, **over):
    """A procedure with a role of each kind, so any step shape is expressible."""
    body = {
        "title": "Pay a supplier",
        "task_type": "payment",
        "purpose": "Settle an approved invoice.",
        "inputs": [{"name": "invoice"}],
        "roles": {"clerk": {"kind": "agent"}, "owner": {"kind": "human"}},
        "steps": steps or [step("check-total"), step("pay")],
    }
    body.update(over)
    return body


def step(name, *, role="clerk", gate=DET, **over):
    out = {"name": name, "role": role, "purpose": f"{name}, properly", "gate": gate}
    out.update(over)
    return out


MONEY = step("pay", role="owner", gate=HUMAN_GATE, tags=["money"])


def live(store, steps=None, asop_id="pay", **over):
    """A procedure a person wrote and put into service."""
    rec = store.create(payment(steps, **over), author="dana", author_kind="human", asop_id=asop_id)
    store.activate(asop_id, rec.version, by_kind="human")
    return rec


def refused(caught, rule):
    assert caught.value.code == f"revision_policy:{rule}"


# --------------------------------------------------------------------------- #
# rule 1 — protected tags
# --------------------------------------------------------------------------- #


def test_an_agent_may_not_revise_a_procedure_carrying_a_money_step(store):
    live(store, [step("check-total"), MONEY])
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"purpose": "settle it faster"}, author="bot", author_kind="agent")
    refused(caught, "protected")


def test_a_human_may_revise_the_very_same_procedure(store):
    live(store, [step("check-total"), MONEY])
    revised = store.revise("pay", {"purpose": "settle it faster"}, author="dana", author_kind="human")
    assert revised.version == 2 and revised.purpose == "settle it faster"


def test_the_freeze_covers_the_steps_around_the_money_one(store):
    """Changing what runs around a `money` step changes what that step does."""
    live(store, [step("check-total"), MONEY])
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": [step("check-total", purpose="skim it"), MONEY]},
                     author="bot", author_kind="agent")
    refused(caught, "protected")


def test_an_agent_may_not_delete_a_money_step(store):
    live(store, [step("check-total"), MONEY])
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": [step("check-total")]}, author="bot", author_kind="agent")
    refused(caught, "protected")


def test_an_agent_may_not_add_a_protected_tag(store):
    live(store)
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": [step("check-total"), MONEY]},
                     author="bot", author_kind="agent")
    refused(caught, "protected")


def test_an_agent_may_not_activate_a_draft_carrying_a_money_step(store):
    """The door beside the policy: a draft a human wrote is still protected."""
    live(store)
    store.revise("pay", {"steps": [step("check-total"), MONEY]}, author="dana", author_kind="human")
    with pytest.raises(Refusal) as caught:
        store.activate("pay", 2, by_kind="agent")
    refused(caught, "protected")
    assert store.get("pay").version == 1                     # nothing moved
    assert store.activate("pay", 2, by_kind="human").status is SopStatus.ACTIVE


def test_a_refused_revision_writes_nothing(store):
    live(store, [step("check-total"), MONEY])
    before = (store.path).read_bytes()
    with pytest.raises(Refusal):
        store.revise("pay", {"purpose": "faster"}, author="bot", author_kind="agent")
    assert store.path.read_bytes() == before
    assert [r.version for r in store.history("pay")] == [1]


# --------------------------------------------------------------------------- #
# rule 1 — the registry may ADD a protected tag, and never remove the defaults
# --------------------------------------------------------------------------- #


def test_a_registry_adds_a_protected_tag_through_the_same_env_var(monkeypatch, tmp_path):
    """`AGENTCO_PROTECTED_TAGS` — the plane's variable, the plane's semantics."""
    plain = AsopStore(tmp_path / "plain.jsonl")
    live(plain, [step("collect", gate=HUMAN_GATE, role="owner", tags=["pii"])], asop_id="p")
    plain.revise("p", {"purpose": "faster"}, author="bot", author_kind="agent")   # not protected

    monkeypatch.setenv("AGENTCO_PROTECTED_TAGS", "pii")
    guarded = AsopStore(tmp_path / "guarded.jsonl")
    assert "pii" in guarded.protected_tags
    live(guarded, [step("collect", gate=HUMAN_GATE, role="owner", tags=["pii"])], asop_id="p")
    with pytest.raises(Refusal) as caught:
        guarded.revise("p", {"purpose": "faster"}, author="bot", author_kind="agent")
    refused(caught, "protected")


def test_the_defaults_survive_any_declaration(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCO_PROTECTED_TAGS", "pii")
    store = AsopStore(tmp_path / "a.jsonl")
    assert {"money", "irreversible"} <= store.protected_tags


# --------------------------------------------------------------------------- #
# rule 2 — the class ratchets toward human
# --------------------------------------------------------------------------- #


def test_an_agent_may_not_demote_a_human_gate(store):
    live(store, [step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)])
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": [step("check-total"), step("sign")]},
                     author="bot", author_kind="agent")
    refused(caught, "ratchet")


def test_a_human_may_demote_the_very_same_gate(store):
    live(store, [step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)])
    revised = store.revise("pay", {"steps": [step("check-total"), step("sign")]},
                           author="dana", author_kind="human")
    assert revised.steps[1].gate["kind"] == "deterministic"


def test_deleting_a_human_step_is_the_same_demotion(store):
    live(store, [step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)])
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": [step("check-total")]}, author="bot", author_kind="agent")
    refused(caught, "ratchet")


def test_an_agent_may_ratchet_toward_human(store):
    """The rule has a direction. Making a step human is what agents may do."""
    live(store)
    revised = store.revise(
        "pay", {"steps": [step("check-total"), step("pay", role="owner", gate=HUMAN_GATE)]},
        author="bot", author_kind="agent",
    )
    assert revised.steps[1].gate["kind"] == "human"


def test_an_agent_may_not_activate_a_draft_that_demotes_a_human_step(store):
    live(store, [step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)])
    store.revise("pay", {"steps": [step("check-total"), step("sign")]},
                 author="dana", author_kind="human")
    with pytest.raises(Refusal) as caught:
        store.activate("pay", 2, by_kind="agent")
    refused(caught, "ratchet")
    assert store.activate("pay", 2, by_kind="human").version == 2


# --------------------------------------------------------------------------- #
# rule 2, absolute — the first activation
# --------------------------------------------------------------------------- #


def test_an_agent_may_not_put_a_never_active_human_step_into_service(store):
    """No version has ever been active, so every differential rule is vacuous:
    baseline and target are the same version and nothing 'changed'. The
    absolute form is the only thing standing in the door."""
    store.create(payment([step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)]),
                 author="dana", author_kind="human", asop_id="pay")
    with pytest.raises(Refusal) as caught:
        store.activate("pay", 1, by_kind="agent")
    refused(caught, "ratchet")


def test_a_human_activates_that_same_first_version(store):
    store.create(payment([step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)]),
                 author="dana", author_kind="human", asop_id="pay")
    assert store.activate("pay", 1, by_kind="human").status is SopStatus.ACTIVE


def test_a_first_activation_names_money_rather_than_the_human_gate_it_implies(store):
    """A protected step must be human-gated, so both rules fire on it. The one
    that answers WHY has to be the one that speaks."""
    store.create(payment([step("check-total"), MONEY]),
                 author="dana", author_kind="human", asop_id="pay")
    with pytest.raises(Refusal) as caught:
        store.activate("pay", 1, by_kind="agent")
    refused(caught, "protected")


def test_an_agent_may_activate_an_all_agent_procedure(store):
    """The capability this port adds. Before it, `activate` was human-only and
    an unattended runtime could not put its own drafted procedure into
    service; §8.1 says "human, or agent under policy", and this is a draft the
    policy has nothing to say about."""
    store.create(payment(), author="bot", author_kind="agent", asop_id="pay")
    assert store.activate("pay", 1, by_kind="agent").status is SopStatus.ACTIVE


# --------------------------------------------------------------------------- #
# rule 3 — no undoing a human
# --------------------------------------------------------------------------- #


def with_mistakes(mistakes):
    """An empty list is refused by the record contract — it is the claim that
    the work has no known failure modes — so 'none' means omit the key."""
    first = step("check-total", **({"common_mistakes": mistakes} if mistakes else {}))
    return [first, step("pay")]


def test_an_agent_may_not_put_back_what_a_human_removed(store):
    live(store, with_mistakes(["trusting the invoice total", "paying on a Friday"]))
    store.revise("pay", {"steps": with_mistakes(["paying on a Friday"])},
                 author="dana", author_kind="human")
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": with_mistakes(["paying on a Friday", "trusting the invoice total"])},
                     author="bot", author_kind="agent")
    refused(caught, "no-undo")


def test_an_agent_may_not_remove_what_a_human_added(store):
    live(store, with_mistakes(["paying on a Friday"]))
    store.revise("pay", {"steps": with_mistakes(["paying on a Friday", "trusting the invoice total"])},
                 author="dana", author_kind="human")
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"steps": with_mistakes(["paying on a Friday"])},
                     author="bot", author_kind="agent")
    refused(caught, "no-undo")


def test_an_agent_may_not_restore_a_scalar_a_human_replaced(store):
    live(store)
    store.revise("pay", {"purpose": "Settle an invoice a person has approved."},
                 author="dana", author_kind="human")
    with pytest.raises(Refusal) as caught:
        store.revise("pay", {"purpose": "Settle an approved invoice."},
                     author="bot", author_kind="agent")
    refused(caught, "no-undo")


def test_a_human_may_undo_a_human(store):
    live(store, with_mistakes(["trusting the invoice total", "paying on a Friday"]))
    store.revise("pay", {"steps": with_mistakes(["paying on a Friday"])},
                 author="dana", author_kind="human")
    restored = store.revise("pay", {"steps": with_mistakes(["paying on a Friday", "trusting the invoice total"])},
                            author="sam", author_kind="human")
    assert set(restored.steps[0].common_mistakes) == {"paying on a Friday", "trusting the invoice total"}


def test_a_later_human_word_lifts_the_ban(store):
    """The rule is about not undoing people, not about freezing the past."""
    live(store, with_mistakes(["trusting the invoice total"]))
    store.revise("pay", {"steps": with_mistakes([])}, author="dana", author_kind="human")
    store.revise("pay", {"steps": with_mistakes(["trusting the invoice total"])},
                 author="dana", author_kind="human")
    kept = store.revise("pay", {"steps": with_mistakes(["trusting the invoice total"])},
                        author="bot", author_kind="agent")
    assert kept.steps[0].common_mistakes == ["trusting the invoice total"]


def test_moving_a_field_somewhere_new_is_not_an_undo(store):
    live(store)
    store.revise("pay", {"purpose": "Settle an invoice a person has approved."},
                 author="dana", author_kind="human")
    moved = store.revise("pay", {"purpose": "Settle an invoice, then reconcile it."},
                         author="bot", author_kind="agent")
    assert moved.purpose == "Settle an invoice, then reconcile it."


def test_an_agent_may_undo_an_agent(store):
    """The rule is about not undoing PEOPLE. An agent's own earlier version
    forbids nothing, or the loop could never revisit its own work."""
    store.create(payment(with_mistakes(["trusting the invoice total"])),
                 author="bot", author_kind="agent", asop_id="pay")
    store.activate("pay", 1, by_kind="human")
    store.revise("pay", {"steps": with_mistakes([])}, author="bot", author_kind="agent")
    back = store.revise("pay", {"steps": with_mistakes(["trusting the invoice total"])},
                        author="bot", author_kind="agent")
    assert back.steps[0].common_mistakes == ["trusting the invoice total"]


# --------------------------------------------------------------------------- #
# rule 4 — human verbs
# --------------------------------------------------------------------------- #


def test_retire_is_refused_to_an_agent(store):
    live(store)
    with pytest.raises(Refusal) as caught:
        store.retire("pay", by_kind="agent")
    refused(caught, "human_only")
    assert store.get("pay").status is SopStatus.ACTIVE


def test_retire_is_allowed_to_a_human(store):
    live(store)
    assert store.retire("pay", by_kind="human").status is SopStatus.RETIRED


def test_an_undeclared_caller_is_refused_before_the_policy_sees_it(store):
    """A misspelled kind is a refusal, not a `ValueError` out of the policy."""
    live(store)
    for call in (lambda: store.activate("pay", 1, by_kind="huamn"),
                 lambda: store.retire("pay", by_kind=None),
                 lambda: store.revise("pay", {}, author="x", author_kind="operator")):
        with pytest.raises(Refusal) as caught:
            call()
        assert caught.value.code == "sop_refused"


# --------------------------------------------------------------------------- #
# who is human — the same declaration the plane reads
# --------------------------------------------------------------------------- #


def cli(tmp_path, *args):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    cfg = tmp_path / "config.yaml"
    if not cfg.exists():
        cfg.write_text(f"tasks_path: tasks.jsonl\nasops_path: {tmp_path / 'asops.jsonl'}\n")
    return CliRunner().invoke(main, ["-c", str(cfg), "sop", *args])


def write_body(tmp_path, body):
    import yaml

    path = tmp_path / "asop.yaml"
    path.write_text(yaml.safe_dump(body))
    return str(path)


def test_a_declared_registry_resolves_the_kind_from_agentco_humans(tmp_path, monkeypatch):
    """`--by-kind human` is an assertion until the operator declares who is."""
    body = write_body(tmp_path, payment([step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)]))
    monkeypatch.setenv("AGENTCO_HUMANS", "dana,sam")
    assert cli(tmp_path, "create", body, "--author", "dana", "--id", "pay").exit_code == 0

    claimed = cli(tmp_path, "activate", "pay", "1", "--by", "bot", "--by-kind", "human")
    assert claimed.exit_code != 0 and "revision_policy:ratchet" in claimed.output

    real = cli(tmp_path, "activate", "pay", "1", "--by", "dana", "--by-kind", "agent")
    assert real.exit_code == 0, real.output


def test_an_undeclared_registry_leaves_the_terminal_operator_their_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCO_HUMANS", raising=False)
    body = write_body(tmp_path, payment([step("check-total"), step("sign", role="owner", gate=HUMAN_GATE)]))
    assert cli(tmp_path, "create", body, "--author", "dana", "--id", "pay").exit_code == 0
    assert cli(tmp_path, "activate", "pay", "1").exit_code == 0
