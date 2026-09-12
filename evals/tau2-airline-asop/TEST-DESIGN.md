# What are we actually trying to prove, and what test proves it?

Written 2026-09-12, after three runs that measured the wrong thing in three
different ways. This is the design document the earlier runs should have had.

## The claim, split into three

"ASOPs work" is not testable. It is three claims with a dependency order, and
we have been testing the last two while the first was silently false.

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

**The dependency is the whole point: C2 and C3 both require C1.** If gate
verdicts do not discriminate, then the evidence feeding a revision is noise, a
revision drafted from noise is a random perturbation, and random perturbations
of a working procedure make it worse. C3 fails for the same reason — a gate
that cannot tell good work from bad cannot substitute for capability.

## Where we actually are

C1 is currently FALSE in this setup, and it is measurable from data already
collected (15 runs across four configurations):

| | mean gate refusals |
|---|---|
| runs that **passed** | 4.25 |
| runs that **failed** | 4.27 |

Zero of 15 runs passed through the gates cleanly. The gate refuses the same
amount whether the run succeeds or fails. It is not a verifier; it is a
constant.

That single table explains the v2 regression. v2 was drafted from these
refusals. The refusals carried no signal, so neither did v2.

Three likely causes, all mine, all fixable:

1. **The verifier is biased to refuse.** Its prompt says "absence of evidence
   is FAIL", which on a truncated transcript is nearly always true.
2. **It sees a window, not the run.** Evidence is the last 12 messages. A
   precondition satisfied at turn 3 is invisible at turn 20.
3. **55% of gates are judged, not deterministic.** The ASOPs declare
   `deterministic` on steps that name no re-runnable check, so a weak model
   guesses where a command should have decided.

## The tests, in dependency order

### T1 — Does the gate discriminate? (C1)

**This is not a benchmark score.** It is a diagnostic-accuracy question, and it
needs no new arm — only runs we already produce.

- Unit: one run. Features: gate verdicts. Label: did the task succeed.
- Report: refusal rate on passing vs failing runs, and the separation between
  them. A gate with no separation is decorative regardless of the pass rate.
- **Gate on the gate:** the verifier must be able to say both yes and no. If no
  run ever passes cleanly, the check cannot pass and nothing downstream is
  interpretable. This is the same two-directional proof `taubench.py selfcheck`
  already applies to the scorer, applied to the verifier.
- Fix first: give the verifier the whole transcript, remove the refuse-bias,
  and either make the deterministic gates real or stop calling them that.

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

| arm | model | procedure |
|---|---|---|
| small, bare | gpt-oss-20b | none |
| small, prose | gpt-oss-20b | prose policy, single shot |
| **small, gated** | **gpt-oss-20b** | **ASOP, stepwise, gated** |
| frontier reference | published runs | prose policy |

**"Small + ASOP beats small + prose" is the claim.** Beating a frontier model
is a headline, not a result — different model, different conditions, different
trial count. The published figures below are a REFERENCE LINE, never a
like-for-like comparison, and any write-up must say so.

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

## What this changes about the earlier framing

The published README now asks "can a procedure get better by being run?" That
is C2. It is the right long-term question and the wrong next question, because
C2 is not answerable while C1 is false. The order is C1, then C2, then C3.
