#!/usr/bin/env bash
# Clef vs Jev on SOPBench: run the locally-served Clef through the identical
# Jev judge path (same payload, same sampler, same seed) for every domain x
# rendering cell the Jev sweep scored. Assumes clef_server.py is up on :8791
# and the decision sets were extracted to $SB_DECISIONS (see README.md).
set -euo pipefail
cd "$(dirname "$0")/../.."
SB_DECISIONS=${SB_DECISIONS:?set SB_DECISIONS to the dir holding <domain>_<render>.jsonl}
URL=${CLEF_URL:-http://127.0.0.1:8791/v1/systemone}

# v1 across all domains first: a complete 7-domain comparison lands halfway.
for r in v1 v2; do
  for d in bank online_market hotel library healthcare dmv university; do
    out=evals/sopbench-clef/$d/$r
    [ -f "$out/sopbench_jev_clef.json" ] && { echo "skip $d $r"; continue; }
    mkdir -p "$out"
    echo "== $d $r"
    python3 scripts/eval/sopbench_judge.py --decisions "$SB_DECISIONS/${d}_$r.jsonl" \
        --jev clef --jev-url "$URL" --jev-workers 2 --out "$out"
  done
done
