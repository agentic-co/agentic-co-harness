"""Stage 1 orchestrator: the elitist reviser loop (EXP-L.md v3, "Stage 1").

Every round's candidate — accepted or rejected — is run on the FULL 195-task
domain, not a partial subset: EDIT (fixed/broken/unchanged), SELECT
(accept/reject) and TEST (the reported curve) all come out of that one run,
"free from the same runs" (RL review #3). This also matches the pre-
registered cost estimate exactly ("~2.5 h per 195-task round" — there is no
other run shape that estimate describes). `--task-ids` is used only for the
narrower jobs this file also drives: the round-0 bootstrap (already a full
run) and the end-of-stage variance reruns (TEST only, cheaper by design).

State lives in `evals/expl/rounds/state.json` (current B: its document, its
full scored record cache, and a rounds[] history) so the loop can be resumed
across sessions without re-scoring or recalling the reviser for work already
done. This file is also the source of every number `report.py` prints.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_THIS_DIR = Path(__file__).resolve().parents[1]
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from expl import diffing, leak_check, reviser as reviser_mod, runner, scoring, split as split_mod  # noqa: E402
from expl.domain_tables import build_domain_tables, self_check_candidate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
EVALS_DIR = REPO_ROOT / "evals" / "expl"
ROUNDS_DIR = EVALS_DIR / "rounds"
DOCS_DIR = EVALS_DIR / "documents"
STATE_PATH = ROUNDS_DIR / "state.json"
SPLIT_PATH = EVALS_DIR / "split.json"
V0_DOC_PATH = REPO_ROOT / "evals" / "sopbench-bank-asop" / "hotel.v2b.asop.md"
MAX_ROUNDS = 3
DOMAIN = "hotel"


# ── state ────────────────────────────────────────────────────────────────────


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"b_doc": None, "b_records_file": None, "rounds": [], "blind": None, "bootstrap": None}


def _save_state(state: dict) -> None:
    ROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str) + "\n")


def _save_records(records: dict, path: Path) -> None:
    """`scoring.score_file` keys by int position; JSON needs string keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(k): v for k, v in records.items()}, default=str))


def _load_records(path: Path) -> dict:
    raw = json.loads(Path(path).read_text())
    return {int(k): v for k, v in raw.items()}


def _split() -> dict[str, list[int]]:
    return split_mod.load_split_file(SPLIT_PATH)


# ── round mechanics ──────────────────────────────────────────────────────────


def top_failure_types(edit_records: dict, max_types: int = 3) -> list[str]:
    groups = scoring.failure_groups(edit_records)
    ordered_labels = [label for label, _ in scoring.CONJUNCT_PRIORITY] + ["other"]
    ranked = sorted(
        groups.keys(),
        key=lambda label: (-len(groups[label]), ordered_labels.index(label)),
    )
    return ranked[:max_types]


def attempt_candidate(
    rev: reviser_mod.Reviser,
    tables,
    parent_doc: str,
    edit_records: dict,
    round_label: str,
    blind: bool = False,
    max_attempts: int = 2,
) -> dict:
    """Preflight -> propose -> apply -> leak-check -> self-check, retried once
    as a WHOLE pipeline (any stage's failure is "the call failed" for the
    purpose of the pre-registered contract) — never a silent substitution.
    Returns a dict recording exactly what happened, `ok` True iff a candidate
    document survived every check.
    """
    groups = scoring.failure_groups(edit_records)
    chosen_types = [] if blind else top_failure_types(edit_records)
    grouped_for_reviser = {t: groups[t] for t in chosen_types}
    flat_failed_records = [rec for recs in groups.values() for _, rec in recs]

    attempts_log = []
    for attempt in range(max_attempts):
        label = f"{round_label}_attempt{attempt}"
        ok, preflight_err = rev.preflight()
        if not ok:
            attempts_log.append({"attempt": attempt, "stage": "preflight", "error": preflight_err})
            continue
        outcome = rev.propose(parent_doc, grouped_for_reviser, label, blind=blind)
        if not outcome.ok:
            attempts_log.append(
                {"attempt": attempt, "stage": "propose", "error": outcome.error,
                 "input_hash": outcome.input_hash, "output_hash": outcome.output_hash}
            )
            continue
        result = diffing.apply_diffs(parent_doc, outcome.diffs)
        if result.errors:
            attempts_log.append(
                {"attempt": attempt, "stage": "apply", "error": "; ".join(result.errors),
                 "input_hash": outcome.input_hash, "output_hash": outcome.output_hash}
            )
            continue
        added_text = diffing.diff_added_text(parent_doc, result.document)
        leak_hits = leak_check.leak_check(added_text, flat_failed_records)
        if leak_hits:
            attempts_log.append(
                {"attempt": attempt, "stage": "leak_check", "error": "; ".join(leak_hits),
                 "input_hash": outcome.input_hash, "output_hash": outcome.output_hash}
            )
            continue
        problems = self_check_candidate(result.document, tables)
        if problems:
            attempts_log.append(
                {"attempt": attempt, "stage": "self_check", "error": "; ".join(problems),
                 "input_hash": outcome.input_hash, "output_hash": outcome.output_hash}
            )
            continue
        return {
            "ok": True,
            "document": result.document,
            "applied_diffs": list(result.applied),
            "diffs": [asdict(d) for d in outcome.diffs],
            "chosen_failure_types": chosen_types,
            "n_edit_failures": sum(len(v) for v in groups.values()),
            "thin_signal": sum(len(v) for v in groups.values()) < 10,
            "input_hash": outcome.input_hash,
            "output_hash": outcome.output_hash,
            "usage": outcome.usage,
            "attempts": attempts_log,
        }

    return {
        "ok": False,
        "document": None,
        "chosen_failure_types": chosen_types,
        "n_edit_failures": sum(len(v) for v in groups.values()),
        "thin_signal": sum(len(v) for v in groups.values()) < 10,
        "attempts": attempts_log,
        "error": "no candidate after retry",
    }


def per_task_transition(b_records: dict, c_records: dict, positions: list[int]) -> dict:
    fixed = broken = unchanged_pass = unchanged_fail = 0
    for pos in positions:
        if pos not in b_records or pos not in c_records:
            continue
        b_ok, c_ok = b_records[pos]["success"], c_records[pos]["success"]
        if not b_ok and c_ok:
            fixed += 1
        elif b_ok and not c_ok:
            broken += 1
        elif b_ok and c_ok:
            unchanged_pass += 1
        else:
            unchanged_fail += 1
    return {"fixed": fixed, "broken": broken, "unchanged_pass": unchanged_pass, "unchanged_fail": unchanged_fail}


def select_success(records: dict, positions: list[int]) -> dict[int, bool]:
    return {pos: records[pos]["success"] for pos in positions if pos in records}


# Substrings that mean a run's transcripts cannot be trusted, however many
# tasks finished — found live in round 1 ("No models loaded" after another
# process unloaded the shared instance mid-run) and in the pre-round-0
# routing bug ("No user query found in messages"). A run log containing any
# of these is grounds for FAULTED regardless of how many tasks got scored.
FAULT_LOG_PATTERNS = (
    "No models loaded",
    "No user query found in messages",
    "call failed",
)


def check_run_health(records: dict, splits: dict[str, list[int]], run_log: Path) -> tuple[bool, list[str]]:
    """Fail loud (RL/ML review + the round-1 incident): a round must never
    compare or decide on a subset. Every split's scored count must equal its
    declared size, and the run log must carry no request-error pattern —
    either one, on its own, makes a round FAULTED and nothing is accepted.
    """
    problems: list[str] = []
    for name in ("edit", "select", "test"):
        expected = len(splits[name])
        present = sum(1 for p in splits[name] if p in records)
        if present < expected:
            problems.append(f"{name}: only {present}/{expected} tasks scored (must equal the declared split size)")
    if run_log.exists():
        text = run_log.read_text(errors="replace")
        for pat in FAULT_LOG_PATTERNS:
            count = text.count(pat)
            if count:
                problems.append(f"run log contains {count}x {pat!r}")
    else:
        problems.append(f"run log missing: {run_log}")
    return (len(problems) == 0), problems


def decide_round(
    round_idx: int,
    round_dir: Path,
    candidate_doc_path: Path,
    b_records: dict,
    splits: dict[str, list[int]],
    output_subdir: str = "full",
) -> dict:
    """Run a candidate document on the full domain, health-check the run,
    and — only if healthy — score it and decide accept/reject on the full
    SELECT split. Returns the run-decision half of a round record; does not
    touch `state` (the caller applies it, so a redo can reuse this against
    the same candidate without re-deriving it from the reviser).
    """
    run_output_dir = round_dir / output_subdir
    result = runner.run_arm(candidate_doc_path, run_output_dir, task_ids_path=None)
    record: dict = {
        "run": {"ok": result.ok, "wall_time_s": result.wall_time_s, "log": str(result.log_file), "error": result.error}
    }
    if not result.ok:
        record["status"] = "FAULTED"
        record["accepted"] = False
        record["reason"] = f"candidate run failed: {result.error}"
        return record

    c_records = scoring.score_file(result.output_file)
    healthy, problems = check_run_health(c_records, splits, result.log_file)
    if not healthy:
        record["status"] = "FAULTED"
        record["accepted"] = False
        record["reason"] = "run health check failed: " + "; ".join(problems)
        record["health_problems"] = problems
        return record

    c_records_path = round_dir / f"candidate_records{'' if output_subdir == 'full' else '_' + output_subdir}.json"
    _save_records(c_records, c_records_path)

    b_select = select_success(b_records, splits["select"])
    c_select = select_success(c_records, splits["select"])
    common_select = sorted(set(b_select) & set(c_select))
    from expl.stats import paired_delta

    select_cmp = paired_delta(c_select, b_select, common_select)
    accepted = select_cmp["delta"] > 0
    transition = per_task_transition(b_records, c_records, splits["edit"])

    record.update({
        "status": "OK",
        "select_comparison": select_cmp,
        "accepted": accepted,
        "edit_transition": transition,
        "candidate_summary": scoring.summarize(c_records),
        "candidate_doc": str(candidate_doc_path),
        "candidate_records_file": str(c_records_path),
    })
    test_positions = [p for p in splits["test"] if p in c_records]
    record["test_summary"] = scoring.summarize({p: c_records[p] for p in test_positions})
    return record


# ── CLI commands ─────────────────────────────────────────────────────────────


def cmd_bootstrap(args: argparse.Namespace) -> int:
    state = _load_state()
    raw_path = Path(args.raw)
    records = scoring.score_file(raw_path)
    splits = _split()
    print(f"[bootstrap] scored {len(records)} tasks from {raw_path}")
    for name in ("edit", "select", "test"):
        present = [p for p in splits[name] if p in records]
        summ = scoring.summarize({p: records[p] for p in present})
        print(f"  {name}: {len(present)}/{len(splits[name])} present — {summ}")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    v0_path = DOCS_DIR / "v0.md"
    shutil.copy(V0_DOC_PATH, v0_path)
    records_path = ROUNDS_DIR / "v0_records.json"
    _save_records(records, records_path)

    state["b_doc"] = str(v0_path)
    state["b_label"] = "v0"
    state["b_records_file"] = str(records_path)
    state["bootstrap"] = {"raw_path": str(raw_path), "n_scored": len(records)}
    _save_state(state)
    print(f"[bootstrap] B = v0 ({v0_path}); state saved to {STATE_PATH}")
    return 0


def cmd_round(args: argparse.Namespace) -> int:
    state = _load_state()
    if not state.get("b_doc"):
        print("no state — run `bootstrap` first", file=sys.stderr)
        return 2
    round_idx = args.n
    round_label = f"round{round_idx}"
    round_dir = ROUNDS_DIR / round_label
    round_dir.mkdir(parents=True, exist_ok=True)

    # Recorded so a later `redo` can revert B to exactly what it was BEFORE
    # this round, regardless of what this round's (possibly-faulted) decision
    # did to `state` — see the round-1 incident in EXP-L.md's deviation log.
    b_doc_before = state["b_doc"]
    b_records_file_before = state["b_records_file"]

    parent_doc_path = Path(state["b_doc"])
    parent_doc = parent_doc_path.read_text()
    b_records = _load_records(Path(state["b_records_file"]))
    splits = _split()
    edit_records = {p: b_records[p] for p in splits["edit"] if p in b_records}

    tables = build_domain_tables(DOMAIN)
    rev = reviser_mod.Reviser(freeze_dir=round_dir / "reviser")

    candidate = attempt_candidate(rev, tables, parent_doc, edit_records, round_label)
    round_record: dict = {
        "round": round_idx,
        "candidate": candidate,
        "b_doc_before": b_doc_before,
        "b_records_file_before": b_records_file_before,
    }

    if not candidate["ok"]:
        round_record["status"] = "NO_CANDIDATE"
        round_record["accepted"] = False
        round_record["reason"] = "no candidate — " + candidate.get("error", "")
        state["rounds"].append(round_record)
        _save_state(state)
        print(f"[{round_label}] NO CANDIDATE — B unchanged. {candidate.get('error')}")
        return 0

    candidate_doc_path = DOCS_DIR / f"{round_label}_candidate.md"
    candidate_doc_path.write_text(candidate["document"])

    decision = decide_round(round_idx, round_dir, candidate_doc_path, b_records, splits)
    round_record.update(decision)

    state["rounds"].append(round_record)
    if decision["status"] == "FAULTED":
        _save_state(state)
        print(f"[{round_label}] FAULTED — B unchanged, nothing accepted. {decision.get('reason')}")
        return 1
    if decision["accepted"]:
        state["b_doc"] = decision["candidate_doc"]
        state["b_label"] = round_label
        state["b_records_file"] = decision["candidate_records_file"]
        print(f"[{round_label}] ACCEPTED — B <- {round_label} (SELECT delta={decision['select_comparison']['delta']:+.3f})")
    else:
        print(f"[{round_label}] REJECTED — B unchanged (SELECT delta={decision['select_comparison']['delta']:+.3f})")
    _save_state(state)
    return 0


def cmd_redo(args: argparse.Namespace) -> int:
    """Re-run an EXISTING round's candidate (no new reviser call) and redo
    its run+decide step — for a round FAULTED after the fact (e.g. round 1's
    instance was unloaded mid-run by another process). Archives the old
    decision fields under `voided` before overwriting them, and reverts
    `state.b_doc`/`b_records_file` to what they were before this round
    (`b_doc_before`/`b_records_file_before`, recorded by `cmd_round`) before
    redeciding — so a round that had wrongly flipped B back off a partial
    run is corrected regardless of which way this redo's real decision goes.
    """
    state = _load_state()
    round_idx = args.n
    round_label = f"round{round_idx}"
    matches = [r for r in state["rounds"] if r["round"] == round_idx]
    if not matches:
        print(f"no existing round {round_idx} in state — nothing to redo", file=sys.stderr)
        return 2
    old_record = matches[-1]
    if not old_record.get("candidate", {}).get("ok"):
        print(f"round {round_idx} has no candidate to redo (was NO_CANDIDATE)", file=sys.stderr)
        return 2
    if not old_record.get("candidate_doc"):
        print(f"round {round_idx} has no candidate_doc on record — nothing to redo", file=sys.stderr)
        return 2
    candidate_doc_path = Path(old_record["candidate_doc"])

    b_doc_before = old_record.get("b_doc_before")
    b_records_file_before = old_record.get("b_records_file_before")
    if not b_doc_before or not b_records_file_before:
        print(
            f"round {round_idx} has no recorded b_doc_before/b_records_file_before — "
            "pass --base-doc/--base-records explicitly",
            file=sys.stderr,
        )
        if not (args.base_doc and args.base_records):
            return 2
        b_doc_before, b_records_file_before = str(args.base_doc), str(args.base_records)

    voided = dict(old_record)
    voided["voided_at"] = _now_iso()
    voided["void_reason"] = args.reason
    old_record.setdefault("voided", []).append(voided)
    for key in ("status", "run", "select_comparison", "accepted", "edit_transition",
                "candidate_summary", "candidate_doc", "candidate_records_file",
                "test_summary", "reason", "health_problems"):
        old_record.pop(key, None)

    # Revert B to what it was before this round's original (voided) decision —
    # correct regardless of whether that decision had wrongly accepted or
    # wrongly rejected on partial data.
    state["b_doc"] = b_doc_before
    state["b_records_file"] = b_records_file_before

    round_dir = ROUNDS_DIR / round_label
    b_records = _load_records(Path(b_records_file_before))
    splits = _split()
    redo_n = len(old_record.get("voided", []))
    decision = decide_round(round_idx, round_dir, candidate_doc_path, b_records, splits, output_subdir=f"full_redo{redo_n}")
    old_record.update(decision)
    old_record["b_doc_before"] = b_doc_before
    old_record["b_records_file_before"] = b_records_file_before

    if decision["status"] == "FAULTED":
        _save_state(state)
        print(f"[{round_label} redo] STILL FAULTED — B unchanged. {decision.get('reason')}")
        return 1
    if decision["accepted"]:
        state["b_doc"] = decision["candidate_doc"]
        state["b_label"] = round_label
        state["b_records_file"] = decision["candidate_records_file"]
        print(f"[{round_label} redo] ACCEPTED — B <- {round_label} (SELECT delta={decision['select_comparison']['delta']:+.3f}, n={decision['select_comparison']['n']})")
    else:
        print(f"[{round_label} redo] REJECTED — B unchanged (SELECT delta={decision['select_comparison']['delta']:+.3f}, n={decision['select_comparison']['n']})")
    _save_state(state)
    return 0


def cmd_blind(args: argparse.Namespace) -> int:
    """V1-blind control: same reviser, V0, asked for clarity only, no failure data."""
    state = _load_state()
    v0_records = _load_records(Path(state["b_records_file"])) if state.get("b_label") == "v0" else _load_records(
        ROUNDS_DIR / "v0_records.json"
    )
    v0_doc = (DOCS_DIR / "v0.md").read_text()
    tables = build_domain_tables(DOMAIN)
    round_dir = ROUNDS_DIR / "blind"
    round_dir.mkdir(parents=True, exist_ok=True)
    rev = reviser_mod.Reviser(freeze_dir=round_dir / "reviser")

    splits = _split()
    edit_records = {p: v0_records[p] for p in splits["edit"] if p in v0_records}
    candidate = attempt_candidate(rev, tables, v0_doc, edit_records, "blind", blind=True)
    blind_record: dict = {"candidate": candidate}
    if not candidate["ok"]:
        blind_record["error"] = candidate.get("error")
        state["blind"] = blind_record
        _save_state(state)
        print(f"[blind] NO CANDIDATE. {candidate.get('error')}")
        return 0

    doc_path = DOCS_DIR / "v1_blind.md"
    doc_path.write_text(candidate["document"])
    result = runner.run_arm(doc_path, round_dir / "full", task_ids_path=None)
    blind_record["run"] = {"ok": result.ok, "wall_time_s": result.wall_time_s, "error": result.error}
    if result.ok:
        records = scoring.score_file(result.output_file)
        _save_records(records, round_dir / "records.json")
        test_positions = [p for p in splits["test"] if p in records]
        blind_record["test_summary"] = scoring.summarize({p: records[p] for p in test_positions})
        blind_record["records_file"] = str(round_dir / "records.json")
        blind_record["doc"] = str(doc_path)
    state["blind"] = blind_record
    _save_state(state)
    print(f"[blind] {'ok' if result.ok else 'FAILED'} — {blind_record.get('test_summary')}")
    return 0 if result.ok else 1


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_boot = sub.add_parser("bootstrap", help="seed state from round 0's completed full V0 run")
    p_boot.add_argument("--raw", required=True, help="path to round 0's raw trajectory JSON")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_round = sub.add_parser("round", help="run one elitist-loop round")
    p_round.add_argument("--n", type=int, required=True)
    p_round.set_defaults(func=cmd_round)

    p_redo = sub.add_parser(
        "redo", help="re-run an existing round's candidate (no new reviser call) after a FAULTED/voided decision"
    )
    p_redo.add_argument("--n", type=int, required=True)
    p_redo.add_argument("--reason", required=True, help="why the prior decision is being voided")
    p_redo.add_argument("--base-doc", type=Path, default=None, help="fallback if b_doc_before wasn't recorded")
    p_redo.add_argument("--base-records", type=Path, default=None, help="fallback if b_records_file_before wasn't recorded")
    p_redo.set_defaults(func=cmd_redo)

    p_blind = sub.add_parser("blind", help="produce + score the V1-blind control")
    p_blind.set_defaults(func=cmd_blind)

    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
