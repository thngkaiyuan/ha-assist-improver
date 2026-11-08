"""What the proposer and reviewer see, from why they need it: the open friction upfront (within a budget), every
other session a page at a time, the sessions a change would apply to, replays only where a change could matter, and
full recordings loaded only when opened."""

from copy import deepcopy
import json

import pytest

from test_ai_roles import ANALYSIS
from recordings import TARGET, make_recording, recorded_today
from kit import variant
from custom_components.assist_improver import evidence, hypotheses, sessions as session_set, triage
from custom_components.assist_improver.analyzer import to_analysis
from custom_components.assist_improver.exploration import SessionView

FIRST = "Toggle the fixture relay."


def household(*specs):
    """Sessions from (id, first request, friction?) specs; friction ones analysed with the fixture's friction."""
    base, analyses, views = make_recording(), [], []
    for rid, text, rough in specs:
        record = variant(base, rid, text)
        view = SessionView(record)
        analyses.append(to_analysis(rid, ANALYSIS, view) if rough else triage.smooth_analysis(rid, {"why": "fine"}))
        views.append(view)
    return hypotheses.Sessions(analyses, views)


def test_the_proposer_gets_open_friction_in_full_and_the_rest_one_line_each(monkeypatch):
    s = household(("roughaa01", FIRST, True), ("roughbb01", "Flip the fixture relay.", True),
                  ("smoothaa1", "Relay off.", False))
    monkeypatch.setattr(hypotheses.households, "SCENARIOS_SHOWN", 1)  # Room for one script: the rest come one line each.
    overview = s.overview()
    [shown] = overview["friction_scenarios"]
    assert shown["session"] in ("roughaa0", "roughbb0")  # Open friction only: not the smooth one.
    more = overview["more_friction"]
    assert len(more) == 1 and more[0]["goals_with_friction"] == [{
        "turn": 1, "summary": more[0]["goals_with_friction"][0]["summary"], "friction": ["extra turn"]}]  # Its kinds.
    assert "1 sessions without friction" in overview["other_sessions"]  # Smooth sessions are a count.
    assert {u["session"] for u in overview["friction_scenarios"]} | {m["session"] for m in more} == \
        {s for s, _ in s.open_friction()}  # Every open goal is in front of the proposer, one way or the other.


def test_every_other_session_is_a_page_away_filtered_by_what_a_rule_touches(monkeypatch):
    s = household(*[(f"talk{n}xxxxx", f"Turn the fixture relay off {n}.", n % 2 == 0) for n in range(5)])
    monkeypatch.setattr(session_set, "PAGE", 2)
    first = s.listing()
    assert first["total"] == 5 and first["pages"] == 3 and len(first["sessions"]) == 2 and "next" in first
    assert s.listing(page=3)["sessions"] and "next" not in s.listing(page=3)
    assert {r["state"] for r in s.listing(state="without_friction", page=1)["sessions"]} == {"without_friction"}
    assert s.listing(words="off 3")["total"] == 1
    assert s.listing(device=TARGET)["total"] == 5 and s.listing(device="Fixture Relay")["total"] == 5
    assert s.listing(device="switch.nothing_like_it")["total"] == 0


def test_a_draft_comes_with_the_sessions_it_would_apply_to_without_friction_first():
    s = household(("roughaa01", FIRST, True), ("roughbb01", "Flip the fixture relay.", True),
                  ("smoothaa1", "Relay off.", False), ("elsewhra1", "Kitchen light on.", False))
    elsewhere = s.briefs["elsewhra"]
    elsewhere.update(entities=["light.kitchen"], words=["kitchen light"], domains=["light"],
                     requests=["Kitchen light on."])
    draft = {"goals": [{"session": "roughaa0", "goal_turn": 1}],
             "changes": [{"kind": "registry", "entity_id": TARGET, "aliases": ["garage button"]}]}
    applies = s.where_it_applies_to(draft)
    order = [r["session"] for r in applies["sessions"]]
    assert order[0] == "smoothaa" and "roughaa0" not in order and "elsewhra" not in order  # Its own goal isn't listed.
    assert applies["count"] == 2 and all(r["touches"] == "its devices" for r in applies["sessions"])


@pytest.mark.asyncio
async def test_regression_replays_only_the_sessions_a_change_could_affect(monkeypatch):
    s = household(("roughaa01", FIRST, True), ("smoothaa1", "Relay off.", False), ("elsewhra1", "Kitchen on.", False))
    s.briefs["elsewhra"].update(entities=["light.kitchen"], words=["kitchen light"], domains=["light"],
                                requests=["Kitchen on."], live_context=False)
    replayed = []

    async def replay_calls(recording, today, setup=None):
        replayed.append(recording["id"])
        return []
    monkeypatch.setattr(evidence, "replay_calls", replay_calls)

    async def no_implementation(self, hypothesis, setup):
        return []
    monkeypatch.setattr(evidence.Evidence, "implementation", no_implementation)
    checks = await evidence.Evidence(s, recorded_today(make_recording())).check(
        {"goals": [], "changes": [{"kind": "registry", "entity_id": TARGET, "aliases": ["garage button"]}]})
    assert sorted(set(replayed)) == ["roughaa01", "smoothaa1"]  # Not the kitchen session: nothing it did can change.
    assert checks["resolution_signals"]["not_replayed"]["count"] == 1


@pytest.mark.asyncio
async def test_full_recordings_load_only_when_opened_and_only_a_few_stay(monkeypatch):
    base = make_recording()
    records = {f"lazy{n}xxxxx": variant(base, f"lazy{n}xxxxx", f"Fixture relay off {n}.") for n in range(4)}
    views = {rid: SessionView(r) for rid, r in records.items()}
    briefs = [session_set.brief(v, rid) for rid, v in views.items()]
    analyses = [triage.smooth_analysis(rid, {"why": "fine"}) for rid in records]
    loaded = []

    async def load(rid):
        loaded.append(rid)
        return SessionView(deepcopy(records[rid]))
    monkeypatch.setattr(session_set, "LOADED", 2)
    s = hypotheses.Sessions(analyses, briefs=briefs, loader=load, household=views["lazy0xxxxx"], assistant=None)
    assert loaded == [] and s.listing()["total"] == 4  # Listing and filtering read briefs only.
    for rid in ("lazy1xxxxx", "lazy2xxxxx", "lazy3xxxxx"):
        assert (await s.load(rid)) is not None
    assert s.views.get("lazy0xxx") is None  # The household is today's setup, never a session as it went.
    assert s.views.get("lazy1xxx") is None and s.views.get("lazy3xxx") is not None  # Only the newest two stay loaded.
    assert loaded == ["lazy1xxxxx", "lazy2xxxxx", "lazy3xxxxx"]
    result = await hypotheses.ReadSession(s).async_call(None, type("I", (), {"tool_args": {"session": "lazy1xxx"}})(), None)
    assert result.data["session"] == "lazy1xxx" and loaded[-1] == "lazy1xxxxx"  # Opened again: loaded again.


@pytest.mark.asyncio
async def test_briefs_are_made_once_per_recording_and_again_only_when_it_changes(hass):
    base = make_recording()
    stores = {"brief001": variant(base, "brief001", "Fixture relay off.")}
    exported = []

    class Recorder:
        index = {"brief001": {"status": "session_expired", "user_turns": 4, "updated": "1"}}

        def __init__(self):
            self.hass = hass

        async def export(self, rid):
            exported.append(rid)
            return deepcopy(stores[rid])
    recorder, cache = Recorder(), session_set.BriefCache(hass)
    briefs, household_view, _ = await session_set.catalog(cache, recorder, ["brief001"])
    assert [b["session"] for b in briefs] == ["brief001"] and household_view is not None
    await session_set.catalog(cache, recorder, ["brief001"])
    assert exported.count("brief001") == 2  # Once for the brief, once for the household's view: never again for the brief.
    recorder.index["brief001"]["updated"] = "2"
    await session_set.catalog(cache, recorder, ["brief001"])
    assert exported.count("brief001") == 3  # Changed: read once more, for its new brief and the household's view.


@pytest.mark.asyncio
async def test_while_briefs_are_made_only_the_views_the_run_may_still_use_are_held(hass):
    """A view read in full is megabytes: while the briefs are made, only the newest that could be the household's view
    (replayable, else any) and the assistant's stay held, and the run gets the same two as choosing among them all."""
    import gc
    import weakref

    class View:  # A recording's view, read in full.
        def __init__(self, rid):
            self.rid = rid

    async def run(specs):
        """specs: {id: (started, replayable, used an assistant)}. Returns (household, assistant, held at each read)."""
        made, held = [], []

        class Cache:
            async def get(self, recorder, rid):
                gc.collect()
                held.append(sorted(ref().rid for ref in made if ref() is not None))
                view = View(rid)
                made.append(weakref.ref(view))
                started, replayable, assistant = specs[rid]
                return {"recording_id": rid, "started": started, "replayable": replayable,
                        "assistant": assistant}, view

            async def save(self, recorder):
                pass

        class Recorder:
            def __init__(self):
                self.hass = hass
        briefs, household, assistant = await session_set.catalog(Cache(), Recorder(), list(specs))
        assert [b["recording_id"] for b in briefs] == list(specs)
        return household and household.rid, assistant and assistant.rid, held

    household, assistant, held = await run({
        "old": ("2026-10-01", True, False), "mid": ("2026-10-02", False, True), "new": ("2026-10-03", True, False),
        "tie": ("2026-10-03", True, True),  # As new as the one before it: the first wins.
        "late": ("2026-10-04", False, False), "last": ("2026-10-01", False, False)})
    assert (household, assistant) == ("new", "tie")
    assert held == [[], ["old"], ["mid", "old"], ["mid", "new"], ["new", "tie"], ["new", "tie"]]
    household, assistant, held = await run({"a": ("2026-10-01", False, False), "b": ("2026-10-02", False, False),
                                            "c": ("2026-10-02", False, False)})
    assert (household, assistant, held[-1]) == ("b", None, ["b"])  # None replayable: the newest.


@pytest.mark.asyncio
async def test_a_view_and_its_time_zone_are_made_off_the_event_loop(hass, monkeypatch):
    """A view and a brief read every turn of a recording (megabytes): they're made off the event loop. The view shows
    the turns' times in the household's time zone, which Home Assistant may read from disk the first time: resolved
    once, off the event loop, and the view uses what it got."""
    import zoneinfo
    from test_retention import on_the_event_loop
    from custom_components.assist_improver import exploration
    from custom_components.assist_improver.hypotheses import scenario
    record = variant(make_recording(), "zoned001", FIRST)
    for turn in record["turns"]:
        turn["source"]["time_zone"] = "America/Los_Angeles"
    first = record["turns"][0]["source"]
    first["states"]["sensor.alarm"] = {"entity_id": "sensor.alarm", "state": "2026-10-24T10:00:00+00:00",
                                       "attributes": {"device_class": "timestamp"}}
    first["exposure"]["sensor.alarm"] = True
    looked_up, zone_info, made, turns_of = [], zoneinfo.ZoneInfo, [], exploration.recording_turns
    briefed, replayable = [], session_set.replayable_turns

    def watched(key):
        looked_up.append(on_the_event_loop())
        return zone_info(key)

    def making(*args, **kwargs):
        made.append(on_the_event_loop())
        return turns_of(*args, **kwargs)
    def briefing(recording):
        briefed.append(on_the_event_loop())
        return replayable(recording)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", watched)
    monkeypatch.setattr(exploration, "recording_turns", making)
    monkeypatch.setattr(session_set, "replayable_turns", briefing)

    class Recorder:
        index = {"zoned001": {"status": "session_expired", "user_turns": 4, "updated": "1"}}

        def __init__(self):
            self.hass = hass

        async def export(self, rid):
            return deepcopy(record)
    _, view, _ = await session_set.catalog(session_set.BriefCache(hass), Recorder(), ["zoned001"])
    assert view.zone.key == "America/Los_Angeles"
    assert scenario({"recording_id": "zoned001", "goals": []}, view)["turns"][0]["at"] == "Wed 2025-12-31 16:00"
    assert not any(looked_up) and made and not any(made) and briefed and not any(briefed)


def test_decisions_carry_each_change_whole_but_bounded():
    steps = [{"action": "switch.turn_on", "target": {"entity_id": TARGET}}] * 40
    feedback = [{"title": "Name it Garage", "decision": "decline", "status": "declined", "note": "Not that name",
                 "changes": [{"kind": "registry", "entity_id": TARGET, "aliases": ["Garage"], "name": None,
                              "area_id": ""},
                             {"kind": "script", "id": "press", "config": {"sequence": steps[:2]}},
                             {"kind": "script", "id": "press_all", "config": {"sequence": steps}}]}]
    [decision] = hypotheses.decisions(feedback)
    assert decision["note"] == "Not that name" and decision["changes"][0] == {
        "kind": "registry", "entity_id": TARGET, "aliases": ["Garage"]}  # Empty values dropped.
    assert decision["changes"][1] == {"kind": "script", "id": "press", "config": {"sequence": steps[:2]}}
    clipped = decision["changes"][2]["config"]
    assert len(clipped) < len(json.dumps(steps)) and clipped.endswith("more characters omitted]")

