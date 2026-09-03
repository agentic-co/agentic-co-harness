"""PRIME cache: extractive generation, content-stamped staleness, prompt injection.

Every test builds its own throwaway git repo under tmp_path — nothing here
reads or writes a real node.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness import prime as prime_mod
from agentco_harness.cli import main


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "Plans").mkdir()
    (root / "README.md").write_text(
        "# Widgetco\n\n"
        "[![badge](x)](y)\n\n"
        "Widgetco turns widgets into revenue for Brazilian MEIs.\n"
    )
    (root / "CLAUDE.md").write_text("# Rules\n\nAlways use bun.\n")
    (root / "ISA.md").write_text("# ISA\n")
    (root / "Plans" / "Launch.md").write_text("# Launch\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "widgetco"\nversion = "0.1"\n\n'
        '[project.scripts]\nwidget = "widgetco.cli:main"\n\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n'
    )
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")

    _git(["init", "-q", "-b", "main"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "feat: first commit"], root)
    return root


# --- generation -------------------------------------------------------------


def test_prime_is_extractive_and_covers_every_required_section(tmp_path):
    root = _repo(tmp_path)
    written = prime_mod.write(root)
    text = written.read_text()

    assert written.name == "PRIME.md"
    # Purpose quoted VERBATIM from README (not the heading, not the badge).
    assert "Widgetco turns widgets into revenue for Brazilian MEIs." in text
    assert "quoted from `README.md`" in text
    # Key paths
    assert "`CLAUDE.md`" in text and "`ISA.md`" in text and "`Plans/Launch.md`" in text
    # Entry points from pyproject
    assert "`widget` → `widgetco.cli:main`" in text
    assert "uv run --extra dev pytest" in text
    # Tree, two levels
    assert "src/" in text and "  main.py" in text
    # Commits
    assert "feat: first commit" in text


def test_stamp_records_head_and_source_hashes(tmp_path):
    root = _repo(tmp_path)
    written = prime_mod.write(root)
    stamp = prime_mod.read_stamp(written)

    assert stamp.git_head and len(stamp.git_head) == 40
    assert set(stamp.sources) == {"README.md", "CLAUDE.md", "ISA.md", "pyproject.toml"}
    assert all(len(h) == 64 for h in stamp.sources.values())
    assert stamp.generated_at


def test_generation_outside_a_git_repo_still_works(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("# X\n\nA plain directory.\n")
    text = prime_mod.write(plain).read_text()
    assert "A plain directory." in text
    assert "_not a git repo_" in text
    assert prime_mod.read_stamp(plain / "PRIME.md").git_head is None


def test_node_in_a_subdir_primes_the_enclosing_repo(tmp_path):
    root = _repo(tmp_path)
    node = root / ".agentco"
    node.mkdir()
    text = prime_mod.write(node).read_text()
    # It describes the REPO, not the two files in .agentco/
    assert "Widgetco turns widgets into revenue" in text
    assert (node / "PRIME.md").is_file()


# --- staleness --------------------------------------------------------------


def test_fresh_right_after_generation(tmp_path):
    root = _repo(tmp_path)
    prime_mod.write(root)
    fresh, reasons = prime_mod.check(root)
    assert fresh, reasons


def test_stale_when_head_moves(tmp_path):
    root = _repo(tmp_path)
    prime_mod.write(root)
    (root / "src" / "other.py").write_text("x = 1\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "chore: second"], root)

    fresh, reasons = prime_mod.check(root)
    assert not fresh
    assert any("HEAD moved" in r for r in reasons)


def test_stale_when_a_source_doc_changes_without_a_commit(tmp_path):
    """The same-day-edit case a time window misses entirely."""
    root = _repo(tmp_path)
    prime_mod.write(root)
    (root / "CLAUDE.md").write_text("# Rules\n\nActually, always use uv.\n")

    fresh, reasons = prime_mod.check(root)
    assert not fresh
    assert any("source changed: CLAUDE.md" in r for r in reasons)


def test_stale_when_a_source_doc_appears_or_disappears(tmp_path):
    root = _repo(tmp_path)
    prime_mod.write(root)
    (root / "ISA.md").unlink()
    (root / "package.json").write_text("{}\n")

    fresh, reasons = prime_mod.check(root)
    assert not fresh
    assert any("source removed: ISA.md" in r for r in reasons)
    assert any("source added since generation: package.json" in r for r in reasons)


def test_check_on_a_missing_prime_is_stale_with_a_reason(tmp_path):
    root = _repo(tmp_path)
    fresh, reasons = prime_mod.check(root)
    assert not fresh
    assert "agentco prime" in reasons[0]


def test_hand_edited_prime_without_a_stamp_raises(tmp_path):
    root = _repo(tmp_path)
    (root / "PRIME.md").write_text("# PRIME\n\nsomeone wrote this by hand\n")
    with pytest.raises(prime_mod.PrimeError, match="no agentco-prime-stamp"):
        prime_mod.check(root)


# --- prompt injection -------------------------------------------------------


def test_injection_block_is_empty_without_a_cache(tmp_path):
    root = _repo(tmp_path)
    assert prime_mod.injection_block(root / "config.yaml") == ""
    assert prime_mod.injection_block(None) == ""


def test_injection_block_carries_prime_content(tmp_path):
    root = _repo(tmp_path)
    prime_mod.write(root)
    block = prime_mod.injection_block(root / "config.yaml")
    assert "Widgetco turns widgets into revenue" in block
    assert "pointers, not conclusions" in block


def test_injection_block_head_truncates_and_says_so(tmp_path):
    root = _repo(tmp_path)
    prime_mod.write(root)
    target = root / "PRIME.md"
    target.write_text(target.read_text() + ("\nfiller line\n" * 2000))

    block = prime_mod.injection_block(root / "config.yaml")
    assert len(block.encode()) < prime_mod.PRIME_INJECT_MAX_BYTES + 500
    assert "PRIME truncated at" in block
    # Head kept: the orientation survives, the filler tail is what was cut.
    assert "# PRIME — repo" in block


def test_executor_store_backed_prompt_includes_prime(tmp_path, monkeypatch):
    """The store-backed prompt is where a bead's context is assembled."""
    from agentco_harness import executor

    root = _repo(tmp_path)
    prime_mod.write(root)

    captured = {}

    def fake_run_proc(cmd, prompt, timeout, claude_bin, **kwargs):
        captured["prompt"] = prompt
        return executor.ExecResult(
            success=True, output="ok", error=None, exit_code=0, duration_seconds=0.1
        )

    monkeypatch.setattr(executor, "_run_proc", fake_run_proc)
    executor.run_store_backed_task("ac-123", config_path=str(root / "config.yaml"))

    assert "Widgetco turns widgets into revenue" in captured["prompt"]
    assert "You are executing AgentCo task ac-123" in captured["prompt"]


def test_executor_prompt_is_unchanged_when_no_prime_exists(tmp_path, monkeypatch):
    from agentco_harness import executor

    root = _repo(tmp_path)
    captured = {}

    def fake_run_proc(cmd, prompt, timeout, claude_bin, **kwargs):
        captured["prompt"] = prompt
        return executor.ExecResult(
            success=True, output="ok", error=None, exit_code=0, duration_seconds=0.1
        )

    monkeypatch.setattr(executor, "_run_proc", fake_run_proc)
    executor.run_store_backed_task("ac-123", config_path=str(root / "config.yaml"))

    assert captured["prompt"].startswith("You are executing AgentCo task ac-123")


# --- CLI + doctor -----------------------------------------------------------


def test_cli_prime_generates_then_checks(tmp_path, monkeypatch):
    runner = CliRunner()
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    result = runner.invoke(main, ["prime"])
    assert result.exit_code == 0, result.output
    assert (root / "PRIME.md").is_file()

    result = runner.invoke(main, ["prime", "--check"])
    assert result.exit_code == 0, result.output
    assert "fresh" in result.output


def test_cli_prime_check_exits_nonzero_when_stale(tmp_path, monkeypatch):
    runner = CliRunner()
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    runner.invoke(main, ["prime"])

    (root / "README.md").write_text("# Widgetco\n\nWe pivoted to pet food.\n")
    result = runner.invoke(main, ["prime", "--check"])
    assert result.exit_code == 1
    assert "stale" in result.output
    assert "source changed: README.md" in result.output


def test_doctor_warns_about_a_stale_prime(tmp_path, monkeypatch, capsys):
    from agentco_harness.doctor import run_doctor

    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    prime_mod.write(root)
    (root / "CLAUDE.md").write_text("# Rules\n\nchanged\n")

    run_doctor(str(root / "config.yaml"))
    captured = capsys.readouterr().out
    assert "DEGRADED (prime.freshness)" in captured
    assert "PRIME.md is stale" in captured
    assert "source changed: CLAUDE.md" in captured
