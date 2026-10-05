# Audit bundles (`agentic-co audit`)

Use this to bring evidence back from a harness that ran somewhere else, such as a
throwaway VM with no hub, without bringing back what the work was about.

```sh
agentic-co audit export --node /path/to/node [--since 2026-10-01T00:00:00Z] [--out DIR]
agentic-co audit verify DIR/agentic-co-audit-<node>-<UTC stamp>          # or the .tar.gz
```

`export` writes a bundle directory and a `.tar.gz` of it into `--out`. The default is
`~/agentic-co-audit`. A bundle inside the node is refused. `verify` recomputes every
file's sha256, byte count and row count against `manifest.json`, and it also flags
files the manifest doesn't list. It exits 0 when the bundle is intact and 1 when it
isn't. The check catches corruption and edits that didn't update the manifest. It is
**not a signature**: someone who rewrites the manifest too gets past it.

## Tier 1: metadata only

| file | contents |
|---|---|
| `beads.jsonl` | id, status, timestamps, assignee/agent, parent/blocked_by, task_class, gate kind and outcome, refusal code |
| `ledgers/*.jsonl`, `ledgers/*.json` | runs, usage, costs, schedules, recurring, asops, heartbeat, node state, cut down per field |
| `refusals.jsonl` | `dispatch_refusal` code + time per bead |
| `doctor.json` | doctor's exit code, counts, and `{class, check}` per finding, without messages |
| `environment.json` | harness/asop-spec/python versions, platform, `claude`/`uv` presence + version token, sha256 of the config file |
| `manifest.json` | schema `agentic-co-audit-v1`, per-file hashes, per-source counts, the redaction report |

None of the following is exported: titles, descriptions, results, chat, prompts,
model output, gate commands or their output, free-text errors, config values, env
values, or paths under the home or node directory. A kept string that contains such
a path is replaced by its sha256.

**Deny by default.** `agentco_harness/audit.py::POLICY` is the single table that
decides every field. Each field is kept, denied, or reduced through a nested shape,
and each decision carries a one-line justification. Any field the table doesn't know
is dropped, and the manifest's `redaction` section counts it by name. When a ledger
writer gains a field that is neither kept nor denied, `tests/test_audit_export.py`
fails.

**Null, never zero.** Unreported values stay `null`. A missing ledger reports
`rows_read: null`. A filter that wasn't applied reports its counts as `null`.

**Read-only.** Ledgers are read with plain file reads. No store class is built and
no lock is taken. Corrupt or quarantined lines are counted and their content is never
exported. Doctor creates `tasks.jsonl`/`recurring.jsonl` when they are missing, so
it runs against a temporary symlink shadow of the node.

`--since` filters event ledgers by `at`, beads by `updated_at` and refusals by `at`.
A timestamp that can't be parsed keeps its row, and the row is counted. Recurring
definitions, ASOPs and heartbeats are state snapshots and are always exported whole.

## Known gaps

- **Refusals are only partly persisted.** The cycle records an egress denial only as
  `dispatch_refusal.code = "egress_denied"`, and the specific `egress:*` code is lost.
  The chat-reply path records nothing structured. `command_floor.check_command` isn't
  called from any execution path. A refusal cleared by `clear_dispatch_refusal` leaves
  no record. The manifest's `refusal_coverage` says this inside every bundle.
- Bead metadata whose shape hasn't been verified (`superseded`, `cancellation`,
  `retirement`, `lease_report`, `run_review`, `hub`) is dropped, and the redaction
  report shows it. Allowlist those fields once their shapes are checked.
- Not exported yet: `asop_candidates.jsonl`, `children/registry.jsonl`, the pull ledger.
