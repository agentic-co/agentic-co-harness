# Do the `bank` findings generalise? Five more domains say: the ranking doesn't, the case for having a judge doesn't, the deployed judge isn't even reproducible — and the one thing that does hold up is T1's rendering fix.

**S1, run 2026-09-23 overnight.** `C1-GROUND-TRUTH.md` measured judges on **one** domain and
said so: *"One domain, offline replay."* `T1-RENDER-FIX.md` measured a rendering fix on **two**
and said so: *"Two domains, not seven … the other four are untested."* Both named the same
falsifier — that `bank` is a favourable special case. This page runs the remaining five
SOPBench domains (`hotel`, `library`, `healthcare`, `dmv`, `university`) through the identical
judges, the identical sampler, and the identical paired-bootstrap scoring, and reports what
survives.

**🚨 The finding that outranks the rest: the deployed judge is not reproducible, and Jev is.**
Run the identical 400 decisions through each judge twice, on **byte-identical evidence**:
**GLM-4.7 changes 34 of its 400 verdicts (8.5%); Jev changes 0.** GLM's flip rate is *higher*
than the ~7.5% floor for which `T1-RENDER-FIX.md` disqualified `gpt-oss-20b` from that
experiment. T1 kept GLM-4.7 on a control that showed 0 flips in 144 rows — a control that
reproduces exactly, and that (as shown below) ran on rows the judge got right **143 times out
of 144**. It measured determinism on the easy rows and generalised it to the borderline rows the
experiment turned on. **Every single-run paired delta this programme has published for GLM-4.7
carries an unreported ±8.5% verdict instability that no interval it printed could see**, because
`judge_paired_bootstrap.py` resamples decisions, not runs. Details, and what it does and does
not do to T1's conclusion, in its own section below.

**Three further headlines. The first two go against the apparatus; the third is the one `bank`
finding that came out of the sweep stronger than it went in.**

1. **🔴 The judge *ranking* does NOT survive — it is domain-dependent.** Across all 14 scored
   cells it is **5 to Jev, 8 to GLM-4.7, 1 tie**; per domain on v1 rendering it is **3–3 with
   one tie**. The three largest gaps all favour GLM-4.7 (`healthcare` 0.496 and 0.391, `dmv`
   0.315). **The reversal was already in the programme's own data**:
   T1's `online_market` table shows GLM-4.7 +0.840 against Jev +0.615, published the same day
   C1 called Jev the best judge in the programme on the strength of a 0.095 gap on `bank`.
   Neither page put the two side by side. **Jev's own spread across domains (+0.374 to +0.845)
   is five times the median gap between the two judges** — the domain matters far more than the
   judge.
2. **⚠️ "A judge beats the dumb control" does NOT survive either.** On `dmv` under v2 rendering
   the deliberately-dumb string-match ALL control reaches **+0.986** — near-perfect — against
   Jev's +0.616, and on `healthcare` v2 it reaches **+0.878** against Jev's +0.235. **`bank` v2
   belongs on that list too**: after T1's own fix, the control (+0.815) outscores both judges on
   the programme's reference domain. **`bank` v1 was a favourable special case for the claim
   that judgment is needed**, which is the claim the gate mechanism rests on. **Which case you
   are in is largely predictable for free**, from the control's own false-refusal rate — see the
   regex section, including where that prediction already failed once.
3. **The T1 rendering fix splits by judge — and for the deployed judge it holds up.** It is
   **provably inert on 2 of 7 domains** (nothing withheld, or the withheld node never rendered).
   On the remaining five, **GLM-4.7 is significantly better on 3 (`library` +0.207, `dmv`
   +0.164, `bank` +0.085) and never significantly worse**, while **Jev is better on 1 and
   significantly worse on 2**. So `--render v2` as the default is now supported by five domains
   for the judge that actually ships, and remains a net loss for Jev — which is what T1
   concluded from two domains, now with the deployed-judge half strengthened rather than
   weakened. T1's explanation — *"it pays only where the false-refusal load was large enough"* —
   predicts the sign in every one of the ten new judge×domain cells.

---

## Why one page instead of five

The question is cross-domain by construction ("does `bank` generalise"), and five
near-identical pages would bury the answer in duplication. Per-domain verdict JSONs live in the
conventional `evals/sopbench-<domain>-asop/{v1,v2}/` directories alongside `bank` and
`online_market`; this page is the analysis over them.

---

## Method — held fixed on purpose

Nothing was re-invented. Identical to `C1-GROUND-TRUTH.md` and `T1-RENDER-FIX.md`:

- **Decisions and ground truth**: `scripts/eval/sopbench_extract.py --domain <name>`, over
  SOPBench's released trajectories, scored by the per-call oracle from
  `per-call-groundtruth.patch`. No agent executed anything; no LLM is anywhere in the ground
  truth.
- **Sample**: `--seed 20260923`, balanced stratified, `--per-class 200` — the same call every
  prior domain used. **Four of five domains cannot supply 200 per class and were run at the
  largest balanced sample they support** (the sampler takes `min(per_class, n_violated,
  n_satisfied)`); the exact n is stated in every table below and never padded.
- **Judges**: `--jev jev-latest` (TypeSafe), `--zai glm-4.7` (the deployed judge, z.ai Coding
  Plan subscription), `--baseline` (both string-match controls). Per T1, `gpt-oss-20b` is
  excluded — it carries a measured ~7.5% run-to-run verdict noise floor. **No local GPU was
  used**; `--llm` was never invoked.
- **Scoring**: `scripts/eval/judge_paired_bootstrap.py`, 10 000 replicates, ids resampled
  stratified within truth class, reused rather than reinvented.
- **LIFT = TPR − FPR** (Youden's J), the same definition as everywhere else in this programme.

### Sample sizes, stated before any result

| domain | decisions | satisfied | violated | distinct tasks | **balanced sample** | why not 400 |
|---|---|---|---|---|---|---|
| `hotel` | 446 | 206 | 240 | 413 | **400** (200/200) | — full sample |
| `library` | 301 | 135 | 166 | 212 | **270** (135/135) | only 135 satisfied exist |
| `healthcare` | 898 | 783 | 115 | 487 | **230** (115/115) | only 115 violated exist |
| `dmv` | 224 | 151 | 73 | 197 | **146** (73/73) | only 73 violated exist |
| `university` | 65 | 55 | 10 | 64 | **20** (10/10) | only 10 violated exist |

`university`'s 20 cells cannot resolve anything and are reported as a bound, not a result.

### A structural finding that arrived before any judge ran

`environment_verified_nodes()` — T1's conjunction rule, unchanged — selects per domain:

| domain | nodes withheld by the environment | rows the v2 renderer actually changes | v1 vs v2 |
|---|---|---|---|
| `hotel` | **none** | 0 / 400 (0%) | **evidence byte-identical** |
| `library` | `internal_get_database`, **`internal_get_interaction_date`** | 225 / 270 (83%) | live contrast |
| `healthcare` | `internal_get_database` | 144 / 230 (63%) | live contrast |
| `dmv` | `internal_get_database` | 138 / 146 (95%) | live contrast |
| `university` | `internal_get_database` | 0 / 20 (0%) | **evidence byte-identical** |

**Two of the five domains have no v1/v2 contrast at all, and this was verified rather than
assumed**: the extracted decision sets were hashed and the `evidence` payloads are identical
between renderings on `hotel` and `university` (only the `render_version` label differs).
T1 predicted the `hotel` case from the rule (*"`hotel` withholds nothing, so v2 is provably
inert there"*) — that prediction is confirmed. **`university` was not predicted**: the rule does
select `internal_get_database` there, but the node never appears in any rendered prerequisite
tree in the extracted decisions, so nothing changes. So of seven domains, **the T1 fix can only
ever do anything in five.**

`library` is the domain T1 flagged as the one untested second instance of its mechanism
(*"`library`'s `internal_get_interaction_date` is the nearest available second instance and is
untested"*). It is tested below.

### The free test–retest control, re-earned

Because `hotel` and `university` render identically under both flags, running both arms buys a
**noise-floor measurement at zero extra design cost** — the check T1 argued should be a
precondition for any paired comparison in this programme.

| judge | domain | rows re-run on byte-identical evidence | verdict flips | run-1 error rate |
|---|---|---|---|---|
| **Jev** | `hotel` | 400 | **0** (0.00%) | 7.8% |
| **Jev** | `university` | 20 | **0** (0.00%) | 0.0% |
| **GLM-4.7** | `hotel` | 400 | **34** (**8.50%**) | 13.5% |
| string-match ALL *(sanity)* | `hotel` | 400 | **0** (0.00%) | 34.5% |

**⚠️ That GLM-4.7 row is a correction to `T1-RENDER-FIX.md`, and it is the most consequential
thing on this page for how the programme reports numbers.** It gets its own section below.

---

## `hotel` — the cleanest generalisation test, and `bank`'s ranking holds

`hotel` is the single best domain for the ranking question: it is the only new domain that
supports the **full 400-cell sample**, it carries the largest violated class in SOPBench (240),
and **v2 is provably inert**, so there is exactly one rendering and no fix to confound the
comparison.

| judge | n | err | TP | FP | FN | TN | TPR | FPR | **lift** | 95% CI | acc | refuses |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Jev** (`jev-latest`) | 400 | 0 | 169 | **0** | 31 | 200 | 0.845 | **0.000** | **+0.845** | [+0.795, +0.895] | 0.922 | 0.422 |
| **GLM-4.7** (deployed) | 400 | 0 | 175 | 29 | 25 | 171 | 0.875 | 0.145 | **+0.730** | [+0.663, +0.797] | 0.865 | 0.510 |
| string-match ALL *(control)* | 400 | 0 | 200 | 138 | 0 | 62 | 1.000 | 0.690 | +0.310 | [+0.246, +0.374] | 0.655 | 0.845 |
| string-match ANY *(control)* | 400 | 0 | 16 | **0** | 184 | 200 | 0.080 | 0.000 | +0.080 | [+0.042, +0.118] | 0.540 | 0.040 |

**The ordering is `bank`'s ordering, and both judges score higher here than there.** Jev
+0.845 (vs +0.770 on `bank`), GLM-4.7 +0.730 (vs +0.675). The gap between them, +0.115, is
wider than `bank`'s +0.095 and the CIs do not overlap. **This is the strongest single judge
result in the programme.**

**Jev's zero false refusals are the number worth pausing on.** 0/200 satisfied decisions
refused, while still catching 169/200 violations. Compare the compounding table
`C1-GROUND-TRUTH.md` built for the deployed judge: at `hotel`'s gate density an FPR of 0.000
compounds to 0.000 no matter how many gates a run passes through. **That is the first judge
arm in this programme for which the false-refusal mechanism (N14) is not available as an
explanation of downstream harm at all.** It is one domain and one run, and it is also the
outcome the apparatus most needs to be real.

**GLM-4.7's false refusals did not vanish, they shrank**: FPR 0.190 on `bank` → 0.145 here,
without the T1 fix, which cannot operate on this domain. So a meaningful part of `bank`'s
deployed-judge false-refusal load was `bank`-specific even before T1 touched it.

**The controls behave, which is what licenses reading the judge numbers.** string-match ALL
reaches +0.310 only by refusing **84.5% of everything**; string-match ANY holds FPR at 0.000
and collapses to TPR 0.080. Neither achieves both; both real judges do. This is the same
scissors as `bank` and it is the reason `hotel` counts as a clean replication.

### The two judges are not making the same mistakes, and the asymmetry is total

`C1-GROUND-TRUTH.md` argued that overlapping lift hides opposite errors, and that *"lift alone
is the wrong summary for choosing a gate judge."* On `hotel` the two judges agree on
**365/400 (91.2%)**, and every single discordant pair falls the same way within its class:

| on the 35 decisions where they disagree | Jev right | GLM-4.7 right | exact p |
|---|---|---|---|
| truth = **violated** (should refuse) | 0 | **6** | 0.031 |
| truth = **satisfied** (should pass) | **29** | 0 | <0.0001 |

**There is no mixed zone.** GLM-4.7 catches 6 violations Jev misses; Jev passes 29 correct
decisions GLM-4.7 refuses. The trade is 29 false refusals bought for 6 extra catches — and by
N14's costing (a missed violation costs one unsafe action, a false refusal costs a run), that
is a bad trade at almost any exchange rate. On `bank` the same comparison against `gpt-oss-20b`
was 40-vs-13 and 3-vs-26, i.e. genuinely two-sided. **Here it is one-sided in both classes at
once, which makes `hotel` the cleanest judge-selection evidence in the programme, and it selects
Jev.**

---

## `library` — where T1's fix works best, and the second environment node finally gets tested

`library` is the domain `T1-RENDER-FIX.md` named as its one untested lead: *"`library`'s
`internal_get_interaction_date` is the nearest available second instance and is untested."*
It is also the only domain in SOPBench where the environment withholds **two** nodes.

Balanced sample **270 (135/135)** — `library` has only 135 satisfied decisions in total, so
this is the largest balanced sample it supports. Not padded.

| judge | render | TPR | FPR | **lift** | 95% CI | acc |
|---|---|---|---|---|---|---|
| **Jev** | v1 | 0.830 | 0.178 | **+0.652** | [+0.561, +0.742] | 0.826 |
| **Jev** | **v2** | 0.815 | **0.052** | **+0.763** | [+0.688, +0.838] | 0.881 |
| **GLM-4.7** | v1 | 0.822 | 0.252 | **+0.570** | [+0.473, +0.668] | 0.785 |
| **GLM-4.7** | **v2** | 0.874 | **0.096** | **+0.778** | [+0.703, +0.853] | 0.889 |
| string-match ALL | v1 | 1.000 | 0.689 | +0.311 | [+0.233, +0.389] | 0.656 |
| string-match ALL | **v2** | 1.000 | 0.222 | **+0.778** | [+0.708, +0.848] | 0.889 |
| string-match ANY | v1/v2 | 0.037 | 0.000 | +0.037 | [+0.005, +0.069] | 0.519 |

**This is the best the T1 fix has ever performed, on both judges, and it is the only domain
where it improves the deployed judge's *recall* as well as its precision.**

| judge | Δlift [95% CI] | P(Δ≤0) | ΔTPR [95% CI] | guard | selectivity (wrong vs right refusals released) |
|---|---|---|---|---|---|
| **GLM-4.7** | **+0.207** [+0.126, +0.296] | 0.0000 | **+0.052** [+0.000, +0.104] | ✅ holds | **67.6% vs 2.8% — 24.4×**, p < 0.0001 |
| **Jev** | **+0.111** [+0.052, +0.170] | 0.0001 | −0.015 [−0.037, +0.000] | ✅ holds | **70.8% vs 1.8% — 38.6×**, p < 0.0001 |
| string-match ALL | +0.467 [+0.385, +0.548] | 0.0000 | +0.000 | — | *(mechanical headroom)* |

Set against T1's own `bank` numbers — GLM Δlift +0.085, selectivity 6.2× — `library` is
**2.4× the effect at 4× the selectivity**, and Jev's 38.6× is the most selective application of
the fix measured anywhere. **GLM-4.7's Δlift of +0.207 is also the only paired GLM result in
this sweep comfortably clear of its 8.5% verdict-noise floor**, which is why it is stated as an
effect at all; the selectivity ratios, being ratios between large classes, are robust to that
noise independently.

**But the second node is confounded, so T1's bound closes only halfway.** Of the 225
re-rendered rows, **all 225** name `internal_get_database` and 81 also name
`internal_get_interaction_date` — **none name the second node alone.** So `library` does not
isolate `internal_get_interaction_date`'s contribution; it confirms the conjunction rule selects
a second real node and that the fix pays where that node appears, but `internal_get_database`
remains the only node whose effect can be attributed. **T1's bound — "`internal_get_database` is
the only node doing work" — is still standing, now across three domains rather than two.**

**And `library` v2 is also where the regex catches up.** string-match ALL reaches **+0.778**,
exactly tying GLM-4.7 and edging Jev. The profiles differ sharply (control 1.000/0.222 against
GLM 0.874/0.096), so this is not "the judge is pointless" — but on the headline metric, the fix
that made the judge better made the free control better *faster*. That is the pattern the regex
section below generalises.

---

## `healthcare` — a regex outscores both judges, and the fix is purely destructive

Balanced sample **230 (115/115)** — `healthcare` has 898 gated decisions but only **115
violated** (a 13% violation rate), so 115 per class is the ceiling. Not padded.

| judge | render | TPR | FPR | **lift** | 95% CI | acc |
|---|---|---|---|---|---|---|
| **GLM-4.7** | v1 | 0.835 | 0.070 | **+0.765** | [+0.683, +0.847] | 0.883 |
| **GLM-4.7** | v2 | 0.809 | 0.078 | **+0.730** | [+0.643, +0.817] | 0.865 |
| **Jev** | v1 | 0.443 | 0.070 | **+0.374** | [+0.272, +0.476] | 0.687 |
| **Jev** | v2 | 0.304 | 0.070 | **+0.235** | [+0.139, +0.331] | 0.617 |
| string-match ALL | v1 | 1.000 | 0.287 | **+0.713** | [+0.630, +0.796] | 0.857 |
| **string-match ALL** | **v2** | 0.991 | 0.113 | **🔴 +0.878** | [+0.818, +0.939] | **0.939** |
| string-match ANY | v1/v2 | 0.061 | 0.070 | −0.009 | [−0.073, +0.055] | 0.496 |

**Under v2 the free control is the best gate on this domain by a clear margin** — +0.878 against
GLM-4.7's +0.730 and Jev's +0.235, with the **highest accuracy of any arm anywhere in this
programme (0.939)** and non-overlapping CIs against both judges. A regex with no model behind it.

**Jev collapses here, and it is not a false-refusal problem.** Its FPR is 0.070 — fine — but its
recall is **0.443**, half what it manages on `hotel`. Of the 64 violations it misses in v1, the
grep catches **64 of 64 (100%)**; in v2 it misses 80 and the grep catches 79. **Jev is not
finding a different kind of signal the matcher lacks; it is discarding signal the matcher
already has.**

**Against GLM-4.7 it is near-domination, not a trade.** Both judges false-refuse exactly **8**
satisfied decisions — and paired, it is the **identical set of 8 ids**. GLM catches 96
violations to Jev's 51 and misses only 5 that Jev caught. Same cost, twice the recall.

### The rendering fix has nothing to recover here, and both judges pay for it

| judge | Δlift [95% CI] | P(Δ≤0) | ΔTPR | guard | selectivity: wrong vs right refusals released |
|---|---|---|---|---|---|
| **Jev** | **−0.139** [−0.217, −0.061] | 1.0000 | −0.139 | ❌ **fails** | **0 / 8 (0%)** vs 19 / 47 (40.4%) |
| **GLM-4.7** | −0.035 [−0.104, +0.035] | 0.854 | −0.026 | ✅ holds | **0 / 8 (0%)** vs 10 / 92 (10.9%) |
| string-match ALL | **+0.165** [+0.096, +0.243] | 0.0000 | −0.009 | — | *(mechanical)* |

**Neither judge releases a single wrong refusal — 0 of 8, both.** T1's mechanism requires false
refusals caused by the unpassable node; on `healthcare` there are none to find, because the
node accounts for none of the 8. So the marker can only subtract, and it does: Jev loses 0.139
of lift, significantly, guard failed. **GLM-4.7's −0.035 sits inside its measured 8.5%
verdict-noise floor and is reported as "no detectable effect", not as a small loss.**

**The one arm the fix helps on this domain is the one with no model in it.** string-match ALL
gains **+0.165**, because its false refusals *were* the env-node rows (its v1 FPR of 0.287 is
exactly the 33/115 satisfied rows that name the node). **The fix removes an unpassable
requirement; a matcher is the only judge here that was mechanically bound by it.**

**Why this domain behaves differently.** The violated class is defined almost entirely by a
missing prerequisite *name*, and only 33 of 115 satisfied rows carry the confusing env node —
so name-presence alone separates the classes at 0.991/0.113. `healthcare` is the extreme of the
regex section's pattern: **the more decidable the precondition, the less a judge adds, and the
more a judge's own recall failures cost.**

---

## `dmv` — the fix's largest win on the deployed judge, and the regex still beats it

Balanced sample **146 (73/73)** — only 73 violated decisions exist. **95% of sampled rows carry
the environment node**, the highest of any domain, so `dmv` is where the rendering fix has the
most surface to act on.

| judge | render | TPR | FPR | **lift** | 95% CI | acc |
|---|---|---|---|---|---|---|
| **string-match ALL** | **v2** | 0.986 | **0.000** | **🔴 +0.986** | [+0.960, +1.000] | **0.993** |
| **GLM-4.7** | v2 | 0.959 | 0.027 | **+0.932** | [+0.873, +0.990] | 0.966 |
| **GLM-4.7** | v1 | 0.836 | 0.068 | **+0.767** | [+0.664, +0.870] | 0.884 |
| **Jev** | v1 | 0.630 | **0.000** | **+0.630** | [+0.519, +0.741] | 0.815 |
| **Jev** | v2 | 0.616 | **0.000** | **+0.616** | [+0.505, +0.728] | 0.808 |
| string-match ANY | v1/v2 | 0.219 | 0.000 | +0.219 | [+0.124, +0.314] | 0.610 |
| string-match ALL | v1 | 1.000 | 0.890 | +0.110 | [+0.038, +0.181] | 0.555 |

**The control moves from worst arm to best arm on one rendering change: +0.110 → +0.986, a
Δ of +0.877.** Its v1 false-refusal rate was 0.890 — it refused 94.5% of everything — and
essentially *all* of that was the unpassable `internal_get_database` requirement. Remove it and
name-presence alone classifies this domain at 0.986/0.000. **This is the cleanest demonstration
in the programme that the T1 marker's effect is mechanical rather than interpretive.**

**It is also the fix's biggest genuine win on the judge that is actually deployed.**

| judge | Δlift [95% CI] | P(Δ≤0) | ΔTPR [95% CI] | guard | selectivity |
|---|---|---|---|---|---|
| **GLM-4.7** | **+0.164** [+0.068, +0.274] | 0.0009 | **+0.123** [+0.041, +0.219] | ✅ holds | 60.0% vs 3.3% — **18.3×**, p = 0.002 |
| **Jev** | −0.014 [−0.068, +0.027] | 0.815 | −0.014 | ✅ holds | 0 wrong refusals existed to release |
| string-match ALL | **+0.877** [+0.795, +0.945] | 0.0000 | −0.014 | — | *(mechanical)* |

GLM-4.7's **recall rises** (+0.123, CI entirely above zero) while its FPR falls — the fix is
not buying precision with recall here, it is improving both, and at +0.164 the effect is clear
of the 8.5% noise floor. **Jev has nothing to gain: its v1 FPR is already 0.000**, so there are
literally zero wrong refusals for the marker to release, and it loses a token 0.014.

**But the regex still wins the domain, in both classes.** +0.986 against GLM's +0.932, at FPR
0.000 against 0.027 and TPR 0.986 against 0.959. **A judge that improved a lot is still second
to a matcher with no model in it.**

## `university` — not a result, reported so the sweep is complete

The entire domain contains **10 violated decisions**, so the balanced sample is **20 cells**.
**Jev, GLM-4.7 and string-match ALL all score +1.000; string-match ANY scores +0.000.** v2 is
inert (the rule selects `internal_get_database`, but the node never appears in any rendered
prerequisite tree here). Nothing can be concluded from 20 cells, and nothing is. It contributes
one thing only: a second, smaller reproducibility control, where **GLM-4.7 flipped 1 of 20
verdicts (5.0%) on byte-identical evidence and Jev flipped 0** — consistent with the `hotel`
measurement, and the reason `university` appears at all.

---

## 🔴 Question 1 answered: the judge ranking does **not** generalise — it is domain-dependent

This is the question `C1-GROUND-TRUTH.md` and `JEV-JUDGE.md` both flagged as untested, and the
answer is cleaner than expected: **there is no stable ordering between the two real judges.**

| domain | render | Jev | GLM-4.7 | gap | winner |
|---|---|---|---|---|---|
| `bank` | v1 | **+0.770** | +0.675 | +0.095 | Jev |
| `bank` | v2 | +0.725 | **+0.760** | −0.035 | GLM-4.7 |
| `online_market` | v1 | +0.615 | **+0.840** | **−0.225** | **GLM-4.7** |
| `online_market` | v2 | +0.540 | **+0.835** | **−0.295** | **GLM-4.7** |
| `hotel` | v1 | **+0.845** | +0.730 | +0.115 | Jev |
| `hotel` | v2 | **+0.845** | +0.740 | +0.105 | Jev |
| `library` | v1 | **+0.652** | +0.570 | +0.081 | Jev |
| `library` | v2 | +0.763 | **+0.778** | −0.015 | GLM-4.7 |
| `healthcare` | v1 | +0.374 | **+0.765** | **−0.391** | **GLM-4.7** |
| `healthcare` | v2 | +0.235 | **+0.730** | **−0.496** | **GLM-4.7** |
| `dmv` | v1 | +0.630 | **+0.767** | −0.137 | **GLM-4.7** |
| `dmv` | v2 | +0.616 | **+0.932** | **−0.315** | **GLM-4.7** |
| `university` | v1 | +1.000 | +1.000 | +0.000 | tie *(n=20)* |
| `university` | v2 | +1.000 | +0.900 | +0.100 | *(GLM noise flip)* |

**Five cells to Jev, eight to GLM-4.7, one tie — and the three largest gaps in the table all go
to GLM-4.7** (`healthcare` v2 −0.496, `healthcare` v1 −0.391, `dmv` v2 −0.315). Counting one
vote per domain on the v1 rendering it is **3–3 with one tie**: Jev leads `bank`, `hotel`,
`library`; GLM-4.7 leads `online_market`, `healthcare`, `dmv`. A dead split either way you
count it. `bank` v1 — the single cell on which `C1-GROUND-TRUTH.md` called Jev *"the
best-discriminating judge measured in this programme, LLM or otherwise"* — is one of Jev's five.

*(`university` v2 is listed for completeness only: its +0.100 "Jev win" is one GLM verdict
flipping on byte-identical evidence in a 20-cell sample — an instance of the noise floor, not a
measurement.)*

**The programme's own data already contained the reversal.** `T1-RENDER-FIX.md`'s
`online_market` table, published the same day as C1's claim, shows GLM-4.7 at **+0.840** against
Jev's **+0.615** — a 0.225 gap in the opposite direction, larger than the 0.095 gap the claim
was made on. Neither page reconciles the two. **This is not a new measurement contradicting an
old one; it is two numbers in the same programme, from the same day, that were never put side by
side.** Putting the seven domains in one table is most of what this page did.

**The within-judge spread dwarfs the between-judge gap, which is the real finding.** Jev ranges
**+0.374 (`healthcare`) to +0.845 (`hotel`)** — a spread of **0.471**. GLM-4.7 ranges +0.570 to
+0.840, a spread of 0.270. The median between-judge gap is **0.095**. **Which domain you are on
matters roughly five times more than which of these two judges you pick.** Any future sentence
of the form "judge X is better" needs a domain attached or it is not a claim about anything.

**`healthcare` is the sharpest case, and it is not a trade-off.** GLM-4.7 does not buy its
+0.391 advantage with extra false refusals — **its 8 false refusals are the *identical set* as
Jev's 8**, and it catches 96 violations to Jev's 51, missing only 5 that Jev caught. On this
domain GLM-4.7 **near-dominates** Jev: same errors on the satisfied class, nearly twice the
recall on the violated one. (That both judges — unrelated architectures, unrelated training —
false-refuse exactly the same 8 satisfied rows points at those rows' evidence rather than at
either judge; worth a look, not looked at here.)

**What this does not say.** It does not say GLM-4.7 is the better judge. On `hotel` its error
profile is strictly worse in both classes (the 29-vs-6 disagreement table above), and it is the
judge that flips 8.5% of its verdicts on identical input while Jev flips none. **Discrimination
ranking and deployment suitability are separate questions, and they do not agree here.**

---

## ⚠️ The deployed judge is **not** reproducible, and T1's control measured the wrong rows

`T1-RENDER-FIX.md` dropped `gpt-oss-20b` for a **~7.5% run-to-run verdict noise floor**,
arguing — correctly — that *"a judge whose own verdicts move by 7.5% between runs cannot
resolve a 9.5% effect in one paired run, and the paired bootstrap would not show it: it
resamples decisions, not runs."* It then kept GLM-4.7 and Jev on the strength of this control:

> **GLM-4.7 and Jev flipped 0 of 144 unchanged rows, in both domains.** So for those two there
> is no run-to-run noise floor to subtract: **every verdict difference reported below is caused
> by the rendering change.**

**That control reproduces exactly — and it is not representative.** Re-derived here from the
committed arms: 100 unchanged rows on `bank`, 44 on `online_market`, 0 flips for both judges.
T1 measured what it said it measured. The problem is *which rows* it measured:

| population | n | GLM-4.7 errors in run 1 | error rate |
|---|---|---|---|
| **`bank`: the unchanged rows T1 used as its control** | 100 | **1** | **1.0%** |
| **`online_market`: the unchanged rows T1 used as its control** | 44 | **0** | **0.0%** |
| `bank`: all 400 sampled rows | 400 | 65 | 16.2% |
| `online_market`: all 400 sampled rows | 400 | 32 | 8.0% |
| **`hotel`: all 400 rows (this page's control)** | 400 | 54 | **13.5%** |

**T1's noise floor was measured on a population the judge got right 143 times out of 144.**
That is not an accident of sampling: a row is "unchanged" exactly when its prerequisite tree
does *not* name the withheld environment node, and T1 itself established that node was present
in **100% of GLM-4.7's false refusals on `bank`**. The control was, by construction, the subset
of rows on which the judge was not making mistakes — and determinism on easy rows was
generalised to the borderline rows the entire experiment turned on.

**On a representative population the deployed judge flips 8.5% of its verdicts at
`temperature: 0`** — *higher* than the 7.5% that disqualified `gpt-oss-20b`. Ruled out before
concluding anything, in the order `C1-GROUND-TRUTH.md` argues for:

- **The prompt is identical.** `build_prompt()` reads only `step_body`, `tool_history` and
  `transcript`; `render_version` is not sent. The `evidence` payloads of `hotel` v1 and v2 hash
  identically, so both runs issued byte-identical requests.
- **Nothing was truncated.** 400/400 `finish_reason: end_turn` in both runs, 0 errors, 0
  unparsed verdicts — the check C1 made a precondition after being burned twice.
- **The generations really are independent.** Only **15 of 400** raw completions are
  byte-identical between runs, which is what a thinking model served off a non-deterministic
  backend looks like at `temperature: 0`.
- **The comparison code is not the bug.** The string-match control, run through the identical
  pipeline over the identical pair of files, flips **0 of 400**.

### What this does and does not do to T1

**It does not overturn T1's conclusion.** T1's central claim does not rest on the paired lift
delta; it rests on *selectivity* — that v2 released **54.1% of GLM-4.7's wrong refusals against
8.7% of its right ones, 6.2×, Fisher p < 0.0001**. That statistic is a ratio between two large
classes and is robust to symmetric verdict noise. It reproduces exactly from the committed
arms here (see the selectivity table below), and it still says the fix did what T1 said it did.

**It does retire two specific sentences.** "There is no run-to-run noise floor to subtract" and
"every verdict difference reported below is caused by the rendering change" are both withdrawn
for GLM-4.7. T1's GLM-on-`bank` Δlift of **+0.085 is 19 decisions out of 400**, and the same
judge moves **34 decisions out of 400 by doing nothing at all**. The bootstrap CI
[+0.020, +0.150] resamples decisions, not runs, so it never saw that. **The point estimate's
direction is supported by the selectivity result; its interval is not trustworthy as published.**

**Jev is the one judge that earned the claim.** 0 flips in 400 at a 7.8% error rate — a
population with real borderline mass, unlike T1's control. Combined with `hotel`'s one-sided
disagreement table, **Jev is better than GLM-4.7 on this task in three independent senses:
higher lift, strictly better error profile, and reproducible.**

**Carried forward into this page's own method.** Every GLM-4.7 paired delta below is reported
with this floor stated. A GLM Δlift smaller than roughly ±0.085 on a 400-row arm is **inside
measured noise** and is not claimed as an effect; where a directional claim is made for GLM, it
is made on the selectivity statistic, not on Δlift.

---

## ⚠️ On three of seven domains a regex beats every judge — and you can predict which, for free

`C1-GROUND-TRUTH.md` introduced the string-match controls with a precise purpose: *"If a string
matcher scores near an LLM judge, the task is 'read a list', not 'adjudicate a precondition',
and the LLM's lift says little about judgment."* On `bank` the controls did their job and lost,
which is the basis for treating the gate as needing judgment at all. **Across seven domains
that no longer holds.**

Sorted by the control's own false-refusal rate, which is the point:

| domain | render | **str-ALL FPR** *(free, no model)* | str-ALL lift | best judge lift | winner |
|---|---|---|---|---|---|
| `dmv` | v2 | **0.000** | **+0.986** | +0.932 | 🔴 **regex** |
| `university` | v1 | **0.000** | +1.000 | +1.000 | 🟡 tie *(n=20)* |
| `university` | v2 | **0.000** | +1.000 | +1.000 | 🟡 tie *(n=20)* |
| `healthcare` | v2 | **0.113** | **+0.878** | +0.730 | 🔴 **regex** |
| `bank` | v2 | **0.185** | **+0.815** | +0.760 | 🔴 **regex** |
| `library` | v2 | **0.222** | **+0.778** | +0.778 | 🟡 **tie** |
| `healthcare` | v1 | **0.287** | +0.713 | **+0.765** | judge *(narrow, CIs overlap)* |
| `bank` | v1 | 0.560 | +0.440 | +0.770 | judge |
| `library` | v1 | 0.689 | +0.311 | +0.652 | judge |
| `hotel` | v1 | 0.690 | +0.310 | +0.845 | judge |
| `hotel` | v2 | 0.690 | +0.310 | +0.845 | judge |
| `online_market` | v2 | 0.760 | +0.240 | +0.835 | judge |
| `online_market` | v1 | 0.785 | +0.215 | +0.840 | judge |
| `dmv` | v1 | 0.890 | +0.110 | +0.767 | judge |

All 14 cells complete — "best judge" is max(Jev, GLM-4.7) on every row.

### ⚠️ The rule was stated before those arms landed, and the first one to land broke it

An earlier revision of this section claimed a **perfect** separation at str-ALL FPR ≤ 0.287,
while three rows still had a pending GLM-4.7 arm. It flagged, in advance, that *"a pending arm
can weaken the regex claim on `healthcare`/`dmv`/`university` but cannot strengthen it."*
**The `healthcare` v1 arm then landed at GLM-4.7 +0.765, above the control's +0.713, and moved
that row out of the regex column.** Recorded rather than quietly edited, because the pre-
registration is the only reason the failure is legible at all.

**What survives is weaker and still useful.** Every cell with str-ALL FPR ≤ 0.222 goes to the
regex or ties it; every cell with str-ALL FPR ≥ 0.287 goes to a judge. The separation is intact
but the empty band has narrowed from [0.287, 0.560] to **[0.222, 0.287]**, and `healthcare` v1
now sits on the boundary as a near-tie with overlapping CIs (+0.765 vs +0.713). **A threshold
fitted to 14 points with one boundary case is a hypothesis, not a rule** — and two rows
(`dmv` v2, `healthcare` v2) can still move it. The defensible version is the direction, not the
cut: *the lower the control's own false-refusal rate, the less a model judge has to offer*, and
the control costs nothing to run first.

That is still a free, deterministic, pre-registerable test: run the
string-match control first — it needs no model, no API and no GPU — and its own false-refusal
rate tells you whether a model judge can add anything on that domain before you spend a cent.

**Note which row is in there: `bank` v2.** After T1's own fix, on the programme's reference
domain, the deliberately-dumb control (+0.815) **outscores both the deployed judge (+0.760) and
Jev (+0.725) on lift.** That is in T1's published table; T1 uses the control's +0.375 gain as a
diagnostic for mechanically-available headroom — correctly — but does not remark that the
control ends up ahead. **The fix did not merely improve the gate; on lift it moved the domain
from one where judgment paid to one where it did not.**

**Stated fairly, because lift is not the deployment criterion.** `C1-GROUND-TRUTH.md` is right
that *"lift alone is the wrong summary for choosing a gate judge"* — the two error types do not
cost the same. The regex wins these cells with a TPR of 1.000 and whatever FPR the domain
gives it; GLM-4.7 on `bank` v2 runs 0.855/0.095. So the correct reading is **not** "the judge is
useless," it is: *where the precondition is name-checkable, a deterministic check dominates the
judge on recall and the only remaining question is which false-refusal rate you prefer* — and
on `dmv` v2 and `university` the regex wins that too, at FPR 0.000.

This is the complementary half of `JUDGE-LADDER.md`'s closing hypothesis. That file concluded
*gate mechanisms work; model-judged gates on ambiguous operational rules do not*. The sweep adds:
**on unambiguous rules, model-judged gates are worse than the deterministic check they replaced.**
The band where a model judge is the right tool is narrower from both sides than `bank` suggested.

### And the handicap the control was given turns out to be empty

`judge_stringmatch`'s docstring names its own limitation: *"Neither checks parameters, which the
real oracle does. That gap is the point: it is the part a matcher cannot reach."* Measured
across every domain and both renderings, that gap contains **almost nothing**:

| violations a name-only grep misses (i.e. the prerequisite was called, with wrong parameters) |
|---|
| **1 of 1 048 violated decisions**, across 7 domains × 2 renderings (`healthcare` v2 and `dmv` v2 contribute the only misses, 1 each; every other cell is 0 of 200/135/115/73/10) |

**SOPBench's per-decision oracle is, in practice, a name-presence oracle.** Parameter mismatch
exists in the evaluator, but it essentially never occurs as the *sole* reason a decision is
violated in the released trajectories. This bounds the whole C1 programme, not just this page:
**the proposition these judges are being scored on is "was this named tool called first", and
almost never "with the right arguments".** `C1-GROUND-TRUTH.md` already scopes its ground truth
to *procedure compliance*, and describes it as "checkable facts about what happened" — this
quantifies how checkable. It is the reason a regex can compete at all, and it is the strongest
available caution against reading these lift numbers as evidence about judgment in general.

### Jev's calibration survives everywhere, but it is a third the size `bank` advertised

`C1-GROUND-TRUTH.md` singled this out as *"the one property worth taking seriously"*: Jev's
confidence is *"properly calibrated, not degenerate: median 0.94, but 0.89 average when correct
vs 0.74 when wrong (354/46 split) — it knows more often than not when it is right."*
Reproduced here from the committed arm — **354/46, 0.892 vs 0.742, to three decimals** — and
then measured on the other six domains:

| domain | render | correct / wrong | mean conf when correct | mean conf when wrong | **gap** |
|---|---|---|---|---|---|
| `bank` | v1 | 354 / 46 | 0.892 | 0.742 | **+0.150** |
| `online_market` | v1 | 323 / 77 | 0.875 | 0.592 | **+0.283** |
| `hotel` | v1 | 369 / 31 | 0.914 | 0.849 | **+0.065** |
| `library` | v1 | 223 / 47 | 0.899 | 0.847 | **+0.051** |
| `healthcare` | v1 | 158 / 72 | 0.749 | 0.645 | **+0.104** |
| `dmv` | v1 | 119 / 27 | 0.941 | 0.889 | **+0.053** |
| `dmv` | v2 | 118 / 28 | 0.919 | 0.908 | **+0.011** |
| `university` | v1/v2 | 20 / 0 | 0.969 | *(never wrong)* | — |

**The direction holds in all 13 scorable cells — the gap is positive every time, so the property
is real and it generalises.** What does not generalise is its *size*: on the three new domains
where Jev is strongest (`hotel`, `library`, `dmv`) the gap is **0.051–0.065, roughly a third of
`bank`'s**, and on `dmv` v2 it is +0.011. A downstream consumer that thresholds on Jev's
confidence to route borderline gates to a human would separate well on `bank` and
`online_market` and barely at all on `hotel`, `library` or `dmv`. **`bank` and `online_market`
are the two most favourable domains for this property out of seven, and they are the two it was
established on.**

---

## The whole sweep in one table

| domain | n | Jev | GLM-4.7 | str-ALL | **v1 ranking** | **Jev Δ(v1→v2)** | **GLM Δ(v1→v2)** |
|---|---|---|---|---|---|---|---|
| `bank` | 400 | **+0.770** | +0.675 | +0.440 | Jev > GLM > grep | −0.045 [−0.095, +0.005] | **+0.085** [+0.020, +0.150] ⚠️ |
| `online_market` | 400 | +0.615 | **+0.840** | +0.215 | GLM > Jev > grep | **−0.075** [−0.115, −0.040] | −0.005 [−0.060, +0.045] |
| `hotel` | 400 | **+0.845** | +0.730 | +0.310 | Jev > GLM > grep | — *(v2 inert)* | — *(v2 inert)* |
| `library` | 270 | **+0.652** | +0.570 | +0.311 | Jev > GLM > grep | **+0.111** [+0.052, +0.170] | **+0.207** [+0.126, +0.296] |
| `healthcare` | 230 | +0.374 | **+0.765** | +0.713 | GLM > **grep** > Jev | **−0.139** [−0.217, −0.061] | −0.035 [−0.104, +0.035] |
| `dmv` | 146 | +0.630 | **+0.767** | +0.110 | GLM > Jev > grep | −0.014 [−0.068, +0.027] | **+0.164** [+0.068, +0.274] |
| `university` | 20 | +1.000 | +1.000 | +1.000 | *all tie (no power)* | — *(v2 inert)* | — *(v2 inert)* |

⚠️ `bank`'s GLM Δ of +0.085 is 19 decisions, and GLM-4.7 moves 34 decisions in 400 by being
re-run on identical input. It sits **at** the noise floor; the direction is supported by
selectivity (6.2×, p < 0.0001), the interval is not.

**Reading the ranking column: 3 domains to Jev, 3 to GLM-4.7, 1 tie.** A dead split.

**Reading the Δ columns — and this is a genuine correction to this page's own earlier framing.**
The fix does not behave the same way on the two judges, and for the **deployed** judge the news
is better than `bank` alone suggested:

| | significantly positive | null | significantly negative |
|---|---|---|---|
| **GLM-4.7** (deployed) | **3** — `library` +0.207, `dmv` +0.164, `bank` +0.085⚠️ | 2 — `online_market`, `healthcare` | **0** |
| **Jev** | 1 — `library` +0.111 | 2 — `bank`, `dmv` | **2** — `healthcare` −0.139, `online_market` −0.075 |

**For GLM-4.7, across five live domains, v2 is never significantly worse and is better on three
— two of them clear of its noise floor.** That is a stronger claim than T1 was able to make from
`bank` alone, and it is the single most deployment-relevant result on this page: **the default
`--render v2` is now supported by five domains rather than argued from one.** For Jev it remains
a net loss, as T1 found.

**What predicts the sign is the false-refusal load available to recover**, exactly as T1 said.
Sorted by each judge's own v1 FPR, the fix pays where there was something to recover and costs
where there was not — `dmv`/Jev (FPR 0.000, nothing to release, −0.014) and `healthcare`/both
(0 of 8 wrong refusals releasable, both judges lose) are the clean negative controls, and
`library` (Jev FPR 0.178 → 0.052) and `dmv` (GLM 0.068 → 0.027, TPR *up*) the clean positives.

---

## What this does and does not settle

**Settles — `bank` was a favourable special case, on every axis it was used to establish.**
The judge ranking reverses (4 cells Jev / 5+ GLM-4.7). The claim that a judge beats the trivial
control fails on `healthcare`, `dmv` and — after T1's own fix — on `bank` itself. Jev's
calibration gap shrinks to a third. The rendering fix's sign flips by domain. **Every
generalisation `C1-GROUND-TRUTH.md` and `T1-RENDER-FIX.md` explicitly declined to make was the
right call, and each of the five new domains supplies a reason why.**

**Settles — the deployed judge is not reproducible, and the programme's control for that
measured the wrong population.** 34 of 400 verdicts change on byte-identical evidence at
`temperature: 0`, against Jev's 0 of 400. This is a property of the judge, not of this page, so
it applies retroactively to every single-run paired delta published for GLM-4.7.

**Settles, provisionally — what actually predicts whether a model judge earns its place is the
decidability of the precondition, and it is measurable for free.** The string-match control's
own false-refusal rate separates the domains where a judge helps from the domains where it
loses to a regex, across 14 cells with one boundary case. This is `JUDGE-LADDER.md`'s hypothesis
approached from the other side: *ambiguous* preconditions defeat model judges, and *unambiguous*
ones make them redundant.

**Does NOT settle — anything about C2/C3, and this remains the half that matters.** Every
number here is judge discrimination on frozen evidence. No agent executed anything, no run was
gated, no pass rate moved. `C1-GROUND-TRUTH.md` names the real falsifier and it is still
unrun: *"If a live SOPBench arm gated by this judge fails to beat ungated on pass rate, then C1
discrimination does not convert into C2/C3 value even where discrimination is real."* Nothing
here reopens the airline −0.240 or retail −0.083 results.

**Does NOT settle — that a regex is the right gate.** Lift is not the deployment criterion, as
`C1-GROUND-TRUTH.md` argues at length. The control wins its cells with TPR ≈ 1.000 and a
domain-dependent FPR; on `dmv` v1 that FPR is 0.890, which would be catastrophic in deployment.
**What is established is narrower and still useful: on name-checkable preconditions a
deterministic check dominates a model judge on recall at comparable or better cost**, which
makes "use a model judge here" a claim that now needs defending per domain.

**Does NOT settle — that these judges would behave this way on ASOP's own gates.** T1's bound
stands unchanged: *tau2's airline/retail gates were authored by a different process
(`EXTRACTION-PROMPT.md`, not a graph-derived SOP)*. This measures SOPBench preconditions.

## Bounds

- **Offline replay, one prompt design, one sample per cell.** As in both prior pages. The single
  run per cell is *not* excused by a noise floor here — for GLM-4.7 it is explicitly not, and
  every GLM delta is read against ±8.5%.
- **`university` is not a result.** 10 violated decisions exist in the entire domain; the
  balanced sample is 20 cells. Every arm scores +1.000. It bounds nothing and is reported only
  so the sweep is complete.
- **Four of five new domains are under-powered relative to `bank`.** `library` 270, `healthcare`
  230, `dmv` 146, `university` 20, against 400. The minority class is the binding constraint in
  every case and was never padded.
- **Ground truth is name-presence in practice.** 1 of 1 048 violated decisions across all seven
  domains is a parameter-only violation. The oracle *can* check parameters; the released
  trajectories essentially never exercise it. This bounds how much any of these lift numbers say
  about judgment.
- **The v1/v2 contrast exists in only five of seven domains**, and in three of those the single
  node `internal_get_database` does all the work. `library`'s second node is present but never
  appears alone, so T1's "one node" bound is still standing.
- **Two judges.** `gpt-oss-20b` was excluded on T1's reproducibility grounds and no local model
  was run at all (LM Studio was in use by another experiment). No size control, no third family.
- **Jev is opaque.** API-only, no control over context handling; T1's length-confound caution
  applies unchanged.

## Reproducing

```bash
# 1. patch SOPBench so per-call verdicts survive (see C1-GROUND-TRUTH.md step 1)
SB=~/Code/SOPBench

# 2. extract each domain in both renderings. v2 prints the node set it selected;
#    `hotel` prints "(none -- v2 is inert on this domain)".
for d in hotel library healthcare dmv university; do
  for r in v1 v2; do
    $SB/.venv/bin/python scripts/eval/sopbench_extract.py \
        --domain $d --render $r --out /tmp/sb/${d}_$r.jsonl
  done
done

# 3. judges. Controls are free and instant; Jev is ~30s and well under a cent per arm;
#    GLM-4.7 is ~1s/decision on the z.ai Coding Plan subscription (no per-token spend).
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sb/hotel_v1.jsonl \
    --baseline --jev jev-latest --out evals/sopbench-hotel-asop/v1
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sb/hotel_v1.jsonl \
    --zai glm-4.7 --out evals/sopbench-hotel-asop/v1

# 3b. the reproducibility control: hotel v1 and v2 are byte-identical in `evidence`,
#     so running both arms is the same input twice. Diff the verdicts.
python3 scripts/eval/sopbench_judge.py --decisions /tmp/sb/hotel_v2.jsonl \
    --baseline --jev jev-latest --zai glm-4.7 --out evals/sopbench-hotel-asop/v2

# 4. paired bootstrap, per domain and judge
python3 scripts/eval/judge_paired_bootstrap.py \
    --a evals/sopbench-library-asop/v1/sopbench_zai_glm-4-7.json \
    --b evals/sopbench-library-asop/v2/sopbench_zai_glm-4-7.json --label "GLM library"
```

The stratified sample is a pure function of the decision set and `--seed 20260923`, so step 2
regenerates the identical sample every arm here was scored on. **Verified rather than assumed:**
the v1 and v2 id sequences match in order for all five domains, and the pipeline reproduces
`bank`'s published Jev calibration (354/46, 0.892 vs 0.742), `bank`'s published selectivity
(20/37 vs 14/161, 6.2×), `bank`'s compounding figure (30.4% at 1.72 gates) and T1's
unchanged-row control (0 flips in 144) to the digit.

### Files here

| path | what it is |
|---|---|
| `evals/sopbench-sweep/JUDGE-SWEEP.md` | this page |
| `evals/sopbench-{hotel,library,healthcare,dmv,university}-asop/v1/` | every v1 arm |
| `evals/sopbench-{...}-asop/v2/` | every v2 arm; on `hotel`/`university` this is the same input re-run, i.e. the reproducibility control |

Extracted decision sets are **not** committed — they regenerate in seconds from step 2.

### Attribution

SOPBench: arXiv:2503.08669 — code MIT, benchmark data and released trajectories **CC BY 4.0**.
The `sopbench_*.json` files are derived measurements over that data.

---

## Status — complete

All five domains, both renderings, all four judge arms: **40 committed arms (5 × 2 × 4), 0
errors, 0 truncations, 0 unparsed verdicts** across every one. Counting the `bank` and
`online_market` arms this page re-analyses, 52 arm files carry a combined error count of **0**.
Nothing is pending and nothing on this page was written before its
numbers existed — the two places where a claim did get ahead of the data are recorded as such
rather than edited away (the `healthcare` v1 row that broke the separation rule, and the
headline that asserted a ranking before three arms had run).

**Not done here — the cross-references.** `ai-tasks/unified/EVIDENCE.md` and `DECISIONS.md` are
gitignored, local-only, and therefore absent from this isolated worktree, so the summary finding
could not be filed against N18–N22. **It needs to be added from the main checkout**, and the
substance is: *the `bank` C1 result does not generalise — judge ranking is domain-dependent
(3–3), a string-match control beats every judge on 3 of 7 domains including `bank` under v2, and
the deployed GLM-4.7 judge carries an 8.5% run-to-run verdict noise floor that retroactively
widens every single-run paired delta published for it. T1's rendering fix survives and
strengthens for the deployed judge (better on 3 of 5 live domains, never significantly worse).*
