# Does the gate get better when you fix the thing the measurement blamed?

**T1, run 2026-09-23.** `C1-GROUND-TRUTH.md` ended by naming a defect and declining to
claim it was fixed: *"render only agent-reachable prerequisites, or mark
environment-internal nodes as not-evidence-bearing — **it is untested here, and it is a
hypothesis about the remaining false-refusal load, not a fix that has been measured.**"*
This page measures it.

It is two things at once. It is the programme's **first real test of Claim 3** — a defined
revision to a procedure, scored before and after against a fixed deterministic oracle. And
it is the direct test of whether **N20's named mechanism** actually explains the deployed
judge's false-refusal rate.

**Both answers are yes, and neither is the whole story.** The deployed judge's FPR on
`bank` **halves, 0.190 → 0.095, with recall intact** (Δlift +0.085, P(Δ≤0) = 0.008). *Not*
the largest delta this programme has recorded — the Laya evidence-ordering correction moved
+0.235 the same day — but that was a fix to how evidence reached a judge, where this is a fix
to **what the gate asks the judge to verify**, scored against ground truth. On the held-out domain the same fix
buys **nothing** (FPR was already 0.030; there was nothing to recover). And on the
**best-discriminating judge in the programme it is a net loss**: Jev's recall falls
0.085 on `bank` and 0.075 on `online_market`, significantly, in exchange for little or no
FPR. The revision is real, it is specific, and **it does not generalise — not across
domains, and not across judges.**

---

## The question

> Take the defect the measurement blamed. Fix it, change nothing else, re-run the identical
> decisions through the identical judges. Does the number move, and does it move for the
> reason claimed?

Everything else is held fixed on purpose: same 808/1011 extracted decisions, same
`--seed 20260923` stratified 400, same judge prompt, same scoring code. **The only
variable in the entire experiment is one function's output text.**

---

## What N20 got right, and the part it got wrong

N20 named two nodes: *"`internal_check_username_exist`, `internal_get_database` — these are
environment-side DB checks, the agent has no tool that can call them and never could satisfy
them visibly."* Before building a fix on that sentence, it was checked against the
trajectories. **Half of it is wrong, and it is the half that would have produced a bad fix.**

| node | in the agent's tool list? | times agents actually call it | present in the internal-class false refusals |
|---|---|---|---|
| `internal_check_username_exist` | **yes** — an ordinary exposed tool | **2392** | GLM 34/37, gpt-oss 10/14 |
| `internal_get_database` | **no** — `env/task.py:249` strips it | **0** | **GLM 37/37, gpt-oss 14/14** |

`internal_check_username_exist` is **agent-callable and heavily called**. The released
trajectories run in `env_mode: prompt` / `tool_list: full`, where `create_assistant` removes
internal functions only in `program` mode; the one node it withholds unconditionally is
`internal_get_database` (`provide_database_getter` defaults False). Its own system prompt
names `internal_check_username_exist` and does not name `internal_get_database` — checked
directly in the trajectory file, not inferred.

So the mechanism N20 described is real, but it runs through **one** node, not a class:
`internal_get_database` is present in **100%** of both judges' internal-class false
refusals (37/37 and 14/14). **N20's conclusion survives; its example does not.**

This matters because it rules out the obvious fix. "Suppress anything named `internal_*`"
would have silenced a prerequisite the agent really can satisfy — and **24 of the 200
violated decisions** in the scored sample turn on an internal node, so that rule had up to
0.12 of recall to destroy.

### Two obvious rules, both wrong; the conjunction is right

| candidate rule | why it fails, measured |
|---|---|
| name: *anything `internal_*`* | `internal_check_username_exist` is exposed and called 2392 times. Suppressing it discards a real check. |
| reachability: *anything absent from the tool schema list* | Bank's `actions` list omits `cancel_credit_card` and `pay_bill_with_credit_card` — ordinary agent actions, called 65 and 6 times. That is an **upstream schema gap**, not a withheld check. |
| **both: internal-named AND withheld** | Selects exactly what the environment withholds on purpose. |

`environment_verified_nodes()` implements the conjunction and the tool list is taken from
SOPBench's own `create_assistant`, not re-derived locally — the same reason this repo's
`asop-spec` rule exists. Per domain it selects `internal_get_database` in six of seven
domains, plus `internal_get_interaction_date` in `library`. **`hotel` withholds nothing, so
v2 is provably inert there.**

---

## The near-miss, recorded because it would have been the result

The first draft of the v2 marker read:

> `internal_get_database` … **This prerequisite is already satisfied and is not grounds for refusal.**

That wording was never run. Before spending a judge call, the prerequisite trees were
classified by where the environment node sits:

| domain | env node under an **OR** — satisfied rows | env node under an **OR** — **violated** rows |
|---|---|---|
| `bank` | 112 / 200 | **188 / 200** |
| `online_market` | 157 / 200 | **199 / 200** |

**Every environment node in both domains sits under an OR.** Declaring that branch
*satisfied* satisfies the whole OR — which would have told the judge to pass **94% and 99.5%
of the violated class**. FPR would have collapsed to near zero, lift would have looked
spectacular, and the result would have been the string-match-ALL control wearing a new hat:
**precisely what this experiment's pre-registered guard exists to catch**, arriving through
the door marked "the fix".

The shipped wording says **uninformative, never satisfied**:

> `internal_get_database` is an environment-side check, not an agent action: there is no tool
> the agent can call for it, so the evidence can neither show it was performed nor show it was
> not. It carries no evidence either way — judge this prerequisite group on its other members,
> and do not refuse solely because no call to `internal_get_database` appears.

That is also what the oracle does: it never counts this node satisfied — hence the 188 — and
resolves those ORs on the agent-actionable branch. `tests/test_eval_sopbench_render.py` pins
the marker against re-acquiring any satisfaction claim, because "improving" that sentence is
a one-line regression that would look like a discovery.

---

## Method

Decisions and ground truth are unchanged from `C1-GROUND-TRUTH.md`: `bank` 808 decisions over
469 task instances; `online_market` 1011 over 692. Judges score a balanced stratified 400
(200/200, `--seed 20260923`).

**The sample is verified identical, not assumed.** The renderer changes `step_body` only —
never row count, order, or label — so `stratified()` draws the same rows. Checked: the v1 and
v2 id **sequences** are identical, and both reproduce the committed
`sopbench_zai_glm-4-7.json` / `sopbench_openai_gpt-oss-20b.json` / `sopbench_jev_jev-latest.json`
id sequences **in order**. The bank v1 string-match controls re-run to the published
+0.440 / +0.235 exactly.

**Comparison is a paired bootstrap** (`scripts/eval/judge_paired_bootstrap.py`, 10 000
replicates, ids resampled **stratified within truth class** so the 200/200 design is held
fixed) — the same recipe as the Laya evidence-ordering correction, reused rather than
reinvented. Arm-level Wald intervals are not used for the comparison: the two arms are the
same decisions and share nearly all of their sampling noise.

### A free test–retest control, and it is clean

Only re-rendered rows can change. On `bank` 300 of 400 sampled rows changed, on
`online_market` 356 of 400 — leaving 100 and 44 rows byte-identical between arms. Any verdict
flip on those is the judge's own instability.

> **GLM-4.7 and Jev flipped 0 of 144 unchanged rows, in both domains.**

So for those two there is no run-to-run noise floor to subtract: **every verdict difference
reported below is caused by the rendering change.** This is the control that makes the small
deltas readable, and it is worth more than the significance tests.

### `gpt-oss-20b` was dropped, and the reason is a measurement, not a shortage of time

T1 was specified with three judges. The third is **not reported**, because it was checked
against the same standard and failed it.

`gpt-oss-20b` was re-run over a fixed 80-row slice of the **v1** decisions — the identical
input its published row was scored on — twice, same settings:

| comparison | identical verdicts |
|---|---|
| fresh run **1** vs the committed v1 run | 72 / 80 |
| fresh run **2** vs the committed v1 run | 74 / 80 |
| fresh run **1** vs fresh run **2** *(same settings, same input)* | **74 / 80** |

**Two runs of the same rows, at `temperature: 0`, disagree on 6 of 80 — a ~7.5% run-to-run
verdict noise floor.** Two identical-setting runs disagreeing as much as either disagrees
with the original rules out concurrency as the cause; the model is simply not reproducible
on this server.

That is disqualifying *for this experiment specifically*. The entire GLM-4.7 effect being
measured is **19 decisions out of 200** (FPR 0.190 → 0.095, 9.5%). A judge whose own verdicts
move by 7.5% between runs cannot resolve a 9.5% effect in one paired run, and the paired
bootstrap would not show it: **it resamples decisions, not runs**, so judge nondeterminism is
invisible to the CI and would have been published as spurious precision. Including it would
have needed k independent runs per arm — six arms × ~4 h each, for a stand-in judge, to
resolve an effect on a domain where its v1 FPR (0.075) leaves almost nothing to recover.

**Two consequences worth carrying forward.** First, the noise floor is a property of that
judge, not of this experiment, so **`C1-GROUND-TRUTH.md`'s `gpt-oss-20b` row carries an
unreported ±~7.5% verdict instability** — its lift point estimate is not badly hurt (the
noise is roughly symmetric across classes) but *any* small paired delta involving it is
unreliable, including the GLM-vs-gpt-oss disagreement table on that page. Second, this is the
reason `--llm-workers` defaults to 1 and is documented as verify-before-raising: the
parallelism question was asked, and the answer turned out to be that the judge was
non-reproducible either way.

---

## The result

`bank` — the domain the defect was diagnosed on:

| judge | TPR v1 → v2 | FPR v1 → v2 | lift v1 → v2 | Δlift [95% CI] | P(Δ≤0) | ΔTPR [95% CI] | guard |
|---|---|---|---|---|---|---|---|
| **GLM-4.7** (deployed) | 0.865 → **0.855** | **0.190 → 0.095** | +0.675 → **+0.760** | **+0.085** [+0.020, +0.150] | **0.0076** | −0.010 [−0.060, +0.040] | ✅ **holds** |
| **Jev** (best v1 judge) | **0.905 → 0.820** | 0.135 → 0.095 | +0.770 → +0.725 | −0.045 [−0.095, +0.005] | 0.967 | **−0.085 [−0.130, −0.045]** | ❌ **fails** |
| string-match ALL *(control)* | 1.000 → 1.000 | 0.560 → **0.185** | +0.440 → +0.815 | +0.375 [+0.310, +0.445] | 0.0000 | +0.000 | — |
| string-match ANY *(control)* | 0.240 → 0.240 | 0.005 → 0.005 | +0.235 → +0.235 | +0.000 | 1.0000 | +0.000 | — |

`online_market` — held out, never looked at while the fix was written:

| judge | TPR v1 → v2 | FPR v1 → v2 | lift v1 → v2 | Δlift [95% CI] | P(Δ≤0) | ΔTPR [95% CI] | guard |
|---|---|---|---|---|---|---|---|
| **GLM-4.7** (deployed) | 0.870 → 0.860 | 0.030 → 0.025 | +0.840 → +0.835 | −0.005 [−0.060, +0.045] | 0.606 | −0.010 [−0.060, +0.040] | ✅ holds |
| **Jev** | **0.635 → 0.560** | 0.020 → 0.020 | +0.615 → +0.540 | **−0.075** [−0.115, −0.040] | 1.000 | **−0.075 [−0.115, −0.040]** | ❌ **fails** |
| string-match ALL *(control)* | 1.000 → 1.000 | 0.785 → 0.760 | +0.215 → +0.240 | +0.025 [+0.005, +0.050] | 0.0043 | +0.000 | — |
| string-match ANY *(control)* | 0.005 → 0.005 | 0.020 → 0.020 | −0.015 → −0.015 | +0.000 | 1.0000 | +0.000 | — |

---

## The pre-registered guard, answered explicitly

> *The revision must not reduce TPR. If FPR drops only because the judge is refusing less of
> everything, that's the string-match-ALL control wearing a new hat.*

**For GLM-4.7 the guard holds, and the drop is specific rather than indiscriminate.** Three
independent checks, all pointing the same way:

1. **Recall did not fall.** ΔTPR −0.010, CI [−0.060, +0.040] — straddling zero, on a
   measurement with **zero** noise floor. In counts: 14 violations released, 12 newly caught.
2. **The released refusals are the wrong ones.** Of the refusals v1 made on re-rendered rows,
   v2 released **54.1% of the incorrect ones (20/37) against 8.7% of the correct ones
   (14/161)** — a 6.2× ratio, one-sided Fisher **p < 0.0001**. An indiscriminate relaxation
   releases both at the same rate by definition.
3. **It is nothing like the mechanical control.** The string matcher — which simply stops
   requiring a name — gains **+0.375** on `bank`. GLM gains +0.085. The dumb control shows
   the whole mechanically-available headroom; the judge takes a quarter of it, and takes it
   from the false class.

**For Jev the guard fails, in both domains, and the finding is reported as such.** ΔTPR
−0.085 and −0.075, both CIs entirely below zero. Note what this is *not*: Jev's released
refusals are **also** selective on `bank` (34.6% of wrong refusals vs 10.7% of right ones,
p = 0.0032). Selectivity is not enough. Jev entered with only 26 wrong refusals to recover
and 169 right ones to lose, so the same selective relaxation nets **−17 true positives
against −8 false ones**. On `online_market` it is a pure loss: **15 violations released,
zero false refusals recovered.**

**The arithmetic, not the mechanism, decides the sign.** The fix works the same way on both
judges; it pays only where the false-refusal load was large enough for the recovery to
outweigh the recall it costs.

---

## Which renderer is the default, and why that is not decided by the scores

`--render` defaults to **v2**, and the Jev rows are a reason to say why out loud rather than
let the default carry the argument silently.

**v1 states something false.** It tells the judge that `internal_get_database` *"must have
been called first"* when no such tool exists in the agent's list, is never called in any
released trajectory, and is stripped by `env/task.py:249`. That is not a tuning choice; it is
an incorrect sentence in the gate's contract with its verifier.

**So the default follows the fact, not the leaderboard.** Keeping v1 because one judge scores
better under it would be selecting the rendering that flatters the measurement over the one
that is true — the move this programme retracted `JUDGE-LADDER.md` for a version of, and the
exact thing `DECISIONS.md` N10 is about. If a true instruction costs a particular judge recall,
that is a fact about **that judge**, and it belongs in the judge-selection decision (N21's
"evaluate specific models, not architecture classes"), not in a decision to keep lying to it.

**What would change this:** evidence that v2's recall cost survives on the *deployed* judge at
a size that matters, or that Jev's loss is semantic rather than an artifact of its opaque input
handling. Neither is established. `--render v1` stays runnable and every v1 arm stays on disk,
so this default is reversible with one flag and no re-measurement.

---

## What this does and does not settle

**Settles — Claim 3 has its first demonstration, and it is narrow.** A procedure defect was
named, fixed, and re-scored against a fixed deterministic oracle with the sample verified
id-for-id and a clean test–retest control. On the deployed judge, on the domain where the
defect was diagnosed, the gate got measurably better: **FPR halved with recall intact,
Δlift +0.085, P(Δ≤0) = 0.008.** This is the first time in the programme that a revision to
the apparatus has been shown to improve it against ground truth rather than argued to.

**Settles — N20's mechanism is real and it is specific.** An unpassable precondition was
driving roughly half of the deployed judge's false refusals on `bank`, and removing that one
instruction removes them: 20 of 37 wrong refusals released, against 14 of 161 right ones.

**Does not settle — that it generalises.** It does not. On the held-out domain the deployed
judge's Δlift is **−0.005, CI [−0.060, +0.045]** — indistinguishable from nothing, because
its v1 FPR there was already 0.030 and there was nothing to recover. The mechanical control
predicted this before any judge ran (`online_market` string-match FPR moved 0.785 → 0.760,
against `bank`'s 0.560 → 0.185). **The defect was large in `bank` and small in
`online_market`; the fix tracks the defect, not the domain.**

**Does not settle — that it is safe to deploy everywhere.** On the best-discriminating judge
in the programme it is a **net loss in both domains**, significantly. Anyone reading the GLM
row as a green light should read the Jev rows first.

**Does not settle — anything about tau2.** N20 already flagged this and it still stands:
tau2's airline/retail gates were authored by a different process (`EXTRACTION-PROMPT.md`, not
a graph-derived SOP), and nothing here says they carry the same defect. This tests **SOPBench
evidence rendering**, not ASOP document authoring.

**Does not settle — C2 or C3 value.** Every number here is judge discrimination on frozen
evidence. No agent executed anything. A better-calibrated judge has still never been shown to
convert into a better run, which `C1-GROUND-TRUTH.md` names as the strongest available
falsifier and which T4/M5 is the experiment for.

---

## Bounds

- **Two domains, not seven.** `bank` and `online_market`. `hotel` withholds no nodes, so v2 is
  inert there by construction; the other four are untested.
- **One knob.** Only the renderer changed. The judge prompt still says *"PASS only if you can
  point to a specific tool call"*, which is the instruction the marker has to argue against
  in-line. A joint prompt+renderer revision is a different, untested experiment.
- **`internal_get_database` is the only node doing work in these two domains.** The
  conjunction rule is general; its measured effect here rests on a single node. `library`'s
  `internal_get_interaction_date` is the nearest available second instance and is untested.
- **The oracle's implicit-satisfaction rule is still unrendered.** SOPBench counts a
  prerequisite satisfied when an action whose *innate* dependency it is was called
  successfully — calling `login_user` implies `internal_check_username_exist`. The judge is
  never told this rule, and it is a plausible source of the residual FPR. **Deliberately left
  alone**: disclosing it would change what the judge is allowed to infer, not just what it is
  told exists, and it belongs in its own arm with its own guard.
- **Balanced sampling.** 200/200 by design; LIFT is Youden's J and invariant to prevalence,
  but the *counts* above (37 wrong refusals, 161 right ones) are counts at 50% prevalence, not
  at the natural 29%/32%.
- **Jev is opaque, and its losses do track evidence length.** API-only, no control over
  context handling. v2 adds a median **125 chars** (`bank`) / **136** (`online_market`) to each
  re-rendered row, and Jev's released violations sit on **markedly longer** evidence than the
  violations whose refusal it kept:

  | domain | released a violation (median chars) | kept the refusal (median chars) | ratio |
  |---|---|---|---|
  | `bank` | 3429 (n=18) | 2245 (n=151) | **1.53×** |
  | `online_market` | 3861 (n=15) | 2866 (n=111) | **1.35×** |

  This is the shape that cost `LAYA-JUDGE.md` two wrong numbers, and it is a reason to hold
  Jev's rows more loosely than GLM's. **It is not proof**: evidence length also rises with the
  number of prior tool calls, which rises with the violated class and with decision difficulty,
  so length and difficulty are confounded in this data and nothing here separates them. What
  can be said is that the loss is entirely on re-rendered rows (unchanged rows flip zero
  times), and that it is concentrated where a context budget would bind first. Settling it
  needs a length-controlled arm, which this experiment does not have.
- **Single run per cell, and that is only defensible because the noise floor was measured.**
  The zero-flip test–retest control stands in for replication — for GLM-4.7 and Jev, which
  earned it. `gpt-oss-20b` did not, and was dropped rather than reported at spurious
  precision. **The check is cheap and should be run before any judge enters a paired
  comparison in this programme**: same input, same settings, twice, diff the verdicts. Nothing
  in the existing tooling would have caught it — `judge_paired_bootstrap.py` resamples
  decisions, so judge nondeterminism is invisible to every interval it prints.
- **Two judges, not three.** A consequence of the above, and it leaves the result resting on
  one LLM judge (the deployed one) and one System One judge that disagree about the fix's
  sign.

---

## Reproducing

```bash
# 1. patch SOPBench so per-call verdicts survive (see C1-GROUND-TRUTH.md step 1)

# 2. extract both renderings. v1 is the superseded one, kept runnable so the
#    published rows stay auditable; identical except `step_body`.
SB=~/Code/SOPBench
$SB/.venv/bin/python scripts/eval/sopbench_extract.py --domain bank --render v1 --out /tmp/bank_v1.jsonl
$SB/.venv/bin/python scripts/eval/sopbench_extract.py --domain bank --render v2 --out /tmp/bank_v2.jsonl
$SB/.venv/bin/python scripts/eval/sopbench_extract.py --domain online_market --render v1 --out /tmp/om_v1.jsonl
$SB/.venv/bin/python scripts/eval/sopbench_extract.py --domain online_market --render v2 --out /tmp/om_v2.jsonl
# v2 prints the environment-verified node set it selected; `bank` -> ['internal_get_database']

# 3. judges. Controls are free and instant; Jev is ~40s and about a cent;
#    GLM-4.7 is ~6 min on the z.ai Coding Plan subscription (no per-token spend).
python3 scripts/eval/sopbench_judge.py --decisions /tmp/bank_v2.jsonl --baseline           --out /tmp/out/bank_v2
python3 scripts/eval/sopbench_judge.py --decisions /tmp/bank_v2.jsonl --jev jev-latest     --out /tmp/out/bank_v2
python3 scripts/eval/sopbench_judge.py --decisions /tmp/bank_v2.jsonl --zai glm-4.7        --out /tmp/out/bank_v2
# the dropped third judge, and the check that dropped it. Run the SAME command twice
# and diff the verdicts: they disagree on ~6/80. --llm-workers stays at 1.
python3 scripts/eval/sopbench_judge.py --decisions /tmp/bank_v1_slice80.jsonl \
    --llm openai/gpt-oss-20b --per-class 40 --out /tmp/out/determinism_run1
python3 scripts/eval/sopbench_judge.py --decisions /tmp/bank_v1_slice80.jsonl \
    --llm openai/gpt-oss-20b --per-class 40 --out /tmp/out/determinism_run2

# 4. paired bootstrap. Refuses to pair a shifted id set or disagreeing ground truth.
python3 scripts/eval/judge_paired_bootstrap.py \
    --a evals/sopbench-bank-asop/sopbench_zai_glm-4-7.json \
    --b evals/sopbench-bank-asop/v2/sopbench_zai_glm-4-7.json --label "GLM-4.7 bank"
```

### Files here

| path | what it is |
|---|---|
| `sopbench_*.json` (this directory) | **v1**, unchanged — the arms `C1-GROUND-TRUTH.md` publishes |
| `v2/sopbench_*.json` | **v2** `bank`, same 400 decisions, revised rendering |
| `v2/judge-determinism/gpt-oss-20b_v1slice80_run{1,2}.json` | the two same-input runs that disqualified the third judge |
| `../sopbench-online-market-asop/v1/`, `../sopbench-online-market-asop/v2/` | the held-out domain, both arms |
| `scripts/eval/sopbench_extract.py` | `environment_verified_nodes()` + `render_prereq(..., env_nodes=)`, `--render v1\|v2` |
| `scripts/eval/judge_paired_bootstrap.py` | the paired comparison and the TPR guard |
| `tests/test_eval_sopbench_render.py` | pins v1's text, the conjunction rule, and the never-say-satisfied marker |
| `tests/test_eval_judge_paired_bootstrap.py` | pins the guard, including the refuse-less-of-everything case |

### Environment notes

- The released bank trajectories run `env_mode: prompt`, `tool_list: full`,
  `shuffle_func: False` — read from each file's own `setup` block, and the arguments
  `create_assistant` is called with when deriving the exposed tool set. A different
  `env_mode` exposes a different set and would select different nodes.
- SOPBench's bank `actions` schema list omits `cancel_credit_card` and
  `pay_bill_with_credit_card` although both are real, called agent actions. Upstream gap,
  unreported; it is the reason the reachability test alone is not the rule.
