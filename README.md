# AgentCo Harness

A standalone runtime for agentic work: a local bead store, a heartbeat cycle
that decomposes, triages, dispatches and verifies beads, human executors as
first-class assignees, and a doctor that tells you what is broken before the
first cycle silently no-ops.

It is the second product of the AgentCo split:

| | **AgentCo Hub** (`agentco`, public) | **AgentCo Harness** (this repo) |
|---|---|---|
| What it is | The coordination plane: scope claims, snapshot pointers, fenced leases, ASOP storage, gate routing, the human decision router | The execution runtime: beads, cycles, executors, RCA, schedules, doctor |
| Runs alone? | Yes — any harness (Codex, Claude Code, your own) connects directly | Yes — solo operators run it with no server at all |
| Together | The Harness is one Hub participant (reference L3 client, publishing off by default) | The Harness applies the Hub's ASOPs and reports outcomes |

The Harness descends from the private v1 hub (`agentco-hub`) with the
personal pipelines removed and replaced by extension seams. Nothing in this
package knows about a particular company, calendar, mailbox, ticket system or
assistant.

## Status

Phase 0 of 4 — mechanical extraction. The suite (1,039 tests, carried from v1
and re-pointed) is green with a fake LM; no network, no keys in CI.

| Phase | Outcome |
|---|---|
| **P0 (done)** | v1 core extracted into `agentco_harness`; retro / feeds / sources replaced by registries; RCA escalation target configurable |
| P1 | Shared ASOP contract package (`agentco/packages/asop`) — gate schema, attestation shapes, refusal vocabulary. Both products validate against it |
| P2 | ASOP runtime inside the Harness (create, apply, maintain SOPs locally) + a real executor backend interface; config loses its v1-only blocks |
| P3 | Optional Hub client — L3 participant, publishing off by default, human-prompted |
| P4 | v1 hub migrates onto Harness + a LifeOS extension pack that re-registers what P0 removed |

## Install

```sh
uv venv && uv pip install -e ".[dev]"      # runtime + tests
uv pip install -e ".[lm]"                   # DSPy triage/planner layer (optional)
uv run pytest -q
```

Console script: `harness` (v1 and the Hub both claim `agentco`).

## The shape

```
harness init                 # config.yaml + tasks.jsonl in the current dir
harness tasks create "..."   # every unit of work is a bead
harness cycle                # recurring → triage → dispatch → verify, once
harness daemon               # the same, forever
harness me                   # the human queue: what depends on you
harness doctor               # preflight, classified by consequence (exit 0/1/2)
harness pull / report        # the cross-machine lease protocol (worker side)
```

A bead is a JSONL line. The store is append-only and quarantine-preserving: a
corrupt line is reported by `doctor`, never executed, never dropped. Watermarks
and heartbeats move only on genuine completion.

## Extension seams

v1 hard-wired its owner's pipelines into the cycle. The Harness exposes the
same three points as registries in `agentco_harness.orchestrator`:

```python
from agentco_harness.orchestrator import (
    register_cycle_handler,      # task metadata `type` → handler(orch, task, now) -> bool
    register_completion_hook,    # hook(orch, task) after a bead goes DONE; isolated
    register_source_factory,     # factory(config) -> [source]; source.poll() -> events
)
```

A handler owns its task type end to end (claim, complete or fail). An
unregistered type is *not* skipped — it takes the ordinary executor path. Hooks
are isolated: one bad extension prints a warning and the cycle continues. With
no source factories registered, `observe()` is a heartbeat-only no-op.
`tests/test_extension_seams.py` pins this contract.

Automated escalations (an RCA loop that exhausts its cycles) land on
`humans.escalate_to` in `config.yaml` (`human:<name>`; default `human:operator`).

## Two-machine lane

`scripts/two-machine/agentco-pull-forced-command.sh` is the hub-side SSH
forced command that lets a worker machine `pull` and `report` beads over a
fenced lease and nothing else — every argument is validated, every call
audited, and a denied call is logged with its reason. The worker-side script
was v1's and is company-specific; a generic one arrives with P4.

## Provenance

Carried over from v1 as-is: module docstrings that narrate the incidents each
invariant came from. They mention the original deployments by name because
that is the evidence; nothing executes against them.

Agent-authored commits carry the bead id (`(ac-xxxxxxxx)`) that produced them.

## License

Apache-2.0.
