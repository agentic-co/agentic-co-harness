# agentco-harness — project rules

Shared agent context lives in [`AGENTS.md`](AGENTS.md); the active work front's state-of-record
is [`ai-tasks/asop-v3.2/CONTEXT.md`](ai-tasks/asop-v3.2/CONTEXT.md). **Read CONTEXT.md before
touching ASOP, gates, verification, adjudication, or the embedded runtime** — Claude Code,
Codex, and agy are all working this front, and that file is what keeps them from re-deriving
(or contradicting) each other. Update it in the same turn as any material change.

> `ai-tasks/` is **local-only and gitignored** — it carries one deployment's coordination state (which agents are live, vendor limits, machine permissions), which is not what a public runtime repo publishes. A fresh clone will not have it; the agents working an existing tree still do.


## Invariants specific to this repo

- **The gate is the contract, not a suggestion.** `Beads.update()` is the single choke point
  where a bead reaches `done`; every path (`complete()`, the CLI, the orchestrator, the agent
  itself) goes through it. Do not add a second way to reach `done`.
- **`asop-spec` arrives by version, never by path.** Gate kinds, refusal codes, and attestation
  shape are defined in `github.com/mabidoli/asop` — change the spec there and adopt it here;
  don't fork the semantics locally.
- **Never push or rebase without checking `git log origin/main..HEAD` first.** This repo
  routinely carries unpushed local commits.
- Tests are `uv run pytest -q`; the green baseline is 1249 passed, 1 skipped.
