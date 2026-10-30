"""Compact, model-readable digest of one recording; deterministic, no HA access.

Raw recordings carry full state and registry snapshots per turn (megabytes).
The journey analyzer needs the conversation, what the assistant did and saw,
what actually changed, and what Assist could and could not do. Nothing here
infers intent; it only selects and shortens recorded observations.
"""

from contextlib import suppress
from copy import deepcopy
from datetime import datetime
import json
import re

from homeassistant.util import dt as dt_util

from .cases import CaseError, recording_turns

FORMAT = 1
RESULT_CHARS = 6000
# Domains whose changes are background telemetry, not journey outcomes.
NOISE_DOMAINS = {"sensor", "binary_sensor", "device_tracker", "event", "update", "tts", "conversation",
                 "sun", "weather", "number", "image", "camera", "button", "stt", "wake_word", "assist_satellite"}
ACTION_DOMAINS = {"script", "scene", "automation"}
ENTITY_ID = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")
OP_ATTRIBUTES = ("hvac_modes", "fan_modes", "preset_modes", "swing_modes", "effect_list", "options",
                 "supported_color_modes", "source_list")
# What a device reports of the world on its own, which no command sets: its readings, what it's playing and what it
# can do now; and its name, which a rename changes.
OWN_REPORTS = {"current_temperature", "hvac_action", "media_title", "media_artist", "media_album_name",
               "supported_features", "friendly_name"}


def _states(change):
    return (change.get("old") or {}).get("state"), (change.get("new") or {}).get("state")


def background(change):
    """Whether a recorded state change is background, which the readers listing what changed skip: a noise domain's,
    an availability flap (to or from unavailable), or a write that changed neither the state nor an attribute someone
    sets (a TV's next song, a thermostat's reading). A correction of an attribute alone (a volume, a brightness)
    counts."""
    old, new = change.get("old") or {}, change.get("new") or {}
    if change["entity_id"].split(".", 1)[0] in NOISE_DOMAINS or "unavailable" in (old.get("state"), new.get("state")):
        return True
    if old.get("state") != new.get("state"):
        return False
    before, after = old.get("attributes") or {}, new.get("attributes") or {}
    return all(before.get(k) == after.get(k) for k in (before.keys() | after.keys()) - OWN_REPORTS)


def flap(change):
    """An availability flap the digest counts: a device (not background telemetry) going to or from unavailable."""
    old, new = _states(change)
    return old != new and "unavailable" in (old, new) and change["entity_id"].split(".", 1)[0] not in NOISE_DOMAINS


def caused(change, requests=()):
    """Whether a person or Assist caused a change: its context has a user or a parent (what retention.involved
    counts), or is one of the conversation's requests (requests: their context IDs, which simulation matches)."""
    context = (change.get("new") or {}).get("context") or {}
    return bool(context.get("user_id") or context.get("parent_id") or (context.get("id") and context["id"] in requests))


def worth_keeping(change, requests=()):
    """Whether the recorder keeps a state change: one some reader uses, so background only when someone caused it."""
    return caused(change, requests) or not background(change)


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


def _ops(entity_id, state):
    attributes = state.get("attributes", {})
    ops = {key: attributes[key] for key in OP_ATTRIBUTES if attributes.get(key)}
    if entity_id.startswith("light.") and set(attributes.get("supported_color_modes") or []) - {"onoff"}:
        ops["brightness"] = True
    if "temperature" in attributes:
        ops["target_temperature"] = True
    if "percentage" in attributes:
        ops["percentage"] = True
    return ops


def _shown(source, entity_id, state):
    """A recorded state as Home Assistant's live context showed it to Assist (async_get_exposed_entities): a sensor's
    rounded to its display precision (sensor.async_rounded_state), a timestamp in the home's time zone."""
    value = state.get("state")
    options = (source.get("entities", {}).get(entity_id) or {}).get("sensor_options") or {}
    precision = options.get("display_precision", options.get("suggested_display_precision"))
    if entity_id.startswith("sensor.") and precision is not None:
        with suppress(TypeError, ValueError):
            value = f"{float(value):z.{precision}f}"
    if (state.get("attributes") or {}).get("device_class") == "timestamp" and state.get("state"):
        with suppress(ValueError):  # A date that can't be (month 13) stays as it was: the card is still shown.
            if (parsed := dt_util.parse_datetime(state["state"])) is not None:
                zone = dt_util.get_time_zone(source.get("time_zone") or "UTC") or dt_util.UTC
                value = parsed.replace(tzinfo=parsed.tzinfo or zone).astimezone(zone).isoformat()
    return value


def _entity_card(source, entity_id, detailed=False):
    name, area, aliases = _naming(source, entity_id)
    state = source.get("states", {}).get(entity_id)
    card = {"entity_id": entity_id, "name": name}
    if area:
        card["area"] = area
    if aliases:
        card["aliases"] = aliases
    card["exposed"] = source.get("exposure", {}).get(entity_id)
    if state is None:
        card["state"] = "no live state (disabled or missing)"
        return card
    card["state"] = _shown(source, entity_id, state)
    ops = _ops(entity_id, state)
    if ops:
        card["operations"] = ops
    if detailed:
        card["attributes"] = {k: v for k, v in state.get("attributes", {}).items()
                              if k not in OP_ATTRIBUTES and k != "friendly_name"}
    return card


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

    out_turns, mentioned = [], set()
    for turn in turns:
        steps = []
        for message in turn["messages"][1:]:
            if message["role"] == "assistant":
                if message.get("content"):
                    steps.append({"assistant_says": message["content"]})
                for call in message.get("tool_calls") or []:
                    step = {"calls": call["tool_name"], "args": call["tool_args"]}
                    if call.get("external"):
                        step["provider_hosted"] = True
                    steps.append(step)
                    mentioned.update(ENTITY_ID.findall(_text(call["tool_args"])))
            elif message["role"] == "tool_result":
                result = _text(message.get("result"))
                mentioned.update(ENTITY_ID.findall(result))
                steps.append({"result_of": message.get("tool_name"), "result": _clip(result)})
        out_turns.append({"turn": turn["number"], "at": turn["messages"][0]["created"],
                          "user_says": turn["messages"][0]["content"], "assistant": steps,
                          **({"handled_by": "Home Assistant itself, without the AI model"}
                             if turn["handled_locally"] else {})})

    # Practice changes a simulated home; its real state changes belong to someone else.
    practice = recording.get("practice")
    # The recorder counts the flaps it didn't keep (worth_keeping).
    changes, flaps = {}, 0 if practice else recording.get("flaps_not_kept", 0)
    for change in practice["simulated_changes"] if practice else recording.get("state_changes", []):
        old, new = _states(change)
        if background(change) or old == new:
            if flap(change):
                flaps += 1
            continue
        changes.setdefault(change["entity_id"], []).append({"at": change["at"], "during_turn": turn_of(change["at"]),
                                                            "old": old, "new": new})

    known = set(first.get("states", {})) | set(first.get("entities", {}))
    relevant = sorted((mentioned & known) | set(changes))
    relevant_cards = []
    for entity_id in relevant:
        card = _entity_card(first, entity_id, detailed=True)
        card["changes_during_session"] = changes.get(entity_id, [])
        relevant_cards.append(card)

    exposed = sorted(e for e, v in first.get("exposure", {}).items() if v is True)
    by_area = {}
    for entity_id in exposed:
        card = _entity_card(first, entity_id)
        by_area.setdefault(card.pop("area", None) or "No area", []).append(card)
    actions = [_entity_card(first, e) for e in sorted(first.get("entities", {}))
               if e.split(".", 1)[0] in ACTION_DOMAINS]

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
            "scripts_scenes_automations": actions,
        },
        "omitted": {
            "availability_flaps": flaps,
            "background_domains": sorted(NOISE_DOMAINS),
            "note": "Full state and registry snapshots stay in the recording; this digest is a selection.",
        },
        "limitations": deepcopy(recording.get("limitations", [])) + (
            [f"practice session: devices were simulated; {len(practice['unsimulated_actions'])} action(s) "
             "were reported to the assistant as done without being simulated"] if practice else []),
    }


def digest_size(digest):
    return len(json.dumps(digest, ensure_ascii=False, separators=(",", ":")))


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
