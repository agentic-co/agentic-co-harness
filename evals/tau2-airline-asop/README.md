# τ²-bench airline: does procedure STRUCTURE change outcomes?

A small, reproducible experiment, published whichever way it comes out.

> **Status: run complete. The result is null and the design turned out not to
> test the thing it was built for.** Start with
> [What this does NOT test](#what-this-does-not-test), then
> [Results](#results). Those two sections and one corrected sentence under
> [The question](#the-question) were added after the run; every other word is
> as it stood before any arm executed.

## The question

Everyone accepts that telling an agent the rules helps. This asks something
narrower: **holding the rules constant, does expressing them as an ordered
procedure with explicit preconditions and gates change how reliably an agent
follows them?**

That is a question about the SHAPE OF A PROMPT, and it is separable from
"prompting works".

**It is not the claim behind ASOP.** An earlier version of this line said it
was, which was wrong and is corrected here rather than quietly deleted. The
claim behind ASOP is that verification is separated from execution — a gate is
actually run, the executor cannot attest to its own work, and outcomes attach
to a version. This experiment tests none of that; see below.

## The design

| arm | system prompt | role |
|---|---|---|
| **A** | none | floor. Establishes what the agent knows without being told |
| **B** | `policy.v0-prose.md` | baseline. Sierra's policy exactly as τ²-bench ships it |
| **C1** | `asops/asop.claude.md` | treatment |
| **C2** | `asops/asop.codex.md` | treatment |
| **C3** | `asops/asop.agy.md` | treatment |

Same tasks, same agent model, same simulated customer, across all five.

**Primary comparison: B vs the mean of C1–C3**, paired on tasks.

**Three extractors, not one, and that is the point.** With a single author, a
win is unattributable — it could be the structure or it could be that one writer
produced a clearer document. Three independent models, each converting the same
prose under the same instructions, turn authorship from a confound into a
measurement:

- all three beat prose → the effect is structural;
- one beats prose → the effect was authorship, and n=1 would have fooled us;
- the spread across C1–C3 is itself the error bar on "who wrote it".

## What this does NOT test

Added after the run, because the limitation is fundamental and a reader deserves
it before the numbers rather than after.

Every arm delivers its document **whole, as a single system prompt**, and the
agent then runs one uninterrupted conversation. τ²-bench owns that loop. So in
every arm, including the treatments:

- no step boundary exists — the procedure is never walked;
- **no gate is ever evaluated**, only described;
- there is no verifier, and nothing prevents the agent from asserting its own
  success;
- no attestation is produced, and no outcome attaches to a version.

The ASOPs here *describe* gates. Nothing ran one. The execution runtime that
implements steps, gates and attestations was not in the loop at any point.

What follows from that: **this experiment could not have validated or refuted
ASOP even with a perfect model.** At best it could have shown that a
procedure-shaped prompt reads better than a prose one. That is worth knowing and
it is not the thesis.

The missing arm is the one that matters:

| arm | document | execution |
|---|---|---|
| B | prose | single shot |
| C1–C3 | ASOP | single shot |
| **(c)** | **ASOP** | **step by step, gate evaluated after each, executor ≠ verifier** |

**(b) vs (c) is the comparison that tests the claim** — same document, different
execution model. It does not exist yet. Building it means writing a τ²-bench
agent adapter that hands turns to the runtime so gates fire between steps.

## Blinding

Extractors received `EXTRACTION-PROMPT.md` (reproduced verbatim in this
directory) and `policy.v0-prose.md`. Nothing else. They were not shown the
tasks, the split, or any result from earlier runs, and were instructed not to
write toward a scenario.

This matters more than it sounds. The evaluation tasks include traps — requests
the policy requires the agent to REFUSE, where the tool will happily comply. An
extractor that had seen them could write to the test, and the experiment would
measure overfitting.

## Scoring

`EvaluationType.ENV` only: a hash of the final database against a gold database
built by replaying the task's recorded correct actions. **No model judges
anything.** τ²-bench can also score what the agent communicated, which is
LLM-judged — excluded here, because an experiment about whether procedures
improve verification should not rest its own verdict on a judge.

Reported per arm: **pass^1** (any single attempt) and **pass^k** (all k attempts
succeed). pass^k is the one that matters. Procedure quality is a consistency
property, so if structure helps, it should show up as the gap narrowing rather
than as a higher peak.

## The split, and why it is fixed

`split.json` carries a seeded DEV/TEST split, written once. TEST is never used
to revise anything.

It is drawn from **26 of the 50** airline tasks. The other 24 are read-only —
the customer asks a question and the database never changes — so under ENV
scoring an agent that did nothing scores identically to one that did the task
correctly. Those are not noise, they are free marks, and including them would
have made 42% of a sample unearnable passes while the run exited cleanly.
`scripts/eval/taubench.py selfcheck` proves the gate both directions per task
before any model is invoked.

## Reproducing

```
scripts/eval/taubench.py prepare   --tau2 <tau2-bench checkout> --out <dir>
scripts/eval/taubench.py selfcheck --tau2 <tau2-bench checkout> --out <dir>
```

Runs are driven through τ²-bench's own runner against any OpenAI-compatible
endpoint. Ours was local (LM Studio), which is why trial counts are governed by
patience rather than budget.

## Known limits, stated before the results

- **Local models.** Absolute scores sit below published leaderboard numbers and
  are not comparable to them. The comparison here is within-experiment, model
  held constant, which is the only claim being made.
- **The simulated customer is a model**, so some variance is the simulator's
  rather than the agent's. It is held constant across arms.
- **Arm A is a sanity check, not a fair comparison.** An agent with no policy
  does not know the domain's rules. A non-zero score there would indicate task
  leakage in the prompts, which is the reason to run it.
- **Word-count parity is checked but is a weak proxy** for content parity. Both
  documents are published here so a reader can judge whether an extraction
  smuggled in content the prose did not contain.

## Pre-registered deviations, recorded before any arm was run

Two things were found while auditing the extractions. Both are written down here
**before** results exist, because a handling rule chosen after seeing numbers is
not a handling rule.

### What the extraction step itself revealed

Three models converted the same prose under identical instructions. None
matched the source length, and deviation scaled with expansion:

| arm | extractor | words | vs prose | content deviations found |
|---|---|---|---|---|
| C1 | claude | 1,343 | **+2.3%** | none found |
| C2 | codex | 1,718 | +31% | 1 — resolved an ambiguity |
| C3 | agy | 1,900 | **+45%** | 2 — one strengthening, one distortion |

That gradient is a result in its own right, and not one we went looking for:
**a model asked to structure a procedure tends to expand it and to resolve what
the source left open.** Both are failures of fidelity dressed as helpfulness,
and neither is visible without a line-by-line audit. Anyone automating
procedure extraction should expect it.

No arm was re-run to bring it into line. A "make it shorter" or "be more
faithful" instruction would have existed in one arm only, and a control that
differs per arm is not a control. Length is therefore a covariate:

- if **C1** wins — the arm that barely grew — "a longer prompt helps" is dead;
- if only the long arms win, length is the live explanation and we say so;
- if all three win, structure survives a 43-point spread in length.

### The three content deviations

**C2** rendered the source's `flies (basic) economy` as `flies basic economy or
economy`, resolving an ambiguity the prose deliberately left open — it insists
elsewhere that basic economy is "completely distinct from economy". Effect: C2
is strictly more likely to refuse compensation.

**C3** rendered `flight can be cancelled if any of the following` as `if and
only if ANY`. Defensible as a reading, still a strengthening.

**C3** also turned a descriptive fact into a requirement: `A flight can be
available at multiple dates` became `Flight must be available at multiple
dates`, sitting in the preconditions of the booking step. Read literally, that
could make the agent refuse to book a single-date flight — a bug in the arm
rather than a property of the treatment.

Each was self-reported by the operator of the arm that produced it, including
on its own output.

### Handling, fixed before any arm was run

All three deviations touch the **same three TEST tasks — 8, 23 and 25** — which
happen to be both the booking tasks and the compensation tasks. So a single
exclusion set covers every compromised arm.

- **Primary analysis:** all 13 TEST tasks.
- **Sensitivity analysis:** excluding 8, 23, 25. Reported **alongside the
  primary, always** — not only when it is favourable.
- If the two disagree, the deviations mattered, and the affected arm is
  reported as compromised rather than averaged into the treatment mean.

## Results

Run 2026-09-12. Agent `gpt-oss-20b`, simulated customer `gemma-4-31b`, both
local via LM Studio. 13 TEST tasks × 2 trials = 26 runs per arm.

| arm | | pass^1 | pass^2 | successes |
|---|---|---|---|---|
| **A** | no policy | 0.115 | 0.077 | 3/26 |
| **B** | prose (baseline) | 0.154 | 0.000 | 4/26 |
| **C1** | claude | 0.231 | 0.077 | 6/26 |
| **C2** | codex | 0.077 | 0.077 | 2/26 |
| **C3** | agy | 0.115 | 0.077 | 3/26 |
| | **C mean** | **0.141** | **0.077** | |

**Primary comparison, B vs C-mean: −0.013 on pass^1.** The treatment mean is
below the baseline. Fisher exact on the strongest treatment against the
baseline (6/26 vs 4/26) gives **p = 0.73**.

### The instrument did not discriminate

**Arm A carried no policy at all and still scored 0.115 / 0.077** — beating C2
and tying C3. The pre-registered rule was that a non-zero score there means the
comparison is invalid. It fired.

**7 of the 13 TEST tasks were never solved by any arm in any trial** (7, 12, 21,
23, 37, 39, 44). Six tasks ever produced a success, and task 16 is solved by
almost everything including the no-policy arm. Published τ²-bench airline
results with frontier models run far above this; at 8–23% the agent cannot
complete the work regardless of what it is told, so procedure quality has
nothing to act on.

This is an instrument failure, not a finding about the treatment. A setup that
cannot tell "no policy" apart from "the policy" says nothing about the policy,
and reporting this as "ASOPs did not help" would be as dishonest as reporting
the pass^2 column as a win.

### The sensitivity analysis removed the one apparent win

Excluding tasks 8, 23 and 25 exactly as pre-registered above:

| arm | pass^1 | pass^2 |
|---|---|---|
| A | 0.150 | 0.100 |
| B | 0.150 | 0.000 |
| C1 | 0.200 | **0.000** |
| C2 | 0.100 | 0.100 |
| C3 | 0.050 | **0.000** |

C1's only pass^2 success was task **8**. C3's was task **25**. Both are on the
exclusion list. Removing them drops both arms to 0.000 — identical to prose —
and the C-mean pass^2 advantage falls from +0.077 to +0.033, carried entirely by
C2, which also ties the no-policy arm.

The single number that looked like a win for the treatment was produced entirely
by the three tasks already written down as compromised. The rule existed so it
could not be chosen after seeing the numbers, and that is what it did.

### What would have to change

1. **A stronger agent model.** The baseline must sit well above floor before a
   treatment has room to move it.
2. **A precondition gate on the experiment itself**: require arm A ≈ 0 *and* arm
   B clearly above floor before any treatment arm is worth running. That check
   costs one arm and would have ended this run in an hour.
3. **Arm (c)** — see [What this does NOT test](#what-this-does-not-test).
   Without it, no configuration of this experiment tests the thesis.

More tasks and more trials matter too, but only after (1) and (3). At 13 tasks
and 2 trials nothing short of an enormous effect is detectable.

## Attribution and licence

`policy.v0-prose.md`, the task data, and the evaluation harness are from
[τ²-bench](https://github.com/sierra-research/tau2-bench) by Sierra, MIT
licensed. The policy is redistributed here unmodified so the baseline arm is
inspectable; all credit for it is theirs.

The extracted ASOPs and this experiment are Apache-2.0, same as the rest of this
repository.
