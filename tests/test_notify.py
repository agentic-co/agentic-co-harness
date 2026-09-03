"""Notify fan-out tests: channel routing, urgency, missing token, failure
tolerance. No network — urlopen is always monkeypatched."""

from __future__ import annotations

import urllib.parse

import agentco_harness.notify as notify_mod
from agentco_harness.config import NotifyConfig
from agentco_harness.notify import notify_event, send_telegram


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_urlopen(monkeypatch, calls: list, fail: bool = False):
    def fake(req, timeout=None):
        if fail:
            raise OSError("connection refused")
        calls.append(
            {
                "url": req.full_url,
                "body": req.data.decode(),
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake)


def test_send_telegram_builds_bot_api_call(monkeypatch):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    assert send_telegram("hello world", chat_id="12345", token="SECRET") is True
    assert calls[0]["url"] == "https://api.telegram.org/botSECRET/sendMessage"
    params = dict(urllib.parse.parse_qsl(calls[0]["body"]))
    assert params == {"chat_id": "12345", "text": "hello world"}


def test_routine_event_goes_to_telegram_only(monkeypatch):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    cfg = NotifyConfig(telegram_chat_id="99", url="http://localhost:31337/notify")

    delivered = notify_event(cfg, "cycle summary", urgent=False)

    assert delivered == ["telegram"]
    assert len(calls) == 1
    assert "api.telegram.org" in calls[0]["url"]


def test_urgent_event_hits_telegram_and_pulse(monkeypatch):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    cfg = NotifyConfig(telegram_chat_id="99", url="http://localhost:31337/notify")

    delivered = notify_event(cfg, "child is stale", urgent=True)

    assert delivered == ["telegram", "pulse"]
    assert len(calls) == 2


def test_missing_token_warns_and_drops_telegram(monkeypatch, capsys):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = NotifyConfig(telegram_chat_id="99")

    delivered = notify_event(cfg, "msg", urgent=False)

    assert delivered == []
    assert "TELEGRAM_BOT_TOKEN is unset" in capsys.readouterr().out


def test_channel_failure_is_warned_never_raised(monkeypatch, capsys):
    _capture_urlopen(monkeypatch, [], fail=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    cfg = NotifyConfig(telegram_chat_id="99")

    delivered = notify_event(cfg, "msg", urgent=True)

    assert delivered == []
    out = capsys.readouterr().out
    assert "telegram send failed" in out


def test_disabled_notify_sends_nothing(monkeypatch):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    cfg = NotifyConfig(enabled=False, telegram_chat_id="99")
    assert notify_event(cfg, "msg", urgent=True) == []
    assert calls == []
