"""Reviser failure contract (EXP-L.md v3, "Reviser failure contract (ML
review #1)"): pre-flight, retry once on any failure, else "no candidate" —
never a silent substitution. Every input/output is frozen+hashed before
scoring. All network calls are stubbed here (`expl.reviser.chat` is
monkeypatched) — nothing in this file makes a real z.ai call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from expl import reviser as reviser_mod  # noqa: E402
from codebench.provider import ChatResult  # noqa: E402

GOOD_DOC = "## Preamble\n\n## Book Room\n\n1. **Check availability.** Gate: deterministic (tool call `show_available_rooms`)\n\n## Routing\n\nbook -> Book Room\n"
GOOD_DIFF_CONTENT = json.dumps({
    "diffs": [
        {"failure_type": "dirgraph", "search": "Check availability.", "replace": "Check availability first."}
    ]
})


class _ScriptedChat:
    """Returns each `ChatResult` in `script`, in order, one per call."""

    def __init__(self, script: list[ChatResult]):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def __call__(self, endpoint, messages, **kwargs):
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("chat() called more times than the test scripted")
        return self.script.pop(0)


def _ok(content: str) -> ChatResult:
    return ChatResult(ok=True, message={"role": "assistant", "content": content}, usage={"total_tokens": 10})


def _err(msg: str) -> ChatResult:
    return ChatResult(ok=False, error=msg)


def test_preflight_then_propose_succeeds_on_first_try(tmp_path, monkeypatch):
    scripted = _ScriptedChat([_ok("OK"), _ok(GOOD_DIFF_CONTENT)])
    monkeypatch.setattr(reviser_mod, "chat", scripted)
    rev = reviser_mod.Reviser(freeze_dir=tmp_path)

    outcome = reviser_mod.propose_with_contract(rev, GOOD_DOC, {}, "r1")
    assert outcome.ok is True
    assert len(outcome.diffs) == 1
    assert outcome.diffs[0].failure_type == "dirgraph"
    # frozen artifacts exist and are hashed BEFORE any scoring could happen
    assert (tmp_path / "r1_attempt0_input.json").exists()
    assert (tmp_path / "r1_attempt0_output.json").exists()
    assert outcome.input_hash and outcome.output_hash


def test_a_transient_call_error_is_retried_once_then_succeeds(tmp_path, monkeypatch):
    scripted = _ScriptedChat([_ok("OK"), _err("HTTP 500"), _ok("OK"), _ok(GOOD_DIFF_CONTENT)])
    monkeypatch.setattr(reviser_mod, "chat", scripted)
    rev = reviser_mod.Reviser(freeze_dir=tmp_path)

    outcome = reviser_mod.propose_with_contract(rev, GOOD_DOC, {}, "r2")
    assert outcome.ok is True
    assert len(scripted.calls) == 4  # preflight, propose(fail), preflight, propose(ok)


def test_two_failures_produce_no_candidate_never_a_substitution(tmp_path, monkeypatch):
    scripted = _ScriptedChat([_ok("OK"), _err("HTTP 500"), _ok("OK"), _err("HTTP 500")])
    monkeypatch.setattr(reviser_mod, "chat", scripted)
    rev = reviser_mod.Reviser(freeze_dir=tmp_path)

    outcome = reviser_mod.propose_with_contract(rev, GOOD_DOC, {}, "r3")
    assert outcome.ok is False
    assert outcome.diffs == []
    assert "no candidate" in outcome.error


def test_malformed_json_output_is_treated_as_a_failure(tmp_path, monkeypatch):
    scripted = _ScriptedChat([_ok("OK"), _ok("not json at all"), _ok("OK"), _ok("not json at all")])
    monkeypatch.setattr(reviser_mod, "chat", scripted)
    rev = reviser_mod.Reviser(freeze_dir=tmp_path)

    outcome = reviser_mod.propose_with_contract(rev, GOOD_DOC, {}, "r4")
    assert outcome.ok is False


def test_a_failing_preflight_never_even_calls_propose(tmp_path, monkeypatch):
    scripted = _ScriptedChat([_err("unreachable"), _err("unreachable")])
    monkeypatch.setattr(reviser_mod, "chat", scripted)
    rev = reviser_mod.Reviser(freeze_dir=tmp_path)

    outcome = reviser_mod.propose_with_contract(rev, GOOD_DOC, {}, "r5")
    assert outcome.ok is False
    assert len(scripted.calls) == 2  # only the two preflight attempts, never propose


def test_outer_pipeline_retries_on_a_self_check_failure(tmp_path, monkeypatch):
    """The FULL pipeline (preflight+propose+apply+leak+self-check) gets one
    retry as a unit, per the contract — a self-check failure on attempt 0
    must not surface as a permanent "no candidate" if attempt 1 passes.
    """
    from expl import loop as loop_mod
    from expl.diffing import Diff

    # attempt0: reviser proposes a diff whose search text is not in the
    # document (hallucinated) -> apply fails. attempt1: a good diff.
    bad_content = json.dumps({"diffs": [{"failure_type": "dirgraph", "search": "NOT IN DOC", "replace": "x"}]})
    scripted = _ScriptedChat([_ok("OK"), _ok(bad_content), _ok("OK"), _ok(GOOD_DIFF_CONTENT)])
    monkeypatch.setattr(reviser_mod, "chat", scripted)

    class _FakeTables:
        exposed = {"show_available_rooms"}

        def prereq_closure(self, tool):
            return set()

    monkeypatch.setattr(loop_mod, "self_check_candidate", lambda doc, tables: [])
    rev = reviser_mod.Reviser(freeze_dir=tmp_path)
    edit_records = {}  # empty failure set is fine — this test is about the retry, not grouping
    result = loop_mod.attempt_candidate(rev, _FakeTables(), GOOD_DOC, edit_records, "r6")
    assert result["ok"] is True
    assert result["applied_diffs"] == ["dirgraph"]
    assert len(result["attempts"]) == 1  # one recorded failure (attempt 0), then success on attempt 1
