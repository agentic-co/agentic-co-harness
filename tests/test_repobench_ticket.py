"""repobench's ticket derivation: mechanical extraction from pytest's own
failure text, never the test source or the mutated function's name — and,
specifically, the assertEqual/assertNotEqual polarity fix (N-shaped bug this
build found and corrected before it shipped: unittest's fixed message
templates show the CURRENT relation, not the source assertion's operator,
which is the opposite of what pytest's own assertion-rewrite text shows)."""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.ticket import derive_ticket  # noqa: E402


def test_plain_assert_equal_says_should_equal():
    stdout = (
        "    def test_merge_sorted():\n"
        ">       assert list(merge_sorted([1, 2, 3], [1, 2, 3])) == [1, 1, 2, 2, 3, 3]\n"
        "E       assert [] == [1, 1, 2, 2, 3, 3]\n"
    )
    t = derive_ticket(stdout, "tests/test_x.py::test_merge_sorted", {"merge_sorted"})
    assert t.tier == "assert-value"
    assert "should equal" in t.text
    assert "should NOT equal" not in t.text
    assert "merge_sorted" not in t.text  # the forbidden name never leaks


def test_plain_assert_notequal_says_should_not_equal():
    stdout = "E       assert 5 != 5\n"
    t = derive_ticket(stdout, "tests/test_x.py::test_thing", set())
    assert "should NOT equal" in t.text


def test_unittest_assertequal_failure_says_should_equal():
    # unittest's FIXED template for assertEqual failing is "A != B" — this
    # means "these came out unequal, but the test wants them equal", the
    # OPPOSITE polarity from a plain `assert A != B` in source.
    stdout = "E       AssertionError: 3 != 4\n"
    t = derive_ticket(stdout, "tests/test_x.py::test_thing", set())
    assert "should equal" in t.text
    assert "should NOT equal" not in t.text
    assert "`4`" in t.text and "`3`" in t.text


def test_unittest_assertnotequal_failure_says_should_not_equal():
    # unittest's FIXED template for assertNotEqual failing is "A == B" —
    # "these came out equal, but the test wants them different."
    stdout = "E       AssertionError: 5 == 5\n"
    t = derive_ticket(stdout, "tests/test_x.py::test_thing", set())
    assert "should NOT equal" in t.text


def test_unittest_lists_differ_prefix_is_stripped_from_actual_value():
    stdout = "E       AssertionError: Lists differ: [1, 2] != [1, 3]\n"
    t = derive_ticket(stdout, "tests/test_x.py::test_thing", set())
    assert "Lists differ" not in t.text
    assert "`[1, 2]`" in t.text and "`[1, 3]`" in t.text


def test_exception_tier_redacts_forbidden_function_name():
    stdout = (
        "E       TypeError: remove() takes 2 positional arguments but 3 were given\n"
    )
    t = derive_ticket(stdout, "tests/test_x.py::test_remove", forbidden_names={"remove"})
    assert t.tier == "exception"
    assert "remove" not in t.text
    assert "the function" in t.text


def test_fallback_tier_when_nothing_matches():
    t = derive_ticket("no useful lines here\n", "tests/test_x.py::test_thing", set())
    assert t.tier == "fallback"
    assert "regressed" in t.text


def test_parametrize_suffix_is_surfaced_as_input_description():
    stdout = "E       assert 3 == 4\n"
    t = derive_ticket(stdout, "tests/test_x.py::test_thing[1-2]", set())
    assert "1-2" in t.text


def test_object_repr_and_address_are_redacted():
    stdout = "E       assert <function foo at 0x104abc123> == None\n"
    t = derive_ticket(stdout, "tests/test_x.py::test_thing", set())
    assert "0x104abc123" not in t.text
    assert "<function foo" not in t.text
