"""The deterministic half of the `lesson-triage` and `fix-defect` ASOPs
(`asops/lesson-triage/`): the checks their gates run, and the two writes
their steps make to the recurrence registry.

A lesson that nothing reads is not learning. These procedures close the loop
in four places, each of which is a check that fails when it is skipped:

- every lesson that went in comes out with a disposition (`check-classified`);
- a defect reuses a known fingerprint or says why it is new, and must name
  any known fingerprint that looks like it (`check-classified`) — without
  that, one defect collects three names and the recurrence count never moves;
- whether a defect is new, still open, or BACK after a remedy is computed
  from the registry, never chosen by the model (`dispose`, `check-disposed`);
- "fixed" means a check that fails on the base and passes on the fix, re-run
  here in a scratch worktree (`check-fix --verify-red`). Without that the
  status is "mitigated", and a later sighting reopens it all the same.

Files live in the run's working directory, by fixed names:

    <dir>/lessons.jsonl     {id, text, source}                 (collect)
    <dir>/classified.jsonl  {id, type, ...}                    (classify)
    <dir>/defects.json      {items: [...]}, <dir>/outcome      (dispose)
    <dir>/metrics.json                                         (report)
    <dir>/fix.json          {fingerprint, status, ...}         (fix-defect)

The registry is an append-only JSONL of events, one per line:
`{fingerprint, event, scope, at, ...}` with event one of `opened`, `seen`,
`reopened`, `fixed`, `mitigated`, `retired`. A fingerprint's status is the
last status event; `seen` only counts. Stdlib only, so a gate can run it
anywhere the harness is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

LESSON_TYPES = ("fact", "defect", "noise")
STATUS_EVENTS = ("opened", "reopened", "fixed", "mitigated", "retired")
EVENTS = STATUS_EVENTS + ("seen",)
FIX_STATUSES = ("fixed", "mitigated", "wont_fix")
OUTCOMES = ("TRIAGED", "REGRESSIONS", "NOTHING_TO_TRIAGE")

FINGERPRINT = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
# What a defect is a defect IN: a procedure step, or a tool by path.
TARGET = re.compile(r"^(asop:[a-z0-9][a-z0-9-]*(#[0-9]+)?|tool:\S+)$")
# Two fingerprints sharing this much of their words are presumed the same
# defect until the classifier says otherwise, by name.
SIMILAR = 0.5


class CheckFailed(Exception):
    """A gate check failed. The message is what the executor is told."""


# --- reading ---------------------------------------------------------------


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise CheckFailed(f"{path.name}:{n}: not JSON ({e.msg})")
        if not isinstance(row, dict):
            raise CheckFailed(f"{path.name}:{n}: a row must be an object")
        rows.append(row)
    return rows


def _text(row: dict, key: str, where: str) -> str:
    v = row.get(key)
    if not isinstance(v, str) or not v.strip():
        raise CheckFailed(f"{where}: '{key}' must be a non-empty string")
    return v


def load_registry(path: Path) -> list[dict]:
    events = _jsonl(path)
    for n, e in enumerate(events, 1):
        if e.get("event") not in EVENTS:
            raise CheckFailed(f"registry:{n}: unknown event {e.get('event')!r}")
        if not FINGERPRINT.match(str(e.get("fingerprint", ""))):
            raise CheckFailed(f"registry:{n}: bad fingerprint {e.get('fingerprint')!r}")
    return events


def fold(events: Iterable[dict], *, exclude_scope: Optional[str] = None) -> dict:
    """fingerprint -> {status, seen, target}, from the events in order.
    `exclude_scope` drops one scope's own events, so a re-run of `dispose`
    for the same scope computes the same answer as the first run did."""
    state: dict = {}
    for e in events:
        if exclude_scope is not None and e.get("scope") == exclude_scope:
            continue
        s = state.setdefault(e["fingerprint"], {"status": None, "seen": 0, "target": None})
        if e["event"] == "seen":
            s["seen"] += 1
        else:
            s["status"] = "open" if e["event"] in ("opened", "reopened") else e["event"]
        if e.get("target"):
            s["target"] = e["target"]
    return state


def _words(fp: str) -> set:
    return set(fp.split("-"))


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) >= SIMILAR


# --- lesson-triage gates ---------------------------------------------------


def check_lessons(d: Path) -> list[dict]:
    lessons = _jsonl(d / "lessons.jsonl")
    seen = set()
    for i, row in enumerate(lessons, 1):
        lid = _text(row, "id", f"lessons:{i}")
        if lid in seen:
            raise CheckFailed(f"lessons:{i}: id {lid!r} appears twice")
        seen.add(lid)
        _text(row, "text", f"lessons:{i}")
        if not isinstance(row.get("source"), dict) or not row["source"]:
            raise CheckFailed(f"lessons:{i}: 'source' must name the run it came from")
    return lessons


def check_classified(d: Path, registry: Path) -> list[dict]:
    lessons = check_lessons(d)
    rows = _jsonl(d / "classified.jsonl")
    known = fold(load_registry(registry))
    want = {r["id"] for r in lessons}
    got: dict = {}
    for i, row in enumerate(rows, 1):
        where = f"classified:{i}"
        lid = _text(row, "id", where)
        if lid in got:
            raise CheckFailed(f"{where}: lesson {lid!r} classified twice")
        got[lid] = row
        kind = row.get("type")
        if kind not in LESSON_TYPES:
            raise CheckFailed(f"{where}: type must be one of {LESSON_TYPES}, got {kind!r}")
        if kind == "fact":
            _text(row, "summary", where)
            _text(row, "recorded_in", where)
        elif kind == "noise":
            _text(row, "reason", where)
        else:
            _check_defect(row, where, known)
    missing, extra = sorted(want - set(got)), sorted(set(got) - want)
    if missing:
        raise CheckFailed(f"lessons with no classification: {', '.join(missing)}")
    if extra:
        raise CheckFailed(f"classifications of lessons that were not collected: {', '.join(extra)}")
    return rows


def _check_defect(row: dict, where: str, known: dict) -> None:
    target = _text(row, "target", where)
    if not TARGET.match(target):
        raise CheckFailed(f"{where}: target {target!r} must be 'asop:<id>[#<step>]' or 'tool:<path>'")
    fp = _text(row, "fingerprint", where)
    if not FINGERPRINT.match(fp):
        raise CheckFailed(f"{where}: fingerprint {fp!r} must be a lowercase slug of 3-64 chars")
    if fp in known:
        return
    _text(row, "new_because", where)
    ruled_out = set(row.get("not_duplicate_of") or [])
    near = sorted(k for k in known if similar(fp, k) and k not in ruled_out)
    if near:
        raise CheckFailed(
            f"{where}: new fingerprint {fp!r} looks like known {', '.join(near)}. Reuse one, "
            f"or list each in 'not_duplicate_of' with the reason in 'new_because'")
    # Later lessons in the same batch may reuse it without re-arguing.
    known[fp] = {"status": None, "seen": 0, "target": target}


def plan_disposition(d: Path, registry: Path, scope: str) -> dict:
    """What `dispose` writes, computed from the classification and the
    registry as it stood before this scope. One item per fingerprint."""
    rows = check_classified(d, registry)
    known = fold(load_registry(registry), exclude_scope=scope)
    items: dict = {}
    for row in rows:
        if row["type"] != "defect":
            continue
        fp = row["fingerprint"]
        prior = known.get(fp, {"status": None, "seen": 0})
        item = items.get(fp)
        if item is None:
            disposition = {None: "new", "open": "matched"}.get(prior["status"], "reopened")
            item = items[fp] = {
                "fingerprint": fp, "target": row["target"], "disposition": disposition,
                "prior_status": prior["status"] or "none", "occurrences": prior["seen"],
                "lessons": [],
            }
        item["occurrences"] += 1
        item["lessons"].append(row["id"])
    out = []
    for item in items.values():
        item["lessons"] = ",".join(item["lessons"])  # hand-offs pass scalars only
        out.append(item)
    if not rows:
        outcome = "NOTHING_TO_TRIAGE"
    elif any(i["disposition"] == "reopened" for i in out):
        outcome = "REGRESSIONS"
    else:
        outcome = "TRIAGED"
    return {"items": out, "outcome": outcome}


def dispose(d: Path, registry: Path, scope: str, now: str) -> dict:
    plan = plan_disposition(d, registry, scope)
    (d / "defects.json").write_text(json.dumps({"items": plan["items"]}, indent=2) + "\n")
    (d / "outcome").write_text(plan["outcome"] + "\n")
    have = {(e.get("scope"), e["fingerprint"], e["event"], e.get("lesson"))
            for e in load_registry(registry)}
    new = []
    for item in plan["items"]:
        fp = item["fingerprint"]
        status = {"new": "opened", "reopened": "reopened"}.get(item["disposition"])
        if status and (scope, fp, status, None) not in have:
            new.append({"fingerprint": fp, "event": status, "scope": scope, "at": now,
                        "target": item["target"]})
        for lid in item["lessons"].split(","):
            if (scope, fp, "seen", lid) not in have:
                new.append({"fingerprint": fp, "event": "seen", "scope": scope, "at": now,
                            "lesson": lid})
    _append(registry, new)
    return plan


def check_disposed(d: Path, registry: Path, scope: str) -> None:
    plan = plan_disposition(d, registry, scope)
    try:
        written = json.loads((d / "defects.json").read_text())
        outcome = (d / "outcome").read_text().strip()
    except (OSError, json.JSONDecodeError) as e:
        raise CheckFailed(f"dispose has not written its files: {e}")
    if written != {"items": plan["items"]}:
        raise CheckFailed("defects.json differs from what the registry implies; re-run dispose, "
                          "do not edit it by hand")
    if outcome != plan["outcome"]:
        raise CheckFailed(f"outcome is {outcome!r}, the registry implies {plan['outcome']!r}")
    events = fold(load_registry(registry))
    for item in plan["items"]:
        if item["disposition"] in ("new", "reopened") and events[item["fingerprint"]]["status"] != "open":
            raise CheckFailed(f"{item['fingerprint']}: dispose did not record it as open")


# --- report ----------------------------------------------------------------


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def metrics(registry: Path) -> dict:
    """Two numbers per fingerprint, and their totals: hours from first
    opened to first fixed, and how often it came back after a `fixed`. If
    neither falls over time, the loop is not working."""
    per: dict = {}
    for e in load_registry(registry):
        m = per.setdefault(e["fingerprint"], {"opened_at": None, "fixed_at": None,
                                              "last": None, "repeats_after_fixed": 0})
        if e["event"] == "seen":
            continue
        if e["event"] == "opened" and m["opened_at"] is None:
            m["opened_at"] = e.get("at")
        if e["event"] == "fixed" and m["fixed_at"] is None:
            m["fixed_at"] = e.get("at")
        if e["event"] == "reopened" and m["last"] == "fixed":
            m["repeats_after_fixed"] += 1
        m["last"] = e["event"]
    out = {}
    for fp, m in sorted(per.items()):
        hours = None
        if m["opened_at"] and m["fixed_at"]:
            hours = round((_ts(m["fixed_at"]) - _ts(m["opened_at"])).total_seconds() / 3600, 1)
        out[fp] = {"hours_to_fix": hours, "repeats_after_fixed": m["repeats_after_fixed"]}
    fixed = sorted(v["hours_to_fix"] for v in out.values() if v["hours_to_fix"] is not None)
    return {
        "fingerprints": out,
        "median_hours_to_fix": fixed[len(fixed) // 2] if fixed else None,
        "repeats_after_fixed": sum(v["repeats_after_fixed"] for v in out.values()),
    }


def check_report(d: Path, registry: Path) -> None:
    try:
        written = json.loads((d / "metrics.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CheckFailed(f"metrics.json missing or unreadable: {e}")
    if written != metrics(registry):
        raise CheckFailed("metrics.json differs from the registry; re-run report")


# --- fix-defect gates ------------------------------------------------------


def _run(cmd: str, cwd: Path, timeout: int) -> int:
    try:
        return subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except subprocess.TimeoutExpired:
        return 124


def check_fix(d: Path, *, repo: Optional[Path] = None, verify_red: bool = False,
              structural: bool = False, allow_retire: bool = False,
              timeout: int = 600) -> dict:
    """`fix.json` is a claim; this is the check that fails if it is false.
    `fixed` needs a regression check that passes NOW and, with `verify_red`,
    fails on the base commit with the check's own files laid over it."""
    try:
        fix = json.loads((d / "fix.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CheckFailed(f"fix.json missing or unreadable: {e}")
    fp = _text(fix, "fingerprint", "fix.json")
    status = fix.get("status")
    if status not in FIX_STATUSES:
        raise CheckFailed(f"fix.json: status must be one of {FIX_STATUSES}, got {status!r}")
    if status == "wont_fix":
        if not allow_retire:
            raise CheckFailed("only a procedure where a person decides may retire a defect "
                              "(wont_fix); record it as mitigated, or escalate")
        _text(fix, "reason", "fix.json")
        return fix
    if status == "mitigated":
        if not (fix.get("check") or "").strip():
            _text(fix, "no_check_reason", "fix.json")
        return fix
    check = _text(fix, "check", "fix.json")
    base = _text(fix, "base", "fix.json")
    files = fix.get("check_files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
        raise CheckFailed("fix.json: 'fixed' needs 'check_files', the files the check adds or changes")
    if structural and fix.get("structural") is not True:
        raise CheckFailed(f"{fp} came back after a remedy; 'fixed' needs a structural change "
                          "(structural: true, with 'structural_change' saying what)")
    if structural:
        _text(fix, "structural_change", "fix.json")
    repo = (repo or Path.cwd()).resolve()
    if _run(check, repo, timeout) != 0:
        raise CheckFailed(f"the regression check does not pass on the fix: {check}")
    if verify_red and _run_on_base(check, repo, base, files, timeout) == 0:
        raise CheckFailed(f"the regression check also passes on {base}, so it does not detect "
                          "the defect. Record the status as 'mitigated' or write a check that does")
    return fix


def _run_on_base(check: str, repo: Path, base: str, files: list, timeout: int) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fixcheck-"))
    wt = tmp / "base"
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(wt), base], cwd=repo,
                       check=True, capture_output=True)
        for f in files:
            src, dst = repo / f, wt / f
            if not src.is_file():
                raise CheckFailed(f"check file {f} does not exist on the fix")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return _run(check, wt, timeout)
    except subprocess.CalledProcessError as e:
        raise CheckFailed(f"could not check out {base}: {e.stderr.decode().strip()}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo,
                       capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def check_red(d: Path, *, repo: Optional[Path] = None, timeout: int = 600) -> None:
    """Before the fix: the regression check exists and FAILS on the tree as
    it is, or the run says why no check can be written (and will end
    mitigated, whatever its fix looks like)."""
    try:
        fix = json.loads((d / "fix.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CheckFailed(f"fix.json missing or unreadable: {e}")
    check = (fix.get("check") or "").strip()
    if not check:
        _text(fix, "no_check_reason", "fix.json")
        return
    _text(fix, "base", "fix.json")
    if _run(check, (repo or Path.cwd()).resolve(), timeout) == 0:
        raise CheckFailed(f"the regression check passes before any fix, so it does not "
                          f"detect the defect: {check}")


def record_fix(d: Path, registry: Path, scope: str, now: str) -> None:
    fix = json.loads((d / "fix.json").read_text())
    event = {"fixed": "fixed", "mitigated": "mitigated", "wont_fix": "retired"}[fix["status"]]
    row = {"fingerprint": fix["fingerprint"], "event": event, "scope": scope, "at": now}
    if any((e.get("scope"), e["fingerprint"], e["event"]) == (scope, row["fingerprint"], event)
           for e in load_registry(registry)):
        return
    _append(registry, [row])


def check_recorded(d: Path, registry: Path) -> None:
    fix = json.loads((d / "fix.json").read_text())
    want = {"fixed": "fixed", "mitigated": "mitigated", "wont_fix": "retired"}[fix["status"]]
    state = fold(load_registry(registry)).get(fix["fingerprint"])
    if not state or state["status"] != want:
        raise CheckFailed(f"registry does not record {fix['fingerprint']} as {want}")


def _append(registry: Path, rows: list) -> None:
    if not rows:
        return
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


# --- CLI -------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agentco_harness.lesson_triage")
    p.add_argument("command", choices=[
        "check-lessons", "check-classified", "dispose", "check-disposed", "report",
        "check-report", "check-red", "check-fix", "record-fix", "check-recorded"])
    p.add_argument("dir", type=Path)
    p.add_argument("--registry", type=Path,
                   default=Path(os.environ.get("LESSON_REGISTRY", "metrics/recurrence.jsonl")))
    p.add_argument("--scope")
    p.add_argument("--repo", type=Path)
    p.add_argument("--verify-red", action="store_true")
    p.add_argument("--structural", action="store_true")
    p.add_argument("--allow-retire", action="store_true")
    a = p.parse_args(argv)
    try:
        c = a.command
        if c in ("dispose", "check-disposed", "record-fix") and not a.scope:
            # A gate is authored with the version and cannot name the run's
            # scope, so the run's first step writes it beside its files.
            scope_file = a.dir / "scope"
            a.scope = scope_file.read_text().strip() if scope_file.exists() else ""
            if not a.scope:
                raise CheckFailed(f"{c} needs --scope, or a '{scope_file}' file")
        if c == "check-lessons":
            check_lessons(a.dir)
        elif c == "check-classified":
            check_classified(a.dir, a.registry)
        elif c == "dispose":
            print(dispose(a.dir, a.registry, a.scope, _now())["outcome"])
        elif c == "check-disposed":
            check_disposed(a.dir, a.registry, a.scope)
        elif c == "report":
            (a.dir / "metrics.json").write_text(json.dumps(metrics(a.registry), indent=2) + "\n")
        elif c == "check-report":
            check_report(a.dir, a.registry)
        elif c == "check-red":
            check_red(a.dir, repo=a.repo)
        elif c == "check-fix":
            check_fix(a.dir, repo=a.repo, verify_red=a.verify_red, structural=a.structural,
                      allow_retire=a.allow_retire)
        elif c == "record-fix":
            record_fix(a.dir, a.registry, a.scope, _now())
        elif c == "check-recorded":
            check_recorded(a.dir, a.registry)
    except CheckFailed as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
