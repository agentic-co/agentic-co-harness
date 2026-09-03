"""CLI tests for add-company and link-child: scaffold + link in one step,
registry↔recurring sync, adoption of existing instances, duplicate rejection."""

from __future__ import annotations

import yaml
from click.testing import CliRunner

from agentco_harness.children import ChildRegistry
from agentco_harness.cli import main
from agentco_harness.recurring import Recurring


def _init_global(runner) -> None:
    result = runner.invoke(main, ["init", "--portfolio"])
    assert result.exit_code == 0, result.output


def test_add_company_scaffolds_and_links(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    # Operator's real settings on the parent must propagate to the child.
    parent_cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    parent_cfg["llm"] = {"default_provider": "anthropic", "default_model": "claude-x"}
    parent_cfg["triage"] = {"model": "openai/local-triage", "api_base": "http://localhost:1234/v1"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(parent_cfg))

    result = runner.invoke(main, ["add-company", "acme", "--interval", "1d"])
    assert result.exit_code == 0, result.output

    # Child is a full fractal instance: config + queue + recurring + registry
    # + company/ docs tree (by default — agents write documents there).
    root = tmp_path / "acme"
    assert (root / "config.yaml").exists()
    assert (root / "tasks.jsonl").exists()
    assert (root / "recurring.jsonl").exists()
    assert (root / "children" / "registry.jsonl").exists()
    assert (root / "company" / "INDEX.md").exists()
    child_cfg = yaml.safe_load((root / "config.yaml").read_text())
    assert child_cfg["instance"] == "acme"
    assert child_cfg["llm"]["default_provider"] == "anthropic"
    assert child_cfg["llm"]["default_model"] == "claude-x"
    assert child_cfg["triage"]["model"] == "openai/local-triage"

    # Parent registry and verify def were written in sync.
    children = ChildRegistry(tmp_path / "children" / "registry.jsonl").list()
    assert [c.name for c in children] == ["acme"]
    assert children[0].path == str(root)
    assert children[0].expected_interval == "1d"
    defs = Recurring(tmp_path / "recurring.jsonl").list()
    assert [d.id for d in defs] == ["verify-acme"]
    assert defs[0].payload == {"type": "verify_child", "child": "acme"}
    assert defs[0].schedule == {"every": "1d"}


def test_add_company_telegram_chat_id_per_company(tmp_path, monkeypatch):
    """--telegram-chat-id wires the company's own group and never leaks into
    the parent's config."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    result = runner.invoke(
        main, ["add-company", "acme", "--telegram-chat-id", "-100777"]
    )
    assert result.exit_code == 0, result.output

    child_cfg = yaml.safe_load((tmp_path / "acme" / "config.yaml").read_text())
    assert child_cfg["notify"]["telegram_chat_id"] == "-100777"
    assert child_cfg["notify"]["cycle_summary"] is True
    assert "Setup step" not in result.output  # already wired — no instructions

    parent_cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert parent_cfg["notify"].get("telegram_chat_id") != "-100777"


def test_add_company_without_chat_id_prints_setup_step(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    result = runner.invoke(main, ["add-company", "acme"])
    assert result.exit_code == 0, result.output
    assert "Setup step" in result.output
    assert "telegram_chat_id" in result.output
    assert "--telegram-chat-id" in result.output


def test_add_company_explicit_path_and_no_notify(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    elsewhere = tmp_path / "somewhere" / "else"
    result = runner.invoke(
        main, ["add-company", "beta", str(elsewhere), "--no-notify"]
    )
    assert result.exit_code == 0, result.output
    assert (elsewhere / "config.yaml").exists()
    child = ChildRegistry(tmp_path / "children" / "registry.jsonl").get("beta")
    assert child.path == str(elsewhere)
    assert child.notify is False


def test_add_company_upgrades_older_instance_in_place(tmp_path, monkeypatch):
    """A pre-0.3.0 instance (config + queue only) gains the missing structure;
    its config and queue are never rewritten."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    existing = tmp_path / "legacy"
    existing.mkdir()
    config_text = "# hand-written comment that must survive\ntasks_path: tasks.jsonl\ninstance: legacy-keep\n"
    (existing / "config.yaml").write_text(config_text)
    (existing / "tasks.jsonl").write_text(
        '{"id": "ac-old00001", "title": "old task", "description": "d", '
        '"status": "pending", "priority": 2}\n'
    )

    result = runner.invoke(main, ["add-company", "legacy", str(existing), "--no-company"])
    assert result.exit_code == 0, result.output
    assert "Upgraded existing instance" in result.output

    # Missing v0.3.0 structure was added beside the queue.
    assert (existing / "recurring.jsonl").exists()
    assert (existing / "children" / "registry.jsonl").exists()
    # Config (comments included) and queue untouched.
    assert (existing / "config.yaml").read_text() == config_text
    assert "ac-old00001" in (existing / "tasks.jsonl").read_text()
    # And linked.
    assert ChildRegistry(tmp_path / "children" / "registry.jsonl").get("legacy")


def test_add_company_upgrade_honors_custom_tasks_path(tmp_path, monkeypatch):
    """Structure lands beside the queue, resolved through the child's config."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    existing = tmp_path / "legacy"
    (existing / "data").mkdir(parents=True)
    (existing / "config.yaml").write_text("tasks_path: data/tasks.jsonl\n")
    (existing / "data" / "tasks.jsonl").touch()

    result = runner.invoke(main, ["add-company", "legacy", str(existing), "--no-company"])
    assert result.exit_code == 0, result.output
    assert (existing / "data" / "recurring.jsonl").exists()
    assert (existing / "data" / "children" / "registry.jsonl").exists()


def test_add_company_up_to_date_instance_links_only(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    # First add creates everything; unlink is simulated by a second parent.
    assert runner.invoke(main, ["add-company", "acme"]).exit_code == 0

    nested = tmp_path / "other-parent"
    nested.mkdir()
    monkeypatch.chdir(nested)
    _init_global(runner)
    result = runner.invoke(main, ["add-company", "acme", str(tmp_path / "acme")])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_add_company_same_name_same_path_converges(tmp_path, monkeypatch):
    """Re-adding the same company at the same path is idempotent convergence,
    not an error — link-child is an upsert (RCA ac-4d6d5ac2)."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    assert runner.invoke(main, ["add-company", "acme"]).exit_code == 0
    result = runner.invoke(main, ["add-company", "acme"])
    assert result.exit_code == 0, result.output
    children = ChildRegistry(tmp_path / "children" / "registry.jsonl").list()
    assert [c.name for c in children] == ["acme"]
    defs = Recurring(tmp_path / "recurring.jsonl").list()
    assert [d.id for d in defs] == ["verify-acme"]


def test_add_company_same_name_different_path_fails_loudly(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    assert runner.invoke(main, ["add-company", "acme"]).exit_code == 0
    result = runner.invoke(main, ["add-company", "acme", str(tmp_path / "elsewhere")])
    assert result.exit_code == 1
    assert "refusing to re-point" in result.output


def test_add_company_then_doctor_in_sync(tmp_path, monkeypatch, capsys):
    """The pair created by add-company passes the doctor drift check."""
    from agentco_harness.doctor import run_doctor

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)
    assert runner.invoke(main, ["add-company", "acme"]).exit_code == 0

    code = run_doctor("config.yaml")
    out = capsys.readouterr().out
    assert "in sync for 1 child(ren)" in out
    assert "drifted" not in out


def test_add_company_no_company_skips_docs_tree(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    _init_global(runner)

    result = runner.invoke(main, ["add-company", "acme", "--no-company"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "acme" / "company").exists()


def test_init_company_never_clobbers_existing_runtime_config(tmp_path, monkeypatch):
    """Re-running `init --company` on a live node must preserve its config.

    Regression for 2026-08-04: `agentco init --company` run from the sommeliwhey
    repo root rewrote .agentco/config.yaml with the placeholder scaffold, dropping
    `instance:` and the `agents:` block. That un-declared the externally-executed
    `box-scout` agent, so the next cycle claimed 50 of its beads and failed every
    one with "Unknown agent: box-scout".
    """
    from agentco_harness.scaffold import scaffold_agentco_runtime

    runtime = tmp_path / ".agentco"
    runtime.mkdir(parents=True)
    live = (
        "instance: sommeliwhey\n"
        "tasks_path: tasks.jsonl\n"
        "agents:\n"
        "  box-scout: {}\n"
    )
    (runtime / "config.yaml").write_text(live)

    scaffold_agentco_runtime(tmp_path)

    # Byte-identical: the operator's config survives untouched.
    assert (runtime / "config.yaml").read_text() == live
    loaded = yaml.safe_load((runtime / "config.yaml").read_text())
    assert loaded["instance"] == "sommeliwhey"
    assert "box-scout" in loaded["agents"]

    # ...and the queue is not truncated either.
    assert (runtime / "tasks.jsonl").exists()


def test_scaffold_runtime_writes_config_on_fresh_node(tmp_path):
    """The placeholder is still written when there is no config yet."""
    from agentco_harness.scaffold import scaffold_agentco_runtime

    scaffold_agentco_runtime(tmp_path)

    cfg = tmp_path / ".agentco" / "config.yaml"
    assert cfg.exists()
    loaded = yaml.safe_load(cfg.read_text())
    assert loaded["tasks_path"] == "tasks.jsonl"


def test_init_never_clobbers_existing_config(tmp_path, monkeypatch):
    """`agentco init` in a live node's own dir must preserve its config.

    The 2026-08-04 fix hardened scaffold_agentco_runtime(), but `init` reached
    config.yaml by a second, independent route: cli.init() called
    `Config().save(config_path)` unconditionally. Since `--config` defaults to
    "config.yaml" in the CWD, running `agentco init` from inside .agentco/ —
    the node's own runtime dir and its launchd WorkingDirectory — replaced the
    live config with a fresh default, dropping `instance:` and the
    externally-executed `box-scout` declaration. That is the whole box-scout
    "Unknown agent" incident (2026-07-22 / 07-29 / 08-04) through a door the
    scaffold guard never covered.
    """
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    live = "instance: sommeliwhey\ntasks_path: tasks.jsonl\nagents:\n  box-scout: {}\n"
    (tmp_path / "config.yaml").write_text(live)

    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output

    # Byte-identical: the operator's config survives untouched.
    assert (tmp_path / "config.yaml").read_text() == live
    loaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert loaded["instance"] == "sommeliwhey"
    assert "box-scout" in loaded["agents"]


def test_init_company_via_cli_never_clobbers_existing_config(tmp_path, monkeypatch):
    """The --company path must be additive too, not just bare `init`."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    live = "instance: sommeliwhey\ntasks_path: tasks.jsonl\nagents:\n  box-scout: {}\n"
    (tmp_path / "config.yaml").write_text(live)

    result = runner.invoke(main, ["init", "--company"])
    assert result.exit_code == 0, result.output

    assert (tmp_path / "config.yaml").read_text() == live
    # ...and the company scaffold it was actually asked for still happened.
    assert (tmp_path / "company" / "INDEX.md").exists()


def test_init_force_overwrites_existing_config(tmp_path, monkeypatch):
    """--force is the deliberate escape hatch: it still rewrites the config."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "instance: sommeliwhey\ntasks_path: tasks.jsonl\nagents:\n  box-scout: {}\n"
    )

    result = runner.invoke(main, ["init", "--force"])
    assert result.exit_code == 0, result.output

    loaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "box-scout" not in loaded["agents"]
    assert "instance" not in loaded


def test_init_still_creates_config_on_fresh_dir(tmp_path, monkeypatch):
    """The no-clobber guard must not break first-run initialization."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output

    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / "tasks.jsonl").exists()
    loaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert loaded["tasks_path"] == "tasks.jsonl"
