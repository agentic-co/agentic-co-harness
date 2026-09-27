#!/usr/bin/env python3
"""Arm (c): execute an ASOP stepwise, with gates, inside τ²-bench.

Arms A/B/C1-C3 of the 2026-09-12 run all did the same thing — hand the whole
document to the model as one system prompt and let it run an uninterrupted
conversation. Nothing walked a step. Nothing evaluated a gate. Those arms
measured the SHAPE OF A PROMPT, which is not the ASOP claim.

This adapter is the missing arm. Same document, different execution model:

  * the model sees ONE step at a time, not the whole procedure;
  * after each turn the current step's gate is evaluated;
  * a gate that refuses sends the model back to the same step with the reason;
  * the executor never decides whether its own step passed.

Compare (b) single-shot ASOP against (c) this, holding the document constant,
and the difference is the execution model rather than the formatting.

──────────────────────────────────────────────────────────────────────────────
THIS FILE IS NOW A SHELL. THE LOGIC LIVES IN `asop_engine.py`.

Everything that is not τ²-specific — parsing, evidence rendering, the
deterministic gate, the verifier, the prompts, the gate scheduler — moved to
`asop_engine.ASOPEngine` so that SOPBench could run THE SAME CODE rather than a
second implementation of it. A reimplementation would have answered a different
question: you would be comparing two codebases, not two hosts.

What remains here is exactly the τ² coupling the engine refuses to carry:

  * `ASOPAgentState(LLMAgentState)` — tau2's pydantic state base;
  * `ASOPAgent(LLMAgent)`          — whose `super().generate_next_message()`
                                     IS the model call;
  * `SystemMessage`                — tau2's system-prompt envelope;
  * `create_asop_agent` / `register` — tau2's factory registry.

The names the engine owns are re-exported below, so every existing importer
(`tests/test_asop_agent.py`, `run_arm_c.py`, `asop_smoke.py`) keeps working
unchanged against `asop_agent.<name>`.

⚠️ **Behaviour here must not drift.** Every published τ² number was produced by
this path, and this tree has NO tau2 checkout — `tests/test_asop_agent.py`
skips every agent test without one. So the shell below is not covered by any
test that runs here, and a change to it would be silently unfalsifiable.
`tests/test_asop_engine.py` covers the extracted logic instead, which is the
half that both hosts share.
──────────────────────────────────────────────────────────────────────────────

Registers as the agent `asop_stepwise`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
from typing import Callable, Optional

# `scripts/eval` is a tool directory, not a package, and `tests/conftest`-style
# loaders bring this file in by PATH (`spec_from_file_location`), which leaves
# its directory off `sys.path`. Put it on so the sibling engine resolves the
# same way whether this module was imported by name or by path.
_HERE = _Path(__file__).resolve().parent
if str(_HERE) not in _sys.path:
    _sys.path.insert(0, str(_HERE))

from asop_engine import (  # noqa: E402  (path bootstrap must precede this)
    ASOP,
    COMPLETE_PROMPT,
    Evidence,
    GateKind,
    JudgeFn,
    Procedure,
    REFUSAL_BLOCK,
    RESTART_PROMPT,
    STEP_PROMPT,
    Step,
    TOOL_RESULT_CLIP,
    TRIAGE_PROMPT,
    VERIFIER_PROMPT,
    ASOPEngine,
    ASOPState,
    Verdict,
    Verifier,
    _assert_no_gold,
    _clip,
    _CONVERSATION,
    _GATE_RE,
    _GOLD_ATTRS,
    _Position,
    _record,
    _render_args,
    _render_turn,
    _sink_path,
    _tool_history,
    check_tool_succeeded,
    is_conditional,
    named_tool,
    next_conversation_id,
    parse_asop,
    parse_verdict,
    require_verifier,
)

__all__ = [
    "ASOP",
    "ASOPAgent",
    "ASOPAgentState",
    "ASOPEngine",
    "ASOPState",
    "Evidence",
    "GateKind",
    "Procedure",
    "Step",
    "Verdict",
    "Verifier",
    "check_tool_succeeded",
    "create_asop_agent",
    "is_conditional",
    "named_tool",
    "parse_asop",
    "parse_verdict",
    "register",
    "require_verifier",
]


# ── the agent ────────────────────────────────────────────────────────────────

try:  # tau2 is a checkout the experiment borrows, never a dependency here.
    from tau2.agent.llm_agent import LLMAgent, LLMAgentState
    from tau2.data_model.message import SystemMessage
except ImportError:  # pragma: no cover - exercised only outside a tau2 tree
    LLMAgent = object  # type: ignore[assignment,misc]
    LLMAgentState = object  # type: ignore[assignment,misc]
    SystemMessage = None  # type: ignore[assignment]


class ASOPAgentState(LLMAgentState):  # type: ignore[misc,valid-type]
    """LLMAgentState plus where we are in the procedure.

    Field-for-field identical to `asop_engine.ASOPState`, which is what the
    non-tau2 hosts use. The engine reads both by attribute and cannot tell them
    apart; the only reason two exist is that tau2 requires its own pydantic
    base and the engine refuses to import one.
    """

    conversation: int = -1
    consecutive_refusals: int = 0
    step_started_at: int = 0
    escalated: list[str] = []
    procedure: Optional[str] = None
    step_index: int = 0
    instances: int = 0  # completed passes of the current procedure (multiplicity)
    refusal: Optional[str] = None
    verdicts: list[dict] = []


class ASOPAgent(LLMAgent):  # type: ignore[misc,valid-type]
    """An executor that is shown one step at a time and cannot clear its own gates.

    A τ² shell over `ASOPEngine`. The only thing this class contributes is the
    model call — `super().generate_next_message()` — and tau2's message
    envelope. Every decision about which step is current, whether its gate
    passed, and what prompt the executor sees is the engine's.
    """

    def __init__(
        self,
        tools,
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
        verifier: Optional[Verifier] = None,
        identity: str = "executor",
        route_fn: Optional[Callable[[str], str]] = None,
        max_refusals: int = 3,
        max_instances: int = 4,
        human_gate_unavailable: bool = True,
    ) -> None:
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args
        )
        self.asop = parse_asop(domain_policy)
        self.identity = identity
        self._engine = ASOPEngine(
            asop=self.asop,
            verifier=verifier,
            identity=identity,
            route_fn=route_fn,
            max_refusals=max_refusals,
            max_instances=max_instances,
            human_gate_unavailable=human_gate_unavailable,
        )
        # Kept as attributes because callers and the write-up read them off the
        # agent. `verdicts` is the SAME list object the engine appends to, not a
        # copy — a copy would silently stop tracking after construction.
        self.verifier = self._engine.verifier
        self.max_refusals = self._engine.max_refusals
        self.max_instances = self._engine.max_instances
        self.human_gate_unavailable = self._engine.human_gate_unavailable
        self.verdicts = self._engine.verdicts

    # -- prompt ----------------------------------------------------------

    def _position(self, state: "ASOPAgentState") -> _Position:
        return self._engine.position(state)

    def _system_prompt_for(self, state: "ASOPAgentState") -> str:
        return self._engine.system_prompt_for(state)

    # -- turn ------------------------------------------------------------

    def generate_next_message(self, message, state):  # type: ignore[override]
        # The system prompt is rebuilt every turn. This is the difference from
        # every arm that came before: the model is never holding the whole
        # procedure, so "followed the procedure" cannot mean "happened to
        # mention something from a document it could see all of".
        #
        # Gates are evaluated at the TOP of the turn, on the evidence that has
        # just ARRIVED, before the next step is chosen and the prompt is built.
        self._engine.before_turn(message, state)

        state.system_messages = [
            SystemMessage(role="system", content=self._engine.system_prompt_for(state))
        ]
        assistant_message, state = super().generate_next_message(message, state)

        self._engine.note_turn(state, assistant_message)
        return assistant_message, state

    def _is_new_evidence(self, message, state: "ASOPAgentState") -> bool:
        return self._engine.is_new_evidence(message, state)

    def _route(self, state: "ASOPAgentState") -> Optional[str]:
        return self._engine.route(state)

    def _run_gates(self, state: "ASOPAgentState", arrived=None) -> None:
        self._engine.run_gates(state, arrived=arrived)

    def get_init_state(self, message_history=None):  # type: ignore[override]
        base = super().get_init_state(message_history)
        return ASOPAgentState(
            system_messages=base.system_messages,
            messages=base.messages,
            conversation=next_conversation_id(),
            consecutive_refusals=0,
            step_started_at=0,
            escalated=[],
            procedure=None,
            step_index=0,
            instances=0,
            refusal=None,
            verdicts=[],
        )


def create_asop_agent(tools, domain_policy, **kwargs):
    """Factory for τ²-bench's registry.

    `verifier_llm` defaults to the executor's model but NEVER to its identity —
    same weights judging a different transcript is a weaker check than a
    different model, and the write-up has to say which was used, so it is
    recorded on every verdict.
    """
    llm = kwargs.pop("llm")
    llm_args = kwargs.pop("llm_args", None)
    verifier_llm = kwargs.pop("verifier_llm", llm)
    judge = kwargs.pop("judge", None)

    if judge is None:
        from tau2.utils.llm_utils import generate  # local: tau2 only

        def judge(prompt: str, allow_na: bool = False) -> tuple[bool, str, bool]:
            reply = generate(
                model=verifier_llm,
                tools=[],
                messages=[SystemMessage(role="system", content=prompt)],
                call_name="asop_gate_verdict",
            )
            return parse_verdict(str(getattr(reply, "content", "") or ""), allow_na)

    def route_fn(prompt: str) -> str:
        from tau2.utils.llm_utils import generate

        reply = generate(
            model=verifier_llm,
            tools=[],
            messages=[SystemMessage(role="system", content=prompt)],
            call_name="asop_route",
        )
        return str(getattr(reply, "content", "") or "")

    return ASOPAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=llm,
        llm_args=llm_args,
        verifier=Verifier(identity=f"verifier:{verifier_llm}", judge=judge),
        identity=f"executor:{llm}",
        route_fn=kwargs.pop("route_fn", route_fn),
        max_refusals=kwargs.pop("max_refusals", 3),
    )


def register() -> None:
    """Register `asop_stepwise` with τ²-bench. Idempotent."""
    from tau2.registry import registry

    try:
        registry.register_agent_factory(create_asop_agent, "asop_stepwise")
    except Exception:  # already registered
        pass
