"""A portable value gate for SOPBench: ask a judge, not a hand-written table.

Why this exists. The value gate every published arm used (`make_value_checker`)
resolves each condition through `_TaskTruth.holds`, which is hand-written per
constraint for `bank` and resolves almost nothing elsewhere — 6 scorable
predictions each on `library` and `online_market` (ASOP-V2-ITERATION.md §4). On
a held-out domain the gate was therefore effectively absent, and "the gate
never demonstrated value" could not be separated from "the gate never ran".
This module replaces the table with a judge that needs no per-domain code, so
enforcement can be tested where it has not been: `hotel`.

Two judges, same interface as `make_value_checker` — `(step, tool_history) ->
(Optional[bool], reason)`, `None` meaning "cannot judge, fall back to liveness":

  jev         TypeSafe's Jev, the typed-choice model that scored +0.845 lift with
              0/200 false refusals on `hotel` (JUDGE-SWEEP.md). ⚠️ That number is
              for a DIFFERENT question — "were this call's prerequisite ACTIONS
              performed" — not "does this tool result satisfy this condition".
              Jev's accuracy here is unmeasured; the verdict log exists so it can
              be graded after the fact rather than assumed.
  laya        Laya (`laya` checkpoint, native window, evidence-first), served by
              `laya-serve` over HTTP so it needs no torch in SOPBench's venv.
              Same question wording as `jev`, so A4 and A6 differ only in the
              judge. Stage 0 on the same 400 `hotel` decisions: lift −0.120
              [−0.217, −0.023] — worse than chance (QUEUED-EXPERIMENTS.md).
              The HTTP path was checked against in-process `judge_laya`: 30/30
              identical verdicts, identical confidences.
  string-all  A deliberately dumb control: PASS iff the step tool's latest result
              looks truthy, ignoring the condition's wording, threshold and
              polarity. The sweep's string-match ALL ("refuse unless every named
              prerequisite was called") cannot be ported literally — in this
              runtime it IS the liveness check every gated arm already applies —
              so this is its nearest honest analogue: a check with no judgment.

Scope, stated so it is not over-read:
  * Only the task's own constraints are judged, matched to the step by the tool
    that verifies them — the same matching `make_value_checker` uses.
  * A constraint in an OR group is NOT judged (abstain -> liveness). Judging one
    member alone demands more than the task does; judging the group needs a
    grouped question this module does not ask yet.
  * The judge sees what `_TaskTruth` sees and nothing else: the tool history and
    `user_known` (what the user said), plus SOPBench's own verbalisation of the
    condition with this task's `constraint_parameters`. Never the gold database,
    `action_should_succeed` or the evaluation criteria.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

JEV_USD_PER_MTOK_INPUT = 0.042  # docs.typesafe.ai/models, fetched 2026-09-23; output free

_ZAI_OPENAI_URL = "https://api.z.ai/api/coding/paas/v4"
_ENV_FILE = pathlib.Path.home() / ".claude" / ".env"


# ── backend wiring ───────────────────────────────────────────────────────────


def register_model(name: str) -> None:
    """Admit `name` to SOPBench's OpenAI backend at runtime.

    SOPBench whitelists model names in `swarm/constants.py`; the T2 ladder
    admitted `openai/gpt-oss-20b` by hand-editing that file. This does the same
    for any name without an edit: `llm_handler` imports the lists by name, so
    mutating the list OBJECTS is visible there. Both lists matter — the first
    admits the handler, the second admits `--tool_call_mode fc`.
    """
    import swarm.constants as C

    if name not in C.OPENAI_MODELS:
        C.OPENAI_MODELS.append(name)
    fc = C.FUNCTION_CALLING_MODELS.setdefault("openai", [])
    if name not in fc:
        fc.append(name)


def _env_value(key: str) -> Optional[str]:
    val = os.environ.get(key)
    if val:
        return val
    try:
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    except OSError:
        pass
    return None


def use_zai() -> str:
    """Point every OpenAI client in this process at z.ai's Coding-Plan endpoint.

    ⚠️ The OpenAI-format CODING path (`/api/coding/paas/v4`), not the
    pay-per-token `/api/paas/v4` one, which answers a Coding-Plan key with 429
    code 1113 (sopbench_judge.py `_ZAI_URL` records that trap). Verified
    2026-09-24: HTTP 200 with a real tool call. The key is read from env or
    ~/.claude/.env and never printed. Returns the base URL for the record.
    """
    key = _env_value("ZAI_API_KEY")
    if not key:
        raise SystemExit("ZAI_API_KEY not found in env or ~/.claude/.env")
    os.environ["OPENAI_BASE_URL"] = _ZAI_OPENAI_URL
    os.environ["OPENAI_API_KEY"] = key
    _more_client_retries()
    return _ZAI_OPENAI_URL


ZAI_MAX_RETRIES = 8


def _more_client_retries() -> None:
    """Give every OpenAI client in this process `ZAI_MAX_RETRIES` retries.

    ⚠️ SOPBench has no rate-limit handling of its own. When a completion raises,
    `run_simulation`'s task loop RETRIES THE WHOLE TASK AT temperature 0.7 /
    top_p 0.95 — a silent change of sampling regime for that task. The openai
    client's default is 2 retries with backoff, which a hosted endpoint under
    concurrent load can exhaust. More client-level retries keep a 429 burst a
    wait rather than a regime change. (Local LM Studio arms never reach this.)
    """
    import openai

    cls = openai.OpenAI
    if getattr(cls.__init__, "_more_retries", False):
        return
    real = cls.__init__

    def init(self, *a, **kw):
        kw.setdefault("max_retries", ZAI_MAX_RETRIES)
        real(self, *a, **kw)

    init._more_retries = True
    cls.__init__ = init


# ── the judges ───────────────────────────────────────────────────────────────

_JEV_URL = "https://api.typesafe.ai/v1/systemone"

#: Same typed-choice shape as `sopbench_judge.judge_jev` (the +0.845 arm); only
#: the question is re-worded from "prerequisite actions" to "this condition".
_JEV_INSTRUCTIONS = (
    "A condition must hold before an action may be taken. Given the evidence "
    "(the tool calls made so far with their results, and what the user "
    "provided), does the evidence establish that the stated condition holds?"
)
_JEV_CRITERIA = {
    "held": "the evidence establishes the condition DOES hold",
    "not_held": "the evidence does not establish the condition holds, or shows it does not",
}


def jev_ask(body: str, model: str = "jev-latest", timeout: int = 60, retries: int = 4,
            _key: Optional[str] = None) -> tuple[Optional[bool], float, str]:
    """One Jev call. `(held, confidence, raw)`; `held is None` on any failure."""
    key = _key or _env_value("TYPESAFE_API_KEY")
    if not key:
        return None, 0.0, "ERROR no TYPESAFE_API_KEY"
    payload = {
        "model": model,
        "state": body,
        "questions": {"verdict": {"type": "choice", "instructions": _JEV_INSTRUCTIONS,
                                  "criteria": _JEV_CRITERIA}},
    }
    raw = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                _JEV_URL, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            ans = json.load(urllib.request.urlopen(req, timeout=timeout))["answers"]["verdict"]
            return ans["choice"] == "held", float(ans.get("confidence", 0.0)), json.dumps(ans)[:200]
        except urllib.error.HTTPError as exc:
            raw = f"ERROR HTTP {exc.code}: {exc.read().decode()[:160]}"
            if exc.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            break
        except Exception as exc:  # network, JSON shape
            raw = f"ERROR {type(exc).__name__}: {exc}"[:200]
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            break
    return None, 0.0, raw


LAYA_URL = os.environ.get("LAYA_URL", "http://127.0.0.1:4243/v1/systemone")


def laya_ask(body: str, timeout: int = 60, retries: int = 3,
             url: Optional[str] = None) -> tuple[Optional[bool], float, str]:
    """One Laya call via `laya-serve`. State is `{"body": ...}` and no `model`
    is sent, exactly as `sopbench_judge.judge_laya` calls `Router.predict` —
    so the router picks the same default checkpoint."""
    payload = {
        "state": {"body": body},
        "questions": {"verdict": {"type": "choice", "instructions": _JEV_INSTRUCTIONS,
                                  "criteria": _JEV_CRITERIA}},
    }
    raw = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url or LAYA_URL, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            ans = json.load(urllib.request.urlopen(req, timeout=timeout))["answers"]["verdict"]
            return ans["choice"] == "held", float(ans.get("confidence", 0.0)), json.dumps(ans)[:200]
        except Exception as exc:
            raw = f"ERROR {type(exc).__name__}: {exc}"[:200]
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
    return None, 0.0, raw


_FALSY = {"", "false", "none", "null", "0", "0.0", "[]", "{}", "no"}


def looks_truthy(result: Optional[str]) -> bool:
    """The string-all control's entire judgment."""
    if result is None:
        return False
    text = str(result).strip()
    return text.lower() not in _FALSY and not text.lower().startswith("error")


# ── the checker ──────────────────────────────────────────────────────────────


class JudgeStats:
    """Process-wide counters, folded into the arm's summary."""

    def __init__(self) -> None:
        self.calls = 0
        self.blocks = 0
        self.passes = 0
        self.abstain_or = 0
        self.abstain_error = 0
        self.input_chars = 0
        # `--judge-gate-mode advisory` only. `advisory_notes` is a not_held
        # verdict that PASSED with a note queued for the executor's next
        # prompt; `advisory_hard_blocks` is a not_held verdict that still hard
        # -stopped because its confidence met the threshold (N36's "confident
        # blocks are ~96% correct" finding). Both are already counted once in
        # `blocks` above (a not_held verdict either way) — these two just split
        # that number by which door it went out.
        self.advisory_notes = 0
        self.advisory_hard_blocks = 0
        # Judge-health window (2026-09-26 outage: api.typesafe.ai unreachable,
        # every Jev call timed out at ~494s and abstained — the smoke test
        # still exited 0 because an abstain is, by design, "cannot judge, fall
        # back to liveness", not a crash. That is correct FOR ONE STEP and
        # wrong for a whole run: a dead judge must not silently produce a run
        # that looks like a completed liveness-only arm. `first20_calls` /
        # `first20_errors` count only the first `JUDGE_HEALTH_WINDOW` live
        # judge calls (jev/laya; `string-all` never calls a judge and is
        # exempt) — see `_check_judge_health`.
        self.first20_calls = 0
        self.first20_errors = 0

    def as_dict(self) -> dict:
        return {
            "judge_calls": self.calls,
            "judge_passes": self.passes,
            "judge_blocks": self.blocks,
            "judge_abstain_or_group": self.abstain_or,
            "judge_abstain_error": self.abstain_error,
            "judge_input_chars": self.input_chars,
            "judge_advisory_notes": self.advisory_notes,
            "judge_advisory_hard_blocks": self.advisory_hard_blocks,
            # ~4 chars/token; an estimate, stated as one
            "judge_est_cost_usd": round(self.input_chars / 4 / 1e6 * JEV_USD_PER_MTOK_INPUT, 6),
        }


STATS = JudgeStats()

# ── advisory mode: pass, but tell the executor ───────────────────────────────
#
# `--judge-gate-mode block` (default) is unchanged: a not_held verdict is a
# hard stop, same as every published judge-gate arm. `advisory` instead lets
# the step through and queues a short note — which condition looked unmet,
# Jev's confidence, and the evidence it saw — for the executor's NEXT prompt,
# UNLESS the verdict's own confidence clears `hard_block_confidence` (default
# 0.99, from N36: confident blocks were 96% correct on `hotel`, confident
# passes only 87% — so confidence is trustworthy on the block side and this is
# the one case advisory mode still treats as a hard stop).

GATE_MODE_BLOCK = "block"
GATE_MODE_ADVISORY = "advisory"
DEFAULT_HARD_BLOCK_CONFIDENCE = 0.99

# ── judge health: fail loud instead of silently degrading ────────────────────
#
# 2026-09-26: `api.typesafe.ai` went unreachable mid-programme (connect times
# out on both its Cloudflare IPs; `docs.typesafe.ai` and z.ai were both fine —
# a Jev-specific outage, not a network problem here). Every call abstained,
# and the checker's contract for one step — "cannot judge, fall back to
# liveness" (`make_judge_value_checker`'s docstring) — is correct in isolation
# and wrong for a whole run: a dead judge must not produce a multi-hour run
# that finishes exit 0 and looks like a completed liveness-only arm. Two
# checks, both fatal (`SystemExit`) rather than logged-and-continued:
#
#   preflight_judge     one real call before the first task. Cheap and fails
#                       in ~20s instead of the ~494s a dead endpoint's own
#                       retry/backoff ladder costs per constraint once the
#                       run is already under way.
#   judge health window running check on the first `JUDGE_HEALTH_WINDOW` live
#                       judge calls of the run: more than `JUDGE_HEALTH_MAX_
#                       ERROR_FRACTION` erroring aborts immediately. Catches an
#                       outage that starts (or a key that expires) AFTER
#                       preflight passed.

JUDGE_HEALTH_WINDOW = 20
JUDGE_HEALTH_MAX_ERROR_FRACTION = 0.20


def preflight_judge(mode: str, timeout: int = 20, ask_fn: Optional[Callable] = None) -> tuple[bool, str]:
    """One real, short-timeout call to the judge before a run starts.

    `string-all` calls no judge and always passes. `ask_fn` defaults to the
    real `jev_ask`/`laya_ask` (tests inject a stub taking the same
    `(body, timeout=, retries=)` shape). One attempt only (`retries=1`) — this
    is a reachability probe, not the checker's own retry ladder.
    """
    if mode == "string-all":
        return True, "string-all calls no judge; nothing to preflight"
    if mode not in ("jev", "laya"):
        raise ValueError(f"unknown judge gate {mode!r}")
    fn = ask_fn or (laya_ask if mode == "laya" else jev_ask)
    body = evidence_body((), {}, "preflight probe: is the judge reachable?")
    held, conf, raw = fn(body, timeout=timeout, retries=1)
    if held is None:
        return False, f"judge preflight failed ({mode}): {raw}"
    return True, f"judge preflight ok ({mode}): held={held} confidence={conf}"


def _check_judge_health(stats: "JudgeStats", mode: str) -> None:
    """Abort the run if the judge looks dead, rather than let it finish as an
    unannounced liveness-only arm. Called after every live (jev/laya) judge
    call; `first20_calls` stops advancing once the window fills, so this is
    idempotent from then on — either it tripped exactly at the window, or it
    never will."""
    max_errors = JUDGE_HEALTH_WINDOW * JUDGE_HEALTH_MAX_ERROR_FRACTION
    if stats.first20_calls >= JUDGE_HEALTH_WINDOW and stats.first20_errors > max_errors:
        raise SystemExit(
            f"--judge-gate {mode}: {stats.first20_errors}/{stats.first20_calls} of the "
            f"first {JUDGE_HEALTH_WINDOW} judge calls errored (> "
            f"{JUDGE_HEALTH_MAX_ERROR_FRACTION:.0%}). Aborting — a dead judge must not "
            "silently finish as a liveness-only run."
        )


# One process runs one conversation at a time (`run_simulation.main`'s task
# loop is a plain `for`, never threaded — see `sopbench_shard.py`'s docstring),
# so a module-level queue is safe: at most one task's notes are ever pending.
_PENDING_ADVISORY_NOTES: list[str] = []


def queue_advisory_note(note: str) -> None:
    _PENDING_ADVISORY_NOTES.append(note)


def drain_advisory_notes() -> list[str]:
    """Pop every queued note. Called once per prompt build so a note is shown
    exactly once — on the turn right after the judge that raised it — and
    never leaks into a later, unrelated step."""
    notes = list(_PENDING_ADVISORY_NOTES)
    _PENDING_ADVISORY_NOTES.clear()
    return notes


def format_advisory_note(constraint: str, condition: str, confidence: Optional[float],
                          evidence: str) -> str:
    conf_str = f"{confidence:.2f}" if confidence is not None else "unknown"
    return (f"- condition ({constraint}): {condition}\n"
            f"  judge confidence the condition does NOT hold: {conf_str}\n"
            f"  evidence the judge saw: {evidence!r}")


ADVISORY_BLOCK = """
<gate_advisory>
A verifier flagged something from a step you already completed as possibly
unmet. This is advisory only — that step was NOT refused and you were not
stopped — but weigh whether it needs attention before you finish:

{note}
</gate_advisory>
"""

_ADVISORY_HOOK_INSTALLED = False


def install_advisory_prompt_hook() -> None:
    """Make `ASOPEngine.system_prompt_for` splice queued advisory notes into
    the next prompt it builds.

    Advisory mode is pointless if the note only reaches the judge log — "the
    gate passes but tells the executor" is a claim about the executor's own
    context, so it has to land where the model's next prompt is actually
    assembled. `system_prompt_for` is that seam: it is where `REFUSAL_BLOCK`
    already gets spliced in for a hard block, called once per turn right after
    `run_gates` has judged the arriving evidence. This rides the same seam for
    a non-blocking note, as a runtime patch rather than an edit to
    `asop_engine.py` — that file is not this feature's lane. Idempotent: a
    second call within one process is a no-op.
    """
    global _ADVISORY_HOOK_INSTALLED
    if _ADVISORY_HOOK_INSTALLED:
        return
    import asop_engine as _ae

    _orig_system_prompt_for = _ae.ASOPEngine.system_prompt_for

    def _system_prompt_for_with_advisory(self, state, *a, **kw):
        text = _orig_system_prompt_for(self, state, *a, **kw)
        notes = drain_advisory_notes()
        if notes:
            text = ADVISORY_BLOCK.format(note="\n".join(notes)) + text
        return text

    _ae.ASOPEngine.system_prompt_for = _system_prompt_for_with_advisory
    _ADVISORY_HOOK_INSTALLED = True


def evidence_body(tool_history: tuple, user_known: dict, condition: str) -> str:
    """Evidence first, then the question — the ordering `judge_jev` settled on."""
    hist = "\n".join(tool_history) or "(no tool call has been made)"
    known = "\n".join(f"{k} = {v!r}" for k, v in (user_known or {}).items()) or "(nothing)"
    return (f"TOOL HISTORY:\n{hist}\n\nWHAT THE USER PROVIDED:\n{known}\n\n"
            f"CONDITION THAT MUST HOLD: {condition}")


def make_judge_value_checker(
    task: dict,
    tool_of: dict,
    mode: str,
    describe: Callable[[tuple, dict], str],
    truth: Any,
    tool_results: Callable[[tuple], dict],
    ask: Optional[Callable[[str], tuple[Optional[bool], float, str]]] = None,
    log_path: Optional[str] = None,
    stats: JudgeStats = STATS,
    gate_mode: str = GATE_MODE_BLOCK,
    hard_block_confidence: float = DEFAULT_HARD_BLOCK_CONFIDENCE,
) -> Callable[[Any, tuple], tuple[Optional[bool], str]]:
    """A `make_value_checker`-compatible checker backed by a judge.

    `truth` is a `_TaskTruth(task, respect_or=True)` — used ONLY for which
    constraints the task imposes, with polarity, and their OR groups; its
    bank-specific `holds()` is never called. `describe(leaf, dep_params)`
    verbalises one of the task's own `("single", name, arg_map)` leaves with the
    task's `constraint_parameters` (SOPBench's `get_dep_verb` in production).
    `ask(body)` defaults to `jev_ask`; tests pass a stub.

    `gate_mode="block"` (default) is unchanged: a not_held verdict is a hard
    stop. `gate_mode="advisory"`: a not_held verdict PASSES the gate and queues
    a note for the executor's next prompt (`queue_advisory_note`,
    `install_advisory_prompt_hook`) instead — unless its own confidence is `>=
    hard_block_confidence`, which still hard-stops (`string-all` has no
    confidence to compare, so it is always advisory-eligible under this mode).
    """
    if mode not in ("jev", "laya", "string-all"):
        raise ValueError(f"unknown judge gate {mode!r}")
    if gate_mode not in (GATE_MODE_BLOCK, GATE_MODE_ADVISORY):
        raise ValueError(f"unknown judge gate mode {gate_mode!r}")
    ask = ask or (laya_ask if mode == "laya" else jev_ask)
    params = task.get("constraint_parameters") or {}
    known = task.get("user_known") or {}
    goal = task.get("user_goal", "")
    leaves = task_leaves(task.get("constraints"))
    tkey = task_key(task)

    def log(entry: dict) -> None:
        if log_path:
            with open(log_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")

    def check(step: Any, tool_history: tuple) -> tuple[Optional[bool], str]:
        from asop_engine import named_tool

        tool = named_tool(step)
        if not tool:
            return None, ""
        relevant = [c for c in truth.wanted if tool_of.get(c) == tool]
        if not relevant:
            return None, ""
        judged: list[tuple[str, bool, str, Optional[float], str]] = []
        for c in relevant:
            want_true = truth.wanted[c]
            base = {"task_key": tkey, "goal": goal, "step": getattr(step, "number", None),
                    "step_title": getattr(step, "title", ""), "tool": tool,
                    "constraint": c, "polarity": want_true, "mode": mode,
                    "gate_mode": gate_mode}
            if len(truth.group_of(c)) > 1:
                stats.abstain_or += 1
                log({**base, "verdict": None, "why": "or-group: not judged"})
                continue
            name = c if want_true else f"not {c}"
            leaf = leaves.get(name, ("single", name, {}))
            condition = describe(leaf, params)
            body = evidence_body(tool_history, known, condition)
            sha = hashlib.sha1(body.encode()).hexdigest()[:12]
            conf: Optional[float] = None
            if mode in ("jev", "laya"):
                t0 = time.time()
                held, conf, raw = ask(body)
                stats.calls += 1
                if stats.first20_calls < JUDGE_HEALTH_WINDOW:
                    stats.first20_calls += 1
                    if held is None:
                        stats.first20_errors += 1
                _check_judge_health(stats, mode)
                if mode == "jev":  # Laya is local; only Jev costs anything
                    stats.input_chars += len(body)
                result_snippet = (tool_results(tool_history).get(tool) or "")[:200]
                entry = {**base, "verdict": held, "confidence": conf, "raw": raw,
                         "evidence_sha1": sha, "latency_s": round(time.time() - t0, 3),
                         "condition": condition, "result": result_snippet}
            else:
                result = tool_results(tool_history).get(tool)
                held = looks_truthy(result)
                result_snippet = (result or "")[:120]
                entry = {**base, "verdict": held, "result": result_snippet,
                         "evidence_sha1": sha, "condition": condition}
            # Per-verdict classification, independent of which constraint ends
            # up chosen as THE reason below — so a log reader (or a later
            # analysis of ignored-vs-acted-on notes) never has to re-derive it.
            is_not_held = held is False
            hard = is_not_held and (
                gate_mode == GATE_MODE_BLOCK
                or (conf is not None and conf >= hard_block_confidence)
            )
            entry["hard_stop"] = hard
            entry["advisory"] = is_not_held and not hard
            log(entry)
            if held is None:
                stats.abstain_error += 1
                continue
            judged.append((c, held, condition, conf, result_snippet))
        if not judged:
            return None, ""
        failed = [j for j in judged if not j[1]]
        if failed:
            stats.blocks += 1
            c, _, condition, conf, result_snippet = failed[0]
            reason = f"{c}: the evidence does not establish that {condition.rstrip('.')}"
            if gate_mode == GATE_MODE_ADVISORY and not (
                conf is not None and conf >= hard_block_confidence
            ):
                stats.advisory_notes += 1
                queue_advisory_note(format_advisory_note(c, condition, conf, result_snippet))
                return True, f"{c}: advisory only (judge said not held, confidence {conf})"
            stats.advisory_hard_blocks += 1 if gate_mode == GATE_MODE_ADVISORY else 0
            return False, reason
        stats.passes += 1
        return True, f"{judged[0][0]} judged satisfied ({mode})"

    return check


def task_key(task: dict) -> str:
    """A stable id for a task, from fields SOPBench never rewrites.

    SOPBench tasks carry no id and the checker never sees the task's position,
    so a verdict log is joined back to its task (and to ground truth) by this
    key: hash the same fields of every task in `data/<domain>_tasks.json`.
    Measured on `hotel`: goal + instruction + `user_known` + constraints give
    only 183 distinct keys for 195 tasks; adding `constraint_parameters` and
    `initial_database` gives 195. The database is HASHED into the key and
    never shown to any judge.
    """
    ident = json.dumps([task.get("user_goal"), task.get("user_instruction"),
                        task.get("user_known"), task.get("constraints"),
                        task.get("constraint_parameters"), task.get("initial_database")],
                       sort_keys=True, default=str)
    return hashlib.sha1(ident.encode()).hexdigest()[:16]


_WS = re.compile(r"\s+")


def task_leaves(tree: Any) -> dict:
    """`"name"` / `"not name"` -> the task's own `("single", name, arg_map)` leaf."""
    out: dict = {}

    def walk(n: Any) -> None:
        if not isinstance(n, (list, tuple)) or not n:
            return
        if n[0] == "single":
            out.setdefault(str(n[1]), tuple(n))
        elif n[0] in ("and", "or", "chain"):
            for c in n[1]:
                walk(c)
        else:
            for c in n:
                walk(c)

    walk(tree)
    return out


def describe_with_sopbench(domain: str) -> Callable[[tuple, dict], str]:
    """`describe` backed by SOPBench's own verbaliser — the text every arm's
    host prompt already carries for these constraints."""
    from env.task import get_dep_verb

    def describe(leaf: tuple, dep_params: dict) -> str:
        return _WS.sub(" ", get_dep_verb(domain, tuple(leaf), dep_params, "structured")).strip()

    return describe
