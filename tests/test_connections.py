"""Machine-level connections registry. No test touches a real claude/agy/network/ssh."""

import json
import os
import stat
import urllib.error

import pytest

from agentco_harness import connections as c
from agentco_harness.completion import Provider

SECRET = "sk-planted-SECRET-0123456789"  # leakguard: allow (planted fixture)


@pytest.fixture(autouse=True)
def reg(tmp_path, monkeypatch):
    d = tmp_path / "conns"
    monkeypatch.setenv("AGENTCO_CONNECTIONS_DIR", str(d))
    return d


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def claude(name="acme-claude", auth="subscription", **kw):
    return c.Connection(name=name, kind="claude_code", auth=auth, config_dir="~/.claude-acme", **kw)


def gh(name="acme-github", **kw):
    return c.Connection(name=name, kind="github", ssh_key="/keys/acme", gitconfig="/cfg/acme.gitconfig",
                        gh_config_dir="/cfg/gh-acme", **kw)


def oai(name="acme-litellm", **kw):
    return c.Connection(name=name, kind="openai_compat", base_url="https://llm.example.com/v1",
                        model="acme-model", **kw)


class FakeRunner:
    def __init__(self, rc=0, out="OK", err=""):
        self.calls, self.rc, self.out, self.err = [], rc, out, err

    def __call__(self, argv, env, timeout):
        self.calls.append((argv, env))
        return self.rc, self.out, self.err


class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- directory + modes -------------------------------------------------------

def test_dir_env_override_and_default(reg, monkeypatch):
    assert c.connections_dir() == reg
    monkeypatch.delenv("AGENTCO_CONNECTIONS_DIR")
    assert str(c.connections_dir()).endswith(".config/agentco/connections")


def test_save_modes(reg):
    c.save(claude(), SECRET)
    assert mode(reg) == 0o700
    assert mode(reg / "acme-claude.json") == 0o600
    assert mode(reg / "acme-claude.secret") == 0o600


def test_secret_never_in_json(reg):
    c.save(claude(), SECRET)
    raw = (reg / "acme-claude.json").read_text()
    assert SECRET not in raw
    data = json.loads(raw)
    assert data["schema"] == "agentco.connection/1"
    assert data["secret_file"].endswith("acme-claude.secret")
    assert (reg / "acme-claude.secret").read_text().strip() == SECRET


def test_roundtrip_and_list_sorted(reg):
    c.save(gh(), None)
    c.save(claude(), SECRET)
    assert c.load("acme-claude") == c.list_all()[0]
    assert [x.name for x in c.list_all()] == ["acme-claude", "acme-github"]


def test_loose_secret_refused(reg):
    c.save(claude(), SECRET)
    os.chmod(reg / "acme-claude.secret", 0o644)
    with pytest.raises(c.ConnectionError_, match="0600") as e:
        c.load("acme-claude")
    assert SECRET not in str(e.value)
    with pytest.raises(c.ConnectionError_, match="0600"):
        c.read_secret(c.Connection(name="acme-claude", kind="claude_code", auth="subscription",
                                   secret_file=str(reg / "acme-claude.secret")))


def test_read_secret(reg):
    conn = c.save(claude(), SECRET)
    assert c.read_secret(conn) == SECRET
    assert c.read_secret(gh()) is None


def test_missing_secret_file_raises_on_read(reg):
    conn = c.save(claude(), SECRET)
    os.unlink(conn.secret_file)
    with pytest.raises(c.ConnectionError_, match="missing"):
        c.read_secret(conn)


def test_load_missing(reg):
    with pytest.raises(c.ConnectionError_, match="not found"):
        c.load("nope")


# ---- names + rules -----------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "Acme", "-acme", "a b", "a/b", "../x", "a" * 64, "a.b"])
def test_bad_names(bad):
    with pytest.raises(c.ConnectionError_, match="name"):
        c.save(gh(name=bad), None)


def test_good_names(reg):
    c.save(gh(name="a"), None)
    c.save(gh(name="0-" + "a" * 61), None)


def test_one_agy(reg):
    c.save(c.Connection(name="agy", kind="agy"), None)
    c.save(c.Connection(name="agy", kind="agy"), None)  # re-save same name ok
    with pytest.raises(c.ConnectionError_, match="agy"):
        c.save(c.Connection(name="agy-two", kind="agy"), None)


def test_claude_requires_valid_auth_and_secret(reg):
    with pytest.raises(c.ConnectionError_, match="auth"):
        c.save(claude(auth="magic"), SECRET)
    with pytest.raises(c.ConnectionError_, match="secret"):
        c.save(claude(), None)


def test_unknown_kind():
    with pytest.raises(c.ConnectionError_, match="kind"):
        c.save(c.Connection(name="x", kind="copilot"), None)


def test_resave_without_secret_keeps_it(reg):
    conn = c.save(claude(), SECRET)
    c.save(conn, None)
    assert c.read_secret(c.load("acme-claude")) == SECRET


def test_remove(reg):
    c.save(claude(), SECRET)
    c.remove("acme-claude")
    assert not list(reg.glob("acme-claude.*"))
    with pytest.raises(c.ConnectionError_):
        c.remove("acme-claude")


# ---- child_env / strip_keys --------------------------------------------------

def test_child_env_subscription(reg):
    conn = c.save(claude(), SECRET)
    env = c.child_env(conn)
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.claude-acme"),
                   "CLAUDE_CODE_OAUTH_TOKEN": SECRET}
    assert c.strip_keys(conn) == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}


def test_child_env_api_key(reg):
    conn = c.save(claude(auth="api_key"), SECRET)
    assert c.child_env(conn) == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.claude-acme"),
                                 "ANTHROPIC_API_KEY": SECRET}
    assert c.strip_keys(conn) == {"CLAUDE_CODE_OAUTH_TOKEN"}


def test_child_env_github():
    env = c.child_env(gh())
    assert env == {
        "GIT_SSH_COMMAND": "ssh -i /keys/acme -o IdentitiesOnly=yes -o IdentityAgent=none",
        "GIT_CONFIG_GLOBAL": "/cfg/acme.gitconfig",
        "GH_CONFIG_DIR": "/cfg/gh-acme",
    }
    assert "SSH_AUTH_SOCK" not in env
    assert "SSH_AUTH_SOCK" in c.strip_keys(gh())


def test_child_env_agy_and_oai():
    assert c.child_env(c.Connection(name="agy", kind="agy")) == {}
    assert c.child_env(oai()) == {}
    assert c.strip_keys(oai()) == set()


def test_ssh_key_with_spaces_quoted():
    env = c.child_env(c.Connection(name="g", kind="github", ssh_key="/my keys/id"))
    assert "'/my keys/id'" in env["GIT_SSH_COMMAND"]


# ---- validate ----------------------------------------------------------------

def test_validate_claude_ok_env_isolated(reg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    conn = c.save(claude(), SECRET)
    r = FakeRunner(out="OK")
    res = c.validate(conn, runner=r)
    assert res.ok and "ok" in res.detail
    argv, env = r.calls[0]
    assert argv[:2] == ["claude", "--print"] and "--max-turns" in argv
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET


def test_validate_claude_fail_redacts(reg):
    conn = c.save(claude(), SECRET)
    res = c.validate(conn, runner=FakeRunner(rc=1, out="", err=f"bad token {SECRET}"))
    assert not res.ok and SECRET not in res.detail


def test_validate_claude_no_ok(reg):
    conn = c.save(claude(), SECRET)
    assert not c.validate(conn, runner=FakeRunner(out="hello")).ok


def test_validate_missing_binary(reg):
    conn = c.save(claude(), SECRET)

    def boom(argv, env, timeout):
        raise FileNotFoundError("claude")

    res = c.validate(conn, runner=boom)
    assert not res.ok and "claude" in res.detail


def test_validate_agy():
    r = FakeRunner(out="OK")
    assert c.validate(c.Connection(name="agy", kind="agy"), runner=r).ok
    assert r.calls[0][0][0] == "agy" and "--dangerously-skip-permissions" in r.calls[0][0]


def test_validate_github_ok_and_user_check():
    r = FakeRunner(rc=1, out="", err="Hi acme-bot! You've successfully authenticated")
    assert c.validate(gh(expect_github_user="acme-bot"), runner=r).ok
    argv, env = r.calls[0]
    assert "IdentitiesOnly=yes" in argv and "SSH_AUTH_SOCK" not in env
    bad = c.validate(gh(expect_github_user="someone-else"), runner=r)
    assert not bad.ok and "acme-bot" in bad.detail
    assert not c.validate(gh(), runner=FakeRunner(rc=255, err="Permission denied")).ok


def test_validate_openai_ok(reg):
    seen = {}

    def opener(req, timeout):
        seen["req"] = req
        return FakeResp(json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode())

    conn = c.save(oai(secret_file=""), SECRET)
    assert conn.secret_file  # a key was supplied
    res = c.validate(conn, opener=opener)
    assert res.ok
    req = seen["req"]
    assert req.full_url == "https://llm.example.com/v1/chat/completions"
    assert req.get_header("Authorization") == f"Bearer {SECRET}"
    assert json.loads(req.data)["max_tokens"] == 5


def test_validate_openai_errors(reg):
    def http(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    def down(req, timeout):
        raise urllib.error.URLError("refused")

    def empty(req, timeout):
        return FakeResp(b"{}")

    assert "401" in c.validate(oai(), opener=http).detail
    assert not c.validate(oai(), opener=down).ok
    assert not c.validate(oai(), opener=empty).ok


def test_validate_openai_models_only():
    seen = {}

    def opener(req, timeout):
        seen["u"] = req.full_url
        return FakeResp(b'{"data": []}')

    assert c.validate(oai(), opener=opener, check="models").ok
    assert seen["u"].endswith("/models")


def test_with_validation_stamps():
    conn = c.with_validation(oai(), c.ValidationResult(True, "ok: fine"))
    assert conn.validated == "ok: fine" and conn.validated_at.endswith("Z")
    assert c.with_validation(oai(), c.ValidationResult(False, "no")).validated.startswith("failed")


# ---- openai_compat -> Provider ----------------------------------------------

def test_to_provider(reg):
    conn = c.save(oai(), SECRET)
    p = c.to_provider(conn)
    assert isinstance(p, Provider)
    assert (p.name, p.base_url, p.model, p.requires_key) == ("acme-litellm", "https://llm.example.com/v1",
                                                            "acme-model", True)
    assert p.api_key_env and SECRET not in repr(p)
    assert c.provider_env(conn) == {p.api_key_env: SECRET}


def test_to_provider_keyless():
    p = c.to_provider(oai())
    assert p.api_key_env is None and p.requires_key is False
    assert c.provider_env(oai()) == {}


def test_to_provider_wrong_kind():
    with pytest.raises(c.ConnectionError_):
        c.to_provider(gh())


# ---- live (opt-in) -----------------------------------------------------------

@pytest.mark.skipif(os.environ.get("AGENTCO_LIVE_CONNECT") != "1", reason="set AGENTCO_LIVE_CONNECT=1")
def test_live_validate_all_registered():
    for conn in c.list_all():
        assert c.validate(conn).ok, conn.name
