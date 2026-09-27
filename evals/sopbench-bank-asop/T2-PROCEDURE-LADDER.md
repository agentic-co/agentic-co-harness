# Does structured procedure guidance improve task accuracy, where the scorer can tell right from wrong?

**2026-09-23. The first Claim-1 measurement in this programme taken in a regime where the
ground truth is real.** Every prior Claim-1 number comes from tau2, where — as
`EVIDENCE.md` states in terms — the judge cannot tell right from wrong by construction.
This page runs SOPBench's own built-in procedure ablation ladder, scored by SOPBench's own
deterministic evaluator, with **no LLM anywhere in the scoring loop**.

**Headline: the ladder is positive and significant in BOTH domains, and PVA wins both.**

| domain | none | `constraint_hint` | `action_order` | **PVA** | PVA delta | p |
|---|---|---|---|---|---|---|
| `bank` (n=134) | 0.500 | 0.552 | 0.634 | **0.642** | **+0.142** [+0.061, +0.222] | **0.0013** |
| `online_market` (n=172, held out) | 0.384 | 0.616 | 0.541 | **0.709** | **+0.326** [+0.250, +0.401] | **0.0000** |

On the held-out domain PVA gained 59 tasks and lost 3.

**This is the opposite sign to every tau2 arm the programme owns** (airline −0.240, retail
−0.083/−0.028). It is the first time the class of intervention has been measured where the
scorer can discriminate, and it wins there, twice.

⚠️ **The middle rungs do not replicate their order.** `action_order` beats `constraint_hint` on
`bank` (0.634 vs 0.552) and loses to it on `online_market` (0.541 vs 0.616). Only the endpoints —
baseline worst, PVA best — are stable across both domains. Do not read the ladder as a ranking of
the three interventions; read it as "procedure helps, and the full enumerate-verify-self-check
loop helps most."

🛑 **Read the bound before quoting the number. This tests the *class* — procedure-in-prompt
plus self-verification — using SOPBench's own PVA scaffold. It is NOT our ASOP document format
and NOT our gate runtime.** That port now exists (`37403f6`, `sopbench_asop_swarm.py` over the
extracted `asop_engine.py`) and its four-arm bank comparison was running overnight at this page's
exact configuration. **This page is that arm's comparator, not a substitute for it.** Nothing
here licenses "our ASOPs work" — it licenses "a third party's implementation of our thesis works
here, so the tau2 losses are confounded rather than conclusive."

---

## The question

Claim 1 in this programme's vocabulary: **does structured procedure guidance increase
task-completion accuracy?** `VALIDATION-MATRIX.md` §1 records it as *refuted on tau2 retail*
and *direction confirmed on airline*, with one confound large enough to make the verdict
uninterpretable:

> Every Claim-1 result the programme owns comes from tau2, where the judge's measured lift is
> **−0.10 to +0.33** — i.e. from the regime where the gate *cannot tell right from wrong*. On
> SOPBench the same class of judge reaches **+0.610 to +0.770**. **"The apparatus hurts" has
> therefore never been tested in a regime where the apparatus works.**

This page closes that gap from the other side. Rather than swapping the judge, it removes the
judge entirely: SOPBench scores final database state, per-call permissibility, and verification
completeness by walking a `directed_action_graph` that the original Python program defines.

## Why this arm had never been run

SOPBench's live runner ships a procedure-vs-no-procedure ablation set upstream (`8790ed7`), and
the released trajectories contain **none of these arms**. The flags exist; nobody had spent them.

| arm | flag | what it adds |
|---|---|---|
| `none` | — | baseline: the SOP constraints as prose, no procedure |
| `constraint_hint` | `--constraint_hint` | annotates each constraint with **which tool verifies it** — the ASOP v2→v3 fix (gates naming their tools) in SOPBench's vocabulary |
| `action_order` | `--action_order` | appends the explicit required verification procedure, **mechanically compiled from the directed action graph** — a ready-made instance of the `isa_to_asop.py` compiler thesis |
| `pva` | `--scaffold pva` | **PVA (Plan-Verify-Act)**: identify → enumerate the constraint checklist → call the helper that verifies each item and state SATISFIED/NOT → self-verify against AND/OR composition → only then act |

PVA is a third-party implementation of this project's own thesis, written by SOPBench's authors,
using only specifications already given to the agent — no oracle leakage. The fourth flag
(`--constraint_verdict`, which leaks ground-truth SATISFIED/NOT) is an oracle-leaking ceiling
control and was **not** run; it measures nothing about method.

## Method

- **Executor:** `openai/gpt-oss-20b` served by local LM Studio on `:4242`. Free, and already
  this programme's measured weak-executor anchor, so results stay comparable with the tau2 arms.
- **Counterpart:** SOPBench's **scripted** user (`--user_model` unset) — a canned response built
  from `user_info["known"]`, **no model call at all**. So there is no user-simulator variance in
  the paired comparison, unlike every tau2 arm, and only one model need be resident.
- **Task set:** **all 134 `bank` tasks and all 172 `online_market` tasks, 1 run each.** This is
  not a chosen sample size — it is SOPBench's own released convention: every released trajectory
  file is the full task set × 1 run, `mode_fc`, `dep_full`, `fmt_structured`, `tool_full`,
  scripted user. Matching it exactly removes a free parameter.
- **Scoring:** SOPBench's `evaluator_function_directed_graph`, reached through SOPBench's own
  `try_eval` parsing so the numbers are its scoring and not a reimplementation. Verified: the
  harness reproduces `run_evaluation.py`'s printed pass rate exactly on the same inputs.
- **Pairing:** arms are compared only on tasks present in **all four** arms, so every delta is
  paired on identical tasks. Significance is **exact McNemar** on the discordant cells; the CI is
  the paired difference-of-proportions interval computed from those same cells.

### What the pre-flight caught — three traps, all before spending a run

This project's culture treats plumbing-presented-as-a-finding as the failure mode to beat. Three
were live here:

1. 🛑 **The output filename does not encode the scaffold or ablation flags.** `run_simulation.py`
   builds it from model/mode/dep/fmt/tool/shuffle only, and `load_existing_results()` resumes
   from whatever it finds. All four arms would have written to **one file and silently inherited
   each other's results** — producing four "arms" that were one arm, with no error anywhere.
   Every arm here runs under its own `--output_dir`.
2. **`--tool_call_mode fc` gates the model twice.** `OPENAI_MODELS` admits the handler
   (`swarm/llm_handler.py:105-118`); `FUNCTION_CALLING_MODELS["openai"]` is asserted separately
   at `:614`. Adding only the first — the documented step — fails at the first tool call.
3. **The arms were verified to actually differ before being spent.** Building the four prompts
   directly gives 10645 / 10739 / 10935 / 12399 chars, all distinct. An arm silently identical to
   baseline is indistinguishable from a null result.

A single baseline task was then run end-to-end and scored through SOPBench's evaluator to confirm
it produced a real scored result rather than a silent all-fail.

## Result — `bank`, n=134 paired

| arm | **success** | verification complete (`dirgraph`) | per-call permissible | final-state match | correct action call |
|---|---|---|---|---|---|
| `none` | **0.500** | 0.597 | 0.754 | 0.843 | 0.716 |
| `constraint_hint` | **0.552** | 0.694 | 0.769 | 0.866 | 0.754 |
| `action_order` | **0.634** | 0.843 | 0.813 | 0.896 | 0.731 |
| `pva` | **0.642** | 0.791 | 0.828 | 0.933 | 0.791 |

Paired against `none`, exact McNemar:

| arm | delta | 95% CI | gained | lost | p |
|---|---|---|---|---|---|
| `constraint_hint` | +0.052 | [−0.020, +0.125] | 16 | 9 | 0.2295 |
| `action_order` | **+0.134** | **[+0.050, +0.219]** | 27 | 9 | **0.0039** |
| `pva` | **+0.142** | **[+0.061, +0.222]** | 26 | 7 | **0.0013** |

**Every metric rises monotonically or near-monotonically.** `dirgraph_satisfied` — "did the agent
actually call the required verification actions" — moves **0.597 → 0.843** under `action_order`.
That is the mechanism visibly doing the thing it claims, not just an outcome shifting.

### The split that decides how to read this

SOPBench mixes tasks where the action **should** succeed with tasks where the agent **should**
refuse. A scaffold that merely over-gates buys the second column by selling the first — which is
precisely the parent project's measured liability on tau2. So the aggregate alone cannot settle it:

| arm | permissible (n=48) | impermissible (n=86) | avg tool calls |
|---|---|---|---|
| `none` | 0.167 | 0.686 | 1.83 |
| `constraint_hint` | 0.271 | 0.709 | 2.17 |
| `action_order` | **0.396** | 0.767 | 2.64 |
| `pva` | 0.333 | **0.814** | 2.31 |

**Both halves rise in every arm.** PVA improves correct refusal (0.686 → 0.814) *and* correct
completion (0.167 → 0.333). **This is not over-gating.** It is the cleanest available evidence
that the gain is real work rather than a refusal bias, and it is the single most important row on
this page.

### Cost, recorded because "flat" would have been the wrong word for it

Procedure guidance is not free. Tool calls per task go 1.83 → 2.31 (PVA) and 2.64
(`action_order`); wall clock ran ~1.5× baseline for PVA on `bank`. Noted up front, before the pass
rates were known, so it cannot read as post-hoc: had accuracy come back flat, flat would have
*understated* the result — the same accuracy for half again the work is a loss, not a tie. It did
not come back flat, so the cost is a price paid for a real gain. But note **`action_order` buys
less accuracy than PVA for more work** (+0.134 at 2.64 calls vs +0.142 at 2.31), which is worth
flagging because `action_order` is the mechanically-compiled procedure — the closest thing here to
the `isa_to_asop.py` compiler thesis. The same holds on `online_market` (3.98 calls for +0.157 vs
PVA's 3.73 for +0.326), so the compiled-order arm is the worst value-per-call **in these two
domains**.

> 🛑 **CORRECTED 2026-09-24, once `library` and `hotel` landed — do not quote the stronger version
> of this claim.** An earlier revision of this paragraph said "the compiled-order arm is the worst
> value-per-call in both domains" and that generalisation does **not** survive four domains:
> `action_order` is worst on `online_market` and `library`, while **`constraint_hint` is worst on
> `bank` and `hotel`**. There is no consistent loser. What does hold 4 of 4 is the positive claim —
> **`pva` is the best value-per-call in every domain measured.** The caution about the compiler
> thesis is therefore weaker than written: `action_order` is *sometimes* the worst buy, not
> reliably so.

## Result — `online_market`, n=172 paired (held-out domain)

`online_market` was **not** a convenience pick. `C1-GROUND-TRUTH.md` pre-registers it as the
falsification target for exactly this kind of reading — *"If `online_market` shows near-zero lift
for the same judge, then bank is a favourable special case"* — and it carries the closest
violation rate to bank (0.32 vs 0.29) over the largest decision count of the seven domains.

| arm | **success** | verification complete | per-call permissible | final-state match | correct action call |
|---|---|---|---|---|---|
| `none` | **0.384** | 0.442 | 0.680 | 0.698 | 0.651 |
| `constraint_hint` | **0.616** | 0.669 | 0.855 | 0.872 | 0.849 |
| `action_order` | **0.541** | 0.669 | 0.715 | 0.715 | 0.692 |
| `pva` | **0.709** | 0.860 | 0.901 | 0.907 | 0.791 |

| arm | delta | 95% CI | gained | lost | p |
|---|---|---|---|---|---|
| `constraint_hint` | **+0.233** | [+0.138, +0.327] | 59 | 19 | **0.0000** |
| `action_order` | **+0.157** | [+0.064, +0.250] | 49 | 22 | **0.0018** |
| `pva` | **+0.326** | [+0.250, +0.401] | 59 | 3 | **0.0000** |

| arm | permissible (n=60) | impermissible (n=112) | avg tool calls |
|---|---|---|---|
| `none` | 0.267 | 0.446 | 3.19 |
| `constraint_hint` | 0.567 | 0.643 | 3.52 |
| `action_order` | **0.600** | 0.509 | 3.98 |
| `pva` | 0.450 | **0.848** | 3.73 |

**The headline replicates and roughly doubles.** Verification completeness moves 0.442 → 0.860
under PVA, and PVA's discordant count (59 gained / 3 lost) is about as one-sided as this design
can produce.

**But the split exposes something bank did not.** The two middle rungs buy *different halves*:

- `action_order` is the best arm on permissible tasks (0.600, above PVA's 0.450) while barely
  moving impermissible ones (0.446 → 0.509). It teaches the agent to complete, not to refuse.
- `pva` is the reverse and stronger overall: impermissible 0.446 → **0.848**, permissible only
  0.267 → 0.450.

So on `online_market` PVA's aggregate win is carried disproportionately by correct refusals,
where on `bank` both halves moved together. PVA still improves the permissible half in both
domains — it is **not** the over-gating pattern, which would show that half falling — but the
balance of where the gain comes from is domain-dependent, and a single-domain read would have
missed it.

## What this settles

- **In a regime where the scorer can discriminate, this class of intervention helps — significantly,
  in two domains, and without the over-gating signature.** That regime had never been tested.
- **The tau2 losses are now clearly confounded rather than simply "the apparatus hurts."** Same
  class of intervention, opposite sign, and the distinguishing variable is whether the scorer can
  tell right from wrong. This does not overturn the tau2 numbers — it bounds what they were
  measuring.
- **The full PVA loop beats its components, in both domains.** Enumerate-verify-self-check
  outperforms either annotating constraints with their tools or supplying the compiled order.
- **`bank` is not a favourable special case.** The pre-registered falsification domain replicated
  the result at roughly twice the effect size. That was the specific way this reading could have
  died, and it didn't.

## What replicated, and what didn't

Worth separating, because only the first column is safe to build on:

| finding | `bank` | `online_market` | replicated? |
|---|---|---|---|
| procedure guidance beats baseline | +0.052…+0.142 | +0.157…+0.326 | ✅ all three arms, both domains |
| PVA is the best arm | 0.642 | 0.709 | ✅ |
| PVA improves the permissible half too | 0.167→0.333 | 0.267→0.450 | ✅ not over-gating |
| verification completeness rises most | 0.597→0.843 | 0.442→0.860 | ✅ mechanism visible |
| `action_order` > `constraint_hint` | ✅ 0.634 > 0.552 | ❌ 0.541 < 0.616 | 🛑 **reverses** |
| where PVA's gain comes from | both halves evenly | mostly refusals | 🛑 **domain-dependent** |
| `constraint_hint` is underpowered alone | ✅ CI spans zero | ❌ +0.233, p=0.0000 | 🛑 **reverses** |

The bank-only conclusion *"naming the verifying tool is directionally right but underpowered"* —
which I would have written had I stopped at one domain — **is wrong.** On `online_market` that
same intervention is worth +0.233 at p=0.0000. One domain was not enough to say which rung
mattered, and the held-out domain is what caught it.

## What this does NOT settle

- 🛑 **It is not a test of our ASOP.** PVA is SOPBench's scaffold, in SOPBench's vocabulary, as
  prompt text. Our ASOP format and the `asop_agent.py` gate runtime are untouched. The result
  licenses "this class of intervention helps here", nothing stronger.
- **One executor, and a weak one.** `gpt-oss-20b` baselines at 0.500 / 0.384 — it is failing most
  tasks it should complete. Scaffolding that rescues a weak executor may constrain a strong one;
  `EVIDENCE.md` already carries that hypothesis and this does not test it.
- **Two of seven domains.** The remaining five (`hotel`, `library`, `healthcare`, `dmv`,
  `university`) are queued as follow-up and are not in this page.
- **No adversarial user.** SOPBench's `adv` mode (free, scripted) tries to talk the agent out of
  the SOP. Not run.
- **Service-desk shaped again.** Same domain-class objection `EVIDENCE.md` raises against
  SOPBench generally: this is not domain escape.
- **Single run per task, temperature 0.** No variance estimate within a cell; the CIs are across
  tasks, not across repeated runs of a task.

## Bounds

n=134 + n=172 paired tasks, **two** domains, **one executor**, one run per task, one prompt
design per arm. Ground truth is **procedure compliance plus final state** — "were the required
prior actions performed with matching parameters, and did the database end up right" — which is
the intended scope and is narrower than "the agent was good".

**The single binding limitation is now the executor, not the sample.** Everything here is
`gpt-oss-20b`, which baselines at 0.500 (`bank`) and 0.384 (`online_market`) — it is failing most
tasks it should complete, and scaffolding that rescues a weak executor may constrain a strong one.
`EVIDENCE.md` already carries that hypothesis. A capability-gradient arm on a stronger local model
is the obvious next test and is **not** in this page.

On `bank` the permissible cell is n=48 and `action_order` vs `pva` differ there by 3 tasks — not a
resolvable ordering. More generally, per the replication table above, **only the endpoints of the
ladder are stable**; the middle two rungs swapped between domains and should not be ranked.

Ablation flags are described in SOPBench's source as augmentations "to the oracle setting"; this
run uses `tool_full` (the dominant released convention) rather than `tool_oracle`. The prompts
were confirmed to differ under `tool_full`, so the flags function — but the authors' own framing
was `tool_oracle`, and that variant is unmeasured here.

## Reproducing

```bash
cd ~/Code/SOPBench
VIRTUAL_ENV=$PWD/.venv uv pip install -r requirements.txt

# swarm/constants.py — BOTH edits are required, not just the first
#   OPENAI_MODELS                 += "openai/gpt-oss-20b"   # admits the handler
#   FUNCTION_CALLING_MODELS[openai]+= "openai/gpt-oss-20b"  # admits --tool_call_mode fc

# one arm; --output_dir MUST differ per arm or the arms overwrite each other
OPENAI_BASE_URL=http://localhost:4242/v1 OPENAI_API_KEY=placeholder \
.venv/bin/python run_simulation.py --domain bank \
  --assistant_model "openai/gpt-oss-20b" --num_run_per_interaction 1 \
  --output_dir <per-arm-dir> [--constraint_hint | --action_order | --scaffold pva]

# score (SOPBench's own evaluator; paired across arms).
# SOPBENCH_DIR defaults to ~/Code/SOPBench; LADDER_DIR is the parent of the per-arm dirs.
LADDER_DIR=<ladder-root> .venv/bin/python score_ladder.py bank
```

`score_ladder.py` is archived alongside this page. Raw trajectories are large (~5 MB per arm)
and live under the session scratchpad rather than in-repo; the per-task scored vectors
(`bank-scored.json`) are the reproducible artifact.

### Environment notes that cost time

- **The output filename omits the scaffold and ablation flags.** This is the trap; see pre-flight
  above. Isolate arms by `--output_dir`.
- **`fc` mode asserts against `FUNCTION_CALLING_MODELS`, separately from `OPENAI_MODELS`.**
- `_init_openai()` passes **no** `base_url`, so the SDK falls back to `OPENAI_BASE_URL`. LM Studio
  serves on **:4242**.
- SOPBench's own local path (`OSS_MODELS` → `_init_vllm`) spawns its own vLLM server and is
  CUDA-only — not the route on Apple silicon. Go through the OpenAI-compatible path instead.
- `core.py` routes to the **Responses API** for any model name containing `o1/o3/o4/gpt-5`.
  `openai/gpt-oss-20b` avoids that and uses chat completions, which LM Studio serves. A model id
  containing `gpt-5` would break against LM Studio.
- **Laguna-S-2.1 (71 GB) resident beside the executor put the machine at ~92.6 GB wired of 96 GB
  with swap at 890 MB free**, roughly halving throughput. `lms unload` it first.
- GPU saturates at **4 concurrent streams**; running 8 arms instead of 4 held aggregate throughput
  flat at ~5.3 tasks/min and only delayed per-domain completion.

## Addendum — `library`, a third domain, scored after the fact (not written by the agent that ran it)

The overnight driver finished all four `library` arms but had moved on to `hotel` before this page
was written up; the data sat unscored until this addendum. Scored with the same `score_ladder.py`,
same executor, same methodology as `bank`/`online_market` above — nothing new invented.

**`library`, n=66 paired:**

| arm | success | dirgraph_satisfied | permissible (n=24) | impermissible (n=42) | calls/task |
|---|---|---|---|---|---|
| none | 0.227 | 0.455 | 0.042 | 0.333 | 2.76 |
| hint | 0.379 | 0.591 | 0.167 | 0.500 | 3.21 |
| order | 0.364 | 0.833 | 0.208 | 0.452 | 4.12 |
| **pva** | **0.561** | **0.939** | 0.333 | **0.690** | 4.64 |

`hint` +0.152 (p=0.0129) · `order` +0.136 (p=0.0352) · **`pva` +0.333 [+0.212, +0.455], p=0.0000
— 23 gained, 1 lost.** The strongest single-domain effect measured anywhere in this programme.
Both halves of the split rise again (0.042→0.333 permissible, 0.333→0.690 impermissible) — not an
over-refusal artifact, consistent with `bank` and `online_market`. `dirgraph_satisfied` moves the
most of any domain yet (0.455→0.939 under `pva`).

**Same middle-rung instability as before, a different pairing this time**: `hint` edges `order`
here. ⚠️ **The parenthetical that stood here — "matching `bank`'s relative order, opposite
`online_market`'s" — was wrong and is retracted.** Measured across four domains: `order > hint` on
`bank` and `hotel`; `hint > order` on `online_market` and `library`. A **2–2 split**, with
`bank` and `library` on *opposite* sides. The conclusion (only the endpoints are stable) survives;
the supporting detail did not.

### Status of the sweep, current as of 2026-09-24 08:00

- ✅ **`hotel` — COMPLETE and scored**, n=195 paired. `pva` **+0.200 [+0.120, +0.280], p=0.0000**
  (55 gained / 16 lost). Full table and the permissible-half caveat below:
  `T2-hotel-scored.json`, commit `7462b06`.
- ⏸ **`healthcare` — PAUSED mid-sweep**, deliberately, to free the GPU for the ASOP-V2 iteration
  (which had found a compiler defect making it the critical path — see `DECISIONS.md` N29).
  `none` **124/124 complete**, `hint` **124/124 complete**, `order` 78/124, `pva` 67/124.
  **Not scored on purpose**: tasks execute grouped by action type, so a 67-task intersection is a
  structured subset, not a random sample, and would produce a number that looks like a result and
  is not one. Resumes losslessly — `run_simulation.py` saves after every task.
- ⬜ `dmv`, `university` — never started, nothing to clean up.

🛑 **`hotel` breaks this page's "both halves rise" claim, and that matters.** On `bank`,
`online_market` and `library`, PVA lifted both the should-succeed and should-refuse halves, which
is the evidence this page uses to argue the gain is real work rather than a refusal bias. **On
`hotel` the permissible half moved 3 tasks of 69** (0.043 → 0.072) while the impermissible half
nearly doubled (0.325 → 0.619) — essentially the entire +0.200 came from correct refusals. Nothing
*fell*, so this is not over-gating, but the defensible claim is now narrower: **PVA reliably
improves aggregate success and verification completeness; whether it improves correct completion is
domain-dependent.** The three unrun domains are exactly what would settle that, which is the case
for finishing the sweep rather than treating it as a formality.

Raw scored data: `T2-library-scored.json`, `T2-hotel-scored.json`. Raw trajectories for all six
domains copied out of the prunable session scratchpad to
`~/Code/agentco-harness-eval-archive/2026-09-24-ladder/` (75 files, 129 MB, verified task-count
identical), since `PROJECTS.md` records this programme already losing a checkout to `/private/tmp`
pruning once.

## Provenance

SOPBench (`arXiv:2503.08669`), code **MIT**, released trajectories **CC BY 4.0**. Changes made to
the upstream checkout: two additive model-id registrations in `swarm/constants.py` (local
environment setup only). The PVA scaffold, the ablation flags and the evaluator are upstream and
unmodified — this page spends them, it does not alter them.
