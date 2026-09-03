"""Egress data-classification gate (ISC-113..121).

The gate is the only thing standing between an unattended overnight cycle and
shipping company data to a PUBLIC-ceiling vendor. Every branch of it is tested
here, including the fail-closed paths — a gate that silently allows on error is
worse than no gate, because it reads as protection.
"""

import json

import pytest

from agentco_harness.egress import (
    AGENT_ROUTE,
    EgressDenied,
    PolicyUnavailable,
    check_egress,
    load_routes,
    resolve_data_class,
)
from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import Config
from agentco_harness.orchestrator import Orchestrator

# ---------------------------------------------------------------- fixtures


def write_routes(tmp_path, monkeypatch, routes=None, schema=1):
    """Materialize a policy artifact and point the module at it."""
    payload = {
        "schemaVersion": schema,
        "classRank": {"RESTRICTED": 0, "CONFIDENTIAL": 1, "INTERNAL": 2, "PUBLIC": 3},
        "routes": routes
        if routes is not None
        else {
            "NATIVE": {
                "vendor": "anthropic", "model": "claude", "ceiling": "RESTRICTED",
                "ceilingUnsupervised": "RESTRICTED", "ceilingVerified": True,
            },
            "TEMPER": {
                "vendor": "z.ai", "model": "glm-4.7", "ceiling": "PUBLIC",
                "ceilingUnsupervised": "PUBLIC", "ceilingVerified": True,
            },
            "FORGE": {
                "vendor": "openai", "model": "gpt-5.6-sol", "ceiling": "RESTRICTED",
                "ceilingUnsupervised": "RESTRICTED", "ceilingVerified": True,
            },
        },
    }
    p = tmp_path / "inference-routes.json"
    p.write_text(json.dumps(payload))
    monkeypatch.setenv("LIFEOS_INFERENCE_ROUTES", str(p))
    return p


# ------------------------------------------------------- resolve_data_class


def test_explicit_data_class_wins_over_company_default():
    assert resolve_data_class({"company": "frontsteps", "data_class": "PUBLIC"}) == "PUBLIC"


def test_personal_defaults_to_internal():
    assert resolve_data_class({"company": "personal"}) == "INTERNAL"


def test_missing_company_defaults_to_internal():
    assert resolve_data_class({}) == "INTERNAL"


def test_real_company_defaults_to_confidential():
    assert resolve_data_class({"company": "sommeli"}) == "CONFIDENTIAL"


def test_unknown_data_class_raises_rather_than_downgrading():
    # A typo must never silently resolve to something permissive.
    with pytest.raises(EgressDenied, match="unknown data_class"):
        resolve_data_class({"data_class": "pubic"})


# -------------------------------------------------------------- load_routes


def test_missing_artifact_raises_policy_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_INFERENCE_ROUTES", str(tmp_path / "nope.json"))
    with pytest.raises(PolicyUnavailable, match="not found"):
        load_routes()


def test_unsupported_schema_raises(tmp_path, monkeypatch):
    write_routes(tmp_path, monkeypatch, schema=99)
    with pytest.raises(PolicyUnavailable, match="schemaVersion"):
        load_routes()


def test_malformed_json_raises(tmp_path, monkeypatch):
    p = tmp_path / "inference-routes.json"
    p.write_text("{not json")
    monkeypatch.setenv("LIFEOS_INFERENCE_ROUTES", str(p))
    with pytest.raises(PolicyUnavailable, match="unreadable"):
        load_routes()


# -------------------------------------------------------------- check_egress


def test_confidential_bead_denied_to_zai(tmp_path, monkeypatch):
    write_routes(tmp_path, monkeypatch)
    with pytest.raises(EgressDenied, match="exceeds the PUBLIC ceiling"):
        check_egress("zai", {"company": "frontsteps"})


def test_public_bead_allowed_to_zai(tmp_path, monkeypatch):
    write_routes(tmp_path, monkeypatch)
    data_class, route = check_egress("zai", {"company": "personal", "data_class": "PUBLIC"})
    assert data_class == "PUBLIC"
    assert route.vendor == "z.ai"


def test_confidential_bead_allowed_to_native(tmp_path, monkeypatch):
    write_routes(tmp_path, monkeypatch)
    data_class, route = check_egress("claude", {"company": "frontsteps"})
    assert (data_class, route.vendor) == ("CONFIDENTIAL", "anthropic")


def test_unknown_agent_is_denied(tmp_path, monkeypatch):
    write_routes(tmp_path, monkeypatch)
    with pytest.raises(EgressDenied, match="no declared egress route"):
        check_egress("some-new-vendor", {"company": "personal"})


def test_missing_policy_denies_non_native_but_allows_native(tmp_path, monkeypatch):
    """Fail-closed for vendors; native stays up so a missing advisory file
    cannot brick the whole system."""
    monkeypatch.setenv("LIFEOS_INFERENCE_ROUTES", str(tmp_path / "absent.json"))
    with pytest.raises(PolicyUnavailable):
        check_egress("zai", {"company": "personal"})
    data_class, route = check_egress("claude", {"company": "frontsteps"})
    assert data_class == "CONFIDENTIAL" and route is None


def test_unverified_ceiling_degrades_when_unsupervised(tmp_path, monkeypatch):
    """A Bellows-shaped route: nominal CONFIDENTIAL, unverified vendor terms."""
    write_routes(tmp_path, monkeypatch, routes={
        "NATIVE": {
            "vendor": "anthropic", "model": "claude", "ceiling": "RESTRICTED",
            "ceilingUnsupervised": "RESTRICTED", "ceilingVerified": True,
        },
        "TEMPER": {
            "vendor": "google", "model": "gemini-3.1-pro-high", "ceiling": "CONFIDENTIAL",
            "ceilingUnsupervised": "INTERNAL", "ceilingVerified": False,
        },
    })
    # CONFIDENTIAL would pass the NOMINAL ceiling but not the degraded one.
    with pytest.raises(EgressDenied, match="ceiling unverified"):
        check_egress("zai", {"company": "frontsteps"})
    # Supervised use may rely on the nominal ceiling.
    data_class, _ = check_egress("zai", {"company": "frontsteps"}, supervised=True)
    assert data_class == "CONFIDENTIAL"


def test_every_declared_agent_route_is_reachable(tmp_path, monkeypatch):
    """AGENT_ROUTE must not name a route the artifact doesn't define."""
    write_routes(tmp_path, monkeypatch)
    table = load_routes()
    for agent, route_name in AGENT_ROUTE.items():
        assert route_name in table, f"agent {agent!r} maps to unknown route {route_name!r}"


def test_agent_route_matches_the_real_lifeos_artifact():
    """The fixture above is synthetic; this asserts against the REAL exported
    table so a models.ts rename can't silently orphan a wired agent. Skips in
    CI, where ~/.claude does not exist (same policy as test_env_reference)."""
    try:
        table = load_routes()
    except PolicyUnavailable:
        pytest.skip("no LifeOS route artifact on this machine (CI)")
    for agent, route_name in AGENT_ROUTE.items():
        assert route_name in table, f"agent {agent!r} maps to unknown route {route_name!r}"


def test_forge_is_restricted_capable(tmp_path, monkeypatch):
    """OpenAI is one of the two RESTRICTED-cleared vendors, so Forge accepts
    any bead class — including the company beads z.ai must never see."""
    write_routes(tmp_path, monkeypatch)
    data_class, route = check_egress("forge", {"company": "frontsteps"})
    assert (data_class, route.vendor) == ("CONFIDENTIAL", "openai")


# ------------------------------------------------------- orchestrator gate


def _orchestrator(tmp_path):
    cfg = Config(tasks_path=str(tmp_path / "tasks.jsonl"))
    return Orchestrator(cfg), Beads(str(tmp_path / "tasks.jsonl"))


def test_orchestrator_blocks_company_bead_routed_to_zai(tmp_path, monkeypatch, capsys):
    write_routes(tmp_path, monkeypatch)
    orch, beads = _orchestrator(tmp_path)
    task = beads.create(
        title="sync invoices",
        description="x",
        assigned_agent="zai",
        metadata={"company": "frontsteps", "store_backed": True},
    )
    assert orch._execute_cycle_task(task) is False
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert "egress denied" in (refreshed.result or "")
    assert "PUBLIC ceiling" in (refreshed.result or "")
    assert "BLOCKED" in capsys.readouterr().out


def test_orchestrator_allows_declared_public_bead_to_zai(tmp_path, monkeypatch):
    """The auditable escape hatch: an explicit PUBLIC declaration passes."""
    write_routes(tmp_path, monkeypatch)
    orch, beads = _orchestrator(tmp_path)
    task = beads.create(
        title="ingest public video",
        description="x",
        assigned_agent="zai",
        metadata={"company": "personal", "data_class": "PUBLIC"},
    )
    assert orch._authorize_egress(task, "zai") is True
