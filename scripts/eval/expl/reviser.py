"""The reviser: GLM-4.7 via z.ai, and the failure contract around it
(EXP-L.md v3, "Reviser failure contract (ML review #1)"):

    pre-flight the z.ai reviser before each round; if a call errors or its
    output fails the self-check, retry once, then record the round as "no
    candidate" (B unchanged) — never substitute silently. Every reviser
    call's raw input and output is frozen and hashed BEFORE any scoring.

What the reviser is shown (EXP-L.md, "Reviser input"): the current document,
plus for each failed EDIT task: the user request, the executor's tool calls,
and which SOPBench success conjunct failed. It is NEVER shown the domain's
dependency tables, action graph, task constraints, or any truth function —
`build_prompt` below is the one place that boundary is enforced, by
construction (it only ever reads the fields listed above off a task record).

Uses `codebench.provider` for the actual HTTP call (z.ai's OpenAI-format
endpoint, already built and smoke-tested for this repo) rather than a new
client — this module only adds the ASOP-specific prompt, the retry/freeze
contract, and diff parsing.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_THIS_DIR = Path(__file__).resolve().parents[1]  # scripts/eval
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from codebench.provider import Endpoint, ZAI_CODING, chat  # noqa: E402

from expl.diffing import Diff  # noqa: E402

MAX_DIFFS_PER_ROUND = 3
MAX_EXAMPLES_PER_FAILURE_TYPE = 5


def freeze_and_hash(freeze_dir: Optional[Path], label: str, payload: dict) -> str:
    """Write `payload` as JSON to `freeze_dir/label.json` (if a dir is given)
    and return its sha256 hex digest. Called BEFORE any scoring happens, on
    both the input sent to the reviser and the raw output it returned, per
    the pre-registered contract.
    """
    blob = json.dumps(payload, indent=2, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    if freeze_dir is not None:
        freeze_dir.mkdir(parents=True, exist_ok=True)
        (freeze_dir / f"{label}.json").write_text(blob)
        (freeze_dir / f"{label}.sha256").write_text(digest + "\n")
    return digest


def _task_view(pos: int, rec: dict) -> dict:
    """Exactly the fields the reviser is allowed to see for one failed task."""
    return {
        "position": pos,
        "user_request": rec.get("user_prompt", ""),
        "tool_calls": [
            {"tool_name": tc.get("tool_name"), "arguments": tc.get("arguments")}
            for tc in rec.get("tool_calls", [])
        ],
        "failed_conjunct": rec.get("failed_conjunct"),
    }


def build_prompt(document: str, grouped_failures: dict[str, list[tuple[int, dict]]], blind: bool = False) -> str:
    if blind:
        return (
            "You are revising a Standard Operating Procedure (SOP) document that an AI "
            "agent follows step by step to complete customer-service tasks.\n\n"
            "Improve the CLARITY of this document only — wording, structure, ambiguity — "
            "with NO information about how it performed on any task. Do not change what any "
            "step does or which tool it names; only make it easier to follow.\n\n"
            "Propose up to 3 diffs. Return ONLY a JSON object of this exact shape, no "
            "other text:\n"
            '{"diffs": [{"failure_type": "clarity", "search": "<verbatim text copied from '
            'the document, unique>", "replace": "<its replacement>"}]}\n\n'
            "# Document\n\n" + document
        )

    sections = []
    # Deterministic order: CONJUNCT_PRIORITY order, capped to the ≤3 groups
    # the caller selected (loop.py picks the top groups before calling this).
    for failure_type, examples in grouped_failures.items():
        lines = [f"## Failures of type: {failure_type} ({len(examples)} in this round)"]
        for pos, rec in examples[:MAX_EXAMPLES_PER_FAILURE_TYPE]:
            view = _task_view(pos, rec)
            lines.append(json.dumps(view, indent=2, default=str))
        sections.append("\n".join(lines))
    failures_block = "\n\n".join(sections)

    return (
        "You are revising a Standard Operating Procedure (SOP) document that an AI agent "
        "follows step by step to complete customer-service tasks. Below are examples of "
        "tasks where following the CURRENT document led to a failure, grouped by which "
        "check failed:\n\n"
        "  - dirgraph: a tool was called before a tool it depends on\n"
        "  - constraint: a business rule/constraint was violated\n"
        "  - database: the final state of the system didn't match what the task required\n"
        "  - action_called_correctly: the wrong action was taken (or none) for the request\n"
        "  - tool_error: a tool call itself errored out\n\n"
        "You do NOT have access to the domain's dependency graph, constraint tables, or any "
        "grading logic — only the document, and what the agent visibly did.\n\n"
        "Propose up to 3 targeted diffs to the document, each one tagged with the failure "
        "type it targets (pick the highest-impact failure types if there are more than 3). "
        "Each diff must copy `search` VERBATIM from the document below (must appear exactly "
        "once) and give its `replace` text. Do NOT include any names, dates, dollar amounts, "
        "or IDs from the example tasks in your `replace` text — fix the general procedure, "
        "not one example.\n\n"
        'Return ONLY a JSON object of this exact shape, no other text:\n'
        '{"diffs": [{"failure_type": "...", "search": "...", "replace": "..."}]}\n\n'
        "# Failures\n\n" + failures_block + "\n\n# Document\n\n" + document
    )


def parse_diffs(content: str) -> tuple[list[Diff], Optional[str]]:
    text = content.strip()
    # Models like to wrap JSON in a fenced code block despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"reviser output was not valid JSON: {exc}"
    diffs_raw = payload.get("diffs") if isinstance(payload, dict) else None
    if not isinstance(diffs_raw, list) or not diffs_raw:
        return [], "reviser output had no non-empty 'diffs' list"
    diffs: list[Diff] = []
    for i, d in enumerate(diffs_raw[:MAX_DIFFS_PER_ROUND]):
        if not isinstance(d, dict) or not {"failure_type", "search", "replace"} <= d.keys():
            return [], f"diffs[{i}] missing required keys (failure_type/search/replace)"
        diffs.append(Diff(str(d["failure_type"]), str(d["search"]), str(d["replace"])))
    return diffs, None


@dataclass
class ReviserOutcome:
    ok: bool
    diffs: list[Diff] = field(default_factory=list)
    input_hash: str = ""
    output_hash: str = ""
    error: Optional[str] = None
    usage: dict = field(default_factory=dict)


class Reviser:
    def __init__(self, endpoint: Endpoint = ZAI_CODING, freeze_dir: Optional[Path] = None):
        self.endpoint = endpoint
        self.freeze_dir = freeze_dir

    def preflight(self) -> tuple[bool, str]:
        """A cheap call to confirm the endpoint answers before spending a round on it.

        GLM-4.7 is a reasoning model: it always emits `reasoning_content`
        before `content`, and a too-small `max_tokens` gets spent entirely on
        reasoning, leaving `content` empty with `finish_reason: "length"` —
        that looked like a broken endpoint the first two times this ran live
        (once at 8 tokens, once again at 64 — the reasoning length is not
        fixed). 300 is a wide enough margin that only a genuinely unreachable
        or misbehaving endpoint should fail this.
        """
        result = chat(
            self.endpoint,
            [{"role": "user", "content": "Reply with exactly the word: OK"}],
            max_tokens=300,
            temperature=0.0,
        )
        if not result.ok:
            return False, result.error or "unknown error"
        content = (result.message or {}).get("content", "") or ""
        if "OK" not in content.upper():
            return False, f"unexpected preflight reply: {content!r}"
        return True, ""

    def propose(
        self,
        document: str,
        grouped_failures: dict[str, list[tuple[int, dict]]],
        round_label: str,
        blind: bool = False,
    ) -> ReviserOutcome:
        prompt = build_prompt(document, grouped_failures, blind=blind)
        raw_input = {
            "round": round_label,
            "blind": blind,
            "model": self.endpoint.model,
            "prompt": prompt,
        }
        input_hash = freeze_and_hash(self.freeze_dir, f"{round_label}_input", raw_input)

        result = chat(
            self.endpoint,
            [{"role": "user", "content": prompt}],
            # GLM-4.7 is a reasoning model: it visibly deliberates (verifying
            # each `search` string's uniqueness, weighing which failure types
            # to target) in `reasoning_content` before ever writing `content`.
            # 4096 total was measured live to be spent entirely on reasoning,
            # with `content` still empty and finish_reason "length" — this
            # headroom is sized off that measurement, not a guess.
            max_tokens=16384,
            temperature=0.2,
            # Measured live: 4096 tokens of pure reasoning took ~78s (~52
            # tok/s) with `content` still empty. `codebench.provider`'s
            # DEFAULT_TIMEOUT_S (120s) is sized for its own smaller calls;
            # 16384 tokens at this model's measured pace needs real headroom.
            timeout=900,
        )
        raw_output = {"ok": result.ok, "message": result.message, "usage": result.usage, "error": result.error}
        output_hash = freeze_and_hash(self.freeze_dir, f"{round_label}_output", raw_output)

        if not result.ok:
            return ReviserOutcome(
                ok=False, input_hash=input_hash, output_hash=output_hash, error=result.error, usage=result.usage
            )

        content = (result.message or {}).get("content", "") or ""
        diffs, err = parse_diffs(content)
        if err:
            return ReviserOutcome(
                ok=False, input_hash=input_hash, output_hash=output_hash, error=err, usage=result.usage
            )
        return ReviserOutcome(
            ok=True, diffs=diffs, input_hash=input_hash, output_hash=output_hash, usage=result.usage
        )


def propose_with_contract(
    reviser: Reviser,
    document: str,
    grouped_failures: dict[str, list[tuple[int, dict]]],
    round_label: str,
    blind: bool = False,
) -> ReviserOutcome:
    """Pre-flight, then propose; on any failure, retry ONCE; otherwise "no
    candidate". Matches EXP-L.md's reviser failure contract verbatim — this
    function is the single choke point loop.py calls, so there is exactly one
    path that can produce a candidate diff set, mirroring the repo's own
    "gate is the contract, not a suggestion" convention.
    """
    last: Optional[ReviserOutcome] = None
    for attempt in range(2):
        ok, preflight_err = reviser.preflight()
        if not ok:
            last = ReviserOutcome(ok=False, error=f"preflight failed: {preflight_err}")
            continue
        outcome = reviser.propose(document, grouped_failures, f"{round_label}_attempt{attempt}", blind=blind)
        if outcome.ok:
            return outcome
        last = outcome
    assert last is not None
    return ReviserOutcome(
        ok=False,
        input_hash=last.input_hash,
        output_hash=last.output_hash,
        error=f"no candidate after 1 retry: {last.error}",
        usage=last.usage,
    )
