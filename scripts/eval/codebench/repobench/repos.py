"""The 3 source repos this tier mutates. Pinned by commit, not by branch —
a re-fetch is reproducible. All three: pure Python, root-level package (no
`src/` layout, no install step — `sys.executable -m pytest` from the repo
root works with nothing but the interpreter this harness already runs on),
green baseline verified 2026-09-26 (counts below), permissive licence.

Considered and DROPPED: python-dateutil (src/ layout needs an install step;
its zoneinfo-dependent tests additionally need a local tzdata cache this
build didn't set up — not a code problem, an environment one, but adding a
4th moving part for a build that already has three working repos wasn't
worth it) and attrs (also src/ layout, needs a wheel build backend). Neither
was excluded because of anything about their SOURCE — flagged so a future
build doesn't have to rediscover this.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoSpec:
    name: str
    github: str  # "owner/repo"
    commit: str  # pinned full SHA
    license: str
    # Paths (relative to repo root) to keep in the committed snapshot —
    # everything else (docs/, CI config, tox.ini, bench/, examples/) is
    # dropped as irrelevant to running the test suite.
    keep_paths: list[str]
    # Directories (relative to repo root) whose .py files are mutation
    # candidates. Deliberately narrower than "everything in keep_paths" —
    # e.g. toolz's tests live INSIDE its package dir, so "toolz" as a
    # keep_path does not mean "toolz" is mutation-safe; source_dirs says
    # what actually is.
    source_dirs: list[str]
    # Any path component that marks a file as a TEST file even if it's
    # nested inside a source_dir (toolz's "toolz/tests/").
    test_dir_markers: list[str] = field(default_factory=lambda: ["tests", "test"])
    # Test invocation, run with cwd = repo root, `sys.executable` prepended.
    # No local module needs installing — see the module docstring.
    pytest_args: list[str] = field(default_factory=lambda: ["-q"])
    # Baseline (green) counts at `commit`, from an actual run — recorded so
    # a later re-run can tell "the mutant changed this" from "the baseline
    # already looked like this."
    baseline_summary: str = ""


REPOS: list[RepoSpec] = [
    RepoSpec(
        name="more-itertools",
        github="more-itertools/more-itertools",
        commit="fbb9a98d8c7b914fcc952afd4976c1bc8deebe87",
        license="MIT",
        keep_paths=["more_itertools", "tests", "setup.cfg", "pyproject.toml", "LICENSE"],
        source_dirs=["more_itertools"],
        baseline_summary="766 passed, 21202 subtests passed in 9.09s",
    ),
    RepoSpec(
        name="toolz",
        github="pytoolz/toolz",
        commit="451af60dec590a6010e2babdbf391ea8f815122f",
        license="BSD-3-Clause",
        # tlz is a tiny compat shim (mirrors the toolz API) — kept so
        # tests/test_tlz.py has something to import; not a source_dir
        # (nothing there is ours to mutate, it's a generated re-export).
        keep_paths=["toolz", "tlz", "pyproject.toml", "LICENSE.txt"],
        source_dirs=["toolz"],
        # test_has_version needs the package pip-installed for
        # importlib.metadata to find it — a packaging-metadata check, not a
        # behavior our mutations touch. Quarantined, not silently dropped.
        pytest_args=["-q", "--deselect", "toolz/tests/test_package.py::test_has_version"],
        baseline_summary=(
            "191 passed, 1 skipped (annotationlib, Python-version-gated), "
            "1 deselected (test_has_version, packaging metadata only) in 0.17s"
        ),
    ),
    RepoSpec(
        name="boltons",
        github="mahmoud/boltons",
        commit="4e5faa3d7e4008d89e0d8bf1ea87b6d9a061a16d",
        license="BSD-3-Clause",
        keep_paths=["boltons", "tests", "setup.cfg", "pyproject.toml", "LICENSE"],
        source_dirs=["boltons"],
        baseline_summary="525 passed, 12 subtests passed in 3.56s",
    ),
]


def by_name(name: str) -> RepoSpec:
    for r in REPOS:
        if r.name == name:
            return r
    raise KeyError(name)
