#!/usr/bin/env python3
"""Prove arm (c) actually walks steps and gates them — with no model involved.

Run inside the tau2 checkout's venv:

    <tau2>/.venv/bin/python scripts/eval/asop_smoke.py --tau2 <tau2 checkout>

The executor's replies and the verifier's verdicts are both stubbed, so what
this exercises is the execution model and nothing else. A smoke test that
called a real model would pass or fail on the weather and tell you nothing
about whether the gate logic is wired up.

Three things it has to show, because all three are the difference between arm
(c) and arm (b):

  1. the executor is shown ONE step, never the whole procedure;
  2. a refused gate leaves the step where it was, and hands back the reason;
  3. a passed gate advances, and the next turn shows the next step.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def load_adapter():
    src = Path(__file__).resolve().parent / "asop_agent.py"
    spec = importlib.util.spec_from_file_location("asop_agent", src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["asop_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument(
        "--asop",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "evals/tau2-airline-asop/asops/asop.claude.md",
    )
    args = ap.parse_args()

    sys.path.insert(0, str(args.tau2 / "src"))
    import tau2.agent.llm_agent as llm_agent
    from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

    aa = load_adapter()

    # ---- stub the executor -------------------------------------------------
    # Every turn returns a tool call, so the gate fires every turn. What the
    # executor "says" is irrelevant here; what matters is what it was SHOWN.
    shown: list[str] = []

    def fake_generate(model, tools, messages, call_name, **kw):
        shown.append(messages[0].content)
        return AssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="1", name="get_reservation_details", arguments={})],
        )

    llm_agent.generate = fake_generate

    # ---- stub the verifier -------------------------------------------------
    # Refuse the first attempt, pass every one after. That is the minimum shape
    # that can distinguish "walks steps" from "advances no matter what".
    calls = {"n": 0}

    def judge(_prompt: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "the user id was never supplied"
        return True, "the precondition is visible in the transcript"

    # Three model roles, all stubbed: executor, verifier, router. Routing is
    # infrastructure and runs for every document; without a router the agent
    # correctly stays in triage forever, which is fail-closed but proves
    # nothing about gates.
    def route(_prompt: str) -> str:
        return "Cancel Flight"

    agent = aa.ASOPAgent(
        tools=[],
        domain_policy=args.asop.read_text(),
        llm="stub-executor",
        verifier=aa.Verifier(identity="stub-verifier", judge=judge),
        identity="stub-executor",
        route_fn=route,
        max_refusals=3,
    )
    # The constructor must reject same-identity; give the verifier its own.
    state = agent.get_init_state([])

    print(f"ASOP: {args.asop.name}")
    print(f"procedures: {[p.name for p in agent.asop.procedures]}\n")

    turns = [
        "I need to cancel my flight, reservation ABC123.",
        "My user id is mya_gonzalez_1234.",
        "The airline cancelled it.",
        "Yes, go ahead.",
    ]
    for i, text in enumerate(turns, 1):
        _msg, state = agent.generate_next_message(
            UserMessage(role="user", content=text), state
        )
        cur = state.procedure or "(triage — no procedure selected yet)"
        print(f"turn {i}: procedure={cur!r} step_index={state.step_index}")
        if state.refusal:
            print(f"        gate REFUSED -> {state.refusal}")

    print("\n--- what the executor was shown ---")
    for i, prompt in enumerate(shown, 1):
        block = ""
        if "<current_step>" in prompt:
            block = prompt.split("<current_step>")[1].split("</current_step>")[0]
            block = " ".join(block.split())[:96]
        else:
            block = "(triage prompt — procedure list only)"
        print(f"  turn {i}: {block}")

    print("\n--- verdicts recorded ---")
    for v in state.verdicts:
        mark = "PASS" if v["passed"] else "FAIL"
        fb = "  [fell back from deterministic]" if v["fell_back"] else ""
        print(f"  {mark}  {v['step']}  gate={v['gate']}{fb}")
        print(f"        {v['verifier']} on {v['executor']}: {v['reason']}")

    # ---- the three assertions ---------------------------------------------
    ok = True
    whole = [p for p in shown if p.count("Procedure") > 2 and "<current_step>" in p]
    if whole:
        print("\nFAIL: a prompt contained more than one procedure's steps")
        ok = False
    if not state.verdicts:
        print("\nFAIL: no gate was ever evaluated — this is arm (b)")
        ok = False
    if not any(not v["passed"] for v in state.verdicts):
        print("\nFAIL: no gate ever refused; a gate that cannot fail is not a gate")
        ok = False
    if state.verdicts and state.step_index == 0 and len(state.verdicts) > 2:
        print("\nFAIL: gates passed but the procedure never advanced")
        ok = False

    # --- scenario 2: a gate that always refuses must escalate -------------
    calls["n"] = 0

    def always_refuse(_prompt: str):
        return False, "never satisfied"

    agent2 = aa.ASOPAgent(
        tools=[],
        domain_policy=args.asop.read_text(),
        llm="stub-executor",
        verifier=aa.Verifier(identity="stub-verifier", judge=always_refuse),
        identity="stub-executor",
        route_fn=route,
        max_refusals=3,
    )
    st2 = agent2.get_init_state([])
    for _ in range(8):
        _m, st2 = agent2.generate_next_message(
            UserMessage(role="user", content="cancel it"), st2
        )

    print("\n--- scenario 2: a gate that never passes ---")
    print(f"  escalated: {st2.escalated}")
    print(f"  step_index reached: {st2.step_index}")
    if not st2.escalated:
        print("  FAIL: the run looped on one step instead of escalating")
        ok = False
    if st2.step_index == 0:
        print("  FAIL: escalation did not unblock the procedure")
        ok = False
    if any(v["passed"] for v in st2.verdicts):
        print("  FAIL: a gate that always refuses recorded a pass")
        ok = False

    print("\nsmoke:", "ok" if ok else "BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
