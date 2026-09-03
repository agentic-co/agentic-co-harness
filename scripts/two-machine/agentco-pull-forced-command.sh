#!/usr/bin/env bash
# agentco-pull-forced-command.sh — the hub-side gate on the pull lane.
#
# Runbook: Plans/TwoMachineSetupRunbook.md B3.  Design: Plans/TwoMachineLifeos.md
# ("Dispatch: SSH pull + leases").  Bead: ac-b8700758.
#
# WHAT THIS IS
# The worker machine holds an SSH key authorized on the hub host.  That key must be
# able to do exactly two things — ask the hub for its lane's work, and hand back
# the outcome — and nothing else.  A bare `authorized_keys` entry grants a full
# login shell on the always-on machine that holds every venture's bead store, so
# the key is pinned to this wrapper as a FORCED COMMAND: sshd ignores whatever
# the client asked to run, runs this instead, and hands us the client's request
# in $SSH_ORIGINAL_COMMAND as an untrusted string.
#
# THE AUTHORIZED_KEYS LINE (hub host ~/.ssh/authorized_keys, one line, no wrap):
#
#   restrict,command="/path/to/agentco-harness/scripts/two-machine/agentco-pull-forced-command.sh" ssh-ed25519 AAAAC3Nza...THE_WORKER_PUBLIC_KEY... agentco-pull-worker
#
# `restrict` is the important half and is not optional.  It implies
# no-agent-forwarding, no-port-forwarding, no-pty, no-X11-forwarding and
# no-user-rc, and — the one that protects this script — it disables
# PermitUserEnvironment for the key, so a client CANNOT set the environment
# variables this wrapper reads for its own configuration.  Older sshd without
# `restrict` must spell the five options out individually.
#
# THE PARSING RULE
# $SSH_ORIGINAL_COMMAND is attacker-controlled input and is NEVER interpreted by
# a shell here — no eval, no unquoted expansion, no `sh -c`.  It is:
#   1. rejected outright unless every character is in a small whitelist,
#   2. split on whitespace with globbing disabled,
#   3. validated token by token against a per-subcommand allowlist of flags and
#      value shapes,
#   4. executed via an argv array built from the validated tokens.
# Anything not explicitly allowed is denied.  There is no passthrough case.
#
# DELIBERATE DENIALS (each is a decision, not an oversight):
#   --config / -c   the hub pins its own store; a remote key must not be able to
#                   point `pull` at another company's beads.
#   --force         `pull --force` is the documented break-glass override of the
#                   reconcile-before-replay guard (Plans/BreakGlassFailover.md).
#                   A guard a remote worker can switch off is advisory, which is
#                   exactly the failure mode that design rejected.  Break-glass
#                   is a human act, performed on the hub host.
#   --flag=value    only the space-separated form is accepted.  Supporting both
#                   doubles the parser surface for no gain; the shipped worker
#                   (frontsteps-worker.sh) uses the space form.
#   absolute paths  argv[0] must be the bare word `agentco`.
#
# EXIT CODES
#   0    request allowed; exit status is then agentco's own (report uses 0/1/2).
#   77   denied by the allowlist (EX_NOPERM).  The client asked for something
#        this key may not do.
#   78   configuration failure (EX_CONFIG): the audit log is unwritable, or the
#        agentco binary / hub config is missing.  FAIL CLOSED — an unauditable
#        remote execution path is worse than a stalled lane.
#
# TESTING HOOKS (safe only because `restrict` blocks client-set environment):
#   AGENTCO_PULL_AUDIT_LOG   override the audit log path
#   AGENTCO_HUB_CONFIG       override the pinned hub config
#   AGENTCO_BIN              override the agentco binary
#   AGENTCO_PULL_DRY_RUN=1   print the argv that WOULD run, one per line, and
#                            exit 0 without executing it
set -uo pipefail

# --- pinned configuration ----------------------------------------------------
# Pin the ONE node this key may pull from. A host running several nodes must
# pin the right one here — the gate never lets the caller choose (the original
# deployment mis-pinned a sibling instance at staging; a probe caught it).
# A relative tasks_path resolves against the CONFIG FILE's directory
# (config.py:475-477), but the children registry resolves against CWD (defect,
# beaded) — the cd below is load-bearing until that is fixed.
HUB_CONFIG="${AGENTCO_HUB_CONFIG:-$HOME/.agentco/config.yaml}"
AGENTCO_BIN="${AGENTCO_BIN:-$HOME/.local/bin/harness}"
AUDIT_LOG="${AGENTCO_PULL_AUDIT_LOG:-$(dirname "$0")/pull-audit.log}"

# Value shapes.  Kept as anchored patterns so a flag can never smuggle a second
# flag through as its own value.
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'
INT_RE='^[0-9]+$'
NUM_RE='^[0-9]+(\.[0-9]+)?$'
KEY_RE='^[A-Za-z0-9][A-Za-z0-9._:@/-]*$'
MAX_RESULT_LEN=500

RAW="${SSH_ORIGINAL_COMMAND-}"
CLIENT="${SSH_CONNECTION%% *}"
[ -n "$CLIENT" ] || CLIENT="-"

# --- audit -------------------------------------------------------------------
# Every invocation is logged, allowed or not: a denial is the more interesting
# record, and a lane that executes without leaving one is not auditable at all.
audit() {
    local decision="$1" reason="$2" line flat
    # One record per line, always: a request containing a newline would otherwise
    # forge a second audit record. Newlines are denied a few lines below, but the
    # denial is itself logged — so the flattening has to happen HERE, before the
    # gate, not be assumed from it. Truncated so one absurd request cannot
    # dominate the log.
    flat="$(printf '%.400s' "$RAW" | LC_ALL=C tr '\n\r\t' '   ')"
    line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') ${decision} reason=${reason} from=${CLIENT} cmd=${flat}"
    mkdir -p "$(dirname "$AUDIT_LOG")" 2>/dev/null
    if ! printf '%s\n' "$line" >> "$AUDIT_LOG" 2>/dev/null; then
        echo "agentco-pull: FATAL cannot write audit log $AUDIT_LOG" >&2
        exit 78
    fi
}

deny() {
    audit DENY "$1"
    echo "agentco-pull: denied ($1)" >&2
    exit 77
}

# --- 1. charset gate ---------------------------------------------------------
# A whitelist, not a denylist: shell metacharacters, newlines, backslashes and
# every control character fall outside it and are rejected before any parsing
# happens.  Nothing downstream evaluates this string, so this is belt-and-braces
# — but it also collapses the whole class of "did the parser handle $(...)"
# questions into one assertion that is trivial to test.
[ -n "$RAW" ] || deny "empty command (interactive shell attempt)"

# Newlines get their own check, ahead of the general one, for two independent
# reasons — and the second is why the general check alone was not enough:
#   1. `read -r -a` below consumes ONE line, so a second line would be dropped
#      silently rather than rejected — a request that reads as legal while
#      carrying a payload the wrapper never examined.
#   2. `$(...)` strips trailing newlines, so a request ending in one leaves the
#      general check's output empty and passes it. (Caught by test, not review.)
case "$RAW" in
    *$'\n'* | *$'\r'*) deny "newline in request" ;;
esac

illegal="$(printf '%s' "$RAW" | LC_ALL=C tr -d "A-Za-z0-9 ._:/=+@,'-")"
[ -z "$illegal" ] || deny "illegal characters in request"

# --- 2. split (no globbing, no expansion) ------------------------------------
set -f
IFS=' ' read -r -a ARGV <<< "$RAW"
set +f
[ "${#ARGV[@]}" -ge 2 ] || deny "too few arguments"

[ "${ARGV[0]}" = "agentco" ] || deny "argv[0] is not agentco"
SUB="${ARGV[1]}"
case "$SUB" in
    pull | report) ;;
    *) deny "subcommand not allowed: ${SUB}" ;;
esac

# --- 3. per-subcommand allowlist ---------------------------------------------
# VALID collects only tokens that passed validation.  The command that finally
# runs is assembled from THIS array, never from $RAW, so a token that was not
# examined cannot reach agentco.
VALID=()
i=2
n="${#ARGV[@]}"

# Pull the value that follows a flag, refusing a missing one or a value that is
# itself a flag (`--agent --node frontsteps` must not silently mean agent=--node).
need_value() {
    local flag="$1"
    i=$((i + 1))
    [ "$i" -lt "$n" ] || deny "${flag} requires a value"
    VAL="${ARGV[$i]}"
    case "$VAL" in -*) deny "${flag} value looks like a flag: ${VAL}" ;; esac
}

# --result is the one free-text value in the protocol.  Accept a single-quoted
# run of tokens ('no executor wired yet') and reassemble it HERE rather than
# letting any shell do it; the quotes are stripped and the text becomes one argv
# element.  Charset is already constrained by the gate above.
collect_result() {
    i=$((i + 1))
    [ "$i" -lt "$n" ] || deny "--result requires a value"
    local tok="${ARGV[$i]}"
    if [ "${tok#\'}" != "$tok" ]; then
        # Quoted. Strip the opening quote, then either it closes on this same
        # token ('done') or we absorb tokens until one ends with the quote.
        VAL="${tok#\'}"
        if [ "${#tok}" -ge 2 ] && [ "${VAL%\'}" != "$VAL" ]; then
            VAL="${VAL%\'}"
        else
            while :; do
                i=$((i + 1))
                [ "$i" -lt "$n" ] || deny "--result has an unterminated quote"
                tok="${ARGV[$i]}"
                if [ "${tok%\'}" != "$tok" ]; then
                    VAL="${VAL} ${tok%\'}"
                    break
                fi
                VAL="${VAL} ${tok}"
            done
        fi
    else
        case "$tok" in
            -*) deny "--result value looks like a flag: ${tok}" ;;
        esac
        VAL="$tok"
    fi
    # No quote may survive reassembly — a stray one means the client's quoting
    # and ours disagree, and a value we cannot parse confidently is not one we
    # should forward.
    case "$VAL" in *"'"*) deny "--result value has a stray quote" ;; esac
    [ "${#VAL}" -le "$MAX_RESULT_LEN" ] || deny "--result value too long"
    [ -n "$VAL" ] || deny "--result value is empty"
}

if [ "$SUB" = "pull" ]; then
    while [ "$i" -lt "$n" ]; do
        tok="${ARGV[$i]}"
        case "$tok" in
            --agent | -a | --node)
                need_value "$tok"
                [[ "$VAL" =~ $NAME_RE ]] || deny "bad value for ${tok}: ${VAL}"
                VALID+=("$tok" "$VAL")
                ;;
            --max | --ttl)
                need_value "$tok"
                [[ "$VAL" =~ $INT_RE ]] || deny "bad value for ${tok}: ${VAL}"
                VALID+=("$tok" "$VAL")
                ;;
            --reconcile-after)
                need_value "$tok"
                [[ "$VAL" =~ $NUM_RE ]] || deny "bad value for ${tok}: ${VAL}"
                VALID+=("$tok" "$VAL")
                ;;
            --reconcile)
                VALID+=("$tok")
                ;;
            *)
                deny "flag not allowed on pull: ${tok}"
                ;;
        esac
        i=$((i + 1))
    done
    # --agent is required by the CLI; catching it here makes the denial legible
    # in the audit log instead of surfacing as a click usage error.
    case " ${VALID[*]-} " in
        *" --agent "* | *" -a "*) ;;
        *) deny "pull requires --agent" ;;
    esac
else # report
    # The task id is the only positional, and it comes first.
    TASK_ID="${ARGV[$i]}"
    [[ "$TASK_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || deny "bad task id: ${TASK_ID}"
    VALID+=("$TASK_ID")
    i=$((i + 1))
    while [ "$i" -lt "$n" ]; do
        tok="${ARGV[$i]}"
        case "$tok" in
            --attempt)
                need_value "$tok"
                [[ "$VAL" =~ $INT_RE ]] || deny "bad value for --attempt: ${VAL}"
                VALID+=("$tok" "$VAL")
                ;;
            --idempotency-key)
                need_value "$tok"
                [[ "$VAL" =~ $KEY_RE ]] || deny "bad value for --idempotency-key: ${VAL}"
                VALID+=("$tok" "$VAL")
                ;;
            --result)
                collect_result
                VALID+=("$tok" "$VAL")
                ;;
            --done | --failed)
                VALID+=("$tok")
                ;;
            *)
                deny "flag not allowed on report: ${tok}"
                ;;
        esac
        i=$((i + 1))
    done
    case " ${VALID[*]-} " in
        *" --attempt "*) ;;
        *) deny "report requires --attempt (the lease fence)" ;;
    esac
fi

# --- 4. execute --------------------------------------------------------------
audit ALLOW "ok"

if [ "${AGENTCO_PULL_DRY_RUN:-}" = "1" ]; then
    printf '%s\n' "$AGENTCO_BIN" --config "$HUB_CONFIG" "$SUB" ${VALID[@]+"${VALID[@]}"}
    exit 0
fi

[ -x "$AGENTCO_BIN" ] || { echo "agentco-pull: FATAL no agentco at $AGENTCO_BIN" >&2; exit 78; }
[ -f "$HUB_CONFIG" ] || { echo "agentco-pull: FATAL no hub config at $HUB_CONFIG" >&2; exit 78; }

cd "$(dirname "$HUB_CONFIG")" || { echo "agentco-pull: FATAL cannot cd to hub" >&2; exit 78; }
exec "$AGENTCO_BIN" --config "$HUB_CONFIG" "$SUB" ${VALID[@]+"${VALID[@]}"}
