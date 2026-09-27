"""Tests for `judge_jev`'s `--jev-url`/`JEV_URL` override (decider replay).

No network: `urllib.request.urlopen` is monkeypatched, exactly as
`tests/test_eval_sopbench_judge_gate.py` stubs `laya_ask`'s HTTP call. The
properties pinned are the ones a local wire-compatible judge server (e.g.
`decider.serve` behind `--jev-url http://127.0.0.1:4250/v1/systemone`) depends
on: the override actually changes the request URL, a loopback host sends no
Authorization header and never touches TYPESAFE_API_KEY, and a non-loopback
URL keeps requiring a key exactly as the unmodified TypeSafe path always did.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "eval"))

import sopbench_judge as J  # noqa: E402

ROW = {
    "_id": "r1",
    "truth": "held",
    "evidence": {
        "step_body": "room_type must be available",
        "tool_history": ["show_available_rooms(room_type='single')"],
        "transcript": ["(agent checked availability)"],
    },
}


def _fake_response(choice="held", confidence=0.9):
    body = json.dumps({
        "model": "decider-test",
        "answers": {"verdict": {"choice": choice, "confidence": confidence,
                                 "probabilities": {"held": confidence, "not_held": 1 - confidence}}},
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def test_jev_url_override_hits_the_given_url_not_typesafes(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _fake_response()

    monkeypatch.setattr(J.urllib.request, "urlopen", fake_urlopen)
    out = J.judge_jev([ROW], url="http://127.0.0.1:4250/v1/systemone")

    assert seen["url"] == "http://127.0.0.1:4250/v1/systemone"
    assert out[0]["passed"] is True
    assert out[0]["confidence"] == 0.9


def test_jev_url_local_host_sends_no_auth_header_and_needs_no_key(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["headers"] = dict(req.headers)
        return _fake_response()

    monkeypatch.setattr(J.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    # even an unreadable ~/.claude/.env must not raise: local hosts never call _jev_key()
    monkeypatch.setattr(J, "_jev_key", lambda: (_ for _ in ()).throw(
        AssertionError("_jev_key() must not be called for a local --jev-url")))

    out = J.judge_jev([ROW], url="http://127.0.0.1:4250/v1/systemone")

    assert "Authorization" not in seen["headers"]
    assert out[0]["passed"] is True


def test_jev_url_env_var_is_used_when_no_explicit_url_given(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        return _fake_response()

    monkeypatch.setattr(J.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("JEV_URL", "http://localhost:4251/v1/systemone")

    J.judge_jev([ROW])

    assert seen["url"] == "http://localhost:4251/v1/systemone"


def test_default_url_is_typesafes_and_still_requires_a_key(monkeypatch):
    monkeypatch.delenv("JEV_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(J, "_JEV_ENV_FILE", Path("/nonexistent/.env"))

    with pytest.raises(SystemExit):
        J.judge_jev([ROW])


def test_jev_url_is_local_recognises_loopback_hosts_only():
    assert J._jev_url_is_local("http://127.0.0.1:4250/v1/systemone")
    assert J._jev_url_is_local("http://localhost:4251/v1/systemone")
    assert not J._jev_url_is_local("https://api.typesafe.ai/v1/systemone")
