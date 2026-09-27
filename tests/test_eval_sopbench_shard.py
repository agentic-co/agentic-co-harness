"""Tests for sharded SOPBench runs and the EXP-A helper-prerequisite closure.

Sharding rides on `run_simulation.main`'s POSITIONAL resume (task i is skipped
iff `results[i]` holds enough interactions), so the properties pinned here are
the ones whose failure would silently change which tasks an arm ran: every
position owned by exactly one shard, resumes preserved, a foreign file refused,
and a merge that refuses anything but exactly one real result per position.

Synthetic data only — a test that needed a SOPBench checkout would be skipped
in CI, which is where a regression would land.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "eval"))

import asop_engine as E  # noqa: E402
import sopbench_shard as S  # noqa: E402


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_sopbench_asop", _ROOT / "scripts" / "eval" / "run_sopbench_asop.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_sopbench_asop"] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load_runner()


def _real(goal: str, model: str = "gpt-oss-20b-s0") -> dict:
    return {
        "setup": {"assistant_agent": {"model": model}},
        "task": {"user_goal": goal},
        "interactions": [{"interaction": []}],
    }


# ── sharding ────────────────────────────────────────────────────────────────


def test_parse_shard_accepts_zero_based_and_rejects_out_of_range():
    assert S.parse_shard("0/3") == (0, 3)
    assert S.parse_shard("2/3") == (2, 3)
    for bad in ("3/3", "-1/2", "1/0", "x", "1-2"):
        with pytest.raises(ValueError):
            S.parse_shard(bad)


def test_every_position_owned_by_exactly_one_shard():
    n_tasks, n = 134, 3
    for i in range(n_tasks):
        assert sum(S.owns(i, k, n) for k in range(n)) == 1


def test_seed_runs_owned_positions_and_skips_the_rest():
    seeded = S.seed_results([], 5, 1, 2)
    assert len(seeded) == 5
    # owned: 1, 3 — empty slots, so SOPBench's loop runs them
    assert seeded[1] == {"interactions": []} and seeded[3] == {"interactions": []}
    # not owned: 0, 2, 4 — a placeholder interaction, so the loop skips them
    assert all(S.is_placeholder(seeded[i]) for i in (0, 2, 4))
    assert not any(S.is_done(e) for e in seeded)


def test_seed_preserves_a_finished_owned_result_so_resume_is_lossless():
    first = S.seed_results([], 4, 0, 2)
    first[0] = _real("a")  # shard 0 finished task 0, then died
    again = S.seed_results(first, 4, 0, 2)
    assert again[0] == _real("a")
    assert again[2] == {"interactions": []}


def test_seed_refuses_a_real_result_it_does_not_own():
    # An unsharded run, or a different shard count, wrote this directory.
    with pytest.raises(SystemExit):
        S.seed_results([_real("a"), _real("b")], 2, 0, 2)


def test_merge_reassembles_in_order_under_the_canonical_model():
    s0 = S.seed_results([], 4, 0, 2)
    s1 = S.seed_results([], 4, 1, 2)
    s0[0], s0[2] = _real("g0", "gpt-oss-20b-s0"), _real("g2", "gpt-oss-20b-s0")
    s1[1], s1[3] = _real("g1", "gpt-oss-20b-s1"), _real("g3", "gpt-oss-20b-s1")
    merged = S.merge_results([s0, s1], "openai/gpt-oss-20b")
    assert [e["task"]["user_goal"] for e in merged] == ["g0", "g1", "g2", "g3"]
    agents = [e["setup"]["assistant_agent"] for e in merged]
    assert {a["model"] for a in agents} == {"openai/gpt-oss-20b"}
    assert [a["instance"] for a in agents] == [
        "gpt-oss-20b-s0", "gpt-oss-20b-s1", "gpt-oss-20b-s0", "gpt-oss-20b-s1",
    ]


def test_merge_refuses_a_missing_position():
    s0 = S.seed_results([], 4, 0, 2)
    s1 = S.seed_results([], 4, 1, 2)
    s0[0], s0[2] = _real("g0"), _real("g2")
    s1[1] = _real("g1")  # shard 1 never finished task 3
    with pytest.raises(SystemExit, match="position 3"):
        S.merge_results([s0, s1])


def test_merge_refuses_a_position_claimed_twice():
    a = [_real("g0"), _real("g1")]
    b = [_real("g0"), S.seed_results([], 2, 0, 2)[1]]
    with pytest.raises(SystemExit, match="position 0"):
        S.merge_results([a, b])


def test_merged_stats_use_the_runners_own_aggregate():
    rows0 = [{"exited_cleanly": True, "turns": 4, "tool_calls": 2, "procedure": "Pay Bill"}]
    rows1 = [{"exited_cleanly": False, "turns": 8, "tool_calls": 6, "procedure": "Pay Bill"}]
    stats = [
        {"summary": {"arm": "asop-gated", "ungated_tripwire_calls": 0}, "rows": rows0},
        {"summary": {"arm": "asop-gated", "ungated_tripwire_calls": 1}, "rows": rows1},
    ]
    out = S.merge_stats(stats, R.aggregate_rows)["summary"]
    assert out == {**out, **R.aggregate_rows(rows0 + rows1)}
    assert out["conversations"] == 2 and out["hit_cap"] == 1 and out["mean_turns"] == 6.0
    assert out["routed"] == {"Pay Bill": 2}
    assert out["ungated_tripwire_calls"] == 1 and out["shards"] == 2


# ── EXP-A: helper-prerequisite closure ──────────────────────────────────────

DOC = """\
Compiled procedure document.

## Routing

| The user wants to... | Procedure |
| --- | --- |
| get loan | Get Loan |

## Procedure: Get Loan

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **logged in.** Condition `logged_in_user`. ESTABLISH: call the login_user tool. Gate: deterministic (tool call: `login_user`)
3. **owed balance.** Condition `get_loan_owed_balance_restr`. VERIFY: call the get_account_owed_balance tool. Gate: deterministic (tool call: `get_account_owed_balance`)
4. **Get Loan — the requested action.** ACT: call the get_loan tool. Gate: deterministic (tool call: `get_loan`)
"""

TOOL_OF = {
    "internal_check_username_exist": "internal_check_username_exist",
    "logged_in_user": "login_user",
    "get_loan_owed_balance_restr": "get_account_owed_balance",
}
HELPER_PREREQS = {
    "get_account_owed_balance": {"logged_in_user", "internal_check_username_exist"},
    "get_loan": {"logged_in_user", "get_loan_owed_balance_restr"},  # the goal: never expanded
}
# The shape N31 counted: the task imposes the owed-balance check but not login.
TASK = {
    "user_goal": "get_loan",
    "constraints": [
        "and",
        [
            ["single", "internal_check_username_exist", {}],
            ["single", "get_loan_owed_balance_restr", {}],
        ],
    ],
}


def _tools(asop) -> list:
    return [E.named_tool(s) for s in asop.procedure("Get Loan").steps]


def test_narrowing_without_the_flag_is_unchanged():
    narrowed = R.make_task_asop_provider(E.parse_asop(DOC), TASK, TOOL_OF)
    assert _tools(narrowed) == [
        "internal_check_username_exist", "get_account_owed_balance", "get_loan",
    ]


def test_restores_a_kept_helpers_own_prerequisite_in_document_order():
    fixed = R.make_task_asop_provider(E.parse_asop(DOC), TASK, TOOL_OF, HELPER_PREREQS)
    assert _tools(fixed) == [
        "internal_check_username_exist", "login_user", "get_account_owed_balance", "get_loan",
    ]
    assert [s.number for s in fixed.procedure("Get Loan").steps] == [1, 2, 3, 4]


def test_closure_is_transitive_and_never_expands_the_goal():
    prereqs = {"a": {"cb"}, "b": {"cc"}, "goal": {"cz"}}
    tool_of = {"cb": "b", "cc": "c", "cz": "z"}
    assert R.close_over_helper_prereqs({"a", "goal"}, "goal", prereqs, tool_of) == {"a", "b", "c", "goal"}


def test_a_prerequisite_no_tool_verifies_adds_nothing():
    assert R.close_over_helper_prereqs({"a"}, "goal", {"a": {"env_only"}}, {}) == {"a"}
