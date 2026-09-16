# Roadmap

**What this is.** A coordination layer for organisations running more than one agentic
harness, and a runtime that executes work against written procedures with gates that can
refuse. Three pieces, deliberately separable:

| piece | repo | what it owns |
|---|---|---|
| **The spec** | the `asop-spec` package | Gate kinds, refusal codes, attestation shape, revision policy. Arrives **by version, never by path** — implementations adopt a version, they do not fork semantics. *(That rule applies to this table too: a repository URL is a path, so the spec is named by package rather than linked.)* |
| **The runtime** | `agentic-co-harness` (this repo) | Executes work: lifecycle, gates, executor backends, egress policy, failure analysis, scheduling, human delegation. |
| **The plane** | [`agentic-co-hub`](https://github.com/agentic-co/agentic-co-hub) | Coordination across independently-owned harnesses: claims, leases, scopes, procedure versioning, adjudication, events. |

Status below is **measured, not remembered**. Every figure here comes from running the
thing, and where a claim is not executable it is marked as belief.

---

## Read this first — the evidence is not flattering

This project's central claim is that a procedure with enforced gates produces better
outcomes than the same instructions as prose. **Measured across two domains, it does not.**

Every arm measured so far loses to bare prose. Figures are **paired against a matched prose
arm**, quoted as recorded rather than derived:

| domain | procedure version | pass^1 | paired delta | p |
|---|---|---|---|---|
| airline | v4 | 0.500 (13/26) | −0.192 | 0.180 |
| airline | v1 | 0.440 (11/25) | −0.240 | 0.070 |
| airline | v5 | 0.360 (9/25) | −0.320 | **0.021** |
| retail | v3 | 0.861 (31/36) | −0.028 | not significant |
| retail | v2 | 0.806 (29/36) | −0.083 | 0.375 |

The direction is consistent across both domains; the magnitude differs by roughly 3×, and
explaining that gap is the open question. Only one arm clears p < 0.05 and it is the **worst**
one — significant in the wrong direction.

**The arms are too small to settle it either way.** At the observed rate of disagreement, 36
cells cannot reach p < 0.05 whichever way a result points. That is a property of the design,
not of the outcome, and it is being fixed by scaling the sample rather than argued around.

Two further results, both negative, both worth stating because they are the kind that
usually goes unpublished:

- **"A small model plus a procedure matches a large model bare" is refuted**, twice.
  0.417 vs 0.889 in one domain; 0.077 vs 0.692 in the other. Not close.
- **The composite gate currently refuses every correct completion.** On the best-performing
  procedure, 31 of 31 correct runs had at least one gate refuse them. Per-gate false-refusal
  is ~50%, and ~11 gates per run compounds that to a near-certainty. **More gates is not
  free**, and nothing in the authoring guidance says so.

The roadmap below is shaped by that. Work that assumes the mechanism already works is
deprioritised; work that would tell us whether it can work is not.

---

## What works today

### The spec — `asop`
- Gate kinds, refusal-code vocabulary (60+ codes), attestation schema, revision policy.
- **61/61 conformance vectors** matching across implementations.
- Versioned adoption: an implementation names a version; nothing forks locally.

### The runtime
| capability | state |
|---|---|
| Work lifecycle with a **single choke point** — every route to any terminal state passes one gate | ✅ |
| Gate kinds: deterministic (re-run a tool check), judged, human | ✅ |
| **Executor backends** — 7 registered across 5 vendors, one interface | ✅ |
| **Egress policy** — data-classification ceilings per vendor route, fail-closed, machine refusal codes proven identical across two adapters | ✅ |
| Lease/claim protocol with attempt fencing and idempotency keys | ✅ |
| Automated failure analysis with bounded retry and escalation | ✅ |
| Recurring work, schedules, natural-key deduplication | ✅ |
| Human delegation: assignment, decline, snooze, approval gates | ✅ |
| Cost and usage accounting per run | ✅ |
| Health checks (`doctor`) over queue dispatchability and policy coverage | ✅ |
| Extension seams — cycle handlers, completion hooks, source factories | ✅ |
| **Static procedure analysis** — reachability (`gate_reach`) and discrimination (`gate_probe`) answered from the document in one second, without spending an executor | ✅ |

### The plane
| capability | state |
|---|---|
| **17 verbs** over one semantic core | ✅ |
| **4 transports** — HTTP, MCP, MCP-remote, outbox — with a conformance suite proving they agree (14 scenarios × 4 transports) | ✅ |
| Lease protocol **proven across 12 OS processes**, verified by mutation | ✅ |
| Derived blockedness proven cross-process | ✅ |
| Scope claims, snapshots (never storing a document body — asserted against raw bytes) | ✅ |
| Procedure library with versioning, supersession, retirement | ✅ |
| Adjudication with executor/adjudicator separation | ✅ |
| Events, outbox, change feed | ✅ |
| Write-back connector — **notice only, never a state change**, off by default | ✅ |
| Identity via HMAC | ✅ |

---

## Next — the current goals

Small, independently shippable, each with one acceptance test that fails before the work.

| # | goal | done means |
|---|---|---|
| **1** | **Backend-agnostic enforcement rules** | Rules that today live as host-specific hooks become policy the runtime enforces, producing an **identical refusal code** under two different vendor adapters. First rule shipped; the rest follow. |
| **2** | Structured attestation everywhere | Approval stops accepting a bare name string and takes a validated principal plus a structured verdict. |
| **3** | **The gate stops refusing correct work** | Per-gate false-refusal under a stated budget — or the finding characterised well enough to change how procedures are authored. |
| **4** | A decisive experiment that can refute | Run the claim where the gate is a **real oracle** (code, where tests decide), with ground truth from independent hidden tests and a patch class that is requirement-violating *but passes the public tests*. |
| **5** | Observability on one stream | Tool-activity history queryable through the evidence/outbox API, not a second log format. |
| **6** | Capabilities as versioned records | A reusable capability expressed as a versioned record a run can pin. |
| **7** | Dependencies in the protocol | Dependency edges expressed in the plane's vocabulary, or a recorded reason they cannot be. |

---

## Later — bigger functionality

- **Identity beyond HMAC** (OIDC) for organisations that want it.
- **Change-feed subscriptions** with webhook authentication.
- **The routing spine**: named owners, shared queues, offers with claim deadlines, explicit
  defer reasons, escalation, and a terminal undeliverable state. Capacity modelling ships as
  **measurement first** — observed and published — and throttles nothing until there is real
  data to throttle on.
- **Signed attestations**, without which cross-organisation attestation is not yet real.
- **Procedure evolution**: revise a procedure from gate evidence and measure whether the
  revision is better. The mechanism now exists (probe a gate's check against constructed
  inputs, offline, with no executor); the claim is unmeasured.

---

## Not planned

**Writing to your system of record**, beyond one narrow path built to the constraint this
section has always stated: separately gated, off by default, and append-only. The write-back
connector notifies an originating record that a human gate is parked. It is a notice, never
a state change, and it does nothing until an operator configures a destination.

The whole proposition is that adopting this cannot damage the system you already trust. A
notification path that can only append is the largest exception that leaves that true.

---

## Proven vs tested — a distinction worth keeping

**Proven under adversarial conditions:** the lease protocol across twelve real OS processes,
verified by *mutation* (remove the lock, the storm test fails; disable the fence, the
stale-holder test fails — a test that cannot fail when the mechanism is removed proves
nothing). Snapshots never storing a document body, asserted against the database's raw bytes.
A second party instructed to refute rather than confirm produced 24 findings across six
load-bearing claims; **five of the six did not survive**.

**Tested but not proven:** the rest. Most suites are hermetic, single-process, and written by
the same author as the code. The protocol has grown faster than its proof — attempt-advance
on report and reap, the reaper's in-lock liveness re-check, and agent enforcement are
single-process tested only.

**A recurring failure mode, named so it stops recurring:** a claim about the state of the code,
written once, believed thereafter, never re-run. It has happened at least seven times in this
project's history. The counter-measure is making claims executable — a property of a document
should be checked *from the document*, and an invariant should be a test that enumerates
rather than a sentence that asserts.

---

## How we would know this failed

The falsification criterion, written before the result is known:

> **Two identities other than the author publishing weekly, for four consecutive weeks.**

Not "have tried it" — that measures politeness. A missed week resets the streak rather than
bridging it. If that fails, the honest reading is that coordination across independently-owned
harnesses is not a problem other people have, and this stops at the primitives that are useful
on their own.

Stars are the metric that will be available. They are not the one being used.
