"""The `lm` extra is optional in fact, not only in the metadata.

`pyproject.toml` has always declared DSPy an optional extra, and the README
has always said why it can be: the default executors are headless agent CLIs
that need no in-process model. The import graph disagreed. `orchestrator`
did `import dspy` at module scope and pulled in `agents`, `optimize` and
`triage`, each of which does the same, so a base install could not import
the `harness` console script at all:

    ModuleNotFoundError: No module named 'dspy'

Nothing noticed, because every environment that ran the suite had the extra
installed. These tests are the thing that notices. They do not assert that
DSPy is absent — it is present here — they assert the SHAPE that lets it be
absent: no module-scope dependency on the layer, and a named, actionable
failure at the point of use.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agentco_harness import _lm

PKG = pathlib.Path(__file__).resolve().parent.parent / "agentco_harness"

#: The modules that ARE the LM layer. They may import dspy at module scope;
#: they exist only when the extra does. Everything else may not.
LM_LAYER = {"agents.py", "optimize.py", "signatures.py", "triage.py"}


def _module_scope_imports(path: pathlib.Path) -> set[str]:
    """Top-level import names in a file — not the ones inside functions.

    A deferred `import dspy` inside a method is the fix, so walking every
    Import node in the tree would flag the cure as the disease. Only the
    module body counts.
    """
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # `level` is the number of leading dots. A relative import of a
            # sibling — `from .agents import AGENTS` — is exactly how the
            # regression got in, so it counts the same as the absolute form.
            # The first version of this helper filtered `level == 0` and would
            # have passed the pre-fix orchestrator (Bellows review, 2026-09-04).
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize(
    "path", sorted(p for p in PKG.glob("*.py") if p.name not in LM_LAYER)
)
def test_no_module_scope_dspy_outside_the_lm_layer(path):
    """The regression itself: one of these imports dspy and the CLI dies."""
    assert "dspy" not in _module_scope_imports(path), (
        f"{path.name} imports dspy at module scope, which makes the optional "
        f"'lm' extra mandatory for anyone importing it. Reach the layer "
        f"through agentco_harness._lm instead."
    )


@pytest.mark.parametrize(
    "path", sorted(p for p in PKG.glob("*.py") if p.name not in LM_LAYER | {"_lm.py"})
)
def test_no_module_scope_import_of_the_lm_layer(path):
    """Importing `agents`/`triage`/`optimize` imports dspy transitively.

    Same defect one hop out, and the hop is how it got in: orchestrator did
    not import dspy for the classifier, it imported `.agents`.
    """
    imported = _module_scope_imports(path)
    layer = {name.removesuffix(".py") for name in LM_LAYER}
    assert not (imported & layer), (
        f"{path.name} imports {sorted(imported & layer)} at module scope. "
        f"Those modules require DSPy; reach them through _lm."
    )


def test_the_entry_points_import_with_no_layer(monkeypatch):
    """`available()` False must not break importing the runtime."""
    monkeypatch.setattr(_lm, "available", lambda: False)
    import importlib

    import agentco_harness.cli as cli
    import agentco_harness.orchestrator as orch

    importlib.reload(orch)
    assert cli.main is not None


def test_a_missing_layer_names_the_extra_and_the_feature(monkeypatch):
    """The failure an operator actually sees carries its own remedy."""
    monkeypatch.setattr(_lm, "available", lambda: False)
    with pytest.raises(_lm.LmUnavailable) as caught:
        _lm.triage("LM triage")
    message = str(caught.value)
    assert "LM triage" in message           # what they were doing
    assert "'lm'" in message                # the extra that supplies it
    assert "uv pip install" in message      # how to get it
    assert caught.value.feature == "LM triage"


def test_agent_names_is_empty_without_the_layer(monkeypatch):
    """The dispatch guards ask "is this name built in?".

    With no layer there are no built-ins, so every assignee falls through to
    the config check. That is the correct answer — an unknown name is still
    reported, it is simply never mistaken for a built-in that cannot exist.
    """
    monkeypatch.setattr(_lm, "available", lambda: False)
    assert _lm.agent_names() == frozenset()


def test_agent_names_is_populated_with_the_layer():
    """And the ordinary install still sees the built-ins."""
    assert {"dev", "pm", "cs"} <= _lm.agent_names()


def test_availability_is_asked_not_remembered(monkeypatch):
    """Installing the extra into a live venv must not need a restart."""
    monkeypatch.setattr(_lm, "available", lambda: False)
    assert _lm.agent_names() == frozenset()
    monkeypatch.undo()
    assert _lm.agent_names()
