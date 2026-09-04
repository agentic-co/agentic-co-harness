# End to end: one plane, one ASOP, three participants

`two_harnesses.py` stands up a plane from the Hub repo, authors the `feature-dev`
ASOP on it, files one run bound to three different participants, and drives the
run to completion while checking every claim the contract makes along the way.

| role | participant | how |
|---|---|---|
| analyst | `claude-code` | a direct participant over signed HTTP (default), or real Claude Code over MCP with `--claude-code mcp` |
| implementer | `harness-bigmac` | this runtime: `harness hub pull` → execute → `harness hub sync` |
| validator | `agy` | a direct participant over signed HTTP (default), or real headless agy over MCP with `--agy mcp` (`agy --print`, agy ≥ 1.1.26; agy is OAuth-only, so the machine must already hold an interactive login) |

```sh
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --auto-approve     # fully automated
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub                    # pauses for agy and for the human gate
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --live             # the runtime runs its real backend for `implement`
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --claude-code mcp  # real headless Claude Code as the analyst
scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub --claude-code mcp --agy mcp --auto-approve  # both real agents
```

**Proven 2026-09-04:** 22/22 in the deterministic mode and 22/22 with
`--claude-code mcp --agy mcp`. Both agents found the plane through the Hub's
`serve-mcp` surface, pulled only their own step, and reported it. Claude Code's
first `work_report` carried an extra `cwd` inside the attestation; the plane
refused it (`attestation_invalid`, §5.3 names exactly five fields) and the
agent moved the detail into `result` and retried — the contract's error text
was enough for an unbriefed agent to self-correct. agy answered in the
principal's DA voice because `~/.gemini/config/AGENTS.md` carries the LifeOS
identity; that file is written by LifeOS setup, so this is expected.

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
