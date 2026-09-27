# Does the gate refuse when the precondition genuinely was not satisfied?

**2026-09-23. The first C1 measurement in this programme scored against ground truth that
is real rather than proxy.** Every earlier C1 number — including `EVIDENCE.md`'s
TP 25 / FP 1 / FN 33 / TN 9 — rests on tau2, which scores one database hash per
conversation. `EVIDENCE.md` already states the consequence plainly: *"run-level ground
truth cannot test C1 at all … gate accuracy [is] unmeasurable here by construction, not by
oversight."* This page replaces that proxy with a deterministic per-decision oracle.

**Headline:** on SOPBench `bank`, a 20B local LLM judge scores **lift +0.655**
(95% CI [+0.583, +0.727], n=400 balanced). The same *class* of judge scored **−0.10 to
+0.33** on tau2. **H4 is supported: the measurement, not the judge, was the ceiling.**
This does **not** rescue the ASOP apparatus — see *What this does not settle*, which is
the more important half of this page.

**Updated same day:** the best-discriminating judge measured here is neither an LLM nor
Laya — it's **Jev** (TypeSafe's hosted "System One" model), at **+0.770** [+0.708, +0.832].
Jev and Laya are the same class of model (non-autoregressive, typed-decision, one forward
pass), and they land at opposite ends of this entire table. **Non-autoregressive architecture
was never the disqualifying factor for Laya's weak showing** — see the results table below.

**Added 2026-09-23 (second pass): the deployed judge and a 20× larger judge are now both
in the table, and they move the reading in opposite directions.** GLM-4.7 — the judge this
harness actually deploys, and which the first pass reported as unrunnable — scores **+0.675
[+0.603, +0.747]**, statistically indistinguishable from the 20B stand-in on lift. But it
reaches that lift at a **2.5× worse false-refusal rate (FPR 0.190 vs 0.075)**, which at
tau2's gate density is the difference between false-refusing 58% and **90%** of fully-correct
runs. **The stand-in was flattering the apparatus on the axis that actually hurts it.**

**Size buys nothing, and that is the cleanest result on this page.** `Laguna-S-2.1` —
256×4.5B, 71 GB on disk, the largest model available here — scores **+0.610
[+0.536, +0.684]**, *below* the 20B `gpt-oss-20b` it was run against as a size control. All
three LLM judges, spanning three unrelated families and roughly an order of magnitude of
scale, land in **+0.610 … +0.675: a spread of 0.065**, every CI overlapping every other.
For scale, the evidence-ordering fix in *The second correction* below moved one judge by
**+0.235 — 3.6× the entire spread across all three.** **On this task, how you render the
evidence matters several times more than which model reads it.**

## The question

C1 asks: **does a gate refuse the action when its precondition genuinely was not
satisfied, and pass it when it was?** That is a two-class claim about individual
decisions, so it needs a per-decision oracle. tau2 has none.

## Why SOPBench, and why this is not another proxy

SOPBench (arXiv:2503.08669, MIT) attaches a `directed_action_graph` to each task: for a
given action, which prerequisite/verification actions must have succeeded first, with
which parameters. Its evaluator walks that graph **per function call**. No LLM anywhere
in it.

Read in `env/evaluator.py`, not taken from the paper: the loop over `func_calls` computes
`all_prev_func_called` for each call and then collapses every one of them into a single
aggregate `dirgraph_satisfied` for the interaction. **The per-call verdict already exists;
it is simply discarded.** Exposing it is a **22-line, purely additive** patch that changes
no existing behaviour — shipped here as `per-call-groundtruth.patch`. The recon's "small
patch, not new infra" claim is therefore confirmed by doing it, not by citing it.

That the oracle is deterministic matters less than *what* it decides: the same
proposition an ASOP gate asserts — "the required prior step actually happened" — settled
by rule instead of by opinion.

## Why the pilot cost nothing

SOPBench ships ~1.9 GB of **released trajectories** (455 files, 7 domains, many assistant
models). Each carries its task, its `directed_action_graph`, its full message log and its
final database — everything the evaluator needs. So decisions are recovered by **replaying
frozen evidence**, the pattern `t1_rejudge.py` and `laya_judge_ladder.py` already use: no
agent execution, no user simulator, no environment, **no hosted-API spend**. Live-executor
runs were never needed and were not run.

## Domain choice

`bank`, chosen after surveying all seven rather than assuming: it is SOPBench's reference
domain (best-documented constraint vocabulary, so rendered preconditions can be checked by
eye), it evaluates with **zero errors**, and it yields a well-populated minority class.

| domain | interactions | gated decisions | satisfied | violated | violation rate |
|---|---|---|---|---|---|
| **bank** | 645 | **808** | 575 | **233** | 0.29 |
| healthcare | 575 | 898 | 783 | 115 | 0.13 |
| library | 329 | 301 | 135 | 166 | 0.55 |
| university | 126 | 65 | 55 | 10 | 0.15 |
| dmv | 373 | 224 | 151 | 73 | 0.33 |
| online_market | 835 | 1011 | 692 | 319 | 0.32 |
| hotel | 578 | 446 | 206 | 240 | 0.54 |

Five fixed assistant models per domain where released; **zero evaluator errors in all
seven**, so the replay is clean everywhere, not just where it was used.

## Method

808 bank decisions were extracted — each a function call the action graph actually
constrains — over **469 distinct task instances**, so the set is not one conversation
repeated. Each decision carries the evidence a verifier would see and nothing more: the
action with its rendered prerequisite tree, the transcript up to that call, and every
earlier tool call with its result. **The call being judged is excluded from its own
evidence.**

Judges are scored on a **balanced stratified sample of 400** (200 violated, 200 satisfied,
fixed seed 20260923). Balancing is legitimate rather than a thumb on the scale because
LIFT is Youden's J, which is invariant to class prevalence; it is what makes the minority
class properly powered, which `LAYA-JUDGE.md`'s power note identifies as the binding
constraint (~82 per arm for J=0.20 — this has 200).

**LIFT = P(refuse | truth=violated) − P(refuse | truth=satisfied) = TPR − FPR**, the same
definition as `gate_value.py`, `JUDGE-LADDER.md` and `LAYA-JUDGE.md`.

This design avoids the defect that got `JUDGE-LADDER.md` retracted. That experiment's
labelled set was **single-class**, so lift was zero-by-construction for every judge. Here
both classes come from the same deterministic oracle, in equal number, by construction.

## The result

| judge | n | err | TP | FP | FN | TN | TPR | FPR | **lift** | 95% CI | prec | acc | refuses |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Jev** (`jev-latest`, TypeSafe, **non-autoregressive**) | 400 | 0 | 181 | 27 | 19 | 173 | **0.905** | 0.135 | **+0.770** | [+0.708, +0.832] | 0.870 | 0.885 | 0.520 |
| **GLM-4.7** (z.ai, **deployed**) | 400 | 0 | 173 | 38 | 27 | 162 | **0.865** | **0.190** | **+0.675** | [+0.603, +0.747] | 0.820 | 0.838 | 0.527 |
| **gpt-oss-20b** (LLM, 20B) | 400 | 0 | 146 | 15 | 54 | 185 | **0.730** | **0.075** | **+0.655** | [+0.583, +0.727] | 0.907 | 0.828 | 0.403 |
| **Laguna-S-2.1** (256×4.5B, 71 GB) | 400 | 0 | 137 | 15 | 63 | 185 | **0.685** | **0.075** | **+0.610** | [+0.536, +0.684] | 0.901 | 0.805 | 0.380 |
| string-match, ALL names | 400 | 0 | 200 | 112 | 0 | 88 | 1.000 | 0.560 | +0.440 | [+0.371, +0.509] | 0.641 | 0.720 | 0.780 |
| string-match, ANY name | 400 | 0 | 48 | 1 | 152 | 199 | 0.240 | 0.005 | +0.235 | [+0.175, +0.295] | 0.980 | 0.618 | 0.122 |
| **Laya** (`laya`, zero-shot) | 400 | 0 | 55 | 13 | 145 | 187 | 0.275 | 0.065 | **+0.210** | [+0.139, +0.281] | 0.809 | 0.605 | 0.170 |
| **Laya** (`typed-decisions`) | 400 | 0 | 18 | 8 | 182 | 192 | 0.090 | 0.040 | **+0.050** | [+0.002, +0.098] | 0.692 | 0.525 | 0.065 |

⚠️ **The two Laya rows were corrected on 2026-09-23 — they originally read −0.025 and −0.045.
That was an input-truncation artifact, not a measurement of Laya.** See *The second correction*
below; the superseded verdicts are kept in `sopbench_laya_*_original.json` so the old numbers stay
auditable rather than disappearing.

⭐ **Jev added 2026-09-23, same day, same sample (verified id-for-id) — it is the best-discriminating
judge measured in this programme, LLM or otherwise.** z = +25.5, the highest of any row here.
Real hosted API (`api.typesafe.ai`), zero-shot, `jev-latest`, same typed-`choice` framing as Laya
(both are "System One" non-autoregressive models — architecture is not what separates them here).
400/400 answered, zero errors, ~0.05s/call — a full order of magnitude faster than any LLM judge
in this table and consistent with TypeSafe's own published latency claims. Confidence is properly
calibrated, not degenerate: median 0.94, but 0.89 average when correct vs 0.74 when wrong (354/46
split) — it knows more often than not when it is right. Full writeup:
`evals/tau2-airline-asop/JEV-JUDGE.md`.

**This reframes the Laya finding.** The failure earlier attributed to "non-autoregressive judges
lack a reasoning chain for compound preconditions" (H3's original hypothesis) does not hold for
Jev on the identical task shape — so that explanation was wrong, or at least incomplete. What
actually separates Jev from Laya here is unmeasured directly, but the likely candidates are Jev's
scale (TypeSafe does not publish parameter counts, but pricing and latency both suggest it is far
larger than Laya's 421M) and/or training data coverage — not the non-autoregressive architecture
itself, which both share. **The architecture was never the ceiling; at minimum Laya specifically
was undertrained or under-scaled for this task, not disqualified by being non-autoregressive.**

**Four judges discriminate, and none is close to noise** — z = +25.5, +18.3, +17.9, +16.2.
On tau2 the best judge ever measured reached +0.33 (itself confounded by sharing the executor's
weights) and two of three swapped judges were negative. **No judge here shares weights with
anything it grades** — SOPBench's trajectories come from five other assistant models — so this
is the first C1 reading in the programme with no same-family confound at all.

**The size control does not merely fail to help; it comes last.** Laguna-S-2.1 is ~13× the
parameter count of `gpt-oss-20b` and scores 0.045 *lower*. Its CI overlaps both others, so the
honest reading is "indistinguishable," not "worse" — but the hypothesis it was run to test,
that a bigger judge would break out of the band, is **refuted in the only direction that
mattered**: there is no evidence of a size effect, and what point estimate there is points the
wrong way. Per-action (which holds the label distribution fixed) tells the same story — Laguna
leads on two of three actions and trails badly on the third, which is what noise looks like,
not scale.

**Equal lift, different judges: the aggregate hides a large and significant disagreement.**
GLM-4.7 and `gpt-oss-20b` have overlapping lift CIs, which invites the conclusion that the
choice of judge does not matter. Paired on the identical 400 decisions, that conclusion is
wrong in both directions at once:

| on decisions where the two disagree | GLM-4.7 right | gpt-oss-20b right | exact p |
|---|---|---|---|
| truth = **violated** (should refuse) | **40** | 13 | **0.0003** |
| truth = **satisfied** (should pass) | 3 | **26** | **<0.0001** |

They agree on only **318/400 (79.5%)**. GLM catches substantially more real violations *and*
falsely refuses substantially more correct work; the two effects very nearly cancel in
Youden's J and are both individually significant. **Lift alone is the wrong summary for
choosing a gate judge**, because the two error types do not cost the same thing — a missed
violation costs one unsafe action, a false refusal costs a run (N14). On that asymmetry the
*lower-lift* judge is the better deployment choice, which the lift column cannot express.

**The two string-match controls exist so the LLM number can be bounded, and they do their
job.** A matcher that ignores the AND/OR structure and all parameters reaches +0.440 —
but only by refusing **78% of everything** (FPR 0.560). The conservative variant holds
FPR to 0.005 and collapses to TPR 0.240. **Neither achieves both**, and both LLM judges
do: TPR 0.730 at FPR 0.075 (`gpt-oss-20b`), TPR 0.865 at FPR 0.190 (GLM-4.7). Note that
GLM-4.7's FPR is now within a factor of three of the *deliberately dumb* ALL-names matcher's
0.560 — the deployed judge is closer to the trivial control on false refusals than its lift
suggests. The per-action breakdown is the sharper version of this,
because it holds the action type fixed and so removes "the judge is just recognising which
action it is" as an explanation:

| action (≥10 per class) | GLM-4.7 | gpt-oss-20b | Laguna-S-2.1 | string-match ALL | string-match ANY |
|---|---|---|---|---|---|
| `get_account_balance` | **+0.55** | **+0.70** | **+0.73** | +0.00 | +0.10 |
| `get_account_owed_balance` | **+0.82** | **+0.50** | **+0.46** | +0.00 | +0.14 |
| `get_credit_card_info` | **+0.55** | **+0.55** | **+0.64** | +0.00 | +0.07 |

Within a single action type the matchers carry essentially no signal while the LLM judge
carries a lot. The discrimination is in the judgment, not in the label distribution.

**Laya discriminates — weakly, and it still loses to the string matcher.** Both checkpoints
clear zero, but `laya`'s +0.210 is under a third of `gpt-oss-20b`'s +0.655 and is beaten by
the deliberately dumb string-match ALL control (+0.440). That is the sharper way to put it:
**a 421M classifier reading this evidence carries less signal than a matcher that greps for
prerequisite names** — though the matcher only gets there by refusing 78% of everything,
where Laya refuses 17%. It still answers `held` on **332 of 400** (83%) regardless of truth,
at median confidence 0.065 and never above 0.524 — honestly uncertain rather than
confidently wrong, which remains the one property worth taking seriously.
`LAYA-JUDGE.md` left `laya-typed-decisions` untested as a bound ("the variant closer to this
task's shape"); it is now tested at **+0.050 — still worse than the base checkpoint**, and
it holds the larger 1024-token window, so it cannot be excused by truncation. That bound is
closed, and it is the finding that survived the correction below intact.

## The second correction — the Laya rows measured a truncated input, not a judge

**Recorded in full because the first version of this page called it a refutation.** Both Laya
rows originally read negative (−0.025 and −0.045) and this page concluded *"Laya is refuted
again, now against real ground truth and at proper power."* That conclusion was wrong, and the
reason was in `sopbench_judge.py`, not in Laya.

`laya.common.build_sequence` truncates the state's **tail**:

```python
st = st[-room:] if truncate_left else st[:room]   # truncate_left defaults False; Agent never passes it
```

The base checkpoint allows 512 tokens total and reserves 192 for the question head, leaving
**320 for evidence**. This script put `TOOL HISTORY` **last** — the exact material this page's
ground truth is defined over ("was `login_user` called first, with this username"). Measured
with Laya's own tokenizer over all 808 bank decisions: median body 443 tokens, **77% overflow
the 320-token budget, and on 63% the tool history is entirely unseen.** The overflow is
**class-correlated** — 85% of `violated` bodies overflow against 74% of `satisfied`, because
violations carry more prior tool calls. Same defect shape as the `max_tokens` truncation
above, found the same day, one section apart.

**Re-run: identical 400 cells, identical seed, identical question, zero training — only the
order of the three evidence blocks changed.**

| checkpoint | original (TOOL HISTORY last) | **corrected (evidence first)** | paired Δ | P(Δ≤0) |
|---|---|---|---|---|
| `laya` | −0.025 [−0.082, +0.032] | **+0.210** [+0.139, +0.281] | **+0.235** [+0.150, +0.320] | 0.0000 |
| `typed-decisions` | −0.045 [−0.082, −0.008] | **+0.050** [+0.002, +0.098] | **+0.095** [+0.050, +0.145] | 0.0000 |

**The control that rules out prompt superstition.** Split the sample by whether the original
body actually overflowed. Where it did, reordering changes what the model can see; where it
fit, reordering changes only position.

| subset | original | corrected |
|---|---|---|
| **overflowed** (n=310) | −0.054 [−0.128, +0.019] | **+0.215** [+0.131, +0.300] |
| **fit in context** (n=90) | +0.000 [+0.000, +0.000] | +0.097 [−0.007, +0.201] |

The gain is concentrated where truncation was real. The fit-in-context subset is also positive
but its CI spans zero on 31 violated cells, so pure order-sensitivity is not ruled out — it is
just not what is doing the work.

**A larger context window is NOT the fix, and assuming it was would have been the next
mistake.** `max_len` is a Laya config value, not an architectural limit — ModernBERT-large's
`max_position_embeddings` is 8192 — so raising it looked free. Measured, it is not:

| checkpoint | ordering | native window | `max_len=2048` |
|---|---|---|---|
| `laya` (native 512) | original | −0.025 | +0.075 |
| `laya` (native 512) | evidence-first | **+0.210** | +0.045 |
| `typed-decisions` (native 1024) | original | −0.045 | −0.050 |
| `typed-decisions` (native 1024) | evidence-first | **+0.050** | +0.030 |

Running these checkpoints long **destroys most of the ordering gain**. They are RLCD-trained
at 512/1024 and degrade outside that length. The correct fix is evidence-first ordering **at
the native window**; `--laya-max-len` exists only so this claim stays checkable.

**What changed in the conclusion, and what did not.** Laya is no longer "refuted" — it
discriminates. But it is still beaten by a 20B LLM three-to-one and by a string matcher, so
**nothing here promotes it to a usable judge**, and `typed-decisions` is still worse than base
despite holding the bigger window. The headline of this page — the LLM judge's +0.655 and H4 —
is untouched: `gpt-oss-20b` and both string-match controls never went through Laya's tokenizer
and were never affected.

**Made checkable rather than remembered.** `--laya-order original` reproduces the superseded
numbers, and the superseded per-decision verdicts are kept as `sopbench_laya_*_original.json`.
The lesson generalises past this page: **two of this page's three corrections are truncation
with a class-correlated bias** (`max_tokens=160` above, and this one), and both were invisible
in the summary table while being visible in one line of the input pipe. When a judge looks
undiscriminating, check what it was actually shown before concluding anything about it.

## Where the deployed judge's extra false refusals come from — one nameable cause, not diffuse noise

GLM-4.7's 38 false refusals are not spread across the space of hard cases. **37 of 38 (97%)
refuse for the same reason**: they demand a visible tool call for an `internal_*` prerequisite
node — `internal_check_username_exist`, `internal_get_database` — that the agent never calls
and never could, because those nodes are the environment's own database checks, not agent
actions. The oracle counts them satisfied by walking the graph; the rendered evidence shows
them as prerequisites; the judge, told to *"PASS only if you can point to a specific tool call
and result in the evidence,"* correctly follows its instruction and refuses. The same
signature accounts for 12 of `gpt-oss-20b`'s 15 (80%).

Both judges are doing what they were told. **The defect is in what the gate asks them to
verify** — a precondition stated in terms of evidence the agent cannot produce is unpassable
by construction, and a judge that refuses it is right about the prompt and wrong about the
world. This is the same lesson as `JUDGE-LADDER.md`'s 55%-degrade-to-opinion finding arriving
from the opposite side: there the precondition was too vague to check, here it is too precise
to satisfy.

(Tracked as **N20**.) It also means **the FPR gap between these two judges is mostly not a quality gap.** GLM-4.7 is
more literal about the instruction, which costs it on exactly the prerequisites where literal
compliance is wrong. The tractable next move is gate authoring — render only agent-reachable
prerequisites, or mark environment-internal nodes as not-evidence-bearing — and that is
cheaper than changing judges. **It is untested here, and it is a hypothesis about the
remaining 30%/90% false-refusal load, not a fix that has been measured.**

## A correction made in flight, recorded because it nearly became the result

The first pass reported **lift +0.688 with 75/400 "judge errors"**. Those errors were not
judge failures. `gpt-oss-20b` is a harmony-format reasoning model that emits its verdict
*last*; at `max_tokens=160` it ran out mid-reasoning and returned empty content with
`finish_reason: "length"`. Truncated prompts were systematically **longer** (mean 3615 vs
2671 chars), and longer evidence skews violated (more prior tool calls), so discarding
them removed **28% of the violated class against 9.5% of the satisfied class** and
**inflated lift**. Re-run at `max_tokens=800`: **0 truncations, 400/400 judged, +0.655**.

The bias was modest (0.033) but it was real, class-correlated, and invisible in the
summary table. `sopbench_judge.py` now records `finish_reason` and warns loudly on any
truncation, so this cannot be silently re-made.

## The "no balance" claim was false, and it blocked two experiments for a day

The first pass of this page stated, as fact: *"z.ai returns `429 / code 1113 Insufficient
balance or no resource package`, so GLM-4.7 could not be run."* **The account was never
empty.** The request was going to the wrong door.

z.ai serves two different APIs. The pay-per-token one
(`/api/paas/v4/chat/completions`, OpenAI-shaped) bills against a **token balance**. The
Coding Plan one (`/api/anthropic/v1/messages`, Anthropic Messages-shaped, Bearer auth) bills
against a **subscription**. This account holds the subscription and no token balance, so the
pay-per-token path answers `1113` — accurately, about a product this account does not buy.
Measured 2026-09-23, same key, same minute, same model:

| route | auth | result |
|---|---|---|
| `/api/anthropic/v1/messages` | `Authorization: Bearer` | **HTTP 200**, real completion |
| `/api/anthropic/v1/messages` | `x-api-key` | **HTTP 200** (also works — see below) |
| `/api/paas/v4/chat/completions` | `Authorization: Bearer` | **HTTP 429, code 1113** |

**The working route was already in this repo**, in `agentco_harness/executor.py`, which has
driven the deployed GLM-4.7 executor through it since 2026-07-14 and documents the base URL,
the Bearer requirement and the explicit `glm-4.7` model name in comments. The pilot did not
have to discover anything; it had to read the code next to it. Running the full 400-decision
set afterwards took **six minutes**.

**Why this counts as a defect and not a footnote.** The claim did not stay on this page. It
propagated into `phase-2-next.md` as a 🛑 **BLOCKED** status on **EXP-1 and EXP-2** — the two
experiments that separate *the completion stance* from *gating itself* (H1) and *causal* from
*diagnostic* gates (H2), which is to say the two most decision-relevant experiments queued —
and into an open decision asking the principal to **spend money recharging an account that did
not need recharging**. An unverified sentence became a schedule, a blocker, and a budget
request. That is `DECISIONS.md` **N10** exactly: *a claim carried in prose, never re-checked.*
It is recorded here rather than quietly fixed because this project treats the uncorrected
version as the actual bug. (Tracked as **N19**.)

**Made harder to re-make:** the endpoint distinction, the measurement above, and the reason
the two are not interchangeable now live in a comment on `_ZAI_URL` in `sopbench_judge.py`,
next to the code that would get it wrong — not only in prose on this page.

*Secondary correction, same shape:* `executor.py`'s comment asserts z.ai **rejects**
`x-api-key` ("verified 2026-07-14"). Row 2 above shows it returns 200 today. The Bearer
route is still the right one to standardise on, but that comment is now stale and should not
be cited as current evidence.

## Three corrections in one day, all the same shape: measure the plumbing before believing the metric

This page now carries three corrections, and it is worth naming what they have in common,
because the common factor is more useful than any of them individually:

| # | Reported as | Actually was | Cost |
|---|---|---|---|
| 1 | `gpt-oss-20b` lift **+0.688** | `max_tokens` truncation, class-correlated | inflated lift 0.033 |
| 2 | Laya **refuted** (−0.025 / −0.045) | evidence-ordering truncation, class-correlated | understated lift **0.235** |
| 3 | z.ai has **no balance** | wrong endpoint (pay-per-token vs subscription) | **two experiments blocked, a spend request raised** |

**None of the three was a wrong conclusion from good data. All three were plumbing
presented as a finding** — a truncated context, a truncated context, and an unmade API
call, each of which produced a number (or a blocker) that looked like a fact about judges
and was a fact about the harness. Two of the three were *class-correlated*, which is what
made them dangerous rather than merely noisy: they moved the result in a consistent
direction, so nothing about the summary table looked odd.

**What this changes going forward.** The cheap check that would have caught all three is the
same one: *before believing a metric, verify the measurement apparatus delivered what you
think it delivered.* Concretely, and now done as a matter of course for the two arms added in
the second pass — **400/400 responses parsed, `finish_reason`/`stop_reason` recorded on every
single call, zero truncations, and the endpoint probed for a 200 before the run rather than
after a failure.** That check is three or four lines and it has now failed more often on this
page than it has passed, which is the argument for making it a precondition rather than a
post-mortem.

## What this does and does not settle

**Settles:** tau2's run-level ground truth was a real and binding ceiling on measuring C1
(**H4 supported**). A judge class that looked undiscriminating there discriminates
strongly here, on a per-decision oracle, at n=400 with both classes present. Any future
claim that "judged gates cannot discriminate" must now be qualified by *on what kind of
precondition*.

**Settles, provisionally:** the useful distinction is not judge architecture or size but
**what the precondition asserts**. SOPBench preconditions are checkable facts about what
happened ("was `login_user` called first, with this username"). tau2 airline/retail
preconditions are largely opinions about state ("is this customer eligible"), which
`JUDGE-LADDER.md` already noted is why 55% of those gates degraded to a model opinion.
This is the sharp form of that file's own closing hypothesis — *gate mechanisms work;
model-judged gates on ambiguous operational rules do not* — and it now has a positive
control rather than only negative results.

**Does NOT settle — and this is the half that matters most:**

- **It does not rescue the ASOP apparatus.** C1 is "can a gate discriminate". C2/C3 are
  "does gating make the run better". The powered retail arm (**v6 loses by −0.126,
  p = 0.000, n=159**) is untouched by this page. A judge with +0.655 lift can still make
  runs worse — **N14's mechanism, that refusal language raises the zero-write rate because
  agents abandon rather than stall, is completely unaddressed here.** Nothing below should
  be read as reopening the retail result.
- **The false-refusal rate compounds, and for the DEPLOYED judge it compounds much
  harder.** These bank interactions carry only ~1.72 gated decisions each (median 2).
  Assuming independence across gates:

  | judge | FPR | ≥1 false refusal @1.72 gates (bank) | @11 gates (tau2) |
  |---|---|---|---|
  | `gpt-oss-20b` (the stand-in) | 0.075 | 12.5% | **57.6%** |
  | **GLM-4.7 (deployed)** | **0.190** | **30.4%** | **90.2%** |
| `Laguna-S-2.1` (size control) | 0.075 | 12.5% | 57.6% |

  **This is the single most consequential number added in the second pass.** Every
  statement of the form "the judge false-refuses 58% of correct tau2 runs" was computed
  from a stand-in that is 2.5× more conservative than the judge actually deployed. On the
  judge that really ran the airline and retail arms, **roughly nine in ten fully-correct
  conversations eat at least one false refusal.** That does not by itself prove the
  refusals caused the −0.240 / −0.126 losses — N14's abandonment mechanism is the proposed
  route and it is still unablated — but it removes "the gates rarely fire on good runs" as
  an available defence. Gate *density* × the deployed judge's FPR is more than sufficient
  to explain the observed harm.
- ~~**The deployed judge was not tested.**~~ **✅ CORRECTED AND CLOSED 2026-09-23 — it is
  now tested, and the reason it wasn't is a defect in this page, not a fact about the
  world.** See *The "no balance" claim was false* above. `gpt-oss-20b` remains in the
  table as the stand-in (it is one of `JUDGE-LADDER.md`'s own three judges, which is why
  it was chosen: it makes the tau2 and SOPBench numbers comparable for *the same model*),
  but it is no longer the only LLM judge here.
- **One domain, offline replay.** No live agent was gated by these verdicts, so
  this measures judgment quality on fixed evidence and nothing else — the same limitation
  `t1_rejudge.py`'s docstring states.

**What would falsify the reading.** If `online_market` (1011 decisions, 319 violated,
already surveyed and free) shows near-zero lift for the same judge, then bank is a
favourable special case and the "checkable vs opinion" distinction is wrong. If a live
SOPBench arm gated by this judge fails to beat ungated on pass rate, then C1 discrimination
does not convert into C2/C3 value even where discrimination is real — which would be the
strongest possible statement of the programme's central problem.

## Bounds

n=400 balanced (of 808 available). One domain (`bank`), **three LLM judges** (`gpt-oss-20b`,
GLM-4.7, Laguna-S-2.1), two Laya checkpoints, **two evidence orderings and two context windows for Laya only** —
every other arm was run once, in one framing (the first bullet below is why that matters more
than it sounds). One prompt design otherwise, offline replay only. Ground truth is *procedure
compliance* — "were the required prior actions performed, with matching parameters" — not
"was the agent's overall answer good"; that is the intended scope, and it is narrower than
"the gate was correct" in the everyday sense. The released trajectories come from five
assistant models, so decisions reflect how *those* models fail, not how any agent could.
Action types are unevenly distributed across truth classes (e.g. `authenticate_admin_password`
is 228 satisfied vs 9 violated), which the per-action table above controls for on the three
actions where both classes clear 10.

**Two bounds the second pass makes sharper rather than looser:**

- 🛑 *One prompt design* is now the binding limitation, and it is **larger than the effects
  being compared.** The false-refusal forensics above show both LLM judges failing on the
  same instruction — "point to a specific tool call" — against prerequisites no tool call can
  satisfy. Every judge here was given that same instruction, in one evidence ordering, run
  once. **The Laya correction above measured what that can be worth: reordering the evidence,
  changing no model and training nothing, moved lift by +0.235.**

  Set that against what this page is comparing. **The three LLM judges span 0.065 in lift,
  end to end. The measured formatting effect is +0.235 — 3.6× the whole spread, and ~12× the
  GLM-vs-gpt-oss gap.** Formatting sensitivity has *not* been characterised for the
  `gpt-oss-20b`, GLM-4.7, Laguna or string-match arms — it was found for Laya only because a
  truncation bug forced someone to look. So the honest statement of the size/family result is
  not "size and family don't matter" but: **"across three unrelated families spanning 20B to
  256×4.5B, lift varies by less than a third of what one untested prompt-formatting decision
  is already known to be worth."** That is still a real finding — it is strong evidence the
  ceiling is not the judge — but it is evidence about where the variance *isn't*, and it does
  **not** license ranking these three judges by lift. The FPR differences are a different
  matter: those are large, paired, and significant.
- *Same-weights confounding does **not** apply here*, unlike on tau2. SOPBench's released
  trajectories come from five assistant models, none of them GLM-4.7, `gpt-oss-20b` or
  Laguna, so **every judge in this table is lineage-independent of the work it grades.** That
  is a genuine improvement over `JUDGE-LADDER.md`'s +0.33, which graded its own family — and
  it is also why the near-identical lift across three unrelated families is informative
  rather than an artifact of shared weights.

## Reproducing

```bash
# 1. patch SOPBench (additive, +22 lines) so per-call verdicts survive
cd ~/Code/SOPBench && git apply ~/Code/agentco-harness/evals/sopbench-bank-asop/per-call-groundtruth.patch

# 2. extract decisions + deterministic ground truth (no API, no agent)
~/Code/SOPBench/.venv/bin/python scripts/eval/sopbench_extract.py \
    --domain bank --out /tmp/sopbench_bank.jsonl

# 3. judges — all local, all free
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sopbench_bank.jsonl \
    --llm openai/gpt-oss-20b --baseline --out /tmp/sopbench_results
~/Tools/laya-runtime/.venv/bin/python scripts/eval/sopbench_judge.py \
    --decisions /tmp/sopbench_bank.jsonl --laya laya --out /tmp/sopbench_results
~/Tools/laya-runtime/.venv/bin/python scripts/eval/sopbench_judge.py \
    --decisions /tmp/sopbench_bank.jsonl --laya typed-decisions --out /tmp/sopbench_results

# the superseded, truncation-biased ordering (and the max_len claim), kept checkable
#   --laya-order original        reproduces -0.025 / -0.045
#   --laya-max-len 2048          reproduces the "bigger window makes it worse" row
# 3b. the DEPLOYED judge, over z.ai's Coding Plan (subscription, not per-token).
#     Needs ZAI_API_KEY in env or ~/.claude/.env. ~6 min at 4 workers.
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sopbench_bank.jsonl \
    --zai glm-4.7 --out /tmp/sopbench_results

# 3c. the size control. 71 GB — unload everything else first or it will not fit.
lms unload --all && lms load poolside/laguna-s-2.1 --context-length 16384
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sopbench_bank.jsonl \
    --llm poolside/laguna-s-2.1 --out /tmp/sopbench_results
```

The 400-decision stratified sample is a pure function of the decision set and
`--seed 20260923`, so step 2 regenerates **the identical sample** every judge here was
scored on — verified by comparing ids against the committed `sopbench_*.json`, identical in
both membership and order. The judges in this table are therefore comparable to each other,
not merely to the same population.

### Files here

`C1-GROUND-TRUTH.md` (this page) · `per-call-groundtruth.patch` (the +22-line evaluator
patch) · `sopbench_*.json` (per-decision verdicts behind every number above, so the table
is auditable without re-running) — including `sopbench_zai_glm-4-7.json` (the deployed
judge) and `sopbench_poolside_laguna-s-2.1.json` (the size control), each carrying the
`finish_reason` of every one of its 400 calls so the zero-truncation claim is checkable
rather than asserted. The extracted decision set itself is **not** committed —
it is 1.8 MB and regenerates in seconds from step 2.

### Attribution

SOPBench: *"SOP-Bench / SOPBench"*, arXiv:2503.08669 — code MIT, benchmark data and
released trajectories **CC BY 4.0**. The `sopbench_*.json` files here are derived
measurements over that data (decision ids, its deterministic ground-truth labels, and this
project's judge verdicts). Changes made: a 22-line additive patch exposing the per-call
prerequisite verdict the evaluator already computes.

### Environment notes that cost time to rediscover

SOPBench needs its own venv: system `python3` is 3.9 and `env/helpers.py` uses `match`.
LM Studio serves on **:4242**, not :1234. Do not use `zai-org/glm-4.7-flash` — its chat
template is broken in this build and it emits `<|user|>` and loops. `gemma-4-31b` and
`qwen3.6-27b` return the answer in `reasoning_content` with `content` empty; the script
reads both, and anything that does not will score them as 100% errors.

**z.ai has two APIs and only one of them works for this account** — the single most
expensive thing rediscovered here. `/api/anthropic/v1/messages` (Anthropic Messages shape,
`Authorization: Bearer`, explicit `glm-4.7` model name) is the Coding Plan subscription and
answers 200. `/api/paas/v4/chat/completions` is pay-per-token and answers `429 / 1113`,
which **reads like an empty account and is not one**. GLM-4.7 replies in Anthropic content
blocks with `thinking` before `text`; take the verdict from the `text` blocks, because
`parse_verdict` takes the last PASS/FAIL token and the reasoning restates both options
before committing.

**Laguna-S-2.1 is 71 GB and LM Studio will not page it in beside anything else** — `lms
unload --all` first. It runs ~6 s/decision (~40 min for 400) and, unlike the other local
models, puts its verdict in `content` normally.
