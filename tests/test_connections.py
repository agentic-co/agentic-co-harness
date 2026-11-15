"""Machine-level connections registry. No test touches a real claude/agy/network/ssh."""

import json
from dataclasses import replace
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


# ---- peer review #28 ---------------------------------------------------------

POISON = {
    "ANTHROPIC_BASE_URL": "https://evil.invalid", "GH_TOKEN": "ghp-ambient",
    "OPENAI_API_KEY": "sk-ambient", "AWS_ACCESS_KEY_ID": "AKIAAMBIENT",  # leakguard: allow (planted fixture)
    "SSH_AUTH_SOCK": "/tmp/agent.sock", "CLAUDE_CODE_USE_BEDROCK": "1",
}


def _poison(monkeypatch):
    for k, v in POISON.items():
        monkeypatch.setenv(k, v)


def test_probe_env_is_allowlisted_not_ambient(reg, monkeypatch):
    _poison(monkeypatch)
    env = c.probe_env(c.save(claude(), SECRET))
    assert not set(POISON) & set(env)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET


def test_agy_probe_env_is_clean(monkeypatch):
    _poison(monkeypatch)
    r = FakeRunner()
    c.validate(c.Connection(name="agy", kind="agy"), runner=r)
    assert not set(POISON) & set(r.calls[0][1])


def test_github_probe_env_is_clean(monkeypatch):
    _poison(monkeypatch)
    r = FakeRunner(rc=1, err="Hi acme-bot!")
    c.validate(gh(), runner=r)
    assert not set(POISON) & set(r.calls[0][1])


def _serve(handler_cls):
    import threading
    from http.server import HTTPServer
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_bearer_not_sent_across_host_redirect(reg):
    from http.server import BaseHTTPRequestHandler
    got = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{}]}')
        do_GET = do_POST

        def log_message(self, *a):
            pass

    sink = _serve(Sink)

    class Redir(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/x")
            self.end_headers()
        do_GET = do_POST

        def log_message(self, *a):
            pass

    front = _serve(Redir)
    conn = c.Connection(name="acme-local", kind="openai_compat", model="m",
                        base_url=f"http://127.0.0.1:{front.server_port}/v1")
    res = c.validate(conn, secret=SECRET, check="models")
    front.shutdown()
    sink.shutdown()
    assert not res.ok
    assert got == []


@pytest.mark.parametrize("url", ["http://llm.example.com/v1", "ftp://x/v1", "llm.example.com", "https://"])
def test_non_tls_base_url_refused(reg, url):
    with pytest.raises(c.ConnectionError_):
        c.save(replace(oai(), base_url=url), SECRET)


@pytest.mark.parametrize("url", ["http://localhost:4000/v1", "http://127.0.0.1:4000/v1", "http://[::1]:4000/v1",
                                 "https://llm.example.com/v1"])
def test_loopback_http_and_https_allowed(reg, url):
    c.save(replace(oai(), base_url=url), SECRET)


def test_probe_refuses_hand_edited_http_url(reg):
    called = []
    conn = c.Connection(name="acme-x", kind="openai_compat", model="m", base_url="http://llm.example.com/v1")
    res = c.validate(conn, opener=lambda *a, **k: called.append(1), secret=SECRET)
    assert not res.ok and not called


def test_write_private_ignores_leftover_tmp_and_is_0600(reg, tmp_path):
    reg.mkdir(parents=True, exist_ok=True)
    stale = reg / "acme-claude.secret.tmp"
    stale.write_text("old")
    os.chmod(stale, 0o666)
    c.save(claude(), SECRET)
    assert mode(reg / "acme-claude.secret") == 0o600
    assert stale.read_text() == "old"  # untouched, never reopened
    assert not [p for p in reg.iterdir() if p.name.endswith(".tmp") and p != stale]


def test_custom_secret_parent_dir_is_0700(reg, tmp_path):
    sf = tmp_path / "newdir" / "tok"
    c.save(claude(secret_file=str(sf)), SECRET)
    assert mode(sf.parent) == 0o700


def test_remove_never_deletes_outside_registry(reg, tmp_path):
    victim = tmp_path / "precious.txt"
    victim.write_text("keep")
    os.chmod(victim, 0o600)
    c.save(claude(), SECRET)
    p = reg / "acme-claude.json"
    d = json.loads(p.read_text())
    d["secret_file"] = str(victim)
    p.write_text(json.dumps(d))
    c.remove("acme-claude")
    assert victim.read_text() == "keep"


# ---- second review (#28): the dispatch path and loaded connections ----------

def _redirecting_pair():
    from http.server import BaseHTTPRequestHandler
    got = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')
        do_GET = do_POST  # a 302 turns the POST into a GET that keeps the header

        def log_message(self, *a):
            pass

    sink = _serve(Sink)

    class Redir(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/v1/chat/completions")
            self.end_headers()

        def log_message(self, *a):
            pass

    front = _serve(Redir)
    return front, sink, got


def test_dispatch_does_not_forward_key_across_host_redirect(reg, monkeypatch):
    from agentco_harness import completion
    front, sink, got = _redirecting_pair()
    conn = c.save(c.Connection(name="acme-local", kind="openai_compat", model="m",
                               base_url=f"http://127.0.0.1:{front.server_port}/v1"), SECRET)
    for k, v in c.provider_env(conn).items():
        monkeypatch.setenv(k, v)
    res = completion.complete("hi", c.to_provider(conn), timeout=5)
    front.shutdown()
    sink.shutdown()
    assert not res.success
    assert got == []


def _hand_edit(reg, name, **fields_):
    p = reg / f"{name}.json"
    data = json.loads(p.read_text())
    data.update(fields_)
    p.write_text(json.dumps(data))


def test_hand_edited_http_base_url_refused_at_load(reg):
    c.save(oai(), SECRET)
    _hand_edit(reg, oai().name, base_url="http://llm.example.com/v1")
    with pytest.raises(c.ConnectionError_):
        c.load(oai().name)


def test_hand_edited_http_base_url_refused_at_to_provider(reg):
    conn = replace(oai(), base_url="http://llm.example.com/v1")
    with pytest.raises(c.ConnectionError_):
        c.to_provider(conn)
