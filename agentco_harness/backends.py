"""Executor backends — the fourth extension seam, and the one ASOP bindings
resolve through.

v1 chose a subprocess by name in a chain of `if task.assigned_agent == ...`
branches: `claude`, `zai`, `forge`, `planner`. Each branch knew its egress
route and its runner, and adding a fifth meant editing the orchestrator,
the doctor's dispatchability guard, and the egress route table in
lockstep — the three places a comment already asked to be kept in
lockstep, which is what a registry is for.

A **backend** is what a role binding names (ASOP.md §3.6, §7): when a run
binds `implementer` to `forge`, `forge` must be something this runtime can
execute. Registering one declares its name, its egress route, and how it
executes a bead. The built-ins register themselves from `orchestrator` on
import; an extension registers its own the same way it registers a cycle
handler, and from then on the name is dispatchable, is known to `doctor`,
and is subject to the egress gate under the route it declared.

The execute callable takes `(orchestrator, task)` and returns whether the
bead completed — the same contract as a cycle handler, and for the same
reason: the backend owns the claim, the completion and the failure path,
and the cycle only asks whether it is done.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import egress

Execute = Callable[[object, object], bool]  # (orchestrator, task) -> completed?


@dataclass(frozen=True)
class Backend:
    name: str
    route: str          # a key in the egress route table: NATIVE, TEMPER, FORGE, ...
    execute: Execute
    egress_checked: bool = True   # False only for in-process work that sends nothing anywhere


EXECUTOR_BACKENDS: dict[str, Backend] = {}


def register_executor_backend(
    name: str, execute: Execute, *, route: str, egress_checked: bool = True
) -> Backend:
    """Make `name` a dispatchable executor with `route` as its egress ceiling.

    Re-registering a name replaces it — an extension may override a built-in
    on purpose (a different `claude` wrapper, say), and the last registration
    wins the way the last handler registration does.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a backend needs a name")
    if not isinstance(route, str) or not route.strip():
        raise ValueError(f"backend {name!r} needs an egress route")
    backend = Backend(name=name.strip(), route=route.strip(), execute=execute, egress_checked=egress_checked)
    EXECUTOR_BACKENDS[backend.name] = backend
    # The egress gate resolves a route by agent name; a backend that is
    # dispatchable but unknown to the gate would be refused at dispatch with
    # "no declared egress route", which is the right refusal for the wrong
    # reason. Registering here is declaring there.
    egress.AGENT_ROUTE[backend.name] = backend.route
    return backend


def resolve(name: str | None) -> Backend | None:
    return EXECUTOR_BACKENDS.get(name) if name else None


def executor_names() -> frozenset[str]:
    """Every name a bead may be assigned to and be executed by a backend.

    A live view, not a constant: the doctor's dispatchability guard and the
    cycle's undispatchable check both ask this, and both must see a backend
    an extension registered after import.
    """
    return frozenset(EXECUTOR_BACKENDS)
