#!/usr/bin/env bash
# F9 probe — persona/voice/denylist confirmed frontend-only (lifeos-exit/ISA.md ISC-9).
#
# Reproduces the two greps the ISA specifies for ISC-9, with one correction and one addition:
#   - Probe 2's `persona` term is word-boundaried (\bpersona\b) so it does not match `personal`
#     or `impersonated` — those were false positives inflating the hit count, not ISC-9 matches.
#   - Every hit is checked against KNOWN_BENIGN, an explicit allowlist of the exact substrings
#     documented in docs/lifeos-exit/f9-persona-voice.md as coincidental word reuse (a bead-title
#     convention, Pulse's opaque "voiced" webhook wording, this repo's own executor-backend
#     nickname convention, and env-var allowlist/denylist framing). A hit matching the allowlist
#     is reported but does not fail the probe. A hit matching NOTHING in the allowlist is a NEW,
#     unclassified persona/voice/denylist reference — exactly what ISC-9 exists to catch — and
#     fails the probe.
#
# Exit code: 0 only if every hit (if any) is in KNOWN_BENIGN. 1 if any hit is new/unclassified,
# or if a probe command itself errors. This makes the script wireable into CI/a pre-merge check,
# unlike a lint that reports a violation and exits success.
#
# Usage: bash docs/lifeos-exit/probe-f9-persona-voice.sh   (run from anywhere in the repo)

set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1

# Exact substrings from the per-hit analysis in f9-persona-voice.md. Add a new entry ONLY after
# writing the corresponding analysis into that doc — this list is a record of reviewed decisions,
# not a way to silence the probe.
KNOWN_BENIGN=(
  "Leeloo owes"                              # orchestrator.py — bead-title convention, code branches on task_class only
  "voiced"                                   # config.py / notify.py — Pulse endpoint described as opaque/voiced downstream
  "voice + whatever Pulse routes onward"     # notify.py — same Pulse-endpoint description, no "voiced" substring
  "denylist this replaced"                   # executor.py — subprocess env-var allowlist/denylist framing
  "not a denylist"                           # executor.py — same
  "denylist over the whole environment"      # executor.py — same
  "is a denylist"                            # executor.py, beads.py — same
  "Bellows persona"                          # executor.py — this repo's nickname for the Google/agy executor backend
  "Forge persona"                            # executor.py, orchestrator.py — this repo's nickname for the OpenAI/codex executor backend
)

is_known_benign() {
  local line="$1"
  local pat
  for pat in "${KNOWN_BENIGN[@]}"; do
    if [[ "$line" == *"$pat"* ]]; then
      return 0
    fi
  done
  return 1
}

# Runs one probe, classifies every hit, prints KNOWN/NEW per line. Returns 1 iff any hit is NEW.
run_probe() {
  local label="$1" pattern="$2" new_found=0 total=0 line
  echo "=== $label ==="
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    total=$((total + 1))
    if is_known_benign "$line"; then
      echo "KNOWN  $line"
    else
      echo "NEW    $line"
      new_found=1
    fi
  done < <(rg -n "$pattern" agentco_harness/ 2>/dev/null)
  echo "$total hit(s)."
  echo
  return "$new_found"
}

overall=0

run_probe "Probe 1: leeloo|elevenlabs|voice (case-insensitive)" "(?i)leeloo|elevenlabs|voice" || overall=1
run_probe "Probe 2: \\bpersona\\b|voiceId|denylist (word-boundaried persona)" "\bpersona\b|voiceId|denylist" || overall=1

if [ "$overall" -eq 0 ]; then
  echo "PASS — every hit matches a documented, reviewed, benign reason."
else
  echo "FAIL — at least one hit is NOT in the reviewed allowlist. Read it, decide whether it's"
  echo "a real ISC-9 violation or a new benign case, then update f9-persona-voice.md + this"
  echo "script's KNOWN_BENIGN together — never just the script."
fi
exit "$overall"
