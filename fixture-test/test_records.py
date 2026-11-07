"""What the improver stores has the shapes records.py declares: every field a record has is declared there, with a
value of its declared type. Checked on what a fixture run stores, and on the household's own stores where present."""

import json
import re
import typing

import pytest

from conftest import private
from recordings import before
from kit import candidate_turn, improver  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import records
from custom_components.assist_improver.retention import unpack

BASIC = {dict: dict, list: list, str: str, int: int, bool: bool}
# The fields only earlier versions wrote, by the record type they were in: deleted from records.py with their last
# reader, and left out of the check of the household's own stores, which keep them.
LEFTOVERS = {"Recording": {"filed", "practice", "flaps_not_kept"}, "Goal": {"because", "message"},
             "AnalysisResult": {"feedback", "journeys", "not_journeys", "wants"}, "PipelineRun": {"force"},
             "Proposal": {"friction", "tested", "to_test", "seeded_reviews"},
             "RecordingSummary": {"filed", "hidden"}}  # A send-in's and Archive's marks, the flow record's since.
# The fields earlier versions wrote with another type, by record type, field and the problem found with the field
# itself (never with one of its items): left out too, as no reader misreads them.
OLD_TYPES = {
    ("AnalysisResult", "notes", "str, not a list"): "the run log's analyses of 10-22/23: only the flow record's "
                                                    "analyses, or one with turns (none of these has), have notes read",
    ("PipelineRun", "picked", "bool, not a list"): "a manual run of 10-25: the flow takes picked only as a list, the "
                                                   "panel only whether it's there",
}


def problems(value, kind, path="record"):
    """Where a stored record doesn't fit kind (contracts.problems, as stored: earlier shapes kept)."""
    from custom_components.assist_improver.contracts import problems as found
    return found(value, kind, path, required=False)


def leftover(line, kind):
    """Whether a problem found in a record of kind is what only earlier versions wrote: a field not declared in the
    record type LEFTOVERS names it under, or one with the problem OLD_TYPES names for it in its record type."""
    path, _, problem = line.partition(": ")
    *steps, field = re.sub(r"\[[^\]]*\]", "", path).split(".")  # The record, the fields down to it, and the field.
    if problem.startswith("not declared in "):
        return field in LEFTOVERS.get(problem.removeprefix("not declared in "), ())
    for step in steps[1:]:  # Down to the record type that holds the field: through list[...], dict[...] and X | None.
        kind = typing.get_type_hints(kind)[step]
        while not typing.is_typeddict(kind):
            kind = next(arm for arm in reversed(typing.get_args(kind)) if arm is not type(None))
    return (kind.__name__, field, problem) in OLD_TYPES


def check_all(index=(), recordings=(), runs=(), replays=(), improvements=(), old=False):
    """What in these stores doesn't fit records.py (old: leaving out what only earlier versions wrote, leftover)."""
    found = []

    def check(value, kind, path):
        found.extend(line for line in problems(value, kind, path) if not (old and leftover(line, kind)))
    for rid, summary in dict(index).items():
        check(summary, records.RecordingSummary, f"index[{rid[:8]}]")
    for recording in recordings:
        check(unpack(recording), records.Recording, f"recording[{recording.get('id', '?')[:8]}]")
    for n, run in enumerate(runs):
        if (kind := records.RUN_LOG.get(run.get("kind"))) is None:
            found.append(f"runs[{n}]: unknown kind {run.get('kind')!r}")
        else:
            check(run, kind, f"runs[{n}]")
    for key, item in dict(replays).items():
        check(item, records.ReplayEntry, f"replay[{key[:8]}]")
    for key, item in dict(improvements).items():
        check(item, records.Improvement, f"improvement[{key[:8]}]")
    return found


def test_the_checker_finds_undeclared_fields_and_wrong_types():
    assert problems({"id": "a", "user_turns": 2}, records.RecordingSummary) == []
    assert problems({"id": "a", "colour": "red"}, records.RecordingSummary) == [
        "record.colour: not declared in RecordingSummary"]
    assert problems({"user_turns": "2"}, records.RecordingSummary) == ["record.user_turns: str, not int"]
    assert problems({"before": {"said": [1, "2"]}}, records.Test) == ["record.before.said[1]: str, not int"]
    assert problems({"error": None, "plan": {"id": "p"}}, records.ReplayEntry) == []


def test_only_the_fields_earlier_versions_wrote_are_left_out_of_the_households_check():
    old = {"recording_id": "a", "goals": [{"turn": 1, "kind": "state", "because": "asked"}], "journeys": [],
           "because": "asked", "colour": "red"}
    found = problems(old, records.AnalysisResult)
    assert sorted(found) == ["record.because: not declared in AnalysisResult",
                             "record.colour: not declared in AnalysisResult",
                             "record.goals[0].because: not declared in Goal",
                             "record.journeys: not declared in AnalysisResult"]
    kept = sorted(line for line in found if not leftover(line, records.AnalysisResult))
    assert kept == [  # A goal's old field, not an analysis's.
        "record.because: not declared in AnalysisResult", "record.colour: not declared in AnalysisResult"]
    # A field's old type only in its record type, only that type, and only for the field itself, not its items.
    runs = [{"kind": "journey_analyzer", "analysis": {"notes": "fix it"}}, {"kind": "pipeline", "picked": True},
            {"kind": "journey_analyzer", "analysis": {"notes": {"sent": True}}}, {"kind": "pipeline", "picked": "a"},
            {"kind": "journey_analyzer", "analysis": {"notes": ["fix it"]}}, {"kind": "pipeline", "picked": [True]}]
    assert check_all(runs=runs, improvements={"i": {"test": {"notes": "fix it"}}}, old=True) == [
        "runs[2].analysis.notes: dict, not a list", "runs[3].picked: str, not a list",
        "runs[4].analysis.notes[0]: str, not dict", "runs[5].picked[0]: bool, not str",
        "improvement[i].test.notes: str, not a list"]


@pytest.mark.asyncio
async def test_what_a_full_run_stores_has_the_declared_shapes(improver):
    runtime, recorder, client = improver.runtime, improver.recorder, improver.client
    await improver.arrives(improver.friction)
    client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    kinds = {r["kind"] for r in runtime.role_runs.runs}
    assert {"journey_analyzer", "hypothesis_generator", "pipeline"} <= kinds
    assert runtime.replays.items and runtime.improvements.items  # Every store has some.
    assert check_all(index=recorder.index, recordings=[await recorder.export(k) for k in recorder.index],
                     runs=runtime.role_runs.runs, replays=runtime.replays.items,
                     improvements=runtime.improvements.items) == []


def stored(name):
    return json.loads(private("stores", f"assist_improver.{name}").read_text())["data"]


def test_the_households_own_stores_have_the_declared_shapes():
    """The household's own stores (a copy in the private folder), with what earlier versions left in them: the fields
    only they wrote, or wrote with another type, are left out (LEFTOVERS, OLD_TYPES), and so are the records of kinds
    this version no longer has (the per-goal test stage's, the reviewer run alone's) and the stores it no longer keeps
    (the improvements, built per goal before an improvement kept its test, which start afresh; the pipeline's, whose job
    the flow record has)."""
    recordings = [json.loads(p.read_text())["data"] for p in sorted(private("recordings").glob("*.json"))]
    runs = [r for r in stored("role_runs").get("runs", []) if r.get("kind") not in ("hypothesis_tests",
                                                                                     "hypothesis_reviewer")]
    index = stored("index").get("sessions", {})
    assert check_all(index=index, recordings=recordings, runs=runs, old=True) == []
    # Old notes as words are left out only because no analysis that has them has turns, whose notes would be read.
    assert not [n for n, r in enumerate(runs)
                if isinstance((r.get("analysis") or {}).get("notes"), str) and r["analysis"].get("turns")]
