# 0001 — A node either ends or it does not

**Status:** accepted · **Date:** 2026-09-06

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

A node is the runtime's unit of place: a queue, a cadence, a heartbeat, and a
registry of children. The registry row records a name, a path, an interval, a
notify flag, a weight, an integration `type` (`beads` / `ado-backed` /
`vault-only`), a vault path, a host and a capability list.

What it does not record is **what kind of thing the node is**, and the word the
codebase reaches for is wrong. `add-company` configures any child, so a
personal ledger and a business are both "companies". A realistic portfolio has
several kinds of child sitting as flat siblings — businesses, a bounded piece of
work with an end date, an ongoing responsibility that has none, a content
pipeline — and the registry cannot tell them apart.

The distinction matters because two of them cannot be measured the same way. A
business is healthy when its cadence holds. A bounded piece of work is healthy
when it is moving toward closing. Nothing today can tell which question to ask,
so it asks neither.

## Alternatives

**(a) Company and project, two kinds.** Matches how most people already talk
about it. Fails on its own members: filing a personal ledger as a "company" is
wrong in plain English, and a field that feels wrong is a field that stops being
maintained.

**(b) Company, project, area — three kinds.** Tidier as a taxonomy, worse as a
system. Nothing behaves differently between a company, an area and a pipeline;
all three are measured by cadence. A kind that no code branches on is a question
with no correct answer, which gets answered differently on different days.

**(c) Project or operation, named for the property.**

## Decision

**(c).** Two kinds, and the split is whether the thing can ever be finished.

| kind | ends? | healthy when | example |
|---|---|---|---|
| `project` | yes | it is moving toward closing | a marketing campaign, a migration, AgentCo itself |
| `operation` | no | its cadence is holding | a business, an ongoing responsibility, a content pipeline |

Four rules follow, and each already has machinery.

1. **Operations hold projects.** An operation's queue carries two populations:
   its own recurring work, which never closes, and the goal beads of its
   projects, which do. Containment is *permitted, not required* — an operation
   with no projects in it is a complete answer, not a gap.

2. **Operations hold operations, and nothing new is needed for it.** The child
   registry is per node, so nesting is the file layout. A portfolio node
   containing businesses is this, and it already works.

3. **A project needs no parent.** A standalone project with no operation above
   it is a first-class case, not an exception.

4. **Promotion changes the kind and keeps the identity.** A project that stops
   having an ending becomes an operation by changing one field on the existing
   row — same name, same path, same store, same history. Filing a second
   identity would split its outcomes in two and make "how has this done over
   time" unanswerable, which is the same reason an ASOP version is superseded
   rather than overwritten.

`company`, `area` and `pipeline` survive as a **descriptive label** on an
operation. They carry no behaviour, which is precisely why they must not be
kinds.

The field cannot be called `type`: that name is taken and means whether a child
is pollable.

### Why a never-closing project is not a naming wart

Anything that measures progress measures closure. A review that reports what
closed, or a rule that retires work which has held a slot too many times without
finishing, will treat a thing that cannot close as a thing that is failing —
and eventually instruct its owner to kill an ongoing responsibility. The two
kinds exist so that measurement asks each thing the question it can actually
answer.

## Revisit when

- A third question appears that neither *is it closing* nor *is its cadence
  holding* can answer. Until then, a third kind is taxonomy, not design.
- Or an operation needs to close for real — a business is sold or shut down.
  Today that is a status, not a kind; if it turns out to need lifecycle of its
  own, this record is what to reopen.
