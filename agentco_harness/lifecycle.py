"""The seam between this runtime and whatever implements the ASOP lifecycle.

P1 of `ai-tasks/embedded-plane/PLAN.md`. Today `open_lifecycle` returns the
runtime's own `Beads`; in P2 it returns a plane embedded in-process, and every
caller changes not at all. That is the entire point of the phase — make the
substitution a one-line change so the risky work is swapping one implementation
for another, not swapping one implementation for another *while also* rewriting
forty call sites.

**Why a construction seam and not method wrappers.** Wrapping every method
would add a layer that forwards and does nothing, and a forwarding layer is a
place for behaviour to drift quietly — the exact failure this whole effort is
about. A `Protocol` is structural: `Beads` satisfies it without inheriting from
it or knowing it exists, so the contract is documented and type-checked while
the call path stays direct.

**What this deliberately does NOT do.** It does not unify the runtime's
lifecycle with the plane's, and nothing here is a step toward equivalence on
its own. The runtime's `beads.py` carries behaviour the plane does not —
specless-done classification, `SELF_REPORTING_EXECUTORS`, the `human:`
assignment lineage — and P2 has to move each of those or prove it unused. This
module makes that comparison possible; it does not perform it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from .beads import Beads, Task, TaskStatus


@runtime_checkable
class Lifecycle(Protocol):
    """What this runtime asks of a store that owns work items.

    The surface is the one actually exercised across the package, not
    everything `Beads` happens to expose — a seam wide enough to admit any
    method a caller might reach for is not a seam. If P2 finds a caller needing
    something absent here, that is a finding about the call site as much as
    about the interface: it means a caller reached past the contract.
    """

    def create(self, title: str, description: str = ..., **kwargs: Any) -> Task: ...

    def get(self, task_id: str) -> Optional[Task]: ...

    def list(self, **kwargs: Any) -> list[Task]: ...

    def ready(self, **kwargs: Any) -> list[Task]: ...

    def update(self, task_id: str, **kwargs: Any) -> Optional[Task]: ...

    def complete(self, task_id: str, result: Optional[str] = ...) -> Optional[Task]: ...

    def claim(self, task_id: str, agent: str, **kwargs: Any) -> Optional[Task]: ...

    def approve_verify(
        self, task_id: str, approver: str, reason: Optional[str] = ...
    ) -> Optional[Task]: ...

    def reject_verify(
        self, task_id: str, approver: str, reason: Optional[str] = ...
    ) -> Optional[Task]: ...

    def awaiting_verify(self) -> list[Task]: ...

    # --- the plane's vocabulary, adopted ahead of the swap (P2a) ------------
    # These are not additions to the runtime's own shape; they are the verbs
    # the plane already speaks, moved in first so that P2c substitutes an
    # implementation rather than a language. `update` and `complete` above are
    # on their way out — P2b deletes them, which is what keeps this option 3
    # rather than an adapter with two vocabularies kept alive side by side.

    def annotate(self, task_id: str, metadata: dict) -> Optional[Task]: ...

    def refuse_dispatch(
        self, task_id: str, code: str, message: str, remediation: Optional[str] = ...
    ) -> Optional[Task]: ...

    def clear_dispatch_refusal(self, task_id: str) -> Optional[Task]: ...


def open_lifecycle(path: Path | str, **kwargs: Any) -> Lifecycle:
    """Open the lifecycle store for this node.

    The one place the implementation is chosen. Callers take what they are
    given and do not name a class, so P2 changes this function and nothing
    else.
    """
    return Beads(path, **kwargs)


__all__ = ["Lifecycle", "open_lifecycle", "Task", "TaskStatus"]
