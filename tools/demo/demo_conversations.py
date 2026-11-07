"""The demo household's made-up Assist conversations, stored as Assist Improver's recorder stores them (demo.py seed).

Each is built on a real recording of the demo's own assistant (the demo home's snapshot, Assist's tools and prompt),
with made-up turns: what was said, Assist's tool calls with Home Assistant's results (a failed step where a name
didn't match), and the changes they made. The last run took in the ones the household picked: three led to the
seeded improvements, one found no fix, and one had no trouble after all. The others wait for the next run. Times are
relative to the seeding.

The analyses are what code derives from the recordings (analyzer.to_analysis) with the goals an analyzer would give;
where each conversation stands is its record in the improver's flow (demo.py seed).
"""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custom_components.assist_improver import analyzer  # noqa: E402
from custom_components.assist_improver.cases import digest, recording_turns, transcript  # noqa: E402
from custom_components.assist_improver.const import VERSION  # noqa: E402
from custom_components.assist_improver.exploration import SessionView  # noqa: E402
from custom_components.assist_improver.retention import pack  # noqa: E402

LIVING, KITCHEN = "conversation.living_room_assistant", "conversation.kitchen_assistant"
LIVE = "Live Context: An overview of the areas and the devices in this smart home:"


def ok(*targets):
    """An intent's result when it acted: (entity_id, name) of each device."""
    return {"speech": {}, "response_type": "action_done",
            "data": {"success": [{"name": name, "type": "entity", "id": entity} for entity, name in targets],
                     "failed": []}}


def no_match(text):
    """Home Assistant's error when no device matched (a failed step)."""
    return {"error": "MatchFailedError", "error_text": text}


def call(tool, args, result, *changes):
    """A tool call, its result, and the changes it made: (entity_id, state, attributes)."""
    return {"tool": tool, "args": args, "result": result, "changes": list(changes)}


# Each: its key, how long ago it started, the assistant, the devices' states before it, its turns (what was said,
# Assist's calls, its reply), and where it stands: up for review (an improvement covers it, its goals as an analyzer
# gives them), nofix (no change helped: settled as No fix found), fine (analysed: nothing to fix), or new (no run has
# taken it in); and the run that took it in.
CONVERSATIONS = [
    {"key": "kitchen-lights", "ago": timedelta(hours=21, minutes=20), "assistant": KITCHEN, "standing": "new",
     "before": {"light.kitchen_lights": ("off", {})},
     "turns": [("Kitchen lights on, please.",
                [call("intent__HassTurnOn", {"name": "Kitchen Lights", "domain": ["light"]},
                      ok(("light.kitchen_lights", "Kitchen Lights")), ("light.kitchen_lights", "on", {}))],
                "The kitchen lights are on.")]},
    {"key": "bedroom-fan", "ago": timedelta(hours=2, minutes=12), "assistant": LIVING, "standing": "new",
     "before": {"fan.ceiling_fan": ("off", {})},
     "turns": [("Turn on the bedroom fan.",
                [call("intent__HassTurnOn", {"name": "bedroom fan", "domain": ["fan"]},
                      no_match("No entities matched the name (given name 'bedroom fan', domains fan)"))],
                "I couldn't find a bedroom fan. Do you mean the ceiling fan?"),
               ("Yes, the ceiling fan.",
                [call("intent__HassTurnOn", {"name": "Ceiling Fan", "domain": ["fan"]},
                      ok(("fan.ceiling_fan", "Ceiling Fan")), ("fan.ceiling_fan", "on", {}))],
                "The ceiling fan is on.")]},
    {"key": "warmer", "ago": timedelta(hours=5, minutes=41), "assistant": LIVING, "standing": "new",
     "before": {},
     "turns": [("Make it a bit warmer in here.", [],
                "Which thermostat should I change: Ecobee, HeatPump or Hvac?"),
               ("The heat pump. It's the only one we use.",
                [call("climate__HassClimateSetTemperature", {"name": "HeatPump", "temperature": 22},
                      ok(("climate.heatpump", "HeatPump")), ("climate.heatpump", None, {"temperature": 22}))],
                "The heat pump is set to 22 degrees.")]},
    {"key": "lights-except-bedroom", "ago": timedelta(hours=17, minutes=34), "assistant": LIVING,
     "standing": "review", "run": "picked",
     "before": {"light.living_room_rgbww_lights": ("on", {}), "light.kitchen_lights": ("on", {}),
                "light.office_rgbw_lights": ("on", {}), "light.bed_light": ("on", {}), "switch.ac": ("on", {}),
                "media_player.living_room": ("on", {})},
     "turns": [("Turn off all the lights except the bedroom.",
                [call("intent__HassTurnOff", {"area": "Living Room"},
                      {"speech": {}, "response_type": "action_done", "data": {"success": [
                          {"name": "Living Room", "type": "area", "id": "living_room"},
                          {"name": "Living Room RGBWW Lights", "type": "entity",
                           "id": "light.living_room_rgbww_lights"},
                          {"name": "Living Room", "type": "entity", "id": "media_player.living_room"},
                          {"name": "AC", "type": "entity", "id": "switch.ac"}], "failed": []}},
                      ("light.living_room_rgbww_lights", "off", {}), ("media_player.living_room", "off", {}),
                      ("switch.ac", "off", {})),
                 call("intent__HassTurnOff", {"area": "Kitchen"},
                      {"speech": {}, "response_type": "action_done", "data": {"success": [
                          {"name": "Kitchen", "type": "area", "id": "kitchen"},
                          {"name": "Kitchen Lights", "type": "entity", "id": "light.kitchen_lights"}], "failed": []}},
                      ("light.kitchen_lights", "off", {})),
                 call("intent__HassTurnOff", {"name": "Office RGBW Lights", "domain": ["light"]},
                      ok(("light.office_rgbw_lights", "Office RGBW Lights")), ("light.office_rgbw_lights", "off", {}))],
                "Done, I turned off the living room, kitchen and office."),
               ("Why is the AC off? Turn it back on, and the TV.",
                [call("intent__HassTurnOn", {"name": "AC"}, ok(("switch.ac", "AC")), ("switch.ac", "on", {})),
                 call("intent__HassTurnOn", {"name": "Living Room", "domain": ["media_player"]},
                      ok(("media_player.living_room", "Living Room")), ("media_player.living_room", "on", {}))],
                "Sorry, the AC and the TV are back on.")],
     "goals": [{"turn": 1, "summary": "Living room lights off", "kind": "state",
                "entity_id": "light.living_room_rgbww_lights", "state": "off"},
               {"turn": 1, "summary": "Kitchen lights off", "kind": "state", "entity_id": "light.kitchen_lights",
                "state": "off"},
               {"turn": 1, "summary": "Office lights off", "kind": "state", "entity_id": "light.office_rgbw_lights",
                "state": "off"},
               {"turn": 2, "summary": "AC back on", "kind": "state", "entity_id": "switch.ac", "state": "on",
                "serves": 1},
               {"turn": 2, "summary": "Living room TV back on", "kind": "state",
                "entity_id": "media_player.living_room", "state": "on", "serves": 1}]},
    {"key": "garage-closed", "ago": timedelta(days=2, hours=17, minutes=38), "assistant": KITCHEN, "standing": "new",
     "before": {"cover.garage_door": ("closed", {})},
     "turns": [("Is the garage door closed?",
                [call("homeassistant__GetLiveContext", {"name": "Garage Door"},
                      {"result": f"{LIVE}\n- names: Garage Door\n  domain: cover\n  state: closed\n"})],
                "Yes, the garage door is closed.")]},
    {"key": "movie", "ago": timedelta(days=1, hours=20, minutes=53), "assistant": LIVING, "standing": "review",
     "run": "picked",
     "before": {"light.living_room_rgbww_lights": ("on", {"brightness": 255}), "cover.living_room_window": ("open", {}),
                "media_player.living_room": ("off", {})},
     "turns": [("Dim the living room lights.",
                [call("light__HassLightSet", {"name": "Living Room RGBWW Lights", "brightness": 10},
                      ok(("light.living_room_rgbww_lights", "Living Room RGBWW Lights")),
                      ("light.living_room_rgbww_lights", "on", {"brightness": 26}))],
                "Done."),
               ("Close the living room blinds too.",
                [call("intent__HassTurnOff", {"name": "Living Room Window", "domain": ["cover"]},
                      ok(("cover.living_room_window", "Living Room Window")),
                      ("cover.living_room_window", "closed", {}))],
                "Done."),
               ("And turn on the TV.",
                [call("intent__HassTurnOn", {"name": "Living Room", "domain": ["media_player"]},
                      ok(("media_player.living_room", "Living Room")), ("media_player.living_room", "on", {}))],
                "The living room TV is on.")],
     "goals": [{"turn": 1, "summary": "Living room lights dimmed", "kind": "state",
                "entity_id": "light.living_room_rgbww_lights", "state": "on", "brightness_pct": 10},
               {"turn": 2, "summary": "Living room blinds closed", "kind": "state",
                "entity_id": "cover.living_room_window", "state": "closed", "serves": 1},
               {"turn": 3, "summary": "Living room TV on", "kind": "state", "entity_id": "media_player.living_room",
                "state": "on", "serves": 1}]},
    {"key": "office", "ago": timedelta(days=3, hours=3, minutes=20), "assistant": LIVING, "standing": "fine",
     "run": "picked",
     "before": {"light.office_rgbw_lights": ("off", {}), "switch.ac": ("off", {})},
     "turns": [("Turn on the office lights.",
                [call("intent__HassTurnOn", {"name": "Office RGBW Lights", "domain": ["light"]},
                      ok(("light.office_rgbw_lights", "Office RGBW Lights")), ("light.office_rgbw_lights", "on", {}))],
                "The office lights are on."),
               ("And the AC too, please.",
                [call("intent__HassTurnOn", {"name": "AC"}, ok(("switch.ac", "AC")), ("switch.ac", "on", {}))],
                "The AC is on.")],
     "goals": [{"turn": 1, "summary": "Office lights on", "kind": "state", "entity_id": "light.office_rgbw_lights",
                "state": "on"},
               {"turn": 2, "summary": "AC on", "kind": "state", "entity_id": "switch.ac", "state": "on"}]},
    {"key": "which-tv", "ago": timedelta(days=4, hours=2, minutes=41), "assistant": LIVING, "standing": "nofix",
     "run": "picked",
     "before": {"media_player.bedroom": ("playing", {})},
     "turns": [("Turn off the TV.", [],
                "Which TV do you mean: Living Room, Bedroom, Kitchen or Lounge room?"),
               ("The one in the bedroom.",
                [call("intent__HassTurnOff", {"name": "Bedroom", "domain": ["media_player"]},
                      ok(("media_player.bedroom", "Bedroom")), ("media_player.bedroom", "off", {}))],
                "The bedroom TV is off.")],
     "goals": [{"turn": 1, "summary": "Bedroom TV off", "kind": "state", "entity_id": "media_player.bedroom",
                "state": "off"}]},
    {"key": "garage", "ago": timedelta(days=3, hours=22, minutes=17), "assistant": LIVING, "standing": "review",
     "run": "picked",
     "before": {"cover.garage_door": ("closed", {})},
     "turns": [("Open the garage.",
                [call("intent__HassTurnOn", {"name": "garage", "domain": ["cover"]},
                      no_match("No entities matched the name (given name 'garage', domains cover)"))],
                "I couldn't find a device called garage. Which one do you mean?"),
               ("The garage door.",
                [call("intent__HassTurnOn", {"name": "Garage Door", "domain": ["cover"]},
                      ok(("cover.garage_door", "Garage Door")), ("cover.garage_door", "open", {}))],
                "The garage door is opening.")],
     "goals": [{"turn": 1, "summary": "Garage door open", "kind": "state", "entity_id": "cover.garage_door",
                "state": "open"}]},
]
# The runs that took them in, with the improvements each proposed: one, on the conversations the household picked
# (the others stayed new).
RUNS = {"picked": {"ago": timedelta(hours=9), "picked": True,
                   "improvements": ["demo-lights-only", "demo-garage", "demo-movie"]}}


def _id(*parts):
    return sha256(":".join(map(str, parts)).encode()).hexdigest()


def _ulid(*parts):
    """A ULID-shaped ID (Home Assistant's conversation and context IDs), the same for the same parts."""
    alphabet, n = "0123456789ABCDEFGHJKMNPQRSTVWXYZ", int(_id(*parts), 16)
    return "01K" + "".join(alphabet[(n >> (5 * i)) & 31] for i in range(23))


def _iso(at):
    return at.isoformat()


def _state(record, state, attributes, at, context):
    out = deepcopy(record)
    if state is not None:
        out["state"] = state
    out["attributes"] = {**out.get("attributes", {}), **attributes}
    out.update(last_updated=_iso(at), context=context)
    if state is not None and state != record.get("state"):
        out["last_changed"] = _iso(at)
    return out


def _system(template, at, zone):
    """The assistant's instructions as Home Assistant wrote them for a turn at this time."""
    local = at.astimezone(zone)
    return re.sub(r"Current time is \d\d:\d\d:\d\d\. Today's date is \d{4}-\d\d-\d\d\.",
                  f"Current time is {local:%H:%M:%S}. Today's date is {local:%Y-%m-%d}.", template)


def recording(spec, now, template, user_id, zone):
    """One made-up conversation, as the recorder keeps it once it has ended (packed), and its index summary."""
    key, cid, agent = _id("demo-conversation", spec["key"]), _ulid("conversation", spec["key"]), spec["assistant"]
    started = (now - spec["ago"]).replace(second=7, microsecond=0)
    base = deepcopy(template["source"])
    states = base["states"]
    for entity, (state, attributes) in spec["before"].items():
        states[entity] = _state(states[entity], state, attributes, started - timedelta(hours=3),
                                {"id": _ulid("before", spec["key"], entity), "parent_id": None, "user_id": None})
    system_text = template["system"]
    messages, turns, changes, events = [], [], [], []
    clock = started

    def tick(seconds):
        nonlocal clock
        clock += timedelta(seconds=seconds)
        return _iso(clock)

    events.append({"at": _iso(started), "type": "created", "data": {"chat_log": {
        "conversation_id": cid, "created": _iso(started), "continue_conversation": False,
        "content": [{"role": "system", "content": "", "created": _iso(started)}]}}})
    for number, (said, calls, reply) in enumerate(spec["turns"], 1):
        if number > 1:
            tick(14 + 3 * number)
        context = {"id": _ulid("context", spec["key"], number), "parent_id": None, "user_id": user_id}
        user = {"role": "user", "content": said, "created": _iso(clock)}
        messages.append(user)
        events.append({"at": user["created"], "type": "content_added", "data": {"content": user}})
        system = {"role": "system", "content": _system(system_text, clock, zone), "created": _iso(started)}
        prepared = deepcopy(template["prepared"])
        prepared["context"]["context"] = context
        source = deepcopy(base)
        source["at"] = user["created"]
        turns.append({"user": deepcopy(user), "source": source, "prepared": prepared,
                      "preparations": [{"conversation_id": cid, "created": _iso(started),
                                        "continue_conversation": False, "content": [system, *deepcopy(messages)]}]})
        events.append({"at": tick(1), "type": "updated", "data": {"chat_log": deepcopy(turns[-1]["preparations"][0])}})
        if calls:
            tool_calls = [{"tool_name": c["tool"], "tool_args": c["args"],
                           "id": f"call_{_id(spec['key'], number, n)[:24]}", "external": False}
                          for n, c in enumerate(calls)]
            asked = {"role": "assistant", "agent_id": agent, "created": tick(2), "tool_calls": tool_calls}
            messages.append(asked)
            events.append({"at": asked["created"], "type": "content_added", "data": {"content": asked}})
            for c, tool_call in zip(calls, tool_calls):
                error = "error" in c["result"]
                result = {"role": "tool_result", "agent_id": agent, "tool_call_id": tool_call["id"],
                          "tool_name": c["tool"], "result": {"data": c["result"], "error": error},
                          "tool_result": c["result"], "created": tick(1)}
                messages.append(result)
                events.append({"at": result["created"], "type": "content_added", "data": {"content": result}})
                for entity, state, attributes in c["changes"]:
                    old = states[entity]
                    new = _state(old, state, attributes, clock, context)
                    changes.append({"at": _iso(clock), "entity_id": entity, "old": old, "new": new})
                    states[entity] = new
        final = {"role": "assistant", "agent_id": agent, "created": tick(2), "content": reply}
        messages.append(final)
        events.append({"at": final["created"], "type": "content_added", "data": {"content": final}})
    ended = clock + timedelta(minutes=5)  # Home Assistant ends a conversation after five quiet minutes.
    events.append({"at": _iso(ended), "type": "deleted", "data": {}})
    last_system = {"role": "system", "content": _system(system_text, clock, zone), "created": _iso(started)}
    record = {"format": 1, "id": key, "conversation_id": cid, "started": _iso(started),
              "core_version": template["core_version"], "integration_version": VERSION, "status": "session_expired",
              "last_activity": _iso(ended), "events": events, "turns": turns, "state_changes": changes,
              "limitations": [], "chat_log": {"conversation_id": cid, "created": _iso(started),
                                              "continue_conversation": False, "content": [last_system, *messages]}}
    recording_turns(deepcopy(record))  # Replayable, as a recorded one is: raises if not.
    summary = {"id": key, "started": record["started"], "updated": _iso(ended), "status": "session_expired",
               "user_turns": len(turns), "stored": "packed"}
    return record, pack(record), summary


def analyses(made, run, run_id, finished):
    """The run log's records of a run's analyses: each conversation it took in, read by code as the analyzer's answer
    would be."""
    out = []
    for spec, record in made:
        if spec.get("run") != run:
            continue
        view = SessionView(deepcopy(record))
        answer = {"goals": spec["goals"], "not_requests": [], "question_for_user": ""}
        out.append({"id": _id("analysis", spec["key"]), "kind": analyzer.ROLE, "role": analyzer.ROLE,
                    "recording_id": record["id"], "status": "completed", "digest_sha256": digest(view.digest),
                    "entity_id": "ai_task.demo_large", "agentic": True, "run_id": run_id, "trace_id": run_id,
                    "started": _iso(finished - timedelta(minutes=2)), "finished": _iso(finished - timedelta(minutes=1)),
                    "seconds": 41.0, "output": answer, "answer_status": "accepted", "submissions": 1,
                    "analysis": {**analyzer.to_analysis(record["id"], answer, view), "problems": []}})
    return out
