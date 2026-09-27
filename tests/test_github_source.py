"""ac-31d030ae: a GitHub source, registered through `register_source_factory`
(never the retired `sources:` block). No test here touches the network or a
subprocess — every client is a stub with canned answers.
"""

from __future__ import annotations

import json

import pytest
import yaml

from agentco_harness.config import Config
from agentco_harness.ext import github_source as gs


class _StubClient(gs.GitHubClient):
    """Canned answers keyed by the exact path `GitHubSource` asks for."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[str] = []

    def get(self, path: str):
        self.calls.append(path)
        if path not in self.answers:
            raise gs.GitHubApiError(f"no stubbed answer for {path}")
        return self.answers[path]


ISSUE = {
    "number": 7, "title": "Widgets break on Tuesdays", "body": "reliably",
    "user": {"login": "alex"}, "labels": [{"name": "bug"}],
}
PR = {
    "number": 3, "title": "Fix the widget", "body": "see #7",
    "user": {"login": "acme"}, "base": {"ref": "main"},
}
RELEASE = {"id": 99, "tag_name": "v1.2.0", "name": "v1.2.0", "body": "notes"}


def _source(repo="alex/acme", events=gs.DEFAULT_EVENTS, poll_interval=None, answers=None, state_path=None):
    client = _StubClient(answers or {})
    return gs.GitHubSource(
        repo, client=client, events=events, poll_interval=poll_interval,
        state_path=state_path,
    ), client


def _answers_for(repo, issues=None, pulls=None, releases=None):
    return {
        f"repos/{repo}/{gs._KIND_PATHS['issues']}": issues if issues is not None else [],
        f"repos/{repo}/{gs._KIND_PATHS['pull_requests']}": pulls if pulls is not None else [],
        f"repos/{repo}/{gs._KIND_PATHS['releases']}": releases if releases is not None else [],
    }


# ------------------------------------------------------------------ rendering

def test_issue_is_rendered_with_labels_and_author(tmp_path):
    repo = "alex/acme"
    src, _ = _source(repo=repo, answers=_answers_for(repo, issues=[ISSUE]),
                      state_path=tmp_path / "s.json")
    events = src.poll()
    issue_events = [e for e in events if e.source_id.endswith("#issue-7") or "issue-7" in e.source_id]
    assert len(issue_events) == 1
    e = issue_events[0]
    assert e.source_id == f"{repo}#issue-7"
    assert "Widgets break on Tuesdays" in e.content
    assert "alex" in e.context and "bug" in e.context


def test_issues_endpoint_excludes_pull_requests(tmp_path):
    """The issues API returns PRs too (they carry a `pull_request` key);
    those are skipped here since `pull_requests` is its own event kind."""
    repo = "alex/acme"
    pr_shaped_issue = dict(ISSUE, number=8, pull_request={"url": "..."})
    src, client = _source(
        repo=repo, events=("issues",),
        answers={f"repos/{repo}/{gs._KIND_PATHS['issues']}": [ISSUE, pr_shaped_issue]},
        state_path=tmp_path / "s.json",
    )
    events = src.poll()
    assert [e.source_id for e in events] == [f"{repo}#issue-7"]


def test_pull_request_is_rendered(tmp_path):
    repo = "alex/acme"
    src, _ = _source(repo=repo, events=("pull_requests",),
                      answers=_answers_for(repo, pulls=[PR]), state_path=tmp_path / "s.json")
    events = src.poll()
    assert len(events) == 1
    assert events[0].source_id == f"{repo}#pr-3"
    assert "base main" in events[0].context or "main" in events[0].context


def test_release_is_rendered(tmp_path):
    repo = "alex/acme"
    src, _ = _source(repo=repo, events=("releases",),
                      answers=_answers_for(repo, releases=[RELEASE]), state_path=tmp_path / "s.json")
    events = src.poll()
    assert len(events) == 1
    assert events[0].source_id == f"{repo}#release-99"
    assert "v1.2.0" in events[0].content


# ---------------------------------------------------------------------- dedupe

def test_an_item_already_seen_is_not_yielded_again(tmp_path):
    repo = "alex/acme"
    state_path = tmp_path / "s.json"
    answers = _answers_for(repo, issues=[ISSUE])
    src, _ = _source(repo=repo, events=("issues",), answers=answers, state_path=state_path)

    first = src.poll()
    assert len(first) == 1

    # A fresh instance re-reading the same state file (as a real cycle would).
    src2, _ = _source(repo=repo, events=("issues",), answers=answers, state_path=state_path)
    second = src2.poll()
    assert second == []


def test_a_new_item_alongside_a_seen_one_still_yields_the_new_one(tmp_path):
    repo = "alex/acme"
    state_path = tmp_path / "s.json"
    src, _ = _source(repo=repo, events=("issues",), answers=_answers_for(repo, issues=[ISSUE]),
                      state_path=state_path)
    assert len(src.poll()) == 1

    new_issue = dict(ISSUE, number=9, title="A second widget bug")
    src2, _ = _source(repo=repo, events=("issues",),
                       answers=_answers_for(repo, issues=[ISSUE, new_issue]), state_path=state_path)
    second = src2.poll()
    assert [e.source_id for e in second] == [f"{repo}#issue-9"]


# ----------------------------------------------------------------- poll interval

def test_poll_interval_skips_a_too_soon_poll(tmp_path):
    from datetime import timedelta
    repo = "alex/acme"
    state_path = tmp_path / "s.json"
    answers = _answers_for(repo, issues=[ISSUE])
    src, _ = _source(repo=repo, events=("issues",), poll_interval=timedelta(hours=1),
                      answers=answers, state_path=state_path)
    assert len(src.poll()) == 1  # first poll always runs

    src2, client2 = _source(repo=repo, events=("issues",), poll_interval=timedelta(hours=1),
                             answers=answers, state_path=state_path)
    assert src2.poll() == []
    assert client2.calls == []  # skipped before any fetch — no wasted API call


# -------------------------------------------------------------------- errors

def test_an_error_on_one_kind_does_not_blank_the_others(tmp_path):
    repo = "alex/acme"
    answers = {
        f"repos/{repo}/{gs._KIND_PATHS['pull_requests']}": [PR],
        f"repos/{repo}/{gs._KIND_PATHS['releases']}": [],
        # issues path deliberately NOT stubbed -> GitHubApiError inside _poll_kind
    }
    src, _ = _source(repo=repo, answers=answers, state_path=tmp_path / "s.json")
    events = src.poll()
    assert [e.source_id for e in events] == [f"{repo}#pr-3"]


def test_a_non_list_response_is_a_warning_not_a_crash(tmp_path, capsys):
    repo = "alex/acme"
    answers = _answers_for(repo)
    answers[f"repos/{repo}/{gs._KIND_PATHS['issues']}"] = {"message": "not found"}
    src, _ = _source(repo=repo, answers=answers, state_path=tmp_path / "s.json")
    events = src.poll()  # must not raise
    assert events == []
    assert "unexpected response" in capsys.readouterr().out


# ------------------------------------------------------------------- config

def test_repos_accepts_singular_or_plural():
    assert gs._repos({"repo": "alex/acme"}) == ["alex/acme"]
    assert gs._repos({"repos": ["alex/acme", "alex/other"]}) == ["alex/acme", "alex/other"]
    assert gs._repos({}) == []


def test_repos_rejects_a_malformed_list():
    with pytest.raises(ValueError, match="list of 'owner/name'"):
        gs._repos({"repos": "not-a-list"})


def test_unknown_event_kind_is_dropped_with_a_warning(capsys):
    events = gs._events({"events": ["issues", "not-a-kind"]})
    assert events == ("issues",)
    assert "unknown event kind" in capsys.readouterr().out


def test_poll_interval_parses_and_warns_on_garbage(capsys):
    from datetime import timedelta
    assert gs._poll_interval({"poll_interval": "15m"}) == timedelta(minutes=15)
    assert gs._poll_interval({}) is None
    assert gs._poll_interval({"poll_interval": "not-a-duration"}) is None
    assert "invalid" in capsys.readouterr().out


def test_build_sources_warns_and_returns_nothing_with_no_repo(capsys):
    config = Config()
    assert gs.build_sources(config) == []
    assert "no 'repo' or 'repos'" in capsys.readouterr().out


def test_build_sources_disabled_flag(tmp_path):
    config = Config()
    config.extension_settings = {"github_source": {"repo": "alex/acme", "enabled": False}}
    assert gs.build_sources(config) == []


def test_build_sources_wires_a_real_source(tmp_path, monkeypatch):
    monkeypatch.setattr(gs.shutil, "which", lambda name: None)  # no gh on PATH
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-token")
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.extension_settings = {"github_source": {"repo": "alex/acme"}}
    sources = gs.build_sources(config)
    assert len(sources) == 1
    assert isinstance(sources[0].client, gs.TokenClient)
    assert sources[0].repo == "alex/acme"


def test_build_sources_without_auth_warns_and_yields_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gs.shutil, "which", lambda name: None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.extension_settings = {"github_source": {"repo": "alex/acme"}}
    assert gs.build_sources(config) == []
    assert "GITHUB_TOKEN is unset" in capsys.readouterr().out


def test_build_sources_prefers_gh_cli_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(gs.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.extension_settings = {"github_source": {"repo": "alex/acme"}}
    sources = gs.build_sources(config)
    assert isinstance(sources[0].client, gs.GhCliClient)


def test_the_old_sources_block_warning_does_not_fire_for_the_new_section(tmp_path, capsys):
    """extension_settings never touches RETIRED_TOP_LEVEL_KEYS — only a
    top-level `sources:` key does. A config migrated to the new section
    carries no such key, so the warning this brief asked to silence never
    had anything to silence in the first place; this pins that it stays
    that way."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "tasks_path": "t.jsonl",
        "extensions": ["agentco_harness.ext.github_source"],
        "extension_settings": {"github_source": {"repo": "alex/acme"}},
    }))
    Config.load(str(cfg_path))
    out = capsys.readouterr().err
    assert "sources" not in out


# --------------------------------------------------------- registration seam

def test_registers_through_the_source_factory_seam():
    from agentco_harness.orchestrator import SOURCE_FACTORIES

    assert gs.build_sources in SOURCE_FACTORIES


# -------------------------------------------------------------- token safety

def test_a_token_fetch_failure_never_logs_the_token(monkeypatch, capsys):
    import urllib.error
    import urllib.request

    token = "ghp_totallysecretvalue"
    client = gs.TokenClient(token)

    def boom(*a, **kw):
        raise urllib.error.URLError("network is down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(gs.GitHubApiError) as e:
        client.get("repos/alex/acme/issues")
    assert token not in str(e.value)
    assert token not in capsys.readouterr().out
