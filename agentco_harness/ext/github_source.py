"""A GitHub source for `observe()` (ac-31d030ae).

Ported from v1's `agentco.sources.GitHubSource` — issues only, no dedupe, no
poll interval — with what this brief asked for added: `pull_requests` and
`releases` as their own event kinds, a poll interval so a fast heartbeat does
not re-fetch a slow-moving repo every cycle, and a per-kind seen-id cursor so
an item already yielded is not fetched-and-classified again forever.
`Classifier.process` already suppresses the duplicate BEAD via
`beads.exists_source` — that is a downstream safety net, not a reason to
re-fetch and re-run every open issue through an LLM call every cycle.

Registered through `register_source_factory`, never through the retired
`sources:` config block (`config.RETIRED_TOP_LEVEL_KEYS`) — this module is an
extension exactly in the sense `extensions.py`'s own docstring describes: it
names a vendor, so it does not belong in the runtime's own Config.

## Config

    extensions:
      - agentco_harness.ext.github_source
    extension_settings:
      github_source:
        repo: "owner/name"              # or `repos: ["owner/a", "owner/b"]`
        events: ["issues", "pull_requests", "releases"]   # default: all three
        poll_interval: "15m"             # default: unset — every cycle
        token_env: "GITHUB_TOKEN"        # default; read only as a fallback
        use_gh_cli: true                 # default

## Auth

`gh api` first, when the `gh` CLI is on PATH and `use_gh_cli` is not `false`
— it runs under the operator's own `gh auth login` session, so this module
never even sees a token. Falls back to a bare token from the environment
(`token_env`, default `GITHUB_TOKEN`) over `urllib`, as v1 did. Neither path
ever appears in a log line: every warning below names the repo and the
failure, never a header or a token value.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..config import Config
from ..orchestrator import register_source_factory
from ..recurring import parse_duration

API_BASE = "https://api.github.com"

#: event kind -> the query suffix against `repos/{repo}/...`.
_KIND_PATHS = {
    "issues": "issues?state=open&sort=created&direction=desc&per_page=30",
    "pull_requests": "pulls?state=open&sort=created&direction=desc&per_page=30",
    "releases": "releases?per_page=30",
}
DEFAULT_EVENTS = ("issues", "pull_requests", "releases")

#: Bound on the per-repo, per-kind seen-id cursor. Not a correctness limit —
#: source_id itself is what dedupes — just a cap so `.github_source/*.json`
#: cannot grow without bound on a repo that never stops producing issues.
MAX_SEEN_PER_KIND = 500


class GitHubApiError(RuntimeError):
    """A GitHub fetch (or building a client to make one) failed."""


@dataclass
class GitHubEvent:
    """What `Orchestrator.observe()` reads off anything a source yields."""

    source: str
    source_id: str
    content: str
    context: str


class GitHubClient:
    """One method: GET a REST path, parsed from JSON. Two implementations
    below; a test supplies a third with canned data and no network."""

    def get(self, path: str):  # pragma: no cover - interface
        raise NotImplementedError


class GhCliClient(GitHubClient):
    """`gh api <path>` — the operator's own `gh auth login` session decides
    what this can see; this class never touches a token directly."""

    def get(self, path: str):
        proc = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            raise GitHubApiError(
                f"gh api {path} failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise GitHubApiError(f"gh api {path} returned unparseable JSON: {e}") from e


class TokenClient(GitHubClient):
    """A bare token over `urllib`, exactly as v1's `GitHubSource` did."""

    def __init__(self, token: str):
        self._token = token

    def get(self, path: str):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{API_BASE}/{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentco-harness",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError) as e:
            raise GitHubApiError(f"GET {path} failed: {e}") from e
        except json.JSONDecodeError as e:
            raise GitHubApiError(f"GET {path} returned unparseable JSON: {e}") from e


def _render(repo: str, kind: str, item: dict) -> tuple[str, str, str]:
    """(source_id, content, context) for one raw API item."""
    if kind == "issues":
        number = item["number"]
        labels = ", ".join(l["name"] for l in item.get("labels", [])) or "none"
        return (
            f"{repo}#issue-{number}",
            f"{item['title']}\n\n{item.get('body') or ''}",
            f"GitHub issue #{number} in {repo} by {item['user']['login']}, labels: {labels}",
        )
    if kind == "pull_requests":
        number = item["number"]
        base = (item.get("base") or {}).get("ref", "?")
        return (
            f"{repo}#pr-{number}",
            f"{item['title']}\n\n{item.get('body') or ''}",
            f"GitHub pull request #{number} in {repo} by {item['user']['login']}, base {base}",
        )
    if kind == "releases":
        release_id = item["id"]
        name = item.get("name") or item.get("tag_name") or f"release-{release_id}"
        return (
            f"{repo}#release-{release_id}",
            f"{name}\n\n{item.get('body') or ''}",
            f"GitHub release {item.get('tag_name', '?')} in {repo}",
        )
    raise AssertionError(f"unreachable: unknown kind {kind!r}")  # callers pre-filter


class GitHubSource:
    """Polls one repo for one or more event kinds.

    `client` is always injected — production gets one of the two classes
    above from `_build_client`; a test hands in a stub with canned `.get()`
    answers, so this class itself never touches the network or a subprocess.
    """

    name = "github"

    def __init__(
        self,
        repo: str,
        *,
        client: GitHubClient,
        events: tuple[str, ...] = DEFAULT_EVENTS,
        poll_interval: Optional[timedelta] = None,
        state_path: Path,
    ):
        self.repo = repo
        self.client = client
        self.events = tuple(events)
        self.poll_interval = poll_interval
        self.state_path = state_path
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state))

    def poll(self) -> list[GitHubEvent]:
        now = datetime.now(timezone.utc)
        if self.poll_interval is not None:
            last = self._state.get("last_poll_at")
            if last and now - datetime.fromisoformat(last) < self.poll_interval:
                return []

        out: list[GitHubEvent] = []
        for kind in self.events:
            out.extend(self._poll_kind(kind))
        self._state["last_poll_at"] = now.isoformat()
        self._save_state()
        return out

    def _poll_kind(self, kind: str) -> list[GitHubEvent]:
        path_suffix = _KIND_PATHS[kind]  # the factory already validated `events`
        try:
            items = self.client.get(f"repos/{self.repo}/{path_suffix}")
        except GitHubApiError as e:
            print(f"[github_source] error polling {self.repo} ({kind}): {e}")
            return []
        if not isinstance(items, list):
            print(f"[github_source] WARNING: unexpected response polling {self.repo} ({kind}) — skipping")
            return []

        seen_key = f"seen_{kind}"
        seen = set(self._state.get(seen_key, []))
        newly_seen = list(seen)
        out: list[GitHubEvent] = []
        for item in items:
            if kind == "issues" and "pull_request" in item:
                continue  # surfaces through pull_requests instead
            source_id, content, context = _render(self.repo, kind, item)
            if source_id in seen:
                continue
            out.append(GitHubEvent(source=self.name, source_id=source_id, content=content, context=context))
            newly_seen.append(source_id)
        self._state[seen_key] = newly_seen[-MAX_SEEN_PER_KIND:]
        return out


# --------------------------------------------------------------- wiring config


def _repos(settings: dict) -> list[str]:
    if "repos" in settings:
        repos = settings["repos"]
        if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
            raise ValueError(
                "extension_settings.github_source.repos must be a list of 'owner/name' strings"
            )
        return list(repos)
    repo = settings.get("repo")
    return [repo] if repo else []


def _events(settings: dict) -> tuple[str, ...]:
    events = settings.get("events") or list(DEFAULT_EVENTS)
    unknown = [e for e in events if e not in _KIND_PATHS]
    if unknown:
        print(
            f"[github_source] WARNING: unknown event kind(s) {unknown} in "
            f"extension_settings.github_source.events — known kinds: "
            f"{', '.join(_KIND_PATHS)}"
        )
    return tuple(e for e in events if e in _KIND_PATHS)


def _poll_interval(settings: dict) -> Optional[timedelta]:
    raw = settings.get("poll_interval")
    if not raw:
        return None
    try:
        return parse_duration(str(raw))
    except ValueError as e:
        print(
            f"[github_source] WARNING: poll_interval={raw!r} is invalid ({e}) "
            f"— polling every cycle instead"
        )
        return None


def _build_client(settings: dict) -> GitHubClient:
    if settings.get("use_gh_cli", True) and shutil.which("gh"):
        return GhCliClient()
    token_env = settings.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env)
    if not token:
        raise GitHubApiError(
            f"no 'gh' CLI on PATH (or use_gh_cli: false) and {token_env} is unset in "
            f"the environment — unauthenticated polling is rate-limited to uselessness"
        )
    return TokenClient(token)


def _state_path(config: Config, repo: str) -> Path:
    return Path(config.store_dir) / ".github_source" / f"{repo.replace('/', '__')}.json"


def build_sources(config: Config) -> list[GitHubSource]:
    """`register_source_factory`'s factory — split out so a caller (or a
    test) can build the source list directly from a `Config` without going
    through the registry."""
    settings = (config.extension_settings or {}).get("github_source") or {}
    if not settings.get("enabled", True):
        return []

    repos = _repos(settings)
    if not repos:
        print(
            "[github_source] WARNING: no 'repo' or 'repos' set under "
            "extension_settings.github_source — nothing will be observed"
        )
        return []

    events = _events(settings)
    poll_interval = _poll_interval(settings)
    try:
        client = _build_client(settings)
    except GitHubApiError as e:
        print(f"[github_source] WARNING: {e} — this source will not be observed")
        return []

    return [
        GitHubSource(
            repo, client=client, events=events, poll_interval=poll_interval,
            state_path=_state_path(config, repo),
        )
        for repo in repos
    ]


register_source_factory(build_sources)
