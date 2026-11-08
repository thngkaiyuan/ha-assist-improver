"""Compact, model-readable digest of one recording; deterministic, no HA access.

Raw recordings carry full state and registry snapshots per turn (megabytes).
The journey analyzer needs the conversation, what the assistant did and saw,
what actually changed, and what Assist could and could not do. Nothing here
infers intent; it only selects and shortens recorded observations.
"""

from copy import deepcopy
from datetime import datetime
import json
import re

from homeassistant.components.sensor import NON_NUMERIC_DEVICE_CLASSES
from homeassistant.util import dt as dt_util

from .cases import CaseError, recording_turns

FORMAT = 1
RESULT_CHARS = 6000  # A tool result's characters a view shows, for size; what's cut is counted where it's cut (_clip).
READINGS_SHOWN = 25  # Entities whose readings a view lists upfront, those with the most first; the rest are named.
# Every change of these is a reading, as Home Assistant's logbook tells one (its ALWAYS_CONTINUOUS_DOMAINS).
READING_DOMAINS = {"counter", "image", "proximity"}
ENTITY_ID = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")
LABELS = ("entity_category", "disabled_by", "hidden_by")  # What a card labels an entity with, where it's set.
NO_STATE = "no live state (disabled or missing)"


def caused(change, requests=()):
    """Whether a person or Assist caused a change: its context has a user or a parent (what retention.involved
    counts), or is one of the conversation's requests (requests: their context IDs, which simulation matches)."""
    context = (change.get("new") or {}).get("context") or {}
    return bool(context.get("user_id") or context.get("parent_id") or (context.get("id") and context["id"] in requests))


def kept(change, requests=(), machinery=(), cut=()):
    """The keep rule (DETAILS P8.2.9), one for the recorder, the sandbox and every reader: how a state change is kept.
    None: cut, only for the entities in cut (the improver's own AI Tasks, P8.1.4). "machinery": one of the answering
    voice assistant's own entities (machinery), which change with every turn and are never what a request did.
    "reading": one nobody caused (caused) that leaves the state as it was, or belongs to a counter, proximity or image
    entity, or to a sensor with a unit, a state class or a numeric device class, as Home Assistant's logbook tells a
    reading. "change": any other. Readings are kept with the rest; the views list them last."""
    entity_id = change["entity_id"]
    if entity_id in cut:
        return None
    if entity_id in machinery:
        return "machinery"
    if caused(change, requests):
        return "change"
    old, new = change.get("old") or {}, change.get("new") or {}
    attributes, domain = new.get("attributes") or old.get("attributes") or {}, entity_id.split(".", 1)[0]
    if (old and new and old.get("state") == new.get("state")) or domain in READING_DOMAINS or domain == "sensor" and (
            "unit_of_measurement" in attributes or "state_class" in attributes
            or attributes.get("device_class") not in (None, *NON_NUMERIC_DEVICE_CLASSES)):
        return "reading"
    return "change"


def requests(recording):
    """The context IDs of a conversation's requests, which the changes Assist makes for them carry."""
    found = {(((t.get("prepared") or {}).get("context") or {}).get("context") or {}).get("id")
             for t in recording.get("turns", [])}
    found.discard(None)
    return found


def machinery(recording, pipeline=None):
    """The answering voice assistant's own entities in a recording (DETAILS P8.2.9): the conversation agent that
    answered, as its replies and each turn's request name it; the agent, speech-to-text, text-to-speech and wake word
    entities each turn's voice assistant (pipeline) names, or pipeline's, a replay's (today's, replay._pipeline_now);
    and the assist_satellite entity of the device each request came through. A voice device's other entities (its
    speaker) are the home's."""
    found, turns = set(), recording.get("turns", [])
    found.update(c["agent_id"] for c in (recording.get("chat_log") or {}).get("content") or []
                 if isinstance(c, dict) and isinstance(c.get("agent_id"), str))
    entities = (turns[0].get("source") or {}).get("entities") or {} if turns else {}
    for turn in turns:
        context, ran = (turn.get("prepared") or {}).get("context") or {}, pipeline or turn.get("pipeline") or {}
        found.update(v for v in (context.get("agent_id"), *(ran.get(k) for k in (
            "conversation_engine", "stt_engine", "tts_engine", "wake_word_entity"))) if isinstance(v, str))
        if device := context.get("device_id"):
            found.update(e for e, entry in entities.items()
                         if e.startswith("assist_satellite.") and (entry or {}).get("device_id") == device)
    return found


def labelled(recording):
    """A recording's changes, each with its label (kept), but the machinery's: what its readers read."""
    asked, gears = requests(recording), machinery(recording)
    return [(c, label) for c in recording.get("state_changes", []) if (label := kept(c, asked, gears)) != "machinery"]


def readings(found, shown=READINGS_SHOWN):
    """Readings as a view lists them, after everything else: found, (entity_id, reading) in time order, as one line
    per entity, {entity_id, readings: how many, first, last}, those with the most first, up to shown; and the other
    entities with readings, by ID, those with the most first (each one's line is in its device details)."""
    by_entity = {}
    for entity_id, reading in found:
        by_entity.setdefault(entity_id, []).append(reading)
    ranked = sorted(by_entity.items(), key=lambda item: -len(item[1]))
    return ([{"entity_id": e, "readings": len(rs), "first": rs[0], "last": rs[-1]} for e, rs in ranked[:shown]],
            [e for e, _ in ranked[shown:]])


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _clip(text, limit=RESULT_CHARS):
    return text if len(text) <= limit else text[:limit] + f" …[{len(text) - limit} more characters omitted]"


def _naming(source, entity_id):
    entity = source.get("entities", {}).get(entity_id, {})
    device = source.get("devices", {}).get(entity.get("device_id") or "", {}) or {}
    state = source.get("states", {}).get(entity_id, {})
    area_id = entity.get("area_id") or device.get("area_id")
    area = source.get("areas", {}).get(area_id or "", {}).get("name")
    name = state.get("attributes", {}).get("friendly_name") or entity.get("name") or device.get("name") or entity_id
    return name, area, [a for a in entity.get("aliases") or [] if a and a != name]


def time_zone(name):
    """The time zone a recording names (Home Assistant's then), or UTC for none it knows. Home Assistant may read a
    zone from disk the first time, so code on the event loop resolves it with async_time_zone and passes it in."""
    return dt_util.get_time_zone(name or "UTC") or dt_util.UTC


async def async_time_zone(name):
    """time_zone, resolved off the event loop."""
    return await dt_util.async_get_time_zone(name or "UTC") or dt_util.UTC


def filled(record):
    """A record with its empty fields left out (None, "", [] and {})."""
    return {k: v for k, v in (record or {}).items() if v not in (None, "", [], {})}


def device_of(source, device_id):
    """A device's registry entry as recorded, its empty fields left out; a child device takes its manufacturer, model
    and area from its parent where it has none of its own (Home Assistant keeps them on the parent)."""
    devices = source.get("devices") or {}
    device = devices.get(device_id or "") or {}
    parent = devices.get(device.get("parent_device_id") or "") or {}
    return filled({**{k: parent.get(k) for k in ("manufacturer", "model", "model_id", "area_id")}, **filled(device)})


def entity_card(source, entity_id, detailed=False, operations=None):
    """One entity as the views show it: its name, area, aliases, whether Assist may use it (exposed) and its labels
    (entity category, disabled, hidden) where set, then its state. detailed: the state whole, as Home Assistant writes
    it, and the entity's and its device's registry entries, each with its empty fields left out; and operations, what
    it can do (actions.operations), where given."""
    name, area, aliases = _naming(source, entity_id)
    entry = source.get("entities", {}).get(entity_id) or {}
    state = source.get("states", {}).get(entity_id)
    card = {"entity_id": entity_id, "name": name, **({"area": area} if area else {}),
            **({"aliases": aliases} if aliases else {}), "exposed": source.get("exposure", {}).get(entity_id),
            **{k: entry[k] for k in LABELS if entry.get(k)}}
    if not detailed:
        card["state"] = state.get("state") if state else NO_STATE
        return card
    card["state"] = filled(state) if state else NO_STATE
    if entry:
        card["entity"] = filled({k: v for k, v in entry.items() if k != "entity_id"})
    if device := device_of(source, entry.get("device_id")):
        card["device"] = device
    if operations is not None:
        card["operations"] = operations
    return card


def _named(actions):
    """The entities a turn's recorded action calls name (P8.2.10): their entity_id, given alone or as a list."""
    found = set()
    for action in actions:
        ids = (action.get("data") or {}).get("entity_id")
        found.update(i for i in (ids if isinstance(ids, list) else [ids]) if isinstance(i, str))
    return found


def build_digest(recording):
    """Return the digest for a closed recording. Raises CaseError when unusable."""
    turns = recording_turns(recording, replay=False)
    first = turns[0]["source"]
    starts = [t["messages"][0]["created"] for t in turns]

    def turn_of(at):
        number = 0
        for index, start in enumerate(starts, 1):
            if datetime.fromisoformat(at) >= datetime.fromisoformat(start):
                number = index
        return number

    out_turns, mentioned = [], {}  # mentioned: {entity: the first turn that names it}
    for turn in turns:
        steps, named = [], _named(turn["actions"] or [])
        for message in turn["messages"][1:]:
            if message["role"] == "assistant":
                if message.get("content"):
                    steps.append({"assistant_says": message["content"]})
                for call in message.get("tool_calls") or []:
                    step = {"calls": call["tool_name"], "args": call["tool_args"]}
                    if call.get("external"):
                        step["provider_hosted"] = True
                    steps.append(step)
                    named.update(ENTITY_ID.findall(_text(call["tool_args"])))
            elif message["role"] == "tool_result":
                result = _text(message.get("result"))
                named.update(ENTITY_ID.findall(result))  # The devices Home Assistant's response names, too.
                steps.append({"result_of": message.get("tool_name"), "result": _clip(result)})
        for entity_id in named:
            mentioned.setdefault(entity_id, turn["number"])
        out_turns.append({"turn": turn["number"], "at": turn["messages"][0]["created"],
                          "user_says": turn["messages"][0]["content"], "assistant": steps,
                          **({"handled_by": "Home Assistant itself, without the AI model"}
                             if turn["handled_locally"] else {})})

    changes, found = {}, []  # found: the readings, (entity_id, reading), listed last.
    for change, label in labelled(recording):
        before, after = change.get("old") or {}, change.get("new") or {}
        old, new = before.get("state"), after.get("state")
        line = {"at": change["at"], "during_turn": turn_of(change["at"]), "old": old, "new": new}
        if label == "reading":
            found.append((change["entity_id"], line))
            continue
        if old == new:  # Only its attributes moved (a light's brightness): which.
            was, now = before.get("attributes") or {}, after.get("attributes") or {}
            line["attributes_changed"] = sorted(k for k in {*was, *now} if was.get(k) != now.get(k))
        changes.setdefault(change["entity_id"], []).append(line)
    shown, more = readings(found)

    known = set(first.get("states", {})) | set(first.get("entities", {}))
    for entity_id, lines in changes.items():
        mentioned[entity_id] = min(mentioned.get(entity_id, len(turns)), max(1, lines[0]["during_turn"]))
    relevant_cards = []
    for entity_id in sorted(e for e in mentioned if e in known or e in changes):
        # As the turn it first comes up in started: that turn's states, the first turn's registries.
        at = {**first, "states": turns[min(mentioned[entity_id], len(turns)) - 1]["source"].get("states") or {}}
        card = entity_card(at, entity_id, detailed=True)
        card["changes_during_session"] = changes.get(entity_id, [])
        relevant_cards.append(card)

    # Only what Assist could see, by area: every other entity is within the views' search (exploration.py).
    exposed = sorted(e for e, v in first.get("exposure", {}).items() if v is True)
    by_area = {}
    for entity_id in exposed:
        card = entity_card(first, entity_id)
        by_area.setdefault(card.pop("area", None) or "No area", []).append(card)

    return {
        "format": FORMAT,
        "recording_id": recording["id"],
        "home": first.get("home_name"),
        "time_zone": first.get("time_zone"),
        "started": starts[0],
        "turns": out_turns,
        "relevant_entities": relevant_cards,
        "capabilities": {
            "assist_tools": [{"name": t["name"], "description": t.get("description")}
                             for t in next((t["prepared"]["tools"] for t in turns if t["prepared"]), [])],
            "exposed_entities_by_area": by_area,
        },
        "omitted": {
            "note": "Full state and registry snapshots stay in the recording; this digest is a selection.",
        },
        "limitations": deepcopy(recording.get("limitations", [])),
        "readings": shown,  # Last: what devices reported on their own (kept).
        **({"more_entities_with_readings": more} if more else {}),
    }


def label_coverage(digest, label):
    """Entities and turns a label depends on that the digest lacks; empty means covered. A label's note (what the
    conversation was sent in with) is its last turn: the analyzer gets it with the conversation, not in the digest."""
    turns = {t["turn"] for t in digest["turns"]} | ({len(digest["turns"]) + 1} if label.get("note") else set())
    present = {c["entity_id"] for c in digest["relevant_entities"]}
    for cards in digest["capabilities"]["exposed_entities_by_area"].values():
        present |= {c["entity_id"] for c in cards}
    missing = []
    for goal in label.get("goals", []) + label.get("optional_goals", []) + label.get("sub_goals", []):
        if goal["turn"] not in turns:
            missing.append(f"turn {goal['turn']}")
        if goal.get("entity_id") and goal["entity_id"] not in present:
            missing.append(goal["entity_id"])
    for item in label.get("not_requests", []):
        if item["turn"] not in turns:
            missing.append(f"turn {item['turn']}")
    if label.get("recording_id") != digest["recording_id"]:
        raise CaseError("Label belongs to another recording")
    return missing
