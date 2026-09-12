#!/usr/bin/env bash
# Run the five arms of the τ²-bench airline structure experiment.
#
# τ²-bench reads the agent's policy from a FIXED path at environment
# construction, with no CLI override. So an arm is run by swapping that file and
# running, sequentially. Two consequences the script enforces rather than
# documents:
#
#   * arms cannot run concurrently — they would share the file, and the second
#     would silently score the first one's policy;
#   * the original MUST come back, whatever happens. A crash that leaves an
#     extracted ASOP sitting at the canonical policy path would quietly corrupt
#     every later run, including runs by somebody who does not know this script
#     exists. Hence the trap.
#
# Everything is local: no keys, no rate limits, and trials are bounded by
# patience rather than budget.

set -uo pipefail

TAU2="${TAU2:?set TAU2 to the tau2-bench checkout}"
DATA="${DATA:?set DATA to the dataset dir (evals/tau2-airline-asop)}"
OUT="${OUT:?set OUT to a results dir}"
AGENT="${AGENT:-openai/openai/gpt-oss-20b}"
USER_LLM="${USER_LLM:-openai/google/gemma-4-31b}"
TRIALS="${TRIALS:-2}"
CONC="${CONC:-2}"

POLICY="$TAU2/data/tau2/domains/airline/policy.md"
BACKUP="$OUT/policy.original.md"
mkdir -p "$OUT"

# Take the backup BEFORE anything is swapped, and restore on any exit path —
# success, failure, or interrupt.
[ -f "$BACKUP" ] || cp "$POLICY" "$BACKUP"
restore() { cp "$BACKUP" "$POLICY"; echo "[arms] restored the original policy"; }
trap restore EXIT INT TERM

read -r -d '' ARMS <<'EOF' || true
A|none
B|policy.v0-prose.md
C1|asops/asop.claude.md
C2|asops/asop.codex.md
C3|asops/asop.agy.md
EOF

TASK_IDS=$(python3 -c "import json;print(' '.join(json.load(open('$DATA/split.json'))['test']))")
echo "[arms] agent=$AGENT user=$USER_LLM trials=$TRIALS tasks=$(echo $TASK_IDS | wc -w | tr -d ' ')"

while IFS='|' read -r arm src; do
    [ -z "$arm" ] && continue
    if [ "$arm" = "A" ]; then
        # The floor. An agent with no policy does not know the domain's rules,
        # so ~0 is expected — this is a leakage check, not a fair comparison. A
        # non-zero score here would mean the tasks or tools carry the answers.
        printf '' > "$POLICY"
    else
        cp "$DATA/$src" "$POLICY"
    fi

    echo "[arms] === $arm ($src) — $(wc -w < "$POLICY" | tr -d ' ') words ==="
    ( cd "$TAU2" && set -- $TASK_IDS && uv run tau2 run \
        --domain airline \
        --agent-llm "$AGENT" \
        --user-llm "$USER_LLM" \
        --num-trials "$TRIALS" \
        --max-concurrency "$CONC" \
        --task-ids "$@" ) > "$OUT/arm_$arm.log" 2>&1

    pass1=$(rg -o "Pass\^1\s+[0-9.]+" "$OUT/arm_$arm.log" | tail -1 | rg -o "[0-9.]+$")
    pass2=$(rg -o "Pass\^2\s+[0-9.]+" "$OUT/arm_$arm.log" | tail -1 | rg -o "[0-9.]+$")
    echo "[arms] $arm  pass^1=${pass1:-?}  pass^2=${pass2:-?}"
    echo "$arm,$src,${pass1:-},${pass2:-}" >> "$OUT/summary.csv"
done <<< "$ARMS"

echo "[arms] done — $OUT/summary.csv"
