"""Config discovery: flag > $AGENTIC_CO_CONFIG > nearest config.yaml upward > cwd default."""

from click.testing import CliRunner

from agentco_harness.cli import main
from agentco_harness.config import CONFIG_ENV_VAR, discover_config


def _node(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    return path


def test_discovers_config_in_parent(tmp_path):
    node = _node(tmp_path / "node")
    sub = node / "a" / "b"
    sub.mkdir(parents=True)
    assert discover_config(sub) == str(node / "config.yaml")


def test_cwd_config_keeps_relative_default(tmp_path):
    node = _node(tmp_path / "node")
    assert discover_config(node) == "config.yaml"


def test_nested_agentco_config_found(tmp_path):
    (tmp_path / ".agentco").mkdir()
    (tmp_path / ".agentco" / "config.yaml").write_text("")
    sub = tmp_path / "x"
    sub.mkdir()
    assert discover_config(sub) == str(tmp_path / ".agentco" / "config.yaml")


def test_nothing_found_falls_back(tmp_path):
    assert discover_config(tmp_path) == "config.yaml"


def test_cli_uses_discovery_from_subdir(tmp_path, monkeypatch):
    node = _node(tmp_path / "node")
    sub = node / "deep"
    sub.mkdir()
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(sub)
    seen = {}

    @main.command("_probe_cfg")
    @__import__("click").pass_context
    def _probe(ctx):
        seen["path"] = ctx.obj["config_path"]

    try:
        CliRunner().invoke(main, ["_probe_cfg"])
    finally:
        main.commands.pop("_probe_cfg", None)
    assert seen["path"] == str(node / "config.yaml")


def test_env_var_outranks_discovery(tmp_path, monkeypatch):
    node = _node(tmp_path / "node")
    other = _node(tmp_path / "other")
    monkeypatch.chdir(node)
    seen = {}

    @main.command("_probe_cfg2")
    @__import__("click").pass_context
    def _probe(ctx):
        seen["path"] = ctx.obj["config_path"]

    try:
        CliRunner().invoke(main, ["_probe_cfg2"], env={CONFIG_ENV_VAR: str(other / "config.yaml")})
    finally:
        main.commands.pop("_probe_cfg2", None)
    assert seen["path"] == str(other / "config.yaml")


def test_init_does_not_adopt_parent_config(tmp_path, monkeypatch):
    _node(tmp_path / "node")
    sub = tmp_path / "node" / "fresh"
    sub.mkdir()
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(sub)
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert (sub / "config.yaml").exists()
