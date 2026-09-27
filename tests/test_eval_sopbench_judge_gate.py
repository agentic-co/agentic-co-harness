"""Tests for the judge-backed value gate and the non-local executor wiring.

Synthetic data and a stubbed judge only: no network, no SOPBench checkout. The
properties pinned are the ones whose failure would silently change what the
gate test measures — which conditions are judged, with what polarity, what the
judge is shown, when it blocks, and that nothing reaches the judge it is not
allowed to see.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "eval"))

import asop_engine as E  # noqa: E402
import sopbench_judge_gate as G  # noqa: E402


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_sopbench_asop", _ROOT / "scripts" / "eval" / "run_sopbench_asop.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_sopbench_asop"] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load_runner()

DOC = """\
Compiled procedure document.

## Routing

| The user wants to... | Procedure |
| --- | --- |
| book room | Book Room |

## Procedure: Book Room

1. **room available.** Condition `room_type_available_for_dates`. VERIFY: call the show_available_rooms tool. Gate: deterministic (tool call: `show_available_rooms`)
2. **no overlap.** Condition `has_overlapping_booking_for_booking`. VERIFY: call the find_booking_info tool. Gate: deterministic (tool call: `find_booking_info`)
3. **loyalty.** Condition `internal_is_loyalty_member`. VERIFY: call the internal_is_loyalty_member tool. Gate: deterministic (tool call: `internal_is_loyalty_member`)
4. **Book Room — the requested action.** ACT: call the book_room tool. Gate: deterministic (tool call: `book_room`)
"""

TOOL_OF = {
    "room_type_available_for_dates": "show_available_rooms",
    "has_overlapping_booking_for_booking": "find_booking_info",
    "internal_is_loyalty_member": "internal_is_loyalty_member",
    "amount_positive_restr": "internal_is_loyalty_member",  # an OR sibling, same tool
}

TASK = {
    "user_goal": "book_room",
    "constraint_parameters": {"max_stays": 4},
    "user_known": {"guest_name": "alex", "room_type": "suite"},
    # gold that must never reach the judge
    "action_should_succeed": True,
    "initial_database": {"secret": "GOLD-DB"},
    "constraints": [
        "and",
        [
            ["single", "room_type_available_for_dates", {"room_type": "room_type"}],
            ["single", "not has_overlapping_booking_for_booking", {"guest_name": "guest_name"}],
            [
                "or",
                [
                    ["single", "internal_is_loyalty_member", {"guest_name": "guest_name"}],
                    ["single", "amount_positive_restr", {"amount": "amount"}],
                ],
            ],
        ],
    ],
}


def _step(n: int):
    return E.parse_asop(DOC).procedure("Book Room").steps[n - 1]


def _describe(leaf, params):
    return f"<{leaf[1]}|{sorted(leaf[2])}|{params.get('max_stays')}>"


def _hist(tool: str, result: str) -> tuple:
    return (f"called {tool}(guest_name='alex')", f"  -> ok: {result}")


class _Judge:
    def __init__(self, answer):
        self.answer, self.bodies = answer, []

    def __call__(self, body):
        self.bodies.append(body)
        return self.answer, 0.9, "stub"


def _checker(mode, judge=None, log=None, stats=None):
    return G.make_judge_value_checker(
        TASK, TOOL_OF, mode, describe=_describe,
        truth=R._TaskTruth(TASK, respect_or=True), tool_results=R._tool_results,
        ask=judge, log_path=log, stats=stats or G.JudgeStats(),
    )


# ── the checker ─────────────────────────────────────────────────────────────


def test_jev_pass_and_block_follow_the_judge():
    ok = _checker("jev", _Judge(True))(_step(1), _hist("show_available_rooms", "['101']"))
    assert ok[0] is True
    no = _checker("jev", _Judge(False))(_step(1), _hist("show_available_rooms", "[]"))
    assert no[0] is False and "room_type_available_for_dates" in no[1]


def test_judge_sees_polarity_the_tasks_own_arg_map_and_parameters_but_no_gold():
    judge = _Judge(True)
    _checker("jev", judge)(_step(2), _hist("find_booking_info", "[]"))
    (body,) = judge.bodies
    # negated leaf, with the task's own arg map and constraint_parameters
    assert "<not has_overlapping_booking_for_booking|['guest_name']|4>" in body
    assert "guest_name = 'alex'" in body  # user_known, which _TaskTruth also reads
    assert "called find_booking_info" in body
    for gold in ("GOLD-DB", "action_should_succeed", "initial_database"):
        assert gold not in body


def test_or_group_member_is_not_judged():
    judge, stats = _Judge(False), G.JudgeStats()
    out = _checker("jev", judge, stats=stats)(_step(3), _hist("internal_is_loyalty_member", "False"))
    assert out == (None, "") and judge.bodies == [] and stats.abstain_or == 2


def test_step_with_no_task_constraint_falls_back_to_liveness():
    judge = _Judge(False)
    assert _checker("jev", judge)(_step(4), _hist("book_room", "True")) == (None, "")
    assert judge.bodies == []


def test_judge_error_abstains_rather_than_blocks():
    stats = G.JudgeStats()
    out = _checker("jev", _Judge(None), stats=stats)(_step(1), _hist("show_available_rooms", "x"))
    assert out == (None, "") and stats.abstain_error == 1


def test_string_all_is_truthiness_only_and_never_calls_a_judge():
    judge = _Judge(True)
    c = G.make_judge_value_checker(
        TASK, TOOL_OF, "string-all", describe=_describe,
        truth=R._TaskTruth(TASK, respect_or=True), tool_results=R._tool_results,
        ask=judge, stats=G.JudgeStats())
    assert c(_step(1), _hist("show_available_rooms", "['101']"))[0] is True
    assert c(_step(1), _hist("show_available_rooms", "[]"))[0] is False
    # blind to polarity: False is what `not has_overlapping...` wants, and it still refuses
    assert c(_step(2), _hist("find_booking_info", "False"))[0] is False
    assert judge.bodies == []


def test_verdict_log_records_what_grading_needs(tmp_path):
    log = tmp_path / "v.jsonl"
    _checker("jev", _Judge(False), log=str(log))(_step(1), _hist("show_available_rooms", "[]"))
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    for key in ("task_key", "goal", "step", "tool", "constraint", "polarity", "verdict",
                "evidence_sha1", "result"):
        assert key in entry
    assert entry["task_key"] == G.task_key(TASK) and entry["result"] == "[]"
    assert entry["verdict"] is False and entry["constraint"] == "room_type_available_for_dates"


def test_stats_count_calls_blocks_and_cost():
    stats = G.JudgeStats()
    _checker("jev", _Judge(False), stats=stats)(_step(1), _hist("show_available_rooms", "[]"))
    d = stats.as_dict()
    assert d["judge_calls"] == 1 and d["judge_blocks"] == 1 and d["judge_input_chars"] > 0
    assert d["judge_est_cost_usd"] > 0


def test_laya_mode_uses_the_same_checker_and_costs_nothing():
    stats, judge = G.JudgeStats(), _Judge(False)
    out = _checker("laya", judge, stats=stats)(_step(1), _hist("show_available_rooms", "[]"))
    assert out[0] is False and len(judge.bodies) == 1
    assert stats.calls == 1 and stats.input_chars == 0  # local judge: no spend counted


def test_laya_ask_sends_judge_layas_state_shape_and_no_model(monkeypatch):
    sent = {}

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

    def fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))
        import io

        return io.BytesIO(json.dumps({"answers": {"verdict": {"choice": "not_held",
                                                               "confidence": 0.7}}}).encode())

    monkeypatch.setattr(G.urllib.request, "urlopen", fake_urlopen)
    held, conf, _ = G.laya_ask("EVIDENCE", url="http://127.0.0.1:1/v1/systemone")
    assert held is False and conf == 0.7
    assert sent["state"] == {"body": "EVIDENCE"} and "model" not in sent


def test_laya_ask_abstains_when_the_server_is_down(monkeypatch):
    def boom(req, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(G.urllib.request, "urlopen", boom)
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    held, _, raw = G.laya_ask("x", url="http://127.0.0.1:1/v1/systemone")
    assert held is None and "refused" in raw


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        _checker("regex")


def test_unknown_gate_mode_is_refused():
    with pytest.raises(ValueError):
        G.make_judge_value_checker(
            TASK, TOOL_OF, "jev", describe=_describe,
            truth=R._TaskTruth(TASK, respect_or=True), tool_results=R._tool_results,
            ask=_Judge(True), stats=G.JudgeStats(), gate_mode="ignore-it",
        )


# ── advisory mode (--judge-gate-mode advisory) ───────────────────────────────


def _advisory_checker(judge, stats=None, hard_block_confidence=0.99):
    return G.make_judge_value_checker(
        TASK, TOOL_OF, "jev", describe=_describe,
        truth=R._TaskTruth(TASK, respect_or=True), tool_results=R._tool_results,
        ask=judge, stats=stats or G.JudgeStats(),
        gate_mode=G.GATE_MODE_ADVISORY, hard_block_confidence=hard_block_confidence,
    )


def test_advisory_mode_held_passes_silently(monkeypatch):
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()
    out = _advisory_checker(_Judge(True), stats=stats)(
        _step(1), _hist("show_available_rooms", "['101']")
    )
    assert out[0] is True
    assert stats.advisory_notes == 0 and stats.advisory_hard_blocks == 0
    assert G.drain_advisory_notes() == []


def test_advisory_mode_not_held_below_threshold_passes_with_a_queued_note(monkeypatch):
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()

    class Judge90:
        def __call__(self, body):
            return False, 0.90, "stub"

    out = _advisory_checker(Judge90(), stats=stats)(
        _step(1), _hist("show_available_rooms", "[]")
    )
    assert out[0] is True  # the gate PASSES
    assert stats.advisory_notes == 1 and stats.advisory_hard_blocks == 0
    (note,) = G.drain_advisory_notes()
    assert "room_type_available_for_dates" in note and "0.90" in note


def test_advisory_mode_not_held_at_or_above_threshold_still_hard_stops(monkeypatch):
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()

    class Judge99:
        def __call__(self, body):
            return False, 0.99, "stub"

    out = _advisory_checker(Judge99(), stats=stats)(
        _step(1), _hist("show_available_rooms", "[]")
    )
    assert out[0] is False and "room_type_available_for_dates" in out[1]
    assert stats.advisory_hard_blocks == 1 and stats.advisory_notes == 0
    assert G.drain_advisory_notes() == []  # nothing queued — this was a hard stop


def test_advisory_mode_custom_threshold_is_respected(monkeypatch):
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()
    out = _advisory_checker(_Judge(False), stats=stats, hard_block_confidence=0.5)(
        _step(1), _hist("show_available_rooms", "[]")
    )
    # _Judge always answers confidence 0.9, which now clears the lowered bar
    assert out[0] is False and stats.advisory_hard_blocks == 1


def test_block_mode_is_unchanged_default_behaviour(monkeypatch):
    """`gate_mode` defaults to block — every existing (pre-advisory) call site
    keeps hard-stopping regardless of confidence, and nothing is queued."""
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()
    out = _checker("jev", _Judge(False), stats=stats)(
        _step(1), _hist("show_available_rooms", "[]")
    )
    assert out[0] is False and stats.advisory_notes == 0 and stats.advisory_hard_blocks == 0
    assert G.drain_advisory_notes() == []


def test_verdict_log_records_gate_mode_and_hard_stop_classification(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    log = tmp_path / "v.jsonl"

    class Judge90:
        def __call__(self, body):
            return False, 0.90, "stub"

    G.make_judge_value_checker(
        TASK, TOOL_OF, "jev", describe=_describe,
        truth=R._TaskTruth(TASK, respect_or=True), tool_results=R._tool_results,
        ask=Judge90(), log_path=str(log), stats=G.JudgeStats(),
        gate_mode=G.GATE_MODE_ADVISORY,
    )(_step(1), _hist("show_available_rooms", "[]"))
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["gate_mode"] == "advisory"
    assert entry["advisory"] is True and entry["hard_stop"] is False


def test_string_all_has_no_confidence_and_is_always_advisory_eligible(monkeypatch):
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()
    c = G.make_judge_value_checker(
        TASK, TOOL_OF, "string-all", describe=_describe,
        truth=R._TaskTruth(TASK, respect_or=True), tool_results=R._tool_results,
        ask=_Judge(True), stats=stats, gate_mode=G.GATE_MODE_ADVISORY,
    )
    out = c(_step(1), _hist("show_available_rooms", "[]"))
    assert out[0] is True and stats.advisory_notes == 1 and stats.advisory_hard_blocks == 0


def test_advisory_prompt_hook_splices_and_drains_pending_notes(monkeypatch):
    calls = []

    def fake_system_prompt_for(self, state):
        calls.append(state)
        return "BASE PROMPT"

    monkeypatch.setattr(E.ASOPEngine, "system_prompt_for", fake_system_prompt_for)
    monkeypatch.setattr(G, "_ADVISORY_HOOK_INSTALLED", False)
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])

    G.install_advisory_prompt_hook()
    G.queue_advisory_note("a queued note")

    text = E.ASOPEngine.system_prompt_for(object(), "state-1")
    assert "a queued note" in text and text.endswith("BASE PROMPT")
    assert calls == ["state-1"]

    # drained: the following prompt build carries no leftover note
    text2 = E.ASOPEngine.system_prompt_for(object(), "state-2")
    assert text2 == "BASE PROMPT"


# ── judge health: preflight + the first-20-calls abort ───────────────────────


def test_preflight_judge_ok(monkeypatch):
    def ok(body, **kw):
        assert kw == {"timeout": 20, "retries": 1}
        return True, 0.9, "raw"

    passed, msg = G.preflight_judge("jev", ask_fn=ok)
    assert passed is True and "held=True" in msg


def test_preflight_judge_failure_is_reported_not_raised():
    def dead(body, **kw):
        return None, 0.0, "ERROR URLError: <urlopen error timed out>"

    passed, msg = G.preflight_judge("jev", ask_fn=dead)
    assert passed is False and "timed out" in msg


def test_preflight_judge_respects_custom_timeout():
    seen = {}

    def probe(body, **kw):
        seen.update(kw)
        return True, 1.0, "raw"

    G.preflight_judge("laya", timeout=5, ask_fn=probe)
    assert seen == {"timeout": 5, "retries": 1}


def test_preflight_judge_skips_string_all():
    calls = []
    G.preflight_judge("string-all", ask_fn=lambda body, **kw: calls.append(1))
    assert calls == []  # never invoked


def test_preflight_judge_rejects_unknown_mode():
    with pytest.raises(ValueError):
        G.preflight_judge("regex")


def test_judge_health_window_does_not_trip_at_the_boundary():
    stats = G.JudgeStats()
    stats.first20_calls, stats.first20_errors = 20, 4  # exactly 20% — not > 20%
    G._check_judge_health(stats, "jev")  # must not raise


def test_judge_health_window_trips_just_over_the_boundary():
    stats = G.JudgeStats()
    stats.first20_calls, stats.first20_errors = 20, 5  # > 20%
    with pytest.raises(SystemExit, match="5/20"):
        G._check_judge_health(stats, "jev")


def test_judge_health_window_ignores_errors_after_the_window_closes():
    stats = G.JudgeStats()
    stats.first20_calls, stats.first20_errors = 20, 4
    G._check_judge_health(stats, "jev")  # fine at the boundary
    # a run's 21st+ call never advances first20_calls (checker-side guard), so
    # a healthy run that later has a rough patch cannot retroactively trip this


def test_a_dead_judge_aborts_the_run_after_the_health_window(monkeypatch):
    """Integration: repeatedly calling the real checker with an always-erroring
    judge raises SystemExit once the first-20-calls window closes unhealthy —
    the mechanism that stops a run from finishing as an unannounced
    liveness-only arm."""
    monkeypatch.setattr(G, "_PENDING_ADVISORY_NOTES", [])
    stats = G.JudgeStats()
    checker = _checker("jev", _Judge(None), stats=stats)
    with pytest.raises(SystemExit, match="20/20"):
        for _ in range(20):
            checker(_step(1), _hist("show_available_rooms", "[]"))
    assert stats.first20_calls == 20 and stats.first20_errors == 20


def test_advisory_prompt_hook_install_is_idempotent(monkeypatch):
    # Register the class attribute with monkeypatch BEFORE mutating it, so its
    # real (unpatched) value is restored at teardown regardless of how many
    # times `install_advisory_prompt_hook` reassigns it inside this test —
    # that reassignment is a direct class mutation, not something monkeypatch
    # would otherwise know to undo.
    monkeypatch.setattr(E.ASOPEngine, "system_prompt_for", E.ASOPEngine.system_prompt_for)
    monkeypatch.setattr(G, "_ADVISORY_HOOK_INSTALLED", False)
    G.install_advisory_prompt_hook()
    patched_once = E.ASOPEngine.system_prompt_for
    G.install_advisory_prompt_hook()
    assert E.ASOPEngine.system_prompt_for is patched_once


def test_task_key_is_stable_and_distinguishes_tasks():
    other = dict(TASK, user_known={"guest_name": "alex", "room_type": "double"})
    assert G.task_key(TASK) == G.task_key(json.loads(json.dumps(TASK)))
    assert G.task_key(TASK) != G.task_key(other)


def test_looks_truthy():
    for v in ("True", "['101']", "42", "confirmed"):
        assert G.looks_truthy(v)
    for v in (None, "", "False", "none", "0", "[]", "{}", "Error: no such guest"):
        assert not G.looks_truthy(v)


def test_task_leaves_keep_polarity_and_arg_map():
    leaves = G.task_leaves(TASK["constraints"])
    assert leaves["not has_overlapping_booking_for_booking"][2] == {"guest_name": "guest_name"}
    assert "amount_positive_restr" in leaves


# ── backend wiring ──────────────────────────────────────────────────────────


def test_register_model_mutates_the_list_objects_handlers_import(monkeypatch):
    openai_models = ["gpt-4o"]
    fc = {"openai": ["gpt-4o"]}
    swarm = types.ModuleType("swarm")
    constants = types.ModuleType("swarm.constants")
    constants.OPENAI_MODELS, constants.FUNCTION_CALLING_MODELS = openai_models, fc
    swarm.constants = constants
    monkeypatch.setitem(sys.modules, "swarm", swarm)
    monkeypatch.setitem(sys.modules, "swarm.constants", constants)
    G.register_model("glm-4.7")
    G.register_model("glm-4.7")  # idempotent
    assert openai_models == ["gpt-4o", "glm-4.7"]  # same object llm_handler imported
    assert fc["openai"] == ["gpt-4o", "glm-4.7"]


def test_use_zai_sets_the_coding_endpoint_and_key_without_returning_it(monkeypatch):
    import os

    import openai

    # use_zai patches the client class process-wide; restore it after this test
    monkeypatch.setattr(openai.OpenAI, "__init__", openai.OpenAI.__init__)
    monkeypatch.setenv("ZAI_API_KEY", "test-key-alex")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    url = G.use_zai()
    assert url == "https://api.z.ai/api/coding/paas/v4" == os.environ["OPENAI_BASE_URL"]
    assert os.environ["OPENAI_API_KEY"] == "test-key-alex" and "test-key" not in url
    # a 429 burst must back off, not fall through to SOPBench's temperature-0.7 task retry
    assert openai.OpenAI(api_key="k").max_retries == G.ZAI_MAX_RETRIES
    assert openai.OpenAI(api_key="k", max_retries=1).max_retries == 1


def test_router_uses_the_api_key_from_env(monkeypatch):
    seen = {}

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            seen.update(base_url=base_url, api_key=api_key)

    fake = types.ModuleType("openai")
    fake.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "k-alex")
    R.make_llm_router(E.parse_asop(DOC), "glm-4.7", "https://example.invalid/v1")
    assert seen == {"base_url": "https://example.invalid/v1", "api_key": "k-alex"}
    monkeypatch.delenv("OPENAI_API_KEY")
    R.make_llm_router(E.parse_asop(DOC), "m", "http://localhost:1/v1")
    assert seen["api_key"] == "placeholder"


def test_router_role_defaults_to_system_and_zai_can_send_user(monkeypatch):
    sent = []

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

        def _create(self, **kw):
            sent.append(kw["messages"][0]["role"])
            msg = types.SimpleNamespace(content="Book Room")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    fake = types.ModuleType("openai")
    fake.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    asop = E.parse_asop(DOC)
    assert R.make_llm_router(asop, "m")("p") == "Book Room"
    R.make_llm_router(asop, "m", role="user")("p")
    assert sent == ["system", "user"]


def test_ladder_argv_is_the_asop_runners_plus_one_flag():
    spec = importlib.util.spec_from_file_location(
        "run_sopbench_ladder", _ROOT / "scripts" / "eval" / "run_sopbench_ladder.py")
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    ns = types.SimpleNamespace(domain="hotel", model="glm-4.7", output_dir=Path("/x"),
                               limit=0, max_turns=0, max_actions=0, arm="pva")
    assert L.ladder_argv(ns) == R.build_argv(ns) + ["--scaffold", "pva"]
    ns.arm = "none"
    assert L.ladder_argv(ns) == R.build_argv(ns)


# ── grading ─────────────────────────────────────────────────────────────────


def test_grader_counts_correct_and_wrong_blocks(tmp_path, monkeypatch):
    import grade_judge_gate as GR

    key = G.task_key(TASK)
    monkeypatch.setattr(GR, "load_tasks", lambda sb, d: {key: (0, TASK)})
    truth = {"room_type_available_for_dates": False,
             "not has_overlapping_booking_for_booking": True}
    monkeypatch.setattr(GR, "truth_of", lambda d, t, name, arg_map: truth[name])
    rows = [
        {"task_key": key, "goal": "book_room", "constraint": "room_type_available_for_dates",
         "polarity": True, "verdict": False},   # correct block
        {"task_key": key, "goal": "book_room", "constraint": "has_overlapping_booking_for_booking",
         "polarity": False, "verdict": False},  # wrong block: the condition holds
        {"task_key": key, "goal": "book_room", "constraint": "room_type_available_for_dates",
         "polarity": True, "verdict": True},    # wrong pass
        {"task_key": key, "goal": "book_room", "constraint": "x", "polarity": True,
         "verdict": None},                      # abstained
        {"goal": "book_room", "constraint": "x", "polarity": True, "verdict": True},
    ]
    log = tmp_path / "v.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows))
    out = GR.grade("hotel", tmp_path, log)
    assert out["blocks_correct"] == 1 and out["blocks_wrong"] == 1
    assert out["passes_wrong_let_through"] == 1
    assert out["abstained"] == 1 and out["no_task_key"] == 1
    assert out["TPR"] == 0.5 and out["FPR"] == 1.0 and out["lift"] == -0.5


def test_assistant_max_tokens_is_passed_only_when_set():
    ns = types.SimpleNamespace(domain="hotel", model="glm-4.7", output_dir=Path("/x"),
                               limit=0, max_turns=0, max_actions=0, assistant_max_tokens=0)
    assert "--assistant_max_tokens" not in R.build_argv(ns)
    ns.assistant_max_tokens = 2048
    argv = R.build_argv(ns)
    assert argv[argv.index("--assistant_max_tokens") + 1] == "2048"
    del ns.assistant_max_tokens  # older namespaces without the field still work
    assert "--assistant_max_tokens" not in R.build_argv(ns)
