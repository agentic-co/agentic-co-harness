import json
import os
import stat

import pytest
from click.testing import CliRunner

from agentco_harness import connections as c
from agentco_harness.cli import main

SECRET = "sk-planted-SECRET-0123456789"  # leakguard: allow (planted fixture)


@pytest.fixture(autouse=True)
def reg(tmp_path, monkeypatch):
    d = tmp_path / "conns"
    monkeypatch.setenv("AGENTCO_CONNECTIONS_DIR", str(d))
    monkeypatch.chdir(tmp_path)
    return d


@pytest.fixture
def probes(monkeypatch):
    """Make every probe succeed without touching claude/agy/ssh/network."""
    calls = []

    def fake(conn, **kw):
        calls.append(conn.name)
        return c.ValidationResult(True, "ok: fake")

    monkeypatch.setattr(c, "validate", fake)
    return calls


def run(*args, input=None):
    return CliRunner().invoke(main, ["connect", *args], input=input, catch_exceptions=False)


ADD = ["add", "claude_code", "--name", "acme-claude", "--auth", "subscription",
       "--config-dir", "~/.claude-acme", "--secret-stdin"]


def test_add_flags_validates_and_saves(reg, probes):
    r = run(*ADD, input=SECRET + "\n")
    assert r.exit_code == 0, r.output
    conn = c.load("acme-claude")
    assert conn.validated.startswith("ok") and probes == ["acme-claude"]
    assert c.read_secret(conn) == SECRET
    assert SECRET not in r.output


def test_add_failed_validation_not_saved(reg, monkeypatch):
    monkeypatch.setattr(c, "validate", lambda conn, **kw: c.ValidationResult(False, "failed: nope"))
    r = CliRunner().invoke(main, ["connect", *ADD], input=SECRET + "\n")
    assert r.exit_code != 0 and "nope" in r.output and SECRET not in r.output
    assert not list(reg.glob("*.json")) if reg.exists() else True


def test_add_no_validate_marks_unvalidated(reg, probes):
    r = run(*ADD, "--no-validate", input=SECRET + "\n")
    assert r.exit_code == 0 and probes == []
    assert "unvalidated" in c.load("acme-claude").validated


def test_add_agy_second_refused(reg, probes):
    assert run("add", "agy", "--name", "agy").exit_code == 0
    r = CliRunner().invoke(main, ["connect", "add", "agy", "--name", "agy-b"])
    assert r.exit_code != 0 and "agy" in r.output


def test_add_github_and_openai(reg, probes):
    assert run("add", "github", "--name", "acme-github", "--ssh-key", "/k/acme",
               "--gitconfig", "/k/gc", "--gh-config-dir", "/k/gh",
               "--expect-github-user", "acme-bot").exit_code == 0
    assert c.load("acme-github").expect_github_user == "acme-bot"
    assert run("add", "openai_compat", "--name", "acme-litellm", "--base-url", "https://llm.example.com/v1",
               "--model", "m1", "--secret-stdin", input=SECRET + "\n").exit_code == 0
    assert c.read_secret(c.load("acme-litellm")) == SECRET


def test_list_never_shows_secret(reg, probes):
    run(*ADD, input=SECRET + "\n")
    run("add", "agy", "--name", "agy")
    r = run("list")
    assert "acme-claude" in r.output and "claude_code" in r.output and "subscription" in r.output
    assert SECRET not in r.output
    j = run("list", "--json")
    assert SECRET not in j.output
    assert {x["name"] for x in json.loads(j.output)} == {"acme-claude", "agy"}


def test_list_empty(reg):
    assert "No connections" in run("list").output


def test_test_updates_validated_at(reg, probes):
    run(*ADD, "--no-validate", input=SECRET + "\n")
    r = run("test", "acme-claude")
    assert r.exit_code == 0 and "ok" in r.output
    assert c.load("acme-claude").validated == "ok: fake"
    assert c.read_secret(c.load("acme-claude")) == SECRET


def test_test_failure_exit_code(reg, monkeypatch):
    run(*ADD, "--no-validate", input=SECRET + "\n")
    monkeypatch.setattr(c, "validate", lambda conn, **kw: c.ValidationResult(False, "failed: x"))
    r = CliRunner().invoke(main, ["connect", "test", "acme-claude"])
    assert r.exit_code != 0
    assert c.load("acme-claude").validated.startswith("failed")


def test_remove_needs_confirmation(reg, probes):
    run(*ADD, input=SECRET + "\n")
    r = CliRunner().invoke(main, ["connect", "remove", "acme-claude"], input="n\n")
    assert r.exit_code != 0 and c.load("acme-claude")
    assert run("remove", "acme-claude", "--yes").exit_code == 0
    assert not list(reg.glob("acme-claude.*"))


def test_interactive_flow_hidden_secret(reg, probes):
    # choice 1 = claude_code; then name, auth, config dir; secret via hidden prompt
    r = run(input="1\nacme-claude\nsubscription\n~/.claude-acme\n" + SECRET + "\n")
    assert r.exit_code == 0, r.output
    assert SECRET not in r.output  # hidden input is not echoed
    assert c.read_secret(c.load("acme-claude")) == SECRET
    assert "agy" in r.output and "openai_compat" in r.output  # the kind selector was shown


def test_interactive_agy(reg, probes):
    r = run(input="2\nagy\n")
    assert r.exit_code == 0, r.output
    assert c.load("agy").kind == "agy"


def test_rendered_outputs_have_no_secret(reg, probes):
    outs = [run(*ADD, input=SECRET + "\n").output, run("list").output, run("list", "--json").output,
            run("test", "acme-claude").output]
    bad = CliRunner().invoke(main, ["connect", "add", "claude_code", "--name", "Bad Name", "--auth",
                                    "subscription", "--secret-stdin"], input=SECRET + "\n")
    outs.append(bad.output)
    assert bad.exit_code != 0
    assert all(SECRET not in o for o in outs)
    for f in reg.glob("*.json"):
        assert SECRET not in f.read_text()
