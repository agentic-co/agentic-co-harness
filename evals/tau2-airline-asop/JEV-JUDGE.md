# Jev as a judge — the real API, not a Laya stand-in

**2026-09-23.** Same offline-replay methodology as `JUDGE-LADDER.md` and `LAYA-JUDGE.md` — same
evidence, only the judge swapped — but this time the real thing: [TypeSafe's Jev](https://typesafe.ai),
called live at `api.typesafe.ai`, not simulated by `laya-serve`'s wire-compatible mock. Motivated
directly by the principal: he holds a TypeSafe API token and asked to test it "if it's the concept,
or if it is actually just a model" — i.e. whether Jev's typed-decision approach is *inherently*
weak on this task shape (as Laya appeared to be) or whether Laya's showing was specific to Laya.

## The question

Laya (a non-autoregressive, typed-decision classifier) scored poorly on this exact task —
compound, evidence-chain precondition verification. The working hypothesis (H3, this file's
predecessor) was architectural: no scratch space for a reasoning chain, so a single forward pass
can't do what a generative judge can. **Jev is the same architectural class.** If the hypothesis is
right, Jev should fail here too. If Jev succeeds, the failure was Laya-specific, not architectural.

## The method

Identical to `LAYA-JUDGE.md`: `ws_glm.jsonl`'s 68 labeled decisions, evidence-first ordering
(tool history, then precondition, then transcript — matching Laya's corrected ordering, though
Jev's own truncation behavior, if any, is not under this project's control or inspection). One
`choice` question, `held`/`not_held` criteria, zero-shot, real API call per decision.

```python
payload = {"model": "jev-latest", "state": body, "questions": {"verdict": question}}
# POST https://api.typesafe.ai/v1/systemone, Authorization: Bearer $TYPESAFE_API_KEY
```

## The result

| judge | n | recall (TPR) | FPR | **lift** | precision | accuracy | avg. confidence |
|---|---|---|---|---|---|---|---|
| Jev (`jev-latest`) | 68 | **0.638** | 0.100 | **+0.538** | 0.974 | 0.676 | 0.652 |
| the deployed LLM judge (`glm-4.7`, from `EVIDENCE.md`'s C1 table) | 68 | 0.43 | 0.10 | +0.33 | 0.96 | 0.50 | — |
| Laya, zero-shot (evidence-first, corrected) | 68 | 0.09 | 0.50 | −0.41 | 0.50 | 0.15 | 0.24 |

Jev beats both, on this set, by a wide margin.

⚠️ **Do not trust this number precisely — the same power problem `LAYA-JUDGE.md` already found
on this exact set applies here too.** `held` truth has only 10 cases. That file's own re-run showed
Laya's lift swinging by ±0.2 to ±0.4 between orderings on this set alone, with both paired CIs
spanning zero — direct evidence this 68-decision set, and specifically its 10-cell minority class,
**cannot reliably resolve a judge comparison at all**. Jev's FPR of 0.100 here is 1 false refusal
out of 10 `held` cases; one different call moves FPR to 0.200 and lift to +0.438. Read this table
as *consistent with* Jev being strong, not as proof at this n.

## The result that matters — SOPBench, properly powered

On SOPBench `bank` (n=400 balanced, real deterministic ground truth, not tau2's proxy labels), Jev
scores **lift +0.770** [+0.708, +0.832], z = +25.5 — the highest of every judge measured in this
programme, ahead of the deployed GLM-4.7 judge (+0.675) and every other LLM tested. Full table and
analysis: `evals/sopbench-bank-asop/C1-GROUND-TRUTH.md`. That page is the one to cite; this one
exists so the tau2-side comparison against Laya's own tau2 numbers is on record, with its limits
stated plainly rather than omitted.

## Answering the actual question

**Concept, not architecture.** Jev is the same non-autoregressive, typed-decision class of model
as Laya, tested on the identical task shape, and it wins the whole table. That refutes the
architectural explanation for Laya's weak showing (H3's original framing: "no scratch space for a
reasoning chain") — Jev has no scratch space either, and it does not need one here. What actually
separates them is unmeasured directly by this experiment — likely scale (TypeSafe does not publish
Jev's parameter count, but its pricing and the README's own comparison table put it well above
Laya's 421M) and/or training data coverage, not the single-forward-pass design itself.

**Practical consequence for the "mix and match" question this project has been chewing on:** the
right frame is not "LLM vs. non-autoregressive," it's "which specific model, trained how, on what."
A well-trained System One model can out-discriminate a frontier-adjacent LLM judge on exactly the
kind of compound precondition-verification task ASOP gates need. A poorly-fitted one (Laya,
zero-shot, on this task) cannot. Judge architecture was never the variable that mattered — see
`C1-GROUND-TRUTH.md`'s N20 finding for what *did* turn out to matter most (evidence rendering).

## Bounds

One domain (bank), one model per family, zero-shot only (no fine-tuning attempted here — see
`LAYA-JEV-FINETUNE-STRATEGY.md`, which separately found TypeSafe documents no fine-tuning path for
Jev at all, so this zero-shot number is likely close to Jev's ceiling on this task, whereas Laya's
is not). Jev is API-only — no visibility into its context window, truncation behavior, or whether
it internally does something closer to chain-of-thought before emitting a typed verdict; treat it
as a black box that happens to work well here, not a mechanism this project understands.

## Reproducing

```bash
python3 scripts/eval/jev_judge_ladder.py            # tau2, 68 decisions
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sopbench_bank.jsonl \
    --jev jev-latest --out evals/sopbench-bank-asop  # SOPBench, 400 decisions
```

Requires `TYPESAFE_API_KEY` in `~/.claude/.env`.
