"""Tests for verify-then-gate: the executor attests, the gate audits.

The architecture inversion. Every gated arm measured before this put the gate
BEFORE the step — machinery decided whether a precondition held, and the
executor never had to articulate anything. `dirgraph_satisfied` (did the agent
actually perform the required verifications) has not moved off ~0.59 across five
such configurations while PVA, whose text makes the model STATE a verdict per
constraint citing the value it saw, reaches 0.79.

What gets pinned here is the part that is easy to break by "improving" the
refusal message, and that would quietly undo the whole design:

  * **A missing verdict is itself the failure, and the gate must not fill it
    in.** If the refusal names the right answer, articulating becomes optional
    again and the barrier is back wearing the new architecture's clothes.
  * **A confirmed failure is TERMINAL.** A refused gate has always meant "not
    yet, fix it and retry"; it never meant "this must not happen". That is why
    the impermissible half kept being escalated past the gate and acted on.
  * **Everything is inert without an attestor**, so the nine published arms
    reproduce byte for byte.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "eval"))

import asop_engine as E  # noqa: E402


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_sopbench_asop", _ROOT / "scripts" / "eval" / "run_sopbench_asop.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_sopbench_asop"] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load_runner()

DOC = """\
Compiled procedure document.

## Routing

| The user wants to... | Procedure |
| --- | --- |
| pay bill | Pay Bill |

## Procedure: Pay Bill

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **account balance sufficiency.** Condition `sufficient_account_balance`. VERIFY: call the get_account_balance tool. Gate: deterministic (tool call: `get_account_balance`)
3. **Pay Bill — the requested action.** ACT: call the pay_bill tool. Gate: deterministic (tool call: `pay_bill`)
"""

TASK_OK = {
    "user_goal": "pay_bill",
    "constraint_parameters": {},
    "user_known": {"username": "john", "amount": 50},
    "constraints": [
        "and",
        [
            ["single", "internal_check_username_exist", {"username": "username"}],
            ["single", "sufficient_account_balance", {"username": "username", "amount": "amount"}],
        ],
    ],
}

TOOL_OF = {
    "internal_check_username_exist": "internal_check_username_exist",
    "sufficient_account_balance": "get_account_balance",
}

HISTORY_RICH = (
    "called internal_check_username_exist(username='john')",
    "  -> ok: True",
    "called get_account_balance(username='john')",
    "  -> ok: 200.0",
)
HISTORY_POOR = (
    "called internal_check_username_exist(username='john')",
    "  -> ok: True",
    "called get_account_balance(username='john')",
    "  -> ok: 10.0",
)


def _steps():
    return list(E.parse_asop(DOC).procedure("Pay Bill").steps[:-1])


def _attest(task, said, history):
    return R.make_attestor(task, TOOL_OF)(_steps(), said, history)


# -- parsing -----------------------------------------------------------------


def test_verdict_lines_are_parsed_with_their_claim_and_citation():
    got = R.parse_verdicts(
        "VERDICT username existence: SATISFIED - internal_check_username_exist returned True\n"
        "VERDICT account balance sufficiency: NOT SATISFIED - balance 10.0 vs requested 50"
    )
    assert [v["satisfied"] for v in got] == [True, False]
    assert "True" in got[0]["cited"]
    assert "10.0" in got[1]["cited"]


# -- the refusal that must not become an answer ------------------------------


def test_a_missing_verdict_is_the_failure_and_the_gate_does_not_supply_it():
    status, msg, rows = _attest(
        TASK_OK,
        "VERDICT username existence: SATISFIED - returned True",
        HISTORY_RICH,
    )
    assert status == "refuse"
    assert "account balance sufficiency" in msg
    # The whole point: the message says WHAT is missing, never what it should say.
    assert "SATISFIED -" not in msg.replace("VERDICT line per condition", "")
    assert "200" not in msg
    assert any(r["stated"] is None for r in rows)


def test_a_verdict_with_no_cited_value_is_refused_as_missing():
    status, msg, _ = _attest(
        TASK_OK,
        "VERDICT username existence: SATISFIED - True\nVERDICT account balance sufficiency: SATISFIED -",
        HISTORY_RICH,
    )
    assert status == "refuse"
    assert "uncited" in msg or "without citing" in msg


def test_a_claim_the_tool_results_contradict_is_refused_and_named():
    status, msg, rows = _attest(
        TASK_OK,
        "VERDICT username existence: SATISFIED - returned True\n"
        "VERDICT account balance sufficiency: SATISFIED - balance is plenty",
        HISTORY_POOR,
    )
    assert status == "refuse"
    assert "does not match the tool results" in msg
    assert "balance 10 vs requested 50" in msg
    assert any(r.get("stated") is True and r.get("truth") is False for r in rows)


# -- the terminal state the engine did not have ------------------------------


def test_executor_says_not_satisfied_and_the_tools_agree_is_terminal():
    status, msg, _ = _attest(
        TASK_OK,
        "VERDICT username existence: SATISFIED - returned True\n"
        "VERDICT account balance sufficiency: NOT SATISFIED - balance 10.0 against 50 requested",
        HISTORY_POOR,
    )
    assert status == "blocked"
    assert "balance 10 vs requested 50" in msg


def test_everything_attested_and_confirmed_releases_the_action():
    status, _msg, rows = _attest(
        TASK_OK,
        "VERDICT username existence: SATISFIED - internal_check_username_exist returned True\n"
        "VERDICT account balance sufficiency: SATISFIED - balance 200.0 exceeds the 50 requested",
        HISTORY_RICH,
    )
    assert status == "ok"
    assert all(r["stated"] == r["truth"] for r in rows)


def test_an_uncheckable_condition_leaves_the_executors_verdict_standing():
    """No arithmetic exists for some conditions, and inventing one is the barrier.

    The executor's stated verdict is then the only judgement available, so it
    stands — including when it says NOT SATISFIED, which still blocks.
    """
    task = dict(TASK_OK, constraints=["single", "maximum_deposit_limit", {}])
    tool_of = {"maximum_deposit_limit": "internal_check_username_exist"}
    steps = _steps()[:1]
    status, _m, rows = R.make_attestor(task, tool_of)(
        steps, "VERDICT username existence: NOT SATISFIED - the deposit is over the cap", ()
    )
    assert status == "blocked"
    assert rows[0]["resolved"] == "uncheckable"


# -- the grader's own composition defect --------------------------------------

TASK_OR = {
    "user_goal": "pay_loan",
    "constraint_parameters": {},
    "user_known": {"username": "john", "pay_owed_amount_request": 50},
    "constraints": [
        "and",
        [
            ["single", "internal_check_username_exist", {"username": "username"}],
            [
                "or",
                [
                    ["single", "pay_loan_account_balance_restr", {"username": "username"}],
                    ["single", "pay_loan_amount_restr", {"username": "username"}],
                ],
            ],
        ],
    ],
}

# balance 100: below the 500 owed (so the payoff condition fails) but above the
# 50 requested (so the amount condition holds). The task needs ANY ONE.
HISTORY_OR = (
    "called internal_check_username_exist(username='john')",
    "  -> ok: True",
    "called get_account_balance(username='john')",
    "  -> ok: 100.0",
    "called get_account_owed_balance(username='john')",
    "  -> ok: 500.0",
)
TOOL_OF_OR = {
    "internal_check_username_exist": "internal_check_username_exist",
    "pay_loan_account_balance_restr": "get_account_balance",
    "pay_loan_amount_restr": "get_account_balance",
}


def test_the_grader_demanded_every_leaf_of_an_or_group():
    """Pinning the defect, because every arm measured before the fix has it.

    `_TaskTruth` records every leaf of the task's constraint tree as required,
    so `("or", [A, B])` becomes "A and B". That is over-refusal written into the
    grader — the same composition defect the V2 DOCUMENT was fixing in its text,
    sitting in the GATE the whole time. Found by validating the grader against
    `action_should_succeed` with perfect evidence, a check nobody had run.
    """
    check = R.make_value_checker(TASK_OR, TOOL_OF_OR)  # respect_or off = as measured
    step = E.parse_asop(DOC).procedure("Pay Bill").steps[1]  # gates get_account_balance
    ok, why = check(step, HISTORY_OR)
    assert ok is False
    assert "pay_loan_account_balance_restr" in why


def test_respecting_the_or_group_satisfies_it_from_one_member():
    check = R.make_value_checker(TASK_OR, TOOL_OF_OR, respect_or=True)
    step = E.parse_asop(DOC).procedure("Pay Bill").steps[1]
    ok, _why = check(step, HISTORY_OR)
    assert ok is True


def test_or_groups_are_read_off_the_task_tree_not_guessed():
    truth = R._TaskTruth(TASK_OR, respect_or=True)
    assert truth.or_groups == [["pay_loan_account_balance_restr", "pay_loan_amount_restr"]]
    assert truth.group_of("internal_check_username_exist") == ["internal_check_username_exist"]
    # Off by default, so the arms already measured keep the behaviour they ran.
    assert R._TaskTruth(TASK_OR).group_of("pay_loan_amount_restr") == ["pay_loan_amount_restr"]


# -- citation grounding: the one check that transfers between domains --------


def test_a_cited_value_no_tool_returned_is_refused():
    """The failure mode an attestation architecture is most exposed to.

    `_TaskTruth.holds` cannot tell you a fabricated citation from a real one —
    it re-derives the verdict and ignores what the executor said it saw. This
    does the opposite, and needs no knowledge of the rule, which is why it is
    the only part of the gate that survives a move to another domain.
    """
    status, msg, rows = R.make_attestor(TASK_OK, TOOL_OF, cite_check=True)(
        _steps(),
        "VERDICT username existence: SATISFIED - returned True\n"
        "VERDICT account balance sufficiency: SATISFIED - balance 9999.0",
        HISTORY_RICH,
    )
    assert status == "refuse"
    assert "cited 9999.0" in msg
    assert any(r.get("problem") == "ungrounded citation" for r in rows)


def test_citing_the_threshold_you_compared_against_is_grounded():
    """"650 vs the required 600" is a correct citation; 600 came from the task."""
    task = dict(TASK_OK, constraint_parameters={"minimum_credit_score": 600})
    status, _msg, _rows = R.make_attestor(task, TOOL_OF, cite_check=True)(
        _steps(),
        "VERDICT username existence: SATISFIED - True\n"
        "VERDICT account balance sufficiency: SATISFIED - 200.0 against the 600 and the 50 asked for",
        HISTORY_RICH,
    )
    assert status == "ok"


def test_grounding_is_float_tolerant_and_ignores_prose():
    assert R.grounded_citation("balance 200 is plenty", "-> ok: 200.0") is None
    assert R.grounded_citation("it looked fine to me", "-> ok: 200.0") is None
    assert R.grounded_citation("balance 201", "-> ok: 200.0") == "201"
    assert R.grounded_citation("returned True", "-> ok: False") == "True"


def test_cite_check_is_off_unless_asked_for():
    """Round 4 must not silently change rounds 2 and 3."""
    status, _m, _r = R.make_attestor(TASK_OK, TOOL_OF)(
        _steps(),
        "VERDICT username existence: SATISFIED - returned True\n"
        "VERDICT account balance sufficiency: SATISFIED - balance 9999.0",
        HISTORY_RICH,
    )
    assert status == "ok"


# -- the engine's side -------------------------------------------------------


class _Msg:
    def __init__(self, content="", reasoning="", tool_calls=()):
        self.role = "assistant"
        self.content = content
        self.reasoning = reasoning
        self.tool_calls = tool_calls


def _engine(attestor):
    return E.ASOPEngine(
        asop=E.parse_asop(DOC),
        verifier=E.Verifier(identity="v", judge=lambda *a, **k: (True, "ok", False)),
        identity="executor",
        attestor=attestor,
    )


def _state_at_action():
    st = E.ASOPState(conversation=0)
    st.procedure = "Pay Bill"
    st.step_index = 2  # the final, acting step
    return st


def test_without_an_attestor_nothing_changes():
    eng = E.ASOPEngine(
        asop=E.parse_asop(DOC),
        verifier=E.Verifier(identity="v", judge=lambda *a, **k: (True, "ok", False)),
        identity="executor",
    )
    st = _state_at_action()
    assert eng.needs_attestation(st) is False
    assert "<current_step>" in eng.system_prompt_for(st)


def test_the_attestation_turn_forbids_the_action_and_names_every_condition():
    eng = _engine(lambda steps, said, hist: ("ok", "", []))
    prompt = eng.system_prompt_for(_state_at_action())
    assert "Do not\ncall any tool this turn" in prompt
    assert "username existence" in prompt
    assert "account balance sufficiency" in prompt


def test_a_refused_attestation_re_prompts_and_does_not_release_the_action():
    eng = _engine(lambda steps, said, hist: ("refuse", "missing: account balance", []))
    st = _state_at_action()
    eng.note_turn(st, _Msg(content="VERDICT username existence: SATISFIED - True"))
    assert st.attested is False
    assert st.attest_refusals == 1
    assert "missing: account balance" in eng.system_prompt_for(st)


def test_a_confirmed_failure_switches_the_run_to_a_terminal_prompt():
    eng = _engine(lambda steps, said, hist: ("blocked", "balance 10 vs requested 50", []))
    st = _state_at_action()
    eng.note_turn(st, _Msg(content="VERDICT account balance sufficiency: NOT SATISFIED - 10.0"))
    assert st.blocked
    prompt = eng.system_prompt_for(st)
    assert "must not be carried out" in prompt
    assert "Do not call the procedure's action" in prompt
    # And no gate re-fires to tell it to try again.
    eng.before_turn(_Msg(), st)
    assert st.refusal is None


def test_an_executor_that_never_produces_the_form_is_escalated_not_starved():
    eng = _engine(lambda steps, said, hist: ("refuse", "missing", []))
    st = _state_at_action()
    for _ in range(3):
        eng.note_turn(st, _Msg(content="I checked everything and it is fine."))
    assert st.attested is True  # stepped past
    assert any("attestation" in e for e in st.escalated)  # but recorded as never attested


# -- round 3: a failed VERIFY check is not "try again" -----------------------


DOC_KINDS = """\
Compiled procedure document.

## Procedure: Pay Bill

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool. Gate: deterministic (tool call: `login_user`)
2. **account balance sufficiency.** Condition `sufficient_account_balance`. VERIFY: call the get_account_balance tool. Gate: deterministic (tool call: `get_account_balance`)
3. **Pay Bill — the requested action.** ACT: call the pay_bill tool. Gate: deterministic (tool call: `pay_bill`)
"""


def test_establish_and_verify_steps_are_told_apart_from_the_document():
    steps = E.parse_asop(DOC_KINDS).procedure("Pay Bill").steps
    assert E.is_establish_step(steps[0]) is True
    assert E.is_establish_step(steps[1]) is False


def _engine_r3(attestor):
    eng = E.ASOPEngine(
        asop=E.parse_asop(DOC_KINDS),
        verifier=E.Verifier(identity="v", judge=lambda *a, **k: (True, "ok", False)),
        identity="executor",
        attestor=attestor,
        attest_on_failed_check=True,
        value_check=lambda step, hist: (False, "balance 10 vs requested 50")
        if "get_account_balance" in step.body
        else (True, "ok"),
    )
    return eng


def _state_on(step_index):
    st = E.ASOPState(conversation=0)
    st.procedure = "Pay Bill"
    st.step_index = step_index
    st.messages = []
    return st


class _ToolMsg:
    role = "tool"
    content = "10.0"
    tool_calls = ()
    error = False


class _Named:
    def __init__(self, name):
        self.name = name
        self.arguments = {}


def _called(tool):
    """An assistant turn that made `tool`'s call — the value gate runs only
    after liveness passes, so the history has to contain the call itself."""
    m = _Msg()
    m.role = "assistant"
    m.tool_calls = (_Named(tool),)
    m.error = False
    return m


def test_a_failed_verify_check_asks_the_executor_for_its_own_verdict():
    """It must not hand over what the check found — that IS the verdict wanted."""
    eng = _engine_r3(lambda steps, said, hist: ("ok", "", []))
    st = _state_on(1)
    st.messages = [_called("get_account_balance")]
    eng.run_gates(st, arrived=_ToolMsg())
    assert st.attest_condition == "account balance sufficiency."
    prompt = eng.system_prompt_for(st)
    assert "VERDICT <condition>" in prompt
    assert "10" not in prompt.split("<global_rules>")[0]  # the value is not given away
    assert "Saying so is a correct outcome" in prompt


def test_a_confirmed_failure_on_a_verify_step_ends_the_procedure():
    eng = _engine_r3(lambda steps, said, hist: ("blocked", "balance 10 vs requested 50", []))
    st = _state_on(1)
    st.messages = [_called("get_account_balance")]
    eng.run_gates(st, arrived=_ToolMsg())
    eng.note_turn(st, _Msg(content="VERDICT account balance sufficiency: NOT SATISFIED - 10.0"))
    assert st.blocked
    assert "must not be carried out" in eng.system_prompt_for(st)


def test_an_establish_step_keeps_the_retry_loop():
    """A login with the wrong password can genuinely be retried; a balance cannot."""
    eng = E.ASOPEngine(
        asop=E.parse_asop(DOC_KINDS),
        verifier=E.Verifier(identity="v", judge=lambda *a, **k: (True, "ok", False)),
        identity="executor",
        attestor=lambda steps, said, hist: ("blocked", "no", []),
        attest_on_failed_check=True,
        value_check=lambda step, hist: (False, "login_user returned False"),
    )
    st = _state_on(0)
    st.messages = [_called("login_user")]
    eng.run_gates(st, arrived=_ToolMsg())
    assert st.attest_condition is None
    assert st.consecutive_refusals == 1  # the ordinary loop, unchanged
    assert st.blocked is None


def test_the_reasoning_channel_counts_as_the_executors_own_output():
    """`gpt-oss-20b` empties `content` whenever it emits a tool call.

    Reading only `content` would refuse attestations the model did make, which
    would measure the harness's serialisation instead of the mechanism.
    """
    seen = {}

    def attestor(steps, said, hist):
        seen["said"] = said
        return ("ok", "", [])

    eng = _engine(attestor)
    eng.note_turn(_state_at_action(), _Msg(content="", reasoning="VERDICT x: SATISFIED - 1"))
    assert "VERDICT x: SATISFIED - 1" in seen["said"]
