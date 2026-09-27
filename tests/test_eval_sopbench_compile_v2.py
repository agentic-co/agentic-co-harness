"""Tests for `sopbench_asop_compile.py`'s v2 compilation rules.

Three properties, each pinned because getting it wrong produced a real,
measured loss rather than a style complaint:

  * **Step order must satisfy the domain's action graph.** SOPBench's
    `dirgraph_satisfied` — a conjunct of its `success` — fails a task if ANY
    tool call happens before the tools its own graph node depends on. v1 emits
    5 of bank's 20 procedures in an order the graph forbids
    (`authenticate_admin_password` before `login_user`, `get_account_balance`
    before `login_user`), and **38 of the 44 dirgraph failures in the published
    `asop-gated-value-rules` arm are exactly that shape**. v2 sorts; this test
    is what keeps it sorted.
  * **AND branches inside `constraint_processes` must not be rendered as
    alternatives.** `pay_loan_account_balance_restr` is
    `or(and(get_account_balance, get_account_owed_balance), internal_get_database)`.
    v1 flattened the tree and called the second call an *alternative* to the
    first, when the domain — and the evaluator walking the same tree — require
    both.
  * **v1 does not move.** Every published arm was measured on v1's bytes.

These run against synthetic tables rather than the real `bank` import: the
tables are the contract, and a test that needed a SOPBench checkout would be
skipped in CI, which is where a regression would land.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_COMPILE = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "sopbench_asop_compile.py"
_BANK_ASOP_DIR = Path(__file__).resolve().parents[1] / "evals" / "sopbench-bank-asop"
_DEFAULT_SOPBENCH = Path.home() / "Code" / "SOPBench"
_SOPBENCH_PYTHON = _DEFAULT_SOPBENCH / ".venv" / "bin" / "python3"
needs_sopbench = pytest.mark.skipif(
    not _SOPBENCH_PYTHON.exists(), reason="SOPBench checkout (with its .venv) not on path"
)


def _load():
    spec = importlib.util.spec_from_file_location("sopbench_asop_compile", _COMPILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sopbench_asop_compile"] = mod
    spec.loader.exec_module(mod)
    return mod


C = _load()


def _tables(**over):
    """Bank's real shapes, reduced to what each property needs."""
    base = dict(
        required={
            "get_safety_box": ("single", "internal_check_username_exist", {"username": "username"}),
            "authenticate_admin_password": ("single", "logged_in_user", {"username": "username"}),
            "get_account_balance": (
                "single",
                "internal_check_username_exist",
                {"username": "username"},
            ),
            "login_user": None,
            "internal_check_username_exist": None,
            "get_account_owed_balance": (
                "single",
                "internal_check_username_exist",
                {"username": "username"},
            ),
            "pay_loan": ("single", "internal_check_username_exist", {"username": "username"}),
        },
        customizable={
            "get_safety_box": [
                ("single", "authenticated_admin_password", {"username": "username"}),
                ("single", "logged_in_user", {"username": "username"}),
            ],
            "authenticate_admin_password": None,
            "get_account_balance": ("single", "logged_in_user", {"username": "username"}),
            "get_account_owed_balance": ("single", "logged_in_user", {"username": "username"}),
            "login_user": None,
            "internal_check_username_exist": None,
            "pay_loan": [
                ("single", "logged_in_user", {"username": "username"}),
                (
                    "or",
                    [
                        ("single", "pay_loan_account_balance_restr", {"username": "username"}),
                        ("single", "pay_loan_amount_restr", {"username": "username"}),
                    ],
                ),
            ],
        },
        links={
            "logged_in_user": ("login_user", {"username": "username"}),
            "authenticated_admin_password": ("authenticate_admin_password", {"username": "username"}),
        },
        processes={
            "internal_check_username_exist": (
                "or",
                [
                    ("single", "internal_check_username_exist", {"username": "username"}),
                    ("single", "internal_get_database", None),
                ],
            ),
            "pay_loan_account_balance_restr": (
                "or",
                [
                    (
                        "and",
                        [
                            ("single", "get_account_balance", {"username": "username"}),
                            ("single", "get_account_owed_balance", {"username": "username"}),
                        ],
                    ),
                    ("single", "internal_get_database", None),
                ],
            ),
            "pay_loan_amount_restr": (
                "or",
                [
                    ("single", "get_account_balance", {"username": "username"}),
                    ("single", "internal_get_database", None),
                ],
            ),
        },
        env_nodes=frozenset({"internal_get_database"}),
        exposed={
            "login_user",
            "authenticate_admin_password",
            "internal_check_username_exist",
            "get_account_balance",
            "get_account_owed_balance",
            "get_safety_box",
            "pay_loan",
        },
    )
    base.update(over)
    return C.Tables(**base)


def _step_tools(proc) -> list[str]:
    return [s.tool for s in proc.steps]


def test_v2_puts_login_before_the_tools_that_require_it():
    """`Get Safety Box` is the procedure v1 compiles in a forbidden order.

    v1: username -> authenticate_admin_password -> login_user. The second call
    fails `dirgraph_satisfied` on every task routed here, because
    `authenticate_admin_password` declares `logged_in_user` as a required
    dependency and `login_user` has not run.
    """
    tables = _tables()
    proc = C.plan_procedure_v2("get_safety_box", tables, {}, {}, {})
    tools = _step_tools(proc)
    assert tools.index("login_user") < tools.index("authenticate_admin_password")
    assert tools[-1] == "get_safety_box"


def test_v1_still_compiles_the_forbidden_order_so_the_contrast_is_real():
    """Pinning the defect, not endorsing it.

    If someone "fixes" v1 in passing, every published arm's document changes
    under numbers that were measured on the old bytes, and the v1/v2 contrast
    stops isolating anything. v1 is the archive.
    """
    tables = _tables()
    proc = C.plan_procedure(
        "get_safety_box",
        tables.required,
        tables.customizable,
        tables.links,
        tables.processes,
        tables.env_nodes,
        tables.exposed,
        {},
        {},
    )
    tools = _step_tools(proc)
    assert tools.index("authenticate_admin_password") < tools.index("login_user")


def test_v2_orders_the_balance_check_after_login():
    tables = _tables()
    proc = C.plan_procedure_v2("get_account_balance", tables, {}, {}, {})
    tools = _step_tools(proc)
    assert tools.index("login_user") < tools.index("get_account_balance")


def test_an_or_group_becomes_one_step_not_two_mandatory_ones():
    """`pay_loan`'s two balance conditions are an OR: satisfying one is enough.

    v1 flattens them into two sequential mandatory steps, which tells the
    executor to satisfy both — strictly more than the domain asks, written into
    the document as over-gating.
    """
    tables = _tables()
    proc = C.plan_procedure_v2("pay_loan", tables, {}, {}, {})
    bodies = [s.line for s in proc.steps]
    any_one = [b for b in bodies if "ANY ONE of" in b]
    assert len(any_one) == 1
    assert "pay_loan_account_balance_restr" in any_one[0]
    assert "pay_loan_amount_restr" in any_one[0]


def test_an_and_branch_is_not_described_as_an_alternative():
    """The v1 wording said "alternatively"; the domain requires both calls.

    `dirgraph_satisfied` walks the same or/and tree the document is compiled
    from, so a run that took the v1 wording at its word — one call, not two —
    satisfies neither OR branch and fails the task.
    """
    tables = _tables()
    branches = tables.verification_branches("pay_loan_account_balance_restr")
    assert branches == [["get_account_balance", "get_account_owed_balance"]]
    proc = C.plan_procedure_v2("pay_loan", tables, {}, {}, {})
    line = next(s.line for s in proc.steps if "ANY ONE of" in s.line)
    assert "get_account_balance and get_account_owed_balance" in line
    assert "alternatively" not in line


def test_establish_and_verify_are_labelled_differently():
    """`constraint_links` names a state-CHANGING action, not a check.

    `logged_in_user -> login_user` is the transition itself. v1 rendered it as
    "Verify the user login status condition ... (tool call: `login_user`)",
    which instructs the executor to *verify* a condition by *changing* it — and
    on a task whose rule is "must NOT be logged in", doing so is the violation.
    """
    tables = _tables()
    proc = C.plan_procedure_v2("get_safety_box", tables, {}, {}, {})
    by_tool = {s.tool: s.line for s in proc.steps}
    assert "ESTABLISH:" in by_tool["login_user"]
    assert "ESTABLISH:" in by_tool["authenticate_admin_password"]
    assert "VERIFY:" in by_tool["internal_check_username_exist"]
    assert "reads state; it does not change it" in by_tool["internal_check_username_exist"]


def test_v2_asks_for_a_cited_verdict_exactly_once():
    """The discipline is stated in the preamble, not repeated on every step.

    v1 repeats "The rules above state what this condition must be for this
    request..." ~60 times; the preamble is what the engine injects into every
    step prompt anyway, so saying it there costs nothing per step and says it
    where the executor actually reads it.
    """
    assert "VERDICT <condition>: SATISFIED" in C.PREAMBLE_V2
    tables = _tables()
    proc = C.plan_procedure_v2("get_safety_box", tables, {}, {}, {})
    for step in proc.steps:
        assert "The rules above state what this condition must be" not in step.line


def test_unsatisfiable_prerequisites_are_reported_not_silently_invented():
    """Some orders cannot be repaired by sorting, and inventing a step is N24.

    `set_admin_password`'s only constraint is `authenticated_admin_password`,
    whose tool needs `login_user` — a step the procedure's own constraint set
    does not carry. Adding one would hand the gated arm a precondition the task
    never imposed, which is the exact defect N24 recorded. It is reported
    instead.
    """
    tables = _tables(
        required={
            "set_admin_password": ("single", "authenticated_admin_password", {"username": "username"}),
            "authenticate_admin_password": ("single", "logged_in_user", {"username": "username"}),
            "login_user": None,
        },
        customizable={"set_admin_password": None, "authenticate_admin_password": None, "login_user": None},
        exposed={"login_user", "authenticate_admin_password", "set_admin_password"},
    )
    notes: dict = {}
    C.plan_procedure_v2("set_admin_password", tables, {}, {}, notes)
    assert "set_admin_password" in notes
    assert any("login_user" in n for n in notes["set_admin_password"])


# ── v2b: argument-binding rendering (N33) ────────────────────────────────────
#
# `online_market`'s real shape, reduced: `within_return_period` is
# `or(and(get_order_details, internal_get_interaction_time), internal_get_database)`
# — an AND branch of a tool that takes arguments and one that takes none. v1/v2
# both discard the param maps `constraint_processes` carries; on `bank` every
# AND branch happens to share one signature so this never showed, and on
# `online_market` it costs 36 tasks calling the zero-argument tool with one
# (N33). These tables reproduce that exact shape rather than a synthetic one
# that would not catch the regression.


def _online_market_like_tables(**over):
    base = dict(
        required={
            "return_order": [
                ("single", "logged_in_user", {"username": "username"}),
                (
                    "single",
                    "within_return_period",
                    {"order_id": "order_id", "username": "username"},
                ),
            ],
            "login_user": None,
            "get_order_details": None,
            "internal_get_interaction_time": None,
            "internal_get_database": None,
        },
        customizable={
            "return_order": None,
            "login_user": None,
            "get_order_details": None,
            "internal_get_interaction_time": None,
            "internal_get_database": None,
        },
        links={"logged_in_user": ("login_user", {"username": "username"})},
        processes={
            "within_return_period": (
                "or",
                [
                    (
                        "and",
                        [
                            (
                                "single",
                                "get_order_details",
                                {"order_id": "order_id", "username": "username"},
                            ),
                            ("single", "internal_get_interaction_time", None),
                        ],
                    ),
                    ("single", "internal_get_database", None),
                ],
            ),
        },
        env_nodes=frozenset({"internal_get_database"}),
        exposed={
            "login_user",
            "get_order_details",
            "internal_get_interaction_time",
            "return_order",
        },
    )
    base.update(over)
    return C.Tables(**base)


def test_v2b_renders_argument_bindings_for_and_branch_tools():
    """The defect this whole variant exists to fix.

    v1/v2 render "this condition needs get_order_details and
    internal_get_interaction_time — call them all" and never say what either
    takes. v2b states each tool's own binding, so a zero-argument tool cannot
    be called with one.
    """
    tables = _online_market_like_tables()
    proc = C.plan_procedure_v2("return_order", tables, {}, {}, {}, render_args=True)
    line = next(s.line for s in proc.steps if "within_return_period" in s.line)
    assert "get_order_details (arguments: order_id, username)" in line
    assert "internal_get_interaction_time (takes no arguments)" in line


def test_v2b_renders_establish_tool_arguments_too():
    """Not just AND branches — the ESTABLISH/single-VERIFY tool gets one too."""
    tables = _online_market_like_tables()
    proc = C.plan_procedure_v2("return_order", tables, {}, {}, {}, render_args=True)
    by_tool = {s.tool: s.line for s in proc.steps}
    assert "login_user tool (arguments: username)" in by_tool["login_user"]


def test_v2_bytes_are_unaffected_by_the_render_args_plumbing():
    """`render_args` defaults to False, and false must reproduce v2 exactly.

    Every published v2 arm was measured on the text v2 emits without this
    flag; v2b has to be additive, not a silent rewrite of v2's own bytes.
    """
    tables = _online_market_like_tables()
    proc = C.plan_procedure_v2("return_order", tables, {}, {}, {})
    line = next(s.line for s in proc.steps if "within_return_period" in s.line)
    assert "(arguments:" not in line
    assert "(takes no arguments)" not in line
    assert (
        "this condition needs get_order_details and internal_get_interaction_time "
        "— call them all" in line
    )


def test_render_arg_binding_treats_none_and_empty_dict_alike():
    """SOPBench spells "no arguments" two ways (`None` and `{}`); both mean it."""
    assert C.render_arg_binding(None) == "takes no arguments"
    assert C.render_arg_binding({}) == "takes no arguments"
    assert C.render_arg_binding({"order_id": "order_id"}) == "arguments: order_id"
    assert (
        C.render_arg_binding({"username": "destination_username"})
        == "arguments: username=destination_username"
    )


def test_v2b_preamble_explains_the_notation_and_v2_does_not():
    assert "arguments:" not in C.PREAMBLE_V2
    assert "(arguments: ...)" in C.PREAMBLE_V2B_SUFFIX


# ── byte-identity against the committed documents ────────────────────────────
#
# Needs a real SOPBench checkout (the domain tables are imported, not
# reproduced), so it is skipped where one is not on path — same convention as
# `test_asop_agent.py`'s `needs_tau2`. Where it CAN run (this developer's
# machine, or CI configured with the checkout), it is the test that would
# catch a v2b change accidentally altering v2's published output.


@needs_sopbench
@pytest.mark.parametrize("domain", ["bank", "library", "online_market"])
def test_v2_documents_reproduce_the_committed_bytes(domain, tmp_path):
    committed = _BANK_ASOP_DIR / f"{domain}.v2.asop.md"
    assert committed.exists(), f"no committed v2 document for {domain!r} to compare against"
    out = tmp_path / f"{domain}.v2.asop.md"
    subprocess.run(
        [
            str(_SOPBENCH_PYTHON),
            str(_COMPILE),
            "--sopbench",
            str(_DEFAULT_SOPBENCH),
            "--domain",
            domain,
            "--variant",
            "v2",
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.read_text() == committed.read_text()
