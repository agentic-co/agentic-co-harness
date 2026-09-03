"""Shared test fixtures and helpers."""

from __future__ import annotations

import pytest
from dspy.utils.dummies import DummyLM

from agentco_harness import usage


@pytest.fixture(autouse=True)
def _no_real_env_file(monkeypatch):
    """Keep `Config.load` from reading the developer's real `~/.claude/.env`.

    `Config.load` merges an env file into `os.environ`, which would otherwise
    pull live provider keys into every test that loads a config — making the
    suite machine-dependent and violating the ISA's "no API keys in CI". The
    empty override disables the load; a test that wants the behavior sets
    AGENTCO_ENV_FILE to its own tmp_path file.
    """
    monkeypatch.setenv("AGENTCO_ENV_FILE", "")


@pytest.fixture(autouse=True)
def _no_real_transkriptor_root(monkeypatch, tmp_path_factory):
    """Keep the transkriptor ledger checks off the developer's real ~/Feeds.

    `doctor` (and anything else calling into `transkriptor`) reads the ledger
    at TRANSKRIPTOR_ROOT; unset, that is the live ~/Feeds/transkriptor on this
    machine, which would make host state decide the suite's colour. Tests that
    want a ledger set the var themselves (the `tk_root` fixture does).
    """
    monkeypatch.setenv("TRANSKRIPTOR_ROOT", str(tmp_path_factory.mktemp("no-transkriptor")))


@pytest.fixture(autouse=True)
def _default_test_attribution(tmp_path_factory):
    """Give the whole suite a throwaway usage attribution.

    The executor refuses to invoke a model it cannot attribute, which is the
    point of the meter — but that makes every direct executor test a call site
    that must declare one, and a per-test declaration would be scaffolding
    repeated ~40 times. Declaring it once here keeps the enforcement honest
    (production code has NO default; only tests get one) while pointing the
    ledger at a tmp dir so no test can write into the repo's real store.

    A test that wants its own ledger or its own bead id opens its own
    `usage.attributed(...)` — the innermost block wins.
    """
    root = tmp_path_factory.mktemp("usage-ledger")
    with usage.attributed(
        bead_id="ac-testtest",
        lane="test",
        tasks_path=str(root / "tasks.jsonl"),
    ):
        yield


def pm_answer(
    assigned_to: str = "dev",
    acceptance=None,
    dependencies=None,
    needs_decomposition: bool = False,
    subtasks=None,
) -> dict:
    """A single PMPrioritize answer dict for DummyLM.

    Includes the ``reasoning`` field that ChainOfThought prepends — DummyLM
    formats whatever keys it is given, and the ChatAdapter parser needs the
    reasoning header present to parse the rest cleanly.
    """
    return {
        "reasoning": "Considered impact and effort before deciding.",
        "priority_rationale": "High customer value, low effort.",
        "acceptance_criteria": acceptance or ["Login works", "Errors surfaced"],
        "estimated_effort": "small",
        "dependencies": dependencies or [],
        "assigned_to": assigned_to,
        "needs_decomposition": needs_decomposition,
        "subtasks": subtasks or [],
    }


def make_pm_lm(n: int = 4, **kwargs) -> DummyLM:
    """Build a DummyLM that returns the same PM answer for up to n calls."""
    return DummyLM([pm_answer(**kwargs) for _ in range(n)], reasoning=True)
