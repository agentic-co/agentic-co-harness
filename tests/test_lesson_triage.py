"""The lesson-triage procedures (`asops/lesson-triage/`) and the checks their
gates run (`agentco_harness.lesson_triage`).

Each rule below is one way lessons used to go unread or come back unseen:
a lesson dropped between collection and disposition, one defect under two
names, a defect that returned after it was "fixed", and "fixed" claimed
without a check that could tell."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from agentco_harness import lesson_triage as lt
from agentco_harness.lesson_triage import CheckFailed

ASOPS = Path(__file__).resolve().parents[1] / "asops" / "lesson-triage"
RECORDS = ("lesson-triage", "fix-defect", "fix-regression")


def write_jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def lesson(i, text="something went wrong"):
    return {"id": f"L{i}", "text": text, "source": {"run": f"r{i}", "step": "build"}}


def defect(i, fp, target="asop:build#3", **kw):
    return {"id": f"L{i}", "type": "defect", "target": target, "fingerprint": fp, **kw}


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "lessons"
    d.mkdir()
    return d, tmp_path / "recurrence.jsonl"


# --- the records -----------------------------------------------------------


def _records():
    return {name: yaml.safe_load((ASOPS / f"{name}.yaml").read_text()) for name in RECORDS}


def test_records_validate_against_the_spec():
    asop = pytest.importorskip("asop")
    from importlib.metadata import version
    if tuple(int(x) for x in version("asop-spec").split(".")[:2]) < (0, 5):
        pytest.skip("hand-off joins need asop-spec >= 0.5.0")
    from asop.chain import validate_handoff_targets
    # The identity fields belong to the store, not the body (asop.validate_asop).
    identity = {"asop_id", "version", "status", "author", "author_kind"}
    bodies = {n: asop.validate_asop({k: v for k, v in r.items() if k not in identity}, asop_id=n)
              for n, r in _records().items()}

    def resolve(to):
        b = bodies.get(to["asop_id"])
        return (1, b) if b else None

    for name, b in bodies.items():
        validate_handoff_targets(b, resolve, asop_id=name)


def test_every_deterministic_gate_names_a_command_the_checker_has():
    commands = {"check-lessons", "check-classified", "check-disposed", "check-report",
                "check-red", "check-fix", "check-recorded"}
    for name, rec in _records().items():
        for step in rec["steps"]:
            check = step["gate"]["check"]
            if step["gate"]["kind"] == "deterministic" and "lesson_triage" in check:
                assert check.split("lesson_triage ")[1].split()[0] in commands, (name, step["name"])


def test_only_the_human_gated_procedure_may_retire():
    gates = {n: [s["gate"]["check"] for s in r["steps"]] for n, r in _records().items()}
    assert any("--allow-retire" in g for g in gates["fix-regression"])
    assert not any("--allow-retire" in g for g in gates["fix-defect"])
    assert any(s["gate"]["kind"] == "human" for s in _records()["fix-regression"]["steps"])


# --- nothing collected leaves without a disposition ------------------------


def test_a_lesson_with_no_classification_fails(run):
    d, reg = run
    write_jsonl(d / "lessons.jsonl", [lesson(1), lesson(2)])
    write_jsonl(d / "classified.jsonl", [defect(1, "relay-corruption")
                                         | {"new_because": "first sighting"}])
    with pytest.raises(CheckFailed, match="no classification: L2"):
        lt.check_classified(d, reg)


def test_fact_and_noise_must_say_where_and_why(run):
    d, reg = run
    write_jsonl(d / "lessons.jsonl", [lesson(1)])
    write_jsonl(d / "classified.jsonl", [{"id": "L1", "type": "noise"}])
    with pytest.raises(CheckFailed, match="'reason'"):
        lt.check_classified(d, reg)
    write_jsonl(d / "classified.jsonl", [{"id": "L1", "type": "fact", "summary": "x"}])
    with pytest.raises(CheckFailed, match="'recorded_in'"):
        lt.check_classified(d, reg)


def test_a_defect_needs_a_real_target(run):
    d, reg = run
    write_jsonl(d / "lessons.jsonl", [lesson(1)])
    write_jsonl(d / "classified.jsonl", [defect(1, "relay-corruption", target="the build",
                                                new_because="first")])
    with pytest.raises(CheckFailed, match="target"):
        lt.check_classified(d, reg)


# --- one defect, one name ---------------------------------------------------


def test_a_new_fingerprint_that_looks_known_must_say_so(run):
    d, reg = run
    write_jsonl(reg, [{"fingerprint": "relay-message-corruption", "event": "opened",
                       "scope": "e0", "at": "2026-10-01T00:00:00Z"}])
    write_jsonl(d / "lessons.jsonl", [lesson(1)])
    write_jsonl(d / "classified.jsonl", [defect(1, "relay-corruption", new_because="different")])
    with pytest.raises(CheckFailed, match="looks like known relay-message-corruption"):
        lt.check_classified(d, reg)
    write_jsonl(d / "classified.jsonl", [defect(1, "relay-corruption", new_because="different",
                                                not_duplicate_of=["relay-message-corruption"])])
    lt.check_classified(d, reg)


def test_a_new_fingerprint_must_say_why_it_is_new(run):
    d, reg = run
    write_jsonl(d / "lessons.jsonl", [lesson(1)])
    write_jsonl(d / "classified.jsonl", [defect(1, "relay-corruption")])
    with pytest.raises(CheckFailed, match="new_because"):
        lt.check_classified(d, reg)


# --- the registry decides, not the model -----------------------------------


def _classify(d, rows):
    write_jsonl(d / "lessons.jsonl", [lesson(r["id"][1:]) for r in rows])
    write_jsonl(d / "classified.jsonl", rows)


def test_new_then_matched_then_reopened(run):
    d, reg = run
    _classify(d, [defect(1, "base-stack-broken", new_because="first"),
                  defect(2, "base-stack-broken")])
    plan = lt.dispose(d, reg, "e1", "2026-10-10T10:05:00Z")
    [item] = plan["items"]
    assert (item["disposition"], item["occurrences"], item["lessons"]) == ("new", 2, "L1,L2")
    assert plan["outcome"] == "TRIAGED"

    _classify(d, [defect(3, "base-stack-broken")])
    assert lt.dispose(d, reg, "e2", "2026-10-10T12:00:00Z")["items"][0]["disposition"] == "matched"

    lt._append(reg, [{"fingerprint": "base-stack-broken", "event": "fixed", "scope": "e2",
                      "at": "2026-10-10T19:32:00Z"}])
    _classify(d, [defect(4, "base-stack-broken")])
    plan = lt.dispose(d, reg, "e3", "2026-10-11T09:00:00Z")
    assert plan["items"][0]["disposition"] == "reopened"
    assert plan["items"][0]["prior_status"] == "fixed"
    assert plan["outcome"] == "REGRESSIONS"
    lt.check_disposed(d, reg, "e3")


def test_a_mitigated_defect_that_returns_is_reopened(run):
    d, reg = run
    write_jsonl(reg, [{"fingerprint": "wrong-reason-red", "event": "opened", "scope": "e0"},
                      {"fingerprint": "wrong-reason-red", "event": "mitigated", "scope": "e0"}])
    _classify(d, [defect(1, "wrong-reason-red")])
    assert lt.dispose(d, reg, "e1", "2026-10-11T00:00:00Z")["items"][0]["disposition"] == "reopened"


def test_dispose_is_idempotent_for_a_scope(run):
    d, reg = run
    _classify(d, [defect(1, "relay-corruption", new_because="first")])
    lt.dispose(d, reg, "e1", "t")
    before = reg.read_text()
    again = lt.dispose(d, reg, "e1", "t")
    assert reg.read_text() == before
    assert again["items"][0]["disposition"] == "new"
    lt.check_disposed(d, reg, "e1")


def test_a_hand_edited_disposition_fails_the_gate(run):
    d, reg = run
    write_jsonl(reg, [{"fingerprint": "relay-corruption", "event": "fixed", "scope": "e0"}])
    _classify(d, [defect(1, "relay-corruption")])
    lt.dispose(d, reg, "e1", "t")
    out = json.loads((d / "defects.json").read_text())
    out["items"][0]["disposition"] = "matched"
    (d / "defects.json").write_text(json.dumps(out))
    with pytest.raises(CheckFailed, match="differs"):
        lt.check_disposed(d, reg, "e1")


def test_no_lessons_is_its_own_outcome(run):
    d, reg = run
    _classify(d, [])
    assert lt.dispose(d, reg, "e1", "t")["outcome"] == "NOTHING_TO_TRIAGE"


# --- the two numbers ---------------------------------------------------------


def test_metrics_count_time_to_fix_and_returns_after_fix(run):
    _, reg = run
    write_jsonl(reg, [
        {"fingerprint": "base-stack-broken", "event": "opened", "scope": "a", "at": "2026-10-10T10:05:00Z"},
        {"fingerprint": "base-stack-broken", "event": "fixed", "scope": "a", "at": "2026-10-10T19:35:00Z"},
        {"fingerprint": "base-stack-broken", "event": "reopened", "scope": "b", "at": "2026-10-11T00:00:00Z"},
        {"fingerprint": "base-stack-broken", "event": "seen", "scope": "b", "at": "2026-10-11T00:00:00Z"},
        {"fingerprint": "lint-only", "event": "opened", "scope": "a", "at": "2026-10-10T00:00:00Z"},
        {"fingerprint": "lint-only", "event": "mitigated", "scope": "a", "at": "2026-10-10T01:00:00Z"},
        {"fingerprint": "lint-only", "event": "reopened", "scope": "b", "at": "2026-10-11T00:00:00Z"},
    ])
    m = lt.metrics(reg)
    assert m["fingerprints"]["base-stack-broken"] == {"hours_to_fix": 9.5, "repeats_after_fixed": 1}
    # a mitigation that fails is a return, but not a return after a FIX
    assert m["fingerprints"]["lint-only"] == {"hours_to_fix": None, "repeats_after_fixed": 0}
    assert m["repeats_after_fixed"] == 1


# --- "fixed" is earned -------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (r / "app.txt").write_text("broken\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True,
                          text=True, check=True).stdout.strip()
    (r / "check.sh").write_text("grep -q fixed app.txt\n")
    (r / "app.txt").write_text("fixed\n")
    git("add", ".")
    git("commit", "-qm", "fix")
    fixdir = r / "fix"
    fixdir.mkdir()
    return r, fixdir, base


def _fix(fixdir, **kw):
    (fixdir / "fix.json").write_text(json.dumps({"fingerprint": "base-stack-broken", **kw}))


def test_fixed_needs_a_check_that_fails_on_base(repo):
    r, fixdir, base = repo
    _fix(fixdir, status="fixed", check="sh check.sh", base=base, check_files=["check.sh"])
    lt.check_fix(fixdir, repo=r, verify_red=True)
    # a check that cannot tell base from fix does not earn "fixed"
    (r / "always.sh").write_text("true\n")
    _fix(fixdir, status="fixed", check="sh always.sh", base=base, check_files=["always.sh"])
    with pytest.raises(CheckFailed, match="also passes on"):
        lt.check_fix(fixdir, repo=r, verify_red=True)


def test_fixed_needs_the_check_to_pass_now(repo):
    r, fixdir, base = repo
    _fix(fixdir, status="fixed", check="false", base=base, check_files=["check.sh"])
    with pytest.raises(CheckFailed, match="does not pass on the fix"):
        lt.check_fix(fixdir, repo=r)


def test_without_a_check_it_is_mitigated_and_must_say_why(repo):
    r, fixdir, _ = repo
    _fix(fixdir, status="mitigated")
    with pytest.raises(CheckFailed, match="no_check_reason"):
        lt.check_fix(fixdir, repo=r)
    _fix(fixdir, status="mitigated", no_check_reason="the failure is in a vendor's model")
    lt.check_fix(fixdir, repo=r)


def test_a_regression_fix_must_be_structural(repo):
    r, fixdir, base = repo
    # describing a change is not the same as asserting it is structural
    _fix(fixdir, status="fixed", check="sh check.sh", base=base, check_files=["check.sh"],
         structural_change="added a second lint")
    with pytest.raises(CheckFailed, match="came back after a remedy"):
        lt.check_fix(fixdir, repo=r, structural=True)
    _fix(fixdir, status="fixed", check="sh check.sh", base=base, check_files=["check.sh"],
         structural=True)
    with pytest.raises(CheckFailed, match="'structural_change'"):
        lt.check_fix(fixdir, repo=r, structural=True)
    _fix(fixdir, status="fixed", check="sh check.sh", base=base, check_files=["check.sh"],
         structural=True, structural_change="the base stack is built by CI, not by hand")
    lt.check_fix(fixdir, repo=r, structural=True)


def test_an_agent_cannot_retire_a_defect(repo):
    r, fixdir, _ = repo
    _fix(fixdir, status="wont_fix", reason="too hard")
    with pytest.raises(CheckFailed, match="only a procedure where a person decides"):
        lt.check_fix(fixdir, repo=r)
    lt.check_fix(fixdir, repo=r, allow_retire=True)


def test_check_red_refuses_a_check_that_already_passes(repo):
    r, fixdir, base = repo
    _fix(fixdir, check="true", base=base)
    with pytest.raises(CheckFailed, match="passes before any fix"):
        lt.check_red(fixdir, repo=r)
    _fix(fixdir, check="false", base=base)
    lt.check_red(fixdir, repo=r)


def test_record_fix_writes_the_status_once(run, repo):
    _, reg = run
    _, fixdir, _ = repo
    _fix(fixdir, status="mitigated", no_check_reason="x")
    lt.record_fix(fixdir, reg, "e1", "t")
    lt.record_fix(fixdir, reg, "e1", "t")
    assert len(reg.read_text().splitlines()) == 1
    lt.check_recorded(fixdir, reg)


def test_cli_reads_scope_from_the_run_directory(run):
    d, reg = run
    _classify(d, [defect(1, "relay-corruption", new_because="first")])
    (d / "scope").write_text("e1\n")
    assert lt.main(["dispose", str(d), "--registry", str(reg)]) == 0
    assert lt.main(["check-disposed", str(d), "--registry", str(reg)]) == 0
    (d / "scope").unlink()
    assert lt.main(["check-disposed", str(d), "--registry", str(reg)]) == 1
