"""Isolation under a poisoned environment, across EVERY spawn site.

Contract: under an `identity:` a child process sees only that instance's
connections. Nothing machine-wide reaches it: not the global env file, not an
unattended-key variable, not the SSH agent, not the machine's git/ssh config,
not the ambient git/gh login. Fabricated names throughout.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentco_harness import connections, executor
from agentco_harness.beads import Beads
from agentco_harness.config import Config, IdentityConfig

POISON = "POISON-machine-wide"
POISONED_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    executor._UNATTENDED_KEY_NAME, "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN",
    "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_AUTHOR_EMAIL", "GIT_ASKPASS",
)


@pytest.fixture(autouse=True)
def poisoned(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".ssh" / "config").write_text(f"Host *\n  IdentityFile ~/.ssh/{POISON}\n")
    (home / ".gitconfig").write_text(f"[user]\n  email = {POISON}@example.com\n")
    (home / ".claude" / ".env").write_text(f"{executor._UNATTENDED_KEY_NAME}={POISON}-file\n")
    monkeypatch.setenv("HOME", str(home))
    for k in POISONED_KEYS:
        monkeypatch.setenv(k, f"{POISON}-{k}")
    monkeypatch.setenv("AGENTCO_CONNECTIONS_DIR", str(tmp_path / "conns"))
    monkeypatch.setattr(executor, "_CANONICAL_ENV_FILE", home / ".claude" / ".env")
    yield home
    executor.configure_identity(None)


def _identity(tmp_path, tag="acme"):
    connections.save(connections.Connection(
        name=f"{tag}-claude", kind="claude_code", auth="subscription",
        config_dir=str(tmp_path / f"{tag}-cfg")), f"tok-{tag}")
    connections.save(connections.Connection(
        name=f"{tag}-github", kind="github", ssh_key=str(tmp_path / f"{tag}_key")), None)
    return IdentityConfig(name=tag, claude=f"{tag}-claude", github=f"{tag}-github")


def assert_isolated(env, home):
    assert env is not None, "an identity spawn must pass an explicit env, never inherit"
    # The instance's own connections may set some of these names, with THEIR
    # values; the machine-wide value must never survive.
    for k in POISONED_KEYS:
        assert POISON not in env.get(k, ""), f"{k} leaked"
    for k in ("ANTHROPIC_API_KEY", executor._UNATTENDED_KEY_NAME, "SSH_AUTH_SOCK",
              "GH_TOKEN", "GITHUB_TOKEN", "GIT_AUTHOR_EMAIL", "GIT_ASKPASS"):
        assert k not in env, f"{k} leaked"
    for k, v in env.items():
        assert POISON not in str(v), f"{k} carries machine-wide material"
    assert "-F /dev/null" in env["GIT_SSH_COMMAND"]
    assert "IdentityAgent=none" in env["GIT_SSH_COMMAND"]
    assert env["GIT_CONFIG_GLOBAL"] and not env["GIT_CONFIG_GLOBAL"].startswith(str(home))
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GH_CONFIG_DIR"] and not env["GH_CONFIG_DIR"].startswith(str(home))


# ---- every spawn site -------------------------------------------------------

def test_executor_claude_spawn(tmp_path, poisoned):
    ident = _identity(tmp_path)
    dump = tmp_path / "env.txt"
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\nenv > {dump}\necho '{{\"result\": \"ok\"}}'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    executor.configure_identity(ident)
    assert executor.run_claude_task("hi", claude_bin=str(binary)).success
    env = dict(l.split("=", 1) for l in dump.read_text().splitlines() if "=" in l)
    assert_isolated(env, poisoned)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-acme"


def test_executor_agy_spawn(tmp_path, poisoned, monkeypatch):
    ident = _identity(tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    executor.configure_identity(ident)
    assert executor.run_agy_task("hi", agy_bin="agy").success
    assert_isolated(seen["env"], poisoned)


def test_agents_claude_p_spawn(tmp_path, poisoned, monkeypatch):
    from agentco_harness.agents import DevAgent

    ident = _identity(tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    stub = SimpleNamespace(
        implement_prompt=lambda **kw: SimpleNamespace(claude_code_prompt="p"),
        _codebase_context=lambda t: "",
    )
    task = SimpleNamespace(title="t", description="d")
    executor.configure_identity(ident)
    DevAgent._execute_with_claude_code(stub, task, SimpleNamespace(approach="a"))
    assert_isolated(seen["env"], poisoned)
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-acme"


def test_lesson_triage_shell_spawn(tmp_path, poisoned, monkeypatch):
    from agentco_harness import lesson_triage

    ident = _identity(tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(lesson_triage.subprocess, "run", fake_run)
    executor.configure_identity(ident)
    assert lesson_triage._run("true", tmp_path, 5) == 0
    assert_isolated(seen["env"], poisoned)


def test_gate_spawn(tmp_path, poisoned, monkeypatch):
    from agentco_harness import beads as beads_mod

    ident = _identity(tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(beads_mod.subprocess, "run", fake_run)
    executor.configure_identity(ident)
    out = Beads(str(tmp_path / "t.jsonl"))._run_one_stage("true", str(tmp_path), 5)
    assert out["passed"]
    assert_isolated(seen["env"], poisoned)


def test_git_gh_env_is_never_ambient(tmp_path, poisoned):
    ident = _identity(tmp_path)
    assert_isolated(executor._identity_env(ident, claude=False), poisoned)


# ---- the four findings ------------------------------------------------------

def test_env_file_is_not_read_under_an_identity(tmp_path, monkeypatch):
    envfile = tmp_path / "machine.env"
    envfile.write_text("PROBE_MACHINE_WIDE_KEY=planted\n")
    monkeypatch.setenv("AGENTCO_ENV_FILE", str(envfile))
    monkeypatch.delenv("PROBE_MACHINE_WIDE_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"identity": {"name": "acme", "claude": "c", "github": "g"}}))
    Config.load(cfg)
    assert "PROBE_MACHINE_WIDE_KEY" not in os.environ
    # control: without an identity the file is still honoured (unchanged behaviour)
    cfg.write_text("tasks_path: t.jsonl\n")
    Config.load(cfg)
    assert os.environ.get("PROBE_MACHINE_WIDE_KEY") == "planted"
    monkeypatch.delenv("PROBE_MACHINE_WIDE_KEY", raising=False)


def test_github_role_missing_is_a_coded_refusal(tmp_path):
    ident = _identity(tmp_path)
    no_gh = IdentityConfig(name="acme", claude=ident.claude)
    with pytest.raises(executor.IdentityUnavailable) as ei:
        executor.check_identity(no_gh)
    assert "github" in str(ei.value)
    dump = tmp_path / "ran"
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\ntouch {dump}\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    executor.configure_identity(no_gh)
    res = executor.run_claude_task("hi", claude_bin=str(binary))
    assert not res.success and "identity_unavailable" in res.error
    assert not dump.exists()


def test_github_connection_without_gitconfig_or_gh_dir_still_pins_them(tmp_path):
    conn = connections.Connection(name="bare-github", kind="github", ssh_key=str(tmp_path / "k"))
    env = connections.child_env(conn)
    assert env["GIT_CONFIG_GLOBAL"] and env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GH_CONFIG_DIR"]
    assert "-F /dev/null" in env["GIT_SSH_COMMAND"]


def test_declared_but_unresolved_identity_refuses(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"identity": {"name": "acme", "claude": "c", "github": "g"}}))
    Config.load(cfg)  # declared, but no Orchestrator ever resolved it
    with pytest.raises(executor.IdentityUnavailable):
        executor.spawn_env(claude=False)
    with pytest.raises(executor.IdentityUnavailable):
        executor._claude_env()


def test_explicit_identity_wins_over_the_process_global(tmp_path, poisoned):
    a = _identity(tmp_path, "acme")
    b = _identity(tmp_path, "globex")
    executor.configure_identity(a)  # another orchestrator overwrote the global
    env = executor.spawn_env(b, claude=True)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-globex"
    assert "globex_key" in env["GIT_SSH_COMMAND"]


def test_doctor_flags_missing_github_role(tmp_path, monkeypatch):
    from tests.test_instance_identity import _doctor

    ident = _identity(tmp_path)
    fs = _doctor(tmp_path, monkeypatch, {"name": "acme", "claude": ident.claude})
    assert any(f.status == "broken" and "identity.github" in f.message for f in fs)


# ---- fail-open reachability (B, D, E) ---------------------------------------

def _identity_yaml(tmp_path, name="node"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"tasks_path": "tasks.jsonl", "identity": {"name": "acme", "claude": "c", "github": "g"}}))
    return d


def test_B_a_later_non_identity_config_load_does_not_unset_the_declaration(tmp_path):
    Config.load(_identity_yaml(tmp_path) / "config.yaml")
    other = tmp_path / "other.yaml"
    other.write_text("tasks_path: t.jsonl\n")
    Config.load(other)  # e.g. a per-spawn idle-timeout load, or another node's config
    with pytest.raises(executor.IdentityUnavailable):
        executor.spawn_env(claude=False)


def test_D_an_identity_less_orchestrator_does_not_unset_the_identity(tmp_path):
    from agentco_harness.orchestrator import Orchestrator

    Orchestrator(Config(tasks_path=str(tmp_path / "a.jsonl"), identity=_identity(tmp_path)))
    Orchestrator(Config(tasks_path=str(tmp_path / "b.jsonl"), identity=None))
    with pytest.raises(executor.IdentityUnavailable):
        executor.spawn_env(claude=False)


def test_E_a_gate_for_an_identity_node_refuses_without_ever_loading_config(tmp_path, monkeypatch):
    from agentco_harness import beads as beads_mod

    node = _identity_yaml(tmp_path)
    ran = []
    monkeypatch.setattr(beads_mod.subprocess, "run", lambda *a, **k: ran.append(a))
    out = Beads(str(node / "tasks.jsonl"))._run_one_stage("true", str(node), 5)
    assert out["passed"] is False and "identity_unavailable" in out["output_tail"]
    assert ran == []


def test_E_control_a_node_without_identity_still_runs_its_gate(tmp_path):
    node = tmp_path / "plain"
    node.mkdir()
    (node / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    assert Beads(str(node / "tasks.jsonl"))._run_one_stage("true", str(node), 5)["passed"]
