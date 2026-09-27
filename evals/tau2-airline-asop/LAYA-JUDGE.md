# Does a non-autoregressive classifier judge beat the LLM judge?

> ⚠️ **CORRECTION FLAGGED, 2026-09-23 (later the same day) — the numbers below likely understate
> Laya, and by a lot.** The fine-tuning-strategy research pass found that Laya's base checkpoint
> truncates its input tail at a 512-token window (192 reserved for the question head, 320 for
> evidence), and this script — like `sopbench_judge.py` — puts `TOOL HISTORY` **last** in the
> state string, i.e. exactly the material most likely to get cut. On the SOPBench replay (same
> bug, same ordering), fixing only the ordering — zero training — moved lift from −0.025 to
> **+0.210** [+0.139, +0.281], a paired-bootstrap Δ of +0.235 (P(Δ≤0) = 0.0000), concentrated on
> the overflowed cells. This file's −0.20 number has not yet been re-run with the fix; treat it as
> **unreliable until re-run**, not as evidence Laya fails this task. See
> `LAYA-JEV-FINETUNE-STRATEGY.md` for the full finding and
> `evals/sopbench-bank-asop/C1-GROUND-TRUTH.md` for the corrected SOPBench numbers.
>
> ---
>
> # ✅ RE-RUN, same day. The −0.20 reproduces — and this page still cannot support its conclusion.
>
> **The defect was real here too.** On this 68-decision set the median evidence body is **940
> tokens** against a 320-token budget; **85% overflow**, so the judge saw about **47% of the
> evidence** on average.
>
> **The fix works on SOPBench, decisively** (−0.025 → +0.210, paired Δ +0.235, P(Δ≤0) = 0.0000,
> n=400 balanced). **On *this* set it does not reproduce.** Both orderings, both checkpoints:
>
> | checkpoint | original ordering | evidence-first | paired Δ | P(Δ≤0) |
> |---|---|---|---|---|
> | `laya` | **−0.197** (reproduces the −0.20 below) | **−0.414** | −0.217 [−0.614, +0.169] | 0.865 |
> | `typed-decisions` | −0.183 | **+0.003** | +0.186 [−0.145, +0.538] | 0.136 |
>
> **Both deltas' CIs span zero, in opposite directions.** The table moves entirely on the 10-cell
> `held` class, where one decision is 0.1 of FPR — `laya` went from 3 to 5 false refusals out of
> 10, and that is the whole of the −0.217. **This set cannot resolve the ordering question**, which
> is exactly what this page's own power note says about its own headline, and what `EVIDENCE.md`
> says about tau2 generally.
>
> **So the honest verdict is narrower than either version of this page claimed.** The −0.20 is
> reproducible and is not a SOPBench-style artifact — but it is not evidence about Laya either; it
> is evidence that 10 rare-class cells cannot measure a judge. The conclusion *"Laya is not a
> like-for-like substitute for an LLM judge"* survives, on **SOPBench's** better-powered evidence
> (+0.210 against `gpt-oss-20b`'s +0.655, and beaten by a string matcher). The claim that **this
> page** established it does not.
>
> Reproduce any row with `--checkpoint` / `--order`. Raising `--max-len` above the native window is
> **not** a fix and makes things worse — see C1-GROUND-TRUTH.md's second correction.

**2026-09-23. First data point, not a verdict.** Same offline-replay methodology as
`JUDGE-LADDER.md` — same evidence, only the judge swapped — but a different *kind* of judge:
[Laya](https://github.com/NandhaKishorM/laya), a non-autoregressive classifier (ModernBERT-large,
421M params) that does one forward pass over typed `choice`/`score`/`noul` questions instead of
generating text. Motivated by two internal findings this file does not re-derive: JUDGE-LADDER.md
(swapping judge *size* among autoregressive LLMs barely moves lift, −0.10 to +0.04) and N16 (the
properly-powered v6 lift reading is not uniformly zero — 9 of 15 reportable gates are informative,
led by authentication steps at +0.50/+0.38). Neither says anything about judge *architecture*.
This is the first test of that variable.

## The question

Is near-zero/negative lift a property of gating these judged preconditions at all, or specific to
*autoregressive generative* judges — such that a calibrated classifier, built to produce typed
verdicts with proper-scoring-rule-trained confidence instead of inferring it from logprobs, does
better on the same evidence?

## The method

`ws_glm.jsonl` (`~/Code/agentco-harness-eval-archive/2026-09-15/`) is the exact 68-decision labeled
set behind `EVIDENCE.md`'s C1 confusion matrix (TP 25 · FP 1 · FN 33 · TN 9 — 58 `not_held` + 10
`held`, ground truth from independent human labeling, `truth_source` recorded per row). Every
decision carries the same evidence the deployed verifier saw: step precondition text, conversation
transcript, tool call history. Replayed through Laya with **zero prompt iteration** — one
`choice` question, criteria `held` / `not_held`, no fine-tuning, no few-shot examples — because the
point of a first pass is to establish a naive baseline before spending any tuning effort.

```python
question = {
    "type": "choice",
    "instructions": "...was the precondition actually satisfied?",
    "criteria": {"held": "...", "not_held": "..."},
}
state = {"body": f"STEP PRECONDITION: {step_body}\n\nTRANSCRIPT:\n{transcript}\n\nTOOL HISTORY:\n{tool_history}"}
router.predict(state, {"verdict": question})
```

⚠️ **That body ordering is the defect the banner describes** — `TOOL HISTORY` last, against a
tail-truncating 320-token window. It is now `--order original`; the default is evidence-first.

**LIFT** = `P(refuse | truth=not_held) − P(refuse | truth=held)` = TPR − FPR, same definition
JUDGE-LADDER.md and `gate_value.py` use.

## The result

| judge | n | recall (TPR) | FPR | **lift** | precision | accuracy | avg. confidence |
|---|---|---|---|---|---|---|---|
| **the deployed LLM judge** (`glm-4.7`, from EVIDENCE.md's C1 table) | 68 | **0.43** | 0.10 | **+0.33** | 0.96 | 0.50 | — |
| **Laya**, zero-shot, naive prompt | 68 | **0.10** | 0.30 | **−0.20** | 0.67 | 0.19 | **0.19** |
| ↳ same, evidence-first ordering (re-run) | 68 | 0.09 | **0.50** | **−0.41** | 0.50 | 0.15 | 0.24 |
| ↳ `laya-typed-decisions`, naive prompt (re-run) | 68 | 0.02 | 0.20 | −0.18 | 0.33 | 0.13 | 0.10 |
| ↳ `laya-typed-decisions`, evidence-first (re-run) | 68 | 0.10 | 0.10 | **+0.00** | 0.86 | 0.22 | 0.09 |

**The three ↳ rows are the 2026-09-23 re-run, and they are here to be un-citable individually.**
Every one of them differs from the row above it by **one to three decisions in a 10-cell class**.
The banner has the paired intervals; all of them span zero. Read this table as *"tau2 cannot
measure this"*, not as four results.

Laya loses on every axis, not narrowly. It agreed with the LLM judge's own verdict on only 41/68
(60%) decisions, and its choice distribution is **59 `held` / 9 `not_held`** — it defaults to
assuming the precondition was satisfied, the opposite bias from the deployed judge, which refuses
more than it should (recall 0.43 but at 64% overall refusal rate per Stage-6 numbers). Its own
confidence never exceeds 0.4555 and medians at 0.197 — Laya is telling you, honestly, that it does
not know. That is the one thing in this table worth taking seriously.

## Why, probably — a hypothesis this run cannot confirm

This task is not what Laya's checkpoints are trained on. The README's own worked example is
department routing from a two-sentence ticket; this task asks the model to hold a multi-clause
precondition in mind, read a tool-call history for the fact that satisfies or violates it, and
adjudicate — a chain of reasoning steps compressed into one forward pass with no scratch space.
Autoregressive judges are bad at this too (lift near zero for two of three swapped LLM judges in
JUDGE-LADDER.md), but a generative judge can at least attempt the reasoning chain in its output
tokens before answering. Laya, architecturally, cannot — there is nothing between the state and
the verdict. **Low confidence across the board is consistent with this**: the model is not
confidently wrong, it is honestly uncertain on an out-of-distribution task, which is a real
advantage over an LLM judge that stays confident while wrong (documented behavior in Laya's own
README, for language routing, not this task — cited as a general property, not re-verified here).

## What this does and does not settle

**Does not settle:** whether a *fine-tuned* Laya checkpoint does better. Laya ships a fine-tuning
notebook and this experiment does not touch it — that is the obvious next step this result argues
for, not against. Does not settle whether Laya is useful as a **confidence-gated abstention layer**
alongside the LLM judge (route low-confidence decisions to the LLM, let Laya's own honesty about
not-knowing do the routing) rather than a standalone replacement — untested here.

**Does settle, provisionally:** a naive, zero-shot, un-tuned classifier judge is not a drop-in
upgrade for this specific kind of compound, evidence-chain precondition verification. The
"mix and match" question the two judge families raise has a first, honest answer — Laya is not
a like-for-like substitute for an LLM judge on the harder verification task ASOPs currently author,
and the reason (single-forward-pass, no reasoning chain, out-of-distribution task shape) generalizes
to why it would likely fail the same way on airline's other judged gates, not just this sample.

> 🛑 **Superseded in its justification, not its conclusion (see the banner).** This paragraph is
> right, but **this page is not what makes it right** — `SOPBench`'s n=400 balanced set is
> (+0.210 vs +0.655, and beaten by a string matcher). Two things it asserted are now known false:
> the ordering here was truncation-biased, and `laya-typed-decisions` — named below as "the variant
> closer to this task's shape" — has been tested on both sets and is **worse** than the base
> checkpoint on SOPBench (+0.050 vs +0.210). Cite C1-GROUND-TRUTH.md for this claim, not this page.

**Bounds.** n=68 — **and the binding constraint is the 10-cell `held` class, which is what the
re-run above turns into the headline rather than a footnote.** Two prompt orderings, two Laya
checkpoints (`laya` and `laya-typed-decisions`, both re-run 2026-09-23), one domain (airline). No
live agent run — this measures judgment quality on fixed evidence only, same limitation
`t1_rejudge.py`'s own docstring states for the LLM ladder.

⚠️ **Added 2026-09-23, from a literature pass (Research, evals/LLM-judge, 26 sources verified).**
This lift metric is exactly **Youden's J** (sensitivity + specificity − 1), which has a standard
power formula. At α=.05/80% power, detecting J=0.20 needs ~82 per arm; J=0.10 needs ~294 per arm.
**The binding constraint is the rarer class**, and here that's `truth=held` at n=10 — nowhere near
powered to trust the FPR=0.30 figure specifically, though the recall/TPR side (n=58) is closer to
adequate for a large effect. Read the −0.20 lift as *directionally* bad, not as a precise number.
Also worth knowing before repeating this pattern: "Inference Scaling fLaws" (arXiv:2411.17501)
proves any verifier with nonzero FPR caps achievable lift regardless of how good it otherwise is —
so **the deployed LLM judge's own +0.33 baseline needs the same skepticism**, especially since
that judge shares weights with the executor it is grading (documented confound, `EVIDENCE.md`:
"Verification confounded. Arm C's judge shares the executor's weights.") — same-weights bias is
[HIGH-confidence real per the literature (MT-Bench, arXiv:2306.05685; Panickssery et al.,
arXiv:2404.13076)], so +0.33 is not a clean number to beat either.

**One genuine piece of supporting evidence for trying Laya at all, despite this naive result:**
MiniCheck (arXiv:2404.10774) — a 770M-parameter classifier, fine-tuned specifically to check a
claim against supplied evidence sentences — matches GPT-4 accuracy on grounded fact-checking at
~400x lower cost. That is the closest published analogue to this exact task shape, and it is a
classifier, not a generative judge — but it is *fine-tuned*, not zero-shot. This result is
consistent with "wrong point on the tuning spectrum," not "classifiers cannot do this."
**Also worth weighing before spending on that fine-tune:** "When Does Verification Pay Off?"
(arXiv:2512.02304) finds verifier value comes mostly from *independence from the executor's
lineage*, not size — and JUDGE-LADDER.md never tested that axis (all three swapped judges were
still same-methodology open-weight LLMs). A cross-family judge swap is a cheaper, more literature-
supported next step than fine-tuning Laya, and would also tell us whether the deployed judge's
+0.33 survives removing the same-weights confound before crediting Laya's −0.20 against it.

## Reproducing

```bash
# corrected default: evidence-first, native window
~/Tools/laya-runtime/.venv/bin/python scripts/eval/laya_judge_ladder.py
~/Tools/laya-runtime/.venv/bin/python scripts/eval/laya_judge_ladder.py --checkpoint typed-decisions

# the superseded ordering, kept runnable so the published -0.20 stays auditable
~/Tools/laya-runtime/.venv/bin/python scripts/eval/laya_judge_ladder.py --order original
```

Per-decision verdicts for all four arms: `laya_judge_results{,_typed-decisions}{,_original}.json`.

Requires the Laya runtime at `~/Tools/laya-runtime/.venv` (set up 2026-09-23, see
`~/Tools/laya-runtime/` — `pip install "laya[serve]"` on Python 3.13 via `uv`) and
`~/Code/agentco-harness-eval-archive/2026-09-15/ws_glm.jsonl`.
