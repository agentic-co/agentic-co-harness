"""Tests for the host-independent ASOP engine and its SOPBench shell.

WHY THESE EXIST
---------------
`tests/test_asop_agent.py` covers the parsing, evidence and verdict layers, but
every test touching the *agent* is `@needs_tau2` and this tree has no tau2
checkout — so the stepwise walk, the gate scheduler and the escalation path
have never executed under `pytest` here. Extracting `ASOPEngine` is what makes
them reachable at all, and the extraction would be worth little if it did not
come with the tests it enables.

The properties worth pinning are the ones whose failure is silent:

  * a gate that passes when its tool was never called,
  * a step that advances although its gate refused,
  * an escalation recorded as a pass,
  * a tool failure read as a success because the host threw the flag away.

Each of those produces a clean run and a wrong number.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"


def _load(name: str):
    """Load a module out of the tool directory by path.

    `scripts/eval` is not a package, and the runtime must not grow an import of
    a benchmark adapter. Registering in `sys.modules` BEFORE exec is required:
    `@dataclass` resolves a class's `__module__` through `sys.modules`.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _EVAL_DIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


eng = _load("asop_engine")


# A two-step procedure with a real deterministic gate on each step. Written in
# the layout the SOPBench compiler emits, so these tests also pin that the
# layout parses the way the compiler asserts it does.
DOC = """\
Compiled procedures for a test domain.

## Routing

| The user wants to... | Procedure |
| --- | --- |
| move money; transfer funds | Transfer Funds |

## Procedure: Transfer Funds

1. **Verify the balance condition.** Establish the account balance before proceeding. Gate: deterministic (tool call: `get_account_balance`)
2. **Complete the Transfer Funds action.** Call the transfer_funds tool. Gate: deterministic (tool call: `transfer_funds`)
"""


class _Call:
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments or {}


class _Msg:
    def __init__(self, role, content="", tool_calls=(), error=False):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.error = error


def _tripwire_judge(prompt, allow_na=False):
    raise AssertionError(
        "the verifier was consulted, but every step in this document declares a "
        "deterministic gate that names a runnable tool — no model opinion should "
        "be reachable"
    )


def _engine(judge=_tripwire_judge, route_fn=None, max_refusals=3):
    return eng.ASOPEngine(
        asop=eng.parse_asop(DOC),
        verifier=eng.Verifier("verifier:test", judge),
        identity="executor:test",
        route_fn=route_fn,
        max_refusals=max_refusals,
    )


# ── the document the compiler emits actually parses ──────────────────────────


def test_compiler_layout_parses_into_deterministic_gates_naming_their_tool():
    """The whole port rests on this: gates that fire without a model.

    On tau2 most gates declared `deterministic` and named nothing re-runnable,
    so they degraded to `DETERMINISTIC_UNAVAILABLE` and became model opinions.
    If the compiled SOPBench document did the same, the gated arm would never
    have tested a deterministic gate at all.
    """
    asop = eng.parse_asop(DOC)
    steps = asop.procedure("Transfer Funds").steps
    assert len(steps) == 2
    assert [s.gate_kinds for s in steps] == [
        (eng.GateKind.DETERMINISTIC,),
        (eng.GateKind.DETERMINISTIC,),
    ]
    assert eng.named_tool(steps[0]) == "get_account_balance"
    assert eng.named_tool(steps[1]) == "transfer_funds"
    # A step silently marked conditional can return N/A and inflate nothing
    # while gating nothing. The compiler asserts zero; so does this.
    assert not any(s.conditional for s in steps)


# ── the gate ─────────────────────────────────────────────────────────────────


def test_gate_refuses_when_the_named_tool_was_never_called():
    e = _engine()
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [_Msg("user", "move money please")]
    e.run_gates(state, arrived=_Msg("tool", "irrelevant result"))

    assert state.step_index == 0, "a refused step must not advance"
    assert state.consecutive_refusals == 1
    assert state.refusal and "has not been called" in state.refusal
    assert state.verdicts[-1]["passed"] is False
    assert state.verdicts[-1]["gate"] == "deterministic"


def test_gate_passes_only_on_a_call_whose_result_came_back_ok():
    e = _engine()
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [
        _Msg("assistant", tool_calls=(_Call("get_account_balance", {"username": "jo"}),)),
    ]
    e.run_gates(state, arrived=_Msg("tool", "1200.0"))

    assert state.step_index == 1, "a passed gate advances exactly one step"
    assert state.consecutive_refusals == 0
    assert state.refusal is None
    assert state.verdicts[-1]["passed"] is True
    assert state.verdicts[-1]["verifier"] == "deterministic-check"


def test_a_failed_tool_call_does_not_satisfy_its_own_gate():
    """The distinction the host throws away, and the reason `_OkRecorder` exists.

    SOPBench computes each call's success boolean and discards it. If the shim
    reported every call as `ok`, a constraint-blocked call would clear the gate
    that exists to catch it — a false PASS, which is the one direction that
    cannot be recovered downstream.
    """
    e = _engine()
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [
        _Msg("assistant", tool_calls=(_Call("get_account_balance", {"username": "jo"}),)),
    ]
    e.run_gates(state, arrived=_Msg("tool", "False", error=True))

    assert state.step_index == 0
    assert state.verdicts[-1]["passed"] is False
    assert "FAILED" in state.verdicts[-1]["reason"]


def test_escalation_steps_past_without_ever_recording_a_pass():
    """A gate that refuses forever starves the task it is protecting.

    The step is stepped past so the run keeps producing evidence, but it must
    be recorded as never attested. Counting an escalation as a pass would make
    every gate statistic a claim about steps that were never verified.
    """
    e = _engine(max_refusals=2)
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [_Msg("user", "go")]

    e.run_gates(state, arrived=_Msg("tool", "nothing useful"))
    assert state.step_index == 0 and state.consecutive_refusals == 1
    e.run_gates(state, arrived=_Msg("tool", "still nothing"))

    assert state.step_index == 1, "escalation unblocks the run"
    assert state.escalated == ["Transfer Funds · step 1"]
    assert state.consecutive_refusals == 0
    assert all(v["passed"] is False for v in state.verdicts)


def test_the_gate_is_never_handed_anything_carrying_gold():
    e = _engine()
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [_Msg("user", "go")]

    captured = {}
    original = eng._assert_no_gold

    def spy(obj, where):
        captured["obj"] = obj
        return original(obj, where)

    eng._assert_no_gold = spy
    try:
        e.run_gates(state, arrived=_Msg("tool", "x"))
    finally:
        eng._assert_no_gold = original

    for attr in eng._GOLD_ATTRS:
        assert not hasattr(captured["obj"], attr)


# ── the prompt the executor sees ─────────────────────────────────────────────


def test_one_step_at_a_time_and_a_terminal_state_at_the_end():
    e = _engine()
    state = eng.ASOPState(procedure="Transfer Funds")

    first = e.system_prompt_for(state)
    assert "Verify the balance condition" in first
    # The remaining steps are listed by TITLE, deliberately — the executor is
    # told where it is in the procedure. What it must not have is the next
    # step's BODY, which is the instruction it could act on early.
    assert "Call the transfer_funds tool" not in first, "no working ahead"
    assert "After this step: Complete the Transfer Funds action" in first

    state.step_index = 2
    done = e.system_prompt_for(state)
    assert "is complete" in done
    assert "Verify the balance condition" not in done


def test_a_refusal_reaches_the_executor_with_its_reason():
    e = _engine()
    state = eng.ASOPState(procedure="Transfer Funds", refusal="get_account_balance has not been called")
    assert "gate_refused" in e.system_prompt_for(state)
    assert "has not been called" in e.system_prompt_for(state)


def test_no_procedure_yet_means_triage_not_step_one():
    e = _engine()
    assert "available_procedures" in e.system_prompt_for(eng.ASOPState())


# ── the SOPBench shim ────────────────────────────────────────────────────────

shim = _load("sopbench_asop_swarm")


def test_shim_reads_sopbench_wire_shapes():
    """Tool calls arrive as OpenAI dicts with arguments as a JSON STRING."""
    history = [
        {"role": "user", "content": "hello", "sender": "user"},
        {
            "role": "assistant",
            "content": None,
            "sender": "bank assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "get_account_balance",
                        "arguments": '{"username": "john_doe"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "tool_name": "get_account_balance", "content": "1200.0"},
    ]
    msgs = shim.adapt(history, {"c1": True})

    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    call = msgs[1].tool_calls[0]
    assert call.name == "get_account_balance"
    assert call.arguments == {"username": "john_doe"}, "the JSON string must be parsed"
    assert msgs[2].error is False

    hist = eng._tool_history(msgs)
    assert hist[0].startswith("called get_account_balance(")
    assert hist[1].startswith("  -> ok")


def test_shim_marks_a_recorded_failure_and_defaults_to_success():
    history = [
        {"role": "tool", "tool_call_id": "bad", "tool_name": "t", "content": "False"},
        {"role": "tool", "tool_call_id": "unknown", "tool_name": "t", "content": "ok"},
    ]
    msgs = shim.adapt(history, {"bad": False})
    assert msgs[0].error is True
    # Guessing FAILED for an unrecorded call would refuse steps that in fact
    # completed — the exact false-refusal mode this port must not reproduce.
    assert msgs[1].error is False


# ── per-task narrowing ───────────────────────────────────────────────────────
#
# The single most consequential piece of the port, and the one whose absence
# produced a real false result in pre-flight. A domain-static document imposes
# every precondition the DOMAIN knows about; a SOPBench task imposes a SUBSET,
# and the assistant's instructions — what every other arm reads — are built
# from the task's subset. Without narrowing, the gated arm is asked to satisfy
# preconditions the task never imposed and the user cannot supply, and it loses
# for a reason that has nothing to do with gating.

runner = _load("run_sopbench_asop")


def test_task_constraints_are_read_out_of_the_json_list_shape():
    """A task's tree is JSON, so every node is a list, not a tuple.

    `["single", name, params]` is ambiguous with "a list of children" unless the
    head is checked — the compiler's tuple-shaped flattener would mis-walk it.
    """
    assert runner.flatten_task_constraints(
        ["single", "internal_check_username_exist", {"username": "username"}]
    ) == {"internal_check_username_exist"}

    nested = [
        "and",
        [
            ["single", "internal_check_username_exist", {"username": "username"}],
            ["chain", [["single", "minimal_elgibile_credit_score", {"username": "username"}]]],
        ],
    ]
    assert runner.flatten_task_constraints(nested) == {
        "internal_check_username_exist",
        "minimal_elgibile_credit_score",
    }
    # SOPBench's polarity marker is not this document's concern: the compiled
    # step says WHICH condition to check, the task rules say which way it must
    # resolve.
    assert runner.flatten_task_constraints(
        ["single", "not internal_check_username_exist", {}]
    ) == {"internal_check_username_exist"}
    assert runner.flatten_task_constraints(None) == set()


NARROW_DOC = """\
Compiled procedures.

## Procedure: Apply Credit Card

1. **Verify the username existence condition.** Establish it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the minimum eligible credit score condition.** Establish it. Gate: deterministic (tool call: `internal_get_credit_score`)
3. **Verify the user login status condition.** Establish it. Gate: deterministic (tool call: `login_user`)
4. **Complete the Apply Credit Card action.** Call it. Gate: deterministic (tool call: `apply_credit_card`)
"""


def test_narrowing_drops_preconditions_the_task_never_imposed():
    """The exact bank task 0 case, which produced a false refusal before this.

    `apply_credit_card` task 0 imposes ONE constraint. The static document
    compiles three. The third — `logged_in_user` — is unsatisfiable for a user
    whose known information carries no credentials, so the executor invented a
    password, `login_user` returned False, and the gate refused a step that
    could never pass on a task whose `action_should_succeed` is True.
    """
    full = eng.parse_asop(NARROW_DOC)
    assert len(full.procedure("Apply Credit Card").steps) == 4

    task = {
        "user_goal": "apply_credit_card",
        "constraints": ["single", "internal_check_username_exist", {"username": "username"}],
    }
    tool_of = {
        "internal_check_username_exist": "internal_check_username_exist",
        "minimal_elgibile_credit_score": "internal_get_credit_score",
        "logged_in_user": "login_user",
    }
    narrowed = runner.make_task_asop_provider(full, task, tool_of)
    steps = narrowed.procedure("Apply Credit Card").steps

    tools = [eng.named_tool(s) for s in steps]
    assert tools == ["internal_check_username_exist", "apply_credit_card"]
    assert "login_user" not in tools, "an unsatisfiable precondition must not survive"
    # The action step is ALWAYS kept: narrowing removes preconditions, never the
    # thing the user asked for.
    assert tools[-1] == "apply_credit_card"
    assert [s.number for s in steps] == [1, 2], "kept steps are renumbered contiguously"


def test_narrowing_keeps_a_precondition_the_task_does_impose():
    full = eng.parse_asop(NARROW_DOC)
    task = {
        "user_goal": "apply_credit_card",
        "constraints": [
            "and",
            [
                ["single", "internal_check_username_exist", {}],
                ["single", "minimal_elgibile_credit_score", {}],
            ],
        ],
    }
    tool_of = {
        "internal_check_username_exist": "internal_check_username_exist",
        "minimal_elgibile_credit_score": "internal_get_credit_score",
        "logged_in_user": "login_user",
    }
    steps = runner.make_task_asop_provider(full, task, tool_of).procedure(
        "Apply Credit Card"
    ).steps
    assert [eng.named_tool(s) for s in steps] == [
        "internal_check_username_exist",
        "internal_get_credit_score",
        "apply_credit_card",
    ]


def test_only_the_goal_procedure_is_narrowed():
    """Other procedures are reached only on a misroute, and trimming them would
    hide that rather than measure it."""
    doc = NARROW_DOC + """
## Procedure: Pay Bill

1. **Verify the user login status condition.** Establish it. Gate: deterministic (tool call: `login_user`)
2. **Complete the Pay Bill action.** Call it. Gate: deterministic (tool call: `pay_bill`)
"""
    full = eng.parse_asop(doc)
    task = {
        "user_goal": "apply_credit_card",
        "constraints": ["single", "internal_check_username_exist", {}],
    }
    narrowed = runner.make_task_asop_provider(
        full, task, {"internal_check_username_exist": "internal_check_username_exist"}
    )
    assert len(narrowed.procedure("Pay Bill").steps) == 2
    assert len(narrowed.procedure("Apply Credit Card").steps) == 2


def test_both_arms_can_be_handed_the_same_narrowed_document():
    """`asop-prompt` is the control that makes a loss attributable, so it has to
    read exactly what the gated arm walks — same steps, same gates."""
    full = eng.parse_asop(NARROW_DOC)
    task = {
        "user_goal": "apply_credit_card",
        "constraints": ["single", "internal_check_username_exist", {}],
    }
    narrowed = runner.make_task_asop_provider(
        full, task, {"internal_check_username_exist": "internal_check_username_exist"}
    )
    text = runner.render_asop(narrowed)
    assert "login_user" not in text
    reparsed = eng.parse_asop(text)
    assert [eng.named_tool(s) for s in reparsed.procedure("Apply Credit Card").steps] == [
        eng.named_tool(s) for s in narrowed.procedure("Apply Credit Card").steps
    ]


# ── the value gate ───────────────────────────────────────────────────────────
#
# Built from the decisions the first full run actually got wrong. `asop-gated`
# scored -0.107 and the split localised it: best arm at performing actions
# (action_called_correctly 0.729), worst at refusing them
# (constraint_not_violated 0.535 vs baseline 0.721). Every case below is a shape
# that liveness passed and a value check must refuse.

VALUE_DOC = """\
Compiled procedures.

## Procedure: Transfer Funds

1. **Verify the username existence condition.** Establish it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the balance condition.** Establish it. Gate: deterministic (tool call: `get_account_balance`)
3. **Complete the Transfer Funds action.** Call it. Gate: deterministic (tool call: `transfer_funds`)
"""


def _hist(*pairs):
    """Render a tool history the way `_tool_history` does."""
    out = []
    for name, value in pairs:
        out.append(f"called {name}()")
        out.append(f"  -> ok: {value}")
    return tuple(out)


def _step(n=1):
    return eng.parse_asop(VALUE_DOC).procedure("Transfer Funds").steps[n - 1]


def test_liveness_passes_the_false_that_the_value_gate_refuses():
    """`internal_check_username_exist` returns (True, False) for a missing user.

    The CALL succeeds, so `check_tool_succeeded` passes it — and this single
    shape sits on 126 of the violating decisions in the first run. It is the
    clearest statement of the bug: the gate confirmed the agent had looked, not
    that the answer was yes.
    """
    hist = _hist(("internal_check_username_exist", "False"))
    assert eng.check_tool_succeeded("internal_check_username_exist", hist)[0] is True

    check = runner.make_value_checker(
        {
            "constraints": ["single", "internal_check_username_exist", {}],
            "constraint_parameters": {},
            "user_known": {},
        },
        {"internal_check_username_exist": "internal_check_username_exist"},
    )
    holds, why = check(_step(1), hist)
    assert holds is False
    assert "internal_check_username_exist" in why


def test_value_gate_passes_a_genuinely_satisfied_precondition():
    check = runner.make_value_checker(
        {
            "constraints": ["single", "internal_check_username_exist", {}],
            "constraint_parameters": {},
            "user_known": {},
        },
        {"internal_check_username_exist": "internal_check_username_exist"},
    )
    holds, _ = check(_step(1), _hist(("internal_check_username_exist", "True")))
    assert holds is True


def test_value_gate_honours_polarity():
    """`open_account` requires the username NOT to exist — bank's only negation."""
    check = runner.make_value_checker(
        {
            "constraints": ["single", "not internal_check_username_exist", {}],
            "constraint_parameters": {},
            "user_known": {},
        },
        {"internal_check_username_exist": "internal_check_username_exist"},
    )
    assert check(_step(1), _hist(("internal_check_username_exist", "True")))[0] is False
    assert check(_step(1), _hist(("internal_check_username_exist", "False")))[0] is True


@pytest.mark.parametrize(
    "constraint,params,known,results,expected",
    [
        # Real values observed in the first run's transcripts.
        ("minimal_elgibile_credit_score", {"minimum_credit_score": 600}, {},
         [("internal_get_credit_score", "300")], False),
        ("minimal_elgibile_credit_score", {"minimum_credit_score": 600}, {},
         [("internal_get_credit_score", "750")], True),
        ("safety_box_eligible", {"minimum_account_balance_safety_box": 300}, {},
         [("get_account_balance", "200.0")], False),
        ("safety_box_eligible", {"minimum_account_balance_safety_box": 300}, {},
         [("get_account_balance", "1000.0")], True),
        ("sufficient_account_balance", {}, {"amount": 500.0},
         [("get_account_balance", "200.0")], False),
        ("sufficient_account_balance", {}, {"amount": 200.0},
         [("get_account_balance", "1000.0")], True),
        ("get_loan_owed_balance_restr", {"maximum_owed_balance": 500}, {},
         [("get_account_owed_balance", "600.0")], False),
        ("get_loan_owed_balance_restr", {"maximum_owed_balance": 500}, {},
         [("get_account_owed_balance", "200.0")], True),
    ],
)
def test_value_gate_compares_the_returned_value(constraint, params, known, results, expected):
    tool = {
        "minimal_elgibile_credit_score": "internal_get_credit_score",
        "safety_box_eligible": "get_account_balance",
        "sufficient_account_balance": "get_account_balance",
        "get_loan_owed_balance_restr": "get_account_owed_balance",
    }[constraint]
    doc = VALUE_DOC.replace("`get_account_balance`", f"`{tool}`")
    step = eng.parse_asop(doc).procedure("Transfer Funds").steps[1]
    check = runner.make_value_checker(
        {"constraints": ["single", constraint, {}], "constraint_parameters": params,
         "user_known": known},
        {constraint: tool},
    )
    assert check(step, _hist(*results))[0] is expected


def test_value_gate_returns_None_when_it_cannot_judge():
    """Falling back to liveness beats guessing — a guess trades one silent
    wrongness for another."""
    check = runner.make_value_checker(
        {"constraints": ["single", "some_unmodelled_constraint", {}],
         "constraint_parameters": {}, "user_known": {}},
        {"some_unmodelled_constraint": "get_account_balance"},
    )
    assert check(_step(2), _hist(("get_account_balance", "1000.0")))[0] is None


def test_engine_uses_the_value_gate_and_records_it_as_such():
    """End to end: a refusal the liveness gate would have passed."""
    hist_step = _step(1)
    engine = eng.ASOPEngine(
        asop=eng.parse_asop(VALUE_DOC),
        verifier=eng.Verifier("verifier:test", _tripwire_judge),
        identity="executor:test",
        value_check=runner.make_value_checker(
            {"constraints": ["single", "internal_check_username_exist", {}],
             "constraint_parameters": {}, "user_known": {}},
            {"internal_check_username_exist": "internal_check_username_exist"},
        ),
    )
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [
        _Msg("assistant", tool_calls=(_Call("internal_check_username_exist", {"username": "nope"}),))
    ]
    engine.run_gates(state, arrived=_Msg("tool", "False"))

    assert state.step_index == 0, "the step must not advance on an unmet precondition"
    assert state.verdicts[-1]["passed"] is False
    assert state.verdicts[-1]["verifier"] == "deterministic-value-check"
    assert state.verdicts[-1]["gate"] == "deterministic"


def test_engine_without_a_value_gate_is_byte_for_byte_the_old_behaviour():
    """tau2 supplies no checker, and every published tau2 number came from that
    path — so absence of a checker must change nothing."""
    engine = eng.ASOPEngine(
        asop=eng.parse_asop(VALUE_DOC),
        verifier=eng.Verifier("verifier:test", _tripwire_judge),
        identity="executor:test",
    )
    assert engine._value_check is None
    state = eng.ASOPState(procedure="Transfer Funds")
    state.messages = [
        _Msg("assistant", tool_calls=(_Call("internal_check_username_exist", {}),))
    ]
    engine.run_gates(state, arrived=_Msg("tool", "False"))
    # Liveness passes it, exactly as before the value gate existed.
    assert state.verdicts[-1]["passed"] is True
    assert state.verdicts[-1]["verifier"] == "deterministic-check"


def test_ok_recorder_catches_the_bare_False_bank_returns():
    """`bank.py` returns a BARE `False` when a constraint blocks the call.

    A recorder that only understood the documented `(ok, value)` tuple would
    score every constraint-blocked call as a success — the silent version of
    having no flag at all.
    """
    class _System:
        def good(self, **kw):
            return True, 1200.0

        def blocked(self, **kw):
            return False  # bank's real shape when domain_dep.process fails

        def boom(self, **kw):
            raise RuntimeError("nope")

    sink: list = []
    proxy = shim._OkRecorder(_System(), sink)
    proxy.good(username="jo")
    proxy.blocked(username="jo")
    with pytest.raises(RuntimeError):
        proxy.boom()

    assert sink == [("good", True), ("blocked", False), ("boom", False)]


# ── the upfront presentation (N28) ───────────────────────────────────────────
#
# The variable these cover is PRESENTATION, and the thing most worth pinning
# down is what did NOT change: a presentation flag that quietly altered the
# gates would make the arm a two-variable experiment and its number
# uninterpretable.


def _upfront_engine():
    return eng.ASOPEngine(
        asop=eng.parse_asop(DOC),
        verifier=eng.Verifier("verifier:test", _tripwire_judge),
        identity="executor:test",
        upfront=True,
    )


def test_stepwise_hides_later_step_bodies_and_upfront_shows_them():
    """The measured difference between the two arms, asserted as text.

    `STEP_PROMPT` gives the current step's body and only the TITLES of the
    rest; the upfront template gives every body at once. If this ever stopped
    holding, the two arms would differ in name only.
    """
    state = eng.ASOPState(procedure="Transfer Funds")
    stepwise = _engine().system_prompt_for(state)
    upfront = _upfront_engine().system_prompt_for(state)

    later = eng.parse_asop(DOC).procedure("Transfer Funds").steps[1]
    assert later.body not in stepwise
    assert later.title in stepwise  # the title, and nothing more
    assert later.body in upfront
    assert "procedure_checklist" in upfront
    assert "ENUMERATE" in upfront


def test_upfront_marks_the_item_the_gate_is_actually_on():
    """A checklist with no cursor makes a gate refusal unattributable.

    The executor is shown every item, so it needs to be told which one the
    verifier is judging — otherwise the arm tests "see everything, ignore the
    gate" rather than "enumerate upfront, still gated".
    """
    e = _upfront_engine()
    steps = eng.parse_asop(DOC).procedure("Transfer Funds").steps

    state = eng.ASOPState(procedure="Transfer Funds", step_index=0)
    assert f"Item 1 of {len(steps)}: {steps[0].title}" in e.system_prompt_for(state)

    state.step_index = 1
    assert f"Item 2 of {len(steps)}: {steps[1].title}" in e.system_prompt_for(state)


def test_upfront_changes_no_verdict_the_stepwise_walk_would_have_reached():
    """PRESENTATION ONLY — the claim the arm's interpretation rests on.

    Same document, same evidence, same gate: the verdicts must match item for
    item. Anything else and the upfront arm is not a clean contrast.
    """
    def run(upfront):
        e = eng.ASOPEngine(
            asop=eng.parse_asop(DOC),
            verifier=eng.Verifier("verifier:test", _tripwire_judge),
            identity="executor:test",
            upfront=upfront,
        )
        state = eng.ASOPState(procedure="Transfer Funds")
        state.messages = [_Msg("user", "move money")]
        arrived = _Msg("tool", "balance is 1200.0")
        state.messages.append(_Msg("assistant", "", (_Call("get_account_balance", {}),)))
        e.before_turn(arrived, state)
        return state.step_index, [
            (v["step"], v["gate"], v["passed"], v["verifier"]) for v in state.verdicts
        ]

    assert run(False) == run(True)


def test_upfront_is_off_by_default_so_the_published_arms_are_unmoved():
    """The published gated arms were measured stepwise; the default must stay
    that, or re-running them silently reproduces a different experiment."""
    state = eng.ASOPState(procedure="Transfer Funds")
    assert "ONE step of a procedure at a time" in _engine().system_prompt_for(state)
    assert "procedure_checklist" not in _engine().system_prompt_for(state)
