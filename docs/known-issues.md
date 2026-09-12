# Known issues

Everything here is known, reproduced, and deliberately not yet fixed — recorded
per `SECURITY.md`'s "what we do with reports" so an external reporter can see it
is not news before spending time rediscovering it.

Unlike the [AgentCo Hub's known-issues.md](https://github.com/agentic-co/agentic-co-hub/blob/main/docs/known-issues.md),
nothing below has a failing test pinned to it yet — that convention is worth
adopting here as findings accumulate, but there is exactly one finding so far
and no `xfail(strict=True)` harness for it.

## The CLI mints an attestation from a self-supplied name

**Status: OPEN and KNOWN.**

Moving the attestation out of metadata (`agentco_harness/beads.py`,
`approve_verify`) closed the REMOTE path: a caller can no longer hand
`Beads.update()` a metadata dict claiming `verify_approval` and have it
believed, because the attestation now travels as an argument the gate itself
constructs.

The local CLI path still constructs that privileged `attestation=` kwarg from a
name the caller chose. `agentco_harness/cli.py`'s `tasks approve-verify`:

    who = approver or os.environ.get("USER") or "unknown"

and `approve_verify` checks `who` against the verifier set from
`declarations.verifiers()`, which reads `ASOP_VERIFIERS` — an environment
variable the same worker process controls (`agentco_harness/declarations.py`).
So the "authentication" is a worker asking itself, via its own environment,
whether the name it just typed is on a list it can also set.

**There IS a self-approval refusal** — `approve_verify` refuses an `approver`
equal to the bead's own recorded executor (ASOP.md §6.1: "a harness where the
only possible adjudicator is the executor's own route has no self-improvement
loop"). That closes the laziest version of this. The attack that remains is
naming a *different* declared verifier: `--approver someone-else`, with
`ASOP_VERIFIERS` set (by the same worker) to include that name. Nothing at this
layer distinguishes that from a real second party, because nothing about
`someone-else` is bound to anyone who is not this process.

**Fix direction, per the reviewer, not yet done.** The requirement is a secret
the worker does not hold. An attestation signed by a private key living off the
box, verified at the gate, defeats forgery without any new service and survives
full worker compromise — unlike a registry check, which a compromised worker
can also satisfy by declaring itself into it.
