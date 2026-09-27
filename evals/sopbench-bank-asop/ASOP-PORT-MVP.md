# Does OUR ASOP work where the scorer can discriminate? The port, and what building it found

**2026-09-24. The first time this project's own ASOP document format and its
`asop_agent.py` gate runtime have run against SOPBench at all.** Every prior
number for these artifacts comes from tau2, where they lost (airline −0.240,
retail −0.083). `T2-PROCEDURE-LADDER.md` measured the *class* of intervention
winning on SOPBench `bank` (+0.142, p=0.0013) — but using **SOPBench's own PVA
scaffold**, a third party's prompt text. Nothing licensed "our ASOPs work".

**Headline: our document is flat, our gate is harmful, and the gate is harmful
in the opposite direction to everything the parent project has measured.**

On the routable subset (n=112, `bank`, `gpt-oss-20b`, T2's exact configuration):

- `asop-prompt` — our compiled document as context, no enforcement:
  **+0.027 [−0.067, +0.121], p = 0.71.** Flat. It does not reproduce PVA's
  +0.170 on the same tasks.
- `asop-gated` — the same document walked by our gate runtime:
  **−0.107 [−0.213, −0.001], p = 0.073.** Worse than baseline.
- **`asop-gated` vs `asop-prompt`: −0.134 [−0.243, −0.025], p = 0.0275.**

**That is pre-registered outcome 3, the most actionable of the three: the value
is in having the document, and the enforcement is what costs accuracy.**

**The identified bug was then fixed and re-measured (§3.1).** Resolving gates on
the returned value rather than on tool liveness recovers **+0.098
[+0.028, +0.168], p = 0.0127** — real and significant. It brings the gated arm
back to **−0.009 against baseline (p = 1.0000)**: the harm is gone. **But no
benefit appears.** The repaired arm is still behind the same document unenforced
(−0.036), still well behind PVA (0.491 vs 0.670), and still spends 64% more tool
calls than baseline to reach baseline accuracy. **"The concept works, this
implementation had a bug" is not supported by the data** — fixing the bug bought
back the loss and nothing more.

And the mechanism is not the one anyone expected. Our gate makes the executor
**better at performing actions** (`action_called_correctly` 0.729, the best of
any arm measured, against baseline 0.625 and PVA 0.500) and **much worse at
refusing them** (`constraint_not_violated` 0.535, the worst of any arm, against
baseline 0.721). This is **under-refusal, not the over-gating measured on
tau2** — because `check_tool_succeeded` asks "did the tool run", so on a task
whose precondition is designed to fail it reports `PASS` and advances the
executor into the forbidden action. The gate told the agent it had verified the
precondition when what it verified was that the agent had looked.

The port itself works, and the structural premise it was built to test is
confirmed: **the gates fire deterministically, without a model — 400 of 400
evaluated, `fell_back` 0.** On tau2 nearly every gate degraded to a model
opinion. All four pre-registered plumbing explanations are clean (§3). The arm
lost anyway.

🛑 **TWO MORE ARMS RAN AFTER THE ABOVE, AND THEY CHANGE WHAT IS OPEN (§3.3,
§3.4).** A fifth confound was found by dumping the prompt instead of reading the
builder: **the gated arms were the only arms not receiving SOPBench's own task
instructions** — 10,753 bytes of constraint specification *with its thresholds*
replaced, every turn, by a 1,830-byte prompt whose steps then say "the rules
above state what this condition must be" about rules the runtime had deleted.
That is N24 arriving from the other side, and it had been confounded with the
stepwise-presentation hypothesis this whole time. Both were then separated and
measured:

- **information** (restore the task's rules, presentation unchanged):
  **+0.054 [−0.001, +0.108], p = 0.11**, 8 gained / 2 lost — the direction worth
  powering properly, and not yet a result.
- **presentation** (the whole checklist upfront, PVA's own enumerate-then-act
  instruction, rules held present): **−0.036 [−0.101, +0.029], p = 0.42**, and
  `dirgraph_satisfied` **0.590 → 0.590** against baseline's 0.597.
  **§3.2's hypothesis is not supported.** Showing the executor the entire
  checklist and telling it to enumerate before acting moved the
  did-it-actually-verify metric **by nothing**.

Neither closes the gap: the best new arm is 0.545 against PVA's 0.670, and the
upfront arm loses to PVA by **−0.161, p = 0.0014**. Both still spend ~3.0 calls
per task against baseline's 1.85.

🛑 **But the run is not the most valuable thing on this page. Four confounds
were caught before any number was quoted** — three in pre-flight, each of which
would have been reported as "our ASOP loses" when the loss was the harness's,
and a fourth that runs the other way and would have handed the gated arm about
**ten free points**. One of them invalidates a central design decision in
`ASOP-PORT-SCOPE.md`. They are written up first, before any number, because all
four would recur in any re-attempt.

---

## 1. What was built

| piece | file | what it is |
|---|---|---|
| the engine | `scripts/eval/asop_engine.py` | ~700 host-independent lines lifted **verbatim** out of `asop_agent.py` |
| tau2 shell | `scripts/eval/asop_agent.py` | now a thin `LLMAgent` wrapper over the engine |
| SOPBench shell | `scripts/eval/sopbench_asop_swarm.py` | a `Swarm` subclass + message shim |
| the compiler | `scripts/eval/sopbench_asop_compile.py` | `bank`'s dependency tables → an ASOP document |
| the document | `evals/sopbench-bank-asop/bank.asop.md` | 20 procedures, 70 steps, 67 deterministic gates |
| the runner | `scripts/eval/run_sopbench_asop.py` | installs either arm, logs the falsifier signatures |
| scoring | `evals/sopbench-bank-asop/score_asop_arms.py` | nine arms, one scoring function |

**Extraction rather than reimplementation was the point.** A second
implementation of the gate logic would have answered "do two codebases
differ?", not "does our ASOP work on a host whose scorer can discriminate?".
The tau2 numbers and the SOPBench numbers now come from the same code.

### The scope document's two load-bearing claims, checked

Both hold.

- **"Coupled at exactly two seams."** Confirmed — `ASOPAgent(LLMAgent)` and
  `ASOPAgentState(LLMAgentState)`, plus `SystemMessage`, which the doc did not
  count but which is the same kind of coupling. Everything else reads messages
  by `getattr` and was portable as-is.
- **"A one-line install, no benchmark patch."** Confirmed by reading rather
  than assuming: `run_simulation.py` does `from swarm.core import *`,
  `swarm/core.py` declares no `__all__`, and `Swarm` is referenced exactly once
  as a module global (`run_simulation.py:170`). `run_simulation.Swarm =
  <subclass>` rebinds it. SOPBench is called by name and never edited.

### The correctness requirement that mattered most

`internal_get_database` appears **zero times** in the compiled document.
N20 measured that node behind 37 of GLM's 38 and 14 of gpt-oss-20b's 15 false
refusals: it is an OR-branch in nearly every `constraint_processes` entry, and
`env/task.py:249` strips it from the agent's tools, so a naïve compiler emits a
gate no agent can ever pass. The compiler imports
`sopbench_extract.environment_verified_nodes()` rather than reimplementing the
predicate, asserts the result is exactly `{internal_get_database}` for `bank`,
and refuses to emit it as a gate tool.

### Gates that are not opinions

67 of 70 steps carry `Gate: deterministic` naming a runnable tool;
`named_tool()` resolves every one. The other three
(`maximum_deposit_limit`, `maximum_exchange_amount`, `call_get_database`) have
no agent-callable verification action in the domain at all, and are emitted as
`Gate: judged`.

**This is the structural difference from tau2**, and it is the one thing the
port set out to establish. Measured in the run: `gates_fell_back = 0` and
`ungated_tripwire_calls = 0` — no gate declared deterministic and then quietly
became a model verdict.

---

## 2. Four confounds caught before any number — the substantive finding

Each of these produced, or would have produced, a clean-looking loss for the
gated arm. None of them is a property of ASOP.

### 2.1 A hand-rolled router scored 0.343, and failed in a way that looks like the method failing

The first router matched the user's words against the document's `## Routing`
table and procedure names. Measured against all 134 `bank` tasks: **0.343**.

The failure mode is worth stating exactly, because it is not random noise.
`set_safety_box` requests mention authenticating an admin password — *because
that is one of the action's preconditions*. "Set Admin Password" is a procedure
whose name those words spell. **21 of 21 `set_safety_box` tasks routed to
`Set Admin Password`.** A router scored on token overlap is hijacked by the
constraint vocabulary of the very procedure it is trying to find.

Replaced with a model router, which is also the more faithful choice:
`asop_agent.create_asop_agent` routes with a model call, so the two hosts now
run the same configuration. Measured: **12 of 14 goals**, and both misses are
goals that have **no procedure at all** (see §2.4). On routable goals, 12/12.

### 2.2 A termination instruction that rewarded refusing

SOPBench ends a conversation only when the assistant calls `exit_conversation`,
so the shell has to say so or every run burns to the turn cap. The first
version of that text was appended to every step prompt and read:

> If the rules above mean this request must not be carried out, say so plainly
> and then call `exit_conversation`. **Refusing correctly is a successful
> outcome**; looping is not.

On the first gated task the executor cleared two gates, had `login_user` come
back `False` on the third, read that **gate refusal** as "the rules forbid
this", and exited — on a task whose `action_should_succeed` is `True`.

A gate refusal means *this step is not satisfied yet, address what is missing
and try again*. A rule prohibition means *this must not happen at all*. The
suffix collapsed the two and attached a reward to the refusing side.
**Over-refusal is the exact liability this experiment measures**, so an
instruction encouraging it would have manufactured the finding it was meant to
detect. Replaced with a neutral statement of the host's stop requirement,
appended identically in both arms.

### 2.3 🛑 The scope document's compiler-input decision is empirically false

`ASOP-PORT-SCOPE.md` §3.1 argues the compiler should consume the domain-static
dependency tables, not the per-task action graph, and justifies it this way:

> `env/task.py::get_default_dep_full` already composes the first three into
> exactly the tree `get_dep_verb(..., "structured")` verbalises into the prose
> every arm already sees. So the compiler consumes the same information the
> baseline prompt does — **no leakage, and no new information**.

**The second half is false, and it is the half the experiment rests on.**
`task_initializer` builds the assistant's instructions from the **task's**
`constraints`, and a task's set is a SUBSET of the domain-static set.

Measured on `bank` task 0 (`apply_credit_card`, `action_should_succeed` True):

| | preconditions |
|---|---|
| what every other arm is told | `internal_check_username_exist` — **one** |
| what the static document compiles | username, credit score, **`logged_in_user`** — three |

The consequence was visible on the first gated task. The user's known
information is `{monthly_income, username, total_assets}` — **no credentials
anywhere**. The executor cleared the two reachable gates, then had to satisfy
`logged_in_user`, invented `password123`, `login_user` returned `False`, and
the gate correctly refused a step that could never pass.

So a domain-static document does not merely *differ* from the other arms. It
hands the gated arm preconditions the task never imposed and the user cannot
supply. A loss would have measured **"static document vs per-task spec"**, not
"gated vs ungated", and the gated arm would have lost for a reason with nothing
to do with gating.

**Fix:** both arms narrow the compiled document to the task's own constraint
set (`run_sopbench_asop.make_task_asop_provider`). The document is still ours,
still compiled deterministically, still walked by our runtime; only *which of
its steps apply* is per task — which is what every other arm already gets.
Information parity is now exact, and the prompt-vs-gated contrast isolates
enforcement alone.

This is a correction to `ASOP-PORT-SCOPE.md` §3.1 and belongs in `DECISIONS.md`
(§6 — the isolated worktree could not touch it).

### 2.4 🛑 A schema gap that pays the gated arm ~10 free points — the fourth confound

`cancel_credit_card` and `pay_bill_with_credit_card` are task goals (**22 of 134
tasks**) but are absent from bank's `actions` schema, so `create_assistant`
never exposes them and no arm can perform them. `sopbench_extract.py` already
documents the upstream gap.

**I first filed this as a ceiling affecting all arms equally. Measuring it showed
that is wrong, and the asymmetry favours the arm under test.**

No arm can perform these actions. But the **gated** arm cannot even *route* to
them — there is no procedure — so it stays in triage, makes no substantive tool
call, and exits. Measured over the first 17 such tasks:

| arm | tool calls per task | exited cleanly |
|---|---|---|
| `asop-gated` | **1.0** (the `exit_conversation` itself) | 17/17 |
| `asop-prompt` | 2.1 | 17/17 |

An arm that calls nothing **cannot violate a constraint**. Of the 22 tasks, **14
are `action_should_succeed=False`** — tasks where the correct outcome is to
refuse. So the gated arm stands to collect `constraint_not_violated`, and very
likely `success`, on all 14 for free: roughly **+10 points of pass rate,
concentrated entirely in the impermissible half, bought by being unable to act
rather than by gating well.**

That is precisely the "a scaffold that over-gates buys the refusal column by
selling the completion column" artifact T2's split exists to expose — arriving
through a schema gap instead of through the gate, which is why the split alone
would not have caught it.

**Consequence for reading this page:** the headline comparison is the
**routable subset (n=112)**, with the 22 no-procedure tasks dropped.
`score_asop_arms.py` reports that table first and the full 134-task table
second. Neither is hidden; the routable one is the one that is about the method.

The good news from the same measurement: the gated arm **exits cleanly on all of
them rather than burning to the turn cap**, so pre-registered falsifier C1
(budget exhaustion) is clean — the turn budget is not where this port fails.

---

## 3. Result

**All arms completed: `bank`, n=134, 1 run each, `openai/gpt-oss-20b`,
scripted user — T2's exact configuration, so `none` and `pva` are reused
unchanged and every delta is paired on identical tasks.**

### Headline — the routable subset (n=112), which is the comparison about the method

The 22 tasks whose goal has no compiled procedure are dropped here for the
reason given in §2.4; the full 134-task table follows.

| arm | success | permissible (n=40) | impermissible (n=72) | calls/task |
|---|---|---|---|---|
| `none` | 0.500 | 0.200 | 0.667 | 1.85 |
| `constraint_hint` | 0.571 | 0.325 | 0.708 | 2.33 |
| `action_order` | 0.661 | 0.475 | 0.764 | 2.56 |
| `pva` | **0.670** | 0.400 | **0.819** | 2.32 |
| **`asop-prompt`** | 0.527 | 0.300 | 0.653 | 2.20 |
| **`asop-gated`** | **0.393** | **0.425** | **0.375** | **3.44** |

Paired against `none`, exact McNemar:

| arm | delta | 95% CI | gained | lost | p |
|---|---|---|---|---|---|
| `pva` | **+0.170** | [+0.074, +0.265] | 26 | 7 | **0.0013** |
| `action_order` | **+0.161** | [+0.066, +0.255] | 25 | 7 | **0.0021** |
| `asop-prompt` | +0.027 | [−0.067, +0.121] | 16 | 13 | 0.7111 |
| **`asop-gated`** | **−0.107** | [−0.213, −0.001] | 13 | 25 | 0.0730 |

And the contrast the experiment was built for:

> **`asop-gated` vs `asop-prompt`: −0.134 [−0.243, −0.025], 13 gained / 28 lost,
> p = 0.0275.**

### This is outcome 3, and it is the most actionable of the three

Of the three pre-registered outcomes, this is the third: **`asop-prompt` beats
`asop-gated`, significantly.** Our document as context is *flat* (+0.027, p=0.71).
Our document **with the gate runtime enforcing it is worse than baseline** and
significantly worse than the same document unenforced. The fix this points at is
"give the agent the document, don't gate it" — a much smaller change than
anything previously tested.

Note also that `asop-prompt` does not reproduce PVA's win (+0.027 vs +0.170)
even though both are procedure-as-prompt-text. So the document format is
underperforming SOPBench's PVA scaffold too — but *flatly*, not harmfully.
The harm is specifically the enforcement.

### The mechanism, and it is the opposite of the tau2 liability

The split is where this becomes a finding rather than a number. On all 134 tasks:

| arm | permissible: `action_called_correctly` | impermissible: `constraint_not_violated` |
|---|---|---|
| `none` | 0.625 | 0.721 |
| `pva` | 0.500 | **0.884** |
| `asop-prompt` | 0.542 | 0.779 |
| **`asop-gated`** | **0.729** (best of any arm) | **0.535** (worst of any arm) |

**Our gate makes the executor better at doing the action and much worse at
refusing it.** On the permissible half it is the strongest arm measured —
0.729 correct action calls against baseline's 0.625 and PVA's 0.500, and
permissible success 0.354 against PVA's 0.333. On the impermissible half it
collapses: constraint violations rise from 0.279 (baseline) to **0.465**.

🛑 **This is NOT the over-gating the parent project measured on tau2. It is
under-refusal, and the cause is `check_tool_succeeded` being liveness rather
than correctness.** On an impermissible task the precondition is *designed to
fail* — the balance is insufficient, the credit score too low. The executor
calls `get_account_balance`, the call returns ok, and the gate reports:

> `get_account_balance ran and returned ok` → **PASS**

The step is marked satisfied and the executor is advanced to the action. **The
gate told the agent it had verified the precondition when what it verified was
that the agent had looked.** An unenforced document leaves the agent reading
the constraint prose and sometimes refusing correctly, which is exactly why
`asop-prompt` is merely flat while `asop-gated` is harmful.

§4 of the scope document flagged liveness-vs-correctness as "the largest single
interpretive bound on this experiment", and predicted it would inflate a *win*.
It did the reverse: it converted a verification step into a **licence to
proceed**.

### The mechanism check that also fails

T2's falsifier B asks whether `dirgraph_satisfied` moves with success — whether
the agent actually called the required verification actions. `action_order`
moved it 0.597 → 0.843. **`asop-gated` moves it 0.597 → 0.604: flat, on 86% more
tool calls** (3.44 vs 1.85). The gated arm is calling substantially more tools
without calling more of the *required* ones. It is more expensive and not more
compliant.

### 3.1 The fix, implemented and re-measured — it removes the harm and adds no benefit

The diagnosis above named a specific bug. A diagnosis is not evidence, so the
gate was changed to resolve on the **value a verification tool returned**
rather than on the tool having run, and the arm was re-run at the identical
configuration. Everything else — document, runtime, executor, task set,
narrowing, router — is unchanged. `asop-gated-value` is that arm.

| arm | success | permissible (n=40) | impermissible (n=72) | calls/task |
|---|---|---|---|---|
| `none` | 0.500 | 0.200 | 0.667 | 1.85 |
| `pva` | **0.670** | 0.400 | **0.819** | 2.32 |
| `asop-prompt` | 0.527 | 0.300 | 0.653 | 2.20 |
| `asop-gated` (liveness) | 0.393 | 0.425 | 0.375 | 3.44 |
| **`asop-gated-value`** | **0.491** | 0.425 | 0.528 | 3.04 |

| contrast | delta | 95% CI | gained | lost | p |
|---|---|---|---|---|---|
| **`gated-value` vs `gated`** | **+0.098** | **[+0.028, +0.168]** | 14 | 3 | **0.0127** |
| `gated-value` vs `none` | −0.009 | [−0.115, +0.098] | 18 | 19 | 1.0000 |
| `gated-value` vs `asop-prompt` | −0.036 | [−0.138, +0.066] | 15 | 19 | 0.6076 |

**The fix is real and it is significant.** It recovers +0.098 against the
liveness gate, 14 tasks gained against 3 lost, p = 0.0127. The mechanism moved
in exactly the predicted place: `constraint_not_violated` on the impermissible
half goes **0.535 → 0.686**, and 257 of 358 gate verdicts are now decided on a
returned value rather than on a call having happened.

🛑 **And it does not vindicate the approach. "The concept works, this
implementation has a bug" is NOT what the data says.**

- **It removes harm; it does not add benefit.** `asop-gated-value` vs baseline
  is **−0.009, p = 1.0000** — statistically indistinguishable from doing nothing.
- **It is still behind the same document unenforced** (−0.036 vs
  `asop-prompt`) and **well behind PVA** (0.491 vs 0.670).
- **Under-refusal is reduced, not resolved.** Impermissible
  `constraint_not_violated` reaches 0.686 against baseline's 0.721 and PVA's
  0.884 — still the worst of the non-broken arms.
- **It costs 64% more tool calls than baseline** (3.04 vs 1.85) to arrive at
  baseline accuracy. Paying more for the same answer is a loss, not a tie.

### 3.2 The remaining gap is not the liveness bug, and the mechanism says where it is

`dirgraph_satisfied` — "did the agent actually perform the required
verification actions, with matching parameters" — is the mechanism check:

| arm | `dirgraph_satisfied` | calls/task |
|---|---|---|
| `none` | 0.597 | 1.83 |
| `action_order` | **0.843** | 2.64 |
| `pva` | **0.791** | 2.31 |
| `asop-gated` | 0.604 | 2.99 |
| `asop-gated-value` | **0.590** | 2.65 |

**Our stepwise walk never engages the mechanism that the two winning arms
engage.** PVA and `action_order` raise required-verification compliance by
~0.2; both of ours leave it at baseline while spending more calls. Fixing the
gate's resolution rule did not change this at all (0.604 → 0.590).

That is the honest location of the remaining problem, and it is upstream of the
gate: **the arm is not failing because its gate judges wrongly — it now judges
much better — it is failing because walking one step at a time does not make
the executor perform the required verifications.** A plausible reading, offered
as hypothesis rather than finding: PVA has the agent enumerate the whole
constraint checklist *before acting*, whereas a one-step-at-a-time walk lets it
satisfy each gate minimally and locally, so it never builds the full
verification trace the scorer rewards. That is a property of the *execution
model*, not of the gate, and it is not addressed by anything tested here.

⚠️ **TESTED, AND NOT SUPPORTED — see §3.4.** The hypothesis in the paragraph
above was built as its own arm and measured on this same comparison.
`dirgraph_satisfied` is **0.590 with upfront enumeration and 0.590 without it**.
Do not cite the paragraph above as the live explanation of the remaining gap;
§3.4 is where that question now stands, and §3.3 is the second variable that had
been confounded with it the whole time.

### 3.3 🛑 The gated arm was also missing the task's own rules — a second variable, confounded with the first

**Found while building the test for §3.2's hypothesis, by dumping the prompt
rather than reading the builder** (`ASOP_DUMP_PROMPT`, in
`sopbench_asop_swarm.py`). It changes what §3.2's hypothesis could have been
tested against, so it is stated before the result.

`run_simulation.task_initializer` sets `assistant_agent.instructions` once per
task from `assistant_info["instructions"]`. That block is the domain's Core
Operating Principles, the AND/OR/CHAIN composition legend, and the **full
per-action constraint specification with its thresholds** — credit score > 600,
deposit ≤ 10000, exchange ≤ 3000, balance ≥ amount, and so on.

Every arm above the gated ones **keeps** that block: `none` is it, `pva` and
`action_order` append their scaffold to it, `asop-prompt` appends our document
to it. The gated arms alone do `agent.instructions = prompt` on every turn, so
they **replace** it. Measured on `bank` task 0:

| | bytes | contains the thresholds? |
|---|---|---|
| what SOPBench built | 10,753 | yes — every action's constraints, with numbers |
| what the gated arm sent instead | 1,830 | **no** — no thresholds, no composition logic |

And the compiled step text the executor *did* receive reads, verbatim:

> **The rules above** state what this condition must be for this request;
> establish the actual value and confirm it against them before proceeding.

The rules above were not there. The runtime had deleted them.

**This is N24 arriving a second time from the other side.** N24 found the gated
arm being told about *more* preconditions than the task imposed, and fixed it by
narrowing. This is the gated arm being told the *value* of none of them. Both
make a loss attributable to information volume rather than to gating, and
neither is a statement about whether stepwise enforcement works.

Restoring the block (`--host-rules`) is therefore **parity, not advantage**: it
makes the gated arm the exact analogue of `asop-prompt` — host instructions plus
our document — plus enforcement, which is the contrast §3 claimed to be
measuring all along.

**So the experiment has two variables, and they are separable.** Each arm below
moves exactly one:

| arm | task rules present | presentation | gate |
|---|---|---|---|
| `asop-gated-value` | **no** | stepwise | value |
| `asop-gated-value-rules` | **yes** | stepwise | value |
| `asop-gated-value-upfront` | **yes** | **upfront checklist** | value |

`-rules` minus `-value` is the **information** effect. `-upfront` minus `-rules`
is the **presentation** effect — §3.2's hypothesis, isolated, and tested in the
one regime where enumerating a checklist is even possible.

### 3.4 The presentation hypothesis is not supported: enumeration upfront moves `dirgraph_satisfied` by nothing

`--upfront` replaces the one-step-at-a-time prompt with the whole per-task
checklist plus a PVA-shaped instruction — enumerate every item, verify each by
calling its tool and citing the returned value, self-verify, only then act. It
is **presentation only**: gates, gate kinds, the value check, escalation and
advancement are untouched, and
`test_upfront_changes_no_verdict_the_stepwise_walk_would_have_reached` asserts
the verdicts are identical item for item in both modes.

Routable subset, n=112, same document, same executor, same configuration:

| arm | success | permissible (n=40) | impermissible (n=72) | calls/task | `dirgraph` |
|---|---|---|---|---|---|
| `none` | 0.500 | 0.200 | 0.667 | 1.85 | 0.597 |
| `pva` | **0.670** | 0.400 | **0.819** | 2.32 | **0.791** |
| `asop-prompt` | 0.527 | 0.300 | 0.653 | 2.20 | 0.679 |
| `asop-gated` (liveness) | 0.393 | 0.425 | 0.375 | 3.44 | 0.604 |
| `asop-gated-value` | 0.491 | 0.425 | 0.528 | 3.04 | 0.590 |
| `asop-gated-value-rules` | **0.545** | 0.450 | 0.597 | 3.02 | 0.597 |
| `asop-gated-value-upfront` | 0.509 | 0.425 | 0.556 | 3.03 | 0.590 |

Paired, on the routable subset:

| contrast | what it isolates | delta | 95% CI | gained/lost | p |
|---|---|---|---|---|---|
| `-rules` vs `-value` | **information** | +0.054 | [−0.001, +0.108] | 8 / 2 | 0.1094 |
| `-upfront` vs `-rules` | **presentation** | **−0.036** | [−0.101, +0.029] | 5 / 9 | 0.4240 |
| `-upfront` vs `asop-prompt` | gate vs no gate | −0.018 | [−0.114, +0.078] | 14 / 16 | 0.8555 |
| `-upfront` vs `pva` | the gap that matters | **−0.161** | [−0.252, −0.070] | 6 / 24 | **0.0014** |

**The hypothesis in §3.2 is not supported.** `dirgraph_satisfied` — the
mechanism check, "did the agent actually perform the required verifications with
matching parameters" — is **0.590 with upfront enumeration, against 0.590
without it and 0.597 at baseline**. Presenting the entire checklist and
instructing the model, in PVA's own words, to enumerate it before acting moved
that metric **by nothing**. This is the outcome the experiment was designed to
be able to return, and it is a clean null:

> *the presentation model was the bottleneck* — **not supported.** Stepwise
> presentation is not why our gated arm fails to build a verification trace.

Success went **down** 0.036 rather than up, on 5 gained against 9 lost. That is
well inside noise (p = 0.42) and is reported as "no gain," not as a cost.

**The information variable is the one that carries what movement there is**, and
it is the better-behaved of the two: +0.054, 8 gained against 2 lost, the only
new contrast whose CI barely touches zero. `-rules` at 0.545 is the first gated
arm to edge past the ungated document (0.527) — but by 0.018 with p = 0.52, so
this is a direction, not a result. **It is the obvious thing to power properly
next, and it should not be quoted as a win.**

**What none of it closes is the gap to PVA.** The best new arm is 0.545 against
PVA's 0.670, and the upfront arm loses to PVA by −0.161 at p = 0.0014 — the
largest and most significant contrast on this page. Both new arms still spend
~3.0 calls per task against baseline's 1.85 and PVA's 2.32: **63% more tool
calls than baseline to land within noise of baseline.**

#### The one signature that moved, and the check it forced

The upfront arm is the only arm in this programme where the cap bites more than
once: **6 of 134 runs** did not exit on `exit_conversation` (stepwise `-rules`:
0 of 134; the N26 run: 1 of 134). More text and an instruction to enumerate
before acting costs turns, which is a real property of the intervention — but if
the 5-gained/9-lost discordance lived inside those 6 runs, "presentation does not
help" would be a budget artifact and must not be quoted.

It does not. Only **1 of the 14 discordant pairs** is a capped run, and removing
all six changes nothing that matters:

| | n | delta | 95% CI | p | `-upfront` dirgraph |
|---|---|---|---|---|---|
| all routable | 112 | −0.036 | [−0.101, +0.029] | 0.4240 | 0.589 |
| minus the 6 capped | 106 | −0.028 | [−0.095, +0.038] | 0.5811 | 0.585 |

#### Falsifier signatures for both new arms

| signature | `-rules` | `-upfront` | verdict |
|---|---|---|---|
| exited cleanly | 134 / 134 | 128 / 134 | see above — checked, and not load-bearing |
| `fell_back` | 0 of 357 | 0 of 350 | clean — no gate declared deterministic and became an opinion |
| `ungated` | 0 | 0 | clean — no step went unverified |
| escalations | 5 | 6 | clean — comparable to N27's 3, the gate did not starve the task |
| routing | **identical between the two arms**, 117 routed goals | | clean — the presentation contrast is not a routing contrast |
| gate resolution | 254 / 362 on the returned value | 266 / 356 | clean — the value gate is doing its job in both |

#### The cell this does not measure, stated plainly

**Upfront presentation *without* the task rules was not run.** The three arms
above move one variable at a time along one path
(`-value` → `-rules` → `-upfront`); the fourth corner of the 2×2 is missing.
It was skipped deliberately — enumerating a checklist whose thresholds are
absent is a foregone null, and the GPU budget bought two arms, not three — but
it means the presentation effect is measured **only in the rules-present
regime**. A reader wanting "is stepwise-vs-upfront ever the variable" has one
cell of evidence here, not two.

### Full 134-task table, for completeness

| arm | success | dirgraph | constraint_not_viol. | db match | action called | paired delta vs `none` | p |
|---|---|---|---|---|---|---|---|
| `none` | 0.500 | 0.597 | 0.754 | 0.843 | 0.716 | — | — |
| `constraint_hint` | 0.552 | 0.694 | 0.769 | 0.866 | 0.754 | +0.052 | 0.2295 |
| `action_order` | 0.634 | 0.843 | 0.813 | 0.896 | 0.731 | +0.134 | 0.0039 |
| `pva` | 0.642 | 0.791 | 0.828 | 0.933 | 0.791 | +0.142 | 0.0013 |
| `asop-prompt` | 0.530 | 0.679 | 0.761 | 0.896 | 0.754 | +0.030 | 0.5847 |
| `asop-gated` | 0.410 | 0.604 | 0.582 | 0.746 | 0.657 | −0.090 | 0.0961 |

The gated arm scores **higher** on the full set (0.410) than on the routable
subset (0.393) precisely because of §2.4's artifact: on the 14 impermissible
no-procedure tasks it cannot act, so it cannot violate, and it banks refusals it
did not earn. That is why the routable subset is the headline.

### Falsifier signatures, all pre-registered, all clean

| signature | measured | verdict |
|---|---|---|
| budget exhaustion (C1) | **1 of 134** runs hit the cap; 133 exited on `exit_conversation` | clean — the loss is not a turn-budget artifact |
| `fell_back` (C2) | **0 of 400** gates | clean — no gate declared deterministic and became an opinion |
| `ungated` | **0** | clean — no step went unverified |
| escalation (C3) | **8 of 408** verdicts (2.0%) | clean — the gate did not starve the task |
| routing accuracy (C4) | every goal's routed count matches its task count exactly; the only misroute is the 5 `pay_bill_with_credit_card` tasks reaching `Pay Bill`, which have no procedure of their own | clean |

**None of the four pre-registered plumbing explanations applies.** The gates
fired, deterministically, on time, without starving the run, on correctly-routed
procedures — and the arm still lost. This is a result about the method, not the
harness.

---

<details>
<summary>Superseded: the state of this page before the run completed</summary>

🛑 **There is no pass-rate result on this page, and that is the honest state.**
The four-arm comparison was launched at T2's exact configuration and is still
running. **No delta is quoted, because at the `n` reached so far every
comparison is noise** — the paired McNemar p-values sit at 1.0, and this project
has enough trouble with numbers that were believed once and never re-measured
without adding one that was quoted before it meant anything.

**Why it did not finish.** The local GPU is shared with another sweep, and the
gated arm additionally pays one routing call per conversation plus more turns
per task. Measured throughput was roughly **2.5–3 minutes per task per arm**
against T2's ~5.3 tasks/min aggregate, which puts a 134-task pair well outside a
single working session. `ASOP-PORT-SCOPE.md` budgeted "3–4 working days plus one
overnight run" — the engineering came in under that, the run did not. Both arms
write incrementally and are still going; §5 has the one command that scores
them.

### What the run HAS established, and it is the part the port existed to test

| signature | measured | reading |
|---|---|---|
| gates evaluated | 18 so far, **18 declared deterministic, 18 resolved deterministic** | no model opinion entered any verdict |
| `fell_back` | **0** | no gate claimed `deterministic` and quietly became a judge call |
| `substituted` | **0** | no gate got a different kind of check than it declared |
| `ungated_tripwire_calls` | **0** | no step went unverified |
| gates passed / refused | 14 / 4 | the gate discriminates; it is not a rubber stamp |
| escalations | 1 | the `max_refusals=3` starvation guard fires as designed |
| runs ending on `exit_conversation` | all | the termination adaptation works; no run burned to the turn cap |

**This is the structural difference from tau2, and it is now demonstrated rather
than predicted.** §3.2 of the scope document argued that on SOPBench — unlike
tau2, where most gates named nothing re-runnable and degraded to
`DETERMINISTIC_UNAVAILABLE` — nearly every constraint names a real verification
tool, so a compiled ASOP could declare gates that genuinely fire without a
model. That premise is confirmed: 67 of 70 compiled gates name a runnable tool,
and every gate actually evaluated resolved deterministically.

The refusals are also the *right* refusals mechanically: three are
`login_user ran and FAILED`, on tasks whose constraint set genuinely includes
`logged_in_user`, and the fourth is the escalation that unblocks the run without
recording a pass.

### What is still unknown

Everything about accuracy. Whether `asop-gated` reproduces PVA's +0.142, loses
like tau2, or is beaten by `asop-prompt` — **the three outcomes this experiment
was built to distinguish — is exactly what has not been measured.** The
apparatus to decide it is built, verified, pre-flighted and committed; it needs
an overnight window, not more engineering.

*(Both arms completed shortly after this was written. The result is above.)*

</details>

---

## 4. What this does and does not prove

**Does:**
- **Our ASOP artifacts do not reproduce PVA's win on `bank`.** The document as
  context is flat (+0.027, p=0.71); the document plus our gate runtime is worse
  than baseline (−0.107) and significantly worse than the same document
  unenforced (−0.134, p=0.0275).
- **The enforcement is the harmful part, and it is separable** — and the harm is
  **repairable**, which was tested rather than asserted. A gate resolving on the
  returned value instead of on tool liveness recovers +0.098 (p = 0.0127) and
  removes the loss entirely (−0.009 vs baseline, p = 1.0).
- **The failure mode was under-refusal caused by a liveness gate**, not the
  over-gating measured on tau2. A gate that asks "did the tool run" licenses the
  action whenever the agent looked, including on tasks whose precondition was
  designed to fail. Concrete, mechanically explained, and now fixed.
- 🛑 **Fixing it produced no benefit, only the absence of harm.** The repaired
  arm ties baseline and remains behind both the unenforced document and PVA,
  at 64% more tool calls. **So the evidence does not support "the concept works,
  the implementation was buggy."** One named defect was found, fixed, and
  measured; the approach still does not beat handing the agent the document.
- **The remaining gap is upstream of the gate.** `dirgraph_satisfied` stays at
  baseline (0.590 vs 0.597) across both gated arms while PVA reaches 0.791 and
  `action_order` 0.843. Our stepwise walk never engages the mechanism the
  winning arms engage, and repairing the gate did not change that.
- The port works and the §3.2 premise is confirmed: on SOPBench, unlike tau2,
  **our gates fire deterministically** — 400 of 400 evaluated, `fell_back` 0,
  no model opinion in any verdict. The apparatus is sound; the result is not an
  artifact of it.
- The compiled document does **not** reproduce the N20 false-refusal defect.

**Does not:**
- Nothing about tau2. The tau2 losses are untouched; no tau2 arm was re-run.
- Nothing about coding, which remains the untested domain and the entire
  remaining argument for the mechanism.
- Nothing about a strong executor. `gpt-oss-20b` baselines at 0.500 on `bank`
  and 0.167 on the permissible half; scaffolding that rescues a weak executor
  may constrain a strong one.
- One domain, one run per task, temperature 0, scripted user, no adversarial
  mode, service-desk shape throughout.

### The interpretive bound that applies to any number here

⚠️ **`check_tool_succeeded` is liveness, not correctness** — "did this tool run
and return ok", never "did its value satisfy the precondition". SOPBench's
`dirgraph` oracle is *also* near-liveness: N23 records **1 of 1,048** violated
decisions across all seven domains as a parameter-only violation. **A
deterministic liveness gate and SOPBench's scorer are close to measuring the
same thing.** This is not leakage — the gate reads only the ASOP text and the
visible transcript, and `_assert_no_gold` still fires — but any win here would
have been **partly structural**.

**In the event the bound bit in the opposite direction, and it is now the
finding rather than a caveat.** The scope document anticipated that alignment
between a liveness gate and a near-liveness scorer would inflate a win. What
actually happened is that liveness made the gate *actively wrong* on the half of
the task set where preconditions are designed to fail: the tool runs, the gate
passes, the executor is advanced into an action it should have refused.
`constraint_not_violated` falls from 0.721 to 0.535.

So the alignment did not flatter the arm — it destroyed it. Which means the
headline loss is **not** explained away by the confound; if anything the
confound should have helped. That strengthens rather than weakens the reading.

### A second bound, specific to the compiled document

`named_tool()` returns **one** tool per gate, so a constraint satisfiable by
either of two tools is gated on whichever is named first, and a run that
satisfied it the other way is refused.

Measured rather than assumed, and it is **smaller than it looks: exactly one**
compiled constraint has two reachable verification tools —
`pay_loan_account_balance_restr` (`get_account_balance` OR
`get_account_owed_balance`). The other OR-branch constraints in
`constraint_processes` (`no_credit_card_balance_on_card`,
`not_over_credit_limit`) belong to `cancel_credit_card` and
`pay_bill_with_credit_card`, which have no procedure at all (§2.4), so they are
never emitted. The alternatives are named in the step's prose without backticks,
so the executor can see them even though the gate cannot. A live over-gating
source, but a one-constraint one.

The compiler is **deterministic in the checkable sense**: recompiling reproduces
`bank.asop.md` byte-for-byte, and its self-check re-parses the emitted markdown
and asserts every procedure survived, every step count matches, every
intended-deterministic gate parsed as `DETERMINISTIC` with `named_tool()`
returning the exact tool named, zero steps parsed as conditional, and
`internal_get_database` never surfaced as a gate tool.

---

## 5. Reproducing

```bash
# compile the document (deterministic, no model)
~/Code/SOPBench/.venv/bin/python scripts/eval/sopbench_asop_compile.py \
    --domain bank --out evals/sopbench-bank-asop/bank.asop.md

# one arm; --output-dir MUST differ per arm or the arms overwrite each other
OPENAI_BASE_URL=http://localhost:4242/v1 OPENAI_API_KEY=placeholder \
~/Code/SOPBench/.venv/bin/python scripts/eval/run_sopbench_asop.py \
    --arm asop-gated --domain bank \
    --asop evals/sopbench-bank-asop/bank.asop.md \
    --output-dir "$SCRATCH/final/gated" --stats-out "$SCRATCH/final/gated_stats.json"

# the two N28 arms: --host-rules restores information parity (§3.3),
# --upfront swaps the presentation (§3.4). Both default off, so every arm
# published before them reproduces byte for byte without either flag.
OPENAI_BASE_URL=http://localhost:4242/v1 OPENAI_API_KEY=placeholder \
ASOP_VERDICT_LOG="$SCRATCH/hr/verdicts.jsonl" \
~/Code/SOPBench/.venv/bin/python scripts/eval/run_sopbench_asop.py \
    --arm asop-gated --value-gate --host-rules --domain bank \
    --asop evals/sopbench-bank-asop/bank.asop.md \
    --output-dir "$SCRATCH/hr/gated" --stats-out "$SCRATCH/hr/rules_stats.json"
# ... and the same with --upfront added, into "$SCRATCH/uf/gated".

# score all nine arms, paired, with SOPBench's own evaluator
ASOP_SCRATCH="$SCRATCH" \
~/Code/SOPBench/.venv/bin/python evals/sopbench-bank-asop/score_asop_arms.py

# what the executor ACTUALLY reads, which is how §3.3 was found: one file per
# turn, plus the host instructions the arm replaced
ASOP_DUMP_PROMPT="$SCRATCH/prompts" ... run_sopbench_asop.py --limit 1 ...
```

`score_asop_arms.py` re-scores T2's own raw trajectories as a self-check and
**reproduces all four published rates exactly** (0.500 / 0.552 / 0.634 /
0.642). That is what makes the new arms comparable to the old ones: provably
the same scoring code, not merely the same intention. If that check ever drifts,
the script says so and the deltas are not to be quoted.

### Environment notes that cost time

- **`timeout` does not exist on macOS.** Use a background run.
- **`--num_tasks` caps the task list**, and the output filename encodes
  model/mode/dep/fmt/tool/shuffle **only** — arms sharing an `--output_dir`
  silently inherit each other's results through `load_existing_results`.
- Bank's failure return is a **bare `False`**, not `(False, ...)`
  (`env/domains/bank/bank.py:165`). `Swarm.handle_tool_calls` computes the
  success boolean and discards it (`core.py:167-168`), so the shell reconstructs
  it; a recorder that only understood the tuple form would score every
  constraint-blocked call as a success.
- The GPU is shared. With another sweep resident, throughput ran ~2.5 min per
  task per arm rather than T2's ~5.3 tasks/min aggregate.

## Provenance

SOPBench (`arXiv:2503.08669`), code MIT, released trajectories CC BY 4.0.
**No change was made to the SOPBench checkout for this work** beyond the two
additive model-id registrations in `swarm/constants.py` that T2 already made.
The `Swarm` subclass and the `task_initializer` wrapper are installed from our
own runner by rebinding module globals; the benchmark's own files are untouched.

Tests: **1587 passed, 3 skipped** (measured 2026-09-24, after the N28 arms; the
figure was 1564 at the value-gate fix and 1583 before these four new
presentation tests — measure it, do not quote it).
`tests/test_asop_engine.py` is new: 17 tests covering the stepwise walk, the
gate scheduler, escalation, the message shim and per-task narrowing, none of
which had ever executed under `pytest` in this tree, because every `ASOPAgent`
test is `@needs_tau2` and there is no tau2 checkout here.
