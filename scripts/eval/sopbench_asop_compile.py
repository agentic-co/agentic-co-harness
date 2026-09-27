#!/usr/bin/env python3
"""Compile SOPBench's `bank` domain tables into a parseable ASOP document.

    <sopbench>/.venv/bin/python scripts/eval/sopbench_asop_compile.py \\
        --domain bank --out /tmp/bank.asop.md --stats-out /tmp/bank.asop.stats.json

WHY THIS EXISTS
---------------
Every ASOP measured in this repo so far was hand-authored or LLM-extracted from
a transcript, and `asop_agent.parse_asop`'s own docstring records that three
models given identical instructions produced three mutually incompatible
structures. That is a finding about ASOP as an interchange format, but it also
means nothing has ever tested the OTHER direction: can a real domain's ALREADY
STRUCTURED ground truth (SOPBench's dependency tables, which are exactly "what
must be verified before this action, and how") be turned into an ASOP with no
model in the loop at all? If it can, gate accuracy on that document is scored
against the domain's actual precondition graph, not against a paraphrase of
one.

This script is that compiler. It reads `bank`'s `action_required_dependencies`,
`action_customizable_dependencies`, `constraint_links`, and
`constraint_processes` (SOPBench's own ground truth, imported, never
re-derived) and emits a numbered-step ASOP that `asop_agent.parse_asop` can
walk, with a deterministic gate on every step whose precondition names an
agent-callable tool.

THE ENV_NODES DECISION (read this before changing tool selection)
-------------------------------------------------------------------
`internal_get_database` is not a real option for a gate. `sopbench_extract.py`
established the reason exactly: it appears as an OR-branch in nearly every
`constraint_processes` entry, but `env/task.py` withholds it from the agent
unless `provide_database_getter` is set (false for `bank`), and the deployed
judge that instruction produced named it in 37 of 38 false refusals on this
same domain (see `render_prereq`'s docstring in `sopbench_extract.py`). This
compiler must not reproduce that defect from the other direction: a gate that
names a tool nothing can call is not "deterministic", it is a step that can
never pass. So `environment_verified_nodes()` — imported, not reimplemented —
is consulted before any tool is chosen for a gate, and a constraint whose ONLY
reachable verification branch is environment-verified gets `Gate: judged`
instead of a gate naming a tool the agent does not have.

THE OR-BRANCH BOUND (the other non-obvious choice)
----------------------------------------------------
`asop_agent.named_tool()` can name exactly one tool per gate — it returns the
FIRST backticked identifier it finds. `constraint_processes` sometimes offers
several agent-reachable ways to verify the same constraint (e.g.
`pay_loan_account_balance_restr` can be checked via `get_account_balance` +
`get_account_owed_balance`, OR via the withheld `internal_get_database`). The
gate can only assert on ONE call, so this compiler picks the first reachable
leaf (in the order the dependency tree lists them) as the gate's tool and
names any other reachable leaves in the step's prose AS PLAIN WORDS, never in
backticks — a backticked mention would be a second tool name in the scan
window `named_tool()` reads, and the instruction is that only the chosen gate
tool may appear that way. Every such constraint is recorded in `--stats-out`
under `multi_reachable_tools` so the write-up can say how often the bound
actually bit.

VARIANT v2 — WHAT CHANGED AND WHY (`--variant v2`, default stays `v1`)
------------------------------------------------------------------------
v1 is kept byte-for-byte reproducible because every published arm was measured
on it. v2 changes four things, each traceable to a measurement rather than to
taste:

1. **STEP ORDER IS TOPOLOGICAL.** ⚠️ This is the one that was found by measuring,
   and it is a defect, not a preference. `dirgraph_satisfied` — a conjunct of
   SOPBench's `success` — requires that before EVERY tool call, that tool's own
   prerequisite subtree has already been satisfied. v1 emits a procedure's
   constraints in `required + customizable` table order, and for **5 of bank's
   20 procedures that order is one the domain's own action graph forbids**:
   `authenticate_admin_password` depends on `logged_in_user`, and so do
   `get_account_balance`, `get_account_owed_balance` and `get_credit_cards`.
   `Get Safety Box` compiles to `internal_check_username_exist ->
   authenticate_admin_password -> login_user`; an executor that obeys that order
   fails `dirgraph` on its second call, every time, by construction.
   Measured on the `asop-gated-value-rules` trajectories: **38 of its 44
   dirgraph failures are exactly this shape** (18 `authenticate_admin_password`
   and 15 `get_account_balance` called with only `internal_check_username_exist`
   before them). v2 stable-topologically-sorts each procedure's steps against
   the transitive closure of its own tools' prerequisites.
2. **CHECK IS SEPARATED FROM ACT.** `constraint_links` maps a state-tracker
   constraint to the action that *changes* its state — `logged_in_user ->
   login_user` — which is not a verification helper at all. v1 rendered it as
   "Verify the user login status condition ... (tool call: `login_user`)". v2
   labels those steps ESTABLISH (perform this action to put the account into the
   required state) and `constraint_processes` steps VERIFY (read state, do not
   change it), which is the distinction PVA's text makes explicitly.
3. **COMPOSITION LOGIC SURVIVES.** v1 flattens AND/OR/chain into one mandatory
   sequential list, so an `("or", [A, B])` group becomes two mandatory steps —
   the agent is required to satisfy both. v2 emits an OR group as ONE step that
   names its alternatives, and states each procedure's ALL/ANY-ONE structure
   above its steps.
4. **THE BOILERPLATE IS STATED ONCE, AND IT ASKS FOR A VERDICT.** v1 repeats
   "The rules above state what this condition must be for this request..."
   roughly 60 times. v2 states the working discipline once in the preamble —
   which is the block the engine injects into every step prompt anyway — and
   that discipline is PVA-shaped: call the helper, then state
   `VERDICT <condition>: SATISFIED|NOT SATISFIED — <the value you observed>`.
   Eliciting the verdict is the mechanism the winning arm has and this one
   never had.

WHAT THIS IS NOT
----------------
Not a general ASOP compiler — it hardcodes `bank`'s dependency-tree shapes
(`single`/`and`/`or`/`chain`/list/`None`) and its own constraint vocabulary
(the `_READABLE` / `_RESTATEMENT` lookup tables are bank-specific by design:
the task instructions prefer a hand-written dictionary over a generic string
transform, because the constraint names are English-ish but not English).
Extending to another SOPBench domain means adding that domain's lookup
entries, not touching the tree-walking or gate-selection logic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_SOPBENCH = Path.home() / "Code" / "SOPBench"

# ── dependency-tree flattening ───────────────────────────────────────────────


def flatten_dep_tree(node: Any) -> list[str]:
    """Read a nested SOPBench dependency tree into an ordered list of leaf names.

    Shapes, per the task spec and confirmed against `bank_assistant.py`:
    `("single", name, param_map)`, `("and", [children])`, `("or", [children])`,
    `("chain", [children])`, a plain `list` (treated as AND), or `None`.

    AND/OR/chain are flattened identically — every leaf they reach is appended
    in tree order with no boolean structure retained. That is a deliberate MVP
    simplification the task spec calls for ("AND/OR/chain all flatten to
    sequential steps"), not an oversight: a gate can only assert on one tool
    per step (see the OR-branch bound above), so preserving OR's "any one of
    these" semantics here would just be thrown away one call site later. What
    survives is leaf ORDER, which the tool-selection pass below depends on
    ("first reachable leaf wins").

    A `"not X"` name (SOPBench's polarity marker for required dependencies) is
    stripped to `X` — this compiler is deterministic ground truth about WHICH
    condition to check, never about which direction it must resolve, so the
    polarity is not this document's concern (the per-task rules supply that).
    """
    names: list[str] = []

    def walk(n: Any) -> None:
        if n is None:
            return
        if isinstance(n, list):
            for child in n:
                walk(child)
            return
        if isinstance(n, tuple):
            kind = n[0]
            if kind == "single":
                name = n[1]
                if name.startswith("not "):
                    name = name[len("not ") :]
                names.append(name)
            elif kind in ("and", "or", "chain"):
                for child in n[1]:
                    walk(child)
            else:
                raise ValueError(f"unrecognised dependency node kind: {kind!r}")
            return
        raise TypeError(f"unrecognised dependency node: {n!r}")

    walk(node)
    return names


def flatten_dep_groups(node: Any) -> list[tuple[str, list[str]]]:
    """Like `flatten_dep_tree`, but OR groups survive as groups. (v2 only.)

    Returns an ordered list of `("single", [name])` / `("or", [name, ...])`.
    AND, chain and plain lists still flatten — they mean "all of these, in this
    order", which a sequence of steps already expresses. OR does not: rendering
    `("or", [A, B])` as two steps tells the executor to satisfy BOTH, which is
    strictly more than the domain requires and is over-gating written into the
    document. Nested ORs are flattened into their enclosing group rather than
    nested further; bank has none, and a deeper shape would need its own
    rendering rather than a silent approximation, so it raises.
    """
    groups: list[tuple[str, list[str]]] = []

    def leaves(n: Any, into: list[str]) -> None:
        for name in flatten_dep_tree(n):
            into.append(name)

    def walk(n: Any) -> None:
        if n is None:
            return
        if isinstance(n, list):
            for child in n:
                walk(child)
            return
        if isinstance(n, tuple):
            kind = n[0]
            if kind == "single":
                name = n[1]
                if name.startswith("not "):
                    name = name[len("not ") :]
                groups.append(("single", [name]))
            elif kind in ("and", "chain"):
                for child in n[1]:
                    walk(child)
            elif kind == "or":
                collected: list[str] = []
                leaves(n, collected)
                groups.append(("or", dedupe_preserve_order(collected)))
            else:
                raise ValueError(f"unrecognised dependency node kind: {kind!r}")
            return
        raise TypeError(f"unrecognised dependency node: {n!r}")

    walk(node)
    return groups


def flatten_dep_tree_with_params(node: Any) -> list[tuple[str, Optional[dict]]]:
    """Like `flatten_dep_tree`, but keeps each leaf's own argument-binding map.

    N33: `flatten_dep_tree` (and every function built on it — tool selection,
    ordering) only ever needed the leaf NAME, so the third tuple element —
    `("single", name, param_map)`'s `param_map`, e.g.
    `{"order_id": "order_id"}`, or `None`/`{}` for a tool that takes no
    arguments — was discarded at the door. That was fine until v2b, whose whole
    point is rendering that map; this is the one place it survives the walk.
    Everything else about the traversal (list/tuple shapes, `"not X"` polarity
    stripping) is identical to `flatten_dep_tree` on purpose, so the two stay
    in lockstep by construction rather than by review.
    """
    out: list[tuple[str, Optional[dict]]] = []

    def walk(n: Any) -> None:
        if n is None:
            return
        if isinstance(n, list):
            for child in n:
                walk(child)
            return
        if isinstance(n, tuple):
            kind = n[0]
            if kind == "single":
                name = n[1]
                param_map = n[2] if len(n) > 2 else None
                if name.startswith("not "):
                    name = name[len("not ") :]
                out.append((name, param_map))
            elif kind in ("and", "or", "chain"):
                for child in n[1]:
                    walk(child)
            else:
                raise ValueError(f"unrecognised dependency node kind: {kind!r}")
            return
        raise TypeError(f"unrecognised dependency node: {n!r}")

    walk(node)
    return out


def render_arg_binding(param_map: Optional[dict]) -> str:
    """One tool's argument binding, in the compiler's own words. (v2b only.)

    `None` and `{}` both mean "takes no arguments" — SOPBench's own tables
    spell the same fact both ways (`internal_get_interaction_time` uses
    `None`, `internal_get_interaction_date` uses `{}`); a renderer that only
    checked for `None` would still get the `{}` case wrong; treated alike.
    Where the tool's own argument name differs from the request-level field
    that fills it (bank's `internal_check_username_exist` called once for
    `username` and once for `destination_username`), both names are stated —
    the point of this rendering is precision, and silently picking one would
    just relocate the ambiguity N33 is about, not remove it.
    """
    if not param_map:
        return "takes no arguments"
    parts = []
    for arg_name, source_name in param_map.items():
        parts.append(arg_name if arg_name == source_name else f"{arg_name}={source_name}")
    return "arguments: " + ", ".join(parts)


def dedupe_preserve_order(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ── tool selection for a constraint's gate ───────────────────────────────────


def select_tool_for_constraint(
    name: str,
    constraint_links: dict,
    constraint_processes: dict,
    env_nodes: frozenset[str],
    exposed: set[str],
) -> tuple[Optional[str], list[str]]:
    """Pick the tool a constraint's gate should assert on, and any runner-ups.

    Priority order, exactly as the task spec states it:

      1. `constraint_links[name]` — the action that SATISFIES the constraint
         (e.g. `logged_in_user` -> `login_user`). This is a stronger claim
         than "verifies" — the linked action is the state transition itself —
         so it outranks anything `constraint_processes` offers.
      2. The first agent-reachable leaf of `constraint_processes[name]`,
         skipping anything in `env_nodes` (see the module docstring). Every
         other reachable leaf is returned too, for OR-branch bookkeeping.
      3. The constraint's own name, if that name is itself an exposed tool
         (several of bank's "verification" constraints ARE the verification
         call, e.g. `internal_check_username_exist`).
      4. `(None, [])` — no agent-callable check exists; the caller emits
         `Gate: judged`.
    """
    linked = constraint_links.get(name)
    if linked is not None:
        action_name = linked[0]
        return action_name, []

    leaves = flatten_dep_tree(constraint_processes.get(name))
    reachable = [leaf for leaf in leaves if leaf not in env_nodes and leaf in exposed]
    if reachable:
        return reachable[0], reachable[1:]

    if name in exposed:
        return name, []

    return None, []


# ── prerequisite ordering (v2) ───────────────────────────────────────────────


class Tables:
    """The four domain tables the compiler reads, carried together.

    Passed around rather than threaded as eight positional arguments, because
    the v2 ordering pass needs all of them at once and a mis-ordered call site
    is exactly the kind of silent defect this file exists to avoid.
    """

    __slots__ = ("required", "customizable", "links", "processes", "env_nodes", "exposed")

    def __init__(
        self,
        required: dict,
        customizable: dict,
        links: dict,
        processes: dict,
        env_nodes: frozenset[str],
        exposed: set[str],
    ) -> None:
        self.required = required
        self.customizable = customizable
        self.links = links
        self.processes = processes
        self.env_nodes = env_nodes
        self.exposed = exposed

    def tool_for(self, constraint: str) -> Optional[str]:
        return select_tool_for_constraint(
            constraint, self.links, self.processes, self.env_nodes, self.exposed
        )[0]

    def verification_branches(self, constraint: str) -> list[list[str]]:
        """The agent-reachable ways to verify `constraint`, with AND/OR intact.

        ⚠️ v1 flattened this and got one of them backwards.
        `constraint_processes["pay_loan_account_balance_restr"]` is
        `or(and(get_account_balance, get_account_owed_balance), internal_get_database)`
        — the first branch needs BOTH calls. v1's flattening produced "first
        reachable leaf is the gate, the rest are alternatives", so the document
        said the owed-balance call was an *alternative* to the balance call when
        the domain requires both, and `dirgraph_satisfied` (which walks the same
        or/and tree) fails a run that took v1 at its word.

        Returns one list per surviving OR branch, each list being the calls that
        branch requires. Branches containing an environment-verified or
        unexposed tool are dropped: those cannot be taken by any agent.
        """
        node = self.processes.get(constraint)
        if node is None:
            return []

        def branches(n: Any) -> list[list[str]]:
            if n is None:
                return [[]]
            if isinstance(n, list):
                out = [[]]
                for child in n:
                    out = [a + b for a in out for b in branches(child)]
                return out
            kind = n[0]
            if kind == "single":
                name = n[1]
                return [[name[len("not ") :] if name.startswith("not ") else name]]
            if kind in ("and", "chain"):
                out = [[]]
                for child in n[1]:
                    out = [a + b for a in out for b in branches(child)]
                return out
            if kind == "or":
                out: list[list[str]] = []
                for child in n[1]:
                    out.extend(branches(child))
                return out
            raise ValueError(f"unrecognised dependency node kind: {kind!r}")

        reachable = []
        for branch in branches(node):
            tools = dedupe_preserve_order(branch)
            if not tools or any(t in self.env_nodes or t not in self.exposed for t in tools):
                continue
            reachable.append(tools)
        return reachable

    def leaf_param_lookup(self, constraint: str) -> dict[str, Optional[dict]]:
        """Argument bindings for every agent-reachable leaf of `constraint`'s
        verification tree, keyed by tool name. (v2b only.)

        Deliberately walks the same `constraint_processes[constraint]` tree
        `select_tool_for_constraint`/`tool_for` read for tool SELECTION —
        `flatten_dep_tree_with_params` in place of `flatten_dep_tree` — and
        applies the identical reachability filter (drop env-verified or
        unexposed leaves), so a tool this lookup cannot answer for is a tool
        `tool_for`/`verification_branches` could not have picked either.
        First occurrence per name wins, same tie-break `dedupe_preserve_order`
        uses everywhere else in this file.
        """
        lookup: dict[str, Optional[dict]] = {}
        for name, param_map in flatten_dep_tree_with_params(self.processes.get(constraint)):
            if name in self.env_nodes or name not in self.exposed:
                continue
            lookup.setdefault(name, param_map)
        return lookup

    def direct_prereq_tools(self, tool: str) -> list[str]:
        """Tools that must already have run before `tool` may be called.

        Read off `tool`'s OWN entry in the two action-dependency tables, which is
        the same graph `env/evaluator.py` walks when it decides
        `dirgraph_satisfied`. A tool absent from those tables (an internal
        checker) has no prerequisites.
        """
        constraints = dedupe_preserve_order(
            flatten_dep_tree(self.required.get(tool)) + flatten_dep_tree(self.customizable.get(tool))
        )
        out = []
        for c in constraints:
            t = self.tool_for(c)
            if t and t != tool:
                out.append(t)
        return out

    def prereq_closure(self, tool: str) -> set[str]:
        """Transitive prerequisite tools, so an indirect edge cannot be missed.

        `get_credit_cards` depends on `authenticated_admin_password`, which
        depends on `logged_in_user`. A sort that only saw direct edges could
        place `login_user` after `get_credit_cards` whenever the admin step is
        absent from the procedure.
        """
        seen: set[str] = set()
        stack = list(self.direct_prereq_tools(tool))
        while stack:
            t = stack.pop()
            if t in seen:
                continue
            seen.add(t)
            stack.extend(self.direct_prereq_tools(t))
        return seen


def topological_step_order(
    groups: list[tuple[str, list[str]]], tables: Tables
) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Order a procedure's constraint groups so no tool runs before its prerequisites.

    Stable: among the groups whose prerequisites are already satisfied, the one
    that came first in the tables' own order is emitted first, so v2 differs
    from v1 only where the graph actually forces it.

    Returns the ordered groups and a list of prerequisites that could NOT be
    satisfied by reordering — a tool whose prerequisite constraint the procedure
    does not carry at all (`set_admin_password` requires
    `authenticate_admin_password`, which requires a login step the procedure's
    own constraint set does not include). Those are reported rather than fixed:
    inserting a step the task's constraint set never imposed is exactly N24's
    defect, and it would be a second variable inside this one.
    """
    pending = list(groups)
    tool_of_group = [tables.tool_for(g[1][0]) for g in pending]
    all_tools = {t for t in tool_of_group if t}

    ordered: list[tuple[str, list[str]]] = []
    emitted: set[str] = set()
    unsatisfiable: list[str] = []
    while pending:
        for i, group in enumerate(pending):
            tool = tool_of_group[i]
            if tool is None:
                break  # a judged step depends on nothing runnable
            needed = tables.prereq_closure(tool) & all_tools
            if needed <= emitted:
                break
        else:  # pragma: no cover - a cycle; bank has none, but do not hang
            i = 0
        group = pending.pop(i)
        tool = tool_of_group.pop(i)
        if tool:
            for need in tables.prereq_closure(tool):
                if need not in emitted and need != tool and need not in all_tools:
                    unsatisfiable.append(f"{tool} needs {need}, absent from this procedure")
            emitted.add(tool)
        ordered.append(group)
    return ordered, unsatisfiable


# ── bank-specific prose lookups ──────────────────────────────────────────────
#
# Hand-written, not a string transform, per the task spec: bank's constraint
# names are English-ish ("sufficient_account_balance") but not reliably so
# ("pay_loan_account_balance_restr", "restr" being SOPBench's own
# abbreviation) — a mechanical `.replace("_", " ")` would ship "restr"
# verbatim into the document a model reads every turn. Every key here is one
# of the 21 entries in `positive_constraint_descriptions`; the compiler
# asserts that coverage at startup rather than silently `KeyError`-ing deep in
# a loop.

_READABLE: dict[str, str] = {
    "logged_in_user": "user login status",
    "authenticated_admin_password": "admin password authentication status",
    "sufficient_account_balance": "account balance sufficiency",
    "get_loan_owed_balance_restr": "loan eligibility owed-balance limit",
    "pay_loan_account_balance_restr": "account-balance sufficiency for loan payoff",
    "pay_loan_amount_restr": "account-balance sufficiency for the requested loan payment",
    "amount_positive_restr": "requested amount positivity",
    "minimal_elgibile_credit_score": "minimum eligible credit score",
    "no_owed_balance": "presence of outstanding owed balance",
    "no_credit_card_balance": "presence of outstanding credit card balance",
    "no_credit_card_balance_on_card": "presence of outstanding balance on the specified card",
    "safety_box_eligible": "safety box eligibility",
    "maximum_deposit_limit": "deposit amount against the maximum deposit limit",
    "maximum_exchange_amount": "exchange amount against the maximum exchange limit",
    "not_over_credit_limit": "requested amount against available credit",
    "login_user": "login credential match",
    "authenticate_admin_password": "admin password credential match",
    "internal_check_username_exist": "username existence",
    "internal_check_foreign_currency_available": "foreign currency availability",
    "internal_check_credit_card_exist": "credit card existence",
    "call_get_database": "bank database state",
}

_RESTATEMENT: dict[str, str] = {
    "logged_in_user": "whether the user has previously logged in with correct credentials",
    "authenticated_admin_password": "whether the user has previously authenticated the admin password on this account",
    "sufficient_account_balance": "the relationship between the account balance and the requested amount",
    "get_loan_owed_balance_restr": "the relationship between the user's owed balance and the maximum owed balance allowed for a new loan",
    "pay_loan_account_balance_restr": "the relationship between the user's account balance and their owed balance",
    "pay_loan_amount_restr": "the relationship between the user's account balance and the requested loan payment amount",
    "amount_positive_restr": "whether the requested amount is positive",
    "minimal_elgibile_credit_score": "the relationship between the user's credit score and the minimum eligible credit score",
    "no_owed_balance": "whether the user has any outstanding owed balance",
    "no_credit_card_balance": "whether the user has any outstanding balance on any credit card",
    "no_credit_card_balance_on_card": "whether the user has an outstanding balance on the specified credit card",
    "safety_box_eligible": "the relationship between the user's account balance and the minimum balance required for safety box eligibility",
    "maximum_deposit_limit": "the relationship between the requested deposit amount and the maximum deposit limit",
    "maximum_exchange_amount": "the relationship between the requested exchange amount and the maximum exchange amount",
    "not_over_credit_limit": "the relationship between the requested amount and the available credit on the specified card",
    "login_user": "whether the provided credentials match the database records for this username",
    "authenticate_admin_password": "whether the provided admin password matches the database record for this username",
    "internal_check_username_exist": "whether the username exists in the database of accounts",
    "internal_check_foreign_currency_available": "whether the requested foreign currency type is available at this bank",
    "internal_check_credit_card_exist": "whether the specified credit card exists in the database",
    "call_get_database": "the state of the bank database as a whole",
}


def _readable_for(name: str) -> str:
    """A condition's readable name, hand-written where one exists.

    v1 raises on an unknown constraint, and stays that way — it is the archive,
    and it only ever ran on `bank`. v2 falls back to the identifier's own words,
    which is what makes it compile for a domain it has never seen. That fallback
    is not a nicety: the held-out test of this whole exercise is running the
    final document on domains it was never iterated against, and a compiler that
    needs a hand-written table per domain cannot be held out at all.

    The prose it loses is small, because v2 no longer restates what a condition
    must resolve to — the host's own operating rules carry that, and the
    preamble says so once. The step needs to name the condition, not explain it.
    """
    if name in _READABLE:
        return _READABLE[name]
    # `restr` is SOPBench's own abbreviation and appears as a whole word
    # (`pay_loan_amount_restr`). ⚠️ A substring replace turns `restricted` into
    # `restrictionicted`, which is what the first version of this line did and
    # what the first compile of `library` printed — caught by reading the
    # emitted document rather than the code that emits it.
    return re.sub(r"\brestr\b", "restriction", name.replace("_", " ")).strip()


def _title(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("_"))


_PUNCT_RE = re.compile(r"[|,;]")


def _short_phrase(description: str, max_words: int = 6) -> str:
    """A safe-to-embed routing phrase from an action's own description.

    Stripped of the characters the routing table and its own parser use as
    delimiters (`|`, `,`, `;`, and the literal word " or ") so a description's
    prose can never be mis-split when `_parse_routing` reads it back.
    """
    first_sentence = description.split(".")[0]
    first_sentence = _PUNCT_RE.sub(" ", first_sentence)
    words = first_sentence.strip().lower().split()
    phrase = " ".join(words[:max_words]).replace(" or ", " ")
    return phrase.strip()


def _routing_phrases(action_name: str, description: str) -> list[str]:
    phrases = [action_name.replace("_", " ")]
    short = _short_phrase(description)
    if short and short != phrases[0] and len(short) >= 3:
        phrases.append(short)
    return [p for p in phrases if len(p) >= 3]


# ── step planning ────────────────────────────────────────────────────────────


class PlannedStep:
    """One step's worth of both the markdown line and its expected-parse shape.

    Keeping the line and its own ground truth in the same object is what makes
    the self-check below meaningful rather than decorative: the assertions
    compare the parser's reading of the emitted markdown against what THIS
    object intended, not against a second re-derivation of the same logic.
    """

    __slots__ = ("number", "line", "deterministic", "tool")

    def __init__(self, number: int, line: str, deterministic: bool, tool: Optional[str]):
        self.number = number
        self.line = line
        self.deterministic = deterministic
        self.tool = tool


def _constraint_step(
    number: int,
    constraint_name: str,
    tool: Optional[str],
    alt_tools: list[str],
) -> PlannedStep:
    readable = _READABLE[constraint_name]
    restatement = _RESTATEMENT[constraint_name]
    base = (
        f"**Verify the {readable} condition.** {restatement}. "
        "The rules above state what this condition must be for this request; "
        "establish the actual value and confirm it against them before proceeding."
    )
    if tool is None:
        line = (
            f"{number}. {base} This condition has no agent-callable verification "
            "action in this domain. Gate: judged"
        )
        return PlannedStep(number, line, deterministic=False, tool=None)

    alt_clause = ""
    if alt_tools:
        # Plain words, no backticks — see the OR-branch bound in the module
        # docstring. named_tool() must find exactly one tool name, and it
        # must be the one in the Gate clause below.
        alt_clause = f" This can alternatively be confirmed via {' or '.join(alt_tools)}."
    line = f"{number}. {base}{alt_clause} Gate: deterministic (tool call: `{tool}`)"
    return PlannedStep(number, line, deterministic=True, tool=tool)


def _constraint_step_v2(
    number: int,
    group: tuple[str, list[str]],
    tables: Tables,
    render_args: bool = False,
) -> tuple[PlannedStep, list[str]]:
    """One v2 step: an ESTABLISH, a VERIFY, or an ANY-ONE group of either.

    The step says three things and stops: which condition, whether checking it
    reads state or changes it, and which tool answers it. What the condition
    must RESOLVE to is not repeated here — it is in the domain rules the arm is
    given, and the preamble says so once. That is the whole of the
    boilerplate-collapse change; a step that restates "the rules above say what
    this must be" sixty times is text a reader learns to skip.

    `render_args` (v2b, N33): also state each mentioned tool's argument
    binding in parentheses, read off the same dependency-table entry this
    function already parses for tool selection and AND/OR structure. v1/v2
    read that entry for step order and threw the argument map away when
    rendering — invisible on `bank`, where an AND branch's tools happen to
    share one signature, and worth up to +0.140 success on `online_market`,
    where 36 tasks call the zero-argument `internal_get_interaction_time`
    with one. `render_args=False` must reproduce v2's bytes exactly — every
    call this file makes with it unset relies on that, and a test pins it.
    """
    kind, names = group
    primary = names[0]
    tool = tables.tool_for(primary)
    branches = [] if primary in tables.links else tables.verification_branches(primary)
    # The branch the gate asserts on is the one starting with the gate's tool.
    chosen = next((b for b in branches if b and b[0] == tool), branches[0] if branches else [])
    also = [t for t in chosen[1:]] if chosen else []
    alts = [b[0] for b in branches if b and b[0] != tool]

    readable = _readable_for(primary)
    establishes = primary in tables.links

    if kind == "or" and len(names) > 1:
        heading = " or ".join(_readable_for(n) for n in names)
        ids = ", ".join(names)
        head = f"**ANY ONE of: {heading}.** Conditions `{ids}` — only one of them need hold."
    else:
        head = f"**{readable}.** Condition `{primary}`."

    if tool is None:
        line = (
            f"{number}. {head} No tool in this domain can check it; read it off "
            "the rules and the request. Gate: judged"
        )
        return PlannedStep(number, line, deterministic=False, tool=None), []

    if render_args:
        param_lookup = (
            {tool: tables.links[primary][1]} if establishes else tables.leaf_param_lookup(primary)
        )

        def _paren(name: str) -> str:
            return f" ({render_arg_binding(param_lookup.get(name))})"
    else:

        def _paren(name: str) -> str:  # noqa: ARG001 - shape matches the render_args branch
            return ""

    if establishes:
        verb = f"ESTABLISH: call the {tool} tool{_paren(tool)}, which puts the account into this state."
    else:
        verb = (
            f"VERIFY: call the {tool} tool{_paren(tool)} and read the value it returns. "
            "This reads state; it does not change it."
        )
        if also:
            # An AND branch. Saying "alternatively" here is what v1 said, and it
            # is wrong: the domain — and the scorer walking the same tree —
            # require every call in the branch.
            mentions = " and ".join(f"{t}{_paren(t)}" for t in [tool] + also)
            verb = (
                f"VERIFY: this condition needs {mentions} — call them "
                "all and read the values they return. These read state; they do not change it."
            )
    alt_clause = f" It can alternatively be established from {' or '.join(alts)}." if alts else ""
    line = f"{number}. {head} {verb}{alt_clause} Gate: deterministic (tool call: `{tool}`)"
    return PlannedStep(number, line, deterministic=True, tool=tool), (
        [tool] + alts + also if (alts or also) else []
    )


def _action_step_v2(number: int, action_name: str, procedure_title: str) -> PlannedStep:
    line = (
        f"{number}. **{procedure_title} — the requested action.** ACT: every condition above "
        f"must carry a SATISFIED verdict first. Then call the {action_name} tool. "
        f"Gate: deterministic (tool call: `{action_name}`)"
    )
    return PlannedStep(number, line, deterministic=True, tool=action_name)


def _action_step(number: int, action_name: str, procedure_title: str) -> PlannedStep:
    line = (
        f"{number}. **Complete the {procedure_title} action.** Call the {action_name} "
        "tool now that its prerequisites above have been verified. "
        f"Gate: deterministic (tool call: `{action_name}`)"
    )
    return PlannedStep(number, line, deterministic=True, tool=action_name)


class PlannedProcedure:
    __slots__ = ("title", "action_name", "steps", "composition")

    def __init__(
        self,
        title: str,
        action_name: str,
        steps: list[PlannedStep],
        composition: str = "",
    ):
        self.title = title
        self.action_name = action_name
        self.steps = steps
        # v2 only: the ALL/ANY-ONE line printed above the steps. Empty in v1,
        # which had no composition to state.
        self.composition = composition


def plan_procedure_v2(
    action_name: str,
    tables: Tables,
    tool_stats: dict[str, list[str]],
    judged_reasons: dict[str, str],
    order_notes: dict[str, list[str]],
    render_args: bool = False,
) -> PlannedProcedure:
    title = _title(action_name)
    groups = flatten_dep_groups(tables.required.get(action_name)) + flatten_dep_groups(
        tables.customizable.get(action_name)
    )
    # Dedupe on the group's constraint set, preserving order: `required` and
    # `customizable` overlap for several bank actions.
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[str, list[str]]] = []
    for g in groups:
        key = tuple(g[1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(g)

    ordered, unsatisfiable = topological_step_order(unique, tables)
    if unsatisfiable:
        order_notes[action_name] = unsatisfiable

    steps: list[PlannedStep] = []
    for i, group in enumerate(ordered, start=1):
        step, alts = _constraint_step_v2(i, group, tables, render_args=render_args)
        if step.tool is None:
            leaves = flatten_dep_tree(tables.processes.get(group[1][0]))
            judged_reasons[group[1][0]] = (
                "only reachable branch is environment-verified" if leaves else "no constraint_processes entry"
            )
        elif alts:
            tool_stats[group[1][0]] = alts
        steps.append(step)
    steps.append(_action_step_v2(len(steps) + 1, action_name, title))

    if not ordered:
        composition = "This procedure has no preconditions."
    elif len(ordered) == 1 and ordered[0][0] != "or":
        composition = "One condition must hold before the final action."
    else:
        composition = (
            f"ALL {len(ordered)} conditions below must hold before the final action. "
            "Work them in the order given — it is the order the domain's own action graph "
            "allows, and a later step's tool may depend on an earlier step's having run."
        )
    return PlannedProcedure(
        title=title, action_name=action_name, steps=steps, composition=composition
    )


def plan_procedure(
    action_name: str,
    action_required_dependencies: dict,
    action_customizable_dependencies: dict,
    constraint_links: dict,
    constraint_processes: dict,
    env_nodes: frozenset[str],
    exposed: set[str],
    tool_stats: dict[str, list[str]],
    judged_reasons: dict[str, str],
    reorder_with: Optional["Tables"] = None,
) -> PlannedProcedure:
    """v1's steps, byte-for-byte.

    `reorder_with` is the `v1-order` variant and the ONLY thing it changes is
    the ORDER of the constraint steps — every step's text, gate and tool are
    v1's. It exists because v2 moves two things at once (the order AND the
    presentation), so a v2 gain could not be attributed. This isolates the
    order, which §0 of ASOP-V2-ITERATION.md says accounts for 38 of 44 dirgraph
    failures. If this one variant recovers most of v2's gain, then five
    configurations of gate-architecture work were downstream of a compiler bug.
    """
    title = _title(action_name)
    required = flatten_dep_tree(action_required_dependencies.get(action_name))
    customizable = flatten_dep_tree(action_customizable_dependencies.get(action_name))
    constraints = dedupe_preserve_order(required + customizable)
    if reorder_with is not None:
        groups = [("single", [c]) for c in constraints]
        ordered, _unsat = topological_step_order(groups, reorder_with)
        constraints = [g[1][0] for g in ordered]

    steps: list[PlannedStep] = []
    n = 1
    for constraint_name in constraints:
        if constraint_name not in _READABLE:
            raise KeyError(
                f"constraint {constraint_name!r} (from {action_name}'s dependency tree) "
                "has no entry in _READABLE/_RESTATEMENT — bank's tables grew a constraint "
                "this compiler does not know how to phrase. Add it before compiling."
            )
        tool, alt_tools = select_tool_for_constraint(
            constraint_name, constraint_links, constraint_processes, env_nodes, exposed
        )
        if tool is None:
            leaves = flatten_dep_tree(constraint_processes.get(constraint_name))
            judged_reasons[constraint_name] = (
                "only reachable branch is environment-verified"
                if leaves
                else "no constraint_processes entry"
            )
        elif alt_tools:
            tool_stats[constraint_name] = [tool] + alt_tools
        steps.append(_constraint_step(n, constraint_name, tool, alt_tools))
        n += 1

    steps.append(_action_step(n, action_name, title))
    return PlannedProcedure(title=title, action_name=action_name, steps=steps)


# ── document assembly ────────────────────────────────────────────────────────

PREAMBLE = (
    "This document is a compiled Agentic Standard Operating Procedure (ASOP) for the "
    "SOPBench `bank` domain, produced deterministically from the domain's own dependency "
    "tables (`action_required_dependencies`, `action_customizable_dependencies`, "
    "`constraint_links`, `constraint_processes`) — no model authored any step or gate. "
    "Each procedure below corresponds to one agent-reachable bank action. Each numbered "
    "step names one precondition to verify, plus how to verify it, and the final step "
    "performs the action itself. The specific value each condition must resolve to is "
    "supplied separately per request; this document only names what to check."
)


# ⚠️ THE PREAMBLE IS INJECTED INTO EVERY STEP PROMPT (`ASOPEngine.system_prompt_for`),
# so length here is a per-turn cost and the text has to earn it. What it buys:
# the working discipline is stated ONCE instead of sixty times, and it asks for
# the thing v1 never asked for — a verdict the executor has to commit to, in its
# own words, citing the value it saw. That is the one mechanism PVA's scaffold
# has that this document did not, and the gate has nothing to audit without it.
PREAMBLE_V2 = """\
This document is a compiled Agentic Standard Operating Procedure (ASOP) for the SOPBench \
`{domain}` domain, produced deterministically from the domain's own dependency tables \
(`action_required_dependencies`, `action_customizable_dependencies`, `constraint_links`, \
`constraint_processes`) — no model authored any step or gate.

HOW TO WORK A PROCEDURE. This applies to every step below and is not repeated on them:

1. Work the steps in the order given. That order is not cosmetic: it is an order the \
domain's action graph permits, and a later step's tool can require an earlier step's tool \
to have run first.
2. A step marked ESTABLISH performs an action that puts the account into the required \
state. A step marked VERIFY only reads state — call the tool it names and read the value \
that comes back. Never assert a condition you have not called a tool for.
3. After the tool result arrives, state one line, in your own words, before going on:
       VERDICT <condition>: SATISFIED - <the value you observed>
   or  VERDICT <condition>: NOT SATISFIED - <the value you observed>
   Cite the actual value the tool returned. A verdict with no value in it is not a verdict.
4. What each condition must RESOLVE to for this particular request — every threshold, every \
must/must-not — is in the operating rules you were given. Read the value you observed \
against them. Do not assume a direction.
5. Take the procedure's final action only once every condition above it carries a SATISFIED \
verdict. If any condition is NOT SATISFIED, do not call the action: say which condition \
failed and why, and stop.
"""


# v2b only (N33): appended after PREAMBLE_V2, never formatted on its own — the
# notation it explains only appears when `--variant v2b` renders argument
# bindings. Its own numbered item continues PREAMBLE_V2's list rather than
# starting a second one, since the engine injects the two as one block.
PREAMBLE_V2B_SUFFIX = """\
6. Where a step names a tool followed by "(arguments: ...)", call it with exactly those \
values, read from the request — the names given are the tool's own argument names, not \
literals to type in. "(takes no arguments)" means call it with none. This is stated because a \
tool named in an AND group ("this condition needs X and Y — call them all") does not \
necessarily take the same arguments as the tool next to it.
"""


def build_document(
    action_names: list[str],
    action_descriptions: dict,
    action_required_dependencies: dict,
    action_customizable_dependencies: dict,
    constraint_links: dict,
    constraint_processes: dict,
    env_nodes: frozenset[str],
    exposed: set[str],
    variant: str = "v1",
    domain: str = "bank",
) -> tuple[str, list[PlannedProcedure], dict[str, list[str]], dict[str, str], dict[str, list[str]]]:
    tool_stats: dict[str, list[str]] = {}
    judged_reasons: dict[str, str] = {}
    order_notes: dict[str, list[str]] = {}
    if variant == "v1-order":
        tables = Tables(
            required=action_required_dependencies,
            customizable=action_customizable_dependencies,
            links=constraint_links,
            processes=constraint_processes,
            env_nodes=env_nodes,
            exposed=exposed,
        )
        procedures = [
            plan_procedure(
                name,
                action_required_dependencies,
                action_customizable_dependencies,
                constraint_links,
                constraint_processes,
                env_nodes,
                exposed,
                tool_stats,
                judged_reasons,
                reorder_with=tables,
            )
            for name in action_names
        ]
        preamble = PREAMBLE
    elif variant in ("v2", "v2b"):
        tables = Tables(
            required=action_required_dependencies,
            customizable=action_customizable_dependencies,
            links=constraint_links,
            processes=constraint_processes,
            env_nodes=env_nodes,
            exposed=exposed,
        )
        render_args = variant == "v2b"
        procedures = [
            plan_procedure_v2(
                name, tables, tool_stats, judged_reasons, order_notes, render_args=render_args
            )
            for name in action_names
        ]
        preamble = PREAMBLE_V2.format(domain=domain)
        if render_args:
            preamble += PREAMBLE_V2B_SUFFIX
    else:
        procedures = [
            plan_procedure(
                name,
                action_required_dependencies,
                action_customizable_dependencies,
                constraint_links,
                constraint_processes,
                env_nodes,
                exposed,
                tool_stats,
                judged_reasons,
            )
            for name in action_names
        ]
        preamble = PREAMBLE

    lines = [preamble, "", "## Routing", "", "| The user wants to... | Procedure |", "| --- | --- |"]
    for proc in procedures:
        phrases = _routing_phrases(proc.action_name, action_descriptions[proc.action_name])
        lines.append(f"| {'; '.join(phrases)} | {proc.title} |")
    lines.append("")

    for proc in procedures:
        lines.append(f"## Procedure: {proc.title}")
        lines.append("")
        if proc.composition:
            lines.append(proc.composition)
            lines.append("")
        for step in proc.steps:
            lines.append(step.line)
        lines.append("")

    return "\n".join(lines), procedures, tool_stats, judged_reasons, order_notes


# ── SOPBench wiring ──────────────────────────────────────────────────────────


def _setup_sopbench(sopbench: Path, script_dir: Path) -> None:
    """Make `env.*` and `sopbench_extract` importable, mirroring `extract()`'s
    own setup (sys.path insert, then chdir) — needed because `create_assistant`
    reads domain resources relative to SOPBench's own root. This function does
    NOT call `sopbench_extract.extract()` itself (that also replays released
    trajectories, which this compiler has no use for and which would cost time
    for nothing); it only reproduces the two setup lines that make the import
    succeed.
    """
    sys.path.insert(0, str(script_dir))
    sys.path.insert(0, str(sopbench))
    os.chdir(sopbench)


def compile_bank(
    sopbench: Path, script_dir: Path, variant: str = "v1", domain_name: str = "bank"
) -> tuple[str, list[PlannedProcedure], dict, dict, frozenset, dict]:
    """Compile one domain's tables into an ASOP document.

    Still named for `bank` because v1 only ever ran there and the name is in the
    published reproduction commands. `domain_name` is v2's doing: the held-out
    test this whole exercise turns on is running the final document on domains
    it was never iterated against, and that is impossible if the compiler can
    only see the domain it was tuned on.
    """
    from sopbench_extract import environment_verified_nodes  # local, script_dir

    _setup_sopbench(sopbench, script_dir)

    from env.task import create_assistant
    from env.variables import domain_assistant_keys

    domain = domain_assistant_keys[domain_name]
    action_descriptions = dict(domain.action_descriptions)

    assistant = create_assistant(domain_name, False, "prompt", None)
    exposed = {t["function"]["name"] for t in assistant["tools"]}
    env_nodes = environment_verified_nodes(action_descriptions.keys(), exposed)

    expected_env_nodes = frozenset({"internal_get_database"})
    if env_nodes != expected_env_nodes:
        if domain_name == "bank":
            raise SystemExit(
                f"env_nodes changed under us: got {sorted(env_nodes)}, expected "
                f"{sorted(expected_env_nodes)}. The tool-selection priority order in this "
                "compiler was written against the latter — re-verify before trusting output."
            )
        # A new domain is allowed a different withheld set — that is what the
        # predicate is for — but it is PRINTED, because a domain that withholds
        # more than `internal_get_database` will emit more `Gate: judged` steps,
        # and a held-out result read without knowing that is unreadable.
        print(
            f"[{domain_name}] environment-verified nodes: {sorted(env_nodes)} "
            f"(bank's is {sorted(expected_env_nodes)}) — every constraint whose only "
            "reachable branch is one of these compiles to `Gate: judged`.",
            file=sys.stderr,
        )

    action_names = [
        name
        for name in action_descriptions
        if name in exposed and not name.startswith("internal_") and name != "logout_user"
    ]

    markdown, procedures, tool_stats, judged_reasons, order_notes = build_document(
        action_names,
        action_descriptions,
        dict(domain.action_required_dependencies),
        dict(domain.action_customizable_dependencies),
        dict(domain.constraint_links),
        dict(domain.constraint_processes),
        env_nodes,
        exposed,
        variant=variant,
        domain=domain_name,
    )
    return markdown, procedures, tool_stats, judged_reasons, env_nodes, order_notes


# ── self-check ───────────────────────────────────────────────────────────────


def self_check(
    markdown: str, procedures: list[PlannedProcedure], tables: Optional[Tables] = None
) -> list[str]:
    """Parse the emitted markdown back and assert it matches what was intended.

    This is the difference between "the compiler ran" and "the compiler
    produced a document `parse_asop` reads the way this file meant it to" —
    the whole point of a deterministic compiler is that the second claim is
    checkable, so it is checked, every run, before anything is written out.
    """
    try:
        from asop_engine import parse_asop, named_tool, GateKind  # type: ignore
    except ImportError:
        from asop_agent import parse_asop, named_tool, GateKind  # type: ignore

    problems: list[str] = []
    asop = parse_asop(markdown)
    parsed_names = {p.name for p in asop.procedures}

    for proc in procedures:
        if proc.title not in parsed_names:
            problems.append(f"procedure {proc.title!r} did not survive parsing")
            continue
        parsed = asop.procedure(proc.title)
        if len(parsed.steps) != len(proc.steps):
            problems.append(
                f"{proc.title!r}: emitted {len(proc.steps)} steps, parser found "
                f"{len(parsed.steps)} — a step was shredded or merged"
            )
            continue
        for intended, step in zip(proc.steps, parsed.steps):
            if step.conditional:
                problems.append(f"{proc.title!r} step {step.number} parsed as conditional")
            if intended.deterministic:
                if GateKind.DETERMINISTIC not in step.gate_kinds:
                    problems.append(
                        f"{proc.title!r} step {step.number} was meant to be deterministic, "
                        f"parsed as {[k.value for k in step.gate_kinds]}"
                    )
                found_tool = named_tool(step)
                if found_tool != intended.tool:
                    problems.append(
                        f"{proc.title!r} step {step.number}: named_tool() returned "
                        f"{found_tool!r}, expected {intended.tool!r}"
                    )
            if named_tool(step) == "internal_get_database":
                problems.append(
                    f"{proc.title!r} step {step.number} names internal_get_database as a "
                    "gate tool — the exact defect this compiler exists to avoid"
                )

    if tables is not None:
        # ⚠️ THE ASSERTION v1 DID NOT HAVE, AND THE ONE THAT MATTERS MOST.
        # `dirgraph_satisfied` is a conjunct of SOPBench's `success`, and it
        # fails the whole task if ANY tool call is made before the tools its own
        # node depends on. A document whose step order violates that graph
        # cannot score, however well it is worded. v1 violates it in 5 of 20
        # bank procedures; this check makes that a compile failure rather than a
        # result someone has to explain afterwards.
        for proc in procedures:
            called: list[str] = []
            for step in proc.steps:
                if not step.tool:
                    continue
                for need in tables.prereq_closure(step.tool):
                    if need in called or need == step.tool:
                        continue
                    if any(s.tool == need for s in proc.steps):
                        problems.append(
                            f"{proc.title!r}: {step.tool} is emitted before {need}, which "
                            "the domain's action graph requires first — dirgraph_satisfied "
                            "would fail on every task routed here"
                        )
                called.append(step.tool)

    if not asop.routing:
        problems.append("ASOP.routing is empty")
    for phrase, target in asop.routing:
        if asop.procedure(target) is None:
            problems.append(f"routing phrase {phrase!r} points at unknown procedure {target!r}")

    return problems


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sopbench", type=Path, default=DEFAULT_SOPBENCH)
    ap.add_argument("--domain", default="bank")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stats-out", type=Path, default=None)
    ap.add_argument(
        "--variant",
        default="v1",
        choices=["v1", "v1-order", "v2", "v2b"],
        help="v1 is the published document, byte-for-byte; v2 is the "
        "verify-then-gate rewrite (topological step order, ESTABLISH/VERIFY "
        "split, OR groups preserved, one stated discipline asking for a cited "
        "verdict); v2b is v2 plus rendered argument bindings per tool (N33 — "
        "fixes a zero-argument tool called with one on `online_market`). "
        "Default v1 so every published arm reproduces.",
    )
    args = ap.parse_args()

    if args.domain != "bank" and args.variant not in ("v2", "v2b"):  # noqa: SIM102
        raise SystemExit(
            f"v1 only ever ran on 'bank' — {args.domain!r} would need its own "
            "_READABLE/_RESTATEMENT entries, and v1 is the archive, not a thing to "
            "extend. Compile another domain with --variant v2 or v2b."
        )

    out_path = args.out.resolve()
    stats_path = args.stats_out.resolve() if args.stats_out else None
    script_dir = Path(__file__).resolve().parent
    sopbench = args.sopbench.resolve()

    markdown, procedures, tool_stats, judged_reasons, env_nodes, order_notes = compile_bank(
        sopbench, script_dir, variant=args.variant, domain_name=args.domain
    )

    # Rebuilt here rather than returned, so the self-check reads the tables from
    # the same place the document was compiled from and cannot drift.
    tables = None
    if args.variant in ("v2", "v2b", "v1-order"):
        from env.task import create_assistant  # noqa: PLC0415  (chdir happens in compile_bank)
        from env.variables import domain_assistant_keys  # noqa: PLC0415

        dom = domain_assistant_keys[args.domain]
        exposed = {
            t["function"]["name"]
            for t in create_assistant(args.domain, False, "prompt", None)["tools"]
        }
        tables = Tables(
            required=dict(dom.action_required_dependencies),
            customizable=dict(dom.action_customizable_dependencies),
            links=dict(dom.constraint_links),
            processes=dict(dom.constraint_processes),
            env_nodes=env_nodes,
            exposed=exposed,
        )

    n_procedures = len(procedures)
    n_steps = sum(len(p.steps) for p in procedures)
    n_deterministic = sum(1 for p in procedures for s in p.steps if s.deterministic)
    n_judged = n_steps - n_deterministic

    print(f"procedures: {n_procedures}", file=sys.stderr)
    print(f"steps: {n_steps}", file=sys.stderr)
    print(f"deterministic gates: {n_deterministic}", file=sys.stderr)
    print(f"judged gates: {n_judged}", file=sys.stderr)
    print(f"env_nodes: {sorted(env_nodes)}", file=sys.stderr)
    print(f"judged constraints and why: {judged_reasons}", file=sys.stderr)
    print(f"constraints with multiple reachable verification tools: {tool_stats}", file=sys.stderr)

    if order_notes:
        print(
            "prerequisites NOT fixable by reordering (the procedure's own constraint set "
            f"does not carry them): {order_notes}",
            file=sys.stderr,
        )

    problems = self_check(markdown, procedures, tables)
    if problems:
        print("SELF-CHECK: FAIL", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(1)
    print(f"SELF-CHECK: PASS ({n_procedures} procedures, {n_steps} steps all parsed as intended)", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown)
    print(f"written to {out_path}", file=sys.stderr)

    if stats_path:
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(
            json.dumps(
                {
                    "procedures": n_procedures,
                    "steps": n_steps,
                    "deterministic_gates": n_deterministic,
                    "judged_gates": n_judged,
                    "env_nodes": sorted(env_nodes),
                    "judged_constraints": judged_reasons,
                    "multi_reachable_tools": tool_stats,
                    "variant": args.variant,
                    "unsatisfiable_prerequisites": order_notes,
                },
                indent=2,
            )
        )
        print(f"stats written to {stats_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
