"""Tests for `scripts/eval/gate_reach.py` — the static deterministic-gate lint.

This lint exists because "the deterministic gate path has never fired" was repeated
across four documents for days while it fired 58 times, and because the opposite
error is just as easy: reading `fell_back: False` credits retail v2 with 44
deterministic gates when it fired none.

So the property under test is narrow and load-bearing: a gate counts as firing only
when it BOTH declares a tool/api kind AND names something re-runnable. Either half
alone is the trap, and each half gets a case here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "eval"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate_reach = _load("gate_reach")
asop_agent = sys.modules["asop_agent"]  # gate_reach loads it as a side effect
GateKind = asop_agent.GateKind


def _asop(gate_line: str) -> str:
    return (
        "# Test ASOP\n\n"
        "## Procedure: Do The Thing\n\n"
        f"1. **Step one.** Precondition: something happened. Gate: {gate_line}\n"
    )


def _only_step(markdown: str):
    doc = asop_agent.parse_asop(markdown)
    return doc.procedures[0].steps[0]


def test_named_tool_gate_is_reachable(tmp_path, capsys):
    p = tmp_path / "named.md"
    p.write_text(_asop("deterministic (tool call: `get_order_details`)."))
    firing, declared = gate_reach.report(p)
    assert (firing, declared) == (1, 1)
    assert "get_order_details" in capsys.readouterr().out


def test_declared_deterministic_without_a_tool_name_is_stranded(tmp_path, capsys):
    """The exact retail v1/v2 defect: rigorous-looking, silently judged."""
    p = tmp_path / "stranded.md"
    p.write_text(_asop("deterministic (tool lookup returns a user id)."))
    firing, declared = gate_reach.report(p)
    assert (firing, declared) == (0, 1)
    out = capsys.readouterr().out
    assert "STRANDED" in out
    assert "FIX: name the tool" in out


def test_plain_deterministic_is_not_even_declared_a_tool_gate(tmp_path):
    """No 'tool'/'api' in the parenthetical downgrades before naming matters."""
    step = _only_step(_asop("deterministic."))
    assert GateKind.DETERMINISTIC not in step.gate_kinds
    assert GateKind.DETERMINISTIC_UNAVAILABLE in step.gate_kinds


def test_human_then_deterministic_declares_both(tmp_path):
    step = _only_step(_asop("human, then deterministic (tool call: `cancel_order`)."))
    assert step.gate_kinds == (GateKind.HUMAN, GateKind.DETERMINISTIC)
    assert asop_agent.named_tool(step) == "cancel_order"


def test_exit_code_is_nonzero_when_a_gate_is_stranded(tmp_path):
    """It is a lint. A stranded gate is the failure it exists to catch."""
    good = tmp_path / "good.md"
    good.write_text(_asop("deterministic (tool call: `get_order_details`)."))
    bad = tmp_path / "bad.md"
    bad.write_text(_asop("deterministic (tool result shows status pending)."))

    assert gate_reach.main([str(good)]) == 0
    assert gate_reach.main([str(bad)]) == 1


def test_shared_prefix_matches_either_authentication_tool():
    """`find_user_id` is deliberate: auth succeeds via _by_email or _by_name_zip.

    check_tool_succeeded matches the tool as a SUBSTRING of the call line, so the
    shared prefix accepts both. If that is ever tightened to an exact match this
    test fails -- which is the point, because the alternative is silently refusing
    every conversation that authenticated the other way.
    """
    for tool in ("find_user_id_by_email", "find_user_id_by_name_zip"):
        history = (f"called {tool}", "  -> ok user_id=yusuf_rossi_9620")
        ok, _ = asop_agent.check_tool_succeeded("find_user_id", history)
        assert ok, f"{tool} should satisfy the shared-prefix gate"

    ok, why = asop_agent.check_tool_succeeded(
        "find_user_id", ("called get_product_details", "  -> ok")
    )
    assert not ok and "has not been called" in why


def test_the_shipped_retail_v3_has_no_stranded_gates():
    """Guards the document itself: v3's whole purpose is 15/15 reachable."""
    v3 = Path(__file__).resolve().parents[1] / (
        "evals/tau2-retail-asop/asops/asop.claude.v3.md"
    )
    firing, declared = gate_reach.report(v3)
    assert declared == 15
    assert firing == 15
