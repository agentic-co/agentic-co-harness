"""A minimal OpenAI-compatible chat-completions client with tool calling.

Stdlib `urllib` only (matches `agentco_harness/completion.py`'s transport
choice) — this is a bigger client than that module's (tools, multi-turn
message history, usage accounting) so it lives here rather than extending
that one, but the "no new HTTP dependency" convention carries over.

Works unchanged against:
  - z.ai's OpenAI-format coding endpoint (GLM-4.7), the smoke-test target
  - any local OpenAI-compatible server (LM Studio on :4242, later) — same
    request/response shape, just a different `base_url` and no API key.

z.ai specifics (per the run brief): base URL
`https://api.z.ai/api/coding/paas/v4`, key in `ZAI_API_KEY`, and z.ai wants a
`user` field on the request (abuse-monitoring identifier, not a person's
name) — set to a fixed non-identifying string, never the operator's email.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_TIMEOUT_S = 120


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    model: str
    api_key_env: Optional[str] = None
    # z.ai's coding endpoint expects a `user` field; harmless/ignored by
    # OpenAI-compatible servers that don't ask for one (LM Studio).
    user_tag: str = "codebench-eval"

    def api_key(self) -> Optional[str]:
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env, "").strip()
        return key or None


ZAI_CODING = Endpoint(
    base_url="https://api.z.ai/api/coding/paas/v4",
    model="glm-4.7",
    api_key_env="ZAI_API_KEY",
)


#: 2026-09-26 repobench smoke found this ceiling (then 2048) truncating a
#: whole-file `write_file` mid-JSON-argument — GLM's structured tool-calling
#: still closed the JSON cleanly, so the truncation didn't even surface as a
#: parse error, just a silently-shortened file. Raised well past what a
#: generous diff/small-file rewrite needs; `finish_reason` on every
#: `ChatResult` is the actual detector now — a caller doesn't have to guess
#: from output size, it can check `result.finish_reason == "length"` directly.
DEFAULT_MAX_TOKENS = 8192


@dataclass
class ChatResult:
    ok: bool
    message: dict = field(default_factory=dict)  # raw assistant message (role/content/tool_calls)
    usage: dict = field(default_factory=dict)  # prompt_tokens/completion_tokens/total_tokens
    finish_reason: Optional[str] = None  # "length" means max_tokens cut this response off
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


def chat(
    endpoint: Endpoint,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    *,
    temperature: float = 0.2,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> ChatResult:
    body: dict = {
        "model": endpoint.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "user": endpoint.user_tag,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    headers = {"content-type": "application/json"}
    key = endpoint.api_key()
    if endpoint.api_key_env and not key:
        return ChatResult(ok=False, error=(
            f"{endpoint.api_key_env} is not set — refusing to call {endpoint.base_url} "
            f"unauthenticated"
        ))
    if key:
        headers["authorization"] = f"Bearer {key}"

    url = endpoint.base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace")[:800]
        return ChatResult(ok=False, error=f"HTTP {e.code} from {endpoint.base_url}: {detail}")
    except urllib.error.URLError as e:
        return ChatResult(ok=False, error=f"{endpoint.base_url} unreachable: {e.reason}")
    except TimeoutError:
        return ChatResult(ok=False, error=f"{endpoint.base_url} exceeded {timeout}s")
    duration = time.monotonic() - started

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ChatResult(ok=False, error=f"no choices in response: {json.dumps(payload)[:400]}", raw=payload)
    first_choice = choices[0] or {}
    message = first_choice.get("message") or {}
    finish_reason = first_choice.get("finish_reason")
    usage = payload.get("usage") or {}
    usage = {**usage, "wall_time_s": duration, "finish_reason": finish_reason}
    return ChatResult(ok=True, message=message, usage=usage, finish_reason=finish_reason, raw=payload)


def endpoint_from_args(base_url: Optional[str], model: Optional[str], api_key_env: Optional[str]) -> Endpoint:
    """Build an Endpoint from CLI flags, defaulting to the z.ai coding
    endpoint when nothing is given (this build's smoke-test target)."""
    if not base_url and not model:
        return ZAI_CODING
    return Endpoint(
        base_url=base_url or ZAI_CODING.base_url,
        model=model or ZAI_CODING.model,
        api_key_env=api_key_env if api_key_env is not None else (
            "ZAI_API_KEY" if (base_url or ZAI_CODING.base_url) == ZAI_CODING.base_url else None
        ),
    )
