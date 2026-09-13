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
    # There is no person in a benchmark. A step declaring `Gate: human` cannot
    # get what it asked for, and an LLM opinion stamped `human` in the log is
    # the same lie as calling a rule a deterministic check. Substitution is
    # recorded, so the write-up can say how much of this procedure was never
    # gated the way it asked to be.
    HUMAN_UNAVAILABLE = "human_unavailable"

    @property
    def is_substituted(self) -> bool:
        """True when the gate did not get the kind of check it declared."""
        return self in (
            GateKind.DETERMINISTIC_UNAVAILABLE,
            GateKind.HUMAN_UNAVAILABLE,
        )


@dataclass(frozen=True)
class Step:
    number: int
    title: str
    body: str
    gate_kinds: tuple[GateKind, ...]
    procedure: str
    # "Change cabin, IF REQUESTED" is optional. Without this the gate reads
    # "the user never asked for a cabin change" as an unmet precondition and
    # refuses a step that correctly did not happen — which it did, 4 times out
    # of 4, on the first run that recorded evidence. Every conditional step in
    # every one of these extractions had the same hole.
    conditional: bool = False

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
    # phrase -> procedure name, read off a "## Routing" table when the document
    # has one. v1 of every extraction had no entry point at all, which is how a
    # Modify Flight task reached Book Flight and jammed there for 42 turns.
    routing: tuple[tuple[str, str], ...] = ()
    routing_text: str = ""

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


_CONDITIONAL_RE = re.compile(
    r",?\s*\bif\s+(requested|needed|applicable|any|the user)\b|\botherwise\b"
    r"|\bwhere\s+applicable\b|\bwhen\s+requested\b",
    re.I,
)


def is_conditional(step_body: str) -> bool:
    """Does this step only apply in some runs?

    Read off the step's own wording rather than configured per document, so a
    differently-worded extraction gets the same treatment as the one this was
    found on.
    """
    head = step_body.split(".")[0] if "." in step_body else step_body
    return bool(_CONDITIONAL_RE.search(head[:200]))


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
            conditional=is_conditional(raw),
        )
        for n, raw in found
    )


_ROUTING_ROW_RE = re.compile(r"^\|([^|]+)\|([^|]+)\|\s*$", re.M)


def _parse_routing(section_body: str, procedures: list[str]) -> tuple[tuple[str, str], ...]:
    """Read a Routing table into phrase -> procedure pairs.

    The table is prose written for a human executor; this turns the same rows
    into something the adapter can select on, so the document and the machine
    agree on routing instead of each having its own idea.
    """
    known = {p.lower(): p for p in procedures}
    out: list[tuple[str, str]] = []
    for row in _ROUTING_ROW_RE.finditer(section_body):
        phrases, target = row.group(1).strip(), row.group(2).strip()
        if target.lower() not in known or set(target) <= set("- "):
            continue
        for phrase in re.split(r"[;,]| or ", phrases.replace("The user wants to...", "")):
            phrase = phrase.strip().strip(".").lower()
            phrase = re.sub(r"^(an?|the)\s+", "", phrase)
            if len(phrase) >= 3:
                out.append((phrase, known[target.lower()]))
    return tuple(out)


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
    routing_body = ""
    for i, (start, name) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(markdown)
        block = markdown[start:end]
        body = block.split("\n", 1)[1] if "\n" in block else ""
        clean = _clean_name(name)
        steps = _steps_in(body, clean)
        if name.strip().lower().startswith("routing"):
            # Captured for the routing call, and deliberately NOT added to the
            # preamble. The preamble is injected into every step prompt, so a
            # routing table left in it becomes noise on every turn after the
            # procedure is already chosen — and proposals only ever ADD text,
            # so each round would inherit a longer prompt and the loop would
            # degrade by construction. v2 lost to v1 on exactly this.
            routing_body = body
            continue
        if steps:
            procedures.append(Procedure(name=clean, steps=steps))
        else:
            # Reference data, scope, definitions — context the executor needs
            # on every step, so it rides along in the preamble rather than
            # being dropped for failing to look like a procedure.
            preamble_parts.append(block.strip())

    if not procedures:
        raise ValueError("no section yielded a step — is this an ASOP?")
    procs = tuple(procedures)
    return ASOP(
        preamble="\n\n".join(p for p in preamble_parts if p),
        procedures=procs,
        routing=_parse_routing(routing_body, [p.name for p in procs]),
        routing_text=routing_body.strip(),
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
    # Turns since THIS step began. A fixed last-N window spans step boundaries
    # and retry churn, so a step's own refusal blocks evict the tool evidence
    # that would have satisfied it.
    transcript: tuple[str, ...]
    # Every tool call in the run so far, with arguments and result. This is the
    # durable evidence: a precondition satisfied at turn 3 by a tool call is
    # still visible at turn 20, where a scoped transcript alone would lose it.
    #
    # Carrying calls at all is the point. Before this, Evidence held tool NAMES
    # for the current turn and nothing else — so the verifier judged
    # preconditions off the executor's own narration, and the separation the
    # identity check enforces on paper leaked in practice.
    tool_history: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    gate_kind: GateKind
    verifier: str
    executor: str
    fell_back: bool = False
    # A conditional step that was never triggered. NOT a pass: nothing was
    # verified, and counting it as one would inflate every gate statistic with
    # steps that never ran.
    not_applicable: bool = False

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


def _render_turn(m: Any) -> str:
    """One transcript line, with tool CALLS made visible.

    An assistant message carrying tool_calls has no content, so the previous
    renderer emitted an empty line for exactly the turns that mattered.
    """
    role = getattr(m, "role", "?")
    calls = getattr(m, "tool_calls", None) or []
    if calls:
        rendered = "; ".join(
            f"{getattr(c, 'name', '?')}({_render_args(getattr(c, 'arguments', None))})"
            for c in calls
        )
        return f"{role} CALLS: {rendered}"
    if role == "tool":
        flag = " [ERROR]" if getattr(m, "error", False) else ""
        return f"tool RESULT{flag}: {_clip(getattr(m, 'content', '') or '', 400)}"
    return f"{role}: {_clip(getattr(m, 'content', '') or '', 600)}"


def _clip(text: str, n: int) -> str:
    """Truncate, and SAY SO.

    Silent truncation is the narration leak wearing a different hat: if the
    precondition-relevant value falls past the cut, the verifier sees nothing
    about it and cannot tell "absent" from "trimmed" — so it falls back on
    whatever the executor said about that value.
    """
    text = str(text)
    return text if len(text) <= n else text[:n] + f"…[+{len(text) - n} chars trimmed]"


def _render_args(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    items = list(args.items())
    shown = ", ".join(f"{k}={_clip(repr(v), 160)}" for k, v in items[:8])
    if len(items) > 8:
        shown += f", …[+{len(items) - 8} more arguments not shown]"
    return shown


def _tool_history(messages: list) -> tuple[str, ...]:
    """Every call and its result, in order. The run's durable evidence."""
    out: list[str] = []
    for m in messages:
        calls = getattr(m, "tool_calls", None) or []
        for c in calls:
            out.append(
                f"called {getattr(c, 'name', '?')}({_render_args(getattr(c, 'arguments', None))})"
            )
        if getattr(m, "role", None) == "tool":
            flag = "FAILED" if getattr(m, "error", False) else "ok"
            out.append(f"  -> {flag}: {_clip(getattr(m, 'content', '') or '', 300)}")
    return tuple(out)


# ── the one gate that is not an opinion ──────────────────────────────────────

_TOOL_NAME_RE = re.compile(r"`([a-z_][a-z0-9_]*)`|\b([a-z_]+_[a-z_]+)\b")


def named_tool(step: Step) -> Optional[str]:
    """The tool a step's gate says must run, if it names one.

    A `deterministic (tool call)` gate is only deterministic if there is
    something to re-check. When the step names a tool, "did that tool run and
    succeed" is answerable from the tool history without asking any model.
    """
    m = _GATE_RE.search(step.body)
    scope = (m.group(1) if m else "") + " " + step.body
    for hit in _TOOL_NAME_RE.finditer(scope):
        name = hit.group(1) or hit.group(2)
        if name and "_" in name and not name.endswith("_economy"):
            return name
    return None


def check_tool_succeeded(tool: str, tool_history: tuple[str, ...]) -> tuple[bool, str]:
    """Re-runnable, model-free: did `tool` run, and did its result come back ok?

    This is what `deterministic` is supposed to mean. It answers yes and no from
    the same evidence every time, and no prompt can talk it round.
    """
    called = False
    for i, line in enumerate(tool_history):
        if line.startswith("called ") and tool in line:
            called = True
            nxt = tool_history[i + 1] if i + 1 < len(tool_history) else ""
            if nxt.startswith("  -> ok"):
                return True, f"{tool} ran and returned ok"
            if nxt.startswith("  -> FAILED"):
                return False, f"{tool} ran and FAILED: {nxt[12:140]}"
    if called:
        return False, f"{tool} was called but no result is recorded yet"
    return False, f"{tool} has not been called"


# ── the verifier ─────────────────────────────────────────────────────────────

JudgeFn = Callable[..., tuple[bool, str, bool]]

VERIFIER_PROMPT = """\
You are a verifier. You did not do this work and you are not helping anyone \
finish it.

A procedure step was attempted. Decide ONE thing: does the visible evidence \
show the step's stated preconditions and prohibitions were satisfied?

THE STEP
{step}

EVERY TOOL CALL MADE SO FAR, WITH RESULTS
{tools}

THIS STEP'S TURNS
{evidence}

Answer with a verdict line and nothing else:
PASS — <what you found true, citing the call or statement that shows it>
or
FAIL — <which precondition or prohibition is unmet>{na_option}

PASS only if you can point to a specific tool-call RESULT or an explicit user
statement in the evidence whose actual value satisfies the precondition — and
name that value in your reason. A precondition satisfied by an earlier call
passes even if this step's turns are brief.

A tool call merely having been made is not evidence that its result satisfied
anything. Cite the value, not the call.

FAIL if the evidence is missing, ambiguous, trimmed where it mattered, or
contradicts the precondition, and say exactly what is absent. Uncertain
evidence is FAIL, not a lean toward PASS. A step narrated as done with no call
or user statement behind it does not pass — saying a thing was checked is not
checking it.\
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
        na_option = (
            "\nor\nN/A — <why this step does not apply to this request>"
            if ev.step.conditional
            else ""
        )
        prompt = VERIFIER_PROMPT.format(
            na_option=na_option,
            step=f"{ev.step.label}: {ev.step.body}",
            tools="\n".join(ev.tool_history) or "(no tool call has been made)",
            evidence="\n".join(ev.transcript) or "(no turns yet)",
        )
        passed, reason, not_applicable = self._judge(prompt, ev.step.conditional)
        return Verdict(
            passed=passed,
            reason=reason,
            gate_kind=kind,
            verifier=self.identity,
            executor=executor,
            fell_back=kind.is_substituted,
            not_applicable=not_applicable,
        )


def parse_verdict(raw: str, allow_na: bool = False) -> tuple[bool, str, bool]:
    """Read a verdict line. Anything unrecognised is a FAIL, never a PASS.

    A verifier that returns something unparseable has not formed a verdict, and
    the safe reading of "no verdict" is that the gate did not pass. Defaulting
    the other way is how a broken judge turns into a green run.
    """
    head = raw.strip().splitlines()[0] if raw.strip() else ""
    if allow_na:
        na = re.match(r"\s*N/?A\b\s*[—:-]?\s*(.*)", head, re.I)
        if na:
            return False, na.group(1).strip() or "step does not apply", True
    m = re.match(r"\s*(PASS|FAIL)\b\s*[—:-]?\s*(.*)", head, re.I)
    if not m:
        return False, f"unparseable verdict: {head[:120]!r}", False
    reason = m.group(2).strip() or "no reason given"
    return m.group(1).upper() == "PASS", reason, False


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


def require_verifier(verifier: "Optional[Verifier]", identity: str) -> None:
    """The most load-bearing guard in this file, kept out of the class.

    It lived in ASOPAgent.__init__, whose only test was skipped whenever tau2
    was absent — which is every normal `pytest` run. The single most important
    invariant here was reported green while never executing. A free function is
    testable anywhere.
    """
    if verifier is None:
        raise ValueError(
            "arm (c) requires a verifier. Running without one is arm (b) "
            "with extra steps, and would be reported as if it were this."
        )
    if verifier.identity == identity:
        raise ValueError("verifier and executor must be different parties")


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
    consecutive_refusals: int = 0
    step_started_at: int = 0
    escalated: list[str] = []
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
        route_fn: Optional[Callable[[str], str]] = None,
        max_refusals: int = 3,
        human_gate_unavailable: bool = True,
    ) -> None:
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args
        )
        self.asop = parse_asop(domain_policy)
        self.identity = identity
        require_verifier(verifier, identity)
        self.verifier = verifier
        self._route_fn = route_fn
        # A gate that refuses forever starves the task it is protecting. The
        # first stepwise run spent 15 evaluations on one step across 4 tasks
        # and ran out of turns. After this many consecutive refusals the step
        # is ESCALATED: recorded as never attested, and stepped past so the
        # run keeps producing evidence about later steps. It is not a pass and
        # must never be counted as one.
        self.max_refusals = max_refusals
        # A benchmark has no person to sign off. Set False only where one
        # genuinely exists, which is the runtime and not here.
        self.human_gate_unavailable = human_gate_unavailable
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
        # Gates are evaluated at the TOP of the turn, on the evidence that has
        # just ARRIVED, before the next step is chosen and the prompt is built.
        #
        # They used to fire at the bottom, on `assistant_message.tool_calls`.
        # Two things were wrong with that. A tool call is a request: its RESULT
        # arrives on the following turn, so a gate meant to check whether the
        # action succeeded was reading evidence that did not exist yet. And any
        # tool call fired the gate, whatever step it belonged to — while a step
        # gated on something the USER supplies had no trigger of its own and
        # was only ever evaluated when the model happened to call a tool.
        if state.procedure is not None and self._is_new_evidence(message, state):
            self._run_gates(state, arrived=message)

        state.system_messages = [
            SystemMessage(role="system", content=self._system_prompt_for(state))
        ]
        assistant_message, state = super().generate_next_message(message, state)

        if state.procedure is None:
            state.procedure = self._route(state)
            if state.procedure:
                state.step_started_at = len(state.messages)
        return assistant_message, state

    def _is_new_evidence(self, message, state: "ASOPAgentState") -> bool:
        """Is there anything here this step's gate could not see last turn?

        A tool RESULT always counts — it is the outcome of an action. A user
        message counts when the step's gate depends on what a person supplies,
        which is most of this domain. Firing on neither would leave
        user-supplied steps ungated; firing on everything would spend a judge
        call on turns that added nothing.
        """
        role = getattr(message, "role", None)
        if role == "tool" or getattr(message, "tool_messages", None):
            return True
        if role == "user":
            pos = self._position(state)
            if pos.procedure is None:
                return False
            step = pos.procedure.steps[min(pos.index, len(pos.procedure.steps) - 1)]
            return any(
                k in (GateKind.HUMAN, GateKind.HUMAN_UNAVAILABLE, GateKind.JUDGED)
                for k in step.gate_kinds
            )
        return False

    def _route(self, state: "ASOPAgentState") -> Optional[str]:
        """The real routing call. See _select_procedure for why it is uniform."""
        said = "\n".join(
            f"user: {getattr(m, 'content', '') or ''}"
            for m in state.messages
            if getattr(m, "role", None) == "user"
        ).strip()
        if not said or self._route_fn is None:
            return None
        names = [p.name for p in self.asop.procedures]
        guidance = (
            f"The procedure document gives this routing guidance:\n\n{self.asop.routing_text}\n"
            if self.asop.routing_text
            else "The document gives no routing guidance; decide from the names alone.\n"
        )
        answer = self._route_fn(
            "Choose which procedure this request belongs to.\n\n"
            "PROCEDURES\n" + "\n".join("- " + n for n in names) + "\n\n"
            f"{guidance}\n"
            f"WHAT THE USER HAS SAID\n{said}\n\n"
            "Answer with the procedure name exactly as written above and nothing "
            "else. If the request is ambiguous, or the user has not yet said what "
            "they want, answer NONE."
        )
        answer = (answer or "").strip().splitlines()[0].strip(" .`*") if answer else ""
        low = answer.lower()
        for n in names:
            if n.lower() == low or n.lower() in low:
                return n
        return None

    def _run_gates(self, state: "ASOPAgentState", arrived=None) -> None:
        pos = self._position(state)
        assert pos.procedure is not None
        step = pos.procedure.steps[min(pos.index, len(pos.procedure.steps) - 1)]

        evidence = Evidence(
            step=step,
            # `arrived` has not been appended to state.messages yet — it is
            # this turn's new evidence and is exactly what the gate is here to
            # judge, so it is included explicitly.
            transcript=tuple(
                _render_turn(m)
                for m in list(state.messages[state.step_started_at :])
                + ([arrived] if arrived is not None else [])
            ),
            tool_history=_tool_history(
                list(state.messages) + ([arrived] if arrived is not None else [])
            ),
        )
        _assert_no_gold(evidence, "the gate evaluator")

        for declared in step.gate_kinds:
            kind = declared
            verdict = None

            # A deterministic gate that names a tool is answerable without a
            # model. This is the ONLY path in this file that is not an opinion,
            # and keeping it separate is the point — everything else is a judge,
            # and the log must not pretend otherwise.
            if declared is GateKind.DETERMINISTIC:
                tool = named_tool(step)
                if tool:
                    passed, reason = check_tool_succeeded(tool, evidence.tool_history)
                    verdict = Verdict(
                        passed=passed,
                        reason=reason,
                        gate_kind=GateKind.DETERMINISTIC,
                        verifier="deterministic-check",
                        executor=self.identity,
                    )
                else:
                    # Declared deterministic, names nothing to re-run.
                    kind = GateKind.DETERMINISTIC_UNAVAILABLE
            elif declared is GateKind.HUMAN and self.human_gate_unavailable:
                # No person exists here. Record the substitution rather than
                # stamping an LLM opinion `human`.
                kind = GateKind.HUMAN_UNAVAILABLE

            if verdict is None:
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
                "declared_gate": declared.value,
                "substituted": kind.is_substituted,
                "fell_back": verdict.fell_back,
                "not_applicable": verdict.not_applicable,
            }
            if os.environ.get("ASOP_RECORD_EVIDENCE"):
                # What the verifier actually saw. Needed to hand-label a
                # decision, and off by default because it is large — a labelled
                # sample is a deliberate act, not a side effect of every run.
                entry["evidence"] = {
                    "step_body": step.body,
                    "transcript": list(evidence.transcript),
                    "tool_history": list(evidence.tool_history),
                }
            state.verdicts.append(entry)
            _record(entry)
            if verdict.not_applicable:
                # Skipped, not passed and not refused. Counting it either way
                # would be a lie about a step that never ran.
                continue
            if not verdict.passed:
                state.refusal = verdict.reason
                state.consecutive_refusals += 1
                if state.consecutive_refusals >= self.max_refusals:
                    # Unblock the run without pretending the gate passed.
                    state.escalated.append(step.label)
                    _record(
                        {
                            "conversation": state.conversation,
                            "procedure": step.procedure,
                            "step": step.number,
                            "label": step.label,
                            "gate": kind.value,
                            "passed": False,
                            "escalated": True,
                            "reason": (
                                f"escalated after {state.consecutive_refusals} "
                                f"consecutive refusals; last: {verdict.reason}"
                            ),
                            "verifier": verdict.verifier,
                            "executor": verdict.executor,
                            "declared_gate": declared.value,
                "substituted": kind.is_substituted,
                "fell_back": verdict.fell_back,
                "not_applicable": verdict.not_applicable,
                        }
                    )
                    state.consecutive_refusals = 0
                    state.refusal = None
                    state.step_index += 1
                    state.step_started_at = len(state.messages)
                return

        state.refusal = None
        state.consecutive_refusals = 0
        state.step_index += 1
        state.step_started_at = len(state.messages)

    def get_init_state(self, message_history=None):  # type: ignore[override]
        base = super().get_init_state(message_history)
        return ASOPAgentState(
            system_messages=base.system_messages,
            messages=base.messages,
            conversation=next(_CONVERSATION),
            consecutive_refusals=0,
            step_started_at=0,
            escalated=[],
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
