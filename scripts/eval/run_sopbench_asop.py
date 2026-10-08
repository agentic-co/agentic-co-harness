#!/usr/bin/env python3
"""Run the `asop-prompt` and `asop-gated` arms on SOPBench.

    <sopbench>/.venv/bin/python scripts/eval/run_sopbench_asop.py \\
        --arm asop-gated --domain bank \\
        --asop evals/sopbench-bank-asop/bank.asop.md \\
        --output-dir /tmp/asop-gated --limit 2

Two arms, and the contrast between them is the experiment:

  `asop-prompt`  our compiled ASOP document handed to the agent whole, as
                 context. No stepwise walk, no gate ever evaluated.
  `asop-gated`   the same document, walked one step at a time by
                 `asop_engine.ASOPEngine`, each step's precondition enforced
                 before the next is shown.

Without the first, a loss in the second cannot be attributed to the document or
to the runtime — which is the entire open question. `T2-PROCEDURE-LADDER.md`
already holds the other two arms (`none`, `pva`) at this exact configuration,
so they are reused rather than re-run.

HOW IT INSTALLS
---------------
`run_simulation.py` does `from swarm.core import *` and `swarm/core.py` declares
no `__all__`, so `Swarm` is an ordinary module global there, referenced exactly
once (`run_simulation.py:170`). Rebinding it is therefore the whole
installation — the benchmark is called by name and never patched, matching
`run_arm_c.py`'s posture and T2's precedent of touching nothing upstream but
two additive model registrations.

NO LLM SITS ANYWHERE EXCEPT THE EXECUTOR
----------------------------------------
Per the scope document, the first port attempt runs with **no LLM judge at
all**: the deterministic tool-liveness gate is the only gate, and the router is
a deterministic phrase match over the document's own `## Routing` table. Two
reasons. It extends T2's best property — no model anywhere in the scoring loop
— to the gate itself, so the first measurement is free and exactly
reproducible. And it removes judge noise from the causal chain, which is the
only way a first result answers "does the execution model help or hurt?"
rather than "is the judge noisy?" (the deployed GLM-4.7 judge flips 8.5% of
verdicts on byte-identical input; the expected effect here is near +0.13).

⚠️ THE CONFOUND THIS BUYS, STATED BEFORE ANY NUMBER IS QUOTED.
`check_tool_succeeded` is **liveness, not correctness** — "did this tool run and
return ok", never "did its value satisfy the precondition". SOPBench's
`dirgraph` oracle is *also* near-liveness: 1 of 1,048 violated decisions across
all seven domains is a parameter-only violation. A deterministic liveness gate
and SOPBench's scorer are therefore close to measuring the same thing. This is
not leakage — the gate reads only the ASOP text and the visible transcript, and
`_assert_no_gold` still fires — but a win would be **partly structural**, and
the write-up has to say so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

DEFAULT_SOPBENCH = Path.home() / "Code" / "SOPBench"


# ── the verifier that must never be needed ───────────────────────────────────


class _UngatedTripwire:
    """Counts steps the deterministic gate could not answer, and says so.

    The compiler emits a deterministic, tool-naming gate for every step, and
    asserts that `named_tool()` resolves on each one. So this judge should
    never fire. If it does, the document and the runtime disagree about what is
    checkable, and the arm is no longer the all-deterministic arm it claims to
    be — so the count is carried into the stats rather than being absorbed.

    It returns PASS, not FAIL, deliberately: a FAIL here would refuse a step for
    a *compiler* defect and manufacture the over-gating the experiment is trying
    to measure. A PASS leaves the step simply ungated, which the count then
    discloses. The pre-flight asserts this count is zero before a full run is
    spent, so PASS-by-default is never silently load-bearing.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.steps: list[str] = []

    def __call__(self, prompt: str, allow_na: bool = False) -> tuple[bool, str, bool]:
        self.calls += 1
        head = prompt.split("\n")[3] if len(prompt.split("\n")) > 3 else "?"
        self.steps.append(head[:120])
        return (
            True,
            "UNGATED: no re-runnable check exists for this step, so nothing was "
            "verified. This is recorded, not a pass on the merits.",
            False,
        )


# ── deterministic routing ────────────────────────────────────────────────────


def make_router(asop: Any) -> Any:
    """Pick a procedure by phrase match against the document's own Routing table.

    ⚠️ **MEASURED 0.343 accuracy on all 134 `bank` tasks and NOT used for the
    reported run.** Kept because it is the control that showed why: routing
    scored on token overlap is hijacked by the *constraint* words in a prompt.
    `set_safety_box` requests mention authenticating an admin password, because
    that is one of the action's preconditions, so 21 of them routed to
    `Set Admin Password` — the procedure whose name those words spell. A
    hand-rolled router failing this way would have shown up in the results as
    "our ASOP loses", and the loss would have been mine, not the method's.

    `make_llm_router` is what the reported run uses, and it is also the more
    faithful choice: tau2's arm (c) routes with a model call
    (`asop_agent.create_asop_agent`'s `route_fn`), so a model router keeps the
    two hosts running the same configuration.

    The engine calls `route_fn(prompt)` and matches the returned text against
    procedure names, so this returns a name, not a decision object; the engine's
    own matching is left to do its job unchanged.
    """
    phrases = list(asop.routing)
    names = [p.name for p in asop.procedures]

    def route(prompt: str) -> str:
        said = prompt.lower()
        # The engine builds the prompt with the user's words under a known
        # heading; everything above it is the procedure list and would match
        # every procedure equally.
        marker = "what the user has said"
        if marker in said:
            said = said.split(marker, 1)[1]

        scores: dict[str, int] = {n: 0 for n in names}
        for phrase, target in phrases:
            if phrase and phrase in said:
                # Longer phrases are more specific; weight by length so
                # "transfer funds" beats a bare "funds".
                scores[target] = scores[target] + len(phrase.split()) + 1
        for n in names:
            tokens = [t for t in re.split(r"\W+", n.lower()) if len(t) > 3]
            if tokens and all(t in said for t in tokens):
                scores[n] = scores[n] + 2 * len(tokens)

        best = max(scores, key=lambda k: scores[k]) if scores else ""
        return best if scores.get(best, 0) > 0 else "NONE"

    return route


def flatten_task_constraints(node: Any) -> set[str]:
    """Constraint names in a task's own `constraints` tree (JSON, so lists).

    The compiler's `flatten_dep_tree` reads the Python tuple shape that lives in
    `bank_assistant.py`; a task's tree comes out of `bank_tasks.json`, where
    every node is a list. `["single", name, params]` is therefore ambiguous with
    "a list of children" unless the first element is checked, which is what the
    discriminator below does.
    """
    out: set[str] = set()

    def walk(n: Any) -> None:
        if not isinstance(n, (list, tuple)) or not n:
            return
        head = n[0]
        if head == "single":
            name = n[1]
            out.add(name[len("not ") :] if str(name).startswith("not ") else name)
        elif head in ("and", "or", "chain"):
            for child in n[1]:
                walk(child)
        else:
            for child in n:
                walk(child)

    walk(node)
    return out


def build_constraint_tool_map(sopbench: Path, domain_name: str = "bank") -> dict:
    """constraint name -> the tool its gate asserts on.

    Built with the compiler's OWN `select_tool_for_constraint`, so the filter
    and the document can never disagree about which tool a constraint gates on.
    """
    import sopbench_asop_compile as compile_mod
    from sopbench_extract import environment_verified_nodes

    sys.path.insert(0, str(sopbench))
    from env.task import create_assistant
    from env.variables import domain_assistant_keys

    domain = domain_assistant_keys[domain_name]
    exposed = {
        t["function"]["name"]
        for t in create_assistant(domain_name, False, "prompt", None)["tools"]
    }
    env_nodes = environment_verified_nodes(dict(domain.action_descriptions).keys(), exposed)
    links = dict(domain.constraint_links)
    procs = dict(domain.constraint_processes)

    names = set(links) | set(procs) | set(dict(domain.positive_constraint_descriptions))
    return {
        c: compile_mod.select_tool_for_constraint(c, links, procs, env_nodes, exposed)[0]
        for c in names
    }


def make_task_asop_provider(
    full_asop: Any,
    current_task: dict,
    tool_of: dict,
    helper_prereqs: Optional[dict] = None,
) -> Any:
    """Narrow the compiled document to the constraints THIS task actually imposes.

    ⚠️ **CAUGHT IN PRE-FLIGHT. Without this the comparison is void, and the
    scope document's §3.1 reasoning for the other choice does not survive
    contact with the data.**

    §3.1 argues the compiler should consume the domain-static dependency tables
    because `get_default_dep_full` "composes ... exactly the tree
    `get_dep_verb(..., 'structured')` verbalises into the prose every arm
    already sees — no leakage, and no new information". The second half is
    false. `task_initializer` builds the assistant's instructions from the
    TASK's `constraints`, not from `default_dep_full`, and a task's set is a
    SUBSET. Measured on `bank` task 0 (`apply_credit_card`,
    `action_should_succeed` True): the baseline prompt states one precondition,
    "username must exist". The domain-static document compiles three —
    username, credit score, and `logged_in_user`.

    The consequence was visible on the first gated task. The executor cleared
    username and credit score, then had to satisfy `logged_in_user` for a user
    whose known information is `{monthly_income, username, total_assets}` — no
    credentials anywhere. It invented `password123`, `login_user` returned
    False, and the gate correctly refused a step that could never pass.

    So a domain-static document does not merely differ from the other arms, it
    hands the gated arm preconditions the task never imposed and the user
    cannot supply. A loss would then measure "static document vs per-task
    spec", not "gated vs ungated" — and the gated arm would lose for a reason
    that has nothing to do with gating.

    Filtering restores exact information parity: the gated arm now walks the
    constraints every other arm is told about, in our format, through our
    runtime. The document is still ours and still compiled deterministically;
    only WHICH of its steps apply is per task, which is what every other arm
    already gets.

    Only the procedure matching the task's `user_goal` is narrowed. The others
    keep their full step lists — they are reached only on a misroute, and
    silently trimming them would hide that.

    ⚠️ **`helper_prereqs` — EXP-A, the N31 ceiling fix.** Narrowing keeps a
    helper step (say `get_account_balance`) while deleting the step its OWN
    prerequisites need (`login_user`), because only the goal action's
    prerequisites are per-task — SOPBench's evaluator overrides
    `default_dep_full` for `user_goal` alone and scores every helper against
    the domain defaults. 40/112 routable `bank` tasks lost a step that way and
    `asop-v2-doc` scored 0.000 `dirgraph_satisfied` on exactly those. When
    `helper_prereqs` (action -> its default constraint names) is given, the
    kept tool set is closed transitively over every kept helper's
    prerequisites before steps are filtered. Parity, not advantage: every
    arm's prompt already states each helper's default constraints
    (`task_initializer` overrides the description of the goal action only).
    `None` reproduces every published arm byte for byte.
    """
    import dataclasses

    from asop_engine import named_tool

    goal = current_task.get("user_goal", "")
    needed = flatten_task_constraints(current_task.get("constraints"))
    keep_tools = {tool_of.get(c) for c in needed}
    keep_tools.discard(None)

    target = " ".join(w.capitalize() for w in goal.split("_"))
    procs = []
    for p in full_asop.procedures:
        if p.name.lower() != target.lower():
            procs.append(p)
            continue
        if helper_prereqs is not None:
            last_tool = named_tool(p.steps[-1]) if p.steps else None
            seed = set(keep_tools) | ({last_tool} if last_tool else set())
            keep_tools = close_over_helper_prereqs(seed, goal, helper_prereqs, tool_of)
        kept = []
        for i, step in enumerate(p.steps):
            last = i == len(p.steps) - 1  # the step that performs the action
            tool = named_tool(step)
            if last or tool in keep_tools or tool == goal:
                kept.append(dataclasses.replace(step, number=len(kept) + 1))
        procs.append(dataclasses.replace(p, steps=tuple(kept)))
    return dataclasses.replace(full_asop, procedures=tuple(procs))


# ── the value gate ───────────────────────────────────────────────────────────
#
# WHY THIS EXISTS. The first full run measured `asop-gated` at -0.107 against
# baseline, and the split localised it precisely: the gated arm was the BEST
# arm at performing actions (`action_called_correctly` 0.729) and the WORST at
# refusing them (`constraint_not_violated` 0.535 against baseline's 0.721).
# That is under-refusal, and the cause was `check_tool_succeeded` asking only
# "did the tool run and return ok".
#
# On an impermissible SOPBench task the precondition is DESIGNED to fail. The
# executor calls `get_account_balance`, the call succeeds and returns 200.0
# against a requested 500.0, and a liveness gate reports
# "get_account_balance ran and returned ok -> PASS" — telling the executor its
# precondition is satisfied when what it verified was that the executor had
# looked. The worst instance is `internal_check_username_exist`, which returns
# `(True, False)` for a username that does NOT exist: the call succeeds, the
# answer is "no", and liveness passes it. It appears on 126 of the violating
# decisions.
#
# ⚠️ WHAT THIS READS, AND WHY IT IS NOT THE ANSWER KEY.
#   * the VALUES returned by tool calls, from the visible transcript;
#   * the task's published constraint thresholds (`constraint_parameters` —
#     per task, but rendered verbatim into the assistant's own instructions, so
#     the executor reads the same numbers);
#   * the parameters the USER states (`user_known` — literally what the user
#     says in the opening message).
# All three are things the executor itself receives. It does NOT read the gold
# database, `evaluation_criteria`, the recorded actions, or
# `action_should_succeed`, and `_assert_no_gold` still fires inside `run_gates`.
# This is not SOPBench's `--constraint_verdict` ablation, which leaks
# ground-truth SATISFIED/NOT; nothing here is told whether a condition holds —
# it computes it from the same evidence the agent has.

_RESULT_RE = re.compile(r"^  -> (ok|FAILED): (.*)$", re.S)


def close_over_helper_prereqs(
    tools: set, goal: str, helper_prereqs: dict, tool_of: dict
) -> set:
    """`tools` plus, transitively, the tool that verifies each constraint a kept
    helper's own DEFAULT prerequisites name.

    The goal action is not expanded: its prerequisites are the task's, which is
    what narrowing already honours. A constraint no tool verifies (e.g. an
    environment-side `internal_*` check) contributes nothing, as in narrowing.
    """
    closed = set(tools)
    frontier = [t for t in closed if t != goal]
    while frontier:
        t = frontier.pop()
        for c in helper_prereqs.get(t, ()):
            dep_tool = tool_of.get(c)
            if dep_tool and dep_tool != goal and dep_tool not in closed:
                closed.add(dep_tool)
                frontier.append(dep_tool)
    return closed


def build_helper_prereq_map(sopbench: Path, domain_name: str = "bank") -> dict:
    """action -> the constraint names in its DEFAULT dependency tree.

    Read with SOPBench's own `get_default_dep_full(domain, "full")` — the same
    call `evaluator_function_directed_graph` makes — so the helpers restored are
    exactly the prerequisites the scorer will hold them to.
    """
    sys.path.insert(0, str(sopbench))
    from env.task import get_default_dep_full

    ddf = get_default_dep_full(domain_name, "full")
    return {a: flatten_task_constraints(dep) for a, dep in ddf.items() if dep}


def _tool_results(tool_history: tuple) -> dict:
    """Latest successful result per tool, parsed out of the rendered history.

    `_tool_history` renders `called <name>(args)` followed by `  -> ok: <value>`,
    so the value for a tool is the line after its most recent call.
    """
    out: dict = {}
    for i, line in enumerate(tool_history):
        if not line.startswith("called "):
            continue
        name = line[len("called ") :].split("(", 1)[0]
        nxt = tool_history[i + 1] if i + 1 < len(tool_history) else ""
        m = _RESULT_RE.match(nxt)
        if m and m.group(1) == "ok":
            out[name] = m.group(2).strip()
    return out


def _as_num(text: Any) -> Optional[float]:
    try:
        return float(str(text).strip())
    except Exception:
        return None


def _as_bool(text: Any) -> Optional[bool]:
    s = str(text).strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


class _TaskTruth:
    """What the task's OWN published numbers say about each condition.

    Extracted from `make_value_checker` unchanged so the verify-then-gate
    attestor can ask the same question of the same evidence: two graders that
    disagreed about what a condition means would make a contradiction between
    the executor and the gate unreadable.

    ⚠️ Reads only what the executor also receives — tool results from the
    visible transcript, the task's published `constraint_parameters` (rendered
    verbatim into the assistant's own instructions) and `user_known` (literally
    what the user said). Never the gold database, `evaluation_criteria`, the
    recorded actions or `action_should_succeed`.
    """

    def __init__(self, task: dict, respect_or: bool = False) -> None:
        self.params = dict(task.get("constraint_parameters") or {})
        self.known = dict(task.get("user_known") or {})
        # Constraint names the task imposes, WITH polarity. `not X` means the
        # task requires the condition to be false (bank uses it only for
        # `not internal_check_username_exist`, on `open_account`).
        self.wanted: dict = {}
        # ⚠️ THE GRADER'S OWN COMPOSITION DEFECT, found by validating this class
        # against `action_should_succeed` with perfect evidence — a check nobody
        # had run, and every gated arm since N27 depends on this class.
        #
        # `wanted` records every LEAF of the task's constraint tree as required,
        # so an `("or", [A, B])` becomes "A and B" and the gate demands both.
        # That is over-refusal written into the grader — the same composition
        # defect the V2 document was fixing in the TEXT, present in the GATE the
        # whole time. Measured blast radius on the routable sets: `bank` 8/112
        # (all `pay_loan`), `library` 30/66, `online_market` 40/172.
        #
        # `or_groups` keeps the alternatives together so a group can be
        # satisfied by any one member. Off by default, because the arms already
        # measured ran with the defect and a silent fix would make them
        # incomparable rather than better.
        self.or_groups: list[list[str]] = []
        self.respect_or = respect_or
        self._walk(task.get("constraints"))

    def _walk(self, n: Any) -> None:
        if not isinstance(n, (list, tuple)) or not n:
            return
        head = n[0]
        if head == "single":
            name = str(n[1])
            neg = name.startswith("not ")
            self.wanted[name[4:] if neg else name] = not neg
        elif head == "or":
            members: list[str] = []
            for c in n[1]:
                before = set(self.wanted)
                self._walk(c)
                members.extend(k for k in self.wanted if k not in before)
            if len(members) > 1:
                self.or_groups.append(members)
        elif head in ("and", "chain"):
            for c in n[1]:
                self._walk(c)
        else:
            for c in n:
                self._walk(c)

    def group_of(self, constraint: str) -> list[str]:
        """The OR group `constraint` belongs to, or just itself."""
        if self.respect_or:
            for g in self.or_groups:
                if constraint in g:
                    return g
        return [constraint]

    def amount(self) -> Optional[float]:
        for k in ("amount", "pay_owed_amount_request", "loan_amount"):
            v = _as_num(self.known.get(k))
            if v is not None:
                return v
        return None

    def holds(self, constraint: str, res: dict) -> tuple[Optional[bool], str]:
        """Does the observed evidence make `constraint` TRUE? None = cannot tell.

        Polarity-free: "is the user logged in", not "is the task satisfied".
        `satisfied()` folds the task's own polarity in on top of this.
        """
        p, k = self.params, self.known
        amount = self.amount

        def num(tool: str) -> Optional[float]:
            return _as_num(res.get(tool))

        if constraint in (
            "internal_check_username_exist",
            "internal_check_credit_card_exist",
            "internal_check_foreign_currency_available",
        ):
            b = _as_bool(res.get(constraint))
            return (b, f"{constraint} returned {res.get(constraint)!r}") if b is not None else (None, "")
        if constraint == "logged_in_user":
            b = _as_bool(res.get("login_user"))
            return (b, f"login_user returned {res.get('login_user')!r}") if b is not None else (None, "")
        if constraint == "authenticated_admin_password":
            b = _as_bool(res.get("authenticate_admin_password"))
            return (
                (b, f"authenticate_admin_password returned {res.get('authenticate_admin_password')!r}")
                if b is not None
                else (None, "")
            )
        if constraint == "minimal_elgibile_credit_score":
            s, mn = num("internal_get_credit_score"), _as_num(p.get("minimum_credit_score"))
            if s is None or mn is None:
                return None, ""
            return s > mn, f"credit score {s:g} vs required > {mn:g}"
        if constraint == "safety_box_eligible":
            b, mn = num("get_account_balance"), _as_num(p.get("minimum_account_balance_safety_box"))
            if b is None or mn is None:
                return None, ""
            return b >= mn, f"balance {b:g} vs required >= {mn:g}"
        if constraint == "get_loan_owed_balance_restr":
            o, mx = num("get_account_owed_balance"), _as_num(p.get("maximum_owed_balance"))
            if o is None or mx is None:
                return None, ""
            return o < mx, f"owed balance {o:g} vs required < {mx:g}"
        if constraint == "no_owed_balance":
            o = num("get_account_owed_balance")
            return (o == 0, f"owed balance {o:g} vs required 0") if o is not None else (None, "")
        if constraint == "sufficient_account_balance":
            b, a = num("get_account_balance"), amount()
            if b is None or a is None:
                return None, ""
            return b > a, f"balance {b:g} vs requested {a:g}"
        if constraint == "pay_loan_account_balance_restr":
            b, o = num("get_account_balance"), num("get_account_owed_balance")
            if b is None or o is None:
                return None, ""
            return b >= o, f"balance {b:g} vs owed {o:g}"
        if constraint == "pay_loan_amount_restr":
            b, a = num("get_account_balance"), amount()
            if b is None or a is None:
                return None, ""
            return b >= a, f"balance {b:g} vs requested payment {a:g}"
        if constraint == "maximum_deposit_limit":
            a, mx = amount(), _as_num(p.get("maximum_deposit"))
            if a is None or mx is None:
                return None, ""
            return a <= mx, f"deposit {a:g} vs limit {mx:g}"
        if constraint == "maximum_exchange_amount":
            a, mx = amount(), _as_num(p.get("maximum_exchange"))
            if a is None or mx is None:
                return None, ""
            return a <= mx, f"exchange {a:g} vs limit {mx:g}"
        return None, ""

    def satisfied(self, constraint: str, res: dict) -> tuple[Optional[bool], str]:
        """Does the observed evidence SATISFY `constraint` as this task states it?

        Folds polarity in, which `holds` does not: a task requiring
        `not internal_check_username_exist` is satisfied when the username does
        NOT exist.
        """
        holds, why = self.holds(constraint, res)
        if holds is None:
            return None, ""
        want_true = self.wanted.get(constraint, True)
        return (holds if want_true else not holds), why


def make_value_checker(task: dict, tool_of: dict, respect_or: bool = False) -> Any:
    """Judge a step's gate on the VALUE its verification tool returned.

    Returns `(None, reason)` for any step it cannot judge, so the engine falls
    back to liveness rather than guessing — a checker that guessed would trade
    one silent wrongness for another.
    """
    truth = _TaskTruth(task, respect_or=respect_or)
    wanted = truth.wanted

    def value_check(step: Any, tool_history: tuple) -> tuple[Optional[bool], str]:
        from asop_engine import named_tool

        tool = named_tool(step)
        if not tool:
            return None, ""
        res = _tool_results(tool_history)
        # Every constraint THIS task imposes that is verified by THIS step's
        # tool. Matching on the tool rather than on step order means a tool
        # serving two of the task's constraints has both checked, and no
        # ordering assumption can silently mis-pair them.
        relevant = [c for c, want_true in wanted.items() if tool_of.get(c) == tool]
        if not relevant:
            return None, ""
        judged = []
        for c in relevant:
            holds, why = truth.holds(c, res)
            if holds is None:
                continue
            want_true = wanted[c]
            satisfied = holds if want_true else not holds
            if not satisfied:
                # An OR group is satisfied by ANY member. Refusing because this
                # one failed, while a sibling holds, is the grader demanding
                # more than the task does.
                group = truth.group_of(c)
                if len(group) > 1:
                    sibling_ok = False
                    for other in group:
                        if other == c:
                            continue
                        oh, _ow = truth.holds(other, res)
                        if oh is not None and (oh if wanted.get(other, True) else not oh):
                            sibling_ok = True
                            break
                    if sibling_ok:
                        satisfied = True
                        why = f"{why} — but an alternative in its OR group holds"
            judged.append((c, satisfied, why, want_true))
        if not judged:
            return None, ""
        failed = [j for j in judged if not j[1]]
        if failed:
            c, _, why, want_true = failed[0]
            direction = "must hold" if want_true else "must NOT hold"
            return False, f"{c} {direction}, but {why}"
        c, _, why, _ = judged[0]
        return True, f"{c} satisfied: {why}"

    return value_check


def make_judge_checker(task: dict, tool_of: dict, args: Any) -> Any:
    """`--judge-gate`: the judge-backed value gate for THIS task.

    `_TaskTruth` supplies only which constraints the task imposes (with
    polarity) and their OR groups — `respect_or=True`, because a judge asked
    about one member of an OR group would demand more than the task does.
    """
    from sopbench_judge_gate import describe_with_sopbench, make_judge_value_checker

    return make_judge_value_checker(
        task,
        tool_of,
        args.judge_gate,
        describe=describe_with_sopbench(args.domain),
        truth=_TaskTruth(task, respect_or=True),
        tool_results=_tool_results,
        log_path=str(args.judge_log) if args.judge_log else None,
        gate_mode=args.judge_gate_mode,
        hard_block_confidence=args.judge_gate_hard_block_confidence,
    )


# ── verify-then-gate: auditing the executor's own stated verdicts ────────────
#
# The architecture inversion. Every gated arm so far put the gate BEFORE the
# step — machinery decided whether the precondition held and the executor never
# had to articulate anything. `dirgraph_satisfied` has not moved off ~0.59
# across five such configurations while PVA, whose text makes the model STATE a
# verdict per constraint citing the value it saw, reaches 0.79.
#
# Here the executor does the work and states its own verdicts; this validates
# each claim against what the tools actually returned, using the SAME
# `_TaskTruth` the value gate uses. Three outcomes:
#
#   refuse   a verdict is missing, cites no value, or is contradicted by the
#            tool results. ⚠️ The message names the condition and the required
#            form and STOPS — it never says what the verdict should have been.
#            Supplying it would make articulating optional again, which is the
#            barrier this design exists to remove.
#   blocked  the executor said a condition is NOT satisfied and the tool results
#            agree. Terminal: the action must not be taken. The engine had no
#            such state before — a refused gate meant "not yet, retry", so on
#            every impermissible task the executor was told to keep trying and
#            eventually escalated past the gate and acted.
#   ok       every condition carries a stated, cited verdict and every one of
#            them matches the evidence.

#: `VERDICT <label>: SATISFIED - <value>`. The label is whatever the executor
#: wrote between the keyword and the colon; matching it to a condition is done
#: by containment below rather than by exact string, because an executor that
#: writes the condition's readable name instead of its identifier has still
#: articulated the thing this is trying to elicit.
_VERDICT_RE = re.compile(
    r"VERDICT\b[:\s]*(?P<label>[^\n:]{0,160}?)\s*:\s*"
    r"(?P<claim>NOT[\s_-]*SATISFIED|SATISFIED)\b[\s:\-–—]*(?P<cited>[^\n]*)",
    re.I,
)

#: A cited value has to be short but real. Below this the "citation" is an empty
#: flourish ("- ok", "- yes") and the verdict is not evidence of having looked.
MIN_CITATION_CHARS = 3


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def parse_verdicts(said: str) -> list[dict]:
    """Every `VERDICT ...` line the executor wrote this turn, in order."""
    out = []
    for m in _VERDICT_RE.finditer(said or ""):
        claim = not m.group("claim").upper().lstrip().startswith("NOT")
        out.append(
            {
                "label": m.group("label").strip(" *`_-"),
                "satisfied": claim,
                "cited": m.group("cited").strip(" *`_-."),
            }
        )
    return out


#: Numbers and booleans inside a citation. Bare words are not checked: prose
#: paraphrases legitimately ("the balance is plenty"), and refusing that would
#: be scoring style. A number or a boolean is a claim about what came back.
_CITED_TOKEN_RE = re.compile(r"\b(?:true|false|-?\d+(?:\.\d+)?)\b", re.I)


def _numeric(tok: str) -> Optional[float]:
    try:
        return float(tok)
    except ValueError:
        return None


def grounded_citation(cited: str, grounding: str) -> Optional[str]:
    """Does every number the executor cited actually appear in the evidence?

    ⚠️ THE ONE CHECK IN THIS FILE THAT TRANSFERS BETWEEN DOMAINS, and that is
    why it exists. `_TaskTruth.holds` — whether a returned value satisfies a
    condition — is hand-written per constraint and does not survive a move from
    bank's 21 constraints to library's 19 or online_market's 26. "Did the value
    you cited come from somewhere the run actually produced" needs no knowledge
    of the rule at all: it catches a fabricated citation, which is the failure
    mode an attestation architecture is most exposed to.

    Grounding is tool results PLUS the task's own published parameters and the
    user's stated ones — citing the threshold you compared against ("650 vs the
    required 600") is a correct citation, not an invention.

    Returns the offending token, or None when everything checks out. A citation
    with no numbers in it is not judged here.
    """
    hay = grounding.lower()
    hay_nums = {n for n in (_numeric(t) for t in _CITED_TOKEN_RE.findall(hay)) if n is not None}
    for tok in _CITED_TOKEN_RE.findall(cited or ""):
        low = tok.lower()
        if low in ("true", "false"):
            if low not in hay:
                return tok
            continue
        val = _numeric(tok)
        if val is None:
            continue
        # Float-tolerant: "200" and "200.0" are the same claim.
        if not any(abs(val - h) < 1e-9 for h in hay_nums):
            return tok
    return None


def make_attestor(
    task: dict, tool_of: dict, cite_check: bool = False, respect_or: bool = False
) -> Any:
    """Build the verify-then-gate auditor for one task."""
    truth = _TaskTruth(task, respect_or=respect_or)
    # Everything a citation may legitimately come from. Built once per task.
    _known_values = " ".join(
        str(v) for v in list(truth.params.values()) + list(truth.known.values())
    )

    def attest(steps: list, said: str, tool_history: tuple) -> tuple[str, str, list]:
        from asop_engine import named_tool

        stated = parse_verdicts(said)
        res = _tool_results(tuple(tool_history))
        rows: list[dict] = []
        missing: list[str] = []
        contradicted: list[str] = []
        confirmed_failures: list[str] = []
        ungrounded: list[str] = []
        grounding = "\n".join(tool_history) + "\n" + _known_values

        for step in steps:
            tool = named_tool(step)
            title = getattr(step, "title", "") or ""
            # The task's own constraints this step is responsible for. Matched
            # on the tool, exactly as the value gate matches, so the two graders
            # cannot disagree about which condition a step covers.
            covers = [c for c in truth.wanted if tool_of.get(c) == tool]
            keys = [_norm(title)] + [_norm(c) for c in covers]
            if tool:
                keys.append(_norm(tool))

            match = None
            for v in stated:
                lab = _norm(v["label"])
                if not lab:
                    continue
                if any(k and (k in lab or lab in k) for k in keys):
                    match = v
                    break
            if match is None:
                missing.append(title or (covers[0] if covers else tool or "?"))
                rows.append({"step": title, "stated": None, "truth": None})
                continue
            if len(match["cited"]) < MIN_CITATION_CHARS:
                missing.append(f"{title} (stated without citing a value)")
                rows.append({"step": title, "stated": match["satisfied"], "cited": match["cited"],
                             "truth": None, "problem": "no value cited"})
                continue

            if cite_check:
                bad = grounded_citation(match["cited"], grounding)
                if bad is not None:
                    ungrounded.append(f"{title}: you cited {bad}, which no tool returned this run")
                    rows.append({"step": title, "stated": match["satisfied"],
                                 "cited": match["cited"], "truth": None,
                                 "problem": "ungrounded citation"})
                    continue

            # What the evidence says, folding in the task's own polarity.
            judged = []
            for c in covers:
                ok, why = truth.satisfied(c, res)
                if ok is False:
                    group = truth.group_of(c)
                    if len(group) > 1 and any(
                        truth.satisfied(o, res)[0] is True for o in group if o != c
                    ):
                        ok, why = True, f"{why} — satisfied via its OR group"
                if ok is not None:
                    judged.append((c, ok, why))
            if not judged:
                # Nothing computable. The executor's own verdict stands — it is
                # the only judgement available, and inventing one here would be
                # the barrier again.
                rows.append({"step": title, "stated": match["satisfied"], "truth": None,
                             "cited": match["cited"], "resolved": "uncheckable"})
                if not match["satisfied"]:
                    confirmed_failures.append(f"{title}: {match['cited']}")
                continue

            actually = all(ok for _, ok, _ in judged)
            why = "; ".join(w for _, _, w in judged)
            rows.append({"step": title, "stated": match["satisfied"], "truth": actually,
                         "cited": match["cited"], "why": why, "resolved": "checked"})
            if match["satisfied"] != actually:
                contradicted.append(
                    f"{title}: you stated "
                    f"{'SATISFIED' if match['satisfied'] else 'NOT SATISFIED'}, "
                    f"but the tool results show {why}"
                )
            elif not actually:
                confirmed_failures.append(f"{title}: {why}")

        if missing:
            return (
                "refuse",
                "You have not attested to every condition. Missing or uncited: "
                + "; ".join(missing)
                + ". Write one VERDICT line per condition, in the form given, citing the "
                "value the tool returned. If you never called that condition's tool, call "
                "it before attesting.",
                rows,
            )
        if ungrounded:
            return (
                "refuse",
                "Your attestation cites a value the run did not produce. "
                + "; ".join(ungrounded)
                + ". Re-read what the tools actually returned and attest again.",
                rows,
            )
        if contradicted:
            return ("refuse", "Your attestation does not match the tool results. "
                    + "; ".join(contradicted) + ". Re-read the results and attest again.", rows)
        if confirmed_failures:
            return ("blocked", "; ".join(confirmed_failures), rows)
        return ("ok", "every condition attested and confirmed against the tool results", rows)

    return attest


def render_asop(asop: Any) -> str:
    """An ASOP object back to markdown, so both arms can carry the SAME document.

    `asop-prompt` is the control that makes a loss attributable: it must differ
    from `asop-gated` in enforcement and in nothing else. If the gated arm walks
    a per-task narrowed procedure while the prompt arm reads the full
    domain-static one, the pair differs in WHICH preconditions it was told about
    as well as in whether they were enforced, and the contrast stops isolating
    anything. So the narrowed document is rendered back out and handed to the
    prompt arm verbatim.
    """
    parts = [asop.preamble.strip(), ""]
    if asop.routing_text:
        parts += ["## Routing", "", asop.routing_text.strip(), ""]
    for p in asop.procedures:
        parts += [f"## Procedure: {p.name}", ""]
        for i, s in enumerate(p.steps, 1):
            parts.append(f"{i}. {s.body.strip()}")
        parts.append("")
    return "\n".join(parts)


def make_llm_router(
    asop: Any, model: str, base_url: str = "http://localhost:4242/v1", role: str = "system"
) -> Any:
    """Route with one model call per conversation, as tau2's arm (c) does.

    This is a ROUTER, not a gate. The scope document's "no LLM judge on the
    first attempt" rule is about the thing that decides whether a step PASSED —
    that stays deterministic (`check_tool_succeeded`), and no model verdict
    enters the gate at any point. Choosing which procedure a request belongs to
    is upstream of any verdict, and `asop_agent.create_asop_agent` already does
    it with a model call, so doing it any other way here would make the two
    hosts differ in something other than what is under test.

    The executor's own model is reused: it is local, free, and already resident,
    so this adds no vendor spend and no second model to keep warm.
    """
    from openai import OpenAI

    # A local server ignores the key; a hosted backend (`--zai`) needs the real
    # one, which `use_zai()` has put in the environment — never in argv or logs.
    client = OpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY") or "placeholder")

    def call(prompt: str, as_role: str, **extra: Any) -> str:
        reply = client.chat.completions.create(
            model=model,
            # ⚠️ z.ai answers a system-only message list with HTTP 400 code
            # 1214 ("messages parameter is illegal") — the first gate-test
            # smoke routed NOTHING because of it. `--zai` passes "user";
            # every local arm keeps "system", byte-identical to before.
            messages=[{"role": as_role, "content": prompt}],
            temperature=0.0,
            max_tokens=64,
            **extra,
        )
        return reply.choices[0].message.content or ""

    def route(prompt: str) -> str:
        try:
            answer = call(prompt, role)
            if answer.strip():
                return answer
            err: Exception = RuntimeError("empty routing answer")
        except Exception as exc:
            err = exc
        # ⚠️ FALLBACK, taken only when the call above failed or came back empty,
        # so a model that routes fine keeps the exact request it had before.
        # 2026-09-26: Qwen3.6's whole V2b arm (2,885 calls) never routed — its
        # LM Studio template rejects a system-only list ("No user query found
        # in messages"), and as "user" it spends all 64 tokens reasoning and
        # returns empty content. As "user" with reasoning off it answers in ~3.
        try:
            answer = call(prompt, "user", extra_body={"reasoning_effort": "none"})
            if answer.strip():
                return answer
            err = RuntimeError("empty routing answer after fallback")
        except Exception as exc:
            err = exc
        # a routing outage must not look like a method failure
        print(f"[route] call failed: {err}", file=sys.stderr)
        return ""

    return route


# ── arms ─────────────────────────────────────────────────────────────────────


def make_prompt_swarm(base_swarm_cls: type, doc_provider: Any, stats_sink: list):
    """`asop-prompt`: the document as context, no stepwise walk, no gates.

    The control that makes a loss attributable. It appends the compiled ASOP to
    whatever instructions SOPBench built for the task, exactly as `--scaffold
    pva` appends its scaffold text, so the only difference from `pva` is which
    document is appended.
    """

    class PromptSwarm(base_swarm_cls):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._installed = False
            self._turns = 0
            self._exited = False
            self._tool_calls = 0
            stats_sink.append(self)

        def get_chat_completion(self, agent: Any, history: list, debug: bool = False):
            if getattr(agent, "client", None) is not None and not self._installed:
                # The SAME termination note the gated arm appends, so the host's
                # stop requirement is stated identically in both and cannot be a
                # difference between them.
                from sopbench_asop_swarm import TERMINATION_NOTE

                agent.instructions = (
                    agent.instructions
                    + "\n\n# Procedure document\n\n"
                    + doc_provider()
                    + "\n\nFollow the procedure that matches this request, in order."
                    + TERMINATION_NOTE
                )
                self._installed = True
            completion = super().get_chat_completion(agent, history, debug)
            if getattr(agent, "client", None) is not None:
                self._turns += 1
                try:
                    calls = completion.choices[0].message.tool_calls or []
                except Exception:
                    calls = []
                self._tool_calls += len(calls)
                for tc in calls:
                    if getattr(getattr(tc, "function", None), "name", None) == "exit_conversation":
                        self._exited = True
            return completion

    return PromptSwarm


# ── entry ────────────────────────────────────────────────────────────────────


def install_shard(
    run_simulation: Any, domain: str, limit: int, spec: Optional[str], task_ids: Any = None
) -> None:
    """Make `run_simulation.main` run only shard `spec` ("K/N", default all) of
    the tasks, restricted to the positions in the `task_ids` file if given.

    Positional resume is the whole mechanism — see `sopbench_shard`. Shared by
    this runner and `run_sopbench_ladder.py`, so the baseline arms and the ASOP
    arms select identically. Must be called with cwd at the SOPBench root.
    """
    from sopbench_shard import load_subset, parse_shard, seed_results

    k, n = parse_shard(spec) if spec else (0, 1)
    subset = load_subset(task_ids)
    tasks_file = Path("data") / f"{domain}_tasks.json"
    n_tasks = sum(len(v) for v in json.loads(tasks_file.read_text()).values())
    if limit:
        n_tasks = min(n_tasks, limit)
    real_load = run_simulation.load_existing_results

    if subset is not None and max(subset, default=-1) >= n_tasks:
        raise SystemExit(f"--task-ids names position {max(subset)}, but only {n_tasks} tasks exist")

    def sharded_load(output_file):
        return seed_results(real_load(output_file), n_tasks, k, n, subset)

    run_simulation.load_existing_results = sharded_load
    mine = sum(1 for i in range(n_tasks) if i % n == k and (subset is None or i in subset))
    print(
        f"[shard] {k}/{n}{' of a ' + str(len(subset)) + '-task subset' if subset is not None else ''}: "
        f"{mine} of {n_tasks} tasks",
        file=sys.stderr,
    )


def route_requests_to_instance(model: str, instance: str) -> None:
    """Rewrite the wire-level `model` of every chat completion from `model` to
    `instance`, process-wide — SOPBench's handler, the LLM router and the
    judge all go through the `openai` client, so one patch covers every call.
    Anything else asked for by name passes through untouched.
    """
    from openai.resources.chat.completions import Completions

    real = Completions.create
    if getattr(real, "_routes_to", None):
        return

    def create(self, *a, **kw):
        if kw.get("model") == model:
            kw["model"] = instance
        return real(self, *a, **kw)

    create._routes_to = instance
    Completions.create = create


def aggregate_rows(rows: list) -> dict:
    """The run-level counts over per-conversation stats rows.

    Factored out so `sopbench_shard.merge` recomputes a merged summary with the
    SAME arithmetic an unsharded run uses — a merged summary is never
    hand-summed. `ungated_tripwire_calls` is not here: it lives on the tripwire,
    not the rows, and a merge sums it across shards.
    """
    out = {
        "conversations": len(rows),
        "exited_cleanly": sum(1 for r in rows if r.get("exited_cleanly")),
        "hit_cap": sum(1 for r in rows if not r.get("exited_cleanly")),
        "mean_turns": round(sum(r.get("turns", 0) for r in rows) / max(len(rows), 1), 2),
        "mean_tool_calls": round(
            sum(r.get("tool_calls", 0) for r in rows) / max(len(rows), 1), 2
        ),
        "gates_run": sum(r.get("gates_run", 0) for r in rows),
        "gates_passed": sum(r.get("gates_passed", 0) for r in rows),
        "gates_deterministic": sum(r.get("gates_deterministic", 0) for r in rows),
        "gates_fell_back": sum(r.get("gates_fell_back", 0) for r in rows),
        "escalations": sum(r.get("escalations", 0) for r in rows),
        # -- verify-then-gate signatures, all zero without `--verdict-gate` --
        # `verdict_lines` is the one to read first: it is how often the executor
        # actually articulated a verdict, which is the mechanism under test. If
        # it is near zero the arm did not test the architecture, it tested
        # whether the model can follow a format.
        "attest_attempts": sum(r.get("attest_attempts", 0) for r in rows),
        "attest_ok": sum(r.get("attest_ok", 0) for r in rows),
        "attest_refused": sum(r.get("attest_refused", 0) for r in rows),
        "attest_contradicted": sum(r.get("attest_contradicted", 0) for r in rows),
        "attest_blocked": sum(1 for r in rows if r.get("attest_blocked")),
        "attest_escalated": sum(1 for r in rows if r.get("attest_escalated")),
        "verdict_lines": sum(r.get("verdict_lines", 0) for r in rows),
        "routed": {},
    }
    for r in rows:
        p = r.get("procedure")
        if p:
            out["routed"][p] = out["routed"].get(p, 0) + 1
    return out


def build_argv(args: argparse.Namespace) -> list[str]:
    """T2's exact configuration, so the new arms pair against existing numbers.

    Everything here matches `T2-PROCEDURE-LADDER.md`'s reproduction block:
    `bank`, all 134 tasks, 1 run each, `openai/gpt-oss-20b`, scripted user
    (`--user_model` unset), `mode_fc`, `dep_full`, `fmt_structured`,
    `tool_full`, shuffle off. The one flag that MUST differ per arm is
    `--output_dir`: the output filename encodes model/mode/dep/fmt/tool/shuffle
    only, so arms sharing a directory silently inherit each other's results
    through `load_existing_results` with no error anywhere.
    """
    argv = [
        "run_simulation.py",
        "--domain", args.domain,
        "--assistant_model", args.model,
        "--num_run_per_interaction", "1",
        "--output_dir", str(args.output_dir),
        "--tool_call_mode", "fc",
    ]
    if args.limit:
        argv += ["--num_tasks", str(args.limit)]
    if args.max_turns:
        argv += ["--max_num_turns", str(args.max_turns)]
    if args.max_actions:
        argv += ["--max_num_actions", str(args.max_actions)]
    # 0 = SOPBench's default (512), so every published arm reproduces. GLM-4.7's
    # reasoning spends that budget and truncates tool-call JSON, which SOPBench
    # then retries at temperature 0.7 (15 of 40 pilot tasks, 2026-09-24).
    if getattr(args, "assistant_max_tokens", 0):
        argv += ["--assistant_max_tokens", str(args.assistant_max_tokens)]
    return argv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["asop-prompt", "asop-gated"])
    ap.add_argument("--domain", default="bank")
    ap.add_argument("--asop", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--sopbench", type=Path, default=DEFAULT_SOPBENCH)
    ap.add_argument("--model", default="openai/gpt-oss-20b")
    ap.add_argument("--base-url", default="http://localhost:4242/v1")
    ap.add_argument("--limit", type=int, default=0, help="task cap, for pre-flight")
    ap.add_argument("--max-turns", type=int, default=0, help="0 = SOPBench default (20)")
    ap.add_argument("--max-actions", type=int, default=0, help="0 = SOPBench default (10)")
    ap.add_argument("--stats-out", type=Path, default=None)
    ap.add_argument(
        "--assistant-max-tokens",
        type=int,
        default=0,
        help="SOPBench --assistant_max_tokens; 0 = its default (512), which every "
        "published arm used",
    )
    ap.add_argument(
        "--shard",
        default=None,
        metavar="K/N",
        help="run only tasks i with i %% N == K (zero-based), for N processes each "
        "pointed at its OWN model instance via --model. Each shard needs its own "
        "--output-dir; reassemble with `sopbench_shard.py merge`. See that module "
        "for why N instances at --parallel 1, not one instance at --parallel N.",
    )
    ap.add_argument(
        "--instance",
        default=None,
        help="LM Studio identifier of the model COPY to send requests to (e.g. "
        "gpt-oss-20b-s1). `--model` stays the canonical name: SOPBench whitelists "
        "model names, derives the results filename from it, and records it, so "
        "only the wire-level `model` field is rewritten — a sharded arm's files "
        "are named and recorded exactly as an unsharded one's.",
    )
    ap.add_argument(
        "--restore-helper-prereqs",
        action="store_true",
        help="EXP-A / N31. After per-task narrowing, re-add (transitively) every step "
        "a KEPT helper's own default prerequisites require, which the scorer holds "
        "every helper to. Off by default so every published arm reproduces.",
    )
    ap.add_argument(
        "--task-ids",
        type=Path,
        default=None,
        metavar="FILE",
        help="run only the task POSITIONS in this JSON list (e.g. the hotel pilot). "
        "Same placeholder mechanism as --shard, and composes with it: a shard runs "
        "selected ∩ owned. Merge with `sopbench_shard.py merge --subset FILE`.",
    )
    ap.add_argument(
        "--register-model",
        action="append",
        default=[],
        metavar="NAME",
        help="admit NAME to SOPBench's OpenAI-backend whitelists at runtime (no "
        "edit to swarm/constants.py). Repeatable. Needed for any --model the "
        "checkout does not list, e.g. glm-4.7.",
    )
    ap.add_argument(
        "--zai",
        action="store_true",
        help="send every OpenAI-format call (executor AND router) to z.ai's "
        "Coding-Plan endpoint, with ZAI_API_KEY from env or ~/.claude/.env. "
        "Overrides --base-url. The key is never printed.",
    )
    ap.add_argument(
        "--judge-gate",
        choices=["jev", "laya", "clef", "string-all"],
        default=None,
        help="value gate backed by a JUDGE instead of `_TaskTruth` — the portable "
        "gate for domains the hand-written table does not cover. `jev` asks "
        "TypeSafe's Jev; `laya` asks Laya via laya-serve (LAYA_URL, default "
        "127.0.0.1:4243); `clef` asks Cloudflare Clef via evals/sopbench-clef/serve (CLEF_URL, default 127.0.0.1:8791); `string-all` is the no-judgment control. See "
        "`sopbench_judge_gate`. Mutually exclusive with --value-gate.",
    )
    ap.add_argument(
        "--judge-log",
        type=Path,
        default=None,
        help="append one JSON line per judged condition (step, constraint, "
        "polarity, verdict, evidence hash) — what grading the gate needs later",
    )
    ap.add_argument(
        "--judge-gate-mode",
        choices=["block", "advisory"],
        default="block",
        help="how a not_held --judge-gate verdict is enforced. `block` "
        "(default, unchanged): hard stop, same as every published judge-gate "
        "arm. `advisory`: the step PASSES and a note (which condition looked "
        "unmet, the judge's confidence, the evidence it saw) is queued for the "
        "executor's NEXT prompt instead — except a not_held verdict at "
        "--judge-gate-hard-block-confidence or above, which still hard-stops "
        "(N36: confident blocks were 96%% correct on `hotel`). Requires "
        "--judge-gate.",
    )
    ap.add_argument(
        "--judge-gate-hard-block-confidence",
        type=float,
        default=0.99,
        metavar="CONF",
        help="in --judge-gate-mode advisory, a not_held verdict at this "
        "confidence or above still hard-stops instead of becoming an advisory "
        "note. Ignored in (default) block mode.",
    )
    ap.add_argument(
        "--trace-steps",
        action="store_true",
        help="print how many steps survive per-task narrowing, per task",
    )
    ap.add_argument(
        "--value-gate",
        action="store_true",
        help="resolve deterministic gates on the RETURNED VALUE rather than on "
        "tool liveness (falls back to liveness for any step it cannot judge). "
        "Off by default so the published liveness arm stays reproducible.",
    )
    ap.add_argument(
        "--host-rules",
        action="store_true",
        help="keep SOPBench's own task instructions (the domain constraint "
        "specification and its thresholds) above the ASOP prompt instead of "
        "replacing them. INFORMATION PARITY with every other arm — see "
        "sopbench_asop_swarm.HOST_RULES_BLOCK. Off by default so the published "
        "gated arms stay reproducible.",
    )
    ap.add_argument(
        "--upfront",
        action="store_true",
        help="present the whole per-task procedure as a checklist to enumerate "
        "and verify before acting, rather than one step at a time. "
        "PRESENTATION ONLY: gates, gate kinds, the value check and advancement "
        "are unchanged. Tests N27's hypothesis directly.",
    )
    ap.add_argument(
        "--verdict-gate",
        action="store_true",
        help="VERIFY-THEN-GATE. Insert one attestation turn before a procedure's "
        "final action: the executor states a SATISFIED/NOT SATISFIED verdict per "
        "condition, citing the value it observed, and the gate validates each "
        "claim against what the tools actually returned. A missing verdict is "
        "itself the refusal, and the gate never supplies one. Off by default so "
        "every published arm reproduces.",
    )
    ap.add_argument(
        "--attest-failed-checks",
        action="store_true",
        help="ROUND 3. A VERIFY step whose gate resolves NOT satisfied ON THE "
        "RETURNED VALUE routes to an attestation for that one condition, and a "
        "NOT SATISFIED the tool results confirm ends the procedure instead of "
        "looping. ESTABLISH steps keep the retry loop — a login can be retried, "
        "an account balance cannot. Requires --verdict-gate.",
    )
    ap.add_argument(
        "--cite-check",
        action="store_true",
        help="ROUND 4. Refuse an attestation that cites a number or boolean no "
        "tool returned this run (grounded against tool results plus the task's "
        "own published parameters). The one check here that TRANSFERS between "
        "domains: it needs no knowledge of the rule, only of the evidence. "
        "Requires --verdict-gate.",
    )
    ap.add_argument(
        "--respect-or",
        action="store_true",
        help="ROUND 5. Treat an OR group in the TASK's constraint tree as "
        "satisfied by any one member, instead of demanding every leaf. Fixes a "
        "defect in the gate's own grader found by validating it against "
        "`action_should_succeed` with perfect evidence; it has over-refused "
        "8/112 routable `bank` tasks (all `pay_loan`), 30/66 `library` and "
        "40/172 `online_market` since N27. Off by default so the arms already "
        "measured stay comparable.",
    )
    args = ap.parse_args()
    if args.cite_check and not args.verdict_gate:
        raise SystemExit("--cite-check needs --verdict-gate: there is no attestation to ground")
    if args.attest_failed_checks and not args.verdict_gate:
        raise SystemExit("--attest-failed-checks needs --verdict-gate: there is no attestor to route to")

    if args.judge_gate and args.value_gate:
        raise SystemExit("--judge-gate and --value-gate are two value gates; pick one")
    if args.judge_gate_mode != "block" and not args.judge_gate:
        raise SystemExit("--judge-gate-mode needs --judge-gate: there is no judge to gate with")
    if not 0.0 <= args.judge_gate_hard_block_confidence <= 1.0:
        raise SystemExit("--judge-gate-hard-block-confidence must be in [0, 1]")
    if args.judge_gate:
        # Fail loud before spending an hour of z.ai/executor time on a run
        # that would silently degrade to liveness-only if the judge is dead
        # (see sopbench_judge_gate.preflight_judge — 2026-09-26 outage).
        from sopbench_judge_gate import preflight_judge

        ok, msg = preflight_judge(args.judge_gate)
        print(f"[judge-preflight] {msg}", file=sys.stderr)
        if not ok:
            raise SystemExit(f"--judge-gate preflight failed, refusing to start: {msg}")
    if args.zai:
        from sopbench_judge_gate import use_zai

        args.base_url = use_zai()
    if args.judge_log:
        args.judge_log = args.judge_log.resolve()  # cwd moves to the SOPBench root below
    if args.task_ids:
        args.task_ids = args.task_ids.resolve()
    document = args.asop.read_text()

    sys.path.insert(0, str(args.sopbench))
    import os

    os.chdir(args.sopbench)

    import run_simulation
    from swarm.core import Swarm as BaseSwarm

    if args.register_model:
        from sopbench_judge_gate import register_model

        for name in args.register_model:
            register_model(name)

    stats_sink: list = []
    tripwire = _UngatedTripwire()

    from asop_engine import Verifier, parse_asop

    if args.judge_gate and args.judge_gate_mode == "advisory":
        from sopbench_judge_gate import install_advisory_prompt_hook

        install_advisory_prompt_hook()

    asop = parse_asop(document)
    print(
        f"[asop] {len(asop.procedures)} procedures, "
        f"{sum(len(p.steps) for p in asop.procedures)} steps, "
        f"{len(asop.routing)} routing phrases",
        file=sys.stderr,
    )

    # Capture the task `run_simulation` is about to run, so the per-task
    # document can be built from its own constraint set. `task_initializer`
    # is called immediately before `Swarm(...)` in `run_task_simulation`, so a
    # wrapper here sees exactly the task the next Swarm will serve. Same
    # posture as rebinding `Swarm`: the benchmark is called by name, never
    # edited.
    #
    # BOTH arms go through this. `asop-prompt` is the control that makes a loss
    # attributable, so it has to read the same document the gated arm walks —
    # otherwise the pair differs in which preconditions it was told about as
    # well as in whether they were enforced.
    tool_of = build_constraint_tool_map(args.sopbench, args.domain)
    helper_prereqs = (
        build_helper_prereq_map(args.sopbench, args.domain)
        if args.restore_helper_prereqs
        else None
    )
    holder: dict = {}
    real_init = run_simulation.task_initializer

    def capturing_initializer(domain, task, *a, **kw):
        holder["task"] = task
        return real_init(domain, task, *a, **kw)

    run_simulation.task_initializer = capturing_initializer

    def asop_provider():
        task = holder.get("task")
        if not task:
            return asop
        narrowed = make_task_asop_provider(asop, task, tool_of, helper_prereqs)
        if args.trace_steps:
            target = " ".join(w.capitalize() for w in task["user_goal"].split("_"))
            p = next((q for q in narrowed.procedures if q.name.lower() == target.lower()), None)
            print(
                f"[asop] {task['user_goal']}: "
                f"{len(p.steps) if p else 0} steps after per-task narrowing",
                file=sys.stderr,
            )
        return narrowed

    if args.arm == "asop-prompt":
        run_simulation.Swarm = make_prompt_swarm(
            BaseSwarm, lambda: render_asop(asop_provider()), stats_sink
        )
    else:
        from sopbench_asop_swarm import make_asop_swarm

        run_simulation.Swarm = make_asop_swarm(
            base_swarm_cls=BaseSwarm,
            asop=asop,
            verifier=Verifier(identity="deterministic-fallback", judge=tripwire),
            identity=f"executor:{args.model}",
            # MEASURED before the run: the model router routes 12/14 bank goals
            # correctly, and both misses are goals with no procedure at all
            # (`cancel_credit_card`, `pay_bill_with_credit_card` are absent from
            # the domain's `actions` schema, so `create_assistant` never exposes
            # them and no arm can perform them). On routable goals it is 12/12.
            # The deterministic `make_router` scored 0.343 on all 134 and is kept
            # only as the control that showed why — see its docstring.
            route_fn=make_llm_router(
                asop, args.model, args.base_url, role="user" if args.zai else "system"
            ),
            stats_sink=stats_sink,
            asop_provider=asop_provider,
            value_check_provider=(
                (lambda: make_value_checker(holder.get("task") or {}, tool_of, args.respect_or))
                if args.value_gate
                else (
                    (lambda: make_judge_checker(holder.get("task") or {}, tool_of, args))
                    if args.judge_gate
                    else None
                )
            ),
            host_rules=args.host_rules,
            upfront=args.upfront,
            attestor_provider=(
                (lambda: make_attestor(
                    holder.get("task") or {}, tool_of, args.cite_check, args.respect_or
                ))
                if args.verdict_gate
                else None
            ),
            attest_on_failed_check=args.attest_failed_checks,
        )

    if args.instance:
        route_requests_to_instance(args.model, args.instance)

    if args.shard or args.task_ids:
        install_shard(run_simulation, args.domain, args.limit, args.shard, args.task_ids)

    argv_backup = sys.argv
    sys.argv = build_argv(args)
    try:
        run_simulation.main()
    finally:
        sys.argv = argv_backup

    # -- the pre-registered failure signatures ---------------------------
    rows = []
    for sw in stats_sink:
        if hasattr(sw, "_stats"):
            rows.append(sw._stats.as_dict())
        else:
            rows.append(
                {
                    "exited_cleanly": sw._exited,
                    "turns": sw._turns,
                    "tool_calls": sw._tool_calls,
                }
            )
    summary = {
        "arm": args.arm,
        "domain": args.domain,
        # The three flags that make two runs of the same `--arm` different
        # experiments. Recorded in the stats file so a trajectory directory is
        # self-describing and cannot be mistaken for another arm's later.
        "value_gate": bool(args.value_gate),
        "host_rules": bool(args.host_rules),
        "upfront": bool(args.upfront),
        "verdict_gate": bool(args.verdict_gate),
        "attest_failed_checks": bool(args.attest_failed_checks),
        "cite_check": bool(args.cite_check),
        "respect_or": bool(args.respect_or),
        "asop_document": str(args.asop),
        "shard": args.shard,
        "task_ids": str(args.task_ids) if args.task_ids else None,
        "instance": args.instance,
        "judge_gate": args.judge_gate,
        "judge_gate_mode": args.judge_gate_mode if args.judge_gate else None,
        "judge_gate_hard_block_confidence": (
            args.judge_gate_hard_block_confidence if args.judge_gate else None
        ),
        "assistant_max_tokens": args.assistant_max_tokens or 512,
        "zai": bool(args.zai),
        "model": args.model,
        "restore_helper_prereqs": bool(args.restore_helper_prereqs),
    }
    summary.update(aggregate_rows(rows))
    if args.judge_gate:
        from sopbench_judge_gate import STATS

        summary.update(STATS.as_dict())
    # Must be zero. Anything else means a step was not gated at all.
    summary["ungated_tripwire_calls"] = tripwire.calls

    print(json.dumps(summary, indent=2), file=sys.stderr)
    if args.stats_out:
        args.stats_out.parent.mkdir(parents=True, exist_ok=True)
        args.stats_out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    if tripwire.calls:
        print(
            f"\n*** {tripwire.calls} UNGATED STEPS — the compiled document has gates "
            f"the runtime cannot answer deterministically. Examples:\n  "
            + "\n  ".join(tripwire.steps[:5]),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
