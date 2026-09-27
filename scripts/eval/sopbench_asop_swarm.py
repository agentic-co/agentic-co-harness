#!/usr/bin/env python3
"""A SOPBench shell over this project's own ASOP gate runtime.

WHAT THIS IS FOR
----------------
`T2-PROCEDURE-LADDER.md` measured *procedure-in-prompt plus self-verification*
winning on SOPBench `bank` (+0.142) using **SOPBench's own PVA scaffold** — a
third party's prompt text. This project's own artifacts, the ASOP document
format and `asop_engine.ASOPEngine`'s stepwise gate runtime, had never been run
against SOPBench at all. They had only run against tau2, where they lost. This
module is the missing half: it drives the SAME engine that produced the tau2
numbers, inside SOPBench's loop.

Running the same engine rather than a reimplementation is the whole point. A
second implementation would answer "do two codebases differ?", not "does our
ASOP work on a host whose scorer can discriminate?".

THE SEAM
--------
SOPBench's agent is a passive pydantic record; the loop lives in
`Swarm.run_user_assistant_interaction`, which calls
`Swarm.get_chat_completion(agent, history, debug)` once per turn and rebuilds
the model's system prompt from `agent.instructions` on EVERY call
(`swarm/core.py:71`). That single fact is what makes this port a shell rather
than a rewrite: overriding one method gives the engine exactly the hook
`ASOPAgent.generate_next_message` is — observe the new evidence, run the gates,
rewrite the prompt — with no patch to the borrowed benchmark.

`Swarm` is also constructed per task (`run_simulation.py:170`), so ASOP state
resets per conversation for free.

THREE ADAPTATIONS THAT CHANGE SEMANTICS, DECLARED RATHER THAN HIDDEN
--------------------------------------------------------------------
1. **Termination.** tau2 had its own stop logic; SOPBench ends only when the
   assistant calls `exit_conversation`, else at the turn/action cap. The
   completion prompt therefore has to instruct that call or every gated run
   burns to the cap. `COMPLETION_SUFFIX` below does that, and it is appended
   ONLY at the end of a procedure.
2. **Routing happens before the first model call, not after it.** The engine
   routes in `note_turn`, i.e. after a turn has been spent on `TRIAGE_PROMPT`.
   In SOPBench the user's whole request is already present as message one and
   the scripted user only ever repeats it, so a triage turn buys nothing and
   costs one of twenty. The engine's own `route()` is used, unchanged — only
   *when* the shell calls it differs. Recorded because the turn budget is this
   experiment's single largest confound.
3. **`.error` is reconstructed.** SOPBench computes each tool call's success
   boolean and then discards it (`core.py:167-168`), but `check_tool_succeeded`
   — the only model-free gate in the engine — has no other discriminator. See
   `_OkRecorder`.

WHAT IS NOT ADAPTED
-------------------
The gate logic, the prompts, the evidence rendering, the verdict shapes and the
leakage guard are the engine's, untouched. Nothing here reads the task, its
`directed_action_graph`, its gold database or its evaluation criteria —
`_assert_no_gold` still fires inside `run_gates`, and the only inputs this shell
gives the engine are the ASOP text and the visible transcript.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from asop_engine import (  # noqa: E402
    ASOP,
    ASOPEngine,
    ASOPState,
    Verifier,
    next_conversation_id,
)


# ── message shim: SOPBench dicts -> what the engine reads by getattr ─────────
#
# The engine reads messages with `getattr` only (`.role`, `.content`,
# `.tool_calls[].name`, `.tool_calls[].arguments`, `.error`), which is why a
# shim of this size is enough. SOPBench carries tool calls in OpenAI's wire
# shape — `.function.name` and `.function.arguments` as a JSON *string* — and
# tool results as `{"role","tool_call_id","tool_name","content"}` with no error
# flag at all.


@dataclass(frozen=True)
class _Call:
    name: str
    arguments: dict


@dataclass(frozen=True)
class _Msg:
    role: str
    content: Any = ""
    tool_calls: tuple = ()
    error: bool = False
    # ⚠️ MEASURED, and it decides whether the verify-then-gate arm can work at
    # all: `gpt-oss-20b` through LM Studio returns an EMPTY `content` on every
    # turn that carries a tool call and puts its prose in a separate `reasoning`
    # field. Both are the model's own output. A gate that read only `content`
    # would be measuring the harness's serialisation rather than whether the
    # executor articulated anything — and would refuse every attestation made
    # alongside a call. Unused by every arm published before the attestor.
    reasoning: str = ""


def _call_of(tc: Any) -> Optional[_Call]:
    """One tool call, from either a dict (history) or an SDK object (completion)."""
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        name, raw = fn.get("name"), fn.get("arguments")
    else:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None)
        raw = getattr(fn, "arguments", None)
    if not name:
        return None
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except Exception:
            # Keep the unparsed text rather than dropping it: the verifier is
            # entitled to see that the executor emitted something malformed.
            args = {"_unparsed_arguments": raw}
    elif isinstance(raw, dict):
        args = raw
    else:
        args = {}
    return _Call(name=name, arguments=args if isinstance(args, dict) else {"_value": args})


def adapt(history: list, ok_by_call_id: dict) -> list[_Msg]:
    """SOPBench's history as engine-readable messages, in order."""
    out: list[_Msg] = []
    for m in history:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "?"
        calls = tuple(c for c in (_call_of(t) for t in (m.get("tool_calls") or [])) if c)
        error = False
        if role == "tool":
            # Absent a recorded flag, assume success: guessing FAILED would
            # make the deterministic gate refuse steps that in fact completed,
            # which is the exact false-refusal failure mode this port is meant
            # to avoid reproducing.
            error = not ok_by_call_id.get(m.get("tool_call_id"), True)
        reasoning = m.get("reasoning") or ""
        out.append(
            _Msg(
                role=role,
                content=m.get("content") or "",
                tool_calls=calls,
                error=error,
                reasoning="" if reasoning in ("None", None) else str(reasoning),
            )
        )
    return out


def adapt_completion(message: Any) -> _Msg:
    """The assistant message the model just produced (engine sees `.tool_calls`)."""
    calls = tuple(
        c for c in (_call_of(t) for t in (getattr(message, "tool_calls", None) or [])) if c
    )
    reasoning = getattr(message, "reasoning", None)
    if reasoning is None:
        # The OpenAI SDK keeps unmodelled response fields in `model_extra`
        # rather than as attributes on some versions; LM Studio's `reasoning`
        # is one of those. Read both rather than assuming which.
        reasoning = (getattr(message, "model_extra", None) or {}).get("reasoning")
    return _Msg(
        role="assistant",
        content=getattr(message, "content", "") or "",
        tool_calls=calls,
        reasoning=str(reasoning or ""),
    )


# ── the success flag SOPBench throws away ────────────────────────────────────


class _OkRecorder:
    """Wraps the domain system so each tool call's success boolean survives.

    `Swarm.handle_tool_calls` calls `getattr(self.system, name)(**args)` and
    then does `if isinstance(raw_result, tuple): raw_result = raw_result[1]` —
    the boolean is computed and dropped on the floor. `check_tool_succeeded`,
    the engine's only model-free gate, has no other discriminator, so without
    this every call would read as `ok` and a failed precondition check would
    gate exactly like a successful one.

    ⚠️ Bank's failure return is a BARE `False`, not `(False, ...)` —
    `env/domains/bank/bank.py:165` reads
    `if not self.domain_dep.process(...): return False`. A recorder that only
    understood the tuple form would score every constraint-blocked call as a
    success, which is the silent version of having no flag at all. Both shapes
    are handled below, and an exception counts as a failure.
    """

    def __init__(self, inner: Any, sink: list) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_sink", sink)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if not callable(attr):
            return attr
        sink = object.__getattribute__(self, "_sink")

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                res = attr(*args, **kwargs)
            except Exception:
                sink.append((name, False))
                raise
            if isinstance(res, tuple) and res and isinstance(res[0], bool):
                ok = res[0]
            elif isinstance(res, bool):
                ok = res
            else:
                ok = True
            sink.append((name, bool(ok)))
            return res

        return wrapped


# ── prompt suffixes the host requires ────────────────────────────────────────

# SOPBench ends a conversation ONLY when the assistant calls
# `exit_conversation`; there is no other stop. Without saying so, the run loops
# against a scripted user that just repeats itself and burns to the turn cap —
# which would be indistinguishable, in the final numbers, from the method
# failing. This text is therefore the HOST'S REQUIREMENT, stated neutrally, and
# it is appended identically in both arms so it cannot be a difference between
# them.
TERMINATION_NOTE = (
    "\n\nThis conversation ends only when you call the `exit_conversation` tool. "
    "Call it once the user's request has been carried out, or once you have "
    "established that the rules do not permit it."
)

# ⚠️ CAUGHT IN PRE-FLIGHT, AND IT WOULD HAVE MANUFACTURED THE FINDING.
# The first version of the note above was appended to every step prompt and
# read: "If the rules above mean this request must not be carried out, say so
# plainly and then call exit_conversation. Refusing correctly is a successful
# outcome; looping is not." On the very first gated task the executor cleared
# two gates, had `login_user` come back False on the third, read that gate
# refusal as "the rules forbid this", and exited — on a task whose
# `action_should_succeed` is True.
#
# A gate refusal means "this step is not satisfied YET, address what is missing
# and try again"; a rule prohibition means "this must not happen at all". The
# suffix collapsed the two, and it rewarded refusing. Over-refusal is the exact
# liability this experiment exists to measure, so an instruction that encourages
# it would have produced the loss it was meant to detect. The disambiguation
# below goes ONLY to the gated arm, because it is the only arm that has gate
# refusals to confuse — it removes a confound rather than adding an advantage.
# 🛑 THE INFORMATION GAP, MEASURED RATHER THAN ARGUED (see ASOP-PORT-MVP.md §3.3).
#
# `run_simulation.task_initializer` sets `assistant_agent.instructions` once per
# task from `assistant_info["instructions"]`: the domain's Core Operating
# Principles, the AND/OR/CHAIN composition legend, and the full per-action
# constraint specification WITH ITS THRESHOLDS (credit score > 600, deposit
# <= 10000, exchange <= 3000, balance >= amount, …). Every other arm keeps that
# text — `none` has it, `pva` and `action_order` append their scaffold to it,
# and `asop-prompt` appends our document to it.
#
# The gated arm alone assigns `agent.instructions = prompt` every turn, so it
# **replaces** that block instead of adding to it. Dumped on `bank` task 0:
# 10,753 bytes of constraint specification out, 1,830 bytes of ASOP prompt in,
# containing no thresholds and no composition logic at all. The compiled step
# text then reads "**The rules above** state what this condition must be for
# this request" — pointing at a block the runtime had already deleted.
#
# This is N24's defect arriving a second time from the other side. N24 found the
# gated arm being told about MORE preconditions than the task imposed and fixed
# it by narrowing; this is the gated arm being told the VALUES of none of them.
# Both make a loss attributable to information volume rather than to gating, and
# neither is a statement about whether stepwise enforcement works.
#
# Restoring the block is therefore parity, not advantage: it makes the gated arm
# the exact analogue of `asop-prompt` (host instructions + our document) plus
# enforcement, which is the contrast N26 claimed to be measuring. It is opt-in
# so both published gated arms stay reproducible byte for byte.
HOST_RULES_BLOCK = """\
<domain_rules>
{rules}
</domain_rules>

"""

GATE_REFUSAL_NOTE = (
    "\n\nA refused step is not a refused request: it means that step's "
    "precondition has not been established yet. Address what the refusal says "
    "is missing and try the step again."
)


# ── run-level instrumentation ────────────────────────────────────────────────


@dataclass
class RunStats:
    """The falsifier signatures, logged per conversation.

    Pre-registered in the scope document: any of these above threshold means
    the run is a plumbing result rather than a verdict on the method.
    """

    exited_cleanly: bool = False       # ended on exit_conversation, not the cap
    turns: int = 0
    tool_calls: int = 0
    gates_run: int = 0
    gates_passed: int = 0
    gates_deterministic: int = 0
    gates_fell_back: int = 0           # declared deterministic, got an opinion
    escalations: int = 0               # steps stepped past unattested
    procedure: Optional[str] = None    # what the router chose
    steps_total: int = 0
    step_index_reached: int = 0
    # -- verify-then-gate signatures; 0 in every arm without an attestor ------
    attest_attempts: int = 0        # how often the executor was asked to attest
    attest_ok: int = 0              # attestations that matched the tool results
    attest_refused: int = 0         # missing, uncited, or contradicted
    attest_contradicted: int = 0    # stated one thing, tools said another
    attest_blocked: bool = False    # executor said a condition fails; tools agreed
    attest_escalated: bool = False  # never produced the form; stepped past unattested
    verdict_lines: int = 0          # VERDICT lines the executor actually wrote

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ── the shell ────────────────────────────────────────────────────────────────


def make_asop_swarm(
    base_swarm_cls: type,
    asop: ASOP,
    verifier: Verifier,
    identity: str,
    route_fn: Optional[Callable[[str], str]] = None,
    max_refusals: int = 3,
    stats_sink: Optional[list] = None,
    asop_provider: Optional[Callable[[], ASOP]] = None,
    value_check_provider: Optional[Callable[[], Any]] = None,
    host_rules: bool = False,
    upfront: bool = False,
    attestor_provider: Optional[Callable[[], Any]] = None,
    attest_on_failed_check: bool = False,
):
    """Build a `Swarm` subclass that drives `ASOPEngine`.

    Returns a class, because `run_simulation.py` constructs `Swarm(...)` itself
    once per task. Installing it is one line from our own runner —
    `run_simulation.Swarm = make_asop_swarm(...)` — and needs no patch to the
    benchmark: `run_simulation` does `from swarm.core import *` and
    `swarm/core.py` declares no `__all__`, so the name lands in that module's
    globals and can be rebound there.
    """

    class ASOPSwarm(base_swarm_cls):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # `Swarm` is constructed once per task, which is what makes a
            # per-task document possible at all: the provider is called here
            # and sees the task `run_simulation` just initialised. See
            # `run_sopbench_asop.make_task_asop_provider` for why the document
            # has to be per task rather than domain-static.
            this_asop = asop_provider() if asop_provider is not None else asop
            self._engine = ASOPEngine(
                value_check=value_check_provider() if value_check_provider else None,
                asop=this_asop,
                verifier=verifier,
                identity=identity,
                route_fn=route_fn,
                max_refusals=max_refusals,
                upfront=upfront,
                attestor=attestor_provider() if attestor_provider else None,
                attest_on_failed_check=attest_on_failed_check,
            )
            self._state = ASOPState(conversation=next_conversation_id())
            self._ok_by_call_id: dict = {}
            self._stats = RunStats()
            self._assistant: Any = None
            self._host_instructions: Optional[str] = None
            self._dumped = 0
            if stats_sink is not None:
                stats_sink.append(self)

        # -- the per-turn hook -------------------------------------------

        def get_chat_completion(self, agent: Any, history: list, debug: bool = False):
            # The user agent is scripted and has no instructions to rewrite;
            # only the assistant is driven by the ASOP. Identified by having a
            # client, since the scripted user is built with `client=None`.
            is_assistant = getattr(agent, "client", None) is not None and (
                self._assistant is None or agent is self._assistant
            )
            if not is_assistant:
                return super().get_chat_completion(agent, history, debug)
            self._assistant = agent

            state = self._state
            msgs = adapt(history, self._ok_by_call_id)

            # ADAPTATION 2: route before spending a turn on triage. The engine's
            # own `route()` is used unchanged; only the timing is the shell's.
            if state.procedure is None and msgs:
                state.messages = msgs
                self._engine.note_turn(state, None)

            # The engine's contract: `state.messages` is everything BEFORE the
            # newly-arrived message, and `arrived` is judged explicitly. tau2
            # threads that through `generate_next_message(message, state)`;
            # here the arrival is simply the last entry in the history.
            arrived = msgs[-1] if msgs else None
            state.messages = msgs[:-1] if msgs else []
            self._engine.before_turn(arrived, state)

            # WHAT THE HOST BUILT, BEFORE WE OVERWRITE IT. `run_simulation`
            # sets `assistant_agent.instructions` once per task from
            # `assistant_info["instructions"]` — the domain rules plus THIS
            # task's constraint prose and thresholds. Every other arm keeps
            # that text; this arm replaces it. Captured (not restored) so the
            # difference is measurable rather than argued. See ASOP_DUMP_PROMPT.
            if self._host_instructions is None:
                self._host_instructions = getattr(agent, "instructions", "") or ""

            prompt = self._engine.system_prompt_for(state)
            if host_rules and self._host_instructions:
                prompt = HOST_RULES_BLOCK.format(rules=self._host_instructions) + prompt
            prompt += TERMINATION_NOTE
            if state.refusal:
                prompt += GATE_REFUSAL_NOTE
            agent.instructions = prompt
            self._dump_prompt(prompt)

            completion = super().get_chat_completion(agent, history, debug)

            state.messages = msgs
            try:
                produced = completion.choices[0].message
            except Exception:
                produced = None
            if produced is not None:
                self._engine.note_turn(state, adapt_completion(produced))

            self._record_stats(produced)
            return completion

        # -- what the executor actually reads ------------------------------

        def _dump_prompt(self, prompt: str) -> None:
            """Write the system prompt this arm installs, and the one it replaced.

            A prompt-level experiment is only as trustworthy as the prompt it
            actually sent. Reading the builder is not the same evidence, and
            this project has been burned by exactly that gap. Off unless
            `ASOP_DUMP_PROMPT` names a directory; one file per turn.
            """
            out = os.environ.get("ASOP_DUMP_PROMPT")
            if not out:
                return
            d = Path(out)
            d.mkdir(parents=True, exist_ok=True)
            conv = self._state.conversation
            if self._dumped == 0:
                (d / f"conv{conv:03d}-HOST-instructions.txt").write_text(
                    self._host_instructions or "<none>"
                )
            (d / f"conv{conv:03d}-turn{self._dumped:02d}-asop.txt").write_text(prompt)
            self._dumped += 1

        # -- keep the success boolean the base class discards -------------

        def handle_tool_calls(self, tool_calls, functions, context_variables, debug):
            sink: list = []
            real_system = self.system
            self.system = _OkRecorder(real_system, sink)
            try:
                response = super().handle_tool_calls(
                    tool_calls, functions, context_variables, debug
                )
            finally:
                self.system = real_system

            # Match recorded flags back to call ids BY NAME IN ORDER, not by
            # position: a tool the base class could not resolve appends an error
            # message without ever calling anything, so positions do not line up.
            by_name: dict = {}
            for name, ok in sink:
                by_name.setdefault(name, []).append(ok)
            for tc in tool_calls:
                name = getattr(getattr(tc, "function", None), "name", None)
                pending = by_name.get(name)
                self._ok_by_call_id[getattr(tc, "id", None)] = (
                    pending.pop(0) if pending else False
                )
            return response

        # -- instrumentation ---------------------------------------------

        def _record_stats(self, produced: Any) -> None:
            s, state = self._stats, self._state
            s.turns += 1
            calls = getattr(produced, "tool_calls", None) or []
            s.tool_calls += len(calls)
            for tc in calls:
                if getattr(getattr(tc, "function", None), "name", None) == "exit_conversation":
                    s.exited_cleanly = True
            s.gates_run = len(state.verdicts)
            s.gates_passed = sum(1 for v in state.verdicts if v.get("passed"))
            s.gates_deterministic = sum(
                1 for v in state.verdicts if v.get("gate") == "deterministic"
            )
            s.gates_fell_back = sum(1 for v in state.verdicts if v.get("fell_back"))
            s.escalations = len(state.escalated)
            s.procedure = state.procedure
            s.step_index_reached = state.step_index
            atts = getattr(state, "attestations", [])
            s.attest_attempts = len(atts)
            s.attest_ok = sum(1 for a in atts if a["status"] == "ok")
            s.attest_refused = sum(1 for a in atts if a["status"] == "refuse")
            s.attest_contradicted = sum(
                1
                for a in atts
                for r in a.get("rows", [])
                if r.get("truth") is not None and r.get("stated") is not None
                and r["truth"] != r["stated"]
            )
            s.attest_blocked = bool(getattr(state, "blocked", None))
            s.attest_escalated = any(
                str(e).endswith("· attestation") for e in state.escalated
            )
            s.verdict_lines = sum(
                1 for a in atts for r in a.get("rows", []) if r.get("stated") is not None
            )
            pos = self._engine.position(state)
            s.steps_total = len(pos.procedure.steps) if pos.procedure else 0

    return ASOPSwarm
