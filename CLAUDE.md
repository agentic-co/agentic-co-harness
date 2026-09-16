# agentco-harness — project rules

Shared agent context lives in [`AGENTS.md`](AGENTS.md).

**Start at [`ai-tasks/unified/README.md`](ai-tasks/unified/README.md)** — the sequenced plan across
all fronts (one core, three hosts; seven phases; three tracks). Its
[`DECISIONS.md`](ai-tasks/unified/DECISIONS.md) is the compact record of what has been decided and
what those decisions rule out — **read that first after a context clear.**

⚠️ **Before believing anything downstream of Phase 3, read
[`ai-tasks/unified/EVIDENCE.md`](ai-tasks/unified/EVIDENCE.md):** the ASOP apparatus **loses to a
bare prompt on airline** (−0.240 paired, 7-1, **p = 0.070** — the previously published 0.039 is
withdrawn) and **ties it on retail** (26/30 vs 26/30). Two domains, opposite answers. The gate has
still never fired deterministically in either. The coding domain is untested and is the entire
remaining argument for the mechanism.

Per-front state-of-record, still current and still authoritative for their own scope:
[`ai-tasks/asop-v3.2/CONTEXT.md`](ai-tasks/asop-v3.2/CONTEXT.md) (contract, gates, verify
authority) · [`ai-tasks/embedded-plane/PLAN.md`](ai-tasks/embedded-plane/PLAN.md) (the runtime
migration, P2a in flight) · [`ai-tasks/asop-eval/CONTEXT.md`](ai-tasks/asop-eval/CONTEXT.md) (the
empirical programme). **Read the relevant one before touching ASOP, gates, verification,
adjudication, or the embedded runtime** — Claude Code, Codex, and agy all work these fronts, and
those files are what keep them from re-deriving (or contradicting) each other. Update them in the
same turn as any material change.

> `ai-tasks/` is **local-only and gitignored** — it carries one deployment's coordination state (which agents are live, vendor limits, machine permissions), which is not what a public runtime repo publishes. A fresh clone will not have it; the agents working an existing tree still do.


## Invariants specific to this repo

- **The gate is the contract, not a suggestion.** `Beads.update()` is the single choke point
  where a bead reaches `done`; every path (`complete()`, the CLI, the orchestrator, the agent
  itself) goes through it. Do not add a second way to reach `done`.
- **`asop-spec` arrives by version, never by path.** Gate kinds, refusal codes, and attestation
  shape are defined by the `asop-spec` package — change the spec at its own source and adopt a
  new version here; don't fork the semantics locally. (The rule applies to this line too: a
  repository URL is a path, so the spec is named by package, not located by link.)
- **Never push or rebase without checking `git log origin/main..HEAD` first.** This repo
  routinely carries unpushed local commits.
- Tests are `uv run pytest -q`; the green baseline is **1377 passed, 2 skipped** (re-measured 2026-09-15; the older 1345 and 1375 figures were both stale).
