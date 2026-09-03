"""Routing evidence — the informativeness gate and the portfolio walk.

The gate is the point of this module: a thin or degenerate ledger must report
that it cannot advise, rather than producing a confident recommendation from
noise. Most of these tests assert a REFUSAL.
"""

from __future__ import annotations

import json

import pytest

from agentco_harness.routing_eval import (
    DEFAULT_MIN_ARM_SAMPLES,
    DEFAULT_MIN_SAMPLES,
    assess_ledger,
    build_groups,
    classify,
    evaluate,
    format_report,
    portfolio_ledger,
    recommendations,
    to_json,
)


def entry(
    *,
    model="m1",
    success=True,
    cost=1.0,
    task_type="build",
    seconds=10.0,
):
    return {
        "model_used": model,
        "success": success,
        "cost_usd": cost,
        "task_type": task_type,
        "duration_seconds": seconds,
    }


def group_of(entries, key="build"):
    return build_groups(entries)[key]


# ── the gate ──────────────────────────────────────────────────────────────


def test_thin_group_is_insufficient_not_a_recommendation():
    g = group_of([entry(model="a"), entry(model="b")])
    v = classify(g)
    assert v.kind == "insufficient"
    assert v.recommended_model is None


def test_single_arm_refuses_even_with_plenty_of_runs():
    g = group_of([entry(model="only") for _ in range(50)])
    v = classify(g)
    assert v.kind == "single_arm"
    assert v.recommended_model is None


def test_a_second_arm_below_its_floor_does_not_make_a_comparison():
    # 20 runs of 'a' and 2 of 'b': the group clears min_samples, but 'b' has
    # not earned the right to be compared against.
    es = [entry(model="a") for _ in range(20)] + [entry(model="b") for _ in range(2)]
    v = classify(group_of(es))
    assert v.kind == "single_arm"


def test_all_failing_is_a_work_problem_not_a_routing_one():
    es = [entry(model="a", success=False) for _ in range(5)] + [
        entry(model="b", success=False) for _ in range(5)
    ]
    v = classify(group_of(es))
    assert v.kind == "all_failing"
    assert v.recommended_model is None
    assert "work problem" in v.reason


def test_equal_success_ranks_on_cost_and_says_so():
    es = [entry(model="cheap", cost=1.0) for _ in range(5)] + [
        entry(model="pricey", cost=9.0) for _ in range(5)
    ]
    v = classify(group_of(es))
    assert v.kind == "cost_only"
    assert v.recommended_model == "cheap"
    assert v.basis == "cost_per_completed"
    assert "quality carries no signal" in v.reason


def test_differing_success_rates_are_informative():
    es = [entry(model="good", success=True, cost=2.0) for _ in range(5)] + [
        entry(model="flaky", success=(i % 2 == 0), cost=1.0) for i in range(6)
    ]
    v = classify(group_of(es))
    assert v.kind == "informative"
    assert v.recommended_model is not None


def test_cost_per_completed_charges_failures_to_the_successes():
    # 'flaky' is half the per-run price but completes half as often, so it is
    # NOT cheaper per delivered bead — the metric must show that.
    es = [entry(model="solid", success=True, cost=1.0) for _ in range(6)] + [
        entry(model="flaky", success=(i % 2 == 0), cost=0.6) for i in range(6)
    ]
    g = group_of(es)
    assert g.arms["solid"].cost_per_completed == pytest.approx(1.0)
    assert g.arms["flaky"].cost_per_completed == pytest.approx(1.2)
    assert classify(g).recommended_model == "solid"


def test_unpriced_arms_never_produce_a_zero_cost_winner():
    es = [entry(model="a", cost=None) for _ in range(5)] + [
        entry(model="b", cost=None) for _ in range(5)
    ]
    g = group_of(es)
    assert g.arms["a"].cost_per_completed is None
    v = classify(g)
    # Everything ties on success and nothing carries a price: no dimension
    # discriminates, so it must refuse rather than pick arbitrarily.
    assert v.kind == "insufficient"
    assert v.recommended_model is None


def test_unpriced_but_differing_success_falls_back_to_success_rate():
    es = [entry(model="good", success=True, cost=None) for _ in range(5)] + [
        entry(model="bad", success=False, cost=None) for _ in range(5)
    ]
    v = classify(group_of(es))
    assert v.kind == "informative"
    assert v.recommended_model == "good"
    assert v.basis == "success_rate"


def test_recommendations_never_include_an_uninformative_group():
    es = [entry(model="only", task_type="solo") for _ in range(10)] + [
        entry(model="a", task_type="pair", cost=1.0) for _ in range(5)
    ] + [entry(model="b", task_type="pair", cost=5.0) for _ in range(5)]
    _, results = evaluate(es)
    recs = recommendations(results)
    assert "solo" not in recs
    assert recs["pair"] == "a"


# ── ledger health ─────────────────────────────────────────────────────────


def test_quality_and_cost_comparability_are_reported_separately():
    # The live shape: two models, every run successful.
    es = [entry(model="a") for _ in range(5)] + [entry(model="b") for _ in range(5)]
    h = assess_ledger(es)
    assert h.can_compare_quality is False  # no outcome variance
    assert h.can_compare_cost is True  # but cost still discriminates


def test_outcome_variance_detected_when_something_fails():
    es = [entry(model="a"), entry(model="b", success=False)]
    assert assess_ledger(es).outcome_variance is True


def test_report_names_the_zero_variance_problem_and_its_caveat():
    es = [entry(model="a", cost=1.0) for _ in range(5)] + [
        entry(model="b", cost=2.0) for _ in range(5)
    ]
    h, results = evaluate(es)
    out = format_report(h, results, "task_type", DEFAULT_MIN_SAMPLES)
    assert "QUALITY CANNOT BE COMPARED" in out
    # The caveat that keeps a cost-only recommendation honest.
    assert "COMPLETED, not that its output was good" in out


def test_empty_ledger_reports_nothing_rather_than_crashing():
    h, results = evaluate([])
    out = format_report(h, results, "task_type", DEFAULT_MIN_SAMPLES)
    assert "No telemetry yet" in out
    assert results == []


def test_single_model_ledger_says_no_comparison_is_possible():
    es = [entry(model="only") for _ in range(10)]
    h, results = evaluate(es)
    out = format_report(h, results, "task_type", DEFAULT_MIN_SAMPLES)
    assert "NO COMPARISON IS POSSIBLE" in out


# ── model attribution ─────────────────────────────────────────────────────


def test_model_used_wins_over_requested_model():
    g = build_groups([{"model_used": "actual", "requested_model": "asked", "success": True}])
    assert "actual" in g["(unset)"].arms


def test_missing_model_is_named_unknown_not_folded_into_a_real_arm():
    g = build_groups([{"success": True}, {"model_used": "real", "success": True}])
    arms = g["(unset)"].arms
    assert set(arms) == {"(unknown)", "real"}


def test_unset_group_key_is_labelled_not_dropped():
    g = build_groups([{"model_used": "m", "success": True, "task_type": None}])
    assert "(unset)" in g


# ── json ──────────────────────────────────────────────────────────────────


def test_json_output_is_serialisable_and_carries_the_verdicts():
    es = [entry(model="a", cost=1.0) for _ in range(5)] + [
        entry(model="b", cost=5.0) for _ in range(5)
    ]
    h, results = evaluate(es)
    blob = json.loads(json.dumps(to_json(h, results)))
    assert blob["health"]["can_compare_cost"] is True
    assert blob["health"]["can_compare_quality"] is False
    assert blob["groups"][0]["verdict"] == "cost_only"
    assert blob["recommendations"]["build"] == "a"


def test_unknown_cost_renders_as_null_never_zero():
    es = [entry(model="a", cost=None) for _ in range(5)]
    h, results = evaluate(es)
    blob = to_json(h, results)
    assert blob["groups"][0]["arms"][0]["cost_per_completed"] is None


# ── portfolio walk ────────────────────────────────────────────────────────


def _node(tmp_path, name, ledger_rows, children=()):
    """Build a minimal AgentCo node with a cost ledger and child registry.

    Both `costs.jsonl` and `children/registry.jsonl` are derived from
    `tasks_path` rather than configured, so the layout has to be real.
    """
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "tasks.jsonl").write_text("", encoding="utf-8")
    (d / "costs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ledger_rows), encoding="utf-8"
    )
    _write_registry(d, children)
    (d / "config.yaml").write_text("tasks_path: tasks.jsonl\n", encoding="utf-8")
    return d


def _write_registry(node_dir, children):
    reg_path = node_dir / "children" / "registry.jsonl"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(
        "".join(
            json.dumps({"name": c.name, "path": str(c), "cadence": "hourly"}) + "\n"
            for c in children
        ),
        encoding="utf-8",
    )


def test_portfolio_walk_pools_child_ledgers(tmp_path):
    child = _node(tmp_path, "child", [entry(model="b", cost=2.0)])
    parent = _node(tmp_path, "parent", [entry(model="a", cost=1.0)], children=[child])
    pooled = portfolio_ledger(str(parent / "config.yaml"))
    assert len(pooled) == 2
    assert {e["model_used"] for e in pooled} == {"a", "b"}


def test_portfolio_walk_tags_each_entry_with_its_node(tmp_path):
    parent = _node(tmp_path, "parent", [entry(model="a")])
    pooled = portfolio_ledger(str(parent / "config.yaml"))
    assert all("_node" in e for e in pooled)


def test_portfolio_walk_survives_a_registry_cycle(tmp_path):
    # A points at B, B points back at A. Without the seen-set this recurses
    # forever — the same guard `me.collect` and `tempo --portfolio` carry.
    a = tmp_path / "a"
    b = tmp_path / "b"
    _node(tmp_path, "a", [entry(model="a")])
    _node(tmp_path, "b", [entry(model="b")])
    _write_registry(a, [b])
    _write_registry(b, [a])
    pooled = portfolio_ledger(str(a / "config.yaml"))
    assert len(pooled) == 2  # each node counted exactly once


def test_an_unreadable_child_is_skipped_with_a_warning_not_raised(tmp_path, capsys):
    missing = tmp_path / "gone"
    parent = _node(tmp_path, "parent", [entry(model="a")], children=[missing])
    pooled = portfolio_ledger(str(parent / "config.yaml"))
    assert len(pooled) == 1  # parent's own runs survive


def test_a_broken_node_makes_evidence_thinner_never_invents_it(tmp_path, capsys):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "config.yaml").write_text("this: [is: not: valid", encoding="utf-8")
    parent = _node(tmp_path, "parent", [entry(model="a")], children=[broken])
    pooled = portfolio_ledger(str(parent / "config.yaml"))
    assert len(pooled) == 1
    err = capsys.readouterr().err
    assert "thinner" in err  # names the consequence, per the house convention
