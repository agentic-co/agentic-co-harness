# End to end: one plane, one ASOP, three participants

`two_harnesses.py` stands up a plane from the Hub repo, authors the `feature-dev`
ASOP on it, files one run bound to three different participants, and drives the
run to completion while checking every claim the contract makes along the way.

| role | participant | how |
|---|---|---|
| analyst | `claude-code` | a direct participant over signed HTTP (default), or real Claude Code over MCP with `--claude-code mcp` |
| implementer | `harness-bigmac` | this runtime: `harness hub pull` → execute → `harness hub sync` |
| validator | `agy` | a direct participant; agy is OAuth-only, so the script pauses at a checkpoint for an interactive session, or plays it over HTTP with `--auto-approve` |

```sh
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --auto-approve     # fully automated
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub                    # pauses for agy and for the human gate
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --live             # the runtime runs its real backend for `implement`
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --claude-code mcp  # real headless Claude Code as the analyst
```

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

`--gate judged` is not wired yet: it needs a declared adjudicator route on the
plane (decision 6). The first run uses the human gate.
