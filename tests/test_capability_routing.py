"""Routing a bead by what its executor can DO, not only by who it names.

`requires` already said what the executing MACHINE must be able to do. This is
the same question asked of the MODEL, and it is the half that lets an ASOP step
say "this one needs a shell" and have the runtime honour it.
"""

from __future__ import annotations

import pytest

from agentco_harness import backends
from agentco_harness.beads import DISPATCH_REFUSAL_KEY, Beads, TaskStatus
from agentco_harness.completion import COMPLETION_ONLY
from agentco_harness.config import Config
from agentco_harness.orchestrator import Orchestrator


@pytest.fixture
def orch(tmp_path):
    cfg = Config()
    cfg.tasks_path = str(tmp_path / "tasks.jsonl")
    cfg.config_path = str(tmp_path / "config.yaml")
    return Orchestrator(cfg)


def _dispatch(orch, task):
    backend = backends.resolve(task.assigned_agent)
    return orch._capability_gap_ok(task, backend)


def test_a_text_shaped_bead_routes_to_a_completion_backend(orch):
    t = orch.beads.create("summarise the run", "", assigned_agent="lmstudio", requires=["text"])
    assert _dispatch(orch, t) is True


def test_a_bead_that_needs_a_shell_is_refused_by_a_completion_backend(orch):
    """The failure this whole seam exists to prevent: a model that can only
    talk, handed work that needs a working tree, failing four minutes later."""
    t = orch.beads.create("implement slugify", "", assigned_agent="lmstudio",
                          requires=["shell", "files"])
    assert _dispatch(orch, t) is False
    after = orch.beads.get(t.id)
    assert after.status is TaskStatus.PENDING
    refusal = after.metadata[DISPATCH_REFUSAL_KEY]
    assert refusal["code"] == "requires_unsatisfied"
    assert "shell" in refusal["message"] and "files" in refusal["message"]
    assert "provides ['text']" in refusal["message"]
    assert t.id not in [r.id for r in orch.beads.ready()]


def test_it_is_blocked_not_failed(orch):
    """Same call the egress gate makes: the work is fine, the routing is not."""
    t = orch.beads.create("implement it", "", assigned_agent="zai-api", requires=["files"])
    _dispatch(orch, t)
    assert orch.beads.get(t.id).status is not TaskStatus.FAILED


def test_a_bead_with_no_requirements_routes_anywhere(orch):
    t = orch.beads.create("say something", "", assigned_agent="lmstudio")
    assert _dispatch(orch, t) is True


def test_an_undeclared_backend_is_treated_as_agentic(orch):
    """The four shipped executors declare nothing. Downgrading them silently
    would be a worse bug than the one this gate prevents."""
    t = orch.beads.create("implement it", "", assigned_agent="claude",
                          requires=["shell", "files"])
    assert backends.resolve("claude").capabilities == frozenset()
    assert _dispatch(orch, t) is True


def test_the_node_manifest_can_supply_what_the_backend_lacks(orch):
    """`requires` mixes two kinds of capability — what the MACHINE has (an
    ADO token) and what the MODEL can do. A machine capability must not be
    read as a model capability the backend is missing."""
    orch.config.capabilities = ["ado-write"]
    t = orch.beads.create("post the summary", "", assigned_agent="lmstudio",
                          requires=["text", "ado-write"])
    assert _dispatch(orch, t) is True


def test_the_gate_names_only_what_is_actually_missing(orch):
    orch.config.capabilities = ["ado-write"]
    t = orch.beads.create("do it", "", assigned_agent="lmstudio",
                          requires=["text", "ado-write", "shell"])
    _dispatch(orch, t)
    result = orch.beads.get(t.id).metadata[DISPATCH_REFUSAL_KEY]["message"]
    assert "['shell']" in result
    assert "ado-write" not in result.split("provides")[0].split("requires")[1]


def test_local_execution_is_not_gated_on_a_vendor_policy_artifact(tmp_path):
    """LOCAL means the bytes never leave the machine, so there is no boundary
    for a ceiling to bound — and a node with no policy artifact must still be
    able to use its own hardware."""
    from agentco_harness.egress import check_egress

    data_class, route = check_egress(
        "lmstudio", {}, routes_path=str(tmp_path / "absent.json"),
        store_dir=str(tmp_path), supervised=False,
    )
    assert route is None          # nothing egressed, nothing to bound


def test_a_vendor_completion_backend_is_still_gated(tmp_path):
    """z.ai is a third party. Local hardware is not a licence for the network."""
    from agentco_harness.egress import PolicyUnavailable, check_egress

    with pytest.raises(PolicyUnavailable):
        check_egress("zai-api", {}, routes_path=str(tmp_path / "absent.json"),
                     store_dir=str(tmp_path), supervised=False)
