# F8(ii) — delegation patterns as operator doctrine (not runtime code)

**ISC:** `lifeos-exit/ISA.md` ISC-8, half (ii). **Status: DRAFTED** — doctrine document only,
generalized. No machine names, repo names, vendor keys, dollar figures, or the principal's
project names appear below; every example is described by role/shape rather than by identity.

Source read (not quoted verbatim): `~/.claude/LIFEOS/USER/CONFIG/OPERATIONAL_RULES.md`'s "Work &
task routing" and "Delegation token economics" sections. Cross-checked against what's already
mechanical code in this repo, so this document doesn't re-claim as "doctrine" what's already
enforced as a rule.

## Why doctrine, not code — the load-bearing reason

Every pattern below currently fires because a human operator directs a Claude Code session and
that session reads a prose file. None of it is enforced by a rule engine; it holds only as long as
whoever is orchestrating happens to have read it and act on it. That's exactly the property ISC-8
calls out as disqualifying for porting-as-code: *these patterns are not currently mechanically
enforced by anything host-specific — they're prose a model reads* — so "porting" them would mean
writing a **new** rule engine, indistinguishable in kind from `f8-identity-policy-design.md`'s
registries, for the minority of this doctrine that actually is mechanically checkable (lane
overlap, tier assignment, decomposition caps). The rest — judgment calls like "do these two tasks
share enough mental model to consolidate" — has no mechanical check today and doesn't get one by
being copied into a config file; a human or a model still applies it, the same way it's applied
now. The doctrine format matches that reality instead of pretending otherwise.

**What's already code, not doctrine, in this repo** — cited so this document doesn't re-litigate
settled ground:

- Decomposition caps (max children per task, max nesting depth) are mechanical and already
  enforced in `agentco_harness/beads.py` (`MAX_SUBTASKS_PER_TASK`, `MAX_SUBTASK_DEPTH`).
- Cost/capability-tier-to-backend mapping is mechanical and already has a home:
  `agentco_harness/config.py`'s `TiersConfig`/`DEFAULT_TIERS`, resolved per task via
  `metadata.executor_tier`.
- Per-task-class time/retry budgets are mechanical and already live in
  `agentco_harness/orchestrator.py`'s `TASK_CLASS_BUDGETS`.

The doctrine below is everything *around* those mechanisms: when to reach for them, and the
judgment calls no config value captures.

## Roles

- **Operator** — the person directing work and accountable for outcomes.
- **Orchestrator** — the process (a session, a scheduler, a script) that decomposes work and
  dispatches it to execution routes.
- **Execution route** — whatever actually performs one unit of work: a model at some
  cost/capability tier, or a human assigned directly. "Backend" and "vendor" are properties of an
  execution route, not a different category of thing.

## Lane discipline — concurrent routes touching one repository

Two or more execution routes writing into the same repository at the same time need one of:
non-overlapping file lanes, serialization, or isolated worktrees per route. This holds regardless
of what executes each route — the failure mode is two writers silently clobbering the same file,
which is a property of concurrent mutable state, not of any particular backend. A single
orchestrator directing several routes into one repo in one pass must assign each route a disjoint
set of files (or paths) before dispatch, not discover the overlap after the fact.

Corollary: no repository-wide operation (a rewrite touching most of the tree, a bulk rename) runs
while other routes are active against that repository. Serialize the wide operation, or hold other
routes, but don't run both at once.

## Model-tier discipline — match capability cost to task shape

Not every unit of work needs the orchestrating session's own capability tier:

- **Mechanical/lookup work** (a file survey, a log grep, a status poll, extract-with-quote) goes
  to the cheapest capable execution route.
- **Judgment inside one lane** (implement, review, investigate a bounded question) goes to a
  mid-tier route.
- **Cross-cutting synthesis** — reconciling several routes' output, a final decision — stays with
  the orchestrating session's own tier. Synthesis is where under-provisioning actually costs
  correctness; the earlier stages are where over-provisioning wastes capacity for no quality gain.

Width is not automatically expensive. A broad fan-out of independent, shallow lookups is cheap
*if* each one runs at the cheap tier appropriate to its shape; what's expensive is depth run at the
wrong tier, repeated across many routes. Escalate a route past its assigned tier only for a stated
reason (adversarial review, cross-implementation audit, unusually high blast radius) — not as a
default.

Concrete tier-to-backend bindings are a per-deployment configuration concern
(`TiersConfig`/`DEFAULT_TIERS` above); this doctrine states the assignment principle, not which
named backend fills which tier in any particular deployment.

## Re-orientation is the hidden tax — one handoff artifact per work front

Every fresh execution route that has to re-derive context (read the design doc, the history, the
current state of a multi-step effort) from scratch pays that cost again, and it compounds across
however many routes touch the same front. The mitigation: maintain **one handoff artifact per
distinct work front** — current state, decisions made, decisions still open — that a new route
reads *instead of* re-deriving. Keep it current as the front moves; treat a stale handoff artifact
as worse than none, because it's actively misleading rather than merely absent.

Corollary — **resume, don't respawn.** Continuing an existing execution route's session preserves
everything it already loaded; starting a brand-new one pays full re-orientation cost even for a
one-line follow-up. Default to resuming; start fresh only when the existing route's context is
itself the problem (it's stuck, or the task has genuinely changed shape).

## Consolidation — merge what shares a mental model

A rule of thumb, not a hard cap: keep the number of concurrently active execution routes small
enough that the orchestrator can actually collect and read every result (in practice, on the order
of a handful at a time); beyond that, consolidate. The stronger signal for *when* to consolidate
two tasks specifically is not headcount but whether they share a mental model — two tasks that
require loading the same context to reason about are cheaper done by one route sequentially than
by two routes each re-deriving that context independently, even if both would otherwise fit
comfortably within a "small enough" fan-out.

An orchestrator that fans work out must actively collect results as they land — dispatching many
routes and then waiting passively is a failure of the orchestrator role, not a property of having
dispatched many routes. Width scales fine; passive orchestration does not.

## Authorization gates — where doctrine hands off to identity-policy

Three categories of action always require operator confirmation before an execution route
proceeds, regardless of tier or role: anything touching a production/live/customer-facing system,
anything that spends or commits money, and anything a third party outside the operator's own
authority will see or receive (a message, a post, a shared-document change). Everything else —
reading, local analysis, local builds and tests, staging changes, filing or closing tracked work —
proceeds without a confirmation round-trip, reported after the fact rather than gated before it.

This is doctrine about *when* a human must be asked, which is exactly the boundary
`f8-identity-policy-design.md`'s identity-policy component exists to enforce mechanically once an
execution route reaches an action of one of these three shapes ("declared human/verifier/
adjudicator authority" in `EMBEDDED_RUNTIME_PLAN.md`'s component list). The two documents describe
the same authorization boundary from two sides: this one is the human-readable rule an operator
states; the identity-policy design is the mechanical check a system applies at the point of
action. Neither replaces the other — the doctrine explains *why* the gate exists and where its
edges are; the mechanism is what actually stops an unattended execution route from crossing it
without a human present to ask.
