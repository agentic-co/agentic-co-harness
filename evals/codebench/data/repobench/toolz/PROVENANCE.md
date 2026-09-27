# toolz — provenance

- Source: `https://github.com/pytoolz/toolz`
- Commit: `451af60dec590a6010e2babdbf391ea8f815122f`
- Licence: BSD-3-Clause
- Snapshot kept: toolz, tlz, pyproject.toml, LICENSE.txt (docs/CI/tox/bench/examples dropped — irrelevant to running the test suite locally)
- Test command: `python3 -m pytest -q --deselect toolz/tests/test_package.py::test_has_version` from `repo/`
- Baseline (verified 2026-09-26, this exact commit, this machine): 191 passed, 1 skipped (annotationlib, Python-version-gated), 1 deselected (test_has_version, packaging metadata only) in 0.17s
- Mutation-eligible source dirs: toolz (test-dir markers excluded even when nested inside one: tests, test)
