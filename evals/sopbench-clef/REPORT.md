# Clef vs Jev for ASOP validation — report

*2026-10-08 · Leeloo · goal bead `ac-08e29c36` (parent `ac-b241594d`)*

## Bottom line

Clef (Cloudflare, Qwen3.8-27B + joint schema head, served locally on bigmac) is a
**better offline auditor than Jev, a slightly worse live gate, and identical in outcome**. On the question
*"were this call's prerequisites satisfied?"* across all 14 SOPBench cells it wins 9,
loses 3, ties 2, with a mean lift of 0.81 vs Jev's 0.67 (GLM-4.7 as an LLM-judge
reference: 0.78). It is perfectly repeatable: 0 of 420 verdicts flipped on
byte-identical evidence.

But it does not, by itself, make ASOP enforcement better. On the live hotel A/B (195 tasks, GLM-4.7
executor) the Clef-gated arm and the Jev-gated arm finished with **exactly the same task
success, 0.718, with 11 tasks going each way** — and neither beats the tool-gate-only
arm (0.723). A value gate of either make trades legitimate work for refusals at an
even rate: should-refuse tasks 0.881 → 0.937, should-succeed tasks 0.435 → 0.319. This
reproduces September's null (A4 vs A3, p=0.84) with a second, independent judge.

The three stages agree on one thing more strongly than on Clef-vs-Jev: **the judge is
only as good as the evidence handed to it.** Starve either judge of the tool history and
both collapse; real AgentCo beads carry almost no evidence today. That, not the choice
of judge, is the binding constraint on automated `judged` gates.

**Recommendation:** adopt Clef as the default *offline* judge (audits, replays,
shadow verifier) now; keep it in **shadow mode** on real beads — never auto-approve —
until beads record executor evidence; do not swap it into the live SOPBench gate on the
strength of this data. Details and caveats below.

## What was run

| Stage | Question | Bead | Status |
|---|---|---|---|
| 0. Audit sweep | Clef vs Jev on the 14 SOPBench audit cells (7 domains × renders v1/v2) | (parent) | done |
| 1. Gate replay | Re-judge September's 958 logged live-gate conditions with Clef and Jev | `ac-a7a1f86f` | done |
| 2. Live gate A/B | Hotel, 195 tasks, GLM-4.7 executor: A3 tool gate / A4 + Jev / A4c + Clef | `ac-d38a9204` | done |
| 3. Shadow verifier | Clef on real AgentCo `awaiting_verify` beads, read-only | `ac-f871a1ae` | done |

Serving: BF16 on MPS (M3 Ultra, 96 GB), 136 s load, ~3–5 s/call (flash-linear-attention
kernels unavailable on MPS → torch fallback; Cloudflare's H200 figure is 209 ms).
Server: `serve/clef_server.py`, Jev/SystemOne wire-compatible.

## Stage 0 — audit sweep

Lift = TPR − FPR, positive class = *not_held* (should refuse). Paired bootstrap on identical
decision ids, seed 20260923; `*` = 95 % CI excludes 0.

| Domain | v1 Jev → Clef (GLM) | Δ | v2 Jev → Clef (GLM) | Δ |
|---|---|---|---|---|
| bank | 0.770 → 0.885 (0.675) | +0.115* | 0.725 → 0.905 (0.760) | +0.180* |
| online_market | 0.615 → 0.820 (0.840) | +0.205* | 0.540 → 0.790 (0.835) | +0.250* |
| hotel | 0.845 → 0.770 (0.730) | −0.075* | 0.845 → 0.770 (0.740) | −0.075* |
| library | 0.652 → 0.578 (0.570) | −0.074 | 0.763 → 0.630 (0.778) | −0.133* |
| healthcare | 0.374 → 0.835 (0.765) | +0.461* | 0.235 → 0.765 (0.730) | +0.530* |
| dmv | 0.630 → 0.849 (0.767) | +0.219* | 0.616 → 0.781 (0.932) | +0.164* |
| university (n=20) | 1.0 → 1.0 (1.0) | tie | 1.0 → 1.0 (0.9) | tie |

Shape: Clef catches more violations (higher TPR) at the price of more false refusals;
its losses are on hotel and library, which are FPR losses. Max input 2,744 tokens — nothing
truncated. Hotel v2 had 32 timeouts from memory contention (LM Studio auto-loaded a 29 GB
model); those rows were re-judged in place after LM Studio was stopped — 0 errors remain.

## Stage 1 — replaying September's live gate log

| Judge | Evidence | Lift | TPR | FPR |
|---|---|---|---|---|
| Jev, live (Sept, A4) | full tool history | +0.880 | 0.983 | 0.103 |
| Jev, replay | condition + 200-char result snippet | +0.008 | 1.000 | 0.992 |
| Clef, replay | same | −0.269 | 0.453 | 0.723 |

The archived log kept only a 200-character snippet of each tool result. On that, Jev
refuses almost everything and Clef is worse than chance (it let 94 real violations
through). **This stage cannot rank the judges** — it measures evidence starvation, and
both fail it. Stage 2 is the fair comparison.

## Stage 2 — live gate, hotel, 195 tasks

Same config as the N36 confirmatory run (v2 ASOP document, `--host-rules`, z.ai GLM-4.7,
`--assistant-max-tokens 2048`), three arms run concurrently against the same Clef server.

**Task outcomes** (SOPBench's own evaluator, all 195 tasks present in every arm):

| Arm | Success | Should-succeed (n=69) | Should-refuse (n=126) |
|---|---|---|---|
| none — no ASOP (Sept, context) | 0.621 | | |
| A3 — v2 doc + tool gate | 0.723 | 0.435 | 0.881 |
| A4 — A3 + Jev value gate | 0.718 | 0.319 | 0.937 |
| A4c — A3 + Clef value gate | 0.718 | 0.319 | 0.937 |

| Pair | Δ success [95 % CI] | gained / lost | McNemar p |
|---|---|---|---|
| A4c Clef vs A3 | −0.005 [−0.059, +0.049] | 14 / 15 | 1.00 |
| A4c Clef vs A4 Jev | +0.000 [−0.047, +0.047] | 11 / 11 | 1.00 |
| A4 Jev vs A3 | −0.005 [−0.063, +0.053] | 16 / 17 | 1.00 |
| A3 / A4 / A4c vs none | +0.103 / +0.097 / +0.097 | 30/10 · 23/4 · 22/3 | 0.002 / 0.0003 / 0.0002 |

The ASOP itself is what moves hotel (+10 pp over no ASOP, all three arms significant).
The value gate on top adds nothing net — it moves ~30 tasks, half each way. Note the gate
arms lose far fewer tasks vs `none` (3–4) than A3 does (10): the gate makes the policy
*safer* at the same success rate, which matters if refusals are cheap and violations are
not.

**Gate accuracy** (every judged condition graded against SOPBench truth on the initial
state):

| Judge | Conditions | Blocks (correct / wrong) | Violations let through | TPR | FPR | Lift |
|---|---|---|---|---|---|---|
| Jev (live, this run) | 981 | 247 (180 / 67) | 5 | 0.973 | 0.084 | **0.889** |
| Clef (live) | 955 | 248 (161 / 87) | 9 | 0.947 | 0.111 | 0.836 |
| Jev (live, Sept, reference) | | | | 0.983 | 0.103 | 0.880 |

Jev reproduces September's live lift (0.889 vs 0.880). Clef is consistently a bit worse
on hotel — the same direction as the hotel audit cell (−0.075) — with its extra wrong
blocks on `book_room` (24 vs 17) and `modify_reservation` (62 vs 50).

**Run health:** 0 judge errors in either log. Clef p50 3.5 s / p95 5.0 s per call (local
MPS); Jev p50 0.3 s but p95 60 s (remote API tail). A4c's first launch failed its 20 s
judge preflight (model warm-up) and was relaunched — no tasks ran in the failed launch.
A3 had 2 tasks crash on GLM-4.7 emitting truncated tool-call JSON at the 2048-token cap
(retried by the runner; all 195 scored). No 429s. Wall time ~6 h (A3) to ~7.2 h (A4).

## Stage 3 — shadow verifier on real beads

Built `agentco_harness/judge_verifier.py` + `scripts/judge/shadow_verify.py` (17 tests).
Policy: auto-approve only on `held` with confidence ≥ 0.9; **no auto-reject path** — a
`not_held` or low-confidence verdict parks for a human, exactly as today. The scan is
read-only and never writes a bead.

- **History:** only 2 beads in the whole portfolio carry a human verify label (both
  approved). Clef said `not_held` on both (0.90, 0.99) → both would have parked. No unsafe
  approval, but 0/2 agreement. One of the two has `result: null` — there was nothing to
  judge.
- **Live scan:** 1 bead currently `awaiting_verify` (`ac-1c40fce4`, human gate): `not_held`
  0.993 → would park.
- **Field notes:** no bead anywhere records a `verify_rejection`; schema drift
  (`verify.class` vs `verify.kind`); ungated beads also park as class `unverified`;
  `~/Portfolio/tasks.jsonl` has 27 duplicate ids. `output_tail` is deliberately excluded from
  evidence (it leaks the verdict).

With n=2 there is no accuracy estimate here — the point of the stage is that the
plumbing works and the evidence does not exist yet.

## Recommendations

1. **Make beads carry evidence before automating any judged gate.** Executors should
   write structured evidence (commands run, files touched, test output, artifact links)
   into the bead at completion. Stages 1 and 3 both show a judge on thin evidence is
   worse than useless — and it is the same fix for Jev, Clef, or a human reviewer.
2. **Run Clef in shadow mode on real gates** (`shadow_verify.py scan` on a schedule) and
   collect human labels alongside. Revisit auto-approve only after ~50 labelled gates with
   0 dangerous approvals.
3. **Use Clef as the default offline judge** for SOPBench audits and replays: better lift
   on 9/14 cells, deterministic, local, no per-call cost.
4. **Do not swap the live SOPBench gate to Clef on this data.** It is slightly less accurate than Jev on hotel and
changes nothing in outcomes. More importantly, **neither** value gate helps outcomes
here: the gate's cost on legitimate tasks is the thing to fix (a calibrated threshold,
or a park-for-human instead of a hard block on low-confidence `not_held`), not the
judge. That is a testable next experiment and costs nothing extra to run on Clef locally.
5. **Serving:** Clef-flash (smaller, reported faster and comparable on decision tasks) is
   the candidate for co-tenancy with LM Studio; the 27B needs bigmac to itself (two
   instances do not fit in 96 GB). Speed on MPS is ~15–25× slower than Cloudflare's H200
   figure; fine for gates and audits, not for per-token hot paths.

## Artifacts

All under `~/Code/agentco-harness/evals/sopbench-clef/` unless noted:
`serve/clef_server.py`, `run_clef_sweep.sh`, `compare_clef_jev.py`, `compare-all.json`,
`<domain>/<render>/sopbench_jev_clef.json`, `live-gate/` (stage 1), `stage2/` (run.sh,
analyze.py, logs, verdicts, graded), `stage3/`; code: `scripts/eval/sopbench_judge_gate.py`
(`--judge-gate clef`), `scripts/eval/decider_replay_live_gate.py` (`--model-name`,
`--bearer-env`, fail-fast), `evals/sopbench-bank-asop/score_asop_arms.py`
(`asop-v2-clef`), `agentco_harness/judge_verifier.py`, `scripts/judge/shadow_verify.py`,
`tests/test_judge_verifier.py`.
