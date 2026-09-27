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
from pathlib import Path
from typing import Optional

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

    parent_doc_path = Path(state["b_doc"])
    parent_doc = parent_doc_path.read_text()
    b_records = _load_records(Path(state["b_records_file"]))
    splits = _split()
    edit_records = {p: b_records[p] for p in splits["edit"] if p in b_records}

    tables = build_domain_tables(DOMAIN)
    rev = reviser_mod.Reviser(freeze_dir=round_dir / "reviser")

    candidate = attempt_candidate(rev, tables, parent_doc, edit_records, round_label)
    round_record: dict = {"round": round_idx, "candidate": candidate}

    if not candidate["ok"]:
        round_record["accepted"] = False
        round_record["reason"] = "no candidate — " + candidate.get("error", "")
        state["rounds"].append(round_record)
        _save_state(state)
        print(f"[{round_label}] NO CANDIDATE — B unchanged. {candidate.get('error')}")
        return 0

    candidate_doc_path = DOCS_DIR / f"{round_label}_candidate.md"
    candidate_doc_path.write_text(candidate["document"])

    run_output_dir = round_dir / "full"
    result = runner.run_arm(candidate_doc_path, run_output_dir, task_ids_path=None)
    round_record["run"] = {
        "ok": result.ok, "wall_time_s": result.wall_time_s, "log": str(result.log_file),
        "error": result.error,
    }
    if not result.ok:
        round_record["accepted"] = False
        round_record["reason"] = f"candidate run failed: {result.error}"
        state["rounds"].append(round_record)
        _save_state(state)
        print(f"[{round_label}] candidate RUN FAILED — B unchanged. {result.error}")
        return 1

    c_records = scoring.score_file(result.output_file)
    c_records_path = round_dir / "candidate_records.json"
    _save_records(c_records, c_records_path)

    b_select = select_success(b_records, splits["select"])
    c_select = select_success(c_records, splits["select"])
    common_select = sorted(set(b_select) & set(c_select))
    from expl.stats import paired_delta  # local import: keeps `expl` import cost off cmd_bootstrap

    select_cmp = paired_delta(c_select, b_select, common_select)
    accepted = select_cmp["delta"] > 0

    transition = per_task_transition(b_records, c_records, splits["edit"])
    refusal = scoring.summarize(c_records)

    round_record.update({
        "select_comparison": select_cmp,
        "accepted": accepted,
        "edit_transition": transition,
        "candidate_summary": refusal,
        "candidate_doc": str(candidate_doc_path),
        "candidate_records_file": str(c_records_path),
    })
    if "test" in splits:
        test_positions = [p for p in splits["test"] if p in c_records]
        round_record["test_summary"] = scoring.summarize({p: c_records[p] for p in test_positions})

    state["rounds"].append(round_record)
    if accepted:
        state["b_doc"] = str(candidate_doc_path)
        state["b_label"] = round_label
        state["b_records_file"] = str(c_records_path)
        print(f"[{round_label}] ACCEPTED — B <- {round_label} (SELECT delta={select_cmp['delta']:+.3f})")
    else:
        print(f"[{round_label}] REJECTED — B unchanged (SELECT delta={select_cmp['delta']:+.3f})")
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

    p_blind = sub.add_parser("blind", help="produce + score the V1-blind control")
    p_blind.set_defaults(func=cmd_blind)

    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
