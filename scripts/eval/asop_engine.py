#!/usr/bin/env python3
"""The host-independent core of the ASOP stepwise gate runtime.

WHY THIS FILE EXISTS
--------------------
`asop_agent.py` grew as a τ²-bench adapter, so the gate machinery and the
benchmark shell were one class. That was fine while τ² was the only host. It
stopped being fine the moment the programme needed to answer "does OUR ASOP
work?" on SOPBench, because every tau2 number the project owns was produced by
that machinery and a *reimplementation* on the second host would answer a
different question — you would be comparing two codebases, not two hosts.

So the gate logic lives here exactly once, and each benchmark gets a thin
shell over it:

    asop_engine.ASOPEngine          <- the logic, no benchmark anywhere in it
      asop_agent.ASOPAgent          <- tau2 shell   (LLMAgent subclass)
      sopbench_asop_swarm.ASOPSwarm <- SOPBench shell (Swarm subclass)

**Nothing in this file was rewritten.** The bodies were moved verbatim out of
`asop_agent.py`; the only change is that the five stateful methods became
`ASOPEngine` methods taking `state` as an argument instead of `ASOPAgent`
methods reaching for `self`. That constraint is deliberate and load-bearing: a
behavioural drift here silently makes every published tau2 number
non-comparable with itself, and it would not announce itself in any test,
because this tree has no tau2 checkout and the agent tests skip without one.

WHAT IS COUPLED TO A HOST, AND WHAT IS NOT
------------------------------------------
Nothing here imports a benchmark. Messages are read by `getattr` only —
`.role`, `.content`, `.tool_calls[].name`, `.tool_calls[].arguments`, `.error`
— so any host that can present that shape can drive this engine. The `state`
object is duck-typed for the same reason: tau2 hands in a pydantic model,
SOPBench hands in the plain `ASOPState` dataclass defined below, and the engine
cannot tell the difference.

THE LEAKAGE RULE, which is the whole reason this code can be trusted
--------------------------------------------------------------------
A gate that consulted a task's recorded actions, its gold database, or its
evaluation criteria would be reading the answer key and feeding it to the
treatment arm. Every number afterwards would be worthless, and the run would
still exit cleanly. So gates derive ONLY from the ASOP text and the visible
transcript. `_assert_no_gold` enforces it, and the evaluator is handed a narrow
`Evidence` record rather than anything task-shaped. If you extend this file,
the invariant to preserve is: the gate may read what the ASOP says and what was
said or called in the conversation. Nothing else.
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
    check — which, in the tau2 domains, is most of them. "Cabin class must be
    the same across all flights" is a rule, not a command; it cannot be re-run
    and compared.

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
    # MULTIPLICITY. A procedure whose rules carry a per-instance cap ("at most one
    # travel certificate per reservation") may have to run MORE THAN ONCE to serve a
    # single request. v1 and v2 had no way to say that and the agent had no way to do
    # it: `step_index` only ever incremented, and `system_prompt_for` clamped to the
    # last step forever once it ran past the end. Measured consequence on task 23,
    # which needs three bookings to spend three certificates: both stepwise arms put
    # 3 passengers and 3 certificates on ONE booking every trial and scored 0.0, while
    # both whole-document arms split it correctly every trial. The rule was quoted
    # verbatim in the document they were reading — they could not act on it, not
    # having failed to understand it.
    repeatable: bool = False
    repeat_unit: str = ""  # e.g. "reservation" — what one run of this procedure covers


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


_MULTIPLICITY_RE = re.compile(
    r"^\s*(?:\*\*)?Multiplicity(?:\*\*)?\s*[:—-]\s*(.+)$", re.M | re.I
)
_UNIT_RE = re.compile(r"\bper\s+([a-z][a-z _-]{2,30}?)\b", re.I)


def _multiplicity(section_body: str) -> tuple[bool, str]:
    """Read a procedure's `Multiplicity:` line, if it declares one.

    Read off the document rather than configured per file, for the same reason
    `is_conditional` is: a differently-worded extraction of the same policy has to get
    the same treatment, or the arms differ in something other than what is under test.

    A procedure is repeatable when its Multiplicity line names a per-instance unit —
    "once per reservation" — which is the shape a per-instance cap takes. Absent the
    line, nothing changes: the procedure runs once, exactly as before.
    """
    found = _MULTIPLICITY_RE.search(section_body)
    if not found:
        return False, ""
    said = found.group(1).strip()
    unit = _UNIT_RE.search(said)
    if not unit:
        return False, ""
    return True, unit.group(1).strip()


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
            repeatable, unit = _multiplicity(body)
            procedures.append(
                Procedure(name=clean, steps=steps, repeatable=repeatable, repeat_unit=unit)
            )
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
# require patching the benchmark. SOPBench has the same property for the same
# reason, which is why the sink moved here with everything else.

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
        return f"tool RESULT{flag}: {_clip(getattr(m, 'content', '') or '', TOOL_RESULT_CLIP)}"
    return f"{role}: {_clip(getattr(m, 'content', '') or '', 600)}"


# Tool results reach the verifier through `_clip`. The limit was 400 chars, and
# MEASURED 2026-09-15 that was starving the gate of the value it was asked to
# confirm: 85% of airline tool results and 78% of retail's exceeded it (medians
# 716 and 899). In retail, the `status` field a step gates on fell past the cut in
# 72% of the results carrying one — which is why every "check its status" step
# refused almost every time (Cancel step 2: 12/12, Return: 17/18, Exchange: 17/18,
# Modify: 41/44) while the agent had in fact retrieved the status correctly.
#
# 2000 exposes the status field in 100% of observed retail results and sits above
# airline's p90 of 1155. It applies ONLY to the verifier's evidence record — the
# executor always saw tau2's messages in full — so this handicapped the gated arms
# and no other. Gate numbers from before this change are not comparable with ones
# after it, T1's precision/recall included.
TOOL_RESULT_CLIP = 2000


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
            out.append(f"  -> {flag}: {_clip(getattr(m, 'content', '') or '', TOOL_RESULT_CLIP)}")
    return tuple(out)


# ── the one gate that is not an opinion ──────────────────────────────────────

_TOOL_NAME_RE = re.compile(r"`([a-z_][a-z0-9_]*)`|\b([a-z_]+_[a-z_]+)\b")


def named_tool(step: Step) -> Optional[str]:
    """The tool a step's gate says must run, if it names one.

    A `deterministic (tool call)` gate is only deterministic if there is
    something to re-check. When the step names a tool, "did that tool run and
    succeed" is answerable from the tool history without asking any model.

    ⚠️ ONE tool, not a set. A step whose real precondition can be satisfied by
    EITHER of two tools gets gated on whichever is named first, and a run that
    satisfied it the other way is refused. That is a live over-gating source on
    any compiled document with OR-branches in its verification tables (SOPBench
    `bank` has six), and the SOPBench compiler records the count so a result can
    be read against it rather than around it.
    """
    m = _GATE_RE.search(step.body)
    scope = (m.group(1) if m else "") + " " + step.body
    for hit in _TOOL_NAME_RE.finditer(scope):
        name = hit.group(1) or hit.group(2)
        if name and "_" in name and not name.endswith("_economy"):
            return name
    return None


_ESTABLISH_RE = re.compile(r"\bESTABLISH\s*:", re.I)


def is_establish_step(step: Step) -> bool:
    """Does this step CHANGE state, or only read it?

    Declared by the document, not inferred: a V2-compiled step says `ESTABLISH:`
    when its tool is the action that puts the account into the required state
    (`logged_in_user` is satisfied BY calling `login_user`) and `VERIFY:` when
    the tool only reads. The distinction decides whether a failed check is
    retryable — a login with a wrong password can be retried, an account balance
    of 10 against a requested 50 cannot — and a document that does not declare
    it simply gets the old retry-everything behaviour.
    """
    return bool(_ESTABLISH_RE.search(step.body))


def check_tool_succeeded(tool: str, tool_history: tuple[str, ...]) -> tuple[bool, str]:
    """Re-runnable, model-free: did `tool` run, and did its result come back ok?

    This is what `deterministic` is supposed to mean. It answers yes and no from
    the same evidence every time, and no prompt can talk it round.

    ⚠️ LIVENESS, NOT CORRECTNESS. "Did this tool run and return ok", never "did
    the value it returned satisfy the precondition". Any experiment whose scorer
    is also near-liveness is therefore measuring two things that partly overlap,
    and has to say so before quoting a number.
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


# ── prompts ──────────────────────────────────────────────────────────────────

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

RESTART_PROMPT = """\
<instructions>
You have completed one full pass of {procedure} — that pass covers ONE {unit}.
You have completed {done} so far.

Decide ONE thing: does this user's request need another {unit}? It does when a
per-{unit} limit in the rules means a single {unit} cannot carry everything they
asked for. If it does, say so to the user and begin {procedure} again from step 1.
If it does not, tell the user what has been done and ask if anything else is needed.

Do not repeat work already completed for a {unit} that is finished.

In each turn you may either send a message to the user or make a tool call,
never both. Generate valid JSON only.
</instructions>

<global_rules>
{preamble}
</global_rules>\
"""

COMPLETE_PROMPT = """\
<instructions>
{procedure} is complete. Every step has been worked and there are no more.

Tell the user what was done and ask whether they need anything else. Do not
re-run steps of a completed procedure. If the user raises something new that a
different procedure covers, follow that one instead.

In each turn you may either send a message to the user or make a tool call,
never both. Generate valid JSON only.
</instructions>

<global_rules>
{preamble}
</global_rules>\
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

# ── verify-then-gate: the attestation turn (opt-in) ───────────────────────────
#
# WHY THIS EXISTS. Every gated arm measured so far puts the gate BEFORE the
# step: the executor arrives, machinery independently decides whether the
# precondition holds, and lets it through or does not. The executor never has to
# articulate anything — and `dirgraph_satisfied` (did it actually perform the
# required verifications) has sat at ~0.59 across five such configurations while
# PVA, whose text makes the model *state* a verdict per constraint, reaches
# 0.79. Gate correctness, information parity and presentation order were each
# tested and each moved it by nothing.
#
# So the gate is moved to the other side of the reasoning. The executor does the
# work and states its own verdict per condition, citing the value it observed;
# then the gate validates those claims against what the tools actually returned.
# The reasoning is elicited (PVA's mechanism) AND machine-checked (the thing PVA
# has no way to do).
#
# ⚠️ THE GATE MUST NEVER SUPPLY A VERDICT THE EXECUTOR DID NOT STATE. A missing
# verdict IS the failure — the whole point is the articulation — so the refusal
# names the condition and the required form and stops there. Handing over the
# answer would restore the barrier this design exists to remove, wearing the
# new architecture's clothes.
#
# COST, STATED UP FRONT: one extra assistant turn per task (plus the scripted
# user's echo), spent once before the final action rather than once per step.
# A per-step attestation was the other candidate and costs ~2 turns per
# condition, which on a 20-turn cap is a turn-budget experiment wearing a
# verification experiment's clothes.
ATTEST_PROMPT = """\
<instructions>
Every condition of {procedure} has been worked. Before the final action you must
attest to what you found.

Write ONE line per condition listed below, and nothing else this turn. Do not
call any tool this turn.

    VERDICT <condition>: SATISFIED - <the value the tool returned>
    VERDICT <condition>: NOT SATISFIED - <the value the tool returned>

Use the condition name exactly as it appears below. Cite the actual value you
observed, not a restatement of the rule. A verdict for a condition whose tool
you never called is not something you may write.

A verifier that is not you will check every line against what the tools
actually returned this run. It will not fill in a verdict you leave out.
</instructions>

<global_rules>
{preamble}
</global_rules>

<conditions_to_attest>
{conditions}
</conditions_to_attest>
{refusal}\
"""

# The round-3 variant: one condition, whose check has just come back not
# satisfied. It must not tell the executor WHAT the check found — that is the
# verdict it is being asked to produce, and handing it over would make this a
# transcription exercise instead of a verification one.
ATTEST_CONDITION_PROMPT = """\
<instructions>
You have gathered what this condition needs. Before anything else, say what you
found.

Write ONE line, and nothing else this turn. Do not call any tool this turn.

    VERDICT <condition>: SATISFIED - <the value the tool returned>
    VERDICT <condition>: NOT SATISFIED - <the value the tool returned>

Read the value the tool returned against the operating rules for THIS request.
Cite the actual value. A verifier that is not you will check your line against
what the tools returned.

If the condition is not satisfied, say so. Saying so is a correct outcome: some
requests must not be carried out, and reporting that plainly is the job.
</instructions>

<global_rules>
{preamble}
</global_rules>

<condition_to_attest>
- {conditions}
</condition_to_attest>
{refusal}\
"""

ATTEST_REFUSAL_BLOCK = """
<attestation_refused>
{reason}
</attestation_refused>
"""

# The terminal state the engine did not have. A refused gate means "not yet —
# fix what is missing and retry"; it never meant "this request must not be
# carried out at all". So on every impermissible task the executor was told to
# keep trying, eventually escalated past the gate, and acted. Here, when the
# executor ITSELF states a condition is not satisfied and the verifier confirms
# that from the tool results, there is nothing to retry.
BLOCKED_PROMPT = """\
<instructions>
{procedure} must not be carried out. You stated that a required condition is not
satisfied, and the verifier confirmed it from the tool results:

    {reason}

Do not call the procedure's action. Tell the user plainly which condition failed
and why, then end the conversation.

In each turn you may either send a message to the user or make a tool call,
never both. Generate valid JSON only.
</instructions>

<global_rules>
{preamble}
</global_rules>\
"""

# ── the upfront presentation (opt-in) ────────────────────────────────────────
#
# WHY THIS EXISTS. `STEP_PROMPT` shows the executor ONE step's body and the
# TITLES of the rest, and tells it in as many words not to work ahead. Measured
# on SOPBench `bank`, both gated arms left `dirgraph_satisfied` — did the agent
# actually perform the required verifications — flat at the ungated baseline
# (0.597 none -> 0.604 liveness-gated -> 0.590 value-gated) while the two
# winning arms gained ~0.2 (`pva` 0.791, `action_order` 0.843). Repairing the
# gate's correctness moved that metric by nothing, which is what pointed
# upstream of the gate to the presentation itself.
#
# The named hypothesis this template exists to test: PVA has the agent
# enumerate the whole constraint checklist BEFORE acting, and a stepwise walk
# lets each gate be satisfied locally and minimally as it is reached, never
# building the verification trace the scorer rewards.
#
# WHAT CHANGES AND WHAT DOES NOT. This is a presentation swap and nothing else.
# The checklist below is the SAME per-task narrowed procedure the stepwise arm
# walks, rendered whole instead of one item at a time; the gate scheduler,
# the gate kinds, the value check, escalation and advancement are untouched, and
# the verifier still decides each step. `<checklist_position>` keeps the
# executor and the gate pointing at the same item, because a checklist with no
# cursor would make the gate's refusals unattributable to anything the executor
# can see.
UPFRONT_STEP_PROMPT = """\
<instructions>
You are a customer service agent following a procedure. Its FULL checklist is
given below. You must work through the whole checklist before taking the
procedure's final action.

1. ENUMERATE. Read every item in <procedure_checklist> and list them as the
   conditions this request requires. Do this before your first tool call.
2. VERIFY EACH ITEM. Take the items in order. For each one, call the
   verification tool it names, then state explicitly whether that condition is
   SATISFIED or NOT SATISFIED, citing the specific value the tool returned that
   justifies your verdict. Do not skip an item, and never assert a verdict
   without first calling the tool that establishes it.
3. SELF-VERIFY. Once every item has a verdict, re-read those verdicts against
   the global rules and decide whether the final action is permissible.
4. ACT. Only if every condition is satisfied, take the final action. Otherwise
   do not call it, and say plainly which condition failed.

A verifier that is not you decides whether each item passed. You do not mark
your own work complete, and saying an item is done does not make it so.
<checklist_position> names the item that verifier is on now — work the
checklist in that order, and do not take the final action while earlier items
are still without a verdict.

In each turn you may either send a message to the user or make a tool call,
never both. Generate valid JSON only.
</instructions>

<global_rules>
{preamble}
</global_rules>

<procedure_checklist>
{procedure}
{checklist}
</procedure_checklist>
{refusal}
<checklist_position>
Item {index} of {total}: {current}
</checklist_position>\
"""


# ── state ────────────────────────────────────────────────────────────────────


@dataclass
class _Position:
    procedure: Optional[Procedure]
    index: int


@dataclass
class ASOPState:
    """Where we are in the procedure, for a host that does not supply a state.

    Field-for-field the additions `ASOPAgentState` makes to tau2's pydantic
    base, plus the two fields that base supplied (`messages`,
    `system_messages`). The engine reads both shapes by attribute, so tau2
    keeps its pydantic state and SOPBench gets this one, and neither the gate
    scheduler nor the prompt builder can tell which it was handed.
    """

    messages: list = field(default_factory=list)
    system_messages: list = field(default_factory=list)
    conversation: int = -1
    consecutive_refusals: int = 0
    step_started_at: int = 0
    escalated: list = field(default_factory=list)
    procedure: Optional[str] = None
    step_index: int = 0
    instances: int = 0
    refusal: Optional[str] = None
    verdicts: list = field(default_factory=list)
    # -- verify-then-gate only; inert unless an attestor is installed --------
    #: The executor has stated a verdict per condition and the verifier
    #: validated every one of them against the tool results.
    attested: bool = False
    #: Consecutive attestation attempts that were refused, so a run cannot be
    #: starved by an executor that will not produce the required form.
    attest_refusals: int = 0
    #: Set when the executor stated a condition is NOT satisfied and the
    #: verifier confirmed that from the tool results. Terminal: the procedure
    #: does not proceed, and this is the reason the user is told.
    blocked: Optional[str] = None
    #: Every attestation verdict the verifier read, for the write-up.
    attestations: list = field(default_factory=list)
    #: Set when a VERIFY step's gate resolved NOT satisfied on the returned
    #: value: the executor is asked to attest to that one condition, and a
    #: confirmed NOT SATISFIED there is terminal. Cleared on any pass.
    attest_condition: Optional[str] = None


def next_conversation_id() -> int:
    """The process-wide conversation counter the verdict sink keys on."""
    return next(_CONVERSATION)


# ── the engine ───────────────────────────────────────────────────────────────


class ASOPEngine:
    """Walk an ASOP one step at a time, gating each step on independent evidence.

    Everything an executor needs in order to be *stepwise and gated* lives here,
    and nothing about any particular benchmark does. A host shell is responsible
    for exactly three things: giving the engine messages it can read by
    `getattr`, calling `before_turn` / `system_prompt_for` / `note_turn` around
    its own model call, and carrying a `state`.
    """

    def __init__(
        self,
        asop: ASOP,
        verifier: Optional[Verifier],
        identity: str = "executor",
        route_fn: Optional[Callable[[str], str]] = None,
        max_refusals: int = 3,
        max_instances: int = 4,
        human_gate_unavailable: bool = True,
        value_check: Optional[Callable[[Step, tuple], tuple[Optional[bool], str]]] = None,
        upfront: bool = False,
        attestor: Optional[Callable[..., tuple[str, str, list]]] = None,
        max_attest_refusals: int = 3,
        attest_on_failed_check: bool = False,
    ) -> None:
        # VERIFY-THEN-GATE. `None` keeps every published arm byte-identical:
        # nothing below reads this flag when it is unset. When it is set, the
        # engine inserts one attestation turn before a procedure's final action
        # (see `ATTEST_PROMPT`), and the attestor validates the executor's own
        # stated verdicts against the tool history. Contract:
        #     attestor(steps, stated_text, tool_history)
        #       -> (status, message, rows)
        #   status "ok"          every condition has a stated verdict, and every
        #                        one of them matches what the tools returned
        #   status "refuse"      a verdict is missing, malformed, or contradicted
        #                        by the tool results; `message` says which, and
        #                        MUST NOT say what the verdict should have been
        #   status "blocked"     the executor stated a condition is not satisfied
        #                        and the tool results agree — terminal
        self._attestor = attestor
        self.max_attest_refusals = max_attest_refusals
        # ⚠️ ROUND 3. Found in the round-2 pre-flight, on the first impermissible
        # task: `internal_check_username_exist` returned False, the value gate
        # correctly refused step 1, and the engine told the executor what it has
        # always told it — "address what is missing and try again". The executor
        # obliged by inventing a password, calling `open_account`, and then
        # applying for the credit card the rules forbade. The attestation turn
        # never fired, because the run never reached the final step.
        #
        # A refused gate has only ever meant "not yet". It has never been able
        # to mean "this must not happen", which is why the impermissible half
        # keeps being escalated past the gate and acted on.
        #
        # With this on, a VERIFY step whose gate resolves NOT satisfied ON THE
        # RETURNED VALUE routes to an attestation for that one condition, and a
        # confirmed NOT SATISFIED is terminal. ESTABLISH steps are exempt and
        # keep the retry loop: a VERIFY step reads state and the value is what
        # it is, while an ESTABLISH step performs an action that can genuinely
        # be retried with different information (a login with the right
        # password). That distinction is only available because the V2 document
        # labels its steps — it is the authoring change paying for itself.
        self.attest_on_failed_check = attest_on_failed_check
        # PRESENTATION ONLY. `True` renders the whole procedure as a checklist
        # with an enumerate-then-verify-then-act instruction (see
        # `UPFRONT_STEP_PROMPT`); `False` keeps the one-step-at-a-time prompt
        # byte for byte. Nothing downstream of the prompt reads this flag — the
        # gates, their kinds, the value check and advancement are identical in
        # both modes — which is what makes the arms a clean contrast on one
        # variable. Off by default so tau2 and the published arms are unmoved.
        self._upfront = upfront
        # OPTIONAL VALUE GATE. `check_tool_succeeded` answers "did the tool run
        # and return ok" — liveness. That is not what a precondition says, and
        # the difference is not academic: measured on SOPBench `bank`, a
        # liveness gate PASSES on tasks whose precondition is designed to fail
        # (the tool runs fine and returns a value showing the condition is
        # violated), then advances the executor into the action it should have
        # refused. `constraint_not_violated` fell 0.721 -> 0.535 on that alone.
        #
        # A value check, when the host can supply one, answers the real
        # question: does the RETURNED VALUE satisfy the stated condition. It
        # returns `(None, reason)` when it cannot judge a given step, and the
        # engine then falls back to liveness — so a host that supplies no
        # checker (tau2 does not) behaves exactly as before, byte for byte.
        self._value_check = value_check
        self.asop = asop
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
        # A repeatable procedure that never stops repeating is the same starvation
        # failure as a gate that never passes, arriving from the other side. Bound it.
        self.max_instances = max_instances
        # A benchmark has no person to sign off. Set False only where one
        # genuinely exists, which is the runtime and not here.
        self.human_gate_unavailable = human_gate_unavailable
        self.verdicts: list[Verdict] = []

    # -- prompt ----------------------------------------------------------

    def position(self, state: Any) -> _Position:
        proc = self.asop.procedure(state.procedure) if state.procedure else None
        return _Position(procedure=proc, index=state.step_index)

    def needs_attestation(self, state: Any) -> bool:
        """Is the executor being asked to attest this turn?

        Two occasions. Standing on the final action with its verdicts not yet
        validated — that is the attestation the whole procedure builds toward.
        And, with `attest_on_failed_check` on, standing on a VERIFY step whose
        gate has just resolved NOT satisfied ON THE RETURNED VALUE.
        """
        if self._attestor is None or state.attested or state.blocked:
            return False
        pos = self.position(state)
        if pos.procedure is None or len(pos.procedure.steps) < 2:
            return False
        if pos.index == len(pos.procedure.steps) - 1:
            return True
        return bool(getattr(state, "attest_condition", None))

    def attestable_steps(self, state: Any) -> list[Step]:
        """The steps this attestation covers.

        At the final action that is every condition. At a failed VERIFY check it
        is that one condition, because that is the only claim there is to audit
        — asking for verdicts on conditions the run has not reached yet would be
        asking the executor to attest to things it has not done, which is the
        fabrication this design is trying to make impossible.
        """
        pos = self.position(state)
        if pos.procedure is None:
            return []
        one = getattr(state, "attest_condition", None)
        if one is not None and pos.index < len(pos.procedure.steps) - 1:
            return [pos.procedure.steps[min(pos.index, len(pos.procedure.steps) - 1)]]
        return list(pos.procedure.steps[:-1])

    def system_prompt_for(self, state: Any) -> str:
        pos = self.position(state)
        if state.blocked and pos.procedure is not None:
            return BLOCKED_PROMPT.format(
                preamble=self.asop.preamble,
                procedure=pos.procedure.name,
                reason=state.blocked,
            )
        if self.needs_attestation(state):
            template = (
                ATTEST_CONDITION_PROMPT
                if getattr(state, "attest_condition", None)
                and pos.index < len(pos.procedure.steps) - 1
                else ATTEST_PROMPT
            )
            return template.format(
                preamble=self.asop.preamble,
                procedure=pos.procedure.name,
                conditions="\n".join(
                    f"- {s.title}" for s in self.attestable_steps(state)
                ),
                refusal=(
                    ATTEST_REFUSAL_BLOCK.format(reason=state.refusal) if state.refusal else ""
                ),
            )
        if pos.procedure is None:
            return TRIAGE_PROMPT.format(
                preamble=self.asop.preamble,
                procedures="\n".join(f"- {p.name}" for p in self.asop.procedures),
            )
        # Past the last step the procedure is DONE. Before this it clamped to the
        # final step and re-presented it forever, which is the "runs past the last
        # step with no terminal state" defect — the agent was told to keep working a
        # step it had already completed, with no way to either finish or start again.
        if pos.index >= len(pos.procedure.steps):
            if pos.procedure.repeatable and state.instances < self.max_instances:
                return RESTART_PROMPT.format(
                    preamble=self.asop.preamble,
                    procedure=pos.procedure.name,
                    unit=pos.procedure.repeat_unit or "instance",
                    done=state.instances,
                )
            return COMPLETE_PROMPT.format(
                preamble=self.asop.preamble, procedure=pos.procedure.name
            )
        step = pos.procedure.steps[pos.index]
        if self._upfront:
            # The same steps the stepwise arm walks, rendered whole. The cursor
            # is stated separately rather than by hiding the other items.
            return UPFRONT_STEP_PROMPT.format(
                preamble=self.asop.preamble,
                procedure=f"Procedure: {pos.procedure.name}",
                checklist="\n".join(
                    f"{'->' if i == pos.index else '  '} {s.number}. {s.body}"
                    for i, s in enumerate(pos.procedure.steps)
                ),
                refusal=REFUSAL_BLOCK.format(reason=state.refusal) if state.refusal else "",
                index=step.number,
                total=len(pos.procedure.steps),
                current=step.title,
            )
        remaining = [s.title for s in pos.procedure.steps[pos.index + 1 :]]
        return STEP_PROMPT.format(
            preamble=self.asop.preamble,
            step=f"{step.label}\n{step.body}",
            refusal=REFUSAL_BLOCK.format(reason=state.refusal) if state.refusal else "",
            remaining=", ".join(remaining) if remaining else "nothing — procedure ends",
        )

    # -- turn ------------------------------------------------------------

    def before_turn(self, message: Any, state: Any) -> None:
        """Run the gates on evidence that has just ARRIVED, before the prompt is built.

        Gates used to fire at the BOTTOM of the turn, on the assistant's own
        `tool_calls`. Two things were wrong with that. A tool call is a request:
        its RESULT arrives on the following turn, so a gate meant to check
        whether the action succeeded was reading evidence that did not exist
        yet. And any tool call fired the gate, whatever step it belonged to —
        while a step gated on something the USER supplies had no trigger of its
        own and was only ever evaluated when the model happened to call a tool.
        """
        if state.blocked:
            # Terminal. Nothing left to gate, and re-gating would re-refuse the
            # step the executor has already been told to stop working.
            return
        if state.procedure is not None and self.is_new_evidence(message, state):
            self.run_gates(state, arrived=message)

    def note_turn(self, state: Any, assistant_message: Any) -> None:
        """Observe what the executor just did: re-entry, then routing.

        RE-ENTRY. Standing at the end of a repeatable procedure, a tool call is the
        agent acting on another instance — the restart prompt just asked it to decide,
        and this is the decision, observed rather than inferred. Re-arm at step 1.
        Deciding FOR it (auto-restarting on completion) would force a second booking
        on every user who only ever needed one.
        """
        pos = self.position(state)
        if (
            pos.procedure is not None
            and pos.index >= len(pos.procedure.steps)
            and pos.procedure.repeatable
            and state.instances < self.max_instances
            and getattr(assistant_message, "tool_calls", None)
        ):
            state.instances += 1
            state.step_index = 0
            state.step_started_at = len(state.messages)
            state.refusal = None
            state.consecutive_refusals = 0

        if state.procedure is None:
            state.procedure = self.route(state)
            if state.procedure:
                state.step_started_at = len(state.messages)
            return

        # VERIFY-THEN-GATE. The executor has just spoken; if it was standing on
        # the attestation turn, this message is the thing being audited. Done
        # here rather than in `before_turn` because the object under audit is
        # the executor's OWN output, which `before_turn` (which fires on
        # arriving evidence, before the model speaks) cannot see.
        if self.needs_attestation(state) and assistant_message is not None:
            self._run_attestation(state, assistant_message)

    def _run_attestation(self, state: Any, assistant_message: Any) -> None:
        """Audit the executor's own stated verdicts against the tool history.

        Three outcomes and no fourth. `ok` releases the final action. `refuse`
        says what is missing or contradicted WITHOUT saying what the verdict
        should have been — supplying it would make the executor's articulation
        optional again, which is the whole thing this is trying to change.
        `blocked` is the terminal state: the executor said a condition fails and
        the tools agree, so there is nothing to retry.
        """
        said = str(getattr(assistant_message, "content", "") or "")
        # Some executors put the turn's prose in a reasoning channel and leave
        # `content` empty when they are also emitting a tool call. Both are the
        # model's own output; reading only one of them would score the harness's
        # serialisation rather than the model's compliance.
        said = (said + "\n" + str(getattr(assistant_message, "reasoning", "") or "")).strip()
        tool_history = _tool_history(list(state.messages))
        status, message, rows = self._attestor(
            self.attestable_steps(state), said, tool_history
        )
        state.attestations.append(
            {
                "conversation": state.conversation,
                "procedure": state.procedure,
                "status": status,
                "message": message,
                "rows": rows,
                "attempt": state.attest_refusals + 1,
            }
        )
        _record(
            {
                "conversation": state.conversation,
                "procedure": state.procedure,
                "step": 0,
                "label": f"{state.procedure} · attestation",
                "gate": "attestation",
                "passed": status == "ok",
                "reason": message,
                "verifier": "attestation-check",
                "executor": self.identity,
                "declared_gate": "attestation",
                "substituted": False,
                "fell_back": False,
                "not_applicable": False,
                "attestation_status": status,
                "attestation_rows": rows,
            }
        )
        # A per-condition attestation (round 3) is a different object from the
        # procedure-wide one: it releases nothing, because the condition it
        # audits has already failed its check. Only `blocked` and `refuse` are
        # reachable, and escalating out of it must not mark the PROCEDURE
        # attested — that would hand the final action a pass it never earned.
        per_condition = bool(state.attest_condition)

        if status == "ok":
            state.refusal = None
            state.attest_refusals = 0
            if per_condition:
                # The tools could not resolve it and the executor says it holds.
                # Its own verdict stands, and the step advances.
                state.attest_condition = None
                state.consecutive_refusals = 0
                state.step_index += 1
                state.step_started_at = len(state.messages)
            else:
                state.attested = True
            return
        if status == "blocked":
            state.blocked = message
            state.refusal = None
            state.attest_condition = None
            return
        state.attest_refusals += 1
        state.refusal = message
        if per_condition and state.attest_refusals >= self.max_attest_refusals:
            # It would not produce the form. Fall back to the behaviour every
            # published arm has: the ordinary refusal loop, which escalates.
            state.attest_condition = None
            state.attest_refusals = 0
            state.consecutive_refusals += 1
            return
        if not per_condition and state.attest_refusals >= self.max_attest_refusals:
            # Starvation guard, same posture as `max_refusals`: step past
            # without pretending the attestation happened. Recorded as an
            # escalation so a run that never articulated anything cannot be
            # read as one that did.
            state.escalated.append(f"{state.procedure} · attestation")
            state.attested = True
            state.refusal = None
            state.attest_refusals = 0

    def is_new_evidence(self, message: Any, state: Any) -> bool:
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
            pos = self.position(state)
            if pos.procedure is None:
                return False
            step = pos.procedure.steps[min(pos.index, len(pos.procedure.steps) - 1)]
            return any(
                k in (GateKind.HUMAN, GateKind.HUMAN_UNAVAILABLE, GateKind.JUDGED)
                for k in step.gate_kinds
            )
        return False

    def route(self, state: Any) -> Optional[str]:
        """The real routing call, asked of a model that is not the executor."""
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

    def run_gates(self, state: Any, arrived: Any = None) -> None:
        pos = self.position(state)
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
                    # Liveness first: a value cannot satisfy anything if the
                    # call never ran or failed outright.
                    passed, reason = check_tool_succeeded(tool, evidence.tool_history)
                    verifier = "deterministic-check"
                    if passed and self._value_check is not None:
                        satisfied, why = self._value_check(step, evidence.tool_history)
                        if satisfied is not None:
                            passed, reason = satisfied, why
                            verifier = "deterministic-value-check"
                    verdict = Verdict(
                        passed=passed,
                        reason=reason,
                        gate_kind=GateKind.DETERMINISTIC,
                        verifier=verifier,
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
                if (
                    self.attest_on_failed_check
                    and self._attestor is not None
                    and verdict.verifier == "deterministic-value-check"
                    and not is_establish_step(step)
                ):
                    # A VERIFY step read a value and the value does not satisfy
                    # the condition. There is nothing to "try again" — but the
                    # gate does not get to end the run on its own either. The
                    # executor is asked to state its own verdict on this one
                    # condition, and only a NOT SATISFIED the tool results
                    # confirm is terminal.
                    state.attest_condition = step.title
                    return
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
