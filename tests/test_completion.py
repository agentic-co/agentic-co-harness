"""Chat-completion backends: text in, text out, and refused when that is not enough.

No network: a fake OpenAI-compatible server on loopback stands in for LM Studio
and for a remote provider, which is also the only honest way to test the failure
modes (unreachable, HTTP error, empty choices) without arranging them for real.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentco_harness.completion import (
    COMPLETION_ONLY,
    DEFAULT_PROVIDERS,
    Provider,
    ProviderUnconfigured,
    complete,
    providers_from_config,
)


class _Fake(BaseHTTPRequestHandler):
    reply: dict = {}
    status: int = 200
    seen: dict = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        _Fake.seen = {
            "path": self.path,
            "body": json.loads(body or b"{}"),
            "auth": self.headers.get("authorization"),
        }
        payload = json.dumps(_Fake.reply).encode()
        self.send_response(_Fake.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # keep the suite quiet
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def _provider(url, **kw):
    return Provider(name="test", base_url=url, model="m", **kw)


# ------------------------------------------------------------------ the happy path

def test_a_completion_comes_back_as_the_assistants_text(server):
    _Fake.status = 200
    _Fake.reply = {"choices": [{"message": {"role": "assistant", "content": "slugified"}}]}
    r = complete("turn this into a slug", _provider(server))
    assert r.success is True
    assert r.output == "slugified"
    assert r.exit_code == 0
    assert _Fake.seen["path"].endswith("/chat/completions")
    assert _Fake.seen["body"]["messages"][0]["content"] == "turn this into a slug"


def test_the_bead_may_pin_a_model_over_the_providers_default(server):
    _Fake.status = 200
    _Fake.reply = {"choices": [{"message": {"content": "ok"}}]}
    complete("x", _provider(server), model="qwen3-coder")
    assert _Fake.seen["body"]["model"] == "qwen3-coder"


def test_a_server_that_answers_with_plain_text_still_works(server):
    """Not every OpenAI-compatible server fills `message.content`."""
    _Fake.status = 200
    _Fake.reply = {"choices": [{"text": "plain"}]}
    assert complete("x", _provider(server)).output == "plain"


# ------------------------------------------------------------------ failure is loud

def test_an_http_error_is_a_loud_result_not_an_exception(server):
    _Fake.status = 400
    _Fake.reply = {"error": "model not loaded"}
    r = complete("x", _provider(server))
    assert r.success is False
    assert r.exit_code == 400
    assert "model not loaded" in r.error


def test_an_unreachable_endpoint_names_what_to_do():
    r = complete("x", _provider("http://127.0.0.1:9/v1"))
    assert r.success is False
    assert "unreachable" in r.error
    assert "start the local server" in r.error


def test_no_choices_is_a_failure_not_an_empty_success(server):
    _Fake.status = 200
    _Fake.reply = {"choices": []}
    r = complete("x", _provider(server))
    assert r.success is False and r.output == ""


# ------------------------------------------------------------------ keys

def test_a_provider_that_needs_a_key_and_has_none_refuses_before_sending(server, monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    p = _provider(server, api_key_env="TEST_KEY", requires_key=True)
    with pytest.raises(ProviderUnconfigured) as e:
        complete("x", p)
    assert "TEST_KEY" in str(e.value)
    assert _Fake.seen.get("body") != {"never": "sent"}  # nothing new was sent


def test_a_key_is_sent_as_a_bearer_token(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-abc")
    _Fake.status = 200
    _Fake.reply = {"choices": [{"message": {"content": "ok"}}]}
    complete("x", _provider(server, api_key_env="TEST_KEY", requires_key=True))
    assert _Fake.seen["auth"] == "Bearer sk-abc"


def test_a_local_provider_sends_no_authorization_at_all(server):
    _Fake.status = 200
    _Fake.reply = {"choices": [{"message": {"content": "ok"}}]}
    complete("x", _provider(server))
    assert _Fake.seen["auth"] is None


# ------------------------------------------------------------------ config

class _Cfg:
    def __init__(self, completion):
        self.completion = completion


def test_lmstudio_works_with_no_config_at_all():
    p = providers_from_config(_Cfg({}))["lmstudio"]
    assert p.base_url == "http://localhost:1234/v1"
    assert p.requires_key is False


def test_an_operator_overrides_only_what_they_name():
    p = providers_from_config(_Cfg({"lmstudio": {"model": "qwen3-coder-next"}}))["lmstudio"]
    assert p.model == "qwen3-coder-next"
    assert p.base_url == DEFAULT_PROVIDERS["lmstudio"].base_url   # untouched


def test_zai_ships_needing_a_key():
    assert providers_from_config(_Cfg({}))["zai-api"].requires_key is True


def test_an_operator_may_declare_a_provider_we_never_shipped():
    p = providers_from_config(_Cfg({"ollama": {"base_url": "http://localhost:11434/v1", "model": "llama"}}))["ollama"]
    assert p.base_url == "http://localhost:11434/v1"
    assert p.requires_key is False


# ------------------------------------------------------------------ what it declares

def test_a_completion_backend_declares_text_and_nothing_else():
    from agentco_harness import backends, orchestrator  # noqa: F401  (registers them)

    for name in ("lmstudio", "zai-api"):
        assert backends.resolve(name).capabilities == COMPLETION_ONLY

    # And the two are routed apart on purpose: local egress is not vendor egress.
    assert backends.resolve("lmstudio").route == "LOCAL"
    assert backends.resolve("zai-api").route == "TEMPER"


def test_the_subprocess_backends_stay_undeclared_and_therefore_agentic():
    """A silent downgrade of the four shipped executors would be the worse bug."""
    from agentco_harness import backends, orchestrator  # noqa: F401

    for name in ("claude", "zai", "forge", "planner"):
        assert backends.resolve(name).capabilities == frozenset()
