"""The check before each install (tools/preflight.py), on fixture recordings: every deterministic step each stage
takes, with the replay worker served inline; no model, no network, no subprocess. A conversation whose replay breaks
fails it, as one recorded through an API the sandbox didn't know did on 2026-10-25; one the pipeline itself can't read
is skipped."""

import importlib.util
import json
from pathlib import Path

import pytest
import pytest_asyncio
from conftest import assist_home
from recordings import REPLAY_ENTITIES, prepared_recording
from kit import variant
from test_failures import analysis

ROOT = Path(__file__).resolve().parent.parent


def preflight():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "tools" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def hass(hass):
    return await assist_home(hass, REPLAY_ENTITIES)


def stores(folder, recordings, flow):
    """The improver's stores as Home Assistant keeps them (fetch copies these files); flow: {recording id: its flow
    record}."""
    def write(key, data):
        (folder / f"assist_improver.{key}").write_text(json.dumps({"version": 1, "minor_version": 1,
                                                                    "key": f"assist_improver.{key}", "data": data}))
    write("index", {"sessions": {r["id"]: {"status": r["status"], "user_turns": len(r["turns"]),
                                           **({"stored": "compact"} if r.get("compact") else {})} for r in recordings}})
    for record in recordings:
        write(f"session_{record['id']}", record)
    write("flow", {"conversations": flow})


def analysed(rid, found, outcome=None):
    """A flow record whose first round analysed the conversation; with outcome, a second round ended it so (a round
    that couldn't read it, say)."""
    rounds = {"1": {"path": ["Your Assist", "Analyze", "Propose"], "handoffs": [],
                    "records": {"Analyze": {"attempts": [], "analysis": found}}}}
    if outcome:
        rounds["2"] = {"path": ["Your Assist", "Analyze", "Your household"], "handoffs": [],
                       "records": {"Your household": {"attempts": [], "outcome": outcome}}}
    return {rid: {"id": rid, "stage": "Your household" if outcome else "Propose", "round": len(rounds), "held": None,
                  "outcome": outcome, "archived": False, "covered_by": None, "sent": None, "rounds": rounds,
                  "trips": [], "trace": []}}


def unknown_field(record):
    """Recorded with a request field today's Home Assistant doesn't take: every step that builds Assist's request
    for it fails."""
    for turn in record["turns"]:
        turn["prepared"]["context"]["a_field_that_is_gone"] = True
    return record


def misaligned(record):
    """Turns that don't line up with the chat log: the pipeline can't read it (CaseError), and sets it aside."""
    record["turns"] = record["turns"][:-1]
    return record


@pytest.mark.asyncio
async def test_the_preflight_runs_every_step_and_fails_on_what_would_break_a_stage(hass, tmp_path):
    check = preflight()
    good = variant(prepared_recording(), "goodgood01", "Toggle the fixture relay.")
    gone = unknown_field(variant(prepared_recording(), "goneapi001", "Please toggle the fixture relay."))
    gone = json.loads(json.dumps(gone).replace("2026-01-01T", "2026-01-02T"))  # The newest: the household's.
    broken = misaligned(variant(prepared_recording(), "misalign01", "Toggle the fixture relay again."))
    flow = analysed("goodgood01", {**analysis(good), "recording_id": "goodgood01"})
    folder = tmp_path / "copied"
    folder.mkdir()
    stores(folder, [good, gone, broken], flow)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    found = await check.check(hass, folder)
    conversations = found["conversations"]
    assert set(conversations) == {"goodgood01", "goneapi001", "misalign01"}
    # Each step ran on the readable one, a test's replay setup included.
    assert set(conversations["goodgood01"]) == set(check.STEPS)
    assert all(kind == check.OK for kind, _ in conversations["goodgood01"].values()), conversations["goodgood01"]
    # A field of its request is gone: replaying, probing and a test's replay setup fail, with why; so does the
    # proposer's probe on it (it's the household's newest conversation), and nothing else stops.
    failed = {step: why for step, (kind, why) in conversations["goneapi001"].items() if kind == check.FAILED}
    assert {"replay", "capability probe", "replay setup"} <= set(failed), conversations["goneapi001"]
    assert all("a_field_that_is_gone" in why for why in failed.values()), failed
    assert failed["proposer"].startswith("capability probe: WorkerError: TypeError"), failed
    # One the pipeline can't read is skipped with why: the pipeline sets it aside the same way.
    assert conversations["misalign01"]["read"][0] == check.SKIPPED
    assert all(kind == check.OK for kind, _ in found["window"].values()), found["window"]
    assert check.counts(found) == {"conversations": 3, "ok": 2, "skipped": 1, "failed": 1, "proposer": "ok",
                                   "improvements": "ok"}
    assert check.failed(found)
    lines = []
    check.report(found, lines.append)
    assert lines[-1] == "3 conversations: 2 ok (1 with a step skipped), 1 failed; proposer ok; improvements ok"
    assert any(line.startswith("FAILED   goneapi0") for line in lines)
    # The copy stays as it was fetched: the stores were read from a scratch copy.
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    # Without it, nothing fails; a conversation recorded with the improver's own Assist tool (file_improvement)
    # replays too: a sandbox serves the improver's llm platform itself, with a filing tool that files nothing.
    filed = variant(prepared_recording(), "filedone01", "Toggle the fixture relay, please.")
    filed = json.loads(json.dumps(filed).replace("2026-01-01T", "2026-01-03T"))  # The newest: its setup is today's.
    for turn in filed["turns"]:  # The filing tool among Assist's.
        turn["prepared"]["tools"].append({"name": "assist_improver__file_improvement", "description": "File it",
                                          "integration": "assist_improver", "parameters": {"type": "object"}})
    clean = tmp_path / "clean"
    clean.mkdir()
    stores(clean, [good, broken, filed], flow)
    found = await check.check(hass, clean)
    assert not check.failed(found), found["conversations"]["filedone01"]


def unknown_identity(record):
    """Turns recorded without the request's identity: a replay can't be set up on it (CaseError), so it's never
    tested (DETAILS P6.2.14)."""
    for turn in record["turns"]:
        turn["prepared"]["context"]["context"].pop("id", None)
    return record


def replayed(folder, rid, version="2026.11.0"):
    """The replays store with one replay without a change, which ran on rid on Home Assistant version."""
    (folder / "assist_improver.replays").write_text(json.dumps({"version": 1, "minor_version": 1,
        "key": "assist_improver.replays", "data": {"replays": {"p1": {
            "plan": {"id": "p1", "kind": "before", "core_version": version}, "recording_id": rid,
            "status": "reproduced"}}}}))


@pytest.mark.asyncio
async def test_a_cant_on_a_step_the_conversation_went_through_before_fails_the_preflight(hass, tmp_path):
    """The pipeline's "can't" is for good (a conversation it can't read is never read again, one it can't replay is
    never tested): on a step that went through before, it's a regression, not a skip."""
    check = preflight()
    broken = misaligned(variant(prepared_recording(), "misalign01", "Toggle the fixture relay again."))
    anonymous = unknown_identity(variant(prepared_recording(), "anonymou01", "Toggle the fixture relay."))
    folder = tmp_path / "copied"
    folder.mkdir()
    stores(folder, [broken, anonymous], {})
    found = await check.check(hass, folder)
    assert found["conversations"]["misalign01"]["read"][0] == check.SKIPPED
    assert found["conversations"]["anonymou01"]["replay setup"][0] == check.SKIPPED
    assert not check.failed(found)  # Nothing shows either went through before.
    went = check.went_through(found, {"gone000001": ["read"]})
    assert "misalign01" not in went and went["gone000001"] == ["read"]  # Only added to.
    assert "replay setup" not in went["anonymou01"] and "read" in went["anonymou01"]
    # An earlier preflight read it: a "can't" now fails, naming the step and why.
    found = await check.check(hass, folder, earlier={"misalign01": ["read"]})
    kind, why = found["conversations"]["misalign01"]["read"]
    assert kind == check.FAILED and why.startswith("can't now, though it went through before: "), why
    assert check.failed(found) and check.counts(found)["failed"] == 1
    lines = []
    check.report(found, lines.append)
    assert any(line.startswith("FAILED   misalign  read: can't now") for line in lines), lines
    # The stores show it: an analysis read it (its flow record), a replay without a change ran on the other. Once a
    # round ended it couldn't be read, its "can't" is a skip again: nothing an install does changes it.
    stores(folder, [broken, anonymous], analysed("misalign01", {"recording_id": "misalign01", "goals": []}))
    replayed(folder, "anonymou01")
    found = await check.check(hass, folder)
    assert found["conversations"]["misalign01"]["read"][0] == check.FAILED
    assert found["conversations"]["anonymou01"]["replay setup"][0] == check.FAILED
    stores(folder, [broken, anonymous], analysed("misalign01", {"recording_id": "misalign01", "goals": []},
                                                 outcome="couldn't be read"))
    found = await check.check(hass, folder)
    assert found["conversations"]["misalign01"]["read"][0] == check.SKIPPED


@pytest.mark.asyncio
async def test_a_replay_that_ran_on_an_earlier_home_assistant_is_evidence_too(hass, tmp_path):
    """A replay is the conversation said again on today's setup, whichever Home Assistant recorded it (DETAILS P6.2.4):
    a replay without a change that ran on an earlier version makes a "can't" now a regression too."""
    check = preflight()
    anonymous = unknown_identity(variant(prepared_recording(), "anonymou01", "Toggle the fixture relay."))
    folder = tmp_path / "storage"
    folder.mkdir()
    stores(folder, [anonymous], {})
    replayed(folder, "anonymou01", "2026.10.2")
    found = await check.check(hass, folder)
    assert found["conversations"]["anonymou01"]["replay setup"][0] == check.FAILED and check.failed(found)


def test_what_earlier_preflights_kept_is_read_back(tmp_path):
    check = preflight()
    went = tmp_path / "went-through.json"
    assert check.kept(went) == {"conversations": {}, "setup": None}  # None yet.
    went.write_text(json.dumps({"setup": "goodgood01", "conversations": {"anonymou01": ["read"]}}))
    assert check.kept(went) == {"conversations": {"anonymou01": ["read"]}, "setup": "goodgood01"}


@pytest.mark.asyncio
async def test_each_build_replays_on_the_setup_an_earlier_preflight_pinned(hass, tmp_path):
    """Both builds replay each conversation on the same setup: the newest conversation's, the first time, then the one
    went-through.json keeps, though a newer conversation came in, until it's gone."""
    check = preflight()
    older = variant(prepared_recording(), "olderone01", "Toggle the fixture relay.")
    newer = json.loads(json.dumps(variant(prepared_recording(), "newerone01", "Toggle the fixture relay, please."))
                       .replace("2026-01-01T", "2026-01-02T"))
    folder = tmp_path / "copied"
    folder.mkdir()
    stores(folder, [older], {})
    assert (await check.check(hass, folder))["setup"] == "olderone01"
    stores(folder, [older, newer], {})
    assert (await check.check(hass, folder))["setup"] == "newerone01"
    found = await check.check(hass, folder, setup="olderone01")
    assert found["setup"] == "olderone01" and not check.failed(found)
    lines = []
    check.report(found, lines.append)
    assert lines[-2] == "setup    olderone's first turn, kept in went-through.json until it's deleted"
    stores(folder, [newer], {})
    assert (await check.check(hass, folder, setup="olderone01"))["setup"] == "newerone01"


@pytest.mark.asyncio
async def test_the_preflight_keeps_what_went_through(tmp_path, monkeypatch):
    check = preflight()
    folder = tmp_path / "storage"
    folder.mkdir()
    went = tmp_path / "went-through.json"
    went.write_text(json.dumps({"conversations": {"gone000001": ["read"]}}))
    seen = []

    async def checked(hass, folder, *, earlier=None, setup=None):
        seen.append((earlier, setup))
        return {"conversations": {"anonymou01": {"read": (check.OK, ""), "replay setup": (check.SKIPPED, "why")}},
                "window": {}, "improvements": {}, "setup": "anonymou01"}
    monkeypatch.setattr(check, "check", checked)
    assert await check.run(folder) == 0
    assert seen == [({"gone000001": ["read"]}, None)]
    assert json.loads(went.read_text()) == {"setup": "anonymou01",
                                            "conversations": {"anonymou01": ["read"], "gone000001": ["read"]}}
    assert await check.run(folder) == 0
    # Read back, only added to, on the setup it pinned.
    assert seen[-1] == ({"anonymou01": ["read"], "gone000001": ["read"]}, "anonymou01")


@pytest.mark.asyncio
async def test_a_conversation_retention_compacted_cant_replay_and_that_is_no_regression(hass, tmp_path):
    """Retention compacts older conversations on purpose, after which they don't replay: an earlier preflight's
    replay, probe and replay setup on one don't make its "can't" now a failure, so installs aren't blocked by it."""
    from custom_components.assist_improver.retention import compact
    check = preflight()
    old = compact(variant(prepared_recording(), "compact001", "Toggle the fixture relay."))
    folder = tmp_path / "copied"
    folder.mkdir()
    stores(folder, [old], {})
    found = await check.check(hass, folder, earlier={"compact001": ["read", "replay", "capability probe",
                                                                   "replay setup"]})
    steps = found["conversations"]["compact001"]
    assert steps["read"][0] == check.OK and steps["replay setup"][0] == check.SKIPPED, steps
    assert not check.failed(found)


def test_the_improvers_own_tool_is_filing_not_an_integration_to_set_up():
    """Found by the preflight on 2026-10-25: every conversation recorded has the file_improvement tool, and
    the worker couldn't seed its sandbox when it took the tool for an integration's."""
    from custom_components.assist_improver.sandbox import seed_options
    turn = prepared_recording()["turns"][0]
    today = {"tools": ["intent__HassTurnOn", "homeassistant__GetLiveContext", "assist_improver__file_improvement",
                       "light__HassLightSet"], "timers": []}  # Assist's tools at home.
    assert seed_options(today, turn) == {"integrations": ["light"], "timer_device": None, "filing": True}


def test_deploy_runs_the_preflight_first_and_stops_on_a_failure():
    script = (ROOT / "tools" / "deploy.sh").read_text()
    assert "tools/preflight.py" in script and "--skip-preflight" in script
    assert script.index("tools/preflight.py") < script.index("build_package.py")  # Before anything is built or sent.
