# End to end: one plane, one ASOP, three participants

`two_harnesses.py` stands up a plane from the Hub repo, authors the `feature-dev`
ASOP on it, files one run bound to three different participants, and drives the
run to completion while checking every claim the contract makes along the way.

| role | participant | how |
|---|---|---|
| analyst | `claude-code` | a direct participant over signed HTTP (default), or real Claude Code over MCP with `--claude-code mcp` |
| implementer | `harness-bigmac` | this runtime: `agentic-co hub pull` → execute → `agentic-co hub sync` |
| validator | `agy` | a direct participant over signed HTTP (default), or real headless agy over MCP with `--agy mcp` (`agy --print`, agy ≥ 1.1.26; agy is OAuth-only, so the machine must already hold an interactive login) |

```sh
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --auto-approve     # fully automated
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub                    # pauses for agy and for the human gate
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --live             # the runtime runs its real backend for `implement`
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --claude-code mcp  # real headless Claude Code as the analyst
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --claude-code mcp --agy mcp --auto-approve  # both real agents
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --gate judged --auto-approve   # a judged gate + its rails
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --claude-code mcp --agy mcp --live --gate judged --auto-approve   # all of it
```

**Proven 2026-09-04**, all four modes green:

| mode | checkpoints |
|---|---|
| deterministic (default) | 22/22 |
| `--claude-code mcp --agy mcp` | 22/22 |
| `--live` | 22/22 |
| `--gate judged` | 25/25 |
| everything at once: `--claude-code mcp --agy mcp --live --gate judged` | 25/25 |

In the MCP mode both agents found the plane through the Hub's `serve-mcp`
surface, pulled only their own step, and reported it. Claude Code's
first `work_report` carried an extra `cwd` inside the attestation; the plane
refused it (`attestation_invalid`, §5.3 names exactly five fields) and the
agent moved the detail into `result` and retried — the contract's error text
was enough for an unbriefed agent to self-correct. Note that each CLI
still loads its own operator-level instruction files, so a participant's replies
carry whatever persona that machine configures — expected, and worth knowing
when reading a transcript.

Deterministic by default: the implementer's work is a prepared patch to a tiny
target repo, and the deterministic gate (`pytest`) is what proves it — the
point is the pipeline, not the model. Everything lands in a temp work dir the
script prints; the plane's log is `plane/server.log`.

Checkpoints, in order: plane up · v1 authored and active · separation of duties
refused at filing · run filed · analyst did step 1 only · runtime did 2–4 ·
human gate parked step 5 · verifier answered · **§5.5 parent closed** ·
outcomes 1/1/0 · good adjudication · self-adjudication refused · `propose`
drafted v2 with the proposal on step 3 · v2 active · v1 pin resolves · new run
pins v2 · outcomes has two rows · retire refuses new runs.

`--gate judged` runs the same procedure with a judged gate on step 5, answered by
a declared verifier (`AGENTCO_VERIFIERS`) rather than a named human. It checks the
rails before the verdict, so the run is 25 checkpoints rather than 22: an
undeclared actor claiming `verify` is refused, the party that executed the step is
refused, and a declared verifier that does not claim the capability is refused too.
Both halves are required — the operator's declaration is the authority, and
claiming the capability is not.

`--live` found a real defect and now guards it. The mirror stamped a pulled
bead with the plane ACTOR (`harness-bigmac`), which is an identity on the
plane, not a runner here — so every pulled bead failed the cycle with
"Unknown agent" and spawned an RCA bead apiece, and the run ended 10/22. The
runtime now separates the two: `hub.actor` is who this node is there,
`hub.executor` is which local backend runs what it pulls, `doctor` refuses a
plane configured without one, and the mirror hands that executor the step's
own words plus the directory its gate runs in. Deterministic mode never saw
it because the script completes the beads itself and never dispatches.

Under `--live` the prepared patch is NOT written to the target repo. The first
live run left it there and the implementing model read it, which proves
dispatch and nothing about solving anything. Unaided, the model wrote its own
`slugify` and the gate passed on an independent re-run; three steps cost about
$2.10 on Opus 5.
