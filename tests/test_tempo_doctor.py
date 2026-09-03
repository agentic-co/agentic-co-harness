"""Doctor must make a dependency cycle LOUD.

A cycle is the quietest possible failure in this system: every member waits on
another forever, so none satisfies ready(), nothing dispatches, nothing goes
stale, nothing errors. Without this check the queue simply stops doing that
work and never says so.
"""

from __future__ import annotations

from pathlib import Path

from agentco_harness.doctor import run_doctor


def _instance(tmp_path: Path, jsonl: str) -> str:
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (tmp_path / "tasks.jsonl").write_text(jsonl)
    return str(tmp_path / "config.yaml")


def _bead(tid: str, blocked_by: list[str]) -> str:
    blockers = ",".join(f'"{b}"' for b in blocked_by)
    return (
        f'{{"id":"{tid}","title":"{tid}","description":"","status":"pending",'
        f'"priority":2,"blocked_by":[{blockers}],"metadata":{{}}}}\n'
    )


def test_doctor_reports_a_dependency_cycle(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _instance(tmp_path, _bead("a", ["c"]) + _bead("c", ["a"]))
    run_doctor(cfg)
    out = capsys.readouterr().out
    assert "dependency cycle" in out
    assert "a" in out and "c" in out


def test_doctor_is_quiet_on_an_acyclic_graph(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _instance(tmp_path, _bead("a", []) + _bead("c", ["a"]))
    run_doctor(cfg)
    out = capsys.readouterr().out
    assert "no dependency cycles" in out


def test_doctor_survives_a_corrupt_queue(tmp_path, capsys, monkeypatch):
    """Doctor must never crash — a broken queue is what it exists to diagnose."""
    monkeypatch.chdir(tmp_path)
    cfg = _instance(tmp_path, "{not json at all\n" + _bead("a", []))
    run_doctor(cfg)  # must not raise
    assert capsys.readouterr().out
