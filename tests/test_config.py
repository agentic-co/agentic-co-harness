"""Config.load behavior: path resolution and noisy warnings."""

from __future__ import annotations

import os

import pytest
import yaml

from agentco_harness.config import Config, load_env_file


def test_relative_tasks_path_resolves_against_config_dir(tmp_path):
    subdir = tmp_path / "project"
    subdir.mkdir()
    cfg_file = subdir / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"tasks_path": "tasks.jsonl"}))

    config = Config.load(cfg_file)

    assert config.tasks_path == str((subdir / "tasks.jsonl").resolve())
    from pathlib import Path

    assert Path(config.tasks_path).is_absolute()
    assert str(subdir.resolve()) in config.tasks_path


def test_unknown_top_level_key_warns(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"tasks_path": "tasks.jsonl", "mystery": 1}))

    Config.load(cfg_file)

    out = capsys.readouterr().out
    assert "unknown top-level key" in out
    assert "mystery" in out


def test_zai_api_key_loads_from_config(tmp_path):
    """llm.zai_api_key is parsed by Config.load — the orchestrator and the
    docstrings both expect the operator to set it in config.yaml."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump({"llm": {"default_provider": "anthropic", "zai_api_key": "zai-secret"}})
    )

    config = Config.load(cfg_file)

    assert config.llm.zai_api_key == "zai-secret"


def test_zai_api_key_round_trips_through_save_and_load(tmp_path):
    """A saved config with zai_api_key set writes it out and reloads it."""
    config = Config()
    config.llm.zai_api_key = "zai-secret"
    cfg_file = tmp_path / "config.yaml"
    config.save(cfg_file)

    # It must actually appear in the serialized YAML.
    dumped = yaml.safe_load(cfg_file.read_text())
    assert dumped["llm"]["zai_api_key"] == "zai-secret"

    reloaded = Config.load(cfg_file)
    assert reloaded.llm.zai_api_key == "zai-secret"


def test_zai_api_key_omitted_from_save_when_unset(tmp_path):
    """When zai_api_key is unset it must not appear in the saved YAML."""
    config = Config()
    cfg_file = tmp_path / "config.yaml"
    config.save(cfg_file)

    dumped = yaml.safe_load(cfg_file.read_text())
    assert "zai_api_key" not in dumped["llm"]


def test_unconsumed_agent_key_warns(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "agents": {"pm": {"model": "gpt-4o", "temprature": 0.7}},
            }
        )
    )

    Config.load(cfg_file)

    out = capsys.readouterr().out
    assert "nothing consumes" in out
    assert "temprature" in out


@pytest.mark.parametrize("block", ["feeds", "sources"])
def test_a_retired_block_says_so_and_says_what_replaced_it(block, tmp_path, capsys):
    """`feeds:` and `sources:` configured one operator's own pipelines.

    They are gone, and an operator upgrading is holding a config file that
    used to work. "unknown top-level key 'feeds'" reads as a typo when it is
    actually a removal, so the warning names the replacement seam instead.
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump({"tasks_path": "tasks.jsonl", block: {"enabled": True}})
    )

    config = Config.load(cfg_file)

    out = capsys.readouterr().out
    assert block in out
    assert "no longer read" in out
    assert "register_source_factory" in out
    assert "unknown top-level key" not in out   # a removal, not a typo
    assert not hasattr(config, block)


def test_a_retired_block_does_not_stop_the_rest_of_the_file_loading(
    tmp_path, capsys
):
    """The block is ignored; everything around it still applies."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "feeds": {"enabled": True, "sources_md": "/tmp/_SOURCES.md"},
                "instance": "still-here",
            }
        )
    )

    config = Config.load(cfg_file)

    assert config.instance == "still-here"


def test_unknown_nested_key_in_llm_warns(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "openai", "typpo": 1},
            }
        )
    )

    Config.load(cfg_file)

    out = capsys.readouterr().out
    assert "nothing consumes" in out
    assert "typpo" in out
    assert "llm" in out


def test_tiers_section_parses_with_defaults(tmp_path):
    """A config with no tiers block still exposes the design defaults."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"tasks_path": "tasks.jsonl"}))

    config = Config.load(cfg_file)

    assert config.tiers.model_for("planner") == "claude-fable-5"
    assert config.tiers.model_for("worker") == "claude-sonnet-5"
    assert config.tiers.model_for("executor") == "claude-haiku-4-5"
    # `local` is deferred — it must not be silently present.
    assert config.tiers.model_for("local") is None


def test_tiers_section_overrides_a_single_tier(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump({"tasks_path": "tasks.jsonl", "tiers": {"executor": "claude-haiku-5"}})
    )

    config = Config.load(cfg_file)

    assert config.tiers.model_for("executor") == "claude-haiku-5"  # overridden
    assert config.tiers.model_for("planner") == "claude-fable-5"  # default kept


def test_tiers_unknown_tier_key_warns_and_is_dropped(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {"tasks_path": "tasks.jsonl", "tiers": {"planner": "claude-fable-5", "local": "lmstudio/qwen"}}
        )
    )

    config = Config.load(cfg_file)

    out = capsys.readouterr().out
    assert "nothing consumes" in out
    assert "local" in out
    assert "tiers" in out
    assert config.tiers.model_for("local") is None


def test_tiers_round_trip_through_save_and_load(tmp_path):
    config = Config()
    cfg_file = tmp_path / "config.yaml"
    config.save(cfg_file)

    dumped = yaml.safe_load(cfg_file.read_text())
    assert dumped["tiers"]["planner"] == "claude-fable-5"

    reloaded = Config.load(cfg_file)
    assert reloaded.tiers.model_for("worker") == "claude-sonnet-5"


def test_llm_zai_api_key_is_not_flagged(tmp_path, capsys):
    # zai_api_key is in the consumed set even though the current loader does not
    # read it — warning on it now would be a false positive.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "openai", "zai_api_key": "secret"},
            }
        )
    )

    Config.load(cfg_file)

    out = capsys.readouterr().out
    assert "zai_api_key" not in out


# --- env file loading -------------------------------------------------------
#
# Regression cover for the four-day feeds outage: ZAI_API_KEY sat in
# ~/.claude/.env while every ingest bead failed "z.ai API key not found",
# because AgentCo runs under launchd and only ever read os.environ.


def _write_env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_env_file_populates_missing_keys(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "ZAI_API_KEY=zk-from-file\n")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    loaded = load_env_file()

    assert loaded == ["ZAI_API_KEY"]
    assert os.environ["ZAI_API_KEY"] == "zk-from-file"


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    """A plist/shell override must never be clobbered by the file."""
    env = _write_env(tmp_path, "ZAI_API_KEY=zk-from-file\n")
    monkeypatch.setenv("ZAI_API_KEY", "zk-from-launchd")
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    loaded = load_env_file()

    assert loaded == []
    assert os.environ["ZAI_API_KEY"] == "zk-from-launchd"


def test_env_file_skips_comments_blanks_and_export_prefix(tmp_path, monkeypatch):
    env = _write_env(
        tmp_path,
        "# a comment\n\nexport FOO_KEY=foo\n  \nBAR_KEY=bar\n",
    )
    monkeypatch.delenv("FOO_KEY", raising=False)
    monkeypatch.delenv("BAR_KEY", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    loaded = load_env_file()

    assert sorted(loaded) == ["BAR_KEY", "FOO_KEY"]
    assert os.environ["FOO_KEY"] == "foo"
    assert os.environ["BAR_KEY"] == "bar"


def test_env_file_preserves_hash_inside_a_secret(tmp_path, monkeypatch):
    """A '#' in a token is legal; only a spaced trailing comment is stripped."""
    env = _write_env(tmp_path, "TOK_A=abc#def\nTOK_B=xyz # trailing note\n")
    monkeypatch.delenv("TOK_A", raising=False)
    monkeypatch.delenv("TOK_B", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    load_env_file()

    assert os.environ["TOK_A"] == "abc#def"
    assert os.environ["TOK_B"] == "xyz"


def test_env_file_unwraps_quoted_values(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "Q_KEY=\"quoted value\"\nS_KEY='single'\n")
    monkeypatch.delenv("Q_KEY", raising=False)
    monkeypatch.delenv("S_KEY", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    load_env_file()

    assert os.environ["Q_KEY"] == "quoted value"
    assert os.environ["S_KEY"] == "single"


def test_malformed_line_warns_and_does_not_abort(tmp_path, monkeypatch, capsys):
    """A fat-fingered .env must not take down a cycle."""
    env = _write_env(tmp_path, "THIS_LINE_HAS_NO_EQUALS\nGOOD_KEY=good\n")
    monkeypatch.delenv("GOOD_KEY", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    loaded = load_env_file()

    assert loaded == ["GOOD_KEY"]
    assert "WARNING" in capsys.readouterr().out


def test_missing_env_file_is_a_silent_noop(tmp_path, monkeypatch, capsys):
    """Not every deployment is a LifeOS install."""
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(tmp_path / "nope.env"))

    assert load_env_file() == []
    assert capsys.readouterr().out == ""


def test_env_file_load_can_be_disabled(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "NEVER_KEY=nope\n")
    monkeypatch.delenv("NEVER_KEY", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", "")

    assert load_env_file(env) == []
    assert "NEVER_KEY" not in os.environ


def test_config_load_merges_env_file(tmp_path, monkeypatch):
    """The wiring itself: loading a config makes the key visible to executors."""
    env = _write_env(tmp_path, "ZAI_API_KEY=zk-wired\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(env))

    Config.load(cfg)

    assert os.environ["ZAI_API_KEY"] == "zk-wired"


def test_humans_escalate_to_round_trips(tmp_path):
    """`humans.escalate_to` names the `human:` executor automated escalations
    land on; it must survive load → save → load, and default to unset."""
    from agentco_harness.config import Config

    path = tmp_path / "config.yaml"
    path.write_text("tasks_path: tasks.jsonl\nhumans:\n  escalate_to: human:alice\n")
    cfg = Config.load(str(path))
    assert cfg.humans.enabled is True
    assert cfg.humans.escalate_to == "human:alice"
    cfg.save(str(path))
    assert Config.load(str(path)).humans.escalate_to == "human:alice"
    assert Config().humans.escalate_to is None
