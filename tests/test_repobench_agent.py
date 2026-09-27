"""End-to-end structural test of `run_repo_episode` with a SCRIPTED fake
model (no network, no real LLM) — the harness's own tool-dispatch/gate/
grading wiring, exercised exactly the way a real conversation would drive
it. Complements the pure-unit tests on `RepoAsopGate` (arms) and
`RepoSandbox` (sandbox) with one that runs them all TOGETHER.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.provider import ChatResult, Endpoint  # noqa: E402
from codebench.repobench import repo_agent  # noqa: E402
from codebench.repobench.repos import RepoSpec  # noqa: E402

GOOD_SRC = "def add(a, b):\n    return a + b\n"
BUGGY_SRC = "def add(a, b):\n    return a - b\n"
TEST_SRC = "from pkg.mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _build_toy_repo(tmp_path: Path) -> RepoSpec:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text(GOOD_SRC)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(TEST_SRC)
    return RepoSpec(
        name="toy", github="nobody/toy", commit="0" * 40, license="MIT",
        keep_paths=["pkg", "tests"], source_dirs=["pkg"],
    )


def _tool_call(name: str, args: dict, call_id: str = "c1") -> dict:
    import json
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _ScriptedChat:
    """Each call to `chat(...)` returns the next scripted assistant message,
    ignoring `messages`/`tools` content — this tests the HARNESS's loop, not
    a model's reasoning."""

    def __init__(self, script: list[dict]):
        self.script = script
        self.i = 0

    def __call__(self, endpoint, messages, tools=None, temperature=0.2, timeout=120, max_tokens=8192):
        msg = self.script[self.i]
        self.i += 1
        return ChatResult(ok=True, message=msg, usage={"prompt_tokens": 10, "completion_tokens": 5})


def _asop_happy_path_script() -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {
                "path": "repro_test.py",
                "content": "from pkg.mod import add\n\ndef test_repro():\n    assert add(2, 3) == 5\n",
            }),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {"path": "pkg/mod.py", "content": GOOD_SRC}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},
    ]


def test_asop_happy_path_end_to_end(tmp_path, monkeypatch):
    spec = _build_toy_repo(tmp_path)
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(_asop_happy_path_script()))

    task = {
        "task_id": "toy/000", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "add(2, 3) returns -1 but should return 5.",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "asop", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    assert ep.status == "finished"
    assert ep.hidden_pass is True
    assert ep.localisation is True
    assert ep.repro_written is True
    assert ep.repro_validity is True
    assert ep.regression_count == 0
    assert ep.remaining_expected_count == 0
    assert ep.false_done is False


def test_asop_happy_path_using_edit_file_for_propose(tmp_path, monkeypatch):
    # edit_file is now what the ASOP prompt tells the agent to use for STEP
    # 2 (PROPOSE) — this exercises that exact path end to end, not just the
    # gate's recognition of it in isolation.
    spec = _build_toy_repo(tmp_path)
    script = [
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {
                "path": "repro_test.py",
                "content": "from pkg.mod import add\n\ndef test_repro():\n    assert add(2, 3) == 5\n",
            }),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("edit_file", {"path": "pkg/mod.py", "old": "return a - b", "new": "return a + b"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},
    ]
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(script))

    task = {
        "task_id": "toy/006", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "add(2, 3) returns -1 but should return 5.",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "asop", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    assert ep.status == "finished"
    assert ep.hidden_pass is True
    assert ep.localisation is True


def test_premature_finish_is_refused_then_recovers(tmp_path, monkeypatch):
    spec = _build_toy_repo(tmp_path)
    script = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},  # refused
        *_asop_happy_path_script(),
    ]
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(script))

    task = {
        "task_id": "toy/001", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "symptom",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "asop", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    assert ep.status == "finished"
    assert ep.hidden_pass is True
    assert any(not tc["ok"] and tc["name"] == "finish" for tc in ep.tool_call_log)


def test_asop_nogate_allows_immediate_finish_and_reports_false_done(tmp_path, monkeypatch):
    spec = _build_toy_repo(tmp_path)
    script = [{"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]}]
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(script))

    task = {
        "task_id": "toy/002", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "symptom",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "asop-nogate", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    assert ep.status == "finished"  # not enforced — immediate finish allowed
    assert ep.hidden_pass is False  # bug never actually fixed
    assert ep.false_done is True
    assert ep.localisation is False


def test_bare_arm_has_no_gate_and_grades_on_final_state(tmp_path, monkeypatch):
    spec = _build_toy_repo(tmp_path)
    script = [
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {"path": "pkg/mod.py", "content": GOOD_SRC}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},
    ]
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(script))

    task = {
        "task_id": "toy/003", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "symptom",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "bare", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    assert ep.status == "finished"
    assert ep.hidden_pass is True
    assert ep.repro_written is False
    assert ep.repro_validity is None


def test_regression_is_detected_when_fix_breaks_something_else(tmp_path, monkeypatch):
    # A second, unrelated, initially-green module+test — so a careless "fix"
    # can be shown to break something the ticket never mentioned.
    spec = _build_toy_repo(tmp_path)
    (tmp_path / "pkg" / "other.py").write_text("def sub(a, b):\n    return a - b\n")
    (tmp_path / "tests" / "test_other.py").write_text(
        "from pkg.other import sub\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
    )

    script = [
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {
                "path": "repro_test.py",
                "content": "from pkg.mod import add\n\ndef test_repro():\n    assert add(2, 3) == 5\n",
            }),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            # fixes add() AND carelessly breaks the unrelated sub()
            _tool_call("write_file", {"path": "pkg/mod.py", "content": GOOD_SRC}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {"path": "pkg/other.py", "content": "def sub(a, b):\n    return a + b\n"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},  # refused: whole suite not checked
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("run_tests", {})]},  # now the agent checks — sees the break
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {"path": "pkg/other.py", "content": "def sub(a, b):\n    return a - b\n"}),  # reverts
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("run_tests", {"path": "repro_test.py"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("run_tests", {})]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},
    ]
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(script))

    task = {
        "task_id": "toy/004", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "symptom",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "asop", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    # the agent caught and reverted its own regression before finishing —
    # final grading should show a clean pass, proving the gate's "whole
    # suite" check is what surfaced the break in the first place.
    assert ep.status == "finished"
    assert ep.hidden_pass is True
    assert ep.regression_count == 0


def test_regression_count_is_nonzero_when_a_break_survives_to_the_end(tmp_path, monkeypatch):
    spec = _build_toy_repo(tmp_path)
    (tmp_path / "pkg" / "other.py").write_text("def sub(a, b):\n    return a - b\n")
    (tmp_path / "tests" / "test_other.py").write_text(
        "from pkg.other import sub\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
    )
    # asop-nogate: finish is never refused, so the episode can end with the
    # fix applied AND the unrelated break still in place.
    script = [
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {"path": "pkg/mod.py", "content": GOOD_SRC}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("write_file", {"path": "pkg/other.py", "content": "def sub(a, b):\n    return a + b\n"}),
        ]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("finish", {})]},
    ]
    monkeypatch.setattr(repo_agent, "chat", _ScriptedChat(script))

    task = {
        "task_id": "toy/005", "repo": "toy", "mutated_file": "pkg/mod.py",
        "mutation_kind": "binop", "ticket": "symptom",
        "failing_node_ids_at_generation": ["tests/test_mod.py::test_add"],
    }
    ep = repo_agent.run_repo_episode(
        task, "asop-nogate", spec, tmp_path, BUGGY_SRC, Endpoint(base_url="fake", model="fake-model"),
    )
    assert ep.status == "finished"
    assert ep.hidden_pass is False  # other.py's test now fails
    assert ep.remaining_expected_count == 0  # the ORIGINAL bug (add) is fixed
    assert ep.regression_count == 1  # but a new failure (sub) was introduced
