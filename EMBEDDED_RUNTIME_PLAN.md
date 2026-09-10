# ASOP embedded runtime — architecture and implementation plan

Status: draft pending independent reviews. Analysis only; no product implementation, release, deployment, or push.

## Goal

“Ok, Leeloo, now based on this analysis, and the existing(latest versions) of the Agentic-Co Hub and Harness, what would you change and also planout the embeded run time, cross validate with claude code and agy”

## Baseline and evidence limits

Inspected current GitHub main snapshots on 2026-09-09:

- Hub: `ca5640958f3d267e2828b634a8dcc9c9d670e4fb`.
- Harness: `c00dee5a5fb9e9f2823382ad9b8c3f21e041569c`.
- ASOP: `d7fb2ffe398822936ad4e29a3d6322b0237eda9a`.
- Also inspected the clean local Harness at `6194e97c67cfff7f2c91af173a9897fa4ca26026`, two commits ahead of that main snapshot. Its judged-gate parking and same-name self-approval rejection count as existing work.

Findings below are source observations, not reproduced runtime incidents. Existing test files were inspected selectively; product test suites were not run in this planning pass. Proposed acceptance probes are future implementation gates, not claims that the current products pass them. No production/deployed version was inferred from a Git commit.

## Recommendation

Develop the embedded runtime as a shared lifecycle component extracted incrementally from existing behavior. Keep the ASOP specification portable, Hub responsible for connected coordination, and Harness responsible for execution. Avoid building a third independent state machine.

The existing products are Python. The lowest-risk reuse path is an extracted lifecycle library in their current implementation language. Your TypeScript/Bun rule applies to new host-facing integration: expose a typed client using a host-managed local adapter to the existing engine initially. That is a local embedded deployment, but not an in-process TypeScript library. A truly in-process TypeScript engine is a separate port and replacement decision; it must replace lifecycle ownership in both consumers, not coexist as a third authority. No language migration is authorized by this plan.

Do not ship a new runtime package first and then hunt for reuse. Establish a narrow boundary inside the Harness/Hub, prove equivalent behavior, and only then extract its distribution.

## What changes, grounded in current code

| Area | Current source evidence | Proposed change |
|---|---|---|
| Contract adoption | Both `uv.lock` files lock `asop-spec` at 0.1.0; both manifests accept `>=0.1.0`. ASOP main's package metadata still says 0.2.0 while its changelog announces 0.3.0. | Reconcile the release metadata, publish/test the intended artifact, update both consumers and fixtures together, and record the contract version per run. Do not assume a source merge upgraded consumers. |
| Local judged gates | Public `beads.py:1552` rejects them. Local `6194e97` parks them and rejects same-name approval. Local approval still accepts an approver string and records no structured verdict. CLI `tasks_approve_verify` accepts `--approver`. | Keep the local parking fix; replace name-only approval with a validated principal and a structured judgment. Dispatch a distinct judge with the pinned rubric and actual artifacts. |
| Hub verifier authority | `policy.py:99` strips verification only when the declared set is nonempty. `work.py:1910` calls it on attest. Human attest checks the named verifier, but the full human-authority policy also needs explicit enforcement. | Empty declarations grant no authority. Separate authentication, role authorization, and execution/judgment separation. Apply the same decision at every transport and store mutation boundary. |
| Filing | Harness `asop_store.py:369-483` creates parent and children incrementally, resolves nested references during filing, then attaches all parent blockers. Hub `sop.py:847` prevalidates a plan but `sop.py:998` still files nodes separately. | Compile and validate the entire pinned tree, then publish it atomically. A crash or missing nested input must never expose a runnable partial tree. |
| Execution context | Harness stores step instructions in `metadata.step` and inputs on the parent; ordinary Claude/agy prompt paths in `orchestrator.py:899,930` use title/description. Hub mirrors render `sop_plan`, but do not assemble a complete run-input/predecessor-artifact envelope. | One context assembler supplies pinned step text, concrete run inputs, predecessor output references, gate/rubric, and local workspace mapping to every backend. No requirement that an agent guess which parent to inspect. |
| Stage evidence | `hub_client.py:attestation_for` can put the whole checks list into `check`. Hub `_gate_outcome` and `Queue.attest` decide completion using one `attestation_passes(record)` call. | Store one evidence record per stage and current verification generation. Complete only when every required stage passes. A passing rung must not release the whole gate. |
| Connected reporting | Harness `report_back` only reports DONE/FAILED; `sync` ignores AWAITING_VERIFY and VERIFY_FAILED. `mirror` returns an existing bead without refreshing a reissued lease. | Separate execution-finished from gate-passed and report both explicitly. Record authoritative Hub receipts. Fence mirror state by Hub, item, and attempt. Use durable idempotent delivery. |
| Completion semantics | Several backends call `beads.complete` and immediately return True; the returned bead may be parked or failed verification. | Return structured execution outcomes rather than a completed boolean. Completion hooks and outcome counts use the committed lifecycle state. |
| Learning | Hub has plan-versus-actual, adjudication, proposals, outcomes, and promotion. Local ASOPStore exposes authoring, filing, and drift, without equivalent learning operations. Generic Harness review/RCA is not automatically ASOP adjudication. | Share the learning record model and services. Keep gate judgment distinct from judgment about whether a departure improved the procedure. |
| Storage | Hub already has JSONL and SQLite queue/library implementations. SQL inherits lifecycle behavior from Queue. | Reuse transaction/CAS lessons and storage adapters. SQLite is the new embedded reference store; JSONL remains an import/export and explicit compatibility option. Avoid an immediate destructive migration. |

## Runtime boundaries

### Standalone

The host owns scheduling and calls the runtime. The runtime owns the run's state, evidence, and allowed transitions. Execution adapters perform work and checks outside database transactions. Their results are committed only if the attempt and artifact/gate generation still match.

### Connected

The Hub is the only authority for that run. The Harness owns a local execution journal, not another authoritative run. Journal entries hold attempts, process/artifact references, evidence, and delivery receipts; they are not ordinary Task objects allowed to execute local run-completion hooks or release blockers. Hub downtime does not prevent unrelated standalone work. For Hub-owned work, a lost response or lease does not authorize local downstream release or a new execution. Preserve evidence and retry delivery; uncertain external side effects require reconciliation before retry.

An in-flight run cannot switch authority merely because a connection fails. Explicit migration at a safe boundary would be a later feature.

### Components

1. **Contract layer:** versioned schemas, refusal codes, stateless validation, semantic conformance scenarios. No vendors or storage dependency.
2. **Run compiler:** resolves nested pins, inputs, role bindings and gate routes, validates the entire DAG, creates an immutable run plan.
3. **Lifecycle service:** claim, renew/expire, report execution, submit evidence, resolve clocks, repair, adjudicate, close. No generic public “set done” operation for ASOP work.
4. **Identity policy:** authenticated principal, declared human/verifier/adjudicator authority, executor route identity, per-run bindings. A backend label is not a credential.
5. **Context and artifact service:** deterministic step envelope and immutable output references. Local adapter resolves artifacts to paths; the portable ASOP does not carry machine-specific paths.
6. **Evidence/judgment service:** stage-aware records bound to the work revision; independent judge requests with fixed rubrics; preserved negative evidence.
7. **Persistence adapter:** transaction, compare-and-swap, append evidence/events, idempotency receipts, transactional outbox.
8. **Executor adapters:** reuse Harness backend implementations; structured outcomes replace backend-owned lifecycle mutation. The host retains scheduling, cost, egress policy, notification, and vendor choices.
9. **Learning service:** plan-versus-actual records, separately authorized adjudications, proposal drafts, per-version outcomes.

## Data and operation contract

Suggested logical entities (not a mandated table layout): procedure versions, run plans, step instances, execution attempts, artifact revisions, evidence, judgments, adjudications, proposals, delivery receipts, and outbox events.

Evidence scope must identify the owner/run/step, attempt, output revision, gate/rubric revision, and stage where applicable. This requires a versioned submission envelope or a contract extension: adding arbitrary fields to today's strict attestation object is not compatible. The authenticated actor is transport-derived. A judge can issue negative evidence; a negative verdict is valid and does not complete work.

Candidate host API: create/revise/activate procedure; start/read run; get ready work; claim work; report execution; submit gate evidence; approve/reject as authorized human; request repair; adjudicate; draft proposal; read events and outcomes. Stdio or local RPC for a TypeScript host is an encoding of these operations, not another workflow engine. Every mutating call carries an idempotency key and explicit expected revision where applicable.

Long-running checks use snapshot → execute outside lock → conditional commit. Recheck authorization at commit, not only at dispatch. Gate commands receive a declared environment and cannot write authoritative state; safety requires process/credential isolation, not just an API convention.

## Delivery plan and falsifiers

| Milestone | Scope and result | Acceptance probes that can falsify success |
|---|---|---|
| 0. Freeze contract decisions and distribution | Reconcile ASOP release metadata; document authority, timeout, activation, and evidence-envelope semantics; update both lockfiles/fixtures. | Fresh isolated installs resolve the intended artifact. Both consumers run verdict and stage conformance fixtures. Old payloads do not silently gain v3.2 approval authority. |
| 1. Correct current execution paths | Deliver authenticated judged/human evidence, full step context, stage aggregation, structured completion outcomes, and connected reporting/lease refresh fixes. | A renamed executor cannot approve; empty registry refuses; missing/negative verdict cannot complete; a two-stage gate stays blocked after stage 0; Claude/agy receive the same pinned inputs and predecessor outputs; parked and failed checks reach Hub. |
| 2. Establish lifecycle boundary and atomic store | Introduce runtime interface around existing behavior, compile full run plans, implement atomic tree publication and transactional evidence/receipts. | Kill filing after each write boundary: zero runnable partial runs. Invalid nested procedure leaves no run. Concurrent claims yield one owner. Expired attempts cannot commit. Crash between state update and delivery leaves a replayable outbox entry. |
| 3. Deliver standalone embedded slice | Host starts a runtime with persistent store and existing executor adapters. Complete a bounded procedure with deterministic, judged, and human gates, repair, and restart recovery. | No Hub process/network required. Process death does not lose the run. Failed originals stay blocked after repair siblings finish. Second gate failure escalates. Every backend returns the actual committed status. |
| 4. Reuse in connected operation | Hub and standalone adapters share lifecycle rules; mirrors become journals; delivery deduplicates; receipts reconcile uncertain responses. | Lost HTTP response does not duplicate a transition. Reissued lease invalidates old local attempts. Old artifact evidence cannot pass current work. Hub outage never creates a second owner. Same operation trace produces equivalent statuses/refusals under both adapters. |
| 5. Complete the improvement loop | Local and connected plan/actual, adjudication, proposals and outcomes share semantics. | Executor self-adjudication refuses; replay does not double-count outcomes; a proposal cites its originating run/step evidence; activation obeys the selected policy; in-flight runs retain their pins after a new activation. |
| 6. Package and migrate | Extract the proven boundary, add the TypeScript host client if needed, publish migration tooling/docs. | Dry-run importer preserves every version/pin/dependency/evidence item and reports corrupt records; migrated data is never dual-written; rollback reads a snapshot only after accounting for post-cutover writes. |

First end-to-end procedure: a small repository change with implementer, independent validator, and human publication approval, using a deterministic check ladder. Publication remains a separate action after approval; a completion gate cannot undo an action already executed. External side-effect idempotency belongs to the action adapter and must not be confused with report deduplication.

## Decisions needed before implementation

- **Activation:** section 8.1 permits policy-bound agents; section 11 says humans activate. Default recommendation: human activation for this delivery, documented as a selected policy until the specification is reconciled.
- **Timeout:** the spec allows `on_timeout: pass`. Do not silently remove it or claim universal positive-evidence completion while retaining it. Recommend prohibiting automatic pass for protected/irreversible gates; document default resolution separately from verified completion.
- **Authority:** distinguish executing checks in the worker domain from accepting evidence and changing authoritative Hub-owned status.
- **Nesting and ordering:** the spec's `uses` body shape and existing schemas need alignment; the current reference validator explicitly permits only dependencies on lower-numbered steps (`asop/sop.py:608`), whereas the prose describes cycle/dangling-reference validation more generally. Preserve the implemented restriction until the contract decides otherwise. Do not call forward-reference handling a demonstrated production bug under that restriction.
- **Language/embedding:** prefer lifecycle reuse over a rewrite. A TypeScript client to a host-managed Python engine is not in-process TypeScript embedding. Choose a true port only if that distinction matters to the intended host.
- **Identity trust:** a process that owns the database and all judge credentials can bypass application checks. State the operator trust boundary and keep worker/judge credentials and state-write privileges separated.

## Migration guardrails

Keep existing runs on their recorded contract/engine version until completion or an explicit migration. Preserve historical evidence honestly; never fabricate verdicts for old approvals. Do not silently reinterpret finished historical runs as newly verified. Refuse or park incompatible new submissions with a migration message. Introduce strengthened behavior for new runs first, then migrate unfinished runs with operator review where evidence is insufficient.

Only one authoritative writer after cutover. Shadow mode computes decisions without writing them, comparing current and candidate transition traces. Backups alone are not rollback after new writes: rollback needs compatible event replay/export or a write freeze and reconciliation.

## Independent review

agy (Gemini 3.1 Pro High) returned a review and a reconciliation response. Adopted: strict non-authoritative execution journal, shared lifecycle semantics, stage-aware wire evidence and Hub aggregation. Corrected after source checks: local stage execution already exists; worker-side deterministic checks are permitted; local ASOP learning is missing rather than a demonstrated exploitable adjudication implementation; judged parking belongs to local 6194e97 rather than public main. agy initially preferred a large extraction and startup conversion; after challenge it accepted targeted fixes and shadow equivalence first. Essential probe: one passing stage never releases a multistage gate. Review output: agy.json and agy-followup.json in this plan directory.

Claude Code (Opus, 2026-09-09) independently confirmed the highest-risk findings: `gate_satisfied` is defined in the contract but imported only by the conformance runner/tests; Hub and Harness therefore decide completion using a single `attestation_passes` result. It also found the public Harness still refuses judged gates, `approve_verify` remains caller-string based in that snapshot, Harness run filing is incremental, the Harness reaper does not advance its lease attempt, local/connected record shapes differ, local learning verbs are absent, and Hub verifier declarations fail open when unset. Claude identified the strongest design objection: “shared semantics” has already failed because the shared package stops at document validation rather than authoritative state transitions. I adopt the remedy: put transition decisions, fail-closed principal resolution, and complete run compilation in pure shared modules, then require both products to call those modules at their status-flip boundaries. I retain the incremental rollout: first add failing transition vectors and targeted fixes, then extract behind shadow equivalence, preserving active runs. Claude’s review output is `claude.json` in this plan directory.

Consensus is not a substitute for evidence; each review claim above was checked against the cited source before adoption.
