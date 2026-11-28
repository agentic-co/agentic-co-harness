# Runtime 1.0: scope, cut to match hub 1.1

**Status:** proposed, 2026-11-14; first build pass 2026-11-28 (see "Build status" below). Nothing here is built unless a row or that section says so. Every "today"
claim below was read from code at `main` `fd6c0ec`, not remembered; file and function names are
given so it can be checked again.

## Why a 1.0 now

The hub froze 1.0 and planned 1.1 (hub `docs/roadmap.md`, "Version 1.1"). Hub ADRs 0012–0016 drew
the line between the two repos. The runtime has no version line yet, and a connected runtime
today is wrong in ways no test catches: it keeps a second authoritative copy of hub work, reports
to whichever hub is configured now, and runs a shell command any hub item names.

Runtime 1.0 is the release that makes **one runtime connected to one or more hubs correct**. It
adds no new product surface. Its compatibility line:

| | requires |
|---|---|
| hub | **1.1** (memory verb, provenance on `sop_revise`, shared line-id derivation) |
| spec | `asop-spec>=0.6.1,<1` (already pinned in `pyproject.toml`; the hub accepts 0.4.0–0.6.1) |

## The scope

Each row ships on its own, with one acceptance test that fails before the work (RED first).

### A. Hub work is the hub's (ADR 0012)

| # | item | today | done means |
|---|---|---|---|
| A1 | **Journal, not mirror** | `HubClient.mirror` files hub items as ordinary beads (`hub_client.py`) | A pulled item is a journal entry that cannot close, unblock or re-parent anything locally. A test that closes a mirrored item locally and expects a local unblock fails. |
| A2 | **Hub of origin on every item** | The idempotency key is `hub:<item_id>` with no hub component; `report_back` uses the configured client, not `metadata.hub.url` | The key carries the hub's generated id. A report goes only to the item's origin hub; if that hub is no longer configured, the report is refused with a code, never sent elsewhere. Two-hub test. |
| A3 | **Fresh fence** | A re-lease returns the existing bead and keeps the old `attempt` | A re-lease updates the attempt; a report under a stale attempt is never sent. |
| A4 | **Lease renewal** | Never renews | A run longer than the lease TTL renews it; a test with a short TTL keeps the lease. |
| A5 | **Gate outcomes reach the hub** | Deterministic gates send an attestation inline with `done` (`attestation_for`). A step with several checks sends the list as `check`. Judged and human gates park locally and are never reported. | Attestation shape valid under the pinned spec for one and several checks. A judged or human gate reports `done` without an attestation, and the hub derives AWAITING_VERIFY (ADR 0012 §3). Nothing parks silently. |
| A6 | **Scope claims** | Never claims | An item that declares a scope is claimed before work and released after; a conflict is a coded refusal, not a run. |
| A7 | **Automatic intake** | Reporting is automatic (`completion_hook`); pulling is manual | A cycle handler pulls up to free capacity on each cycle, off by default per node. |
| A8 | **Trust what the hub sends only as far as configured** | Hub metadata sets the brief, `workdir` and the gate, and gate checks run as shell (`config.py` notes that nothing verifies what comes back) | `workdir` must sit under configured roots; a gate check must be a named check from the check library (ADR 0014) unless the node opts in to raw commands. Anything else is a coded refusal before the executor starts. |

### B. Everything the runtime says goes through the outbox (ADR 0013, 0015)

| # | item | today | done means |
|---|---|---|---|
| B1 | **Outbox writer + clean room** | Built and merged (#24, #25) | — |
| B2 | **Every statement through it** | Report and attest still go over direct HTTP | Report, attest, proposals and memory are outbox lines; the direct HTTP path remains only for pull and renew. |
| B3 | **The drainer is the hub's** | None on a runtime-only node | The runtime depends on the hub's client package (base install, no server extra) and runs its drainer once per cycle under its drain lock. The runtime never re-implements signing. See "Answers" Q2. |
| B4 | **Memory client** | None | Write via the 1.1 `memory_create` verb with `provenance{kind,ref}`; read via `/memory/search`. Waits on hub 1.1 item 1. |
| B5 | **Proposals carry provenance** | Client built (#26); the hub drops `provenance` today | The hub stores it. Waits on hub 1.1 item 3. |
| B6 | **One line-id derivation** | Runtime derives `ob_` + sha256[:16] itself | Uses the hub's shared helper. Waits on hub 1.1 item 4. |

### C. One identity per instance

| # | item | today | done means |
|---|---|---|---|
| C1 | **Connections registry + `connect`** | In review (#28) | Merged, review findings closed. |
| C2 | **Identity wired into every subprocess** | In review (#29) | Merged; no path (executor, agents, triage, gate env, git/gh) falls back to a machine credential under an identity. |
| C3 | **The hub credential is part of the identity** | The hub transport credential comes from node config/env, outside `identity:` | `identity:` names the hub credential too, so two instances on one machine cannot report with each other's credential. |

### D. Pinned procedures (ADR 0016)

| # | item | today | done means |
|---|---|---|---|
| D1 | **Digest pinning** | Store level merged (#27) | — |
| D2 | **Execution resolves the pin** | `resolve_pinned` exists, nothing calls it | A run whose pinned digest does not match is refused `asop_digest_mismatch`; audit records the digest. |
| D3 | **Hub library as a pinned cache** | The runtime keeps its own store, no fetch | A connected runtime fetches a procedure version from the hub and caches it by digest; a standalone runtime keeps its own store. |

### E. Execution robustness

| # | item | done means |
|---|---|---|
| E1 | Evidence ledger | Each gate run leaves its check artefact per run key. |
| E2 | Crash is not an attempt | A process crash returns the item without spending an attempt. |
| E3 | Tier escalation on retry | A retry after a gate refusal may move up one model tier, bounded by config. |

### F. Hygiene

| # | item | done means |
|---|---|---|
| F1 | Command name | Prompts and messages say `agentic-co`, not the old `agentco` (executor, doctor, schedules). |
| F2 | Orphan-bead doctor check | `doctor` warns on a bead with no parent goal. |

## Build status (2026-11-28, branch `feat/v1-config-and-connected`)

| item | state |
|---|---|
| A1 | **Partly.** Nothing local can block on or be parented under a hub-pulled bead (`Beads._refuse_hub_owned`), so a local close releases nothing. Not done: the separate journal type of hub ADR 0012 §2. A pulled item is still a bead the executor runs, and completion hooks still fire for it (the hub report is one). Moving to a journal entry changes what the executor runs; that is a design call. |
| A2 | Done. Key is `hub:<hub generated id from GET /hub>:<item id>` (falls back to the url on a plane that has no `/hub`); a report from any other hub is refused `hub_of_origin_mismatch`, an unconfigured one is not sent. |
| A3 | Done. A re-lease takes the new attempt; a bead that finished under the old one is refused `stale_attempt` at report time. |
| A4 | Done. `HubClient.keep_lease` renews every third of the TTL while a mirrored bead runs (both dispatch paths). |
| A5 | Done. A staged gate sends one attestation per rung, each with `stage` (the old single record with a list as `check` was invalid under the pinned spec); the first rides with the report, the rest go by `attest`. A judged or human gate reports `done` with no attestation. |
| A6 | **Skipped, needs a decision.** The hub work item has no scope field (hub roadmap open item 7): either the hub adds one or the runtime derives scope from repository and workdir. |
| A7 | Done. `hub.intake: true` pulls up to `hub.intake_max_open` (default 3) minus items still held, at the start of each cycle. Off by default. |
| A8 | **Partly.** `workdir` must sit under `hub.workdir_roots` (refused `workdir_outside_roots` before the executor starts; unset roots mean unrestricted until a node opts in). Not done: restricting gate checks to named checks. ADR 0014's check library is PROPOSED and does not exist, so there is nothing to name checks against; the raw-command opt-in would be a flag with no library behind it. |
| C3 | Done. New `hub` connection kind (url, actor, secret; validated live against the plane) and `identity.hub`. Under a declared identity the hub secret comes from that connection only, never `AGENTCO_HUB_SECRET`; a connection made for another url or actor is refused `identity_hub_mismatch`. |
| F1 | Done for the runtime's own prompts and messages (executor, doctor, schedules, rca, orchestrator and the rest). |
| Config discovery | Done (not in the original scope): `AGENTIC_CO_CONFIG`, then an upward search for `config.yaml`. |
| E1-E3, D3 | **Deferred to 1.1.** |
| B3-B6 | Untouched; they wait on hub 1.1. |

## Not in 1.0

- Reading the hub's change feed (`GET /events`). Later.
- Usage and cost reporting to the hub. Not planned on either side.
- Safety-classifier registries. The owner decides; the default is out.
- OIDC and signed attestations across organisations (roadmap "Later").

## Boundary map: runtime-side corrections

Checked against the hub session's draft map of 2026-11-13.

- **Gate evidence:** "never calls attest" is not quite right. A `done` report from a deterministic
  gate carries the attestation inline (`HubClient.attestation_for`). The separate
  `attest()` method exists and nothing calls it. The rest of that row stands: the list-of-checks
  shape and the parked judged and human gates (A5).
- **Report the outcome / take work / trust:** confirmed as written (A2, A3, A8).
- **Outbox, proposals, digest pinning:** merged, not in review (#24, #25, #26, #27).
- **Intake:** reporting back is automatic; only pulling is manual (A7).
- **One more overlap:** request signing. `hub_client.sign` duplicates the hub's `auth.sign`.
  It should go with B3: one signed client, from the hub package.
- **One more gap:** the hub credential is outside the per-instance identity (C3).

## Answers to the three open questions

**Q1. The person's answer signer exists in both repos. Which one stays?**
The hub's. Answering a gate is a person's act under ADR 0008, not work the runtime runs. The hub
owns the envelope contract and its vectors, and its client package installs without the server.
The runtime's `partition.py` has one thing the hub client lacks: the ledger that confirms a held
answer by digest (ADR 0008 §8 S4). Port that to the hub client, then retire the runtime's
module and its `partition` command. Until then, the runtime's copy keeps testing against the
hub's published vectors.

**Q2. Who runs the outbox drainer on a runtime-only node?**
The runtime runs it and the hub ships it (B3). The runtime takes a dependency on the hub's client
package and calls its drainer once per cycle. One signing implementation and one review intake (hub
1.1 item 2) stay true only if nothing else writes to the hub. The cost is that the hub's base
package must stay light: `asop-spec` plus the standard library. The hub repo would need to
commit to that.

**Q3. Is the embedded local store for standalone work only?**
Yes, and per item, as ADR 0012 says, not per install. A connected runtime can still own work filed
on it. For hub work it keeps a journal (A1).
