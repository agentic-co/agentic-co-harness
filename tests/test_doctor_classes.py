"""Doctor's consequence classes and the exit codes derived from them.

The property under test is a negative one: **no quantity of lesser findings can
change the verdict of a greater one.** Before this split, doctor returned
`1 if failures else 0` from a counter that several checks forgot to increment
(the dependency-cycle check printed FAIL and returned 0 for its entire life),
and every advisory line — a missing optional API key, an unverified vendor term
— shared a class with genuine breakage. Nothing could gate on it.

Each test here fails against the pre-split doctor: either the class did not
exist, the exit code was a boolean, or the finding was mute.
"""

from __future__ import annotations

import json

import pytest
import yaml

from agentco_harness import doctor as doc
from agentco_harness.doctor import (
    BROKEN,
    DEGRADED,
    EXIT_BROKEN,
    EXIT_DEGRADED,
    EXIT_OK,
    INFO,
    OK,
    DoctorReport,
    collect,
    exit_code_for,
    run_doctor,
)


# --------------------------------------------------------------- the table --

def test_exit_codes_are_derived_from_class_not_from_a_count():
    assert exit_code_for([]) == EXIT_OK
    assert exit_code_for([OK, OK, OK]) == EXIT_OK
    assert exit_code_for([INFO, OK]) == EXIT_OK
    assert exit_code_for([DEGRADED, INFO, OK]) == EXIT_DEGRADED
    assert exit_code_for([BROKEN]) == EXIT_BROKEN


def test_a_single_broken_is_not_maskable_by_any_number_of_lesser_findings():
    """The headline property. Twenty INFO lines and a wall of OK cannot make a
    broken node report clear, and DEGRADED's numerically LARGER code (2) must
    not outrank BROKEN's (1) — the bug a `max()` over exit codes would have."""
    report = DoctorReport()
    for i in range(20):
        report.add(INFO, "advisory", f"advice {i}")
    for i in range(50):
        report.add(OK, "healthy", f"fine {i}")
    for i in range(5):
        report.add(DEGRADED, "degraded", f"reduced {i}")
    report.add(BROKEN, "the.one", "a lane cannot claim")

    assert report.exit_code() == EXIT_BROKEN
    assert report.counts() == {"broken": 1, "degraded": 5, "info": 20, "ok": 50}


def test_degraded_only_is_its_own_code_not_a_failure_and_not_a_pass():
    report = DoctorReport()
    report.add(OK, "a", "fine")
    report.add(DEGRADED, "b", "unverified vendor terms")
    assert report.exit_code() == EXIT_DEGRADED


def test_info_can_never_gate():
    report = DoctorReport()
    report.add(INFO, "a", "a permanent, true, actionless disclosure")
    assert report.exit_code() == EXIT_OK


def test_a_finding_must_declare_a_known_class():
    """v2's rule: a class-less check is unregistrable. Here: unclassifiable."""
    with pytest.raises(ValueError, match="unknown consequence class"):
        DoctorReport().add("critical", "x", "y")


# ------------------------------------------------------------- filtering ----

def _report():
    report = DoctorReport()
    report.add(OK, "a", "fine")
    report.add(INFO, "b", "advice")
    report.add(DEGRADED, "c", "reduced")
    report.add(BROKEN, "d", "broken thing")
    return report


def test_class_filtering_changes_what_is_printed_and_never_the_exit_code():
    report = _report()
    rendered = report.render(classes=[INFO])
    assert "advice" in rendered
    assert "broken thing" not in rendered
    # The verdict line still carries the true exit code AND says what was hidden.
    assert "exit 1" in rendered
    assert "1 broken finding(s) hidden" in rendered
    assert report.exit_code() == EXIT_BROKEN


def test_filtering_to_broken_shows_only_broken():
    rendered = _report().render(classes=[BROKEN])
    assert "broken thing" in rendered
    assert "reduced" not in rendered
    assert "advice" not in rendered
    # Nothing was hidden that mattered, so no hidden-broken warning.
    assert "hidden by" not in rendered


def test_json_carries_class_check_id_counts_and_the_true_exit_code():
    payload = json.loads(_report().to_json())
    assert payload["schema"] == doc.JSON_SCHEMA
    assert payload["exit_code"] == EXIT_BROKEN
    assert payload["counts"] == {"broken": 1, "degraded": 1, "info": 1, "ok": 1}
    assert payload["filtered_to"] is None
    assert {f["check"] for f in payload["findings"]} == {"a", "b", "c", "d"}
    broken = [f for f in payload["findings"] if f["class"] == "broken"]
    assert broken == [{"class": "broken", "check": "d", "message": "broken thing"}]


def test_json_filtered_still_reports_the_unfiltered_exit_code_and_counts():
    payload = json.loads(_report().to_json(classes=[INFO]))
    assert payload["exit_code"] == EXIT_BROKEN          # not 0
    assert payload["counts"]["broken"] == 1            # not hidden
    assert [f["check"] for f in payload["findings"]] == ["b"]


# ------------------------------------------------- end-to-end on a real node -

@pytest.fixture(autouse=True)
def _hermetic_host(tmp_path, monkeypatch):
    """Same isolation as test_doctor.py — see its fixture for why."""
    monkeypatch.setenv("TRANSKRIPTOR_ROOT", str(tmp_path / "_no_transkriptor"))
    from agentco_harness.egress import AGENT_ROUTE

    artifact = tmp_path / "_inference-routes.json"
    artifact.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "routes": {
                    name: {
                        "vendor": "test",
                        "model": "test-model",
                        "ceiling": "CONFIDENTIAL",
                        "ceilingUnsupervised": "INTERNAL",
                        "ceilingVerified": True,
                    }
                    for name in sorted(set(AGENT_ROUTE.values()))
                },
            }
        )
    )
    monkeypatch.setenv("LIFEOS_INFERENCE_ROUTES", str(artifact))


def _node(tmp_path, **extra) -> str:
    (tmp_path / "company").mkdir(exist_ok=True)
    cfg = {
        "tasks_path": "tasks.jsonl",
        "llm": {"default_provider": "lmstudio", "default_model": "local"},
    }
    cfg.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def _classes(report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in report.findings:
        out.setdefault(f.status, []).append(f.check)
    return out


def test_a_dependency_cycle_is_broken_and_actually_changes_the_exit_code(
    tmp_path, monkeypatch
):
    """The regression this backport exists to catch.

    The cycle check printed FAIL and never touched `failures`, so a store with
    a permanent deadlock — every member waiting on another forever, nothing
    dispatchable, nothing stale enough to notice — exited 0. It shipped that way.
    Deriving the code from the recorded class makes forgetting the increment
    structurally impossible: there is no increment.
    """
    from agentco_harness.beads import Beads

    monkeypatch.chdir(tmp_path)
    cfg = _node(tmp_path)
    store = tmp_path / "tasks.jsonl"
    beads = Beads(store)
    a = beads.create(title="a", description="d", assigned_agent="claude")
    b = beads.create(title="b", description="d", assigned_agent="claude")
    beads.update(a.id, blocked_by=[b.id])
    # `update()` refuses to CLOSE a cycle, so the only way one exists is the way
    # real ones did: hand-edited JSONL, or rows written before that guard
    # shipped. Close the loop by rewriting the row, exactly as a human would.
    rows = [json.loads(line) for line in store.read_text().splitlines() if line.strip()]
    for row in rows:
        if row["id"] == b.id:
            row["blocked_by"] = [a.id]
    store.write_text("".join(json.dumps(r) + "\n" for r in rows))

    report = collect(cfg)
    cycle = [f for f in report.findings if f.check == "store.dependency_cycles"]
    assert [f.status for f in cycle] == [BROKEN]
    assert "NEVER become ready" in cycle[0].message
    assert report.exit_code() == EXIT_BROKEN


def test_a_silently_dead_schedule_is_broken(tmp_path, monkeypatch):
    """Backport 3's audit, wired into doctor as a BROKEN-class check.

    A recurring def that parses, is enabled, and has produced nothing for a
    fortnight is silent non-execution. Every other doctor check reports green
    on it — that is what made F5 cost 10+ days.
    """
    from agentco_harness.recurring import Recurring, RecurringDef

    monkeypatch.chdir(tmp_path)
    cfg = _node(tmp_path)
    Recurring(tmp_path / "recurring.jsonl").add(
        RecurringDef(
            id="finances-intake",
            title="Monthly intake",
            schedule={"every": "1d"},
            created_at="2020-01-01T00:00:00+00:00",
        )
    )

    report = collect(cfg)
    found = [f for f in report.findings if f.check == "schedules.liveness"]
    assert [f.status for f in found] == [BROKEN]
    assert "STOPPED FIRING" in found[0].message
    assert "finances-intake" in found[0].message
    assert report.exit_code() == EXIT_BROKEN


def test_a_freshly_declared_schedule_is_not_born_broken(tmp_path, monkeypatch):
    """A schedule declared a moment ago has missed nothing.

    Without the declaration-time clamp, every newly added recurring def would
    turn doctor red until its first firing — a check that fires on healthy
    systems, which is how an operator learns to skim the report. That habit is
    the thing this whole backport is trying to reverse, so it must not be
    reintroduced by the fix.
    """
    from agentco_harness.recurring import Recurring, RecurringDef

    monkeypatch.chdir(tmp_path)
    cfg = _node(tmp_path)
    Recurring(tmp_path / "recurring.jsonl").add(
        RecurringDef(id="brand-new", title="Just added", schedule={"every": "1h"})
    )

    report = collect(cfg)
    found = [f for f in report.findings if f.check == "schedules.liveness"]
    assert [f.status for f in found] == [OK]
    assert BROKEN not in _classes(report)


def test_the_usage_telemetry_gap_is_degraded_not_broken_and_not_advisory(
    tmp_path, monkeypatch
):
    """Backport 2's gap, classed. A telemetry hole loses no work (not BROKEN)
    but silently under-counts spend (not INFO)."""
    from agentco_harness.beads import Beads, TaskStatus

    monkeypatch.chdir(tmp_path)
    cfg = _node(tmp_path)
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(title="ran unmetered", description="d", assigned_agent="claude")
    beads.update(task.id, status=TaskStatus.DONE)

    report = collect(cfg)
    gaps = [
        f
        for f in report.findings
        if f.check == "usage.telemetry" and "no rows" in f.message
    ]
    assert [f.status for f in gaps] == [DEGRADED]
    assert "ran unmetered" in gaps[0].message
    assert report.exit_code() == EXIT_DEGRADED
    assert BROKEN not in _classes(report)


def test_the_static_unmetered_path_list_is_info_so_exit_2_is_not_the_resting_state(
    tmp_path, monkeypatch
):
    """UNMETERED_PATHS is a constant of the build. Emitting it as DEGRADED made
    every node permanently non-zero, which is a class that means nothing."""
    monkeypatch.chdir(tmp_path)
    report = collect(_node(tmp_path))
    named = [
        f for f in report.findings if "known model-invoking path(s)" in f.message
    ]
    assert [f.status for f in named] == [INFO]
    assert report.exit_code() == EXIT_OK


def test_a_healthy_node_is_exit_zero_with_no_broken_and_no_degraded(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    report = collect(_node(tmp_path))
    by_class = _classes(report)
    assert BROKEN not in by_class, by_class.get(BROKEN)
    assert DEGRADED not in by_class, by_class.get(DEGRADED)
    assert report.exit_code() == EXIT_OK


def test_every_finding_carries_a_stable_check_id(tmp_path, monkeypatch):
    """`--class` output and the JSON envelope are only subscribable if each
    finding names the check that produced it."""
    monkeypatch.chdir(tmp_path)
    report = collect(_node(tmp_path))
    assert report.findings
    for f in report.findings:
        assert f.check and f.check != "startup", f
        assert "." in f.check, f  # namespaced: <subsystem>.<check>


# ------------------------------------------------------------------- CLI ----

def test_cli_exposes_class_filtering_and_json_without_softening_the_exit_code(
    tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    cfg = _node(tmp_path)
    # A corrupt bead line: the store parses everything else, quarantines this
    # one, and doctor must call the node BROKEN for it.
    yaml_cfg = yaml.safe_load(open(cfg))
    tasks_path = tmp_path / yaml_cfg["tasks_path"]
    with open(tasks_path, "a") as f:
        f.write('{"id": "ac-corrupt-line", "not_a_task": true}\n')

    runner = CliRunner()

    plain = runner.invoke(main, ["-c", cfg, "doctor"])
    assert plain.exit_code == EXIT_BROKEN

    filtered = runner.invoke(main, ["-c", cfg, "doctor", "--class", "info"])
    assert filtered.exit_code == EXIT_BROKEN  # filtering cannot green a red node
    assert "ac-corrupt-line" not in filtered.output
    assert "[doctor] BROKEN (store.tasks_parse)" not in filtered.output
    assert "broken finding(s) hidden" in filtered.output

    as_json = runner.invoke(main, ["-c", cfg, "doctor", "--json"])
    assert as_json.exit_code == EXIT_BROKEN
    payload = json.loads(as_json.stdout)  # quarantine WARNINGs go to stderr
    assert payload["counts"]["broken"] >= 1
    assert any(
        f["check"] == "store.tasks_parse" and f["class"] == "broken"
        for f in payload["findings"]
    )


def test_run_doctor_keeps_its_single_argument_call_signature(tmp_path, monkeypatch):
    """Every existing caller passes exactly one positional argument."""
    monkeypatch.chdir(tmp_path)
    assert run_doctor(_node(tmp_path)) == EXIT_OK


# ------------------------------------------------- one table, two consumers --

def test_schedules_audit_keeps_its_own_single_class_exit_code(tmp_path):
    """`schedules audit` subscribes to one class, so it must NOT be handed the
    aggregate code. doctor maps the same finding to BROKEN; the standalone
    audit keeps EXIT_LIVENESS. Both constants now come from one table."""
    from agentco_harness import schedules

    assert schedules.EXIT_LIVENESS is doc.EXIT_LIVENESS
    assert schedules.EXIT_OK is doc.EXIT_OK
    assert schedules.EXIT_LIVENESS not in (EXIT_BROKEN, EXIT_DEGRADED, EXIT_OK)
