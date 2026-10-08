#!/usr/bin/env bash
# Stage 2: does a Clef value gate change outcomes? Replicates the N36 confirmatory
# design (QUEUED-EXPERIMENTS.md) at current HEAD: hotel, all 195 tasks, z.ai
# GLM-4.7 executor, --assistant-max-tokens 2048. Three arms run side by side so
# code drift and executor run-to-run noise hit all of them equally:
#   A3  v2 document, tool-ran gate (no judge)
#   A4  v2 + Jev value gate
#   A4c v2 + Clef value gate (the only change vs A4 is the judge)
cd "$(dirname "$0")/../../.."
PY=~/Code/sopbench/.venv/bin/python
C=$PWD/evals/sopbench-clef/stage2
Z="--model glm-4.7 --register-model glm-4.7 --zai --domain hotel --assistant-max-tokens 2048"
V2="--arm asop-gated --host-rules --asop $PWD/evals/sopbench-bank-asop/hotel.v2.asop.md"
echo "commit $(git rev-parse --short HEAD) (+ uncommitted clef gate mode) start $(date)"; start=$(date +%s)
( $PY scripts/eval/run_sopbench_asop.py $V2 $Z --output-dir $C/toolgate --stats-out $C/toolgate.stats.json > $C/A3-toolgate.log 2>&1; echo "A3 exit $? $(($(date +%s)-start))s" ) &
( $PY scripts/eval/run_sopbench_asop.py $V2 $Z --judge-gate jev --judge-log $C/jev.verdicts.jsonl --output-dir $C/jev --stats-out $C/jev.stats.json > $C/A4-jev.log 2>&1; echo "A4 exit $? $(($(date +%s)-start))s" ) &
( $PY scripts/eval/run_sopbench_asop.py $V2 $Z --judge-gate clef --judge-log $C/clef.verdicts.jsonl --output-dir $C/clef --stats-out $C/clef.stats.json > $C/A4c-clef.log 2>&1; echo "A4c exit $? $(($(date +%s)-start))s" ) &
wait; echo "all done $(($(date +%s)-start))s $(date)"
