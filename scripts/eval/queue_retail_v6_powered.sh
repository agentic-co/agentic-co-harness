#!/bin/bash
# RETAIL v6 vs STOCK — the first arm in this programme sized to reach significance.
#
# WHY THIS IS 40 TASKS AND NOT 18. EVIDENCE.md §4 has said it for a while and every
# arm since has ignored it: retail's discordant rate is ~14%, so 18 tasks × 2 trials
# (36 cells) yields ~5 discordant pairs, and SIX all-one-way is the minimum that can
# clear p<0.05. A 36-cell arm cannot produce a significant result whichever way it
# points. Four numbers from this design have already been withdrawn after being
# quoted as though they meant something. 40 × 4 = 160 cells per arm ⇒ ~22 discordant.
#
# WHAT IS BEING TESTED (N14). EXTRACTION-PROMPT.md:71-75 mandates ordering,
# preconditions, prohibitions and completion — three about constraint, one about
# proving doneness, NONE about accomplishing the task. Zero-write rate tracks how
# ASOP-shaped a document is (prose 8% → v2 15% → +gates 19% → +routing 23%) while
# tool calls and turns stay flat: the agent does the same amount of work, then
# ABANDONS. v6 = v3's firing gates + one Global Rule (G8) telling it to finish.
# Verified before spending anything: diff v3→v6 is 2 lines, +30 words, gate_reach
# 15/15, gate_probe 10/0/0/5 — all identical to v3. One variable moved.
#
# 🛑 SAME REGIME, SAME SPLIT, BOTH ARMS. --defer-consent-stop on both. Never pair
# across regimes — that mistake has been made four times. And an arm on TEST-40 is
# NOT comparable to a historical 36-cell arm, which is why stock is re-run here
# rather than reused. paired.py is the only sanctioned comparison.
#
# ⚠️ A DOSE CAVEAT, recorded before the result exists so it cannot be rationalised
# after. v6 answers ~7 constraint rules, ~25 preconditions and ~30 gates with ONE
# completion rule. A null result is ambiguous between "the stance does not work"
# and "the dose was too small". Say so when reporting, whichever way it lands.
set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

TAU2=~/Code/tau2-bench
OUT="${OUT:?set OUT to the output dir}"
TRIALS="${TRIALS:-4}"
mkdir -p "$OUT"

# z.ai coding plan for the executor; LM Studio (port 4242, see tau2-bench/.env)
# for the user simulator.
export ANTHROPIC_API_BASE=https://api.z.ai/api/anthropic
export ANTHROPIC_API_KEY=$(grep -m1 '^ZAI_API_KEY=' ~/.claude/.env | cut -d= -f2-)
set -a; . "$TAU2/.env"; set +a

# Assert the user sim is loaded rather than assume it: an expired TTL is exactly
# how a 2-hour run died on 2026-09-14. `lms load` on a loaded id is a no-op.
lms load google/gemma-4-31b --identifier google/gemma-4-31b --context-length 32768 --yes >/dev/null 2>&1
lms ps

TEST=$(python3 -c "import json;print(' '.join(json.load(open('evals/tau2-retail-asop/split.test40.json'))['test']))")
echo "=== TEST-40: $(echo $TEST | wc -w) tasks × $TRIALS trials × 2 arms ==="

echo "=== arm B (stock prose) $(date '+%H:%M:%S') ==="
rm -rf "$OUT/stock"
"$TAU2/.venv/bin/python" scripts/eval/run_arm_b.py \
  --tau2 "$TAU2" --domain retail --data evals/tau2-retail-asop \
  --out "$OUT/stock" --tasks $TEST --trials "$TRIALS" --max-steps 40 \
  --defer-consent-stop --agent-llm anthropic/glm-4.7 \
  > "$OUT/stock.log" 2>&1
echo "arm B rc=$?"; grep -aE "arm b\] pass\^1" "$OUT/stock.log"

echo "=== arm C (ASOP v6) $(date '+%H:%M:%S') ==="
rm -rf "$OUT/v6"
"$TAU2/.venv/bin/python" scripts/eval/run_arm_c.py \
  --tau2 "$TAU2" --domain retail --data evals/tau2-retail-asop \
  --out "$OUT/v6" --asop asops/asop.claude.v6.md \
  --tasks $TEST --trials "$TRIALS" --max-steps 40 \
  --defer-consent-stop --agent-llm anthropic/glm-4.7 --verifier-llm anthropic/glm-4.7 \
  > "$OUT/v6.log" 2>&1
echo "arm C rc=$?"; grep -aE "arm c\] pass\^1|gate evaluations" "$OUT/v6.log"

echo "=== paired comparison — the ONLY sanctioned one $(date '+%H:%M:%S') ==="
python3 scripts/eval/paired.py stock "$OUT/stock" v6 "$OUT/v6" | tee "$OUT/paired.txt"

echo "=== verdict classes: deterministic vs judged, NEVER pooled ==="
python3 - "$OUT" <<'PY'
import collections, json, pathlib, sys
out = pathlib.Path(sys.argv[1])
p = out / "v6" / "verdicts.jsonl"
if not p.exists():
    print("  no verdicts.jsonl"); raise SystemExit
c = collections.Counter(
    json.loads(l).get("verifier", "?") for l in p.read_text().splitlines() if l.strip()
)
for k, n in c.most_common():
    print(f"  {k}: {n}")
print("  ^ `deterministic-check` rows are LIVENESS (did the tool run and return ok),")
print("    never correctness. Do not pool them with judged rows.")
PY
echo "=== done $(date '+%H:%M:%S') ==="
