"""link-child as a repair tool (RCA ac-4d6d5ac2): the command is an upsert
that converges registry ↔ recurring defs to the linked state from ANY
starting point — fresh, half-written (crash between the two appends),
hand-edited, or disabled — and only hard-errors when the name already
points at a different path without --force. Doctor prescribes it as the
fix for drift, so every drift state doctor detects must be repairable here."""

from __future__ import annotations

from click.testing import CliRunner

from agentco_harness.children import ChildRegistry
from agentco_harness.cli import main
from agentco_harness.doctor import run_doctor
from agentco_harness.recurring import Recurring


def _setup(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(main, ["init", "--portfolio"]).exit_code == 0
    child = tmp_path / "acme"
    child.mkdir()
    return child


def _drop_line(path, needle: str) -> None:
    lines = [l for l in path.read_text().splitlines() if needle not in l]
    path.write_text("".join(l + "\n" for l in lines))


def test_fresh_link_creates_both(tmp_path, monkeypatch):
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)

    result = runner.invoke(main, ["link-child", "acme", str(child)])
    assert result.exit_code == 0, result.output
    assert "registry: created" in result.output
    assert "verify def: created" in result.output
    assert ChildRegistry(tmp_path / "children" / "registry.jsonl").get("acme")
    assert Recurring(tmp_path / "recurring.jsonl").get("verify-acme")


def test_repairs_missing_verify_def(tmp_path, monkeypatch, capsys):
    """The exact drift state from the RCA: registered child, no verify def."""
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)
    assert runner.invoke(main, ["link-child", "acme", str(child)]).exit_code == 0

    _drop_line(tmp_path / "recurring.jsonl", "verify-acme")
    run_doctor("config.yaml")
    assert "silently unmonitored" in capsys.readouterr().out

    result = runner.invoke(main, ["link-child", "acme", str(child)])
    assert result.exit_code == 0, result.output
    assert "registry: unchanged" in result.output
    assert "verify def: created" in result.output

    run_doctor("config.yaml")
    out = capsys.readouterr().out
    assert "in sync for 1 child(ren)" in out
    assert "silently unmonitored" not in out


def test_reenables_disabled_verify_def(tmp_path, monkeypatch, capsys):
    """A disabled def also counts as unmonitored (doctor filters on enabled)."""
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)
    assert runner.invoke(main, ["link-child", "acme", str(child)]).exit_code == 0

    Recurring(tmp_path / "recurring.jsonl").update("verify-acme", enabled=False)
    run_doctor("config.yaml")
    assert "silently unmonitored" in capsys.readouterr().out

    result = runner.invoke(main, ["link-child", "acme", str(child)])
    assert result.exit_code == 0, result.output
    assert "verify def: re-enabled" in result.output
    assert Recurring(tmp_path / "recurring.jsonl").get("verify-acme").enabled is True

    run_doctor("config.yaml")
    assert "in sync for 1 child(ren)" in capsys.readouterr().out


def test_repairs_orphaned_registry_entry(tmp_path, monkeypatch, capsys):
    """Drift in the other direction: verify def present, registry entry gone."""
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)
    assert runner.invoke(main, ["link-child", "acme", str(child)]).exit_code == 0

    _drop_line(tmp_path / "children" / "registry.jsonl", '"acme"')
    run_doctor("config.yaml")
    assert "unregistered child(ren)" in capsys.readouterr().out

    result = runner.invoke(main, ["link-child", "acme", str(child)])
    assert result.exit_code == 0, result.output
    assert "registry: created" in result.output
    assert "verify def: unchanged" in result.output

    run_doctor("config.yaml")
    assert "in sync for 1 child(ren)" in capsys.readouterr().out


def test_different_path_requires_force(tmp_path, monkeypatch):
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)
    assert runner.invoke(main, ["link-child", "acme", str(child)]).exit_code == 0

    other = tmp_path / "other"
    other.mkdir()
    result = runner.invoke(main, ["link-child", "acme", str(other)])
    assert result.exit_code == 1
    assert "refusing to re-point" in result.output
    registry = ChildRegistry(tmp_path / "children" / "registry.jsonl")
    assert registry.get("acme").path == str(child)

    result = runner.invoke(main, ["link-child", "acme", str(other), "--force"])
    assert result.exit_code == 0, result.output
    assert "registry: updated" in result.output
    assert registry.get("acme").path == str(other)


def test_idempotent_second_run_is_noop(tmp_path, monkeypatch):
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)
    assert runner.invoke(main, ["link-child", "acme", str(child)]).exit_code == 0

    registry_before = (tmp_path / "children" / "registry.jsonl").read_text()
    recurring_before = (tmp_path / "recurring.jsonl").read_text()

    result = runner.invoke(main, ["link-child", "acme", str(child)])
    assert result.exit_code == 0, result.output
    assert "registry: unchanged" in result.output
    assert "verify def: unchanged" in result.output
    assert (tmp_path / "children" / "registry.jsonl").read_text() == registry_before
    assert (tmp_path / "recurring.jsonl").read_text() == recurring_before


def test_interval_change_updates_both(tmp_path, monkeypatch):
    """The invocation's flags declare the desired state — a new interval
    converges both the registry entry and the verify def's schedule."""
    runner = CliRunner()
    child = _setup(tmp_path, monkeypatch, runner)
    assert runner.invoke(main, ["link-child", "acme", str(child), "--interval", "1h"]).exit_code == 0

    result = runner.invoke(main, ["link-child", "acme", str(child), "--interval", "1d"])
    assert result.exit_code == 0, result.output
    assert "registry: updated" in result.output
    assert "verify def: updated" in result.output
    assert ChildRegistry(tmp_path / "children" / "registry.jsonl").get("acme").expected_interval == "1d"
    assert Recurring(tmp_path / "recurring.jsonl").get("verify-acme").schedule == {"every": "1d"}
