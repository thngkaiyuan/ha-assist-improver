"""The test graph's own checks. Top tests each hold one real clause, and every clause is held by exactly one; every
other test refines exactly one existing parent, and sits in the file of the clause it's under. Tests touch the product
only through household.py, using only what it declares. These pass or fail on the graph's shape alone; the tests they
list fail until the code does what their clauses say. A section heading carries no claim, so it needs no test."""

import re
from pathlib import Path

from household import Household
from spec_graph import GRAPH, claims, parent, refinements, root
from spec_graph import tests as graph_tests


def held():
    return [named[0] for named in graph_tests().values() if len(named) == 1]


def file_root(test):
    """test_p0.py holds P0's clauses; test_p0_7.py holds what sits under P0.7."""
    return test.split("::")[0].removesuffix(".py").removeprefix("test_").replace("_", ".").upper()


def test_every_test_holds_one_clause_or_refines_one_parent():
    held_, refined = graph_tests(), refinements()
    defined = set(claims())
    wrong = {test: (held_[test], refined[test]) for test in held_
             if not (len(held_[test]) == 1 and held_[test][0] in defined and not refined[test])
             and not (not held_[test] and len(refined[test]) == 1)}
    assert wrong == {}


def test_test_names_are_unique_across_the_graph():
    names = [test.split("::")[1] for test in graph_tests()]
    assert sorted({name for name in names if names.count(name) > 1}) == []


def test_every_test_sits_under_a_clause_in_that_clauses_file():
    wrong = {}
    for test, named in graph_tests().items():
        under = root(test)
        expected = file_root(test)
        if under is None or (named and under.split(".")[0] != expected) or (not named and under != expected):
            wrong[test] = under
    assert wrong == {}


def test_every_clause_is_held_by_exactly_one_test():
    holding = held()
    assert [clause for clause in claims() if holding.count(clause) != 1] == []


def test_a_held_clauses_parent_is_held_too():
    holding, defined = set(held()), set(claims())
    assert sorted(clause for clause in holding if parent(clause) in defined and parent(clause) not in holding) == []


def test_tests_use_only_what_the_household_driver_declares():
    used = set()
    for path in Path(GRAPH).glob("test_*.py"):
        used |= set(re.findall(r"\bhome\.(\w+)\(", path.read_text()))
    assert sorted(name for name in used if not hasattr(Household, name)) == []
