"""Shadow judge verifier: policy, evidence, and the read-only guarantee. No network."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from agentco_harness import judge_verifier as jv


def bead(id="ac-1", status="awaiting_verify", cls="judged", check="the report names the bug",
         rubric=None, result=None, description="do the thing", **meta):
    verify = {"class": cls, "check": check}
    if rubric:
        verify["rubric"] = rubric
    m = {"verify": verify, "verify_result": {"class": cls, "passed": None, "output_tail": "awaiting"}}
    m.update(meta)
    return {"id": id, "title": f"title {id}", "description": description, "status": status,
            "result": json.dumps(result) if result is not None else None, "metadata": m}


def stub(held, conf=0.99):
    calls = []

    def ask(evidence, check, rubric, url, timeout):
        calls.append((evidence, check))
        return held, conf, "stub"

    ask.calls = calls
    return ask


@pytest.mark.parametrize("held,conf,want", [
    (True, 0.95, "would_auto_approve"),
    (True, 0.9, "would_auto_approve"),
    (True, 0.89, "would_park_for_human"),
    (False, 0.99, "would_park_for_human"),
    (False, 0.1, "would_park_for_human"),
    (None, 0.0, "would_park_for_human"),
])
def test_policy_table(held, conf, want):
    assert jv.policy(held, conf) == want


def test_policy_has_no_reject_outcome():
    outs = {jv.policy(h, c) for h in (True, False, None) for c in (0.0, 0.5, 0.95, 1.0)}
    assert outs == {"would_auto_approve", "would_park_for_human"}


def test_evidence_keeps_check_and_outcome_when_truncating():
    b = bead(check="CHECK-MARKER", rubric="RUBRIC-MARKER",
             result={"result": "OUTCOME-MARKER"}, description="x" * 100_000)
    ev = jv.build_evidence(b)
    assert len(ev) <= jv.MAX_EVIDENCE_CHARS
    assert "CHECK-MARKER" in ev and "RUBRIC-MARKER" in ev and "OUTCOME-MARKER" in ev
    assert "[truncated" in ev


def test_evidence_huge_outcome_still_keeps_check():
    b = bead(check="CHECK-MARKER", result={"result": "y" * 200_000})
    ev = jv.build_evidence(b)
    assert len(ev) <= jv.MAX_EVIDENCE_CHARS and "CHECK-MARKER" in ev


def test_evidence_includes_sop_and_never_leaks_verdict():
    b = bead(cls="human", sop={"purpose": "P-MARK", "definition_of_done": "DOD-MARK"},
             verify_approval={"approver": "reviewer-1", "verdict": {"reason": "LEAK"}})
    b["metadata"]["verify_result"] = {"class": "human", "passed": True, "output_tail": "approved by reviewer-1"}
    ev = jv.build_evidence(b)
    assert "P-MARK" in ev and "DOD-MARK" in ev
    assert "LEAK" not in ev and "approved by" not in ev


def test_ask_judge_failure_returns_none_never_raises():
    held, conf, raw = jv.ask_judge("e", "c", None, "http://127.0.0.1:1/x", timeout=1, retries=1)
    assert held is None and conf == 0.0 and raw.startswith("ERROR")


def test_judge_failure_parks(tmp_path):
    rows = jv.shadow_scan([bead()], "u", 0.9, tmp_path / "l.jsonl", ask=stub(None, 0.0))
    assert rows[0]["policy"] == "would_park_for_human" and rows[0]["verdict"] is None


def test_shadow_scan_selects_and_logs(tmp_path):
    beads = [bead("a", cls="judged"), bead("b", cls="human"), bead("c", cls="deterministic"),
             bead("d", status="done", cls="judged"), bead("e", cls="human", check="")]
    log = tmp_path / "l.jsonl"
    rows = jv.shadow_scan(beads, "http://u", 0.9, log, ask=stub(True))
    assert [r["bead_id"] for r in rows] == ["a", "b"]
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(lines) == 2
    assert {"bead_id", "gate_class", "check", "verdict", "confidence", "policy", "evidence_sha1",
            "latency_s", "ts", "judge_url", "judge_model"} <= set(lines[0])
    jv.shadow_scan(beads, "http://u", 0.9, log, ask=stub(True))
    assert len(log.read_text().splitlines()) == 4  # append, never rewrite
    only_judged = jv.shadow_scan(beads, "u", 0.9, tmp_path / "m.jsonl", ask=stub(True), include_human=False)
    assert [r["bead_id"] for r in only_judged] == ["a"]


def test_shadow_scan_never_mutates(tmp_path):
    store = tmp_path / "tasks.jsonl"
    beads = [bead("a"), bead("b", cls="human")]
    store.write_text("\n".join(json.dumps(b) for b in beads) + "\n")
    before_file, before_beads = store.read_bytes(), copy.deepcopy(beads)
    jv.shadow_scan(beads, "u", 0.9, tmp_path / "l.jsonl", ask=stub(True))
    assert store.read_bytes() == before_file and beads == before_beads
    assert sorted(p.name for p in tmp_path.iterdir()) == ["l.jsonl", "tasks.jsonl"]


def test_module_cannot_reach_a_verdict_door():
    tree = ast.parse(Path(jv.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"approve_verify", "reject_verify", "Beads", "update"}
    imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any("beads" in m for m in imported)


def test_human_label():
    assert jv.human_label(bead(status="done", verify_approval={"approver": "m"})) is True
    assert jv.human_label(bead(status="verify_failed", cls="human")) is False
    assert jv.human_label(bead(status="done", cls="human", verify_rejection={"approver": "m"})) is False
    # a machine-failed deterministic gate is NOT a human verdict
    assert jv.human_label(bead(status="verify_failed", cls="deterministic")) is None
    assert jv.human_label(bead(status="awaiting_verify")) is None


def test_history_eval_confusion_and_dangerous_error():
    beads = [
        bead("ok1", status="done", verify_approval={"approver": "m"}),
        bead("ok2", status="done", verify_approval={"approver": "m"}),
        bead("bad1", status="verify_failed", cls="human"),
        bead("bad2", status="verify_failed", cls="human"),
        bead("skip", status="pending"),
    ]
    verdicts = {"ok1": (True, 0.95), "ok2": (False, 0.8), "bad1": (True, 0.97), "bad2": (False, 0.9)}

    def ask(evidence, check, rubric, url, timeout):
        bid = next(k for k in verdicts if f"title {k}\n" in evidence + "\n")
        h, c = verdicts[bid]
        return h, c, "stub"

    out = jv.history_eval(beads, "u", 0.9, ask=ask)
    s = out["summary"]
    assert s["n"] == 4 and len(out["rows"]) == 4
    assert s["agreement"] == 0.5
    assert s["confusion"] == {"tp": 1, "fn": 1, "fp": 1, "tn": 1, "unknown": 0}
    assert s["would_auto_approve"] == 2 and s["dangerous_errors"] == 1
