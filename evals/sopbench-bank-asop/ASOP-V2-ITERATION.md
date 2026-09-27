# ASOP V2: the gate audits the reasoning instead of replacing it

## The result, before anything else

On **two domains it had never seen**, the final configuration beat baseline
significantly on both — `library` **+0.333** (p = 0.0000, 23 gained / 1 lost) and
`online_market` **+0.262** (p = 0.0000, 59 / 14). It **tied PVA on `library`**
(0.561 vs 0.561) and lost `online_market` by 0.064, where it beats PVA on four of
the five conjuncts of `success` and loses the fifth to a malformed-argument
defect in our own compiler worth up to 0.140.

**That is the first time this project's own ASOP apparatus has been competitive
with the scaffold it has been losing to, and it happened on held-out data.**

Every `bank` number below is a DEVELOPMENT number, iterated against that
domain's own failures, and none of the individual mechanisms is significant
there. The two readings are reconciled in §4b, and the experiment that would
settle them is §4c.

---

**Status: four `bank` arms plus both held-out domains, then a hard stop for
review. Every `bank` number on this page is a DEVELOPMENT number — the rounds
were iterated against `bank`'s own failures — and is labelled as such. The
result is §4, the held-out domains, which none of this has seen.**

⚠️ **Rounds 4 and 5 were built, tested and then NOT run.** `--cite-check` and
`--respect-or` exist, are covered by tests and are reproducible from §5, but the
principal stopped the programme for review after the held-out runs, and a
further development round on `bank` is worth less than a complete held-out
result. They are apparatus on this page, not arms. Nothing below quotes a number
for them.

The prior work ([`ASOP-PORT-MVP.md`](ASOP-PORT-MVP.md), N24–N28) ended in a
specific place. Five distinct gated configurations — liveness gate, value gate,
+ the task's own rules, + upfront enumeration, and the ungated document — left
`dirgraph_satisfied` (did the agent actually perform the required verifications,
with matching parameters) at **0.590–0.679** while SOPBench's own PVA scaffold
reaches **0.791** and `action_order` **0.843**. Gate correctness, information
parity and presentation order were each tested and each moved it by nothing.

Two things are being changed here, and they are separable:

| | what changes | why |
|---|---|---|
| **the document** | how the compiled ASOP is authored | a measured defect, §1 |
| **the architecture** | the gate moves to the other side of the reasoning | the principal's design decision, §3 |

The architecture change, stated by the principal: *"let the LLM do the task,
self-verify the task, and then have the gate validate that the LLM actually did
it and verify that it was correct. So it's a double check, not an initial
check."* Today's gate is a **barrier before** each step — the model never has to
articulate anything, which is the leading explanation for the flat metric.
Verification is a cognitive act, and a barrier outsources it to machinery. The
new gate is a **deterministic auditor of the model's own stated verdicts**: the
reasoning gets elicited (PVA's mechanism) *and* machine-checked (the thing PVA
has no way to do).

---

## 0. Before any round: the document's step order violates the domain's own action graph

**This was found by measuring, not by reading, and it is the largest single
thing on this page.** It cost no GPU: it is a re-scoring of trajectories that
already existed.

`dirgraph_satisfied` is a conjunct of SOPBench's `success`. It walks
`task["directed_action_graph"]` and requires that **before every tool call**,
that tool's own prerequisite subtree has already been satisfied by earlier calls
with matching parameters (`env/evaluator.py:dfscheck_called_functions`). One
mis-ordered verification call fails the whole task.

The v1 compiler emits a procedure's constraints in `required + customizable`
table order. For **5 of bank's 20 procedures that order is one the graph
forbids**, because bank's *verification helpers have preconditions of their
own*:

```
## get_safety_box
   compiled order: internal_check_username_exist -> authenticate_admin_password -> login_user
   ✗ authenticate_admin_password needs login_user first
## pay_bill
   compiled order: internal_check_username_exist -> get_account_balance -> login_user
   ✗ get_account_balance needs login_user first
## get_credit_cards, set_admin_password, open_account   (same shape)
```

An executor that obeys `Get Safety Box` as written fails `dirgraph` on its
second call, every time, by construction.

**Blamed against the published trajectories.** Re-scoring each arm with
SOPBench's own evaluator and recording which call first lacked its
prerequisites:

| arm | dirgraph (routable n=112) | first offending call, top shapes |
|---|---|---|
| `pva` | 0.791 | failures land on the FINAL action (`set_safety_box` 7, `transfer_funds` 4) — a genuinely missed check |
| `action_order` | 0.848 | same shape |
| `none` | 0.804* | `authenticate_admin_password` 10, `get_account_balance` 4 |
| `asop-prompt` | 0.652 | `authenticate_admin_password` 13, `get_account_balance` 8 |
| **`asop-gated-value-rules`** | **0.607** | **`authenticate_admin_password` 18, `get_account_balance` 15, `get_account_owed_balance` 5** |

\* the blame script scores only the routable subset and counts differently from
the headline table; the ordering is what matters, not the third decimal.

**38 of the gated arm's 44 dirgraph failures are one shape**: a tool called with
only `internal_check_username_exist` before it, when the graph required
`login_user` first. The document does not merely fail to help here — **it makes
the executor worse than no document at all** (18 vs baseline's 10 on the admin
step), because the executor follows it.

This is the kind of defect a compiled artifact is supposed to make impossible,
and the compiler's own self-check did not look for it. It does now: v2
stable-topologically sorts each procedure against the transitive closure of its
tools' prerequisites, and **a compile fails if the emitted order still violates
the graph**.

Two procedures cannot be fixed by sorting: `set_admin_password` needs a login
step its own constraint set does not carry, and `open_account`'s verification
helpers require a login the procedure has no business performing. Those are
**reported, not invented** — inserting a step the task never imposed is exactly
N24's defect, arriving for a third time.

### 0b. The order-only arm — is the whole gate programme downstream of this?

v2 moves the step ORDER and the PRESENTATION at once, so a v2 gain cannot be
attributed to either. `--variant v1-order` is **v1's document with only the
constraint steps topologically sorted**: identical step set, identical text,
identical gates and tools, verified by diffing the emitted markdown (three
procedures reorder — `Pay Bill`, `Get Safety Box`, `Get Credit Cards` — and
nothing else moves). The runtime configuration is `asop-gated-value-rules`'s,
unchanged.

So `v1-order` minus `asop-gated-value-rules` is the **order effect, alone**. It
is the single most informative number available here: if it recovers most of
v2's gain, then five configurations of gate-architecture work (N26, N27, N28)
were downstream of a compiler bug.

**Result (DEVELOPMENT number — `bank`, routable n=112).**

| arm | success | permissible | impermissible | calls | dirgraph |
|---|---|---|---|---|---|
| `asop-gated-value-rules` (the arm it differs from) | 0.545 | 0.450 | 0.597 | 3.02 | 0.607 |
| **`asop-v1-order`** | **0.562** | 0.475 | 0.611 | 3.04 | 0.625 |

**`v1-order` vs `-rules`: +0.018 [−0.025, +0.061], 4 gained / 2 lost, p=0.69.**
Correcting the order, alone, buys essentially nothing.

§0c explains why, and the explanation is not "the order did not matter": the
order fix can only help the 72 tasks narrowing left intact, and on those the arm
was already at `dirgraph` 0.917. The 40 deprived tasks are unreachable by any
ordering whatsoever. **So the answer to "was the whole gate programme downstream
of a compiler bug" is: no — it was downstream of a DIFFERENT one, upstream of
the document entirely.**

Run health: 1/134 hit the turn cap, 356 gates, 0 fell back, 0 ungated,
6 escalations.

---

## 0c. 🛑 THE CEILING — what Round 1 actually found, and it retires §0's hypothesis

**Round 1 falsified §0.** The V2 document sorts `Get Safety Box` into username →
login → admin-auth, and the executor **still** called
`authenticate_admin_password` with only `internal_check_username_exist` before
it — **20 times, up from 18**. Correcting the order did not remove the shape the
order was supposed to cause. So the corrected order was not reaching the
executor.

**It was not.** `make_task_asop_provider` — N24's information-parity fix — keeps
only the steps whose tool verifies a constraint *this task* imposes.
`dirgraph_satisfied` does not care what the task imposes: `env/evaluator.py` does
`default_dep_full[task["user_goal"]] = task["constraints"]`, which overrides the
prerequisites of the **goal action only**. Every other tool — every verification
helper — keeps the domain's defaults. `get_account_balance` always requires
`logged_in_user`, whatever the task says about `set_safety_box`.

So a task that imposes `authenticated_admin_password` but not `logged_in_user`
gets a narrowed procedure with the login step **deleted**, and then no ordering
can save it. Measured: **40 of 112 routable `bank` tasks (35.7%) have a
prerequisite of a kept helper deleted by narrowing** — 18 `set_safety_box`,
9 `transfer_funds`, 5 `get_loan`, 3 `open_account`, 3 `pay_loan`, 2 `pay_bill`.

**Split the metric on that, and the whole picture inverts:**

| arm | deprived (n=40) | intact (n=72) |
|---|---|---|
| `none` | 0.425 | 0.653 |
| `pva` | 0.500 | 0.972 |
| `asop-gated-value-rules` | 0.050 | 0.917 |
| **`asop-v2-doc`** | **0.000** | **0.986** |

1. **Where narrowing is harmless, the V2 gated arm reaches `dirgraph` 0.986 —
   above PVA's 0.972 and far above baseline's 0.653.** The mechanism works, and
   works better than the arm it has been losing to, wherever it is allowed to.
2. **Where narrowing deleted a helper's prerequisite, it scores 0.000.** Not
   low — zero. It cannot do otherwise: it obediently follows a procedure that
   omits the required step.
3. **The better an arm follows our document, the worse it does on the deprived
   third**: `none` 0.425 → `-rules` 0.050 → `v2-doc` 0.000. PVA scores 0.500
   there precisely *because* it does not follow our document.

The arithmetic closes exactly: `(0.000 × 40 + 0.986 × 72) / 112 = 0.634`, which
is the observed aggregate to three decimals. **The ceiling is 0.643 and the arm
reached 98.6% of what was reachable.**

🛑 **This retires "the metric never moves" as a finding about stepwise gating.**
`dirgraph_satisfied` sat at ~0.59–0.63 across five configurations because **the
information-parity fix capped it at 0.64**, not because gate correctness,
information or presentation could not move it. N26, N27 and N28 were each
measuring against a ceiling none of them could see. That is a correction to this
programme's central negative result, and it is bigger than anything V2's
document or gate does.

**What it does NOT say.** It does not say the ASOP wins: `success` is still
0.580 against PVA's 0.670, and the deprived third is a real cost the arm pays.
The ceiling explains the mechanism metric, not the accuracy gap.

**The fix, stated and NOT run** (the programme stopped here for review): after
narrowing, re-add any step a *kept helper's* own prerequisites require. That is
parity, not advantage — the host rules already state every action's constraints,
including `get_account_balance`'s own `logged_in_user`, so every arm is already
told. It is one change to `make_task_asop_provider` and it is the single
highest-value experiment left on this front.

---

## 1. Round 1 — the V2 document (authoring), gate unchanged

**Change.** `sopbench_asop_compile.py --variant v2`. v1 stays the default and
recompiles byte-for-byte, because every published arm was measured on it.

1. **Topological step order** (§0).
2. **Check separated from act.** `constraint_links` maps a state-tracker
   constraint to the action that *changes* its state — `logged_in_user ->
   login_user`. v1 rendered that as *"Verify the user login status condition ...
   Gate: deterministic (tool call: `login_user`)"*: verify a condition by
   performing the action that establishes it. Verified against
   `bank_assistant.py` before acting on it — `constraint_links` and
   `constraint_processes` are **mutually exclusive tables**, and the comment
   above `constraint_links` says in as many words that it "links the dependency
   to the action that changes its state". v2 labels those steps **ESTABLISH**
   and `constraint_processes` steps **VERIFY**, the distinction PVA's text makes
   explicitly. It also earns its keep later: round 3 uses it to decide which
   failed checks are retryable.
3. **Composition logic survives.** v1 flattens AND/OR/chain into one mandatory
   list, so `pay_loan`'s `("or", [A, B])` became two mandatory steps — the
   executor is told to satisfy both, which is strictly more than the domain
   asks. And `constraint_processes["pay_loan_account_balance_restr"]` is
   `or(and(get_account_balance, get_account_owed_balance), internal_get_database)`;
   v1's flattening described the second call as an **alternative** to the first
   when the domain requires both, and the evaluator walks the same tree. v2
   emits an OR group as one step naming its alternatives, and an AND branch as
   "call them all".
4. **The boilerplate is stated once, and it asks for a verdict.** v1 repeats
   *"The rules above state what this condition must be for this request..."*
   ~60 times. v2 states the working discipline once in the preamble — the block
   the engine injects into every step prompt anyway — and that discipline is
   PVA-shaped: call the tool, then state
   `VERDICT <condition>: SATISFIED|NOT SATISFIED - <the value you observed>`.

Configuration is otherwise identical to `asop-gated-value-rules`
(`--value-gate --host-rules`, stepwise), so the arm differs from it in the
document and nothing else.

**Pre-flight (6 tasks), read before spending the arm.** The prompt the executor
actually receives was dumped and read. Host rules present, V2 preamble present,
stepwise. Two things were visible and both are recorded rather than fixed
mid-flight:

- **The executor did not write a single `VERDICT` line.** The engine's
  `STEP_PROMPT` says *"In each turn you may either send a message to the user or
  make a tool call, never both"*, and `gpt-oss-20b` chooses the tool call and
  leaves `content` empty. The document asks for something the harness makes
  expensive. **That is the measurement that justifies round 2** — a document can
  request a verdict, but only an engine change can require one.
- On the impermissible pre-flight task the executor cleared the gate's refusal
  by going off-procedure entirely (invented a password, called `open_account`,
  then applied for the card the rules forbade). The engine gates *step
  advancement*, not *tool calls*. Recorded; it becomes round 3.

**Result (DEVELOPMENT number — `bank`, routable n=112).**

| arm | success | permissible | impermissible | calls | dirgraph | constr_ok |
|---|---|---|---|---|---|---|
| `none` | 0.500 | 0.200 | 0.667 | 1.85 | 0.571 | 0.750 |
| `pva` | **0.670** | 0.400 | **0.819** | 2.32 | 0.804 | 0.839 |
| `asop-gated-value-rules` | 0.545 | 0.450 | 0.597 | 3.02 | 0.607 | 0.679 |
| `asop-v1-order` | 0.562 | 0.475 | 0.611 | 3.04 | 0.625 | 0.705 |
| **`asop-v2-doc`** | **0.580** | **0.550** | 0.597 | 3.16 | 0.634 | 0.723 |

| contrast | isolates | delta | 95% CI | gained/lost | p |
|---|---|---|---|---|---|
| `v2-doc` vs `-rules` | the whole V2 document | +0.036 | [-0.029, +0.101] | 9 / 5 | 0.42 |
| `v2-doc` vs `v1-order` | everything BUT the order | +0.018 | [-0.037, +0.073] | 6 / 4 | 0.75 |
| `v2-doc` vs `none` | | +0.080 | [-0.028, +0.189] | 24 / 15 | 0.20 |

**Nothing here is significant, and the honest summary is "small and in the right
direction".** Two things are worth naming anyway:

* **`permissible` 0.550 is the best should-succeed number of any arm measured** —
  above `action_order`'s 0.475 and PVA's 0.400. The document helps the executor
  complete permissible work; that much replicates the direction every gated arm
  has shown since N26.
* **`impermissible` does not move at all** (0.597, identical to `-rules`) against
  PVA's 0.819. **That is where the entire remaining accuracy gap lives**, and it
  is what round 3 was built to address.

Run health: **8/134 hit the turn cap** (`-rules`: 0) — the V2 document costs
turns, and that is a real cost, not a rounding error. 371 gates, 0 fell back,
0 ungated, 9 escalations, routing unchanged.

---

## 2. Round 2 — verify-then-gate (the architecture inversion)

**Change.** `--verdict-gate`. One attestation turn before a procedure's final
action. The executor writes one `VERDICT <condition>: SATISFIED|NOT SATISFIED -
<value>` line per condition; the gate validates every line against the tool
results, using the **same `_TaskTruth`** the value gate uses (two graders that
disagreed about what a condition means would make an executor/gate contradiction
unreadable).

Three outcomes:

| status | when | what happens |
|---|---|---|
| `refuse` | a verdict is missing, cites no value, or is contradicted by the tool results | re-prompt. ⚠️ **The message names the condition and the required form and stops. It never says what the verdict should have been** — supplying it would make articulating optional again, which is the barrier this exists to remove. |
| `blocked` | the executor said a condition is NOT satisfied and the tools agree | **terminal.** The engine had no such state: a refused gate has only ever meant "not yet, retry". |
| `ok` | every condition attested and confirmed | the action is released |

**Cost, stated up front:** one extra assistant turn per task (plus the scripted
user's echo), spent once before the action rather than once per condition. The
per-step variant costs ~2 turns per condition, which on a 20-turn cap is a
turn-budget experiment wearing a verification experiment's clothes.

**A measured harness fact that decides whether this can work at all:**
`gpt-oss-20b` through LM Studio returns an **empty `content` on every turn that
carries a tool call** and puts its prose in a separate `reasoning` field. Both
are the model's own output. A gate reading only `content` would refuse
attestations the model did make, measuring the harness's serialisation rather
than the mechanism. Both are read.

**Pre-flight (4 tasks): the mechanism fires and the executor complies.**
3 attestation turns, 2 passed first time, 1 refused for a missing verdict. The
verdict lines are real and cite real values:

```
VERDICT username existence: SATISFIED - True
  -> gate: internal_check_username_exist returned 'True'   [checked, agrees]
VERDICT minimum eligible credit score: SATISFIED - 650
  -> gate: credit score 650 vs required > 600              [checked, agrees]
```

Turns 6.75 vs round 1's 4.5 on the same tasks; 0 cap hits, 0 gate fallbacks,
0 ungated steps.

**Result (DEVELOPMENT number — `bank`, routable n=112).**

| arm | success | permissible | impermissible | calls | dirgraph |
|---|---|---|---|---|---|
| `asop-v2-doc` | 0.580 | 0.550 | 0.597 | 3.16 | 0.634 |
| **`asop-v2-verdict`** | 0.571 | 0.525 | 0.597 | 3.12 | **0.634** |

**`v2-verdict` vs `v2-doc`: -0.009 [-0.048, +0.030], 2 gained / 3 lost, p=1.00.**
Against PVA: -0.098 [-0.187, -0.009], p=0.052.

🛑 **The architecture inversion moves accuracy by nothing, and it is a clean
null rather than a failure to fire.** The mechanism demonstrably ran:

| signature | measured |
|---|---|
| **VERDICT lines the executor wrote** | **108** |
| attestation turns | 62 — 37 ok, 21 refused, 4 blocked |
| **claims the tool results CONTRADICTED** | **4** |
| gates fell back / ungated | 0 / 0 |
| turn cap | 6/134 |

So the executor did articulate, in its own words, citing values; the gate did
audit those claims; and it caught four cases of the executor asserting something
the tools refuted. **And `success` moved -0.009 and `dirgraph` moved 0.000.**

`dirgraph` staying at exactly 0.634 is not a coincidence — §0c shows that is the
ceiling, and both arms are sitting on it. **Whatever verify-then-gate does or
does not do, this measurement could not have detected it through `dirgraph`**,
because the metric had no room left to move. That bound applies to the accuracy
number too, though less sharply. It is the honest limit on what round 2 shows.

---

## 3. Round 3 — a failed VERIFY check is not "try again"

**Evidence for this round came from round 2's pre-flight**, on the first
impermissible task, and it is recorded here because evidence-driven revision is
itself the claim under test:

> `internal_check_username_exist` returned `False`. The value gate correctly
> refused step 1. The engine said what it has always said — *"address what is
> missing and try again"* — and the executor obliged by inventing a password,
> calling `open_account`, and then applying for the credit card the rules
> forbade. The attestation turn never fired, because the run never reached the
> final step.

A refused gate has only ever meant "not yet". It has never been able to mean
"this must not happen" — which is the under-refusal N26 localised, seen from its
cause rather than its symptom.

**Change.** `--attest-failed-checks`. A **VERIFY** step whose gate resolves NOT
satisfied *on the returned value* routes to an attestation for that one
condition. A NOT SATISFIED the tool results confirm ends the procedure.

**ESTABLISH steps are exempt and keep the retry loop.** A VERIFY step reads
state and the value is what it is; an ESTABLISH step performs an action that can
genuinely be retried with different information (a login with the right
password). That distinction is only available because the V2 document labels its
steps — round 1's authoring change paying for itself.

The gate still never decides alone. The executor must state the verdict first,
and the prompt is written so it does not hand over what the check found:

> Read the value the tool returned against the operating rules for THIS request.
> Cite the actual value. […] If the condition is not satisfied, say so. Saying so
> is a correct outcome: some requests must not be carried out, and reporting that
> plainly is the job.

**Pre-flight (6 tasks): 2 of 2 impermissible tasks now terminate correctly.**

```
conv1 blocked | username existence.: internal_check_username_exist returned 'False'
conv3 blocked | minimum eligible credit score.: credit score 300 vs required > 400
```

The same task that invented an account in round 2's pre-flight now reads:

```
CALLS internal_check_username_exist(new_user_123)  -> False
"VERDICT username existence: NOT SATISFIED - False"
CALLS exit_conversation()
```

Two tool calls, no forbidden action, clean exit. 5 attestations, 3 ok,
2 blocked, **0 refused, 0 contradicted, 0 escalations**; turns 5.33 (below round
2's 6.75), calls 3.17.

**Result (DEVELOPMENT number — `bank`, routable n=112).**

| arm | success | permissible | impermissible | calls | dirgraph | constr_ok |
|---|---|---|---|---|---|---|
| `none` | 0.500 | 0.200 | 0.667 | 1.85 | 0.571 | 0.750 |
| `pva` | **0.670** | 0.400 | **0.819** | 2.32 | 0.804 | 0.839 |
| `asop-gated-value-rules` | 0.545 | 0.450 | 0.597 | 3.02 | 0.607 | 0.679 |
| `asop-v2-doc` | 0.580 | 0.550 | 0.597 | 3.16 | 0.634 | 0.723 |
| `asop-v2-verdict` | 0.571 | 0.525 | 0.597 | 3.12 | 0.634 | 0.714 |
| **`asop-v2-r3`** | **0.598** | 0.525 | **0.639** | 3.04 | 0.661 | 0.741 |

**`v2-r3` vs `v2-verdict`: +0.027 [−0.012, +0.066], 4 gained / 1 lost, p=0.375.**
Against `none`: +0.098 [−0.018, +0.214], p=0.135. Neither significant.

**`asop-v2-r3` is the best gated arm this programme has measured** (0.598, from
`asop-gated`'s 0.393 through `-value` 0.491 and `-rules` 0.545), and it is the
first to move the impermissible half at all: **0.597 → 0.639**, the number that
had been frozen across every V2 arm. The mechanism fired hard — **39 terminal
blocks** and 141 VERDICT lines across 134 tasks.

🛑 **But be precise about where the `dirgraph` gain comes from, because it is not
better verification.** Split on §0c's deprived/intact partition:

| arm | deprived (n=40) | intact (n=72) |
|---|---|---|
| `v2-doc` | 0.000 | 0.986 |
| `v2-verdict` | 0.000 | 0.986 |
| **`v2-r3`** | **0.075** | 0.986 |

`(0.075 × 40 + 0.986 × 72) / 112 = 0.661` — the whole aggregate gain is **3
deprived tasks that now pass because the run is terminated BEFORE it makes the
call that would have violated the graph.** That is passing by not acting. It is
the *correct* behaviour on an impermissible task, and it is still not evidence
that the executor verified anything better. The intact 72 sit at 0.986 in all
three V2 arms: the ceiling holds, and nothing in rounds 1–3 moved what happens
above it.

**And 39 blocks bought about 3 net correct impermissible outcomes.** Most blocks
landed on tasks that were already being handled correctly. The terminal state is
doing much less work than its firing rate suggests.

Run health: 2/134 hit the turn cap (the lowest of the three V2 arms), 359 gates,
0 fell back, 0 ungated, 7 escalations, 99 attestations (39 ok, 21 refused,
**1 contradicted**, 39 blocked).

---

## 3a. The gate's own grader was never checked, and it is wrong in two ways

Not a round — a rigor check run while the arms were in flight, and it changes
how every gated number since N27 should be read.

`_TaskTruth.holds` decides whether a returned value satisfies a condition. Every
value-gated arm depends on it. **Nothing had ever validated it.**

**The audit.** Give the grader perfect evidence — every verification tool's
result, computed from the task's own initial database, built the way
`env/evaluator.py` builds its system — and ask whether every condition the task
imposes is satisfied. That verdict should equal `action_should_succeed`. It is
offline and one-off; the runtime grader still sees only the transcript.

*(The first attempt reported 48 disagreements and every one was a replay
artefact: the domain classes gate their own methods on innate dependencies, so a
hand-constructed instance returns `False` for everything. The audit was measuring
the audit. Recorded because it is the same shape of error as N28's.)*

**`bank`: 114 agree, 10 disagree — and the disagreements are diagnosable.**

| defect | what it is | blast radius (routable sets) |
|---|---|---|
| **OR-COLLAPSE** | `wanted` records every LEAF of the task's constraint tree as required, so `("or", [A, B])` becomes "A **and** B" and the gate demands both | `bank` **8/112** (all `pay_loan`) · `library` **30/66** · `online_market` **40/172** |
| **KEY-COLLAPSE** | `wanted` is keyed by constraint NAME, so `transfer_funds`'s two `internal_check_username_exist` checks (`username`, `destination_username`) collapse and the task is graded on whichever result came last | `bank` **26/112** · `library` **20/66** · `online_market` **0/172** |

**OR-COLLAPSE is the same composition defect the V2 document was fixing in its
text, sitting in the gate the whole time** — over-refusal written into the
grader. `--respect-or` fixes it and takes the bank audit from 10 disagreements
to 6. KEY-COLLAPSE is *not* fixed here: the fix needs tool results keyed by
arguments rather than by tool name, and it is recorded with its blast radius
instead of being half-done.

⚠️ **The first KEY-COLLAPSE count was 91/112.** That was a scare number — a
repeated constraint name is only harmful when the two instances differ in
parameters or polarity, and most repeats are the same check reached twice. It
was re-measured before being written down. 26 is the number.

**How to read the published gated arms given this.** Both defects are present in
`asop-gated-value`, `-rules`, `-upfront` and every V2 round below, so **paired
contrasts between those arms are unaffected** — they share the defect. What is
biased is their comparison against `none`, `pva` and `asop-prompt`, none of which
use this grader: those comparisons are **biased against the gated arms**, by an
amount bounded by the 30% of `bank`'s routable set the two defects touch.

Both flags default off. A silent fix would have made the arms already measured
incomparable rather than better.

---

## 3b. Round 4 — citation grounding, the one check that transfers

🛑 **BUILT AND NOT RUN AS A BANK ARM.** It ships in the held-out configuration
(§4), which was fixed before any bank round reported, so it has never been
measured on its own. No number for it appears anywhere.

**Change.** `--cite-check`. An attestation citing a number or boolean **no tool
returned this run** is refused, grounded against the tool results plus the
task's own published parameters (citing the threshold you compared against —
"650 against the required 600" — is a correct citation, not an invention).

Why it is a separate round rather than part of round 2: `_TaskTruth.holds`
re-derives the verdict and **ignores what the executor said it saw**, so it
cannot tell a fabricated citation from a real one. That is the failure mode an
attestation architecture is most exposed to — and it is also, per §4, the only
part of this gate that survives a move to another domain.

🛑 **`--respect-or` (the §3a grader fix) was likewise BUILT AND NOT RUN as a
bank arm.** It ships in the held-out configuration only. The one thing that can
be said about it without an arm is what §3a measures directly: it takes the
grader's audit against `action_should_succeed` from 10 disagreements to 6.

---

## 4. Held-out — the only numbers that count

Rounds are iterated on `bank` and measured on `bank`, which by itself produces a
number that means nothing. `library` (n=66) and `online_market` (n=172) have
never been seen by any of this, and both already have `none` / `constraint_hint`
/ `action_order` / `pva` numbers to compare against
([`T2-PROCEDURE-LADDER.md`](T2-PROCEDURE-LADDER.md)).

**The compiler now sees any domain.** `--variant v2` compiles `library`
(13 procedures, 38 steps) and `online_market` (15, 53); both pass the self-check
**including the new ordering assertion**. `_readable_for` falls back to the
identifier's own words, which costs little because v2 no longer restates what a
condition must resolve to — the host's operating rules carry that.

**And the scorer is the same scorer.** `ASOP_DOMAIN` points
`score_asop_arms.py` at a held-out domain rather than forking a second one, and
its self-check was run before any held-out arm existed: it **reproduces T2's
published rates exactly on both domains** (`library` 0.227 / 0.379 / 0.364 /
0.561 at n=66; `online_market` 0.384 / 0.616 / 0.541 / 0.709 at n=172), and
still does on `bank`. That is what makes a held-out delta comparable to a
development one rather than merely similar-looking.

⚠️ **A limitation this exercise surfaced, no prior page records, and §3a's audit
now MEASURES: the document compiles, and the gate does not.** `_TaskTruth.holds`
is hand-written per constraint, and bank's 21 do not transfer to library's 19 or
online_market's 26. Run on the held-out domains, the grader resolves almost
nothing — **6 scorable predictions each**, because every constraint except
`logged_in_user` is outside its table:

| domain | conditions the grader cannot resolve (top) |
|---|---|
| `library` | `internal_check_book_exist` 42 tasks, `internal_check_book_available` 32, `valid_membership` 30, `internal_is_restricted` 20, `within_borrow_limit` 17, … |
| `online_market` | `internal_check_order_exist` 110, `credit_status_not_restricted_or_suspended` 56, `internal_check_product_exist` 54, `product_exists_in_order` 45, … |

So on a held-out domain the value gate is **effectively absent**, and what runs
is: the V2 document, the elicited per-condition verdict, the citation check
(§3b — the one part that needs no knowledge of the rule), and the terminal
consequence when the executor attests a failure. **That is the portable core of
this architecture, and the held-out number is a number about it** — not about
the machine check, which does not travel. Stated before the arm runs, so it
cannot become an excuse afterwards.

**Pre-flight on `library` (4 tasks), run before the arm and before any bank round
had reported — so the held-out configuration was fixed in advance rather than
chosen after seeing which bank number came out highest.** The whole final
configuration (`--value-gate --host-rules --verdict-gate --attest-failed-checks
--cite-check --respect-or`) on a domain none of it has seen:

| signature | measured |
|---|---|
| exited cleanly | 4/4, 0 cap hits |
| gates | 12, all deterministic, **0 fell back, 0 ungated** |
| attestations | 3 — 2 ok, **1 blocked**, 0 refused, 0 contradicted |
| **verdict lines written** | **6** — the executor articulates on a domain it has never seen |

And the attestation rows show exactly the split §4 predicts:

```
conv0 ok       user login status  stated=True  truth=True   resolved=checked
               internal is admin  stated=True  truth=None   resolved=uncheckable
conv3 blocked  user login status  stated=True  truth=True   resolved=checked
               internal is admin  stated=False truth=None   resolved=uncheckable
```

`logged_in_user` is machine-checked — it is the one constraint whose semantics
are shared across every SOPBench domain, via `constraint_links`. Everything else
is uncheckable, so **the executor's own verdict is what ends the run**, with the
citation check the only thing standing behind it. That is the portable core
doing the work, observed rather than assumed.

### The result

**`library` (n=66) — it ties PVA on a domain it has never seen.**

| arm | success | dirgraph | constr_ok | permissible (24) | impermissible (42) | calls |
|---|---|---|---|---|---|---|
| `none` | 0.227 | 0.455 | 0.530 | 0.042 | 0.333 | 2.76 |
| `constraint_hint` | 0.379 | 0.591 | 0.636 | 0.167 | 0.500 | 3.21 |
| `action_order` | 0.364 | 0.833 | 0.636 | 0.208 | 0.452 | 4.12 |
| `pva` | **0.561** | **0.939** | 0.727 | **0.333** | 0.690 | 4.64 |
| **`asop-v2-heldout`** | **0.561** | 0.879 | **0.773** | 0.292 | **0.714** | **4.27** |

**vs `none`: +0.333 [+0.212, +0.455], 23 gained / 1 lost, p = 0.0000** — the same
delta as PVA, to three decimals. It refuses better (`constraint_not_violated`
0.773 vs 0.727; impermissible 0.714 vs 0.690) and costs fewer calls (4.27 vs
4.64); PVA completes more permissible work (0.333 vs 0.292) and builds a fuller
verification trace (0.939 vs 0.879).

**`online_market` (n=172) — it beats PVA on every component metric and loses
`success` to one malformed call.**

| arm | success | dirgraph | constr_ok | db match | action ok | **no_tool_call_error** | calls |
|---|---|---|---|---|---|---|---|
| `none` | 0.384 | 0.442 | 0.680 | 0.698 | 0.651 | 0.994 | 3.19 |
| `pva` | **0.709** | 0.860 | 0.901 | 0.907 | 0.791 | 0.994 | 3.73 |
| **`asop-v2-heldout`** | 0.645 | **0.924** | **0.936** | **0.936** | **0.820** | **0.791** | 4.15 |

**vs `none`: +0.262 [+0.172, +0.351], 59 gained / 14 lost, p = 0.0000.**
Against PVA: −0.064.

🛑 **`success` is the AND of five conjuncts. This arm wins four of them and loses
the fifth**, and the fifth is not a method result:
`no_tool_call_error` 0.791 against everyone else's 0.994. Diagnosed to a single
call — **36 of 172 tasks invoke `internal_get_interaction_time`, which takes no
arguments, with one.** PVA does it once.

The cause is mine, and it is the compiled-artifact thesis failing where it should
be strongest. The V2 AND-branch renderer writes *"this condition needs
`get_order_details` and `internal_get_interaction_time` — call them all"* and
**never says what arguments either takes**. The dependency tables carry the
parameter maps (`{"username": "username"}`); the compiler reads them for
ordering and throws them away for rendering. On `bank` the AND branch was two
tools that both take `username`, so the executor guessed right and the defect
never showed.

**What it cost, bounded rather than asserted:** of the 36 affected tasks,
**24 satisfy every other conjunct** — so the defect alone is worth up to
**+0.140 success**, which would place the arm at 0.785 against PVA's 0.709. That
is an **upper bound, not a prediction**: removing a call changes the trajectory
and the other conjuncts could move either way. **No fixed arm was run** (the
programme stopped here), so the honest statement is: *this arm loses to PVA by
0.064 on `online_market`, and a single argument-rendering defect in our own
compiler is worth more than the gap.*

### What the held-out numbers are actually about

⚠️ Two things must be read with them, both stated before the runs:

1. **The held-out configuration is not identical to any bank arm.** It carries
   `--cite-check` and `--respect-or`, which no bank arm ran (§3b). It was fixed
   in advance, pre-flighted on `library`, and never tuned — but it is one
   configuration measured once per domain, not an ablation.
2. **The value gate is effectively absent here** (§4 above), and the run
   confirms it: **0 contradicted attestations on both domains**, because
   `_TaskTruth` can resolve almost nothing outside `bank` and a claim it cannot
   resolve cannot be contradicted. So these numbers measure the **portable
   core** — the V2 document, the elicited cited verdict, the citation check and
   the terminal consequence — with the machine check switched off by absence.

**That makes the result stronger than it looks, not weaker.** The portable core
alone ties PVA on `library` and beats it on four of five conjuncts on
`online_market`.

**Run health.** Both clean where it matters — 0 gates fell back, 0 ungated steps,
2/66 and 5/172 turn-cap hits — and one signature is not clean and is the honest
cost of the architecture:

| | library | online_market |
|---|---|---|
| attestation turns | 108 | 177 |
| **refused** | **91** | **93** |
| contradicted | 0 | 0 |
| blocked | 8 | 35 |
| VERDICT lines written | 114 | 443 |
| escalations | **35 / 66 tasks** | 5 |

**The executor writes the verdicts but struggles to write them in the required
form on an unfamiliar domain**, and on `library` that pushed over half the tasks
into an escalation — the gate refusing, then being stepped past. The elicitation
transfers; the *protocol* for it does not transfer cleanly, and that is a real
cost, not a rounding error.

---

## 4b. Every arm in one table

**`bank`, routable n=112 — DEVELOPMENT. These arms were iterated against this
domain's own failures. None of them is a result.**

| arm | source | success | perm | imperm | calls | dirgraph | vs `none` | p |
|---|---|---|---|---|---|---|---|---|
| `none` | T2 | 0.500 | 0.200 | 0.667 | 1.85 | 0.571 | — | — |
| `constraint_hint` | T2 | 0.571 | 0.325 | 0.708 | 2.33 | 0.688 | +0.071 | 0.15 |
| `action_order` | T2 | 0.661 | 0.475 | 0.764 | 2.56 | **0.848** | +0.161 | 0.0021 |
| `pva` | T2 | **0.670** | 0.400 | **0.819** | 2.32 | 0.804 | **+0.170** | 0.0013 |
| `asop-prompt` | N26 | 0.527 | 0.300 | 0.653 | 2.20 | 0.652 | +0.027 | 0.71 |
| `asop-gated` | N26 | 0.393 | 0.425 | 0.375 | 3.44 | 0.571 | −0.107 | 0.073 |
| `asop-gated-value` | N27 | 0.491 | 0.425 | 0.528 | 3.04 | 0.554 | −0.009 | 1.00 |
| `asop-gated-value-rules` | N28 | 0.545 | 0.450 | 0.597 | 3.02 | 0.607 | +0.045 | 0.52 |
| `asop-gated-value-upfront` | N28 | 0.509 | 0.425 | 0.556 | 3.03 | 0.589 | +0.009 | 1.00 |
| `asop-v1-order` | §0b | 0.562 | 0.475 | 0.611 | 3.04 | 0.625 | +0.062 | 0.34 |
| `asop-v2-doc` | §1 | 0.580 | **0.550** | 0.597 | 3.16 | 0.634 | +0.080 | 0.20 |
| `asop-v2-verdict` | §2 | 0.571 | 0.525 | 0.597 | 3.12 | 0.634 | +0.071 | 0.27 |
| **`asop-v2-r3`** | §3 | **0.598** | 0.525 | 0.639 | 3.04 | 0.661 | +0.098 | 0.14 |

The gated line rises monotonically — 0.393 → 0.491 → 0.545 → 0.598 — and **not
one step of it is statistically significant**, nor is the endpoint against
baseline (p = 0.14). `pva` and `action_order` remain ahead of every gated arm.
**Do not quote any number in this table as a result.**

**HELD OUT — `library` and `online_market`, never seen by any round. These are
the result.**

| domain | arm | success | vs `none` | p | dirgraph | constr_ok | calls |
|---|---|---|---|---|---|---|---|
| library (66) | `none` | 0.227 | — | — | 0.455 | 0.530 | 2.76 |
| library | `pva` | **0.561** | +0.333 | 0.0000 | **0.939** | 0.727 | 4.64 |
| library | **`asop-v2-heldout`** | **0.561** | **+0.333** | **0.0000** | 0.879 | **0.773** | **4.27** |
| online_market (172) | `none` | 0.384 | — | — | 0.442 | 0.680 | 3.19 |
| online_market | `pva` | **0.709** | +0.326 | 0.0000 | 0.860 | 0.901 | 3.73 |
| online_market | **`asop-v2-heldout`** | 0.645 | **+0.262** | **0.0000** | **0.924** | **0.936** | 4.15 |

**The one-line result: on two domains it had never seen, this project's own ASOP
apparatus beat baseline significantly on both (+0.333 and +0.262, both
p < 0.0001), tied PVA on one, and lost the other by 0.064 through a
malformed-argument defect in our own compiler worth up to 0.140.** That is the
first time in this programme that the apparatus has been competitive with the
scaffold it has been losing to — and it happened on held-out data, with the
machine check switched off by absence.

⚠️ **It is one run per task, one weak local executor, two domains, and no
ablation of the held-out configuration.** The bank rounds say the individual
mechanisms are each worth little; the held-out says the package is worth a lot.
Those are not contradictory — `bank` has a 35.7% structurally-impossible subset
(§0c) that neither held-out domain was checked for — but reconciling them needs
the experiment below, not more confidence.

---

## 4c. Recommendations — written down instead of run

The programme stopped here for review. Each of these is the next experiment, in
the order I would spend compute on them.

1. **Fix the narrowing ceiling and re-measure `bank`** (§0c). After narrowing,
   re-add any step a *kept helper's* own prerequisites require. One change to
   `make_task_asop_provider`. It is parity, not advantage: the host rules already
   state every action's constraints. **35.7% of `bank`'s routable set currently
   has a success ceiling of zero imposed by the harness**, and every gated number
   in the table above was measured under it. Until this is done, no `bank`
   comparison in this programme means what it appears to mean.
2. **Emit tool parameters in the compiled document** (§4). The dependency tables
   carry the parameter maps and the renderer discards them; that cost up to
   0.140 success on `online_market`. Cheap, mechanical, and it is the difference
   between "behind PVA" and "ahead of PVA" on that domain.
3. **Run the held-out configuration on `bank`, and the bank configuration on the
   held-out domains.** The two halves of this page ran different flag sets, so
   the package-vs-mechanism question is currently unanswerable. Two arms.
4. **Fix KEY-COLLAPSE** (§3a) — tool results keyed by arguments rather than tool
   name. `bank` 26/112, `library` 20/66.
5. **Reduce the attestation refusal rate.** 91/108 and 93/177 refused on the
   held-out domains, and `library` escalated 35 of 66 tasks. The elicitation
   transfers; the protocol for it does not. A looser accepted form, or accepting
   the verdict from the reasoning channel without the exact keyword, is the
   obvious first thing to try.

---

## 5. Reproducing

```bash
# the V2 document (deterministic, no model). --variant v1 reproduces the
# published document byte for byte; verified.
~/Code/SOPBench/.venv/bin/python scripts/eval/sopbench_asop_compile.py \
    --domain bank --variant v2 --out evals/sopbench-bank-asop/bank.v2.asop.md

# round 1 — the document, gate unchanged
OPENAI_BASE_URL=http://localhost:4242/v1 OPENAI_API_KEY=placeholder \
~/Code/SOPBench/.venv/bin/python scripts/eval/run_sopbench_asop.py \
    --arm asop-gated --value-gate --host-rules --domain bank \
    --asop evals/sopbench-bank-asop/bank.v2.asop.md \
    --output-dir "$SCRATCH/r1/gated" --stats-out "$SCRATCH/r1/stats.json"

# round 2 — + the attestation turn        (add --verdict-gate)
# round 3 — + terminal on a confirmed VERIFY failure
#                                         (add --attest-failed-checks)

# score every arm, paired, with SOPBench's own evaluator
ASOP_SCRATCH="$SCRATCH" \
~/Code/SOPBench/.venv/bin/python evals/sopbench-bank-asop/score_asop_arms.py
```

`score_asop_arms.py` still re-scores T2's own raw trajectories as a self-check
and must reproduce 0.500 / 0.552 / 0.634 / 0.642 exactly. If it drifts, nothing
on this page is comparable to anything on the others and the script says so.

### Known and NOT fixed, with its blast radius

**KEY-COLLAPSE (§3a) is still live in every arm on this page.** The fix needs
tool results keyed by argument values rather than by tool name, plus the
constraint's own parameter binding threaded through `holds()`. It was left alone
deliberately: the defect's direction is not systematic (the gate reads whichever
matching call came last, which can favour either half), its radius is recorded
(`bank` 26/112, `library` 20/66, `online_market` 0/172), and introducing a third
unvalidated grader variant hours before the held-out run is how a number stops
being attributable. It is the named next step, not a loose end.

### Run health, checked every round

Turn-cap hits, gate fallback rate, ungated steps, escalations, routing counts,
and — new for V2 — `verdict_lines` (how often the executor actually articulated
a verdict). **If `verdict_lines` is near zero the arm did not test the
architecture, it tested whether the model can follow a format**, and the round's
number should not be read as evidence about verify-then-gate.
