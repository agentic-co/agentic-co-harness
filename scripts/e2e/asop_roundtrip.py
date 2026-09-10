"""End-to-end round trip of a real ASOP, with every refusal it should make.

Run it from an empty directory:  python3 scripts/e2e/asop_roundtrip.py

This is the dogfood that exercises the 2026-09-09 gate work against the actual
contract rather than against unit fixtures. It authors a procedure, tries to
misuse it six ways, then walks a legitimate run to done.

What it proves, in order:

  draft run refused             a procedure not activated files no work
  all roles bound to one actor  constraint_unsatisfiable — you CANNOT be both
                                the operator and the validator, which is the
                                separation the whole contract is for
  missing input                 inputs_missing
  deterministic gate            fails while its condition is false, passes after
  judged gate                   parks; it is not the executor's to answer
  executor self-approval        refused
  approval with no verdict      refused (ASOP.md §5.3)
  distinct approver + verdict   done, verdict stored in the contract's shape
  parent close                  auto-closes with a run_review over every step

The parent auto-closing is correct HERE because this run's parent carries no
gate of its own; a gated parent is deliberately left for the path that
enforces gates (see _on_step_closed).
"""

from pathlib import Path
from agentco_harness.asop_store import AsopStore
from agentco_harness.beads import Beads

store = AsopStore(Path("asops.jsonl"))
beads = Beads(Path("tasks.jsonl"))

body = {
    "title": "Bulk vault operation",
    "task_type": "vault-restructure",
    "purpose": "Reorganise notes without losing any.",
    "trigger": "An operation will touch more than 20 vault files.",
    "inputs": [
        {"name": "vault", "description": "the vault root"},
        {"name": "operation", "description": "the command to run"},
    ],
    "roles": {
        "operator": {"kind": "agent"},
        "validator": {"kind": "agent"},
    },
    "constraints": [{"distinct": ["operator", "validator"]}],
    "steps": [
        {"step": 1, "name": "back-up-outside-the-sync-tree", "role": "operator",
         "purpose": "A dated copy outside the sync tree before any write.",
         "definition_of_done": "A restorable backup exists outside the vault.",
         "validation": "The backup path exists and is non-empty.",
         "gate": {"kind": "deterministic", "check": "test -s backup.tar"}},
        {"step": 2, "name": "run-under-the-manifest-guard", "role": "operator",
         "purpose": "Perform the operation, proving nothing was lost.",
         "definition_of_done": "The operation ran and the manifest diff is clean.",
         "validation": "manifest_guard exits zero.",
         "gate": {"kind": "deterministic", "check": "test -f guard-passed"}},
        {"step": 3, "name": "validate-the-diff", "role": "validator",
         "purpose": "A second party reads the diff before it is called done.",
         "definition_of_done": "Every change is intended.",
         "validation": "A verdict naming what was checked.",
         "gate": {"kind": "judged",
                  "check": "every added/removed path is accounted for",
                  "judge_route": "distinct-from-executor"},
         "after": [2]},
    ],

}

rec = store.create(body, author="leeloo", author_kind="agent", asop_id="bulk-vault-op")
print(f"created  {rec.asop_id} v{rec.version} status={rec.status.value}")

# ---- 1. try to run a DRAFT
try:
    store.run("bulk-vault-op", inputs={"vault": "/v", "operation": "mv"},
              bindings={"operator": "leeloo", "validator": "leeloo"}, beads=beads)
    print("RAN A DRAFT  <-- should not happen")
except Exception as e:
    print(f"refused draft run     -> {getattr(e, 'code', type(e).__name__)}")

store.activate("bulk-vault-op", 1, by_kind="human")
print("activated v1 (by_kind=human)")

# ---- 2. try to activate as an agent (policy)
try:
    store.create({**body, "title": "x"}, author="leeloo", author_kind="agent", asop_id="other")
    store.activate("other", 1, by_kind="agent")
    print("agent activated       <-- allowed")
except Exception as e:
    print(f"agent activation      -> {getattr(e, 'code', type(e).__name__)}")

# ---- 3. ALL ROLES ME, as asked
try:
    store.run("bulk-vault-op", inputs={"vault": "/v", "operation": "mv"},
              bindings={"operator": "leeloo", "validator": "leeloo"}, beads=beads)
    print("ALL-ROLES-ME RAN      <-- should not happen")
except Exception as e:
    print(f"all roles = me        -> {getattr(e, 'code', type(e).__name__)}: {e}")

# ---- 4. missing input
try:
    store.run("bulk-vault-op", inputs={"vault": "/v"},
              bindings={"operator": "leeloo", "validator": "cato"}, beads=beads)
except Exception as e:
    print(f"missing input         -> {getattr(e, 'code', type(e).__name__)}")

# ---- 5. distinct bindings: the run that should file
parent = store.run("bulk-vault-op", inputs={"vault": "/v", "operation": "mv"},
                   bindings={"operator": "leeloo", "validator": "cato"}, beads=beads)
kids = sorted((t for t in beads.list() if t.parent_id == parent.id),
              key=lambda t: t.metadata["sop_ref"]["step"])
print(f"filed run {parent.id} with {len(kids)} steps; parent blocked_by={len(parent.blocked_by)}")
for k in kids:
    print(f"   step {k.metadata['sop_ref']['step']} {k.title!r} agent={k.assigned_agent} "
          f"gate={k.metadata['verify']['kind']} blocked_by={k.blocked_by}")

beads = Beads(Path("tasks.jsonl"))
parent = [t for t in beads.list() if t.metadata.get("run")][0]
kids = sorted((t for t in beads.list() if t.parent_id == parent.id),
              key=lambda t: t.metadata["sop_ref"]["step"])
s1, s2, s3 = kids

def show(t, label):
    t = beads.get(t.id)
    print(f"  {label:<34} {t.status.value}")

# step 1 — deterministic gate that FAILS (no backup.tar yet)
beads.claim(s1.id, "leeloo"); beads.complete(s1.id)
show(s1, "step1 no backup -> gate fails")
Path("backup.tar").write_text("pretend tarball")
beads.complete(s1.id)
show(s1, "step1 after backup exists")

# step 2 — downstream was blocked until now
print(f"  step2 ready (step1 is done)             {"yes" if s2.id in {t.id for t in beads.ready()} else "NO"}")
Path("guard-passed").write_text("manifest clean")
beads.claim(s2.id, "leeloo"); beads.complete(s2.id)
show(s2, "step2 guard-passed")

# step 3 — the judged gate
beads.claim(s3.id, "cato"); beads.complete(s3.id)
show(s3, "step3 judged -> parks")

print("  --- now the separation-of-duties fixes ---")
for who, reason, label in [
    ("cato", "looks fine", "executor approves itself"),
    ("mabidoli", None, "approval with no verdict"),
    ("mabidoli", "  ", "approval with blank verdict"),
]:
    try:
        beads.approve_verify(s3.id, approver=who, reason=reason)
        print(f"  {label:<34} ALLOWED  <-- hole")
    except ValueError as e:
        print(f"  {label:<34} refused: {str(e)[:58]}")

done = beads.approve_verify(s3.id, approver="mabidoli",
                            reason="both added paths trace to the move; no deletions")
print(f"  distinct approver + verdict        {done.status.value}")
print(f"  verdict recorded                   {done.metadata['verify_approval']['verdict']}")
print(f"  PARENT (run) status                {beads.get(parent.id).status.value}")
review = beads.get(parent.id).metadata.get("run_review")
print(f"  run_review steps                   {[r['step'] for r in review['steps']] if review else None}")
