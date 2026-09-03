"""Outbound notifications — best-effort, loud on failure, never fatal.

Two channels:

- **Pulse** (`notify.url`): the local Pulse server's /notify endpoint —
  voice + whatever Pulse routes onward. Reserved for urgent events
  (a stale child) so the operator isn't spoken to every hour.
- **Telegram** (`notify.telegram_chat_id`): direct Bot API call. The bot
  token is read from the environment (`notify.telegram_token_env`,
  default TELEGRAM_BOT_TOKEN) and never lives in config files. Used for
  both urgent events and routine cycle summaries.

A notification failure logs a WARNING and returns False — a broken
channel must never fail the cycle that tried to report on it.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .config import NotifyConfig


def send_pulse(message: str, url: str, timeout: float = 5.0) -> bool:
    """POST {"message"} to the Pulse notify endpoint."""
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        print(f"[notify] WARNING: pulse notify failed ({e})")
        return False


def send_telegram(message: str, chat_id: str, token: str, timeout: float = 10.0) -> bool:
    """Send a message via the Telegram Bot API."""
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        print(f"[notify] WARNING: telegram send failed ({e})")
        return False


def notify_event(cfg: NotifyConfig, message: str, urgent: bool = False) -> list[str]:
    """Fan a message out to the configured channels. Returns channels that
    accepted it.

    Routine messages (cycle summaries) go to Telegram only; urgent ones
    (stale child, failed verification) also hit Pulse so they're voiced.
    """
    if not cfg.enabled:
        return []

    delivered: list[str] = []

    if cfg.telegram_chat_id:
        token = os.environ.get(cfg.telegram_token_env, "")
        if not token:
            print(
                f"[notify] WARNING: telegram_chat_id is configured but "
                f"${cfg.telegram_token_env} is unset — telegram message dropped"
            )
        elif send_telegram(message, cfg.telegram_chat_id, token):
            delivered.append("telegram")

    if urgent and cfg.url:
        if send_pulse(message, cfg.url):
            delivered.append("pulse")

    return delivered
