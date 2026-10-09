#!/usr/bin/env bash
# Stage 2b: A4f — A4c's exact config with Clef-flash (served on :8792) as the
# value-gate judge. Run after A3/A4/A4c, not beside them, so executor run-to-run
# noise is NOT equalized against those arms; read A4f vs A4c with that in mind.
cd "$(dirname "$0")/../../.."
PY=~/Code/sopbench/.venv/bin/python
C=$PWD/evals/sopbench-clef/stage2
Z="--model glm-4.7 --register-model glm-4.7 --zai --domain hotel --assistant-max-tokens 2048"
V2="--arm asop-gated --host-rules --asop $PWD/evals/sopbench-bank-asop/hotel.v2.asop.md"
echo "commit $(git rev-parse --short HEAD) start $(date)"; start=$(date +%s)
CLEF_URL=${CLEF_URL:-http://127.0.0.1:8792/v1/systemone} \
  $PY scripts/eval/run_sopbench_asop.py $V2 $Z --judge-gate clef --judge-log $C/flash.verdicts.jsonl \
  --output-dir $C/flash --stats-out $C/flash.stats.json > $C/A4f-flash.log 2>&1
echo "A4f exit $? $(($(date +%s)-start))s $(date)"
