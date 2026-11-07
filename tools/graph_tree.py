"""Red/green view of the spec test graph, from one graph run.

    .venv/bin/python tools/graph_tree.py <junit.xml> <out.html>

Reads the graph statically (fixture-test/spec_graph.py), the clause names from SPEC.md and the run's JUnit XML
(pytest fixture-test/graph --graph --junitxml=...), and writes one self-contained page from graph_tree.html beside
this file. Re-run it after each graph run to watch the tree light up. Prints the counts per state and per clause.
"""

import ast
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fixture-test"))
import spec_graph  # noqa: E402

TEMPLATE = Path(__file__).with_name("graph_tree.html")
STATES = ["pass", "fail", "product", "kit", "skipped", "absent", "untested"]  # rollup order; worst-first is SEVERITY
SEVERITY = ["fail", "product", "kit", "skipped", "pass"]
BULLET = re.compile(r"^\s*-\s+\*\*(P\d+(?:\.\d+)*)\b\s*([^*]*)\*\*\s*(.*)$")
HEADING = re.compile(r"^##\s+(P\d+)\.\s*(.*)$")


def spec_clauses():
    """{id: (name, text)} for SPEC.md's sections and clauses."""
    found, current = {}, None  # id -> [name, lines]
    for line in spec_graph.SPEC.read_text().splitlines():
        heading, bullet = HEADING.match(line), BULLET.match(line)
        if heading or bullet:
            current = heading.group(1) if heading else bullet.group(1)
            name = (heading.group(2) if heading else bullet.group(2)).strip().rstrip(".")
            found[current] = [name, [] if heading else [bullet.group(3)]]
        elif line.startswith("#"):
            current = None
        elif current and line.strip():
            found[current][1].append(line.strip())
        elif current and found[current][1]:  # a blank line ends a clause, or a section's first paragraph
            current = None
    out = {}
    for cid, (name, lines) in found.items():
        text = re.sub(r"\s*\(Kai,[^)]*\)\s*$", "", " ".join(lines))
        if not name:  # a clause without a name: its first few words
            head = re.split(r"[;.:]", text, maxsplit=1)[0].split()
            name = " ".join(head[:6]) + (" ..." if len(head) > 6 else "")
        out[cid] = (name, text)
    return out


def one_line(text):
    return re.sub(r"(?<!\n)\n(?!\n)", " ", text or "").strip()


def sentence(name):
    return name.removeprefix("test_").replace("_", " ")


def classify(case):
    """(state, message) of one JUnit testcase."""
    for tag in ("failure", "error", "skipped"):
        found = case.find(tag)
        if found is None:
            continue
        message = (found.get("message") or (found.text or "").strip().split("\n")[0]).strip()
        if "Missing:" in message:
            return ("kit" if "Missing: harness:" in message else "product"), message
        return ("skipped" if tag == "skipped" else "fail"), message
    return "pass", ""


def read_run(path, held):
    """{"file::test": (state, message)} for the graph's tests in the run, and the run's facts."""
    tree = ET.parse(path).getroot()
    suites = [tree] if tree.tag == "testsuite" else tree.findall("testsuite")
    by_name = defaultdict(list)
    for key in held:
        by_name[key.split("::")[1]].append(key)
    found = defaultdict(list)
    for case in tree.iter("testcase"):
        func = case.get("name", "").split("[")[0]
        parts = case.get("classname", "").split(".")
        stems = [p for p in parts if p.startswith("test_")]
        key = f"{stems[0]}.py::{func}" if stems else None
        if key not in held:
            key = by_name[func][0] if len(by_name[func]) == 1 else None
        if key:
            found[key].append(classify(case))
    results = {}
    for key, outcomes in found.items():
        state = min((s for s, _ in outcomes), key=SEVERITY.index)
        messages = list(dict.fromkeys(m for s, m in outcomes if s == state and m))
        results[key] = (state, "\n".join(messages))
    stamp = suites[0].get("timestamp", "") if suites else ""
    took = sum(float(s.get("time") or 0) for s in suites)
    when = ""
    if stamp:
        try:
            when = datetime.fromisoformat(stamp).strftime("%-d %b %Y, %H:%M")
        except ValueError:
            when = stamp
    return results, {"iso": stamp, "when": when, "took": round(took)}


def build(junit):
    held, refined = spec_graph.tests(), spec_graph.refinements()
    docs = {f"{p.name}::{n.name}": one_line(ast.get_docstring(n)) for p, n, _ in spec_graph._functions(spec_graph.GRAPH)}
    results, run = read_run(junit, held)
    named = spec_clauses()
    by_name = {key.split("::")[1]: key for key in held}
    top = {marks[0]: key for key, marks in held.items() if len(marks) == 1}

    kids = defaultdict(list)  # parent test key -> child test keys, in source order
    orphans = []
    for key, parents in refined.items():
        if len(held[key]) == 1:
            continue
        target = (top.get(parents[0]) or by_name.get(parents[0])) if len(parents) == 1 else None
        (kids[target].append(key) if target else orphans.append(key))

    def test_node(key, seen=()):
        state, message = results.get(key, ("absent", ""))
        children = [test_node(child, seen + (key,)) for child in kids.get(key, []) if child not in seen]
        return {"k": "test", "n": sentence(key.split("::")[1]), "s": state, "m": message, "d": docs.get(key, ""),
                "t": key, "ch": children}

    def clause_node(cid):
        key = top.get(cid)
        node = test_node(key) if key else {"k": "test", "s": "untested", "m": "", "d": "", "t": "", "ch": []}
        name, text = named.get(cid, (cid, ""))
        subs = [clause_node(c) for c in claims if spec_graph.parent(c) == cid]
        node.update({"k": "clause", "c": cid, "ts": node.get("n", ""), "n": name, "x": text, "ch": subs + node["ch"]})
        return node

    claims = spec_graph.claims()
    sections = [c for c in spec_graph.clauses() if c not in claims]
    roots = []
    for section in sections:
        name, text = named.get(section, (section, ""))
        tops = [clause_node(c) for c in claims if spec_graph.parent(c) == section]
        roots.append({"k": "section", "c": section, "n": name, "x": text, "s": "none", "ch": tops})
    loose = [c for c in claims if spec_graph.parent(c) not in claims and spec_graph.parent(c) not in sections]
    if loose or orphans:
        roots.append({"k": "section", "c": "", "n": "Not linked to a clause", "x": "", "s": "none",
                      "ch": [clause_node(c) for c in loose] + [test_node(k) for k in orphans]})

    def roll(node):
        counts = dict.fromkeys(STATES, 0)
        if node["s"] in counts:
            counts[node["s"]] += 1
        for child in node["ch"]:
            for state, n in roll(child).items():
                counts[state] += n
        node["r"] = [counts[s] for s in STATES]
        node["f"] = node["s"] == "fail" and bool(node["ch"]) and all(c["s"] == "pass" for c in node["ch"])
        return counts

    totals = dict.fromkeys(STATES, 0)
    for root in roots:
        for state, n in roll(root).items():
            totals[state] += n

    def count_flags(node):
        return int(node["f"]) + sum(count_flags(c) for c in node["ch"])

    run.update({"totals": totals, "flags": sum(count_flags(r) for r in roots), "states": STATES})
    return {"run": run, "roots": roots}


def report(data):
    t = data["run"]["totals"]
    print(f"run {data['run']['when']}: {sum(t.values())} tests, {t['pass']} pass, {t['fail']} fail, "
          f"{t['product'] + t['kit']} not built ({t['product']} product, {t['kit']} test kit)"
          + "".join(f", {t[s]} {s}" for s in ("skipped", "absent", "untested") if t[s])
          + f"; {data['run']['flags']} parents fail while all their children pass")

    def walk(node, depth):
        r = dict(zip(STATES, node["r"]))
        print(f"{'  ' * depth}{node['c']:<8} {node['n'][:40]:<40} pass {r['pass']:>3}  fail {r['fail']:>3}  "
              f"product {r['product']:>3}  kit {r['kit']:>3}  (own: {node['s']})")
        for child in node["ch"]:
            if child["k"] == "clause":
                walk(child, depth + 1)

    for root in data["roots"]:
        walk(root, 0)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().split("\n\n")[1])
    data = build(sys.argv[1])
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text().replace("__GRAPH_DATA__", payload)
    Path(sys.argv[2]).write_text(page)
    report(data)
    print(f"wrote {sys.argv[2]}")


if __name__ == "__main__":
    main()
