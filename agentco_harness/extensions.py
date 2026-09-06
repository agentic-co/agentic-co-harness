"""Loading the extensions that fill the seams.

P0 pulled one operator's pipelines out of the cycle and replaced them with four
registries: cycle handlers, completion hooks, source factories, executor
backends. What it did not add was anything that *loads* an extension, so the
seams existed and nothing could ever reach them — a registration only happens
if some module calls it, and nothing imported a module the operator wrote.

This is that loader, and it is deliberately the dullest possible one: config
names modules, we import them, their import side effects register whatever they
register. No plugin manifest, no entry points, no discovery. An extension is a
module you wrote and named.

## Failure is fatal, on purpose

The tempting design is to warn and continue. It is wrong here, and the reason
is specific to what an extension does.

A cycle handler owns a task type end to end. When its module fails to import,
the type is simply unregistered — and an unregistered type is *not* skipped, it
takes the ordinary executor path. So a typo in a module name does not produce
"extension missing". It produces beads quietly running down a path their author
never intended, with an executor that has no idea what they are.

That is the exact silent-failure shape this codebase keeps paying for: a config
rewritten by a tool that dropped a declared agent, a queue full of work assigned
to a name nothing could dispatch, a source promised in config with no
implementation behind it. Every one was a thing that looked configured and did
nothing. So a declared extension that will not import stops the command.
"""

from __future__ import annotations

import importlib
from typing import Iterable

#: Modules already imported this process. Import is idempotent, but a second
#: `load()` would re-run any *registration* that is not, and a handler
#: registered twice is a handler that runs twice.
_LOADED: set[str] = set()


class ExtensionError(RuntimeError):
    """A declared extension could not be loaded. Never swallowed."""


def load(modules: Iterable[str]) -> list[str]:
    """Import each declared extension. Returns the ones loaded this call.

    Raises `ExtensionError` naming the module and the underlying cause. The
    message says what silently happens if it is ignored, because "module not
    found" alone does not tell an operator that their beads are about to take
    a path nobody wrote.
    """
    loaded: list[str] = []
    for name in modules:
        name = (name or "").strip()
        if not name or name in _LOADED:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — an extension may fail any way
            raise ExtensionError(
                f"extension {name!r} declared in config could not be imported: "
                f"{type(exc).__name__}: {exc}\n"
                f"Refusing to continue. An extension that does not load leaves its "
                f"task types UNREGISTERED, and an unregistered type is not skipped — "
                f"it takes the ordinary executor path. The beads would run, wrongly, "
                f"and nothing would say so. Fix the module or remove it from "
                f"`extensions:` in config."
            ) from exc
        _LOADED.add(name)
        loaded.append(name)
    return loaded


def loaded() -> frozenset[str]:
    """What has been loaded in this process. Read by `doctor`."""
    return frozenset(_LOADED)


def _reset_for_tests() -> None:
    """Forget what was loaded. Tests only — a real process loads once."""
    _LOADED.clear()
