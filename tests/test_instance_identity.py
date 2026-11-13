"""Per-instance identity: child credentials come ONLY from the named connections.

Spec: docs/connections.md ("Instance config"). Fabricated names throughout.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from agentco_harness import connections, executor
from agentco_harness.beads import DISPATCH_REFUSAL_KEY, Beads, TaskStatus
from agentco_harness.config import Config, IdentityConfig
from agentco_harness.doctor import collect
from agentco_harness.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_CONNECTIONS_DIR", str(tmp_path / "conns"))
    # Ambient credentials the child must NOT inherit when an identity is set.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY_UNATTENDED", "ambient-unattended")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ambient.sock")
    envfile = tmp_path / "claude.env"
    envfile.write_text("ANTHROPIC_API_KEY_UNATTENDED=file-unattended\nANTHROPIC_API_KEY=file-key\n")
    monkeypatch.setattr(executor, "_CANONICAL_ENV_FILE", envfile)
    yield
    executor.configure_identity(None)


def _add(name, kind, secret=None, **kw):
    return connections.save(connections.Connection(name=name, kind=kind, **kw), secret)


def _acme(tmp_path, tag="acme", auth="subscription"):
    _add(f"{tag}-claude", "claude_code", f"tok-{tag}", auth=auth,
         config_dir=str(tmp_path / f"{tag}-cfg"))
    _add(f"{tag}-github", "github", ssh_key=str(tmp_path / f"{tag}_key"))
    return IdentityConfig(name=tag, claude=f"{tag}-claude", github=f"{tag}-github")


def _fake_claude(tmp_path, dump):
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\nenv > {dump}\necho '{{\"result\": \"ok\"}}'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


def _child_env(tmp_path, identity):
    dump = tmp_path / "env.txt"
    dump.unlink(missing_ok=True)
    executor.configure_identity(identity)
    res = executor.run_claude_task("hi", claude_bin=_fake_claude(tmp_path, dump))
    assert res.success, res.error
    return dict(l.split("=", 1) for l in dump.read_text().splitlines() if "=" in l)


# ---- executor ---------------------------------------------------------------

def test_subscription_identity_env(tmp_path):
    env = _child_env(tmp_path, _acme(tmp_path))
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "acme-cfg")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-acme"
    assert "acme_key" in env["GIT_SSH_COMMAND"]
    for leaked in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_UNATTENDED", "SSH_AUTH_SOCK"):
        assert leaked not in env


def test_api_key_identity_is_the_inverse(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-oauth")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-cfg"))
    env = _child_env(tmp_path, _acme(tmp_path, auth="api_key"))
    assert env["ANTHROPIC_API_KEY"] == "tok-acme"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "acme-cfg")


def test_two_identities_get_different_envs(tmp_path):
    a = _child_env(tmp_path, _acme(tmp_path, "acme"))
    b = _child_env(tmp_path, _acme(tmp_path, "globex"))
    assert a["CLAUDE_CODE_OAUTH_TOKEN"] != b["CLAUDE_CODE_OAUTH_TOKEN"]
    assert a["CLAUDE_CONFIG_DIR"] != b["CLAUDE_CONFIG_DIR"]
    assert a["GIT_SSH_COMMAND"] != b["GIT_SSH_COMMAND"]


def test_without_identity_behaviour_is_unchanged(tmp_path):
    env = _child_env(tmp_path, None)
    assert env["ANTHROPIC_API_KEY"] == "ambient-unattended"  # the unattended-billing path
    assert "GIT_SSH_COMMAND" not in env


def test_missing_connection_refuses_before_spawning(tmp_path):
    dump = tmp_path / "env.txt"
    executor.configure_identity(IdentityConfig(name="acme", claude="nope"))
    res = executor.run_claude_task("hi", claude_bin=_fake_claude(tmp_path, dump))
    assert not res.success and "identity_unavailable" in res.error
    assert not dump.exists()  # the fake claude never ran


def test_wrong_kind_and_loose_secret_are_refused(tmp_path):
    ident = _acme(tmp_path)
    with pytest.raises(executor.IdentityUnavailable):
        executor.check_identity(IdentityConfig(name="x", claude=ident.github))
    secret = Path(connections.load(ident.claude).secret_file)
    secret.chmod(0o644)
    with pytest.raises(executor.IdentityUnavailable) as ei:
        executor.check_identity(ident)
    assert "tok-acme" not in str(ei.value)


def test_agy_route_gets_github_env_and_nothing_else(tmp_path, monkeypatch):
    ident = _acme(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-cfg"))
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw["env"]
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    executor.configure_identity(ident)
    assert executor.run_agy_task("hi", agy_bin="agy").success
    env = captured["env"]
    assert "acme_key" in env["GIT_SSH_COMMAND"]
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "SSH_AUTH_SOCK"):
        assert k not in env


# ---- orchestrator refusal ---------------------------------------------------

def _orch(tmp_path, identity):
    cfg = Config(tasks_path=str(tmp_path / "tasks.jsonl"), identity=identity)
    return Orchestrator(cfg), Beads(str(tmp_path / "tasks.jsonl"))


def test_dispatch_is_refused_with_identity_unavailable(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    orch, beads = _orch(tmp_path, IdentityConfig(name="acme", claude="missing"))
    task = beads.create(title="t", description="d", assigned_agent="claude", metadata={})
    assert orch._execute_cycle_task(task) is False
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.metadata[DISPATCH_REFUSAL_KEY]["code"] == "identity_unavailable"
    assert spawned == []


def test_no_identity_does_not_refuse(tmp_path):
    orch, beads = _orch(tmp_path, None)
    assert orch._authorize_identity(beads.create(title="t", description="d")) is True


def test_completions_resolve_from_identity_connections(tmp_path, monkeypatch):
    ident = _acme(tmp_path)
    _add("acme-litellm", "openai_compat", "sk-acme", base_url="http://127.0.0.1:4000/v1", model="m1")
    ident.completions = ["acme-litellm"]
    seen = {}

    def fake_complete(prompt, provider, *, model=None, timeout=0, **kw):
        seen["provider"] = provider.name
        seen["key"] = provider.key()
        return executor.ExecResult(success=True, output="hi", exit_code=0, duration_seconds=0.1, error=None)

    import agentco_harness.completion as completion
    monkeypatch.setattr(completion, "complete", fake_complete)
    orch, beads = _orch(tmp_path, ident)
    task = beads.create(title="t", description="d", assigned_agent="acme-litellm")
    orch._execute_completion_task(task, "acme-litellm")
    assert seen == {"provider": "acme-litellm", "key": "sk-acme"}
    assert "AGENTCO_CONN_ACME_LITELLM_KEY" not in os.environ  # overlay restored


# ---- config + doctor --------------------------------------------------------

def test_config_parses_identity(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"identity": {"name": "acme", "claude": "acme-claude",
                                              "github": "acme-github", "completions": ["a"], "agy": "agy"}}))
    ident = Config.load(p).identity
    assert (ident.name, ident.claude, ident.github, ident.completions, ident.agy) == (
        "acme", "acme-claude", "acme-github", ["a"], "agy")
    p.write_text("tasks_path: t.jsonl\n")
    assert Config.load(p).identity is None


def _doctor(tmp_path, monkeypatch, identity):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTCO_LAUNCH_AGENTS_DIR", str(tmp_path / "_la"))
    (tmp_path / "_la").mkdir(exist_ok=True)
    monkeypatch.setattr("agentco_harness.doctor.loaded_launchd_labels", lambda: {"fixture"})
    (tmp_path / "company").mkdir(exist_ok=True)
    data = {"tasks_path": "tasks.jsonl", "llm": {"default_provider": "lmstudio", "default_model": "local"}}
    if identity:
        data["identity"] = identity
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data))
    report = collect(str(tmp_path / "config.yaml"))
    return [f for f in report.findings if f.check == "identity.connections"]


def _stamp(name, when):
    c = connections.load(name)
    connections.save(connections.replace(c, validated_at=when, validated="ok: fine"), None)


def test_doctor_without_identity_notes_it(tmp_path, monkeypatch):
    fs = _doctor(tmp_path, monkeypatch, None)
    assert [f.status for f in fs] == ["info"] and "identity" in fs[0].message


def test_doctor_missing_connection_is_broken(tmp_path, monkeypatch):
    fs = _doctor(tmp_path, monkeypatch, {"name": "acme", "claude": "ghost"})
    assert any(f.status == "broken" and "ghost" in f.message for f in fs)


def test_doctor_wrong_kind_and_loose_secret_are_broken(tmp_path, monkeypatch):
    ident = _acme(tmp_path)
    fs = _doctor(tmp_path, monkeypatch, {"claude": ident.github})
    assert any(f.status == "broken" and "expected claude_code" in f.message for f in fs)
    Path(connections.load(ident.claude).secret_file).chmod(0o640)
    fs = _doctor(tmp_path, monkeypatch, {"claude": ident.claude})
    assert any(f.status == "broken" and "0600" in f.message for f in fs)


def test_doctor_validation_age(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    ident = _acme(tmp_path)
    cfg = {"claude": ident.claude}
    fs = _doctor(tmp_path, monkeypatch, cfg)
    assert any(f.status == "degraded" and "never been validated" in f.message for f in fs)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    _stamp(ident.claude, (datetime.now(timezone.utc) - timedelta(days=45)).strftime(fmt))
    fs = _doctor(tmp_path, monkeypatch, cfg)
    assert any(f.status == "degraded" and "30 days" in f.message for f in fs)
    _stamp(ident.claude, datetime.now(timezone.utc).strftime(fmt))
    fs = _doctor(tmp_path, monkeypatch, cfg)
    assert [f.status for f in fs] == ["ok"]


# ---- routes not yet wired for identity refuse rather than run ambient -----------

def test_zai_route_refuses_under_identity(tmp_path):
    executor.configure_identity(_acme(tmp_path))
    res = executor.run_zai_store_backed_task("t-1", zai_api_key="zk", claude_bin="/nonexistent/claude")
    assert not res.success
    assert executor.IDENTITY_UNAVAILABLE in (res.error or "")


def test_forge_route_refuses_under_identity(tmp_path):
    executor.configure_identity(_acme(tmp_path))
    res = executor._run_forge_task_inner("hi", 30, None, "/nonexistent/codex", None)
    assert not res.success
    assert executor.IDENTITY_UNAVAILABLE in (res.error or "")
