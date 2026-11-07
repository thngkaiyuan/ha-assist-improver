"""The spec's test graph: which SPEC.md clauses exist, and which tests hold them.

The graph's top tests each hold one SPEC.md clause, named with @spec("P0.7"). Below them, layer by layer, each test
refines one parent test, named with @refines("P0.7") (the top test holding that clause) or @refines("test_parent")
(a child test, by its function name). A child is written as if its parent's behaviour existed: if all of a parent's
children pass, the parent's claim holds. Children of P0.7, at any depth, live in test_p0_7.py.

Read statically (no test is run or imported), so the checks hold whichever tests are selected.
"""

import ast
import re
from pathlib import Path

import pytest

spec = pytest.mark.spec  # @spec("P0.7")
refines = pytest.mark.refines  # @refines("P0.7") or @refines("test_parent")

GRAPH = Path(__file__).resolve().parent / "graph"
SPEC = GRAPH.parent.parent / "SPEC.md"
CLAUSE = re.compile(r"^\s*-\s+\*\*(P\d+(?:\.\d+)*)\b", re.M)
SECTION = re.compile(r"^##\s+(P\d+)\.", re.M)


def clauses(text=None):
    """The clause ids SPEC.md defines (its sections, such as P0, and their clauses), in order."""
    text = SPEC.read_text() if text is None else text
    found = SECTION.findall(text) + CLAUSE.findall(text)
    return list(dict.fromkeys(found))


def claims(text=None):
    """The clauses that say something (not section headings), in order: each is held by exactly one test."""
    text = SPEC.read_text() if text is None else text
    return list(dict.fromkeys(CLAUSE.findall(text)))


def parent(clause):
    return clause.rsplit(".", 1)[0] if "." in clause else None


def _marks(decorators, mark):
    out = []
    for node in decorators:
        call = node if isinstance(node, ast.Call) else None
        name = ast.unparse(call.func) if call is not None else ""
        if name.split(".")[-1] == mark and call.args and isinstance(call.args[0], ast.Constant):
            out.append(call.args[0].value)
    return out


def _functions(folder):
    for path in sorted(folder.glob("test_*.py")):
        if path.name == "test_graph.py":
            continue
        module = ast.parse(path.read_text())
        defaults = {"spec": [], "refines": []}
        for node in module.body:  # pytestmark = spec("P0")
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
                values = node.value.elts if isinstance(node.value, ast.List) else [node.value]
                defaults = {mark: _marks(values, mark) for mark in defaults}
        for node in ast.walk(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                yield path, node, {mark: _marks(node.decorator_list, mark) or defaults[mark] for mark in defaults}


def tests(folder=GRAPH):
    """{"file::test": [clauses it holds]} for every test function in the graph's files."""
    return {f"{path.name}::{node.name}": marks["spec"] for path, node, marks in _functions(folder)}


def refinements(folder=GRAPH):
    """{"file::test": [parents it refines]} for every test function in the graph's files."""
    return {f"{path.name}::{node.name}": marks["refines"] for path, node, marks in _functions(folder)}


def root(test, folder=GRAPH):
    """The clause a test sits under: its own, or its top ancestor's. None if its chain is broken or loops."""
    held, refined = tests(folder), refinements(folder)
    by_name = {key.split("::")[1]: key for key in held}
    top = {named[0]: key for key, named in held.items() if len(named) == 1}
    seen = set()
    while test not in seen:
        seen.add(test)
        if len(held[test]) == 1:
            return held[test][0]
        if len(refined[test]) != 1:
            return None
        target = refined[test][0]
        test = top.get(target) or by_name.get(target)
        if test is None:
            return None
    return None
