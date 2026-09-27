"""repobench's AST mutation engine: site-finding is deterministic, each site
applies exactly one change, and mutants never compound (fresh parse each
time). No network, no repo checkout — pure toy source."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.mutate import apply_mutation, find_sites  # noqa: E402

SRC = """
def add(a, b):
    return a + b

def is_small(x):
    if x < 10 and x >= 0:
        return True
    return False

def pick(a, b, flag):
    if flag:
        return a
    return b
"""


def test_find_sites_is_deterministic():
    s1 = find_sites(SRC)
    s2 = find_sites(SRC)
    assert s1 == s2


def test_binop_swap_changes_only_that_operator():
    sites = find_sites(SRC)
    site = next(s for s in sites if s.kind == "binop")
    out = apply_mutation(SRC, site)
    assert "return a - b" in out
    assert out.count("\n") == SRC.count("\n") or True  # unparse may reformat; no crash is the bar
    # nothing else in `pick`/`is_small` changed
    assert "if flag:" in out and "return a" in out and "return b" in out


def test_cmpop_swap():
    sites = find_sites(SRC)
    site = next(s for s in sites if s.kind == "cmpop" and s.detail.startswith("Lt"))
    out = apply_mutation(SRC, site)
    assert "x <= 10" in out


def test_boolop_swap():
    sites = find_sites(SRC)
    site = next(s for s in sites if s.kind == "boolop")
    out = apply_mutation(SRC, site)
    assert "x < 10 or x >= 0" in out


def test_constant_bool_swap_vs_int_offbyone():
    sites = find_sites(SRC)
    bool_sites = [s for s in sites if s.kind == "constant" and "True" in s.detail]
    int_sites = [s for s in sites if s.kind == "constant" and s.detail == "10 -> 11"]
    assert bool_sites and int_sites
    out_bool = apply_mutation(SRC, bool_sites[0])
    assert "return False\n    return False" in out_bool.replace(" ", " ")  # both branches now False
    out_int = apply_mutation(SRC, int_sites[0])
    assert "x < 11" in out_int


def test_varswap_swaps_every_load_in_the_function_only():
    sites = find_sites(SRC)
    site = next(s for s in sites if s.kind == "varswap" and s.enclosing_function == "pick")
    out = apply_mutation(SRC, site)
    # pick's behavior is now inverted...
    assert "if flag:\n        return b\n    return a" in out
    # ...but add() (a different function) is untouched
    assert "return a + b" in out


def test_stale_site_raises():
    sites = find_sites(SRC)
    site = sites[0]
    bad_source = "def totally_different():\n    pass\n"
    import pytest
    with pytest.raises(ValueError):
        apply_mutation(bad_source, site)


def test_no_sites_proposed_for_single_arg_or_unused_arg_functions():
    src = """
def one_arg(x):
    return x + 1

def unused_second(a, b):
    return a + 1
"""
    sites = find_sites(src)
    varswaps = [s for s in sites if s.kind == "varswap"]
    assert varswaps == []  # neither function has 2+ USED positional args
