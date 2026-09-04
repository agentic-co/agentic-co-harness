"""The DSPy layer, reached only when it is actually installed.

`lm` is declared an optional extra, and the runtime's own documentation says
why it can be: the default executors are headless agent CLIs, which need no
in-process model. The metadata said optional and the import graph said
mandatory — `orchestrator` did `import dspy` at module scope and pulled in
`agents`, `optimize` and `triage`, each of which does the same. A base
install could not import the `harness` console script at all:

    >>> from agentco_harness.cli import main
    ModuleNotFoundError: No module named 'dspy'

An extra that every entry point requires is not an extra. This module is the
one place that knows whether the layer is present, so the cost of it being
absent is paid where the layer is USED, not where the package is imported.

The contract, in one line each:

* `available()` — is the layer importable at all.
* `require(feature)` — raise a `LmUnavailable` that names the missing extra
  and the feature that wanted it. Every LM entry point calls this first.
* `agents()` / `optimize()` / `triage()` / `dspy()` — the modules themselves,
  imported on demand.

Nothing here caches a negative result: an operator who installs the extra
into a live virtualenv gets the layer on the next call rather than on the
next restart.
"""

from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType

#: The extra that supplies the layer, named in every error this module raises
#: so the message carries its own remedy.
EXTRA = "lm"

INSTALL_HINT = 'uv pip install -e ".[lm]"'


class LmUnavailable(RuntimeError):
    """An LM-only path was reached in an install that has no LM layer.

    A `RuntimeError` rather than an `ImportError`: by the time this is raised
    the import has already succeeded on purpose, and the caller is a cycle
    deciding what to do with a bead, not a loader.
    """

    def __init__(self, feature: str):
        super().__init__(
            f"{feature} needs the optional '{EXTRA}' extra (DSPy), which is not "
            f"installed. Install it with: {INSTALL_HINT}\n"
            "The runtime does not need it to run headless agent CLIs — only "
            "the in-process planner, triage and classifier paths do."
        )
        self.feature = feature


def available() -> bool:
    """True when the DSPy layer can be imported.

    Asked, not remembered — see the module docstring.
    """
    return importlib.util.find_spec("dspy") is not None


def require(feature: str) -> None:
    """Raise `LmUnavailable` unless the layer is installed.

    `feature` is what the operator was trying to do, quoted back to them.
    """
    if not available():
        raise LmUnavailable(feature)


def _load(name: str, feature: str) -> ModuleType:
    require(feature)
    return importlib.import_module(name)


def dspy(feature: str = "This") -> ModuleType:
    return _load("dspy", feature)


def agents(feature: str = "Built-in LM agents") -> ModuleType:
    return _load("agentco_harness.agents", feature)


def optimize(feature: str = "Optimized signatures") -> ModuleType:
    return _load("agentco_harness.optimize", feature)


def triage(feature: str = "LM triage") -> ModuleType:
    return _load("agentco_harness.triage", feature)


def agent_names() -> frozenset[str]:
    """The built-in LM agent names, or an empty set with no layer installed.

    Used by the dispatch guards, which ask "is this name built in?" to decide
    whether an unknown assignee is a config-declared agent or a typo. With no
    layer there are no built-ins, and every such name falls through to the
    config check — which is the correct answer, not a degraded one.
    """
    if not available():
        return frozenset()
    return frozenset(importlib.import_module("agentco_harness.agents").AGENTS)
