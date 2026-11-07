"""One clock for a replay (clock.py, DETAILS P6.2.5; Kai, 2026-11-05), in real worker processes: the sandbox's Home
Assistant, its scheduler, timers, templates and the sun read one time, which starts at the recorded moment, moves on
to each turn's recorded time, and lets waits pass at once inside a turn, never past the next recorded turn; an
automation's week runs through seven simulated days within a budget."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import time

import pytest
import pytest_asyncio

from conftest import assist_home
from recordings import PROFILE, REPLAY_ENTITIES, TARGET, bind_agent, prepared_recording, recorded_today
from test_worker import provider_process, real_worker  # noqa: F401 - fixtures.
from custom_components.assist_improver.cases import recording_turns
from custom_components.assist_improver.replay import SandboxHome

START = datetime(2026, 3, 1, 6, 0, 10, tzinfo=UTC)  # The conversation's first turn: months before the tests run.
PORCH = "switch.fixture_porch"
NOW = "{{ now().isoformat() }}"  # Instructions that say the sandbox's time.


@pytest_asyncio.fixture
async def hass(hass):
    """The offline HA with HA's real Assist tools; replays run them in a sandbox of their own."""
    return await assist_home(hass, REPLAY_ENTITIES)


def moved(record, by, since=None):
    """record with each recorded time at or after since (ISO; None: every one) moved on by a timedelta."""
    def walk(node):
        for key, value in (node.items() if isinstance(node, dict) else enumerate(node)):
            if key in ("at", "created", "last_activity") and isinstance(value, str) and value >= (since or ""):
                node[key] = (datetime.fromisoformat(value) + by).isoformat()
            elif isinstance(value, (dict, list)):
                walk(value)
    walk(record)
    return record


def conversation(*gaps, end=None):
    """prepared_recording in UTC with a porch switch (off), its first turn at START, each later turn the gap given
    (timedeltas) after the one before, and its end (last_activity) end after the last turn (None: none)."""
    record = prepared_recording()
    moved(record, START - datetime.fromisoformat(record["turns"][0]["source"]["at"]))
    for number, gap in enumerate(gaps, 2):
        moved(record, gap, record["turns"][number - 1]["user"]["created"])
    for turn in record["turns"]:
        turn["source"].update(time_zone="UTC")
        turn["source"]["states"][PORCH] = {"entity_id": PORCH, "state": "off",
                                           "attributes": {"friendly_name": "Fixture porch"}}
        turn["source"]["entities"][PORCH] = {"name": "Fixture porch", "aliases": [None], "area_id": "fixture_area"}
    if end is not None:
        record["last_activity"] = (datetime.fromisoformat(record["turns"][-1]["source"]["at"]) + end).isoformat()
    return record


def at(moment):
    """A time trigger at a moment's time of day (UTC: the conversation's time zone)."""
    return {"trigger": "time", "at": moment.strftime("%H:%M:%S")}


def porch(service="turn_on"):
    return [{"action": f"switch.{service}", "target": {"entity_id": PORCH}}]


def said(prompt):
    """The time a prompt rendered from NOW says."""
    return datetime.fromisoformat(prompt.split("\n", 1)[0].strip())


async def clock_home(hass, record, today, change=None, **kwargs):
    """A replay's sandbox in a worker of its own at the recording's first turn, its instructions saying its time
    (NOW), with a change on top: Assist's tools ready (assist)."""
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-clock", today, change={**(change or {}), "prompt": NOW}, **kwargs)
    await home.assist(turn)
    return home


async def now_in(home):
    return said((await home.assist(home.first_turn))["prompt"])


async def tool(home, called, **args):
    found = await home.sandbox.call("tool", tool_name=called, tool_args=args)
    assert not found["error"], found
    return found["data"]


async def left(home):
    """The seconds left on the asking device's timers (HA's own timer status)."""
    return [t["total_seconds_left"] for t in (await tool(home, "intent__HassTimerStatus"))["speech_slots"]["timers"]]


@pytest.mark.asyncio
async def test_triggers_the_sun_templates_and_timers_agree_with_the_virtual_clock(hass, real_worker):
    """In a sandbox started at a conversation's moment, months ago: its instructions render that time; moving on to a
    later turn's time fires a timed automation and a sunrise automation at their times on the way; the asking
    device's timer counts down by the time moved on, keeps its time while paused, takes added time, and ends."""
    record = conversation()
    sunrise = {"id": "at_sunrise", "alias": "At sunrise", "triggers": [{"trigger": "sun", "event": "sunrise"}],
               "actions": porch("turn_off")}
    automations = [{"id": "in_a_minute", "alias": "In a minute", "triggers": [at(START + timedelta(minutes=1))],
                    "actions": porch()}, sunrise]
    today = recorded_today(record, scripts={}, automations=automations)
    today["timers"] = ["fixture-phone"]
    home = await clock_home(hass, record, today, watch=["automation.in_a_minute", "automation.at_sunrise", "sun.sun"])
    try:
        began = await now_in(home)
        assert START <= began < START + timedelta(seconds=30)  # The conversation's time, never today's.
        first = (await home.snapshot())["attributes"]["sun.sun"]
        rising = datetime.fromisoformat(first["next_rising"])
        assert began < rising < began + timedelta(days=1)  # The sun on the conversation's day.

        await tool(home, "intent__HassStartTimer", minutes=10)
        await home.observe({"at": (START + timedelta(minutes=2)).isoformat()}, [])
        [running] = await left(home)
        assert 600 - 120 <= running <= 600 - 120 + 30
        world = await home.snapshot()
        fired = datetime.fromisoformat(world["attributes"]["automation.in_a_minute"]["last_triggered"])
        assert START + timedelta(minutes=1) <= fired < START + timedelta(minutes=1, seconds=1)  # At its time.
        assert world["states"][PORCH] == "on"

        await tool(home, "intent__HassPauseTimer")
        await home.observe({"at": (START + timedelta(minutes=5)).isoformat()}, [])
        assert await left(home) == [running]  # Paused: three minutes on, the same time left.
        await tool(home, "intent__HassIncreaseTimer", minutes=5)
        assert await left(home) == [running + 300]
        await tool(home, "intent__HassUnpauseTimer")
        later = max(rising, START + timedelta(minutes=20)) + timedelta(minutes=1)  # Past the timer's end and sunrise.
        await home.observe({"at": later.isoformat()}, [])
        assert await left(home) == []  # It ended on the way.
        world = await home.snapshot()
        fired = datetime.fromisoformat(world["attributes"]["automation.at_sunrise"]["last_triggered"])
        assert rising <= fired < rising + timedelta(seconds=1)
        assert world["states"][PORCH] == "off"
        assert datetime.fromisoformat(world["attributes"]["sun.sun"]["next_rising"]) > rising  # The next day's.
        assert world["states"]["sun.sun"] == "above_horizon"
        assert later <= await now_in(home) < later + timedelta(minutes=1)
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_a_scripts_delay_passes_at_once_as_the_sandbox_settles(hass, real_worker):
    """A script Assist starts waits ten minutes, then switches the porch on: the sandbox's home settles at once, the
    clock ten minutes on."""
    record = conversation()
    scripts = {"slow": {"alias": "Slow", "sequence": [{"delay": {"minutes": 10}}, *porch()]}}
    home = await clock_home(hass, record, recorded_today(record, scripts=scripts, automations=[]),
                            change={"exposure": {"script.slow": True}})
    try:
        began = await now_in(home)
        await tool(home, "intent__HassTurnOn", name="Slow")
        real = time.monotonic()
        world = await home.snapshot()
        assert world["states"][PORCH] == "on" and time.monotonic() - real < 30
        assert began + timedelta(minutes=10) <= await now_in(home) < began + timedelta(minutes=11)
    finally:
        await home.stop()


def toggling(name, *triggers, **more):
    """An automation that toggles the porch."""
    return {"id": name, "alias": name, "triggers": list(triggers), "actions": porch("toggle"), **more}


EVERY_MINUTE, HOURLY = {"trigger": "time_pattern", "minutes": "/1"}, {"trigger": "time_pattern", "minutes": 0}


@pytest.mark.asyncio
async def test_an_every_minute_automations_week_records_every_run(hass, real_worker):
    """A new automation that toggles the porch every minute, over the week after the conversation ended: each of its
    10,080 runs, a minute apart, with what it changed; no week without it to compare (it's new)."""
    from custom_components.assist_improver.evidence import automation_week
    record = conversation(end=timedelta(minutes=1))
    week = await automation_week(record, recorded_today(record, scripts={}, automations=[]),
                                 {"automations": {"every_minute": toggling("every_minute", EVERY_MINUTE)}})
    assert week["without"] is None and week["with"]["days"] == 7
    runs = week["with"]["runs"]
    assert len(runs) == 7 * 24 * 60
    ended = datetime.fromisoformat(record["last_activity"])
    first = datetime.fromisoformat(runs[0]["at"])
    assert ended < first < ended + timedelta(minutes=2)
    assert all(timedelta(seconds=59) < datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])
               < timedelta(seconds=61) for a, b in zip(runs, runs[1:]))
    assert {(r["automation"], r["id"]) for r in runs} == {("automation.every_minute", "every_minute")}
    assert [r["changed"] for r in runs[:2]] == [[{"entity_id": PORCH, "old": "off", "new": "on"}],
                                                [{"entity_id": PORCH, "old": "on", "new": "off"}]]


@pytest.mark.asyncio
async def test_an_automation_its_own_save_sets_off_runs_at_the_start_of_its_week(hass, real_worker):
    """Applying saves an automation through Home Assistant's editor, whose reload fires automation_reloaded once it's
    loaded: one triggered by that runs as it's accepted, and its week shows that run as the week starts."""
    from custom_components.assist_improver.evidence import automation_week
    record = conversation(end=timedelta(minutes=1))
    on_save = toggling("on_save", {"trigger": "event", "event_type": "automation_reloaded"})
    week = await automation_week(record, recorded_today(record, scripts={}, automations=[]),
                                 {"automations": {"on_save": on_save}})
    runs = week["with"]["runs"]
    ended = datetime.fromisoformat(record["last_activity"])
    assert [(r["id"], r["changed"]) for r in runs] == [("on_save", [{"entity_id": PORCH, "old": "off", "new": "on"}])]
    assert ended <= datetime.fromisoformat(runs[0]["at"]) < ended + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_a_self_retriggering_automations_week_stops_at_the_budget_with_the_days_it_ran(hass, real_worker):
    """An edit that makes an hourly automation toggle the porch again each time the porch changes never lets the week
    move on: it stops once its budget's real seconds are spent, and says how many whole days ran. The week without
    the edit, the household's automation as it is, runs whole."""
    from custom_components.assist_improver.evidence import automation_week
    record = conversation(end=timedelta(minutes=1))
    today = recorded_today(record, scripts={}, automations=[toggling("hourly", HOURLY)])
    looping = toggling("hourly", HOURLY, {"trigger": "state", "entity_id": PORCH}, mode="queued")
    began = time.monotonic()
    # Both weeks get the budget: the looping one spends it all; the one without takes about a second alone and five
    # with sixteen at once on sixteen cores, so it gets three times that.
    week = await automation_week(record, today, {"automations": {"hourly": looping}}, seconds=15)
    assert time.monotonic() - began < 60
    assert week["with"]["days"] < 7 and len(week["with"]["runs"]) > 100
    assert week["without"]["days"] == 7 and len(week["without"]["runs"]) == 7 * 24


@pytest.mark.asyncio
async def test_an_edits_week_is_reviewed_as_the_runs_it_adds_and_drops(hass, real_worker):
    """Kai's words: "for a change to one you have, the runs it adds and drops." An edit moving the household's evening
    porch toggle three hours later: its week with the edit and without it, side by side; its review counts the runs
    added and dropped with their times first, then each run with what it changed (weeks.week)."""
    from custom_components.assist_improver.evidence import automation_week
    from custom_components.assist_improver.weeks import reviewed, week
    record = conversation(end=timedelta(minutes=1))
    today = recorded_today(record, scripts={}, automations=[toggling("evening", {"trigger": "time", "at": "18:00:00"})])
    later = toggling("evening", {"trigger": "time", "at": "21:00:00"})
    found = await automation_week(record, today, {"automations": {"evening": later}})
    shown = week(found, {"id": "evening"})
    added, dropped = ([r for r in shown["runs"] if r["sign"] == sign] for sign in "+-")
    assert len(added) == len(dropped) == 7 and shown["days"] == 7 and shown["more"] == 0
    at = {sign: {r["at"].split()[1] for r in runs} for sign, runs in (("+", added), ("-", dropped))}
    assert len(at["+"]) == len(at["-"]) == 1 and at["+"] != at["-"]  # One time of day each, the home's.
    assert shown["result"] == f"7 runs added at {at['+'].pop()}, 7 dropped at {at['-'].pop()}"
    assert all(c["entity_id"] == PORCH for r in shown["runs"] for c in r["changed"])
    item = {"week": {"automations": [{"id": "evening", "config": later, "new": False, "week": shown}]}}
    hass.states.async_set(PORCH, "off", {"friendly_name": "Porch light"})
    review = reviewed(hass, item)[0]
    assert (review["name"], review["change"], review["when"], review["if"]) == ("evening", "edited", "At 21:00", None)
    assert review["week"]["runs"][0]["changed"] in ("Porch light: off → on", "Porch light: on → off")


async def replayed(hass, monkeypatch, record, blocks, through, scripts=None, automations=None, helpers=None,
                   timers=()):
    """Simulate's replay without the change, through that turn, of a recording whose household has today these
    scripts, automations, helpers ({domain: its configuration.yaml part}) and timer devices; the household's
    assistant, its instructions saying the time (NOW), answers with these blocks: (the replay, the requests its
    model got)."""
    from custom_components.assist_improver import replay as replay_module, simulate
    for turn in record["turns"]:
        turn["source"].update(scripts=deepcopy(scripts or {}), automations=deepcopy(automations or []))

    async def setup_now(hass):
        found = recorded_today(record, scripts=scripts or {}, automations=automations or [])
        return {**{k: found[k] for k in ("home", "tools", "services")}, "timers": list(timers),
                "helpers": {d: {"yaml": {d: conf}} for d, conf in (helpers or {}).items()}}
    monkeypatch.setattr(replay_module, "setup_now", setup_now)
    _, client = await bind_agent(hass, record, {**PROFILE, "prompt": NOW})
    client.blocks = blocks
    plan, today = await simulate.planned(hass, record)
    return await simulate.Trial(hass, plan, record, today, through=through).run(), client.requests


def call(tool_name, **args):
    return {"type": "tool_use", "id": f"call-{tool_name}", "name": tool_name, "input": args}


def relay_off(*more):
    """Turn 1 as recorded (the relay off), with more calls before it."""
    return [[*more, call("intent__HassTurnOff", name="Fixture relay", domain=["switch"])],
            [{"type": "text", "text": "Done."}]]


def system(request):
    return "".join(b["text"] for b in request["system"]) if isinstance(request["system"], list) else request["system"]


STAMP = {"stamp": {"alias": "Stamp", "sequence": [{"action": "input_datetime.set_datetime",  # When it ran.
          "target": {"entity_id": "input_datetime.stamp"}, "data": {"datetime": "{{ now().isoformat() }}"}}]}}
STAMPED = {"input_datetime": {"stamp": {"name": "Stamp", "has_date": True, "has_time": True}}}


def stamped(home):
    return datetime.fromisoformat(home["input_datetime.stamp"]["state"]).replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_two_turn_replay_keeps_turn_ones_chat_log_and_runs_turn_two_at_its_recorded_time(
        hass, monkeypatch, provider_process):
    """Turn 2 came ten minutes after turn 1, the conversation still going at home: the replay says it with turn 1 in
    its chat log (Home Assistant ends a chat session idle for five minutes: the replay's lasts, as at home), at its
    recorded time, as the household's script Assist runs then reads it; the household's automation due five minutes
    after turn 1 fires on the way, at its time, and none fires early or once the conversation moved on."""
    record = conversation(timedelta(minutes=10))
    first, second = (datetime.fromisoformat(t["source"]["at"]) for t in record["turns"][:2])
    automations = [{"id": "between", "alias": "Between", "triggers": [at(first + timedelta(minutes=5))],
                    "actions": porch()},
                   {"id": "later", "alias": "Later", "triggers": [at(second + timedelta(minutes=5))],
                    "actions": porch("turn_off")}]
    for turn in record["turns"]:
        turn["source"]["exposure"]["script.stamp"] = True
    found, requests = await replayed(hass, monkeypatch, record,
                                     [*relay_off(), [call("script__stamp")], [{"type": "text", "text": "Ok."}]],
                                     through=2, scripts=STAMP, automations=automations, helpers=STAMPED)
    assert found["status"] == "reproduced", found.get("error")
    assert first <= said(system(requests[0])) < first + timedelta(seconds=30)  # Its instructions, as of turn 1.
    history = json.dumps(requests[2]["messages"])
    assert "Toggle the fixture relay." in history and "Done." in history  # Turn 1, in turn 2's chat log.
    one, two = (step["home"] for step in found["steps"])
    assert second <= stamped(two) < second + timedelta(seconds=30)
    assert PORCH not in one and two[PORCH]["state"] == "on"
    fired = datetime.fromisoformat(two["automation.between"]["attributes"]["last_triggered"])
    assert first + timedelta(minutes=5) <= fired < first + timedelta(minutes=5, seconds=1)
    assert "automation.later" not in two


@pytest.mark.asyncio
async def test_a_wait_turn_one_set_off_passing_five_minutes_still_leaves_turn_one_in_turn_twos_chat_log(
        hass, monkeypatch, provider_process):
    """Turn 1 sets off the household's automation that waits nine minutes, then switches the porch on; turn 2 came ten
    minutes after turn 1: the wait passes as turn 1 settles, and turn 2 still has turn 1 in its chat log, the
    conversation held through the wait as through the gap (DETAILS P6.2.11)."""
    record = conversation(timedelta(minutes=10))
    automations = [{"id": "waits", "alias": "Waits", "actions": [{"delay": {"minutes": 9}}, *porch()],
                    "triggers": [{"trigger": "state", "entity_id": TARGET, "to": "off"}]}]
    found, requests = await replayed(hass, monkeypatch, record, [*relay_off(), [{"type": "text", "text": "Ok."}]],
                                     through=2, automations=automations)
    assert found["status"] == "reproduced", found.get("error")
    assert found["steps"][0]["home"][PORCH]["state"] == "on"
    history = json.dumps(requests[2]["messages"])
    assert "Toggle the fixture relay." in history and "Done." in history


@pytest.mark.asyncio
async def test_an_alarm_set_for_tomorrow_in_a_replay_of_a_turn_three_days_old_doesnt_fire_early(
        hass, monkeypatch, provider_process):
    """Three days ago, Assist ran the household's script that sets the alarm for tomorrow at 07:00, and the
    household's automation switches the porch on at the alarm's time: in the replay, the alarm is set for the day after
    the conversation, and the automation doesn't fire (that moment is days in the past by the real clock)."""
    record = conversation(timedelta(minutes=5))
    moved(record, datetime.now(UTC) - timedelta(days=3) - START)
    first = datetime.fromisoformat(record["turns"][0]["source"]["at"])
    scripts = {"set_alarm": {"alias": "Set alarm", "sequence": [{"action": "input_datetime.set_datetime",
               "target": {"entity_id": "input_datetime.alarm"},
               "data": {"datetime": "{{ (today_at('07:00') + timedelta(days=1)).isoformat() }}"}}]}}
    automations = [{"id": "wake_up", "alias": "Wake up", "actions": porch(),
                    "triggers": [{"trigger": "time", "at": "input_datetime.alarm"}]}]
    for turn in record["turns"]:
        turn["source"]["exposure"]["script.set_alarm"] = True
    found, _ = await replayed(hass, monkeypatch, record, relay_off(call("script__set_alarm")), through=1,
                              scripts=scripts, automations=automations,
                              helpers={"input_datetime": {"alarm": {"name": "Alarm", "has_date": True,
                                                                    "has_time": True}}})
    assert found["status"] == "reproduced", found.get("error")
    [home] = [step["home"] for step in found["steps"]]
    assert home["input_datetime.alarm"]["state"] == f"{(first + timedelta(days=1)).date()} 07:00:00"
    assert PORCH not in home and "automation.wake_up" not in home


@pytest.mark.asyncio
async def test_a_ten_minute_script_delay_settles_in_a_replay(hass, monkeypatch, provider_process):
    """Assist runs the household's script that waits ten minutes, then switches the porch on, and waits for it, as
    at home; turn 2 came a quarter of an hour later: the replay's turn finishes at once, the porch on."""
    record = conversation(timedelta(minutes=15))
    scripts = {"slow": {"alias": "Slow", "sequence": [{"delay": {"minutes": 10}}, *porch()]}}
    for turn in record["turns"]:
        turn["source"]["exposure"]["script.slow"] = True
    began = time.monotonic()
    found, _ = await replayed(hass, monkeypatch, record, relay_off(call("script__slow")), through=1, scripts=scripts)
    assert time.monotonic() - began < 60 and found["status"] == "reproduced", found.get("error")
    assert found["steps"][0]["home"][PORCH]["state"] == "on"


@pytest.mark.asyncio
async def test_a_command_for_later_stays_held_in_a_replay(hass, monkeypatch, provider_process):
    """Assist sets a command to run in five minutes, as Home Assistant offers it (2026.12's schedule command tool, a
    timer's command before): the replay logs it as a call beyond the home, and when its time comes on the way to
    turn 2, nothing runs it."""
    try:
        from homeassistant.components.intent.llm import ScheduleCommandTool
        later = call(ScheduleCommandTool.name, command="Turn on the fixture porch", minutes=5)
    except ImportError:
        later = call("intent__HassStartTimer", minutes=5, conversation_command="Turn on the fixture porch")
    record = conversation(timedelta(minutes=10))
    found, _ = await replayed(hass, monkeypatch, record, [*relay_off(later), [{"type": "text", "text": "Ok."}]],
                              through=2, timers=["fixture-phone"])
    assert found["status"] == "reproduced", found.get("error")
    one, two = found["steps"]
    assert one["outside"] == [{"action": "timer command", "answer": "not simulated",
                               "data": {"command": "Turn on the fixture porch"}}]
    assert PORCH not in two["home"]
