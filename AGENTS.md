# agentco-harness — agent context

## Active work front: ASOP v3.2

**Before doing anything on ASOP, gates, verification, adjudication, or the embedded runtime,
read [`ai-tasks/asop-v3.2/CONTEXT.md`](ai-tasks/asop-v3.2/CONTEXT.md).** It carries verified
baseline SHAs, what has already landed, what is in flight and by whom, and the ranked pending
list. It exists so you do not re-derive state three agents have already established.

If you change something material on this front, update that file in the same turn.

## Lane discipline

Claude Code, Codex, and agy have all been active in this repo simultaneously. Parallel agents in
one repo need **disjoint file lanes** — claim yours in `CONTEXT.md` before writing code, or
serialize. Two agents extending the same file silently clobber each other.

## Repo facts worth knowing

- `main` here can sit **ahead of origin and unpushed** — check `git log origin/main..HEAD`
  before assuming the remote reflects local work. Do not force-push or rebase without asking.
- The contract package `asop-spec` comes from `github.com/mabidoli/asop` **by version**, not by
  path. The spec is the source of truth for gate/refusal/attestation semantics; this repo
  implements it, it does not define it.
- Tests: `uv run pytest -q`. Green baseline is **1249 passed, 1 skipped**.
