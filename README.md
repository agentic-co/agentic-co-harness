# AgentCo Harness

A standalone runtime for agentic work: a local bead store, a heartbeat cycle
that decomposes, triages, dispatches and verifies beads, human executors as
first-class assignees, and a doctor that tells you what is broken before the
first cycle silently no-ops.

It is the second product of the AgentCo split:

| | **AgentCo Hub** (`agentic-co-hub`, public) | **AgentCo Harness** (`agentic-co-harness`, this repo) |
|---|---|---|
| What it is | The coordination plane: scope claims, snapshot pointers, fenced leases, ASOP storage, gate routing, the human decision router | The execution runtime: beads, cycles, executors, RCA, schedules, doctor |
| Runs alone? | Yes — any harness (Codex, Claude Code, your own) connects directly | Yes — solo operators run it with no server at all |
| Together | The Harness is one Hub participant (reference **L2 worker** — pull, report, attest; publishing off by default) | The Harness applies the Hub's ASOPs and reports outcomes |

The Harness descends from the private v1 monolith (`mabidoli/agentco-v1`) with the
personal pipelines removed and replaced by extension seams. Nothing in this
package knows about a particular company, calendar, mailbox, ticket system or
assistant.

## Status

Phases 0–3 of 4 are done: the runtime is extracted, it speaks the ASOP contract,
it runs procedures locally with no server, and it participates in a coordination
plane as a level-three client. The suite is green with a fake LM — no network, no
keys in CI.

| Phase | Outcome |
|---|---|
| **P0 (done)** | v1 core extracted into `agentco_harness`; retro / feeds / sources replaced by registries; RCA escalation target configurable |
| **P1 (done)** | Shared ASOP contract package (`packages/asop` in the Hub repo) — gate schema, attestation shapes, refusal vocabulary. Both products validate against it |
| **P2 (done)** | ASOP runtime inside the Harness (create, apply, maintain procedures locally) + the executor backend seam; config lost its v1-only blocks |
| **P3 (done)** | Optional Hub client — an L2 worker on the plane (pull, report, attest), publishing off by default. Proven end to end: one plane, one procedure, three participants from three vendors |
| P4 | v1 hub migrates onto the Harness + a LifeOS extension pack that re-registers what P0 removed |
| Later | The ASOP contract (`agentco-asop`) moves to its own repository with its own releases, and both products pin a version. Until the first version is finalised it stays in the Hub repo's `packages/asop/` and this runtime depends on it by git subdirectory — decided 2026-09-04 |

### Proven, not asserted

`scripts/e2e/two_harnesses.py` stands up a plane, authors the `feature-dev`
procedure on it, files one run bound to three different participants, and drives
it to completion while checking every claim the contract makes. All five modes are
green ([details](scripts/e2e/README.md)):

| mode | what it adds | checkpoints |
|---|---|---|
| deterministic | the pipeline, no model in the loop | 22/22 |
| `--claude-code mcp --agy mcp` | two other vendors' CLIs as real participants | 22/22 |
| `--live` | this runtime dispatches a real model for its own steps | 22/22 |
| `--gate judged` | a declared verifier answers, and three usurpations are refused | 25/25 |
| all of the above at once | | 25/25 |

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
harness sop create f.yaml    # a procedure, versioned — an ASOP (see below)
harness sop activate ID 1    # only an active version files runs
harness sop run ID --input k=v --bind role=agent
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
from agentco_harness.backends import (
    register_executor_backend,   # name + egress route + execute(orch, task) -> bool
)
```

A handler owns its task type end to end (claim, complete or fail). An
unregistered type is *not* skipped — it takes the ordinary executor path. Hooks
are isolated: one bad extension prints a warning and the cycle continues. With
no source factories registered, `observe()` is a heartbeat-only no-op.
`tests/test_extension_seams.py` pins this contract.

A backend is what an ASOP role binding names: bind `implementer` to `forge`
and `forge` must be a registered backend. `claude`, `zai`, `forge` and
`planner` are built in; registering another makes it dispatchable, known to
`doctor`, and gated by egress under the route it declared.

## ASOPs

The runtime keeps procedures in `asops.jsonl` beside the queue, versioned,
and speaks the ASOP v3 contract (`agentco-asop`, the package the Hub also
imports — nothing here imports the Hub). An ASOP is an ordered sequence of
steps for one type of task; `harness sop run` files a parent bead pinned to
the version and one bead per step, each carrying its step's text and gate.
The full definition, verbs and decisions are in the contract's `ASOP.md`.

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
