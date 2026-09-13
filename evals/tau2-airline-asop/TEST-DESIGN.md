# What are we actually trying to prove, and what test proves it?

Written 2026-09-12, after three runs that measured the wrong thing in three
different ways. This is the design document the earlier runs should have had.

## The claim, split into three

"ASOPs work" is not testable. It is three claims with a dependency order, and
we have been testing the last two while the first was never established.

**C1 — VERIFICATION.** *An ASOP tells you when the work was not done properly.*
A gate refuses runs that are going wrong and passes runs that are going right.
This is the claim the whole spec rests on: "your agent says it ran the tests —
did it?"

**C2 — EVOLUTION.** *A procedure improves when its steps are revised from the
evidence its own gates produced.* Round two beats round one, and the
improvement traces to specific adjudicated failures.

**C3 — ECONOMICS.** *A gated procedure lets a small cheap model do work that
otherwise needs a large one.* The commercially interesting claim, and the one
most tempting to reach for early.

**C2 and C3 depend on C1 — but only through one channel, and the first draft of
this document overstated it.** Cross-review (below) pointed out that a
procedure can evolve from a second, independent channel: raw execution
behaviour. Loops, escalation streaks, parse failures and 42-turn jams are
diagnosable straight from a transcript without trusting any PASS/FAIL verdict.

This project's own best revision came from exactly that channel. The missing
routing table was found by reading a transcript, not by aggregating verdicts.

So the honest statement is narrower: **automated revision from aggregated
verdict statistics requires C1. Qualitative revision from execution behaviour
does not.** Those are two different loops and the experiment should stop
conflating them. C3 still depends on C1 — a gate that cannot tell good work
from bad cannot substitute for capability.

## Where we actually are

C1 is **unestablished**, and the first draft of this document claimed more than
that. Across 15 runs in four configurations:

| | mean gate refusals |
|---|---|
| runs that **passed** | 4.25 |
| runs that **failed** | 4.27 |

Zero of 15 runs passed through the gates cleanly.

The first draft read this as "the gate is not a verifier, it is a constant".
That overreached, in three ways worth recording rather than quietly deleting:

1. **Wrong unit.** Gate discrimination is a property of each *decision* — did
   this verdict match this precondition's true state? This measures a run-level
   *count* against a whole-task outcome. Runs fail for reasons no gate touches
   (bad tool arguments, max turns, a mutation error) and succeed despite a
   wrongly-passed gate when the error did not reach the final hash.
2. **The count is partly an artifact.** `max_refusals = 3` means a genuinely
   stuck step mechanically produces exactly three refusals before escalation,
   regardless of whether any judgment was accurate. Part of "mean refusals" is
   the escalation policy, not a measurement.
3. **"0 of 15 clean" is not evidence of zero signal.** It is what compounding
   produces. A verifier at 0.85 per-decision accuracy across ~8 sequential
   gates clears a whole run about 27% of the time; add refuse-bias and it
   approaches zero. An imperfect-but-real verifier looks exactly like this.

And the same document argued that T2 needs 4 trials × 13 tasks before a round's
outcome is signal, then drew a strong causal conclusion from n=15 with 4
passes. That standard has to apply here too.

**What the v2 regression does and does not show.** The first draft attributed
it entirely to noise-driven revision. A second cause was live and is documented
in the adapter's own comments: proposals only ever ADD text, and a longer
system prompt degrades a 20B model by itself. Re-running v2 with that leak
fixed scored 0.000 against the leaky 0.250 — which argues against prompt growth
being the explanation, but at n=4 and one trial it does not settle it. Two
explanations remain live and the experiment cannot currently separate them.

What IS solid, and points at the verifier rather than at the concept:

1. **The verifier is biased to refuse.** Its prompt says "absence of evidence
   is FAIL", which on a truncated transcript is nearly always true.
2. **It sees a window, not the run.** Evidence is the last 12 messages, spanning
   step boundaries and retry churn, so a step's own refusal blocks can evict the
   tool evidence that would have satisfied it.
3. **It may not see the evidence at all.** `Evidence.tool_calls` carries tool
   NAMES for the current turn — no arguments, no results. A verifier judging
   preconditions off the executor's narration is not independent of the
   executor, whatever the identity check says.
4. **55% of gates are judged, not deterministic.** The ASOPs declare
   `deterministic` on steps that name no re-runnable check, so a weak model
   guesses where a command should have decided.

Point 3 is the serious one. It is the core invariant — the executor does not
attest its own work — holding on paper while leaking in practice.

## The tests, in dependency order

### T1 — Does the gate discriminate? (C1)

**This is not a benchmark score.** It is a diagnostic-accuracy question, and it
needs no new arm — only runs we already produce.

- **Unit: one gate decision**, not one run. Did this verdict match whether the
  step's precondition actually held? A run-level refusal count answers a
  different question and is confounded with run length and with the escalation
  policy's fixed three refusals per stuck step.
- Ground truth per decision has to come from somewhere other than the verifier.
  Two honest sources: the subset of preconditions that ARE checkable against
  the database, and a small hand-labelled sample. Both are work; neither is
  optional, because a classifier evaluated against its own output measures
  nothing.
- Report precision and recall of refusals against that label, with a
  confidence interval. Not a difference of two means with no interval, which is
  what the first draft did.
- **Gate on the gate:** the verifier must be able to say both yes and no. If no
  run ever passes cleanly, the check cannot pass and nothing downstream is
  interpretable. This is the same two-directional proof `taubench.py selfcheck`
  already applies to the scorer, applied to the verifier.
- Fix first, and each fix has a trap attached:
  - **Evidence is the wrong shape.** `Evidence.tool_calls` carries only the
    current turn's tool NAMES — no arguments, no results. So the verifier may
    be judging preconditions largely off the executor's own narration, which
    weakens executor-verifier separation in practice even though the identity
    check passes on paper. That is the core invariant, undermined quietly.
  - **The window is scoped wrong.** The last 12 messages spans step boundaries
    and retry churn, so a step's own refusal blocks can evict the tool evidence
    that would have satisfied it. Scope evidence to the current step's turns,
    not to a fixed count.
  - **Widening the window will make T1 look fixed for the wrong reason.** See
    the Goodhart section below before believing an improved gap.

**Until T1 separates, T2 and T3 are not worth running.**

### T2 — Does the procedure improve across rounds? (C2)

- Adjudicate on DEV, measure on TEST. Never revise against the tasks reported.
- Report the slope across v1..vN, with each version's proposal traced to the
  refusals that produced it.
- **A negative round is a valid result and must be reported.** v2 losing to v1
  is the current state and it is more informative than a win would have been:
  it shows the loop is not rigged to look good.
- Power: at 4 tasks and 1 trial, two task flips span the entire observed range.
  4 trials on 13 TEST tasks is the minimum where a round's outcome is a signal
  rather than a coin.

### T3 — Does the procedure substitute for model size? (C3)

The comparison that makes this claim, and the arm we do not yet have:

| arm | model | document | execution |
|---|---|---|---|
| small, bare | gpt-oss-20b | none | single shot |
| small, prose | gpt-oss-20b | prose policy | single shot |
| **small, prose, gated** | **gpt-oss-20b** | **prose policy** | **stepwise, gated** |
| **small, ASOP, gated** | **gpt-oss-20b** | **ASOP** | **stepwise, gated** |
| frontier reference | published runs | prose policy | single shot |

**The third arm is the one the first draft forgot**, and leaving it out would
have repeated round one's exact mistake in the arm that matters most.
Comparing single-shot prose against stepwise-gated ASOP confounds the document
with the scaffolding, and the scaffolding is doing real work independent of any
document: up to three retries per step is more inference-time attempts than a
single-shot run gets at the whole task, and rebuilding the prompt each turn
around the current step gives a recency advantage that a 20B model holding a
full prose document across fifteen turns does not have. Chunk the prose and
gate it the same way and it may capture most of the gain.

**The claim is "ASOP, gated beats prose, gated."** Anything less isolates
nothing. Trial counts inherit T2's standard; an underpowered T3 is round one
again.

Beating a frontier model is a headline, not a result — different model,
different conditions, different trial count. The published figures below are a
REFERENCE LINE, never a like-for-like comparison, and any write-up must say so.

Recomputed DB-only from the runs shipped in the τ²-bench checkout (their
default reward mixes in an LLM-judged communication score; ours does not), on
our exact task sets, 4 trials each:

| model | full 50 | our 13 TEST | our 4 |
|---|---|---|---|
| claude-3-7-sonnet | 0.510 | 0.385 | 0.312 |
| gpt-4.1 | 0.580 | 0.462 | 0.500 |
| gpt-4.1-mini | 0.555 | 0.500 | 0.562 |
| o4-mini | 0.595 | 0.346 | 0.312 |

Reproduce with `scripts/eval/baselines.py`.

## Is τ²-bench the right instrument?

Partly, and it is worth being honest about where it is not.

**Good for**: a realistic multi-turn business procedure, a mutable database, a
deterministic outcome scorer nobody can argue with, and published baselines on
the same tasks.

**Bad for C1 specifically**: this domain's rules are mostly assertions about
state that no command can re-run — "cabin class must match across flights" is a
rule, not a check. That forces 55% of gates onto a model judge, which is the
weakest possible way to test whether gates discriminate.

**A second domain would fix that.** In a software-task benchmark the gate is
literally `run the tests` — deterministic, re-runnable, and not a matter of
opinion. `scripts/eval/swebench.py` already exists. Testing C1 where gates can
actually be deterministic would separate "gates do not work" from "model-judged
gates do not work", and those are very different findings.

## The way this programme most likely produces a result that is wrong

Named in cross-review, pre-registered here because a failure mode you write
down after the fact is not a safeguard.

**Goodharting the verifier on narration density.**

The mechanism, step by step:

1. T1 says the verifier is refuse-biased and sees too little, so we widen its
   window and soften the bias.
2. The passed/failed refusal gap widens — but partly for a tautological reason.
   Runs that are going well produce more, cleaner, easier-to-find evidence *by
   construction*. A verifier reading a wider window gets better at detecting
   "this transcript smells successful", which is not the same as judging
   whether a precondition held.
3. T1 now "passes its own gate-on-the-gate".
4. T2 runs against that verifier. The proposer sees refusals landing on steps
   where the executor did not *say* enough, and revises the ASOP to make it
   narrate more: "confirm X and state that you have confirmed it."
5. Verdict pass rates climb every round. The T1 gap widens further. Every
   instrument we built shows improvement.
6. **ENV pass^k does not move** — because narrating a check is not performing
   one — and there is a ready, genuinely plausible excuse for that: a 20B model
   really is near its floor on this benchmark.

An improvable gameable proxy, a real ground truth that is hard to move, and a
prepared explanation for why the ground truth stayed flat. That is the standard
configuration for optimising what you can measure and calling it validation of
what you cannot.

**The guards, fixed now rather than later:**

- **ENV pass^k is the primary metric and the only one a headline may cite.**
  Verdict quality, refusal gaps and gate-agreement are diagnostics. If ENV is
  flat, the round did not improve the procedure, whatever the proxies say.
- **Track narration density per version** — executor tokens and
  self-referential confirmation phrases per completed step. A version that
  improves verdicts while growing narration is the failure mode, visible.
- **A revision that only adds narration is rejected at proposal time.** The
  proposer may make a precondition explicit; it may not instruct the executor
  to announce that it did something.
- **"Still floor-limited" is not available as an explanation unless the bare
  arm moves too.** If the floor is the cause, it binds every arm.

## Scope limit on a second domain

Moving to a software-task domain where the gate is `run the tests` isolates
"judged gate" from "gate", which is worth doing. But a positive C1 there
licenses only: **deterministic gates on executable checks discriminate.** That
was never seriously in doubt.

It does not transfer to the claim this programme exists to test — whether
*judged* gates on ambiguous operational rules (refund eligibility, cabin-class
matching) discriminate. Those are the 55% here, and they are the commercially
interesting ones, because business procedures are mostly not executable checks.
Any result from that domain must be scoped in those words, or the programme has
quietly swapped the hard question for an easy one and reported the answer to
the wrong one.

## What this changes about the earlier framing

The published README now asks "can a procedure get better by being run?" That
is C2. It is the right long-term question and the wrong next question, because
C2 is not answerable while C1 is false. The order is C1, then C2, then C3.

## Cross-review

This document was reviewed adversarially by a second model (GPT-5.4 via codex)
before any test was run against it. Recorded because a design that survives
review only because nobody read it has not survived anything.

Accepted and changed above: the C1 dependency was overstated (execution
behaviour is a second evidence channel); the T1 statistic used the wrong unit
and was underpowered by the document's own T2 standard; "0 of 15 clean" is what
compounding produces, not proof of zero signal; T3 was missing the
prose-stepwise-gated control and would have confounded document with
scaffolding; the verifier may be judging off narration rather than evidence.

Accepted with a correction: the review argued the v2 regression might be
explained by prompt growth rather than by noise-driven revision, which the
adapter's own comments support. It did not have the later run where v2 with
that leak fixed scored *lower* still. That weakens the prompt-growth
explanation without settling it, and both remain live at this n.

The most valuable part of the review was not a correction. It was naming the
way this programme most likely produces something that looks good and is wrong,
which is now pre-registered above with its guards.
