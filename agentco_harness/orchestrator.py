"""Orchestrator - Main loop that ties everything together."""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

from . import _lm

from pathlib import Path
from typing import Callable

from . import __version__
from .beads import (
    Beads,
    CapabilityError,
    Task,
    TaskResult,
    TaskStatus,
    TaskPriority,
    DepthLimitError,
    MAX_SUBTASKS_PER_TASK,
    full_thread,
)
from .children import (
    DEFAULT_DUE_GRACE,
    OUTAGE_EVIDENCE_WINDOW_INTERVALS,
    ChildRegistry,
    verify_child,
)
from .config import Config
from .cost import record_run
from .egress import EgressDenied, PolicyUnavailable, check_egress
from .executor import (
    COMPLETION_MARKER,
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT,
    extract_result_text,
    run_claude_task,
    run_forge_task,
    run_store_backed_task,
    run_zai_store_backed_task,
)
from .notify import notify_event
from .usage import attributed as _attributed
from .recurring import (
    Recurring,
    any_due,
    parse_duration,
    reconcile,
    supersede_resolved_rcas,
    supersede_stale_failures,
)

# ---------------------------------------------------------------------------
# Extension seams. The Harness runs beads; what ELSE a cycle knows how to do
# is registered from outside. v1 hard-wired three of these (a nightly retro
# task type, a feeds watermark advanced on completion, and a set of polled
# sources) — all personal pipelines. They plug in here now instead of being
# imported, so a hub-less, integration-less Harness still runs.
# ---------------------------------------------------------------------------

# task metadata `type` -> handler(orchestrator, task, now) -> bool. A handler
# owns the whole bead lifecycle for its type (claim, complete/fail), exactly
# as `_execute_verify_child` does for `verify_child`.
CYCLE_HANDLERS: dict[str, Callable] = {}

# hook(orchestrator, task) called after a task is recorded DONE. Must not
# raise into the cycle — each hook is isolated (see `_run_completion_hooks`).
COMPLETION_HOOKS: list[Callable] = []

# factory(config) -> list of source objects with `.name` and `.poll()`
# yielding events carrying `.source`, `.content`, `.context`, `.source_id`.
SOURCE_FACTORIES: list[Callable] = []


def register_cycle_handler(task_type: str, handler: Callable) -> None:
    CYCLE_HANDLERS[task_type] = handler


def register_completion_hook(hook: Callable) -> None:
    COMPLETION_HOOKS.append(hook)


def register_source_factory(factory: Callable) -> None:
    SOURCE_FACTORIES.append(factory)



# Agent names that dispatch through a dedicated `_execute_*_task` branch in
# _execute_cycle_task rather than through agents.get_agent(). They are as
# dispatchable as the built-in AGENTS classes even though they are absent from
# that registry — so anything reasoning about "can this bead run?" (the
# external-agent guard, doctor's queue check) must consult AGENTS *and* this
# set. Keep it in lockstep with the `task.assigned_agent == ...` branches in
# _execute_cycle_task: a name that dispatches but is missing here gets misread
# as externally-executed and its beads sit pending forever.
SPECIAL_EXECUTORS = frozenset({"planner", "claude", "zai", "forge"})


# The planner writes its decision as a JSON STRING inside TaskResult.output — the
# TaskResult envelope carries {status, output}, and `output` holds this schema.
# (The decision cannot ride as top-level TaskResult fields: from_str drops unknown
# keys, so a `decision` key there would vanish.)
_PLANNER_DECISION_SCHEMA = """\
{
  "decision": "execute_directly" | "decompose" | "route",
  "rationale": "<why this decision>",
  "sequential": true | false,                      // decompose ONLY: do the subtasks
                                                   // depend on each other in order?
                                                   // true chains them (1 blocks 2 blocks 3)
  "subtasks": [                                    // decompose ONLY
    {
      "title": "<short title>",
      "description": "<what to do>",
      "proposed_assigned_to": "<human:name | agent name | null>",
      "executor_tier": "<planner | worker | executor>",
      "acceptance_criteria": ["<check>", "..."],
      "estimate_hours": <most-likely hours, number>,
      "estimate_optimistic": <best-case hours, number | null>,
      "estimate_pessimistic": <worst-case hours, number | null>
    }
  ],
  "proposed_assigned_to": "<human:name | null>",   // route ONLY
  "proposed_agent": "<agent name | null>"          // route ONLY
}"""


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse an ISO timestamp from a heartbeat, UTC-normalised; None if unusable.

    Heartbeats are written by other processes and by older builds, so a missing
    or malformed field is a normal input here, not an error worth raising.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def infer_provider(model: str, default: str) -> str:
    """Infer the LLM provider from a model name."""
    name = model.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if name.startswith("glm"):
        return "zai"
    return default


# Per-task-class budget floors, applied when a bead carries no budget of its
# own and has no parent to inherit one from (ac-e0a43696).
#
# `metadata.task_class` was introduced as a pure annotation. It is load-bearing
# here, deliberately: it is the ONLY signal present at filing time that says
# what KIND of work a bead is, and "remember the --timeout flag" has now failed
# three times in a row on the same 600s wall.
#
# `agent` is the class the DA files against itself when it defers work it owes
# ("[Leeloo owes] ..."). That work is read-the-code, edit, run-the-suite — the
# same shape as an RCA fix bead — so it gets the same number RCA_BUDGET already
# proved is the right size. Duplicated rather than imported (orchestrator only
# reaches into `rca` lazily, inside `_fail_with_rca`); the duplication is pinned
# by test_the_agent_class_budget_matches_the_rca_budget.
#
# `ritual` is the fourth time (ac-11290723, 2026-08-21). Ritual beads are filed
# by ~/Portfolio/rituals/run.sh with a title, a description and
# `--task-class ritual` — no budget, and no parent to inherit one from, so they
# were the exact orphan shape this floor exists for and were simply never added
# to it. StandDown ran in 205s on 08-19 and 203s on 08-20; on 08-21 the weekly
# retro appended a new MANDATORY section to standdown.md at 17:02 and the 18:00
# run died on the 600s wall an hour later, losing the Telegram message and the
# retro file after ten minutes of real work. A playbook is prose an agent can
# extend at any time; nothing couples it to the wall its bead runs on, so the
# wall has to be sized for a ritual that grew rather than for the one measured
# last week. Same number as `agent` — a daily ritual reads several sources,
# writes a file and sends a message, which is not less work than a fix bead.
# run.sh also passes explicit per-ritual flags now; this is the backstop for
# when it does not (an older copy on the second machine, or a new ritual filed
# by hand).
TASK_CLASS_BUDGETS = {
    "agent": {"timeout": 1800, "max_turns": 120},
    "ritual": {"timeout": 1800, "max_turns": 120},
}


def _resolve_budget(beads: Beads, task: Task) -> tuple[int, int]:
    """Resolve (timeout, max_turns) for a dispatch, inheriting from the parent.

    A bead with its own `metadata.budget` always wins. A bead with NONE
    inherits its parent's rather than dropping to DEFAULT_TIMEOUT, because
    the only way a child bead exists is that something decomposed work into
    it — and a fix filed by a 1800s analysis is not 600s of work just
    because the CLI that filed it left the field blank.

    This is the deterministic half of the ac-f698a0c3 fix. The RCA prompt
    now tells the agent to pass --timeout/--max-turns, but a prompt is
    advice; both fix beads that died at exactly 600s (ac-83b2f89b,
    ac-7ea4b8a1) were filed by an agent doing exactly what it was told with
    the flags that did not yet exist. Inheritance holds even when the agent
    forgets, and stops at one hop — no walking the ancestry — so the budget
    a bead runs on is always readable from itself or its parent.

    Inheritance stopped one hop short of the beads that need it most. It can
    only help a bead that HAS a parent, and the beads the DA files against
    itself under the "[Leeloo owes]" rule are roots — filed with a title, a
    description and `--task-class agent`, nothing else. Two of them
    (ac-eb1d962a, ac-c32d5540) died at exactly 600s on 2026-08-18 with the same
    signature as ac-83b2f89b/ac-7ea4b8a1, and all 27 `task_class: agent` beads
    in the store carry no budget. So there is a third source, tried only after
    the first two come up empty: a floor keyed on `metadata.task_class`.

    Precedence, highest first: the bead's own budget, its parent's, its class
    default, the global default. Explicit always wins — a class floor is what
    happens when nobody chose, not an override of somebody who did.

    Module-level (not a method) so a caller with only a `Beads` handle — the
    webui's immediate-dispatch path, which has no full `Orchestrator` — can
    resolve the same budget the cycle path would, rather than defaulting
    silently to a different number.
    """
    budget = task.metadata.get("budget") or {}
    if not budget and task.parent_id:
        parent = beads.get(task.parent_id)
        if parent is not None:
            inherited = (parent.metadata or {}).get("budget") or {}
            if inherited:
                print(
                    f"[cycle] {task.id} carries no budget — inheriting "
                    f"{inherited} from parent {parent.id}"
                )
                budget = inherited
    if not budget:
        class_budget = TASK_CLASS_BUDGETS.get(task.metadata.get("task_class"))
        if class_budget:
            print(
                f"[cycle] {task.id} carries no budget and has no parent to "
                f"inherit one — applying the {task.metadata['task_class']}-class "
                f"floor {class_budget}"
            )
            budget = class_budget
    return (
        int(budget.get("timeout", DEFAULT_TIMEOUT)),
        int(budget.get("max_turns", DEFAULT_MAX_TURNS)),
    )


def _record_cost(tasks_path, task: Task, agent: str, exec_result) -> None:
    """Append this execution's telemetry to the cost ledger.

    Called on BOTH outcomes — a failed run still burned tokens, and an
    average that quietly omits failures reads better than reality. Takes
    `tasks_path` rather than a `Config` so a caller with only a `Beads`
    handle (webui's immediate-dispatch path) can record the same way the
    cycle path does.
    """
    record_run(
        tasks_path,
        task_id=task.id,
        agent=agent,
        exec_result=exec_result,
        company=task.metadata.get("company"),
        data_class=task.metadata.get("data_class"),
        task_type=task.metadata.get("type"),
        requested_model=task.metadata.get("model"),
    )


def _attribution_for(tasks_path, task: Task, lane: str, model: str | None = None):
    """The `usage.attributed(...)` block a dispatch must open before invoking a
    model.

    Every model-invoking path in `executor.py` validates attribution before it
    spawns anything, so a dispatcher that forgets this does not silently spend —
    it raises. That is the point: the meter's guarantee is only worth what its
    weakest call site is, and the weakest call site is the one nobody remembered
    to instrument. Built from the bead itself, so "what was this for" is answered
    by the same record that answers "what did it cost".
    """
    return _attributed(
        bead_id=task.id,
        lane=lane,
        tasks_path=str(tasks_path),
        company=task.metadata.get("company"),
        task_type=task.metadata.get("type"),
        data_class=task.metadata.get("data_class"),
        requested_model=model if model is not None else task.metadata.get("model"),
    )


class _ChatLeaseTaken(Exception):
    """Internal signal: another live attempt already holds this bead's chat-answer lease."""


# Comfortably above DEFAULT_TIMEOUT (600s): a live answer's own lease must
# never expire out from under it while it is still legitimately running.
_CHAT_LEASE_TTL_S = 900


def _claim_chat_lease(beads: Beads, task_id: str, now: datetime | None = None) -> Task | None:
    """CAS-claim the right to answer `task_id`'s pending chat.

    Returns the freshly-claimed task (carrying a fresh `chat_in_flight_at`)
    on success, or None when there is nothing pending to answer, or a live
    attempt already holds the lease. Runs the check-and-stamp entirely inside
    `Beads.update`'s own `precheck` — the same flock every other
    read-modify-write on this store already serializes on — so a cycle-
    triggered answer and a POST-triggered answer racing for the same bead
    can never both win. Same CAS idiom `Beads.claim()` uses for bead
    assignment (ac-9cae7593), applied here to a metadata flag instead of
    status so it can run on a human-assigned bead without touching
    `assigned_to`.

    A lease older than `_CHAT_LEASE_TTL_S` is treated as abandoned (the
    holder crashed mid-run) and reclaimed rather than blocking forever.
    """
    now = now or datetime.now(timezone.utc)

    def cas(fresh: Task) -> dict:
        if not fresh.metadata.get("chat_pending"):
            raise _ChatLeaseTaken(f"{task_id}: no pending chat to answer")
        held_at = fresh.metadata.get("chat_in_flight_at")
        if held_at:
            try:
                age = (now - datetime.fromisoformat(held_at)).total_seconds()
            except ValueError:
                age = None
            if age is not None and age < _CHAT_LEASE_TTL_S:
                raise _ChatLeaseTaken(f"{task_id}: already in flight since {held_at}")
        meta = dict(fresh.metadata)
        meta["chat_in_flight_at"] = now.isoformat()
        return {"metadata": meta}

    try:
        return beads.update(task_id, precheck=cas)
    except _ChatLeaseTaken as e:
        print(f"[chat] lease not claimed — {e}")
        return None


_CHAT_AUTHORITY_LADDER = (
    "AUTHORITY: act immediately on what the human asks in this thread — never "
    "a \"shall I?\" round-trip. Confirm first, IN YOUR REPLY, "
    "only when the action touches:\n"
    "  (a) PRODUCTION — deployed/customer-facing changes, blob/template "
    "replacement, live config, live DB writes;\n"
    "  (b) MONEY — purchases, subscriptions, billing;\n"
    "  (c) OTHER PEOPLE — outbound messages, posts, emails, calendar invites, "
    "shared-doc changes.\n"
    "Everything else — reading, analysis, local builds, tests, staging "
    "commits, filing/closing OTHER beads, writing docs — just do it, then "
    "report what you did. Silence must never look like inaction: if you did "
    "something, say so plainly.\n\n"
    "HARD INVARIANT: never change THIS bead's own status, assignment, or any "
    "of its fields yourself (no `agentco tasks update/approve/complete` on "
    "this bead's id) — it stays the human's no matter what else you do. Your "
    "reply text is appended to the thread automatically; you never write to "
    "the thread directly."
)


def _format_sop(sop: dict | None) -> str:
    if not sop:
        return ""
    lines = ["SOP:"]
    for key in ("purpose", "trigger", "inputs", "definition_of_done"):
        if sop.get(key):
            lines.append(f"  {key}: {sop[key]}")
    mistakes = sop.get("common_mistakes") or []
    if mistakes:
        lines.append("  common_mistakes:")
        lines.extend(f"    - {m}" for m in mistakes)
    return "\n".join(lines) + "\n\n"


def _format_checklist(checklist: list | None) -> str:
    if not checklist:
        return ""
    lines = ["Checklist:"]
    for item in checklist:
        mark = "x" if item.get("done") else " "
        lines.append(f"  [{mark}] {item.get('text', item)}")
    return "\n".join(lines) + "\n\n"


def _format_thread(thread: list[dict]) -> str:
    lines = []
    for m in thread:
        kind = m.get("type", "?")
        lines.append(f"- [{kind}] {m.get('text', '')}")
    return "\n".join(lines) if lines else "(no prior messages)"


def answer_pending_chat(beads: Beads, task: Task, tasks_path) -> None:
    """Answer an unanswered human comment on `task` — and nothing else.

    Shared by both dispatch paths — `webui.api_chat`'s immediate background
    task and `Orchestrator.cycle`'s safety-net sweep — so there is exactly
    ONE implementation of "what does answering a chat message do", not two
    that can drift. `_claim_chat_lease` makes the two paths racing for the
    same bead safe: only one ever proceeds past this point.

    Deliberately does NOT go through `ready()` / `_execute_cycle_task`: that
    path refuses any bead carrying `assigned_to` on purpose (a human-owned
    task must never be executed by a model), but a human commenting on their
    OWN assigned bead — the exact case this closes — is precisely such a
    bead. `_append_chat_reply` is the only write that lands the answer, and
    it touches nothing but the chat thread (see its own docstring for the
    invariant).
    """
    claimed = _claim_chat_lease(beads, task.id)
    if claimed is None:
        return
    task = claimed

    print(f"[chat] answering pending chat on {task.id}")
    try:
        data_class, route = check_egress("claude", task.metadata, supervised=False)
    except (EgressDenied, PolicyUnavailable) as e:
        print(f"[chat] reply for {task.id} skipped — egress denied: {e}")
        _append_chat_reply(
            beads,
            task,
            f"[unable to answer — egress policy denied this bead's data "
            f"classification: {e}]",
        )
        return
    where = f"{route.vendor}/{route.model}" if route else "anthropic (native)"
    print(f"[chat] egress OK: {task.id} [{data_class}] -> {where}")

    thread_text = _format_thread(full_thread(task))
    prompt = (
        f"{_CHAT_AUTHORITY_LADDER}\n\n"
        "You are answering (and, per the authority above, acting on) a "
        "human's latest message in an AgentCo task thread. Read the whole "
        "thread below — it is the full conversation, chronological, system "
        "events plus human and agent messages — then respond to the human's "
        "MOST RECENT message directly. Do not narrate what you are about to "
        "do or restate the question.\n\n"
        f"Bead: {task.id} — {task.title}\n"
        f"Status: {task.status.value}\n"
        f"Description:\n{task.description}\n\n"
        f"{_format_sop(task.metadata.get('sop'))}"
        f"{_format_checklist(task.metadata.get('checklist'))}"
        f"Thread so far:\n{thread_text}\n\n"
        "Write your reply now."
    )
    timeout, max_turns = _resolve_budget(beads, task)
    with _attribution_for(tasks_path, task, "chat"):
        exec_result = run_claude_task(prompt, timeout=timeout, max_turns=max_turns)
    _record_cost(tasks_path, task, "claude", exec_result)
    if not exec_result.success:
        print(f"[chat] reply for {task.id} FAILED: {exec_result.error}")
        reply = f"[unable to answer — {exec_result.error}]"
    else:
        reply = extract_result_text(exec_result.output).strip() or "[agent returned no output]"
    _append_chat_reply(beads, task, reply)


def _append_chat_reply(beads: Beads, task: Task, text: str) -> None:
    """Append one `{"type": "agent"}` chat entry and clear the pending/lease
    flags. The ONLY write `answer_pending_chat` makes. Re-reads the bead
    first so a long-running answer doesn't clobber metadata changed while it
    was thinking; passes `metadata` alone to `Beads.update` so status and
    assigned_to are never touched, human-assigned or not.
    """
    fresh = beads.get(task.id) or task
    chat = list(fresh.metadata.get("chat", []))
    chat.append(
        {
            "type": "agent",
            "text": text[:4000],
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    meta = dict(fresh.metadata)
    meta["chat"] = chat
    meta.pop("chat_pending", None)
    meta.pop("chat_pending_at", None)
    meta.pop("chat_in_flight_at", None)
    beads.update(task.id, metadata=meta)


class Orchestrator:
    """Main orchestrator that runs the agent loop."""

    def __init__(self, config: Config):
        self.config = config
        if config.humans.escalate_to:
            from . import rca as _rca
            _rca.DEFAULT_ESCALATION_ASSIGNEE = config.humans.escalate_to
        self.beads = Beads(config.tasks_path)
        self.classifier = _lm.agents("The event classifier").Classifier(self.beads)
        self.recurring = Recurring(config.recurring_path)
        self.children = ChildRegistry(config.children_registry_path)
        self._state_path = Path(config.tasks_path).parent / ".agentco-heartbeat.json"
        self._cycle_heartbeat_path = Path(config.heartbeat_path)
        self._agent_lms: dict[str, object] = {}
        self._setup_dspy()

    @property
    def node_capabilities(self) -> list[str]:
        """This node's manifest — what an IN-PROCESS agent can do (ac-39d4dbc8).

        Every claim made by this orchestrator is a claim by THIS machine: the
        agent runs here, in this process, with this host's credentials and this
        host's repos. So the hub's own manifest is the honest claimant identity,
        and passing it is what makes the gate mean something in-process rather
        than only across the SSH hop.
        """
        return self.config.capabilities

    def _make_lm(self, provider: str, model: str):
        """Build a dspy.LM for a provider/model pair."""
        dspy = _lm.dspy("The DSPy planner")
        if provider == "openai":
            api_key = self.config.llm.api_key or os.environ.get("OPENAI_API_KEY")
            return dspy.LM(f"openai/{model}", api_key=api_key)
        elif provider == "anthropic":
            api_key = self.config.llm.api_key or os.environ.get("ANTHROPIC_API_KEY")
            return dspy.LM(f"anthropic/{model}", api_key=api_key)
        elif provider == "lmstudio":
            base_url = self.config.llm.base_url or "http://localhost:4242/v1"
            return dspy.LM(f"openai/{model}", api_base=base_url, api_key="lm-studio")
        elif provider == "zai":
            # z.ai (Zhipu AI) — OpenAI-compatible cloud API.
            # Key resolution: config.llm.zai_api_key → ZAI_API_KEY env → config.llm.api_key.
            api_key = (
                self.config.llm.zai_api_key
                or os.environ.get("ZAI_API_KEY")
                or self.config.llm.api_key
            )
            base_url = self.config.llm.base_url or "https://api.z.ai/api/paas/v4"
            return dspy.LM(f"openai/{model}", api_base=base_url, api_key=api_key)
        elif provider == "ollama":
            return dspy.LM(f"ollama_chat/{model}")
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def _setup_dspy(self):
        """Configure DSPy with the default LLM and per-agent overrides."""
        provider = self.config.llm.default_provider
        model = self.config.llm.default_model
        dspy = _lm.dspy("The DSPy planner")
        dspy.configure(lm=self._make_lm(provider, model))

        # Per-agent model overrides — applied via dspy.context at execution
        for name, agent_cfg in self.config.agents.items():
            if agent_cfg.model and agent_cfg.model != model:
                agent_provider = infer_provider(agent_cfg.model, provider)
                self._agent_lms[name] = self._make_lm(agent_provider, agent_cfg.model)

        self._load_optimized_prompts()

    def _load_optimized_prompts(self):
        """Load any available optimized DSPy programs."""
        from .signatures import ClassifyEvent

        optimize = _lm.optimize("Optimized signatures")
        optimized = optimize.list_optimized()
        if optimized:
            print(f"[orchestrator] Found optimized signatures: {optimized}")

        # Load optimized classifier if available
        opt_classify = optimize.load_optimized("classify", ClassifyEvent)
        if opt_classify:
            self.classifier.classify = opt_classify
            print("[orchestrator] Using optimized classifier")

    def _heartbeat(self, **fields) -> None:
        """Persist cycle timestamps so silence is detectable from status."""
        state = {}
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        state.update(fields)
        self._state_path.write_text(json.dumps(state, indent=2))

    def _read_heartbeat(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    # ------------------------------------------------------------------
    # Heartbeat cycle (v0.3.0): generator → triage → execute → heartbeat
    # ------------------------------------------------------------------

    def _make_triage_lm(self):
        """Build the triage LM from config. Cheap model, DSPy path only."""
        t = self.config.triage
        kwargs = {}
        if t.api_base:
            kwargs["api_base"] = t.api_base
            kwargs["api_key"] = t.api_key or "lm-studio"
        elif t.api_key:
            kwargs["api_key"] = t.api_key
        return _lm.dspy("The DSPy planner").LM(t.model, **kwargs)

    def _triage(self, ready: list[Task]) -> list[Task]:
        """Advisory triage with a loud fallback.

        If the triage LM is down or returns garbage, run everything in
        queue order with a WARNING — triage-model downtime must never
        block verify tasks or silently defer work.
        """
        if not ready:
            return []
        # Explicitly disabled (e.g. a feeds-only instance): skip quietly, run in
        # queue order. This is intentional config, not a failure — no warning.
        if not self.config.triage.model or self.config.triage.model.lower() in ("none", "off", "disabled"):
            return ready
        try:
            return _lm.triage("LM triage").triage_order(ready, self._make_triage_lm())
        except Exception as e:
            print(
                f"[cycle] WARNING: triage failed ({e}) — falling back to "
                f"running all {len(ready)} ready task(s) in queue order"
            )
            return list(ready)

    def _external_agent(self, name: str | None) -> bool:
        """True when a bead's agent is declared in config but has no in-process class.

        Such beads belong to an out-of-band worker (e.g. sommeliwhey's
        brands/box-scout ops/work_beads.py, which claims and closes its own
        beads). The cycle must leave them pending instead of claiming and
        failing them — on 2026-07-22 and 2026-07-29 the sommeliwhey cycle
        failed 41 box-scout beads with "Unknown agent: box-scout" because
        get_agent() only knows the built-in AGENTS classes. Names that are
        neither built-in, nor a special executor, nor declared in config
        still fail loudly (a typo should surface, not linger).
        """
        if name is None:
            return False
        if name in _lm.agent_names() or name in SPECIAL_EXECUTORS:
            return False
        return name in self.config.agents

    def _fail_with_rca(self, task: Task, result: str | None) -> Task | None:
        """Fail a bead and, unless it's itself an RCA bead, spawn its RCA root.

        Centralizes the failure -> RCA hook so every failure path in the
        cycle (verify_child, claude/zai/planner execution, planner-decision
        handling, and the top-level cycle()/work() exception guards) gets
        automatic root-cause analysis without each call site remembering to
        wire it up. Guards against RCA-of-RCA: a task whose own source is
        "rca" (an RCA phase bead, or the human escalation bead) never spawns
        another RCA — it just fails, same as before this hook existed.

        Idempotent per ``rca.find_existing_rca_root``: a batch of beads failing
        the same way yields ONE root, and a bead that is retried and re-fails
        reuses its original root instead of opening a second one.
        """
        failed = self.beads.fail(task.id, result=result)
        if task.source == "rca":
            return failed
        try:
            from .rca import create_rca_task, find_existing_rca_root

            error = result or "(no error captured)"
            existing = find_existing_rca_root(self.beads, task, error)
            root = create_rca_task(self.beads, task, error)
            if existing is None:
                print(f"[cycle] RCA spawned for {task.id}: {root.id} — {root.title}")
            else:
                print(
                    f"[cycle] RCA for {task.id} folded into existing root {root.id} "
                    f"— {root.title} (no duplicate spawned)"
                )
        except Exception as e:  # noqa: BLE001 — RCA creation must never crash the cycle
            print(f"[cycle] WARNING: could not create RCA for {task.id}: {e}")
        return failed

    def _execute_verify_child(self, task: Task, now: datetime | None = None) -> bool:
        """Execute a verify_child bead. Pure code path — no LLM at all."""
        child_name = task.metadata.get("child")
        child = self.children.get(child_name) if child_name else None
        if child is None:
            msg = (
                f"verify_child task names unknown child {child_name!r} — "
                f"not in {self.children.path}"
            )
            print(f"[cycle] FAIL: {msg}")
            self._fail_with_rca(task, msg)
            return False

        self.beads.claim(task.id, "verify_child", capabilities=self.node_capabilities)
        result = verify_child(
            child,
            now=now,
            parent_next_due_at=self._own_next_due_at(),
            parent_recent_outage_s=self._own_recent_outage_seconds(now),
        )
        if result["level"] == "fail":
            print(f"[cycle] verify_child FAIL: {child.name}: {result['detail']}")
            self._fail_with_rca(task, json.dumps(result))
            if child.notify and self.config.notify.enabled:
                notify_event(
                    self.config.notify,
                    f"🚨 AgentCo [{self.config.instance_name}]: child "
                    f"'{child.name}' failed verification — {result['detail']}",
                    urgent=True,
                )
            return False
        if result["level"] == "warn":
            print(f"[cycle] verify_child WARN: {child.name}: {result['detail']}")
        else:
            print(f"[cycle] verify_child OK: {child.name}: {result['detail']}")
        self.beads.complete(task.id, result=json.dumps(result))
        return True

    def _record_completion_marker(self, task: Task, exec_result) -> None:
        """Record whether the worked agent signalled completion, never fail on it.

        v1 MEASURES before it enforces. A missing `AGENTCO_DONE:` line on a
        clean exit is the signature of an agent that drifted off the end of its
        turn rather than finishing, but the bead may still be perfectly
        complete — so it is flagged (`metadata.completion_marker = "missing"`)
        and warned about, not failed. Once the flag's rate is known, gating on
        it is a one-line change.

        When the marker IS present and the bead carries no richer result, the
        one-liner becomes the result: a thin true summary beats an empty field.
        """
        refreshed = self.beads.get(task.id)
        if refreshed is None:
            return
        marker = getattr(exec_result, "completion_marker", None)
        if marker is not None:
            if marker and not (refreshed.result or "").strip():
                self.beads.update(task.id, result=marker)
            return
        print(
            f"[cycle] WARNING: {task.id} exited cleanly without a "
            f"'{COMPLETION_MARKER}' line — completion is unconfirmed "
            f"(flagged metadata.completion_marker=missing, bead NOT failed)"
        )
        metadata = dict(refreshed.metadata or {})
        metadata["completion_marker"] = "missing"
        self.beads.update(task.id, metadata=metadata)

    def _resolve_budget(self, task: Task) -> tuple[int, int]:
        """Resolve (timeout, max_turns) for a dispatch — see module-level
        `_resolve_budget` for the full contract. Thin wrapper so instance
        callers keep their existing `self._resolve_budget(task)` call shape."""
        return _resolve_budget(self.beads, task)

    def _resolve_model(self, task: Task) -> str | None:
        """Resolve the model for a claude dispatch.

        Precedence: an explicit `metadata.model` always wins (the existing
        per-task override). Otherwise a `metadata.executor_tier` resolves through
        the tier registry. An unknown tier degrades LOUDLY — a WARNING plus a
        fall back to the CLI default model — consistent with triage's advisory
        degradation rather than failing the bead.
        """
        model = task.metadata.get("model")
        if model:
            return model
        tier = task.metadata.get("executor_tier")
        if not tier:
            return None
        resolved = self.config.tiers.model_for(tier)
        if resolved is None:
            print(
                f"[cycle] WARNING: task {task.id} names unknown executor_tier {tier!r} "
                f"— falling back to the claude CLI default model "
                f"(known tiers: {', '.join(sorted(self.config.tiers.models))})"
            )
            return None
        return resolved

    def _execute_claude_task(self, task: Task) -> bool:
        """Execute a bead via a headless Claude subagent (subprocess boundary).

        Two modes selected by task.metadata["store_backed"]:
          False (default) — full prompt via stdin, result from stdout.
          True            — tiny prompt (task ID only); agent reads context
                            from AgentCo store and writes TaskResult back via
                            `agentco tasks complete`. Use for group-chat and
                            Obsidian flows where context grows large.
        """
        timeout, max_turns = self._resolve_budget(task)
        store_backed = bool(task.metadata.get("store_backed", False))
        # None → inherit the claude CLI default. An explicit metadata.model wins;
        # otherwise metadata.executor_tier resolves through the tier registry.
        model = self._resolve_model(task)

        self.beads.claim(task.id, "claude", capabilities=self.node_capabilities)
        mode = "store-backed" if store_backed else "prompt"
        print(
            f"[cycle] Executing {task.id} via claude subagent "
            f"(mode={mode}, model={model or 'cli-default'}, timeout={timeout}s, max_turns={max_turns})"
        )

        if store_backed:
            with self._attribution(task, "cycle", model):
                exec_result = run_store_backed_task(
                    task.id,
                    config_path=self.config.config_path,
                    timeout=timeout,
                    max_turns=max_turns,
                    model=model,
                )
            self._record_cost(task, 'claude', exec_result)
            if not exec_result.success:
                print(f"[cycle] FAIL: claude subagent for {task.id}: {exec_result.error}")
                self._fail_with_rca(task, exec_result.error)
                return False
            # Result lives in the store — agent wrote it via `agentco tasks complete`
            refreshed = self.beads.get(task.id)
            if refreshed is None or refreshed.status != TaskStatus.DONE:
                msg = "agent did not complete the task — result missing from store"
                print(f"[cycle] FAIL: {task.id}: {msg}")
                self._fail_with_rca(task, msg)
                return False
            self._record_completion_marker(task, exec_result)
            self._run_completion_hooks(refreshed)
            print(f"[cycle] Completed {task.id} via claude store-backed ({exec_result.duration_seconds:.0f}s)")
            return True
        else:
            prompt = task.metadata.get("prompt") or f"{task.title}\n\n{task.description}"
            with self._attribution(task, "cycle", model):
                exec_result = run_claude_task(prompt, timeout=timeout, max_turns=max_turns, model=model)
            self._record_cost(task, 'claude', exec_result)
            if not exec_result.success:
                print(f"[cycle] FAIL: claude subagent for {task.id}: {exec_result.error}")
                self._fail_with_rca(task, exec_result.error)
                return False
            self.beads.complete(task.id, result=exec_result.output)
            print(f"[cycle] Completed {task.id} via claude ({exec_result.duration_seconds:.0f}s)")
            return True

    def _execute_zai_task(self, task: Task) -> bool:
        """Execute a bead via z.ai's Coding Plan (Anthropic-compatible endpoint).

        Store-backed only — z.ai acts as a drop-in for Claude for store-backed
        beads. Set `agent: zai` (and `store_backed: true`) in a recurring def.
        """
        timeout, max_turns = self._resolve_budget(task)
        model = task.metadata.get("model")

        self.beads.claim(task.id, "zai", capabilities=self.node_capabilities)
        print(
            f"[cycle] Executing {task.id} via z.ai subagent "
            f"(model={model or 'zai-default'}, timeout={timeout}s, max_turns={max_turns})"
        )

        with self._attribution(task, "cycle", model):
            exec_result = run_zai_store_backed_task(
                task.id,
                config_path=self.config.config_path,
                timeout=timeout,
                max_turns=max_turns,
                model=model,
                zai_api_key=self.config.llm.zai_api_key,
            )
        self._record_cost(task, 'zai', exec_result)
        if not exec_result.success:
            print(f"[cycle] FAIL: z.ai subagent for {task.id}: {exec_result.error}")
            self._fail_with_rca(task, exec_result.error)
            return False
        refreshed = self.beads.get(task.id)
        if refreshed is None or refreshed.status != TaskStatus.DONE:
            msg = "z.ai agent did not complete the task — result missing from store"
            print(f"[cycle] FAIL: {task.id}: {msg}")
            self._fail_with_rca(task, msg)
            return False
        self._record_completion_marker(task, exec_result)
        self._run_completion_hooks(refreshed)
        print(f"[cycle] Completed {task.id} via z.ai ({exec_result.duration_seconds:.0f}s)")
        return True

    def _planner_prompt(self, task: Task) -> str:
        """Build the planner subagent prompt (store-backed: decision → store)."""
        config_flag = f"--config {self.config.config_path} " if self.config.config_path else ""
        return (
            f"You are the AgentCo PLANNER for task {task.id}. You ANALYZE, DECOMPOSE, and "
            f"ROUTE work — you NEVER execute the work itself.\n\n"
            f"Step 1 — read the task:\n"
            f"  agentco {config_flag}tasks show {task.id}\n\n"
            f"Step 2 — decide EXACTLY ONE of:\n"
            f"  - execute_directly: the task is a single atomic unit; recommend running it as-is.\n"
            f"  - decompose: split into at most {MAX_SUBTASKS_PER_TASK} distinct subtasks, each a real work item.\n"
            f"  - route: the task should be re-assigned as-is to a better executor (a PROPOSAL only).\n\n"
            f"Depth rule: judgment / multi-file / synthesis → a capable tier (planner or worker); "
            f"atomic yes/no, extraction, single-file edits → the executor tier.\n\n"
            f"Step 3 — write your decision back BEFORE finishing, as a TaskResult whose `output` "
            f"field is a JSON STRING carrying the decision:\n"
            f"  agentco {config_flag}tasks complete {task.id} --result '<TaskResult JSON>'\n\n"
            f'TaskResult envelope: {{"status": "complete", "output": "<DECISION JSON as a string>"}}\n\n'
            f"DECISION JSON schema (this is the string you put in `output`):\n"
            f"{_PLANNER_DECISION_SCHEMA}\n\n"
            f"Rules:\n"
            f"- Propose only. Every subtask you create lands pending_approval; a human approves it.\n"
            f"- NEVER re-assign a task assigned to a human (assigned_to starting 'human:') to an agent.\n"
            f"- decompose: give each subtask a title, description, proposed_assigned_to, executor_tier "
            f"(a configured tier), and acceptance_criteria (a checklist).\n"
            f"- decompose: ALWAYS estimate each subtask — estimate_hours (most likely), plus "
            f"estimate_optimistic and estimate_pessimistic when you can bound it. Estimates feed "
            f"deadline scheduling; a missing estimate defaults to 30 minutes, which understates "
            f"real work. Set sequential=true when the subtasks must happen in order.\n"
            f"- route: give proposed_assigned_to or proposed_agent plus a rationale.\n"
            f"- Never leave the result empty — if unsure, choose execute_directly with a rationale.\n"
        )

    def _execute_planner_task(self, task: Task) -> bool:
        """Run a planner bead through the store-backed claude path, then handle its
        decision (propose-only). The planner produces a decision; it never works.

        The bead reuses the existing store-backed executor (same env-strip, stdin
        delivery, truncation detection, bare-exit retry, missing-result loud fail)
        — the only difference is a planner-specific prompt and the tiers['planner']
        model. All loud-failure machinery therefore applies automatically.
        """
        timeout, max_turns = self._resolve_budget(task)
        # tiers['planner'] is the capable-tier default; an explicit metadata.model
        # override still wins (the existing per-task model mechanism).
        model = task.metadata.get("model") or self.config.tiers.model_for("planner")

        self.beads.claim(task.id, "planner", capabilities=self.node_capabilities)
        print(
            f"[cycle] Planning {task.id} via planner subagent "
            f"(model={model or 'cli-default'}, timeout={timeout}s, max_turns={max_turns})"
        )
        with self._attribution(task, "planner", model):
            exec_result = run_store_backed_task(
                task.id,
                config_path=self.config.config_path,
                timeout=timeout,
                max_turns=max_turns,
                model=model,
                prompt=self._planner_prompt(task),
            )
        self._record_cost(task, 'planner', exec_result)
        if not exec_result.success:
            print(f"[cycle] FAIL: planner subagent for {task.id}: {exec_result.error}")
            self._fail_with_rca(task, exec_result.error)
            return False
        refreshed = self.beads.get(task.id)
        if refreshed is None or refreshed.status != TaskStatus.DONE:
            msg = "planner did not write a decision — result missing from store"
            print(f"[cycle] FAIL: {task.id}: {msg}")
            self._fail_with_rca(task, msg)
            return False
        return self._handle_planner_decision(refreshed)

    def _handle_planner_decision(self, task: Task) -> bool:
        """Parse and act on a completed planner bead's decision (propose-only).

        A malformed or unknown decision is a planner failure — loud, flips the
        bead to FAILED, counts as a cycle error.
        """
        tr = TaskResult.from_task(task)
        if tr is None:
            msg = "planner completed but wrote no parseable TaskResult — cannot read decision"
            print(f"[cycle] FAIL: {task.id}: {msg}")
            self._fail_with_rca(task, msg)
            return False
        try:
            decision = json.loads(tr.output)
        except (json.JSONDecodeError, TypeError):
            decision = None
        if not isinstance(decision, dict) or "decision" not in decision:
            msg = f"planner decision unparseable or missing 'decision' — output was: {tr.output[:200]!r}"
            print(f"[cycle] FAIL: {task.id}: {msg}")
            self._fail_with_rca(task, msg)
            return False

        kind = decision.get("decision")
        if kind == "decompose":
            return self._planner_decompose(task, decision)
        if kind == "route":
            return self._planner_route(task, decision)
        if kind == "execute_directly":
            rationale = decision.get("rationale", "")
            hint = (
                f"planner recommends executing this task directly, as-is (no "
                f"decomposition): {rationale}"
            ).strip()
            # Surface to `me`: write a needs_input TaskResult so the DONE planner
            # bead shows up in `agentco me` (which lists DONE + needs_input).
            # Otherwise the recommendation lands on a DONE+complete bead that no
            # human ever sees — the decision silently vanishes.
            result = TaskResult(
                status="needs_input", output=hint, continuation_hint=hint
            ).to_json()
            self.beads.update(
                task.id,
                result=result,
                metadata={
                    **task.metadata,
                    "planner_recommendation": "execute_directly",
                    "planner_rationale": rationale,
                },
            )
            print(
                f"[cycle] planner: {task.id} → execute_directly "
                f"(recommendation recorded, surfaced in `me`): {rationale[:160]}"
            )
            self._notify_planner_decision(
                task,
                f"🧭 AgentCo [{self.config.instance_name}]: planner recommends "
                f"running '{task.title}' directly as-is — awaiting your call "
                f"(surfaced in `agentco me`).",
            )
            return True

        msg = f"planner returned unknown decision {kind!r} (expected execute_directly|decompose|route)"
        print(f"[cycle] FAIL: {task.id}: {msg}")
        self._fail_with_rca(task, msg)
        return False

    def _notify_planner_decision(self, task: Task, message: str) -> None:
        """Best-effort planner-decision notification — never fails the bead.

        Advisory, like every other notify in the cycle: a broken channel warns
        and is swallowed so a notification failure can't turn a recorded planner
        decision into a cycle error.
        """
        if not self.config.notify.enabled:
            return
        try:
            notify_event(self.config.notify, message, urgent=False)
        except Exception as e:  # noqa: BLE001 — advisory, never fails the cycle
            print(f"[cycle] WARNING: planner-decision notify failed for {task.id}: {e}")

    def _planner_decompose(self, task: Task, decision: dict) -> bool:
        """Create planner-proposed subtasks as pending_approval (propose-only).

        Reuses the existing agent-subtask machinery: MAX_SUBTASKS_PER_TASK cap and
        the depth cap in beads.create both apply. Assignees are recorded as
        PROPOSALS in metadata (proposed_assigned_to) — assigned_agent is never set
        from a planner proposal, so nothing human-lineaged can be flipped to an
        agent (a human applies the proposal explicitly).
        """
        proposed = decision.get("subtasks")
        if not isinstance(proposed, list) or not proposed:
            msg = "planner chose decompose but proposed no subtasks"
            print(f"[cycle] FAIL: {task.id}: {msg}")
            self._fail_with_rca(task, msg)
            return False

        capped = proposed[:MAX_SUBTASKS_PER_TASK]
        if len(proposed) > MAX_SUBTASKS_PER_TASK:
            print(
                f"[cycle] WARNING: planner {task.id} proposed {len(proposed)} subtask(s); "
                f"capping at {MAX_SUBTASKS_PER_TASK}"
            )

        # sequential=true chains siblings (1 blocks 2 blocks 3) — the OmniFocus
        # pattern: one boolean authors a whole blocked_by chain, instead of
        # asking the model to emit edge lists it would have to invent ids for.
        sequential = bool(decision.get("sequential", False))

        def _hours(raw) -> float | None:
            """Tolerant numeric parse: a malformed estimate degrades to None
            (the 30-minute default downstream), never fails the decompose."""
            if raw is None:
                return None
            try:
                val = float(raw)
            except (TypeError, ValueError):
                return None
            return val if val > 0 else None

        created = 0
        created_ids: list[str] = []
        for st in capped:
            if not isinstance(st, dict):
                print(f"[cycle] WARNING: planner {task.id} skipping non-object subtask: {st!r}")
                continue
            title = st.get("title") or st.get("description")
            if not title:
                print(f"[cycle] WARNING: planner {task.id} skipping subtask with no title/description")
                continue
            # Validate the executor_tier at CREATION, not just at dispatch: an
            # unknown tier stored on a subtask would later silently fall back UP
            # to the CLI default model. Skip it loudly here (consistent with the
            # other malformed-subtask skips); dispatch keeps its warn+fallback as
            # a backstop for tiers that go missing after creation.
            tier = st.get("executor_tier")
            if tier is not None and tier not in self.config.tiers.models:
                print(
                    f"[cycle] WARNING: planner {task.id} skipping subtask {title!r} — "
                    f"unknown executor_tier {tier!r} (known: "
                    f"{', '.join(sorted(self.config.tiers.models)) or 'none'})"
                )
                continue
            sub_meta: dict = {"requires_approval": True, "planner_parent": task.id}
            if tier is not None:
                sub_meta["executor_tier"] = tier
            if st.get("acceptance_criteria") is not None:
                sub_meta["acceptance_criteria"] = st["acceptance_criteria"]
            # Stage 2 has no assigned_to field (Stage 1 owns it) — the assignee is
            # a proposal in metadata, never a live assigned_agent.
            if st.get("proposed_assigned_to") is not None:
                sub_meta["proposed_assigned_to"] = st["proposed_assigned_to"]
            try:
                priority = TaskPriority(int(st.get("priority", 2)))
            except (ValueError, TypeError):
                priority = TaskPriority.MEDIUM
            try:
                # Born PENDING_APPROVAL on the FIRST append (create's status kwarg),
                # never PENDING-then-flipped — no window where a proposal subtask
                # is briefly dispatchable by a concurrent cycle.
                new_task = self.beads.create(
                    title=title,
                    description=st.get("description") or title,
                    parent_id=task.id,
                    priority=priority,
                    status=TaskStatus.PENDING_APPROVAL,
                    metadata=sub_meta,
                    # Chain to the previous sibling when sequential. New ids
                    # cannot close a cycle by construction.
                    blocked_by=[created_ids[-1]] if sequential and created_ids else None,
                    estimate_hours=_hours(st.get("estimate_hours")),
                    estimate_optimistic=_hours(st.get("estimate_optimistic")),
                    estimate_pessimistic=_hours(st.get("estimate_pessimistic")),
                )
            except DepthLimitError as e:
                print(f"[cycle] depth cap: skipping planner subtask of {task.id} — {e}")
                continue
            created_ids.append(new_task.id)
            print(
                f"[cycle] planner queued for approval: {new_task.id} — {new_task.title} "
                f"(tier={sub_meta.get('executor_tier', '-')}, "
                f"proposed_assigned_to={sub_meta.get('proposed_assigned_to', '-')})"
            )
            created += 1

        if created == 0:
            # decompose that yields no real subtasks is a planner failure, exactly
            # like the empty-list case above — every proposed subtask was malformed,
            # unknown-tier, or depth-capped. Fail loudly, don't report success.
            msg = (
                "planner chose decompose but created 0 subtasks "
                "(every proposed subtask was malformed, unknown-tier, or depth-capped)"
            )
            print(f"[cycle] FAIL: {task.id}: {msg}")
            self._fail_with_rca(task, msg)
            return False

        # The parent IS the deliverable: it cannot complete until its parts do,
        # so it gains its subtasks as blockers. This is what makes the parent's
        # due_at propagate — the tempo backward pass walks blocked_by, so each
        # subtask (and each link of a sequential chain) inherits its share of
        # the parent's deadline with no further wiring. parent_id alone is a
        # display edge and would leave the deadline stranded on the parent.
        try:
            existing = self.beads.get(task.id)
            merged = list(dict.fromkeys((existing.blocked_by if existing else []) + created_ids))
            self.beads.update(task.id, blocked_by=merged)
        except Exception as e:  # noqa: BLE001 — gate is best-effort, decompose already succeeded
            print(
                f"[cycle] WARNING: could not gate parent {task.id} on its "
                f"subtasks ({e}) — the parent's deadline will not propagate to them"
            )

        self._notify_planner_decision(
            task,
            f"⏳ AgentCo [{self.config.instance_name}]: planner decomposed "
            f"'{task.title}' into {created} subtask(s) awaiting approval. "
            f"Run: agentco approve --list",
        )
        print(f"[cycle] planner: {task.id} → decompose ({created} subtask(s) pending_approval)")
        return True

    def _planner_route(self, task: Task, decision: dict) -> bool:
        """Record a planner route as a PROPOSAL on the parent — never auto-applied.

        A human applies the route explicitly. assigned_agent is left untouched, so
        the human-lineage invariant holds: the planner can never flip an assignment.
        """
        proposal = {
            "proposed_assigned_to": decision.get("proposed_assigned_to"),
            "proposed_agent": decision.get("proposed_agent"),
            "rationale": decision.get("rationale", ""),
        }
        target = proposal["proposed_assigned_to"] or proposal["proposed_agent"] or "?"
        hint = (
            f"planner proposes re-routing this task to {target} (proposal only, "
            f"NOT applied — a human must apply it): {proposal['rationale']}"
        ).strip()
        # Surface to `me`: needs_input TaskResult on the DONE bead so the route
        # proposal is visible in `agentco me`, not stranded on a DONE+complete bead.
        result = TaskResult(
            status="needs_input", output=hint, continuation_hint=hint
        ).to_json()
        self.beads.update(
            task.id,
            result=result,
            metadata={**task.metadata, "proposed_route": proposal},
        )
        print(
            f"[cycle] planner: {task.id} → ROUTE PROPOSAL (NOT auto-applied) "
            f"proposed_assigned_to={proposal['proposed_assigned_to']!r} "
            f"proposed_agent={proposal['proposed_agent']!r} — a human must apply it: "
            f"{proposal['rationale'][:160]}"
        )
        self._notify_planner_decision(
            task,
            f"🧭 AgentCo [{self.config.instance_name}]: planner proposes re-routing "
            f"'{task.title}' → {target} (proposal only, awaiting your decision).",
        )
        return True

    def _run_completion_hooks(self, task: Task) -> None:
        """Run every registered completion hook for a task that just went DONE.

        Hooks are extension code (v1's feeds watermark lived here); one bad
        hook must never crash the cycle, so each is isolated and reported.
        """
        for hook in COMPLETION_HOOKS:
            try:
                hook(self, task)
            except Exception as e:  # noqa: BLE001 -- see docstring
                hook_name = getattr(hook, "__name__", repr(hook))
                print(f"[cycle] WARNING: completion hook {hook_name} failed on {task.id}: {e}")

    def _answer_pending_chat(self, task: Task) -> None:
        """Answer an unanswered human comment on `task` — thin wrapper over
        the module-level `answer_pending_chat`, which webui's immediate-
        dispatch path also calls. See its docstring for the full contract."""
        answer_pending_chat(self.beads, task, self.config.tasks_path)

    def _execute_cycle_task(self, task: Task, now: datetime | None = None) -> bool:
        """Route one cycle task: verify_child and registered handlers → pure in-process code;
        claude → subprocess; anything else → the normal DSPy agent path.

        Defense-in-depth: a task with ``assigned_to`` set must never reach an
        LLM. ready() already excludes it, so arriving here at all is an anomaly
        — quarantine it loudly (mark BLOCKED with the reason) instead of
        falling through to any agent path. An unrecognized (non-``human:``)
        assignee scheme gets the same treatment so an unknown token is never
        silently routed to a model.
        """
        if task.assigned_to is not None:
            assignee = task.assigned_to
            if isinstance(assignee, str) and assignee.startswith("human:"):
                reason = (
                    f"task is assigned to {assignee} (human executor) — "
                    f"a human-owned task is never executed by a model"
                )
            else:
                reason = (
                    f"unrecognized assignee token {assignee!r} — refusing to "
                    f"dispatch a task with an assignee scheme this code does "
                    f"not understand"
                )
            print(
                f"[cycle] BLOCKED: {task.id} reached dispatch but {reason}; "
                f"marking BLOCKED, not executing"
            )
            self.beads.update(task.id, status=TaskStatus.BLOCKED, result=reason)
            return False
        # Defense-in-depth for the approval gate: a subtask that requires approval
        # is born PENDING_APPROVAL and ready() excludes it, so a PENDING task still
        # carrying requires_approval reached dispatch WITHOUT passing the gate
        # (approve() clears the flag). Quarantine it loudly rather than run it.
        if task.status == TaskStatus.PENDING and task.metadata.get("requires_approval"):
            reason = (
                "task is PENDING but still carries requires_approval metadata — it "
                "reached dispatch without passing the approval gate (approve() clears "
                "the flag on promotion); refusing to execute"
            )
            print(f"[cycle] BLOCKED: {task.id} {reason}; marking BLOCKED, not executing")
            self.beads.update(task.id, status=TaskStatus.BLOCKED, result=reason)
            return False
        if task.metadata.get("type") == "verify_child":
            return self._execute_verify_child(task, now=now)
        handler = CYCLE_HANDLERS.get(task.metadata.get("type") or "")
        if handler is not None:
            return handler(self, task, now)
        if task.assigned_agent == "planner" or task.metadata.get("executor") == "planner":
            return self._execute_planner_task(task)
        if task.assigned_agent == "claude" or task.metadata.get("executor") == "claude":
            if not self._authorize_egress(task, "claude"):
                return False
            return self._execute_claude_task(task)
        if task.assigned_agent == "zai" or task.metadata.get("executor") == "zai":
            if not self._authorize_egress(task, "zai"):
                return False
            return self._execute_zai_task(task)
        if task.assigned_agent == "forge" or task.metadata.get("executor") == "forge":
            if not self._authorize_egress(task, "forge"):
                return False
            return self._execute_forge_task(task)
        return self._execute_task(task)

    def _execute_forge_task(self, task: Task) -> bool:
        """Execute a bead via OpenAI's codex CLI (the Forge persona).

        Prompt-mode only. codex has no equivalent of the store-backed contract
        (it cannot call `agentco tasks complete`), so the result comes back on
        stdout and is written here — the same shape as the plain claude path.
        """
        timeout, _ = self._resolve_budget(task)
        model = task.metadata.get("model")

        self.beads.claim(task.id, "forge", capabilities=self.node_capabilities)
        print(
            f"[cycle] Executing {task.id} via forge/codex subagent "
            f"(model={model or 'codex-default'}, timeout={timeout}s)"
        )
        prompt = task.metadata.get("prompt") or f"{task.title}\n\n{task.description}"
        with self._attribution(task, "forge", model):
            exec_result = run_forge_task(prompt, timeout=timeout, model=model)
        self._record_cost(task, "forge", exec_result)
        if not exec_result.success:
            print(f"[cycle] FAIL: forge subagent for {task.id}: {exec_result.error}")
            self._fail_with_rca(task, exec_result.error)
            return False
        self.beads.complete(task.id, result=exec_result.output)
        print(f"[cycle] Completed {task.id} via forge ({exec_result.duration_seconds:.0f}s)")
        return True

    def _record_cost(self, task: Task, agent: str, exec_result) -> None:
        """Append this execution's telemetry to the cost ledger — thin wrapper
        over the module-level `_record_cost`. Called on BOTH outcomes: a
        failed run still burned tokens, and an average that quietly omits
        failures reads better than reality."""
        _record_cost(self.config.tasks_path, task, agent, exec_result)

    def _attribution(self, task: Task, lane: str, model: str | None = None):
        """Open this node's usage attribution for one dispatch — thin wrapper
        over the module-level `_attribution_for`. Every model-invoking call in
        this class runs inside one; the executor refuses to spawn without it."""
        return _attribution_for(self.config.tasks_path, task, lane, model)

    def _authorize_egress(self, task: Task, agent: str) -> bool:
        """Gate a cross-vendor dispatch on the bead's data classification.

        The dispatch decision is where "this data may not go to that vendor"
        becomes true, so that is where it fails — loudly, per the ISA's
        fail-at-the-layer principle. A refusal is BLOCKED, not FAILED: the work
        is fine, the routing is not, and a human has to re-route it.

        AgentCo is unsupervised by construction (launchd, nobody watching), so
        the gate always evaluates against the unsupervised ceiling.
        """
        try:
            data_class, route = check_egress(agent, task.metadata, supervised=False)
        except (EgressDenied, PolicyUnavailable) as e:
            reason = f"egress denied: {e}"
            print(f"[cycle] BLOCKED: {task.id} {reason}")
            self.beads.update(task.id, status=TaskStatus.BLOCKED, result=reason)
            return False
        where = f"{route.vendor}/{route.model}" if route else "anthropic (native)"
        print(f"[cycle] egress OK: {task.id} [{data_class}] -> {where}")
        return True

    def _open_bead_count(self, tasks: list[Task] | None = None) -> int:
        """Count beads that represent unfinished work (pending/in-progress/blocked)."""
        tasks = self.beads._read_all() if tasks is None else tasks
        return len(
            [
                t
                for t in tasks
                if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
            ]
        )

    def _actionable_bead_count(self, tasks: list[Task] | None = None) -> int:
        """Count beads the cycle could actually act on (pending/in-progress).

        Blocked beads are excluded on purpose: the cycle can do nothing with
        them, so they must not hold the adaptive interval at baseline. When a
        blocked bead unblocks it becomes pending, which resets the cadence
        naturally. They remain visible via `beads_open` in the heartbeat.
        """
        tasks = self.beads._read_all() if tasks is None else tasks
        return len(
            [t for t in tasks if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)]
        )

    def _write_cycle_heartbeat(
        self,
        done: int,
        spawned: int,
        errors: int,
        now: datetime | None = None,
        current_interval_s: float | None = None,
        next_due_at: datetime | None = None,
    ) -> None:
        """Atomically write heartbeat.json (temp file + rename).

        Only called when a cycle ran to completion — a crashed or wedged
        cycle never updates it, so staleness IS the failure signal.

        `current_interval_s` / `next_due_at` carry the adaptive-backoff state
        so freshness monitors (parent `verify_child`, Pulse, doctor) can judge
        against the interval this instance is *actually* on rather than a fixed
        expected cadence. They are written only when backoff is active; a
        backoff-disabled instance emits exactly today's heartbeat shape.
        """
        all_tasks = self.beads._read_all()
        open_count = self._open_bead_count(all_tasks)
        payload = {
            "instance": self.config.instance_name,
            "cycle_completed_at": (now or datetime.now(timezone.utc)).isoformat(),
            "beads_open": open_count,
            "beads_done_this_cycle": done,
            "recurring_spawned_this_cycle": spawned,
            "errors_this_cycle": errors,
            "version": __version__,
        }
        if current_interval_s is not None:
            payload["current_interval_s"] = current_interval_s
        if next_due_at is not None:
            payload["next_due_at"] = next_due_at.isoformat()
        payload.update(
            self._outage_evidence(
                completed=now or datetime.now(timezone.utc),
                interval_s=current_interval_s,
            )
        )
        directory = self._cycle_heartbeat_path.parent
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".heartbeat-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._cycle_heartbeat_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------
    # Adaptive cycle backoff (v0.4.1)
    # ------------------------------------------------------------------

    def _resolve_backoff(self) -> tuple[bool, float, float, float]:
        """Resolve the backoff policy to numbers, degrading advisory-style.

        Returns (active, base_s, factor, max_s). `active` is False when the
        block is disabled OR malformed — a malformed block never skips a cycle
        (loud WARNING here, hard FAIL in `agentco doctor`), it just falls back
        to running every wake at baseline. base_s is always a usable number so
        callers can compute a next_due_at even in the disabled case.
        """
        b = self.config.backoff
        errs = b.validation_errors()
        if errs:
            print(
                f"[cycle] WARNING: backoff config is malformed ({'; '.join(errs)}) "
                f"— disabling backoff for this cycle (running at baseline). "
                f"Run `agentco doctor` to see the FAIL."
            )
            return (False, 3600.0, 2.0, 7 * 86400.0)
        base_s = parse_duration(b.base).total_seconds()
        max_s = parse_duration(b.max).total_seconds()
        return (bool(b.enabled), base_s, float(b.factor), max_s)

    def _reset_signal(self, now: datetime, cycle_hb: dict) -> str | None:
        """Return a human-readable reason to run *now* despite backoff, or None.

        Any live activity resets the cadence to baseline: actionable work in
        the queue (pending/in-progress — NOT blocked, which the cycle can't
        act on), a bead created since the last completed cycle, or a recurring
        def that has come due. This is the 'a new task appeared → go back to
        1h' rule. Read-only and cheap (one JSONL scan of the local queue).
        """
        tasks = self.beads._read_all()
        if self._actionable_bead_count(tasks) > 0:
            return "actionable beads in the queue"

        last_completed = cycle_hb.get("cycle_completed_at")
        if last_completed:
            try:
                marker = datetime.fromisoformat(last_completed)
                if marker.tzinfo is None:
                    marker = marker.replace(tzinfo=timezone.utc)
                for t in tasks:
                    try:
                        created = datetime.fromisoformat(t.created_at)
                    except (ValueError, TypeError):
                        continue
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created > marker:
                        return f"bead {t.id} created since last cycle"
            except (ValueError, TypeError):
                pass

        if any_due(self.recurring, now=now):
            return "a recurring definition is due"
        return None

    def cycle(
        self, now: datetime | None = None, limit: int = 50, force: bool = False
    ) -> dict:
        """Run one heartbeat cycle: reconcile recurring defs → triage the
        open queue → execute selected tasks → write heartbeat.json.

        Adaptive backoff (advisory): when nothing is active and the wake lands
        before the persisted `next_due_at`, the cycle exits fast with a single
        log line and only touches a lightweight `last_wake_at` (proof the
        launchd job is alive) — it deliberately does NOT move the heartbeat's
        `cycle_completed_at`, because the heartbeat moves only on real work.
        `--force` (force=True) always runs and resets the interval to baseline.

        Returns a summary dict. Raises (and writes NO heartbeat) if the
        cycle itself crashes — bead-level failures are counted as errors
        but do not crash the cycle.
        """
        now = now or datetime.now(timezone.utc)
        backoff_active, base_s, factor, max_s = self._resolve_backoff()

        # --- Backoff gate: a near-free wake that may exit before doing work ---
        if backoff_active and not force:
            cycle_hb = self._read_cycle_heartbeat() or {}
            next_due_raw = cycle_hb.get("next_due_at")
            if next_due_raw:
                try:
                    next_due_at = datetime.fromisoformat(next_due_raw)
                    if next_due_at.tzinfo is None:
                        next_due_at = next_due_at.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    next_due_at = None
                if next_due_at is not None and now < next_due_at:
                    reason = self._reset_signal(now, cycle_hb)
                    if reason is None:
                        # Idle and not yet due: skip. Touch last_wake_at only —
                        # heartbeat.json (cycle_completed_at) stays put.
                        self._heartbeat(
                            last_wake_at=datetime.now().astimezone().isoformat()
                        )
                        print(
                            f"[cycle] skipped — backoff until {next_due_at.isoformat()}"
                        )
                        return {
                            "skipped": True,
                            "reason": "backoff",
                            "next_due_at": next_due_at.isoformat(),
                            "current_interval_s": cycle_hb.get("current_interval_s"),
                            "spawned": 0,
                            "executed": 0,
                            "errors": 0,
                            "open_after": self._open_bead_count(),
                        }
                    print(f"[cycle] backoff reset — running now ({reason})")

        spawned = reconcile(self.recurring, self.beads, now=now)

        # Close recurring failures that a later run already disproved. Without this
        # every health-check failure lives forever and the queue stops carrying a
        # signal — 37 stale `verify-sommeliwhey` failures were sitting at the top of
        # "needs you" on 2026-08-06 behind 388 subsequent passes.
        stale = supersede_stale_failures(self.beads)
        if stale:
            print(f"[cycle] superseded {len(stale)} stale recurring failure(s)")
        # Order matters: sweeping samples first resolves the subjects these RCAs
        # diagnose, so the RCA pass sees the post-sweep truth in the same cycle.
        moot = supersede_resolved_rcas(self.beads)
        if moot:
            print(f"[cycle] closed {len(moot)} RCA(s) whose failure is resolved")

        # Return beads abandoned by a dead lease to the ready set (ac-9cae7593).
        # Lives here, beside the other supersede passes, because those solve the
        # same class of problem: state that stopped being true and that nothing
        # else was ever going to correct. A remote worker that loses power mid-
        # bead leaves it IN_PROGRESS forever otherwise — invisible to ready(),
        # never retried, and not even counted as failed. Reaping is deliberately
        # NOT a failure (see reap_expired_leases): the attempt counter is the
        # record, and the bead simply becomes claimable again.
        expired = self.beads.reap_expired_leases(now=now)
        if expired:
            print(f"[cycle] reclaimed {len(expired)} bead(s) from expired leases")

        # Detect ghost blockers — pending tasks whose blocked_by IDs don't exist anywhere.
        # These tasks will never become ready on their own; surface them loudly.
        for stuck_task, ghost_ids in self.beads.ghost_blockers():
            msg = (
                f"⚠️ AgentCo [{self.config.instance_name}]: task '{stuck_task.title}' "
                f"({stuck_task.id}) is blocked by non-existent ID(s): "
                f"{', '.join(ghost_ids)} — task will never become ready"
            )
            print(f"[cycle] GHOST_BLOCKER: {msg}")
            if self.config.notify.enabled:
                notify_event(self.config.notify, msg, urgent=True)

        # verify_child beads are never crowded out of a limited cycle —
        # a busy queue must not starve liveness verification.
        all_ready = self.beads.ready()
        external = [t for t in all_ready if self._external_agent(t.assigned_agent)]
        if external:
            print(
                f"[cycle] leaving {len(external)} bead(s) for externally-executed "
                f"agent(s): {sorted({t.assigned_agent for t in external})}"
            )
            all_ready = [t for t in all_ready if t not in external]

        # Preflight: a name that survives the external filter but has no
        # in-process class and no special-executor branch cannot be dispatched
        # — `get_agent()` will raise "Unknown agent: <name>" for every one of
        # them. Claiming them anyway turns ONE config defect into N failed
        # beads plus an RCA bead apiece: the sommeliwhey box-scout incident
        # (2026-07-22, 07-29, 08-04) burned 50 beads and ~95 duplicate RCA
        # beads at ~$6 each on a single missing `agents:` key. The detector for
        # this already existed in `doctor` check (r), but doctor only runs when
        # an operator types it, so it never stood between the defect and the
        # queue. This is that same test, on the execution path.
        #
        # Threshold, not a blanket skip, because two different defects wear the
        # same symptom and want opposite handling. A TYPO is a property of one
        # bead ("box-scoot"), and failing it loudly is right — that is what
        # `test_undeclared_unknown_agent_still_fails_loudly` protects. An
        # UN-DECLARED agent is a property of the config, so it shows up on every
        # ready bead naming it. Grouping by name separates them: a name carrying
        # ≥2 ready beads is config-shaped and gets stalled; a lone bead still
        # fails as before. The worst case is bounded — a clobber caught while
        # only one bead is ready costs one failure, not fifty.
        #
        # Stalled beads are left PENDING, not failed: the defect is in config,
        # the fix is in config, and the next cycle picks them up for free.
        # Silence is the risk a stall carries, so the stall is loud — an error
        # line every cycle, an urgent notification, `undispatchable` in
        # runs.jsonl, and a doctor FAIL that already names the agent.
        undispatchable_counts: dict[str, int] = {}
        for t in all_ready:
            name = t.assigned_agent
            if name and name not in _lm.agent_names() and name not in SPECIAL_EXECUTORS:
                undispatchable_counts[name] = undispatchable_counts.get(name, 0) + 1
        config_shaped = {n for n, c in undispatchable_counts.items() if c >= 2}
        undispatchable = [t for t in all_ready if t.assigned_agent in config_shaped]
        if undispatchable:
            names = sorted(config_shaped)
            msg = (
                f"⚠️ AgentCo [{self.config.instance_name}]: {len(undispatchable)} "
                f"ready bead(s) name agent(s) that cannot be dispatched: "
                f"{', '.join(names)} — not built-in, not a special executor, and "
                f"not declared under `agents:` in config. Left pending; fix the "
                f"config (or the agent name) and they run next cycle."
            )
            print(f"[cycle] UNDISPATCHABLE_AGENT: {msg}")
            if self.config.notify.enabled:
                notify_event(self.config.notify, msg, urgent=True)
            all_ready = [t for t in all_ready if t not in undispatchable]

        verifies = [t for t in all_ready if t.metadata.get("type") == "verify_child"]
        rest = [t for t in all_ready if t.metadata.get("type") != "verify_child"]
        ready = verifies[:limit] + rest[: max(0, limit - len(verifies))]
        ordered = self._triage(ready)

        done = 0
        errors = 0
        outcomes: list[dict] = []
        for task in ordered:
            outcome = {"id": task.id, "title": task.title, "agent": task.assigned_agent}
            try:
                if self._execute_cycle_task(task, now=now):
                    done += 1
                    outcome["outcome"] = "done"
                else:
                    errors += 1
                    outcome["outcome"] = "failed"
            except Exception as e:
                print(f"[cycle] Error executing {task.id}: {e}")
                self._fail_with_rca(task, str(e))
                errors += 1
                outcome["outcome"] = "failed"
            if outcome["outcome"] == "failed":
                refreshed = self.beads.get(task.id)
                if refreshed and refreshed.result:
                    outcome["error"] = refreshed.result[:200]
            outcomes.append(outcome)

        # Unanswered human comments — closes the comment loop (a human-assigned
        # bead never gets a normal agent run, so this is the only dispatch path
        # for it). Independent of `ready()`/`limit`/triage: it never competes
        # with the main queue and never touches status or assigned_to.
        chat_pending_tasks = [t for t in self.beads.list() if t.metadata.get("chat_pending")]
        chat_replies = 0
        for t in chat_pending_tasks:
            try:
                self._answer_pending_chat(t)
                chat_replies += 1
            except Exception as e:  # noqa: BLE001 — one bad reply must not kill the cycle
                print(f"[cycle] WARNING: chat reply for {t.id} failed: {e}")

        # --- Adaptive interval: real work (or a still-busy queue, or --force)
        # snaps back to baseline; a genuinely idle cycle doubles up to the cap. ---
        current_interval_s = next_due_at_out = None
        if backoff_active:
            actionable_after_count = self._actionable_bead_count()
            did_work = (len(spawned) > 0) or (done > 0) or (errors > 0) or (chat_replies > 0)
            if force or did_work or actionable_after_count > 0:
                current_interval_s = base_s
            else:
                prev = (self._read_cycle_heartbeat() or {}).get("current_interval_s")
                prev_s = float(prev) if prev else base_s
                current_interval_s = min(prev_s * factor, max_s)
            next_due_at_out = now + timedelta(seconds=current_interval_s)

        self._heartbeat(last_work_at=datetime.now().astimezone().isoformat())
        self._write_cycle_heartbeat(
            done=done,
            spawned=len(spawned),
            errors=errors,
            now=now,
            current_interval_s=current_interval_s,
            next_due_at=next_due_at_out,
        )
        summary = {
            "spawned": len(spawned),
            "executed": done,
            "errors": errors,
            "open_after": len(self.beads.ready()),
        }
        if undispatchable:
            # Only when non-zero: every run record carrying the key would make
            # the signal easy to skim past, and the whole point is that it is
            # abnormal. runs.jsonl is where a later RCA reconstructs the day.
            summary["undispatchable"] = len(undispatchable)
        if chat_replies:
            summary["chat_replies"] = chat_replies
        if backoff_active:
            summary["current_interval_s"] = current_interval_s
            summary["next_due_at"] = next_due_at_out.isoformat()
        self._log_run(now=now, summary=summary, outcomes=outcomes)
        print(
            f"[cycle] Completed: spawned={summary['spawned']} executed={done} "
            f"errors={errors} open={summary['open_after']}"
        )
        if self.config.notify.cycle_summary:
            self._notify_cycle_summary(summary, outcomes)
        return summary

    def _log_run(self, now: datetime, summary: dict, outcomes: list[dict]) -> None:
        """Append a structured record of this cycle to runs.jsonl.

        Same contract as the heartbeat: only a cycle that ran to completion
        gets a record — the execution log is the history of real runs.
        """
        record = {
            "at": now.isoformat(),
            "instance": self.config.instance_name,
            **summary,
            "tasks": outcomes,
        }
        with open(self.config.runs_path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _notify_cycle_summary(self, summary: dict, outcomes: list[dict]) -> None:
        """Send the per-execution message (Telegram channel, non-urgent)."""
        mark = "✅" if summary["errors"] == 0 else "⚠️"
        lines = [
            f"{mark} AgentCo [{self.config.instance_name}] cycle: "
            f"{summary['executed']} done, {summary['errors']} errors, "
            f"{summary['spawned']} spawned, {summary['open_after']} open"
        ]
        for o in outcomes[:6]:
            icon = "✅" if o["outcome"] == "done" else "❌"
            line = f"{icon} {o['title']} ({o['agent'] or '-'})"
            if o.get("error"):
                line += f" — {o['error'][:80]}"
            lines.append(line)
        if len(outcomes) > 6:
            lines.append(f"… and {len(outcomes) - 6} more")
        notify_event(self.config.notify, "\n".join(lines), urgent=False)

    def observe(self) -> list[Task]:
        """Poll every registered source and create tasks from its events.

        The Harness ships no sources of its own — integrations register a
        factory via `register_source_factory`; with none registered this is
        a heartbeat-only no-op.
        """
        sources = [src for factory in SOURCE_FACTORIES for src in factory(self.config)]
        created_tasks = []

        for source in sources:
            try:
                for event in source.poll():
                    task = self.classifier.process(
                        source=event.source,
                        content=event.content,
                        context=event.context,
                        source_id=event.source_id,
                    )
                    if task:
                        created_tasks.append(task)
                        print(f"[observe] Created task: {task.id} - {task.title}")
            except Exception as e:
                print(f"[observe] Error polling {source.name}: {e}")

        self._heartbeat(last_observe_at=datetime.now().astimezone().isoformat())
        return created_tasks

    def work(self, agent_name: str | None = None, limit: int = 10) -> list[Task]:
        """Execute ready tasks."""
        ready_tasks = self.beads.ready(assigned_agent=agent_name)[:limit]
        completed_tasks = []

        for task in ready_tasks:
            try:
                result = self._execute_task(task)
                if result:
                    completed_tasks.append(task)
            except Exception as e:
                print(f"[work] Error executing {task.id}: {e}")
                self._fail_with_rca(task, str(e))

        self._heartbeat(
            last_work_at=datetime.now().astimezone().isoformat(),
            last_work_completed=len(completed_tasks),
        )
        return completed_tasks

    def _execute_task(self, task: Task) -> bool:
        """Execute a single task.

        A False return is the *failure* signal to cycle() (it increments
        ``errors``), so a branch that returns False must also leave the bead in
        a terminal state. Until 2026-08-04 the unassigned branch below did not:
        it printed a skip, returned False, and touched nothing — so the bead
        stayed PENDING, ready() re-selected it next hour, and it re-"failed"
        forever at zero token cost with ``error: None``. On the sommeliwhey node
        24 beads did this every cycle from 2026-07-24 onward, which pinned
        ``errors`` non-zero in both runs.jsonl and heartbeat.json for 11 days
        and made the three box-scout incidents invisible in exactly the signal
        that should have caught them. A skip that repeats forever is not a skip.
        """
        if not task.assigned_agent:
            reason = (
                "task reached dispatch with no assigned_agent and no assigned_to — "
                "nothing can execute it. Assign an agent (or a human:<name> "
                "executor) and reopen it; leaving it PENDING would re-fail it "
                "every cycle with no error recorded."
            )
            print(f"[work] BLOCKED: {task.id} {reason}")
            self.beads.update(task.id, status=TaskStatus.BLOCKED, result=reason)
            return False

        if self._external_agent(task.assigned_agent):
            print(
                f"[work] Task {task.id} is assigned to externally-executed agent "
                f"'{task.assigned_agent}' — leaving pending for its out-of-band worker"
            )
            return False

        print(f"[work] Executing: {task.id} - {task.title} (agent: {task.assigned_agent})")

        # Claim the task. The claim is now compare-and-set (ac-9cae7593), so a
        # None means another claimant — a second cycle, or a remote worker
        # pulling the same store — already owns this bead. Execute anyway and
        # both run it, then both write a result and the later one silently wins:
        # exactly the double-execution the CAS exists to prevent. Checking the
        # answer is what makes it a lock rather than a decoration. Nothing
        # re-loops here: ready() excludes beads under a live lease, so the bead
        # is not re-selected next cycle; it is simply the other holder's.
        # The claim also carries this node's capability manifest (ac-39d4dbc8).
        # A CapabilityError here is NOT contention — it is a bead that landed in
        # a lane this machine physically cannot serve (the write-scoped ADO PAT
        # lives on the MacBook and nowhere else). Retrying it every cycle would
        # burn a dispatch slot forever and record nothing, which is the same
        # silent-repetition failure the unassigned branch above was fixed for.
        # BLOCKED is terminal AND visible: it surfaces in `agentco me` with the
        # missing capability named, so the fix (move the bead to the right lane,
        # or correct its requires) is one read away.
        try:
            claimed = self.beads.claim(
                task.id, task.assigned_agent, capabilities=self.node_capabilities
            )
        except CapabilityError as e:
            reason = (
                f"{e} This node ({self.config.instance_name}) cannot execute it; "
                f"reassign it to a node that declares those capabilities."
            )
            print(f"[work] BLOCKED: {task.id} {reason}")
            self.beads.update(task.id, status=TaskStatus.BLOCKED, result=reason)
            return False

        if claimed is None:
            print(
                f"[work] Task {task.id} was claimed by another worker — "
                f"skipping (lease held elsewhere)"
            )
            return False

        # Get and run the agent, honoring its model override if one exists
        agent_config = self.config.agents.get(task.assigned_agent)
        agent = _lm.agents("Built-in LM agents").get_agent(
            task.assigned_agent, self.beads, agent_config=agent_config
        )
        agent_lm = self._agent_lms.get(task.assigned_agent)
        if agent_lm is not None:
            with _lm.dspy("Per-agent model overrides").context(lm=agent_lm):
                result = agent.execute(task)
        else:
            result = agent.execute(task)

        if not result.success:
            self._fail_with_rca(task, result.error)
            return False

        # Create subtasks as pending_approval — never auto-execute agent-spawned tasks.
        # Bounded by MAX_SUBTASKS_PER_TASK (count) and the depth cap in beads.create.
        if result.subtasks:
            proposed = result.subtasks
            capped = proposed[:MAX_SUBTASKS_PER_TASK]
            if len(proposed) > MAX_SUBTASKS_PER_TASK:
                print(
                    f"[work] WARNING: {task.id} proposed {len(proposed)} subtask(s); "
                    f"capping at {MAX_SUBTASKS_PER_TASK}"
                )
            created = 0
            for st in capped:
                try:
                    # Born PENDING_APPROVAL atomically (create's status kwarg) — no
                    # PENDING-then-flip window where a concurrent cycle could grab a
                    # subtask that is meant to wait for approval.
                    new_task = self.beads.create(
                        title=st["title"],
                        description=st["description"],
                        assigned_agent=st.get("assigned_agent"),
                        parent_id=task.id,
                        priority=TaskPriority(st.get("priority", 2)),
                        status=TaskStatus.PENDING_APPROVAL,
                        metadata={**st.get("metadata", {}), "requires_approval": True},
                    )
                except DepthLimitError as e:
                    print(f"[work] depth cap: skipping subtask of {task.id} — {e}")
                    continue
                print(f"[work] Queued for approval: {new_task.id} - {new_task.title}")
                created += 1
            if created and self.config.notify.enabled:
                notify_event(
                    self.config.notify,
                    f"⏳ AgentCo [{self.config.instance_name}]: task '{task.title}' "
                    f"proposed {created} subtask(s) awaiting your approval. "
                    f"Run: agentco approve --list",
                    urgent=False,
                )

        # Complete the task
        self.beads.complete(task.id, result=json.dumps(result.output))
        print(f"[work] Completed: {task.id}")
        return True

    def daemon(
        self,
        observe_interval: int = 300,
        work_interval: int = 60,
        cycle_interval: int = 3600,
    ):
        """Run continuously in daemon mode.

        The hourly heartbeat cycle (recurring generator → triage → execute
        → heartbeat.json) rides the existing loop — no new process.
        Reconciliation makes a too-frequent heartbeat cheap: nothing
        overdue → no-op.
        """
        print(f"[daemon] Starting AgentCo daemon")
        print(
            f"[daemon] Observe interval: {observe_interval}s, "
            f"Work interval: {work_interval}s, Cycle interval: {cycle_interval}s"
        )

        last_observe = 0.0
        last_cycle = 0.0

        while True:
            now = time.time()

            # Observe periodically
            if now - last_observe >= observe_interval:
                print(f"\n[daemon] {datetime.now().isoformat()} - Observing sources...")
                self.observe()
                last_observe = now

            # Heartbeat cycle periodically (recurring tasks + child checks)
            if now - last_cycle >= cycle_interval:
                print(f"\n[daemon] {datetime.now().isoformat()} - Heartbeat cycle...")
                try:
                    self.cycle()
                except Exception as e:
                    # A crashed cycle writes no heartbeat — staleness is the
                    # signal — but the daemon itself keeps running.
                    print(f"[daemon] WARNING: heartbeat cycle crashed: {e}")
                last_cycle = now

            # Work on ready tasks
            ready = self.beads.ready()
            if ready:
                print(f"\n[daemon] {datetime.now().isoformat()} - {len(ready)} tasks ready")
                self.work(limit=5)

            time.sleep(work_interval)

    def _children_status(self, now: datetime | None = None) -> list[dict]:
        """Live staleness check of every registered child — pure code."""
        return [verify_child(child, now=now) for child in self.children.list()]

    def _children_quarantined(self) -> list[str]:
        """Registry rows too malformed to verify. `list()` populates this."""
        return list(self.children._quarantined)

    def _own_next_due_at(self) -> datetime | None:
        """This instance's OWN deadline, from the heartbeat of the PREVIOUS cycle.

        Safe to read mid-cycle: `_write_cycle_heartbeat` runs only at the end of
        `cycle()`, so during task execution heartbeat.json still holds the last
        completed cycle. That is exactly the value wanted — how late THIS parent
        is tells `verify_child` whether a stale child is independently broken or
        was killed by the same host outage that just stopped the parent.
        """
        hb = self._read_cycle_heartbeat() or {}
        raw = hb.get("next_due_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _outage_evidence(
        self, completed: datetime, interval_s: float | None
    ) -> dict:
        """`last_outage_*` fields for the heartbeat about to be written.

        A gap between the previous cycle's completion and this one that exceeds
        `(1 + DEFAULT_DUE_GRACE) x interval` is a host-level outage — the same
        threshold `verify_child` uses to call a child stale, applied to this
        instance's own cadence. It is recorded here so the NEXT cycles can still
        explain children that launchd has not restarted yet: the parent stops
        looking late the moment it takes its first tick back, tens of minutes
        before its children do.

        The record is carried forward while it stays admissible and then simply
        stops being written, so the fields are absent on a normally-ticking node
        and no consumer has to reason about a stale outage marker.
        """
        interval = float(interval_s or 0.0)
        if interval <= 0:
            interval = self._resolve_backoff()[1]
        prev = self._read_cycle_heartbeat() or {}
        prev_completed = _parse_ts(prev.get("cycle_completed_at"))

        if prev_completed is not None:
            gap = (completed - prev_completed).total_seconds()
            if gap > (1 + DEFAULT_DUE_GRACE) * interval:
                return {
                    "last_outage_gap_s": gap,
                    "last_outage_ended_at": completed.isoformat(),
                }

        carried = prev.get("last_outage_gap_s")
        ended_at = _parse_ts(prev.get("last_outage_ended_at"))
        if carried and ended_at is not None:
            window = OUTAGE_EVIDENCE_WINDOW_INTERVALS * interval
            if (completed - ended_at).total_seconds() <= window:
                return {
                    "last_outage_gap_s": float(carried),
                    "last_outage_ended_at": ended_at.isoformat(),
                }
        return {}

    def _own_recent_outage_seconds(self, now: datetime | None = None) -> float:
        """How long THIS instance was itself out, while that is still evidence.

        Read mid-cycle like `_own_next_due_at`, from the previous cycle's
        heartbeat. Returns 0.0 on a node that has been ticking normally, which
        is what leaves `verify_child`'s loud-failure path untouched.
        """
        hb = self._read_cycle_heartbeat() or {}
        gap = hb.get("last_outage_gap_s")
        ended_at = _parse_ts(hb.get("last_outage_ended_at"))
        if not gap or ended_at is None:
            return 0.0
        interval = float(hb.get("current_interval_s") or 0.0)
        if interval <= 0:
            interval = self._resolve_backoff()[1]
        now = now or datetime.now(timezone.utc)
        if (now - ended_at).total_seconds() > (
            OUTAGE_EVIDENCE_WINDOW_INTERVALS * interval
        ):
            return 0.0
        return max(0.0, float(gap))

    def _read_cycle_heartbeat(self) -> dict | None:
        if self._cycle_heartbeat_path.exists():
            try:
                return json.loads(self._cycle_heartbeat_path.read_text())
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def status(self, now: datetime | None = None) -> dict:
        """Get current status, including last-cycle heartbeat."""
        all_tasks = self.beads._read_all()
        heartbeat = self._read_heartbeat()
        cycle_hb = self._read_cycle_heartbeat()
        children = self._children_status(now=now)
        # A consumer that sees only the children list cannot tell a fully
        # monitored portfolio from one where rows were dropped on the way in —
        # three real children once vanished behind a clean-looking list. The
        # count of unparseable rows travels WITH the list so a dashboard can
        # never render a green all-clear over a hole in it.
        children_quarantined = self._children_quarantined()
        return {
            "instance": self.config.instance_name,
            "last_cycle_completed_at": (cycle_hb or {}).get("cycle_completed_at"),
            "errors_last_cycle": (cycle_hb or {}).get("errors_this_cycle"),
            "children": children,
            "children_quarantined": len(children_quarantined),
            "children_unverified": len([c for c in children if c.get("level") == "unverified"]),
            # Counted separately for the same reason `children_quarantined`
            # exists: a dashboard that subtracts the unverified from the total
            # would otherwise render an off-machine node as locally verified —
            # a green all-clear over a node this host never actually observed.
            "children_remote": len([c for c in children if c.get("level") == "remote"]),
            "total": len(all_tasks),
            "pending": len([t for t in all_tasks if t.status == TaskStatus.PENDING]),
            "in_progress": len([t for t in all_tasks if t.status == TaskStatus.IN_PROGRESS]),
            "done": len([t for t in all_tasks if t.status == TaskStatus.DONE]),
            "failed": len([t for t in all_tasks if t.status == TaskStatus.FAILED]),
            "skipped": len([t for t in all_tasks if t.status == TaskStatus.SKIPPED]),
            "quarantined": len(self.beads._quarantined),
            "by_agent": {
                agent: len([t for t in all_tasks if t.assigned_agent == agent])
                for agent in ("cs", "pm", "dev", "devops", "analyst")
            },
            "last_observe_at": heartbeat.get("last_observe_at"),
            "last_work_at": heartbeat.get("last_work_at"),
        }
