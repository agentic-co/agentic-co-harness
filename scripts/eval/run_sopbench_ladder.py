#!/usr/bin/env python3
"""Run one of SOPBench's OWN ladder arms (`none`, `hint`, `order`, `pva`) for any
executor, with the same sharding and backend wiring as `run_sopbench_asop.py`.

The T2 ladder (T2-PROCEDURE-LADDER.md) was launched as bare `run_simulation.py`
after hand-editing `swarm/constants.py` to admit `openai/gpt-oss-20b`. That
cannot admit a second executor without another edit, and it has no sharding.
This is the same call — `build_argv` is the ASOP runner's, so every flag a
paired arm depends on is identical — plus the arm's one ladder flag:

    none   (no flag)            hint   --constraint_hint
    order  --action_order       pva    --scaffold pva

    ~/Code/SOPBench/.venv/bin/python scripts/eval/run_sopbench_ladder.py \\
        --arm pva --domain hotel --model glm-4.7 --register-model glm-4.7 --zai \\
        --output-dir <root>/ladder/hotel/pva [--shard 0/2]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import run_sopbench_asop as R  # noqa: E402

LADDER_FLAGS = {
    "none": [],
    "hint": ["--constraint_hint"],
    "order": ["--action_order"],
    "pva": ["--scaffold", "pva"],
}


def ladder_argv(args: argparse.Namespace) -> list[str]:
    return R.build_argv(args) + LADDER_FLAGS[args.arm]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", required=True, choices=sorted(LADDER_FLAGS))
    ap.add_argument("--domain", default="bank")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--sopbench", type=Path, default=R.DEFAULT_SOPBENCH)
    ap.add_argument("--model", default="openai/gpt-oss-20b")
    ap.add_argument("--register-model", action="append", default=[], metavar="NAME")
    ap.add_argument("--zai", action="store_true", help="executor on z.ai's Coding-Plan endpoint")
    ap.add_argument("--instance", default=None, help="LM Studio copy, as in run_sopbench_asop")
    ap.add_argument("--shard", default=None, metavar="K/N")
    ap.add_argument("--task-ids", type=Path, default=None, metavar="FILE",
                    help="run only these task positions (JSON list); composes with --shard")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-turns", type=int, default=0)
    ap.add_argument("--max-actions", type=int, default=0)
    ap.add_argument("--assistant-max-tokens", type=int, default=0,
                    help="SOPBench --assistant_max_tokens; 0 = its default (512)")
    args = ap.parse_args()

    args.output_dir = args.output_dir.resolve()
    if args.task_ids:
        args.task_ids = args.task_ids.resolve()
    if args.zai:
        from sopbench_judge_gate import use_zai

        use_zai()
    sys.path.insert(0, str(args.sopbench))
    os.chdir(args.sopbench)
    import run_simulation

    from sopbench_judge_gate import register_model

    for name in args.register_model:
        register_model(name)
    if args.instance:
        R.route_requests_to_instance(args.model, args.instance)
    if args.shard or args.task_ids:
        R.install_shard(run_simulation, args.domain, args.limit, args.shard, args.task_ids)

    argv_backup = sys.argv
    sys.argv = ladder_argv(args)
    print(f"[ladder] {args.arm} on {args.domain}: {' '.join(sys.argv[1:])}", file=sys.stderr)
    try:
        run_simulation.main()
    finally:
        sys.argv = argv_backup
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
