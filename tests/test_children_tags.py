"""CLI surface for a child's `host` / `capabilities` tags (bead ac-ccd66c91).

Both fields have existed on `ChildRef` since ac-39d4dbc8, but the only way to
set them was to hand-edit a line of `children/registry.jsonl` — which
`Plans/BreakGlassFailover.md` has to prescribe in prose, mid-incident, on the
one path where a typo quarantines the row that names the lane. These tests pin
the command that replaces that edit, and the round-trip that proves the tags it
writes are the same ones the claim gate reads.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from agentco_harness.beads import Beads
from agentco_harness.children import ChildRegistry
from agentco_harness.cli import main

WORKER = "frontsteps-worker"


def _hub(tmp_path: Path, extra: str = "") -> Path:
    """A hub config with a registry beside it and one linked child."""
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n" + extra)
    cfg = tmp_path / "config.yaml"
    result = CliRunner().invoke(
        main,
        [
            "--config", str(cfg),
            "link-child", "frontsteps", str(tmp_path / "frontsteps"),
            "--interval", "1h", "--priority", "1",
        ],
    )
    assert result.exit_code == 0, result.output
    return cfg


def _registry(tmp_path: Path) -> ChildRegistry:
    return ChildRegistry(tmp_path / "children" / "registry.jsonl")


def _run(cfg: Path, *args: str):
    return CliRunner().invoke(main, ["--config", str(cfg), "children", *args])


# ------------------------------------------------------------------ set-tags


def test_set_tags_writes_host_and_capabilities_and_round_trips(tmp_path):
    cfg = _hub(tmp_path)

    result = _run(
        cfg, "set-tags", "frontsteps",
        "--host", "macbook-pro.local",
        "--capability", "ado-write",
    )
    assert result.exit_code == 0, result.output

    child = _registry(tmp_path).get("frontsteps")
    assert child.host == "macbook-pro.local"
    assert child.capabilities == ["ado-write"]
    assert child.is_remote is True
    assert child.verifiable is False  # a remote path is not ours to poll


def test_capability_is_repeatable_and_the_flags_given_are_the_whole_set(tmp_path):
    """Convergent, not additive: the row ends up saying exactly what was typed.

    An additive flag would make the registry a function of command history —
    you could never narrow a lane, only widen it, and widening by accident is
    the direction that matters here.
    """
    cfg = _hub(tmp_path)
    _run(cfg, "set-tags", "frontsteps", "--capability", "ado-write", "--capability", "gpu")
    assert _registry(tmp_path).get("frontsteps").capabilities == ["ado-write", "gpu"]

    _run(cfg, "set-tags", "frontsteps", "--capability", "gpu")
    assert _registry(tmp_path).get("frontsteps").capabilities == ["gpu"]


def test_duplicate_capabilities_are_normalized_away_in_order(tmp_path):
    cfg = _hub(tmp_path)
    _run(
        cfg, "set-tags", "frontsteps",
        "--capability", "ado-write", "--capability", "gpu", "--capability", "ado-write",
    )
    assert _registry(tmp_path).get("frontsteps").capabilities == ["ado-write", "gpu"]


def test_an_invalid_capability_token_fails_loudly_and_writes_nothing(tmp_path):
    """The whole point of a command over a hand-edit: reject the typo at the
    keyboard instead of quarantining the row that names the lane."""
    cfg = _hub(tmp_path)
    _run(cfg, "set-tags", "frontsteps", "--capability", "ado-write")
    before = (tmp_path / "children" / "registry.jsonl").read_bytes()

    result = _run(cfg, "set-tags", "frontsteps", "--capability", "ADO WRITE")
    assert result.exit_code == 1
    assert "capabilities" in result.output
    assert (tmp_path / "children" / "registry.jsonl").read_bytes() == before


def test_clear_capabilities_empties_the_tag(tmp_path):
    cfg = _hub(tmp_path)
    _run(cfg, "set-tags", "frontsteps", "--capability", "ado-write")

    result = _run(cfg, "set-tags", "frontsteps", "--clear-capabilities")
    assert result.exit_code == 0, result.output
    assert _registry(tmp_path).get("frontsteps").capabilities == []


def test_clear_host_makes_the_child_locally_verifiable_again(tmp_path):
    """Standing a node back down onto the hub is the B-scenario failover move;
    it must not require deleting and re-linking the row."""
    cfg = _hub(tmp_path)
    (tmp_path / "frontsteps").mkdir()
    _run(cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local")

    result = _run(cfg, "set-tags", "frontsteps", "--clear-host")
    assert result.exit_code == 0, result.output
    child = _registry(tmp_path).get("frontsteps")
    assert child.host is None
    assert child.is_remote is False
    assert child.verifiable is True


def test_setting_only_the_host_leaves_capabilities_intact(tmp_path):
    """Each tag is set independently — an unmentioned tag is not an empty one."""
    cfg = _hub(tmp_path)
    _run(cfg, "set-tags", "frontsteps", "--capability", "ado-write")

    _run(cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local")
    child = _registry(tmp_path).get("frontsteps")
    assert child.capabilities == ["ado-write"]
    assert child.host == "macbook-pro.local"


def test_set_tags_preserves_every_other_registry_field(tmp_path):
    cfg = _hub(tmp_path)
    before = _registry(tmp_path).get("frontsteps")

    _run(cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local")
    after = _registry(tmp_path).get("frontsteps")

    assert after.path == before.path
    assert after.expected_interval == before.expected_interval
    assert after.priority == before.priority
    assert after.notify == before.notify
    assert after.type == before.type


def test_host_and_clear_host_together_is_refused(tmp_path):
    cfg = _hub(tmp_path)
    result = _run(
        cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local", "--clear-host"
    )
    assert result.exit_code == 1
    assert _registry(tmp_path).get("frontsteps").host is None


def test_capability_and_clear_capabilities_together_is_refused(tmp_path):
    cfg = _hub(tmp_path)
    result = _run(
        cfg, "set-tags", "frontsteps", "--capability", "gpu", "--clear-capabilities"
    )
    assert result.exit_code == 1
    assert _registry(tmp_path).get("frontsteps").capabilities == []


def test_set_tags_with_no_flags_is_an_error_not_a_silent_no_op(tmp_path):
    """A command that reports success without changing anything teaches the
    operator that it ran. Mid-incident that is the expensive lie."""
    cfg = _hub(tmp_path)
    result = _run(cfg, "set-tags", "frontsteps")
    assert result.exit_code == 1
    assert "--host" in result.output


def test_an_empty_host_string_is_refused(tmp_path):
    cfg = _hub(tmp_path)
    result = _run(cfg, "set-tags", "frontsteps", "--host", "   ")
    assert result.exit_code == 1
    assert _registry(tmp_path).get("frontsteps").host is None


def test_unknown_child_names_the_registry_and_exits_nonzero(tmp_path):
    """A typo'd name must not create a row: a child that exists only in the
    registry is a lane nothing staffs, and `link-child` is where rows are born
    (it writes the verify def in the same breath)."""
    cfg = _hub(tmp_path)
    result = _run(cfg, "set-tags", "sommeli", "--host", "macbook-pro.local")

    assert result.exit_code == 1
    assert "sommeli" in result.output
    assert "link-child" in result.output
    assert [c.name for c in _registry(tmp_path).list()] == ["frontsteps"]


def test_set_tags_reports_the_outcome_as_json(tmp_path):
    cfg = _hub(tmp_path)
    result = _run(
        cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local",
        "--capability", "ado-write", "--json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["child"] == "frontsteps"
    assert payload["host"] == "macbook-pro.local"
    assert payload["capabilities"] == ["ado-write"]
    assert payload["outcome"] in {"created", "updated", "unchanged"}


def test_re_running_the_same_tags_is_unchanged(tmp_path):
    cfg = _hub(tmp_path)
    _run(cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local")
    result = _run(cfg, "set-tags", "frontsteps", "--host", "macbook-pro.local", "--json")
    assert json.loads(result.output)["outcome"] == "unchanged"


# ------------------------------------------------------------------ listing


def test_children_list_renders_host_and_capabilities(tmp_path):
    cfg = _hub(tmp_path)
    _run(
        cfg, "set-tags", "frontsteps",
        "--host", "macbook-pro.local", "--capability", "ado-write",
    )

    result = _run(cfg, "list")
    assert result.exit_code == 0, result.output
    assert "frontsteps" in result.output
    assert "macbook-pro.local" in result.output
    assert "ado-write" in result.output


def test_children_list_says_local_and_none_for_an_untagged_child(tmp_path):
    """Absence has to render as a word. A blank column reads as a truncated
    line, and 'no capabilities' is a fact about routing, not missing data."""
    cfg = _hub(tmp_path)
    result = _run(cfg, "list")
    assert result.exit_code == 0, result.output
    assert "local" in result.output
    assert "none" in result.output


def test_children_list_json_carries_the_tags(tmp_path):
    cfg = _hub(tmp_path)
    _run(
        cfg, "set-tags", "frontsteps",
        "--host", "macbook-pro.local", "--capability", "ado-write",
    )

    result = _run(cfg, "list", "--json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [r["name"] for r in rows] == ["frontsteps"]
    assert rows[0]["host"] == "macbook-pro.local"
    assert rows[0]["capabilities"] == ["ado-write"]


def test_children_list_on_an_empty_registry_says_so(tmp_path):
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    result = _run(tmp_path / "config.yaml", "list")
    assert result.exit_code == 0, result.output
    assert "no children" in result.output.lower()


# -------------------------------------------------------------- the payoff


def test_tags_written_by_the_cli_are_the_ones_the_claim_gate_reads(tmp_path):
    """End of the rough edge: `link-child` + `children set-tags` now produce a
    registry row that routes a lane-restricted bead, with no hand-edit anywhere
    in the path."""
    cfg = _hub(tmp_path, "capabilities: [venture-keys]\n")
    _run(
        cfg, "set-tags", "frontsteps",
        "--host", "macbook-pro.local", "--capability", "ado-write",
    )

    beads = Beads(tmp_path / "tasks.jsonl")
    ado = beads.create("ado work", "d", assigned_agent=WORKER, requires=["ado-write"])

    payload = json.loads(
        CliRunner()
        .invoke(
            main,
            ["--config", str(cfg), "pull", "--agent", WORKER, "--node", "frontsteps"],
        )
        .stdout
    )
    assert [t["id"] for t in payload["claimed"]] == [ado.id]
    assert payload["capabilities"] == ["ado-write"]
