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
THE LEAKAGE RULE, which is the whole reason this file can be trusted

τ²-bench scores by hashing the final database against a gold database built
from the task's recorded actions. A gate that consulted those actions — or the
gold hash, or `task.evaluation_criteria` — would be reading the answer key and
feeding it to the treatment arm. Every number afterwards would be worthless,
and the run would still exit cleanly.

So gates here derive ONLY from the ASOP text and the visible transcript.
`_assert_no_gold` enforces it at construction, and the evaluator is handed a
narrow `Evidence` record rather than anything task-shaped. If you extend this
file, the invariant to preserve is: the gate may read what the ASOP says and
what was said or called in the conversation. Nothing else.
──────────────────────────────────────────────────────────────────────────────

Registers as the agent `asop_stepwise`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
import itertools
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

# ── the ASOP, parsed ─────────────────────────────────────────────────────────


class GateKind(str, Enum):
    """The three kinds the spec defines, plus the one honesty demands.

    `DETERMINISTIC_UNAVAILABLE` is not in the spec. It is what this adapter
    records when an ASOP *claims* a deterministic gate but names no re-runnable
    check — which, in this domain, is most of them. "Cabin class must be the
    same across all flights" is a rule, not a command; it cannot be re-run and
    compared.

    Calling those `deterministic` anyway would let the arm claim a rigour it
    does not have. They fall through to a judged verdict, and the fallback is
    recorded per step so the write-up can say how often it happened.
    """

    DETERMINISTIC = "deterministic"
    DETERMINISTIC_UNAVAILABLE = "deterministic_unavailable"
    JUDGED = "judged"
    HUMAN = "human"


@dataclass(frozen=True)
class Step:
    number: int
    title: str
    body: str
    gate_kinds: tuple[GateKind, ...]
    procedure: str

    @property
    def label(self) -> str:
        return f"{self.procedure} · step {self.number}"


@dataclass(frozen=True)
class Procedure:
    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class ASOP:
    preamble: str
    procedures: tuple[Procedure, ...]

    def procedure(self, name: str) -> Optional[Procedure]:
        want = name.strip().lower()
        for p in self.procedures:
            if p.name.strip().lower() == want:
                return p
        return None


# Three models wrote these under identical instructions and produced three
# incompatible structures. That divergence is a finding about ASOP as an
# interchange format, not a parser inconvenience — see PARSE NOTE below.
#
#   claude  ## Procedure: X        1. **Title.** … Gate: human.
#   codex   ## Procedure 1 — X     1. **Title.** … Gate — deterministic: …
#   agy     ## Procedure: X        ### Step 1: Title / - **Gate:** `human`
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_STEP_HEADING_RE = re.compile(
    r"^###\s+Step\s+(\d+)\s*[:.\-—]?\s*(.*?)\s*$(.*?)(?=^###\s+Step\s+\d+|\Z)",
    re.M | re.S,
)
_STEP_LIST_RE = re.compile(r"^(\d+)\.\s+(.*?)(?=^\d+\.\s|\Z)", re.M | re.S)
# "Gate:", "Gate —", "**Gate:**", with the kind optionally in backticks.
_GATE_RE = re.compile(
    r"\*{0,2}Gate\*{0,2}\s*[:—–-]\s*`?(.+?)`?(?:\.\s|\.$|\n|$)", re.I
)


def _gate_kinds(step_body: str) -> tuple[GateKind, ...]:
    """Read the declared gate(s) off a step.

    A step may declare more than one — "Gate: human, then deterministic (tool
    call)" is two gates in sequence, and both have to hold.
    """
    m = _GATE_RE.search(step_body)
    if not m:
        return (GateKind.JUDGED,)
    text = m.group(1).lower()
    kinds: list[GateKind] = []
    for part in re.split(r",\s*then\s*|,\s*|\s+then\s+", text):
        part = part.strip()
        if not part:
            continue
        if part.startswith("human"):
            kinds.append(GateKind.HUMAN)
        elif part.startswith("judged"):
            kinds.append(GateKind.JUDGED)
        elif part.startswith("deterministic"):
            # A deterministic gate has to name something re-runnable. In this
            # domain that means a tool call; a parenthetical like "(agent-side
            # rule check)" names a rule, which is not a check.
            kinds.append(
                GateKind.DETERMINISTIC
                if "tool" in part or "api" in part
                else GateKind.DETERMINISTIC_UNAVAILABLE
            )
    return tuple(kinds) or (GateKind.JUDGED,)


def _steps_in(body: str, procedure: str) -> tuple[Step, ...]:
    """Extract steps from one section, trying both layouts.

    Heading-style first: a document using `### Step N:` also contains numbered
    lists *inside* steps (preconditions, options), and the list matcher would
    happily shred those into bogus steps.
    """
    found = [
        (int(m.group(1)), f"**{m.group(2)}**\n{m.group(3)}")
        for m in _STEP_HEADING_RE.finditer(body)
    ]
    if not found:
        found = [(int(m.group(1)), m.group(2)) for m in _STEP_LIST_RE.finditer(body)]
    return tuple(
        Step(
            number=n,
            title=_title_of(raw),
            body=raw.strip(),
            gate_kinds=_gate_kinds(raw),
            procedure=procedure,
        )
        for n, raw in found
    )


def parse_asop(markdown: str) -> ASOP:
    """Split an extracted ASOP into procedures and steps.

    PARSE NOTE — worth more than the code. Three models converted the same
    prose under identical instructions and produced three mutually
    unparseable structures: numbered lists, `### Step N:` headings, and three
    different gate syntaxes (`Gate:`, `Gate —`, `**Gate:** \\`kind\\``). Nothing
    in the extraction prompt invited that, and an ASOP that only one reader can
    walk is not portable in the sense the spec claims. That belongs in the
    write-up.

    A section counts as a procedure when steps can be read out of it, rather
    than because its heading matched a pattern — guessing which headings are
    procedures is how one arm silently ends up with fewer gates than another.
    """
    bounds = [(m.start(), m.group(1)) for m in _SECTION_RE.finditer(markdown)]
    if not bounds:
        raise ValueError("no '## ' sections — is this an ASOP?")

    preamble_parts = [markdown[: bounds[0][0]].strip()]
    procedures: list[Procedure] = []
    for i, (start, name) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(markdown)
        block = markdown[start:end]
        body = block.split("\n", 1)[1] if "\n" in block else ""
        clean = _clean_name(name)
        steps = _steps_in(body, clean)
        if steps:
            procedures.append(Procedure(name=clean, steps=steps))
        else:
            # Reference data, scope, definitions — context the executor needs
            # on every step, so it rides along in the preamble rather than
            # being dropped for failing to look like a procedure.
            preamble_parts.append(block.strip())

    if not procedures:
        raise ValueError("no section yielded a step — is this an ASOP?")
    return ASOP(
        preamble="\n\n".join(p for p in preamble_parts if p),
        procedures=tuple(procedures),
    )


def _clean_name(heading: str) -> str:
    """"Procedure: Cancel Flight" and "Procedure 3 — Cancel a flight" -> the name.

    Each extractor prefixed its headings differently. Keeping the prefix would
    make the same procedure a different string per arm, and the step-selection
    lookup would miss on two of the three.
    """
    return re.sub(r"^\s*Procedure\s*\d*\s*[:—–\-]?\s*", "", heading, flags=re.I).strip()


def _title_of(step_body: str) -> str:
    m = re.match(r"\s*\*\*(.+?)\*\*", step_body)
    if m:
        return m.group(1).strip()
    return step_body.strip().split(".")[0][:80]




# ── the verdict sink ─────────────────────────────────────────────────────────
#
# The point of walking steps is not a better score. It is that a failure lands
# ON A STEP — "Cancel Flight step 4's gate refused because eligibility was
# never checked" — instead of on a whole run. A step-located refusal is an
# adjudication, and adjudications are what draft the next version. A single
# pass/fail for the conversation cannot feed that loop at all.
#
# So every verdict is written out as it happens, not summarised at the end.
# tau2 builds agents inside its own runner, so there is no handle to read them
# off afterwards; a sink the agent writes to is the way out that does not
# require patching the benchmark.

_SINK_LOCK = threading.Lock()
_CONVERSATION = itertools.count()


def _sink_path() -> Optional[Path]:
    raw = os.environ.get("ASOP_VERDICT_LOG")
    return Path(raw) if raw else None


def _record(entry: dict) -> None:
    path = _sink_path()
    if path is None:
        return
    with _SINK_LOCK:
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")


# ── evidence and verdicts ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evidence:
    """Everything a gate is allowed to see. Deliberately narrow.

    Note what is absent: the task, its evaluation criteria, its gold actions,
    the environment's database, and any hash of it. A gate that could reach
    those would be grading the treatment against the answer key.
    """

    step: Step
    transcript: tuple[str, ...]
    tool_calls: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    gate_kind: GateKind
    verifier: str
    executor: str
    fell_back: bool = False

    def __post_init__(self) -> None:
        # The invariant the whole project rests on. An attestation naming the
        # executor as its own verifier is not a verdict.
        if self.verifier == self.executor:
            raise ValueError(
                f"executor {self.executor!r} attested its own work on "
                f"{self.step_label if hasattr(self, 'step_label') else 'a step'}"
            )
        if not self.reason.strip():
            raise ValueError("a verdict must say what it found, not merely pass")


# ── the verifier ─────────────────────────────────────────────────────────────

JudgeFn = Callable[[str], tuple[bool, str]]

VERIFIER_PROMPT = """\
You are a verifier. You did not do this work and you are not helping anyone \
finish it.

A procedure step was attempted. Decide ONE thing: does the visible evidence \
show the step's stated preconditions and prohibitions were satisfied?

THE STEP
{step}

WHAT WAS SAID AND DONE
{evidence}

Answer with a verdict line and nothing else:
PASS — <what you found true>
or
FAIL — <which precondition or prohibition is unmet>

Refuse to pass a step whose evidence is merely plausible. Absence of evidence \
is FAIL. You are not required to be helpful.\
"""


class Verifier:
    """An independent party. Separate identity, separate call.

    `judge` is injected so tests can drive verdicts without a model, and so the
    verifier can run on a different model from the executor. Its identity is
    carried on every verdict and checked against the executor's.
    """

    def __init__(self, identity: str, judge: JudgeFn) -> None:
        if not identity:
            raise ValueError("a verifier must be nameable")
        self.identity = identity
        self._judge = judge

    def attest(self, ev: Evidence, kind: GateKind, executor: str) -> Verdict:
        if self.identity == executor:
            raise ValueError(
                f"verifier and executor are both {executor!r} — "
                "the separation this gate exists to enforce is absent"
            )
        prompt = VERIFIER_PROMPT.format(
            step=f"{ev.step.label}: {ev.step.body}",
            evidence="\n".join(ev.transcript[-12:] + ev.tool_calls[-12:]) or "(nothing)",
        )
        passed, reason = self._judge(prompt)
        return Verdict(
            passed=passed,
            reason=reason,
            gate_kind=kind,
            verifier=self.identity,
            executor=executor,
            fell_back=kind is GateKind.DETERMINISTIC_UNAVAILABLE,
        )


def parse_verdict(raw: str) -> tuple[bool, str]:
    """Read a verdict line. Anything unrecognised is a FAIL, never a PASS.

    A verifier that returns something unparseable has not formed a verdict, and
    the safe reading of "no verdict" is that the gate did not pass. Defaulting
    the other way is how a broken judge turns into a green run.
    """
    head = raw.strip().splitlines()[0] if raw.strip() else ""
    m = re.match(r"\s*(PASS|FAIL)\b\s*[—:-]?\s*(.*)", head, re.I)
    if not m:
        return False, f"unparseable verdict: {head[:120]!r}"
    reason = m.group(2).strip() or "no reason given"
    return m.group(1).upper() == "PASS", reason


# ── guard ────────────────────────────────────────────────────────────────────

_GOLD_ATTRS = ("evaluation_criteria", "actions", "get_db_hash", "reward_info")


def _assert_no_gold(obj: Any, where: str) -> None:
    """Refuse anything carrying the answer key.

    Cheap, and it fires at construction rather than after a run that already
    looks clean. The failure this prevents does not announce itself: a gate
    with access to gold passes exactly the runs it should and the arm posts a
    beautiful, meaningless number.
    """
    for attr in _GOLD_ATTRS:
        if hasattr(obj, attr):
            raise ValueError(
                f"{where} was handed {type(obj).__name__}, which exposes "
                f"{attr!r}. Gates read the ASOP and the transcript, nothing "
                "that could carry the gold state."
            )


# ── the agent ────────────────────────────────────────────────────────────────

try:  # tau2 is a checkout the experiment borrows, never a dependency here.
    from tau2.agent.llm_agent import LLMAgent, LLMAgentState
    from tau2.data_model.message import SystemMessage
except ImportError:  # pragma: no cover - exercised only outside a tau2 tree
    LLMAgent = object  # type: ignore[assignment,misc]
    LLMAgentState = object  # type: ignore[assignment,misc]
    SystemMessage = None  # type: ignore[assignment]


STEP_PROMPT = """\
<instructions>
You are a customer service agent working ONE step of a procedure at a time.

You are on this step. Do not work ahead, and do not announce steps you have not
reached. When the step's preconditions are met, take its action.

A verifier that is not you decides whether this step passed. You do not mark
your own work complete, and saying a step is done does not make it so.

In each turn you may either send a message to the user or make a tool call,
never both. Generate valid JSON only.
</instructions>

<global_rules>
{preamble}
</global_rules>

<current_step>
{step}
</current_step>
{refusal}
<remaining>
After this step: {remaining}
</remaining>\
"""

TRIAGE_PROMPT = """\
<instructions>
You are a customer service agent. Find out what the user needs, then follow the
procedure that matches. Send a message to the user; do not call tools yet.

In each turn you may either send a message to the user or make a tool call,
never both. Generate valid JSON only.
</instructions>

<global_rules>
{preamble}
</global_rules>

<available_procedures>
{procedures}
</available_procedures>\
"""

REFUSAL_BLOCK = """
<gate_refused>
The verifier refused this step: {reason}

You have NOT completed it. Address what is missing before trying again.
</gate_refused>
"""


class ASOPAgentState(LLMAgentState):  # type: ignore[misc,valid-type]
    """LLMAgentState plus where we are in the procedure."""

    conversation: int = -1
    procedure: Optional[str] = None
    step_index: int = 0
    refusal: Optional[str] = None
    verdicts: list[dict] = []


@dataclass
class _Position:
    procedure: Optional[Procedure]
    index: int


class ASOPAgent(LLMAgent):  # type: ignore[misc,valid-type]
    """An executor that is shown one step at a time and cannot clear its own gates."""

    def __init__(
        self,
        tools,
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
        verifier: Optional[Verifier] = None,
        identity: str = "executor",
    ) -> None:
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args
        )
        self.asop = parse_asop(domain_policy)
        self.identity = identity
        if verifier is None:
            raise ValueError(
                "arm (c) requires a verifier. Running without one is arm (b) "
                "with extra steps, and would be reported as if it were this."
            )
        if verifier.identity == identity:
            raise ValueError("verifier and executor must be different parties")
        self.verifier = verifier
        self.verdicts: list[Verdict] = []

    # -- prompt ----------------------------------------------------------

    def _position(self, state: "ASOPAgentState") -> _Position:
        proc = self.asop.procedure(state.procedure) if state.procedure else None
        return _Position(procedure=proc, index=state.step_index)

    def _system_prompt_for(self, state: "ASOPAgentState") -> str:
        pos = self._position(state)
        if pos.procedure is None:
            return TRIAGE_PROMPT.format(
                preamble=self.asop.preamble,
                procedures="\n".join(f"- {p.name}" for p in self.asop.procedures),
            )
        step = pos.procedure.steps[min(pos.index, len(pos.procedure.steps) - 1)]
        remaining = [s.title for s in pos.procedure.steps[pos.index + 1 :]]
        return STEP_PROMPT.format(
            preamble=self.asop.preamble,
            step=f"{step.label}\n{step.body}",
            refusal=REFUSAL_BLOCK.format(reason=state.refusal) if state.refusal else "",
            remaining=", ".join(remaining) if remaining else "nothing — procedure ends",
        )

    # -- turn ------------------------------------------------------------

    def generate_next_message(self, message, state):  # type: ignore[override]
        # The system prompt is rebuilt every turn. This is the difference from
        # every arm that came before: the model is never holding the whole
        # procedure, so "followed the procedure" cannot mean "happened to
        # mention something from a document it could see all of".
        state.system_messages = [
            SystemMessage(role="system", content=self._system_prompt_for(state))
        ]
        assistant_message, state = super().generate_next_message(message, state)

        if state.procedure is None:
            state.procedure = self._select_procedure(state)
            return assistant_message, state

        attempted = bool(getattr(assistant_message, "tool_calls", None))
        if attempted:
            self._run_gates(assistant_message, state)
        return assistant_message, state

    def _select_procedure(self, state: "ASOPAgentState") -> Optional[str]:
        """Pick the procedure from what the user has actually said.

        Keyed off the procedure names in the document rather than a hardcoded
        list, so an extraction that named its procedures differently still
        routes instead of silently never starting.
        """
        said = " ".join(
            str(getattr(m, "content", "") or "")
            for m in state.messages
            if getattr(m, "role", None) == "user"
        ).lower()
        for p in self.asop.procedures:
            key = p.name.lower().split()[0]
            if key and key in said:
                return p.name
        return None

    def _run_gates(self, assistant_message, state: "ASOPAgentState") -> None:
        pos = self._position(state)
        assert pos.procedure is not None
        step = pos.procedure.steps[min(pos.index, len(pos.procedure.steps) - 1)]

        evidence = Evidence(
            step=step,
            transcript=tuple(
                f"{getattr(m, 'role', '?')}: {getattr(m, 'content', '') or ''}"
                for m in state.messages[-12:]
            ),
            tool_calls=tuple(
                str(getattr(tc, "name", tc))
                for tc in (getattr(assistant_message, "tool_calls", None) or [])
            ),
        )
        _assert_no_gold(evidence, "the gate evaluator")

        for kind in step.gate_kinds:
            verdict = self.verifier.attest(evidence, kind, executor=self.identity)
            self.verdicts.append(verdict)
            entry = {
                "conversation": state.conversation,
                "procedure": step.procedure,
                "step": step.number,
                "step_title": step.title,
                "label": step.label,
                "gate": kind.value,
                "passed": verdict.passed,
                "reason": verdict.reason,
                "verifier": verdict.verifier,
                "executor": verdict.executor,
                "fell_back": verdict.fell_back,
            }
            state.verdicts.append(entry)
            _record(entry)
            if not verdict.passed:
                state.refusal = verdict.reason
                return

        state.refusal = None
        state.step_index += 1

    def get_init_state(self, message_history=None):  # type: ignore[override]
        base = super().get_init_state(message_history)
        return ASOPAgentState(
            system_messages=base.system_messages,
            messages=base.messages,
            conversation=next(_CONVERSATION),
            procedure=None,
            step_index=0,
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

        def judge(prompt: str) -> tuple[bool, str]:
            reply = generate(
                model=verifier_llm,
                tools=[],
                messages=[SystemMessage(role="system", content=prompt)],
                call_name="asop_gate_verdict",
            )
            return parse_verdict(str(getattr(reply, "content", "") or ""))

    return ASOPAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=llm,
        llm_args=llm_args,
        verifier=Verifier(identity=f"verifier:{verifier_llm}", judge=judge),
        identity=f"executor:{llm}",
    )


def register() -> None:
    """Register `asop_stepwise` with τ²-bench. Idempotent."""
    from tau2.registry import registry

    try:
        registry.register_agent_factory(create_asop_agent, "asop_stepwise")
    except Exception:  # already registered
        pass
