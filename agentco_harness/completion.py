"""Chat-completion execution — the non-agentic half of the backend seam.

Every executor this runtime shipped with is a subprocess CLI: `claude`,
`codex`, and a `claude` pointed at a different base URL. Those are *agentic* —
they read files, run commands and edit code, and a bead that says "implement
the feature" needs one.

A chat-completions endpoint does none of that. It takes text and returns text.
That is a strictly smaller capability, and pretending otherwise is how a bead
gets routed to a model that cannot possibly do it and fails four minutes later
with something unhelpful. So a completion backend declares what it provides
(`text`) and does not declare what it does not (`tools`, `shell`, `files`), and
the dispatcher refuses the mismatch instead of discovering it.

What it IS good for is the large share of beads that are text in, text out:
triage, classification, summaries, drafts, extraction. Those move onto local
hardware or cheap tokens and stop competing for the same quota as the work that
genuinely needs an agent.

Transport is stdlib `urllib`, matching `hub_client` — this package's only
runtime dependencies are click, pyyaml and the ASOP contract, and a completion
backend is not a reason to add a fourth.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .executor import ExecResult

#: What a backend can do. A bead's `requires` is matched against these.
TEXT = "text"      # takes a prompt, returns prose
TOOLS = "tools"    # can call tools / functions
SHELL = "shell"    # can run commands on the executing machine
FILES = "files"    # can read and write the working tree

#: Everything a subprocess agent CLI provides.
AGENTIC = frozenset({TEXT, TOOLS, SHELL, FILES})
#: Everything a chat-completions endpoint provides. The point of the module.
COMPLETION_ONLY = frozenset({TEXT})

DEFAULT_TIMEOUT_S = 300
DEFAULT_MAX_TOKENS = 4096


@dataclass(frozen=True)
class Provider:
    """Where a completion backend sends its request, and as whom.

    `api_key_env` rather than a key: a config file that can hold a secret
    eventually does. Unset, the request goes out unauthenticated, which is
    exactly right for a local server and exactly wrong for anything else —
    so `requires_key` says which is which and the caller refuses loudly.
    """

    name: str
    base_url: str
    model: str
    api_key_env: Optional[str] = None
    requires_key: bool = False

    def key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env, "").strip() or None if self.api_key_env else None


#: Shipped defaults. LM Studio's is the address its server binds by default, so
#: `lmstudio` works with no config at all; z.ai must be configured because a
#: default endpoint that silently costs money is not a default.
DEFAULT_PROVIDERS: dict[str, Provider] = {
    "lmstudio": Provider(
        name="lmstudio",
        base_url="http://localhost:1234/v1",
        model="local-model",
        api_key_env=None,
        requires_key=False,
    ),
    "zai-api": Provider(
        name="zai-api",
        base_url="https://api.z.ai/api/paas/v4",
        model="glm-4.7",
        api_key_env="ZAI_API_KEY",
        requires_key=True,
    ),
}


class ProviderUnconfigured(Exception):
    """The provider needs a key it does not have. Refuse before spending."""


def complete(
    prompt: str,
    provider: Provider,
    *,
    model: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ExecResult:
    """One OpenAI-compatible chat completion. Never raises on a transport
    failure — every failure mode comes back as a loud ExecResult, the same
    contract the subprocess executors keep."""
    if provider.requires_key and not provider.key():
        raise ProviderUnconfigured(
            f"provider {provider.name!r} needs {provider.api_key_env} in the environment; "
            f"refusing to send an unauthenticated request to {provider.base_url}"
        )

    body = json.dumps({
        "model": model or provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()

    headers = {"content-type": "application/json"}
    key = provider.key()
    if key:
        headers["authorization"] = f"Bearer {key}"

    url = provider.base_url.rstrip("/") + "/chat/completions"
    started = time.monotonic()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace")[:400]
        return ExecResult(
            success=False, output="", exit_code=e.code,
            duration_seconds=time.monotonic() - started,
            error=f"{provider.name} returned HTTP {e.code}: {detail}",
        )
    except urllib.error.URLError as e:
        return ExecResult(
            success=False, output="", exit_code=None,
            duration_seconds=time.monotonic() - started,
            error=(
                f"{provider.name} unreachable at {provider.base_url} ({e.reason}). "
                f"For lmstudio, start the local server; for a remote provider, check the URL."
            ),
        )
    except TimeoutError:
        return ExecResult(
            success=False, output="", exit_code=None,
            duration_seconds=time.monotonic() - started,
            error=f"{provider.name} exceeded {timeout}s",
        )

    text = _first_message(payload)
    if text is None:
        return ExecResult(
            success=False, output="", exit_code=None,
            duration_seconds=time.monotonic() - started,
            error=f"{provider.name} returned no message: {json.dumps(payload)[:300]}",
        )
    return ExecResult(
        success=True, output=text, error=None, exit_code=0,
        duration_seconds=time.monotonic() - started,
    )


def _first_message(payload: dict) -> Optional[str]:
    """The assistant's text, or None. Tolerant of the shapes servers actually
    send: OpenAI's `choices[].message.content`, and the `text` some
    OpenAI-compatible servers return instead."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # Some servers put plain text on the choice itself.
    text = first.get("text")
    return text if isinstance(text, str) and text.strip() else None


def providers_from_config(config) -> dict[str, Provider]:
    """The shipped defaults, overridden by anything the operator declared.

    An operator names only what they change — a model, a port — and the rest
    of the provider stays as shipped.
    """
    declared = dict(getattr(config, "completion", None) or {})
    out: dict[str, Provider] = {}
    for name, base in DEFAULT_PROVIDERS.items():
        over = declared.get(name) or {}
        out[name] = Provider(
            name=name,
            base_url=str(over.get("base_url", base.base_url)),
            model=str(over.get("model", base.model)),
            api_key_env=over.get("api_key_env", base.api_key_env),
            requires_key=bool(over.get("requires_key", base.requires_key)),
        )
    for name, over in declared.items():
        if name in out:
            continue
        out[name] = Provider(
            name=name,
            base_url=str(over.get("base_url", "")),
            model=str(over.get("model", "")),
            api_key_env=over.get("api_key_env"),
            requires_key=bool(over.get("requires_key", bool(over.get("api_key_env")))),
        )
    return out
