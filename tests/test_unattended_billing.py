"""Unattended-billing env injection (ANTHROPIC_API_KEY_UNATTENDED opt-in).

Contract: headless claude subprocesses bill against the unattended API key
when the principal has provisioned one (process env first, then the canonical
~/.claude/.env); with no key provisioned, behavior is byte-identical to before
— ANTHROPIC_API_KEY stays stripped from the child env. Hermetic: no network,
no real ~/.claude/.env reads (the file path is injected or env is used).
"""

from pathlib import Path

import agentco_harness.executor as executor


def test_no_key_means_prior_behavior(monkeypatch, tmp_path):
    monkeypatch.delenv(executor._UNATTENDED_KEY_NAME, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "interactive-key-must-not-leak")
    monkeypatch.setattr(executor, "_CANONICAL_ENV_FILE", tmp_path / "absent.env")
    env = executor._claude_env()
    assert "ANTHROPIC_API_KEY" not in env  # _STRIP_KEYS still governs


def test_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(executor._UNATTENDED_KEY_NAME, "sk-ant-from-env")
    monkeypatch.setattr(executor, "_CANONICAL_ENV_FILE", tmp_path / "absent.env")
    env = executor._claude_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-from-env"


def test_canonical_env_file_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv(executor._UNATTENDED_KEY_NAME, raising=False)
    envfile = tmp_path / ".env"
    envfile.write_text(
        "TELEGRAM_BOT_TOKEN=x\n"
        'ANTHROPIC_API_KEY_UNATTENDED="sk-ant-from-file"\n'
    )
    monkeypatch.setattr(executor, "_CANONICAL_ENV_FILE", envfile)
    env = executor._claude_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-from-file"


def test_empty_value_is_absence(monkeypatch, tmp_path):
    monkeypatch.delenv(executor._UNATTENDED_KEY_NAME, raising=False)
    envfile = tmp_path / ".env"
    envfile.write_text("ANTHROPIC_API_KEY_UNATTENDED=\n")
    monkeypatch.setattr(executor, "_CANONICAL_ENV_FILE", envfile)
    env = executor._claude_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_zai_path_untouched(monkeypatch, tmp_path):
    """The z.ai env must never pick up the unattended Anthropic key."""
    monkeypatch.setenv(executor._UNATTENDED_KEY_NAME, "sk-ant-should-not-appear")
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    env = executor._zai_env()
    assert env.get("ANTHROPIC_API_KEY") != "sk-ant-should-not-appear"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "zai-key"
