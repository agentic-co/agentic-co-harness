"""RepoSandbox: multi-file tools, and the integrity safeguard the single-file
tier didn't need — test files are restored to pristine before grading, so
tampering with (or deleting) the failing assertion during the episode can't
fake a pass. Builds its own tiny two-file "repo" (no real network/checkout)
so this runs fast and offline."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.repo_sandbox import RepoSandbox, network_isolation_mode  # noqa: E402
from codebench.repobench.repos import RepoSpec  # noqa: E402

GOOD_SRC = "def add(a, b):\n    return a + b\n"
BUGGY_SRC = "def add(a, b):\n    return a - b\n"
TEST_SRC = "from pkg.mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _build_toy_repo(tmp_path: Path) -> RepoSpec:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text(GOOD_SRC)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(TEST_SRC)
    return RepoSpec(
        name="toy", github="nobody/toy", commit="0" * 40, license="MIT",
        keep_paths=["pkg", "tests"], source_dirs=["pkg"],
    )


def test_localisation_false_until_agent_edits_the_buggy_file(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        assert sb.localisation() is False
        sb.write_file("pkg/mod.py", GOOD_SRC)
        assert sb.localisation() is True


def test_hidden_grading_fails_before_fix_passes_after(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        result, failing = sb.run_hidden_test()
        assert not result.ok
        assert failing == ["tests/test_mod.py::test_add"]
        sb.write_file("pkg/mod.py", GOOD_SRC)
        result2, failing2 = sb.run_hidden_test()
        assert result2.ok
        assert failing2 == []


def test_tampering_with_test_file_is_ignored_at_grading_time(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        # agent neuters the test instead of fixing the bug
        sb.write_file("tests/test_mod.py", "def test_add():\n    pass\n")
        during_episode = sb.run_tests(None)
        assert during_episode.ok  # the agent's OWN view: looks green now

        result, failing = sb.run_hidden_test()
        assert not result.ok  # grading restores the real test — still fails
        assert failing == ["tests/test_mod.py::test_add"]


def test_list_dir_and_path_escape_refused(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        listing = sb.list_dir(".").output
        assert "pkg/" in listing and "tests/" in listing
        assert not sb.read_file("/etc/passwd").ok
        assert not sb.write_file("../outside.py", "x=1").ok


def test_run_tests_with_explicit_path_runs_only_that_file(tmp_path):
    # repo-tier tests run through pytest (matching the repo's own test
    # command), so a repro file needs a real `def test_...():` — a bare
    # top-level `assert` is a collection-time statement, not a test item,
    # and pytest reports "no tests collected" (nonzero exit) for it. This is
    # the one behavioral difference from the single-file tier's sandbox,
    # which runs plain `python3 <path>` instead — worth the prompt in
    # repo_arms.py saying so explicitly.
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        sb.write_file(
            "repro_test.py",
            "from pkg.mod import add\n\ndef test_repro():\n    assert add(2, 3) == 5\n",
        )
        r = sb.run_tests("repro_test.py")
        assert not r.ok  # correctly demonstrates the (still-present) bug: FAILS now


def test_network_isolation_blocks_a_raw_socket(tmp_path):
    # Proxy-env poisoning alone (the mechanism this build shipped with
    # first) only stops HTTP-client libraries that check http_proxy/
    # https_proxy — a raw socket.connect() sails right past it. Verified
    # gap, closed 2026-09-26 with a `sandbox-exec` profile
    # (`(allow default)(deny network-outbound)(deny network-bind)` — the
    # `(deny default)` form was tried first and refused to exec even
    # /bin/echo under SIP on this machine). This test is the proof the gap
    # stays closed, not a description of it.
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        if network_isolation_mode() != "sandbox-exec":
            import pytest
            pytest.skip("sandbox-exec unavailable on this host — degraded to proxy-env-only")
        r = sb.run_python(
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(3)\n"
            "try:\n"
            "    s.connect(('8.8.8.8', 443))\n"  # leakguard: allow — a real public host on purpose: an unsandboxed connect to it succeeds, which is what makes the "denied" assert prove the block (TEST-NET-1 would time out either way)
            "    print('CONNECTED')\n"
            "except Exception as e:\n"
            "    print('denied:', type(e).__name__)\n"
        )
        assert r.ok  # the wrapper script itself still runs fine
        assert "denied" in r.output
        assert "CONNECTED" not in r.output


def test_sandboxed_pytest_run_gives_the_same_result_as_unwrapped(tmp_path):
    # The isolation wrapper must not change what a normal (network-free)
    # test run reports — only block network.
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", GOOD_SRC) as sb:
        result, failing = sb.run_hidden_test()
        assert result.ok
        assert failing == []


def test_edit_file_replaces_unique_exact_match(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        r = sb.edit_file("pkg/mod.py", "return a - b", "return a + b")
        assert r.ok
        assert sb.read_file("pkg/mod.py").output == GOOD_SRC


def test_edit_file_refuses_when_old_not_found(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        r = sb.edit_file("pkg/mod.py", "this text is not in the file", "whatever")
        assert not r.ok
        assert "not found" in r.output


def test_edit_file_refuses_when_old_is_not_unique(tmp_path):
    spec = _build_toy_repo(tmp_path)
    dup_src = "def add(a, b):\n    x = 1\n    x = 1\n    return a + b + x - x\n"
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", dup_src) as sb:
        r = sb.edit_file("pkg/mod.py", "x = 1", "x = 2")
        assert not r.ok
        assert "unique" in r.output


def test_edit_file_refuses_on_nonexistent_file(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        r = sb.edit_file("pkg/nope.py", "x", "y")
        assert not r.ok
        assert "use write_file" in r.output


def test_write_file_refuses_on_a_long_existing_file_and_points_to_edit_file(tmp_path):
    spec = _build_toy_repo(tmp_path)
    long_content = "\n".join(f"x = {i}" for i in range(300)) + "\n"
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        sb.write_file("pkg/big.py", long_content)  # creating a NEW file is fine, any size (< byte cap)
        assert sb.read_file("pkg/big.py").ok
        r = sb.write_file("pkg/big.py", "replacement content\n")  # now it's an EXISTING long file
        assert not r.ok
        assert "edit_file" in r.output


def test_damaged_files_empty_when_nothing_shrank(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        assert sb.damaged_files() == []
        sb.edit_file("pkg/mod.py", "return a - b", "return a + b")
        assert sb.damaged_files() == []  # a same-size edit is not damage


def test_damaged_files_detects_a_shrunk_file(tmp_path):
    spec = _build_toy_repo(tmp_path)
    # a bigger "buggy start" so a truncation reads as a clear >20% shrink
    padded_buggy = BUGGY_SRC + ("# padding line\n" * 20)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", padded_buggy) as sb:
        assert sb.damaged_files() == []
        sb.write_file("pkg/mod.py", "def add(a, b):\n    return a + b\n")  # much shorter
        assert sb.damaged_files() == ["pkg/mod.py"]


def test_damaged_files_detects_a_deleted_file(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        (sb.workdir / "pkg" / "mod.py").unlink()
        assert sb.damaged_files() == ["pkg/mod.py"]


def test_write_file_still_allows_replacing_a_short_existing_file(tmp_path):
    spec = _build_toy_repo(tmp_path)
    with RepoSandbox(tmp_path, spec, "pkg/mod.py", BUGGY_SRC) as sb:
        r = sb.write_file("pkg/mod.py", GOOD_SRC)  # short file, full replace is fine
        assert r.ok
        assert sb.read_file("pkg/mod.py").output == GOOD_SRC
