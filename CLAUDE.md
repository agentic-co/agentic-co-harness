# agentco-harness — project rules

Shared agent context lives in [`AGENTS.md`](AGENTS.md).

**Start at [`ai-tasks/unified/README.md`](ai-tasks/unified/README.md)** — the sequenced plan across
all fronts (one core, three hosts; seven phases; three tracks). Its
[`DECISIONS.md`](ai-tasks/unified/DECISIONS.md) is the compact record of what has been decided and
what those decisions rule out — **read that first after a context clear.**

⚠️ **Before believing anything downstream of Phase 3, read
[`ai-tasks/unified/EVIDENCE.md`](ai-tasks/unified/EVIDENCE.md):** the ASOP apparatus **loses to a
bare prompt in BOTH domains, by very different margins** — airline −0.240 (7-1, **p = 0.070**;
the published 0.039 is withdrawn), retail **−0.083** (4-1, p = 0.375) in the clean
`--defer-consent-stop` regime. ⚠️ **An earlier "retail ties" reading is withdrawn** — it came from
a 30-cell comparison after dropping faulted cells; the 36-cell fixed-regime pair is the better
measurement. The gate has still never fired deterministically on retail. The coding domain is untested and is the entire
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
  where a bead reaches **any terminal state**; every path (`complete()`, the CLI, the
  orchestrator, `decline --terminal`, `approve reject`, the agent itself) goes through it. Do not
  add a second way to reach a terminal state.
  **This rule used to say `done`, and that is exactly how it was broken** (N9, resolved by the
  principal 2026-09-16): `decline(terminal=True)` and `approve reject` reached SKIPPED without
  ever consulting `metadata.verify`, so the letter held while the purpose did not. What the choke
  point does at each terminal status is now declared in `beads.TERMINAL_GATE_POLICY`, and
  `tests/test_terminal_paths.py` enumerates the doors independently of that table — so a new
  terminal status or a new door fails a test rather than relying on somebody remembering
  this paragraph.
- **`asop-spec` arrives by version, never by path.** Gate kinds, refusal codes, and attestation
  shape are defined by the `asop-spec` package — change the spec at its own source and adopt a
  new version here; don't fork the semantics locally. (The rule applies to this line too: a
  repository URL is a path, so the spec is named by package, not located by link.)
- **Never push or rebase without checking `git log origin/main..HEAD` first.** This repo
  routinely carries unpushed local commits.
- Tests are `uv run pytest -q`; the green baseline is **1473 passed, 2 skipped** (2026-09-16, after N9, N11, C1-coding scoring and P2b). ⚠️ **The recorded figure has now been stale four times** — 1417 here and in `AGENTS.md` while the tree measured 1431, and 1345/1375/1377 before that. It is the smallest possible instance of N10: a claim about the code, written once, believed after. **Measure it, do not quote it.**
