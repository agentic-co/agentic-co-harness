"""Stage-1 final table: the per-round TEST curve, the primary comparison
(V_best vs V0 on TEST, paired exact McNemar + 95% CI), the bar check (V_best
> V1-blind), and cost per round (EXP-L.md "Adoption gate, versioning, cost").

Reads `evals/expl/rounds/state.json`, written by `loop.py`. Print-only; it
does not re-run anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parents[1]
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from expl import scoring, split as split_mod  # noqa: E402
from expl.stats import holm_bonferroni, paired_delta  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
EVALS_DIR = REPO_ROOT / "evals" / "expl"
ROUNDS_DIR = EVALS_DIR / "rounds"
STATE_PATH = ROUNDS_DIR / "state.json"
SPLIT_PATH = EVALS_DIR / "split.json"


def _records(path: str) -> dict:
    raw = json.loads(Path(path).read_text())
    return {int(k): v for k, v in raw.items()}


def main() -> int:
    if not STATE_PATH.exists():
        print("no state.json — nothing to report yet", file=sys.stderr)
        return 1
    state = json.loads(STATE_PATH.read_text())
    splits = split_mod.load_split_file(SPLIT_PATH)
    test_positions = splits["test"]

    v0_records = _records(ROUNDS_DIR / "v0_records.json") if (ROUNDS_DIR / "v0_records.json").exists() else None
    if v0_records is None:
        print("no v0_records.json — run `bootstrap` first", file=sys.stderr)
        return 1
    v0_test = {p: v0_records[p]["success"] for p in test_positions if p in v0_records}

    print("### EXP-L Stage 1 — TEST curve (never used for accept/reject)\n")
    print(f"{'version':16s}{'accepted':>10s}{'test n':>8s}{'success':>10s}")
    print(f"{'V0':16s}{'—':>10s}{len(v0_test):>8d}{sum(v0_test.values())/len(v0_test):>10.3f}")

    v_best_label = "v0"
    v_best_test = v0_test
    total_wall = 0.0
    total_reviser_tokens = 0

    for r in state.get("rounds", []):
        label = f"round{r['round']}"
        cand = r.get("candidate", {})
        usage = cand.get("usage") or {}
        total_reviser_tokens += usage.get("total_tokens", 0) or 0
        wall = (r.get("run") or {}).get("wall_time_s") or 0.0
        total_wall += wall
        if not cand.get("ok"):
            print(f"{label:16s}{'no-candidate':>10s}{'—':>8s}{'—':>10s}")
            continue
        ts = r.get("test_summary") or {}
        acc = "yes" if r.get("accepted") else "no"
        n = ts.get("n", 0)
        sr = ts.get("success_rate")
        print(f"{label:16s}{acc:>10s}{n:>8d}{(sr if sr is not None else float('nan')):>10.3f}")
        if r.get("accepted"):
            v_best_label = label
            v_best_test = {p: v["success"] for p, v in _records(r["candidate_records_file"]).items() if p in test_positions}

    print(f"\nV_best = {v_best_label}")
    print(f"total round wall-clock: {total_wall/3600:.2f} h; total reviser tokens: {total_reviser_tokens}\n")

    common = sorted(set(v_best_test) & set(v0_test))
    primary = paired_delta(v_best_test, v0_test, common)
    print("### Primary: V_best vs V0 on TEST (paired exact McNemar)")
    print(
        f"  delta={primary['delta']:+.3f} [{primary['lo']:+.3f},{primary['hi']:+.3f}] "
        f"gained={primary['gained']} lost={primary['lost']} p={primary['p']:.4f} n={primary['n']}"
    )
    bar_ci_excludes_zero = primary["lo"] > 0 or primary["hi"] < 0
    print(f"  bar (CI excludes 0): {'PASS' if bar_ci_excludes_zero else 'fail'}")

    blind = state.get("blind")
    if blind and blind.get("test_summary"):
        blind_records = _records(blind["records_file"])
        blind_test = {p: v["success"] for p, v in blind_records.items() if p in test_positions}
        common_b = sorted(set(v_best_test) & set(blind_test))
        vs_blind = paired_delta(v_best_test, blind_test, common_b)
        print("\n### V_best vs V1-blind on TEST")
        print(
            f"  delta={vs_blind['delta']:+.3f} [{vs_blind['lo']:+.3f},{vs_blind['hi']:+.3f}] "
            f"gained={vs_blind['gained']} lost={vs_blind['lost']} p={vs_blind['p']:.4f}"
        )
        bar_vs_blind = vs_blind["delta"] > 0 or (
            sum(v_best_test.values()) / len(v_best_test) > sum(blind_test.values()) / len(blind_test)
        )
        print(f"  bar (V_best > V1-blind): {'PASS' if bar_vs_blind else 'fail'}")
        overall = bar_ci_excludes_zero and bar_vs_blind
    else:
        print("\n### V1-blind: not yet run (`loop.py blind`)")
        overall = False

    print(f"\n### Stage-1 bar overall: {'PASS' if overall else 'NOT MET'}")

    holm = holm_bonferroni([("v_best_vs_v0", primary["p"])])
    print(f"\nHolm (Stage-1 primaries only; pooled across stages later): {holm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
