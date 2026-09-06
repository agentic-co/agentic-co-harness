"""Loading the extensions that fill the seams.

The registries existed from P0 and nothing imported the modules that would use
them, so an extension could never register itself. This is that loader, and its
one interesting property is that failure is fatal.
"""

from __future__ import annotations

import sys

import pytest
import yaml

from agentco_harness import extensions
from agentco_harness.config import Config


@pytest.fixture(autouse=True)
def _clean():
    extensions._reset_for_tests()
    yield
    extensions._reset_for_tests()


def _module(tmp_path, name, body):
    (tmp_path / f"{name}.py").write_text(body)
    sys.path.insert(0, str(tmp_path))
    yield_name = name
    return yield_name


def test_a_declared_extension_is_imported(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "ext_ok.py").write_text("LOADED = True\n")
    assert extensions.load(["ext_ok"]) == ["ext_ok"]
    assert sys.modules["ext_ok"].LOADED is True


def test_its_registrations_actually_reach_the_registry(tmp_path, monkeypatch):
    """The whole point: import side effects are how a seam gets filled."""
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "ext_reg.py").write_text(
        "from agentco_harness.orchestrator import register_cycle_handler\n"
        "register_cycle_handler('feeds-ingest', lambda orch, task, now: True)\n"
    )
    extensions.load(["ext_reg"])
    from agentco_harness.orchestrator import CYCLE_HANDLERS

    assert "feeds-ingest" in CYCLE_HANDLERS


def test_a_broken_extension_stops_the_command(tmp_path, monkeypatch):
    """Warn-and-continue would be worse than useless here: an unregistered task
    type is NOT skipped, it takes the ordinary executor path. The beads would
    run down a path nobody wrote and nothing would say so."""
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "ext_boom.py").write_text("raise ValueError('bad wiring')\n")
    with pytest.raises(extensions.ExtensionError) as e:
        extensions.load(["ext_boom"])
    assert "ext_boom" in str(e.value)
    assert "bad wiring" in str(e.value)
    assert "UNREGISTERED" in str(e.value)      # says what silence would cost


def test_a_missing_module_is_the_same_refusal(tmp_path):
    with pytest.raises(extensions.ExtensionError) as e:
        extensions.load(["nope_not_a_module"])
    assert "could not be imported" in str(e.value)


def test_loading_twice_registers_once(tmp_path, monkeypatch):
    """Import is idempotent; registration is not. A handler registered twice
    is a handler that runs twice."""
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "ext_count.py").write_text(
        "import builtins\n"
        "builtins._ext_loads = getattr(builtins, '_ext_loads', 0) + 1\n"
    )
    extensions.load(["ext_count"])
    extensions.load(["ext_count"])
    import builtins

    assert builtins._ext_loads == 1
    assert extensions.loaded() == frozenset({"ext_count"})
    del builtins._ext_loads


def test_nothing_declared_loads_nothing():
    assert extensions.load([]) == []
    assert extensions.loaded() == frozenset()


def test_config_reads_the_list(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"tasks_path": "t.jsonl", "extensions": ["a.b", " c.d "]}))
    assert Config.load(str(cfg)).extensions == ["a.b", "c.d"]


def test_config_refuses_a_malformed_declaration(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"tasks_path": "t.jsonl", "extensions": {"not": "a list"}}))
    with pytest.raises(ValueError, match="list of module names"):
        Config.load(str(cfg))
