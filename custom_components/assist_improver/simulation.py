"""What a recording shows the assistant did, as a replay compares itself with it.

Replays themselves run in a sandboxed HA instance (sandbox.py, replay.py).
"""

from datetime import datetime, timedelta

from homeassistant.helpers.entity import CONTEXT_RECENT_TIME_SECONDS

from .cases import CaseError
from .devices import MOVERS, UNDERWAY


# The conversation machinery's own entities change with every turn; they aren't the home.
MACHINERY = {"conversation", "ai_task", "tts", "stt", "wake_word", "assist_satellite"}


class UnsupportedSimulation(CaseError):
    pass


def _request_changes(recording, turn, until=None):
    """The changes the assistant made during one recorded turn, attributes too: practice's simulated changes, until
    the reply; or the real changes HA attributes to the turn's request, until as long after the reply as HA puts what
    a device reports down to a command it got (CONTEXT_RECENT_TIME_SECONDS): a real device may report a little after
    the reply (a garage relay's press, its last step 60 ms after). until: a later end."""
    start, practice = turn["source"]["at"], recording.get("practice")
    end = until or (turn["ended"] if practice else (datetime.fromisoformat(turn["ended"])
                                                     + timedelta(seconds=CONTEXT_RECENT_TIME_SECONDS)).isoformat())
    if practice:
        changes = practice["simulated_changes"]
    else:
        request = (turn.get("prepared") or {}).get("context", {}).get("context", {}).get("id")
        changes = [c for c in recording.get("state_changes", [])
                   if request and request in ((c.get("new") or {}).get("context") or {}).values()]
    return [c for c in changes if start <= c["at"] <= end and c.get("old") and c.get("new")
            and c["entity_id"].split(".", 1)[0] not in MACHINERY]


def recorded_transitions(recording, turn):
    """The state changes the assistant's request made during one recorded turn: practice's simulated changes, or the
    real changes HA attributes to the turn's request, a template entity's own follow-ons among them."""
    return [{"entity_id": c["entity_id"], "old": c["old"]["state"], "new": c["new"]["state"]}
            for c in _request_changes(recording, turn) if c["old"]["state"] != c["new"]["state"]]


def assist_transitions(recording, turn):
    """The state changes Assist made itself during one recorded turn, as a replay's are compared with them (DETAILS
    P6.6.1): recorded_transitions without a template entity's own follow-ons."""
    follow = template_follow_ons(turn["source"], {c["entity_id"] for c in _request_changes(recording, turn)})
    return [t for t in recorded_transitions(recording, turn) if t["entity_id"] not in follow]


def moved_by_others(recording, turns, turn):
    """What others in the home moved between the recorded turn before this one and this one: each state and attribute
    this turn's snapshot shows otherwise than the turn before's left it, Assist's own changes on it: those under any
    recorded turn's request (a script it started may act turns later), and a device it set moving getting there (a
    door closed, past what Home Assistant puts down to the request). A replay applies them on top of what it did
    itself (sandbox.observe). {entity_id: {"state"?: new, "attributes": {name: new}, "removed": [name]}}"""
    before = turns[turn["number"] - 2]
    start, end, practice = before["source"]["at"], turn["source"]["at"], recording.get("practice")
    requests = {(t["prepared"]["context"].get("context") or {}).get("id") for t in turns} - {None}
    left, moving = dict(before["source"]["states"]), {}  # moving: Assist's last state for each device, from the start.
    for change in sorted((c for c in (practice["simulated_changes"] if practice else recording.get("state_changes", []))
                          if c["at"] < end and c.get("old") and c.get("new")), key=lambda c: c["at"]):
        entity_id, old, new = change["entity_id"], change["old"]["state"], change["new"]["state"]
        context = change["new"].get("context") or {}
        own = practice or requests & {context.get("id"), context.get("parent_id")} or (
            moving.get(entity_id) == old and UNDERWAY.get(new) == old)
        if own or new != old:  # A door's own reports on its way (its position) leave it moving.
            moving[entity_id] = new if own else None
        if own and change["at"] >= start:
            left[entity_id] = change["new"]
    out = {}
    for entity_id, now in turn["source"]["states"].items():
        if (was := left.get(entity_id)) is None:
            continue  # New since: the replay's home never had it.
        old, new = was.get("attributes") or {}, now.get("attributes") or {}
        move = {"attributes": {k: v for k, v in new.items() if k not in old or old[k] != v},
                "removed": sorted(set(old) - set(new)), **({"state": now["state"]} if now["state"] != was["state"] else {})}
        if "state" in move or move["attributes"] or move["removed"]:
            out[entity_id] = move
    return out


def template_follow_ons(source, changed):
    """Of the entities one request changed, those a template entity it changed switches with its own actions (a
    template light's helper; source["template_actions"], as snapshot.py records them): Home Assistant carries those
    out as it runs that entity, under the same request, so they're its own follow-on, not changes Assist made itself.
    A replay's sandbox emulates the template entity as a device instead (P11.1)."""
    acts_on = source.get("template_actions") or {}
    return {e for entity in changed for e in acts_on.get(entity, ()) if e in changed}


def recorded_attributes(recording, turn, entities):
    """{entity: attributes} as the recorded turn's request left each (a light's brightness): as the turn began, or
    as the request's last change set them."""
    left = {e: (turn["source"]["states"].get(e) or {}).get("attributes") or {} for e in entities}
    for change in _request_changes(recording, turn):
        if change["entity_id"] in left:
            left[change["entity_id"]] = change["new"].get("attributes") or {}
    return left


def reports_movement(recording):
    """The devices the recording shows reporting their movement (a garage door "closing", a lock "locking"): a
    replay's emulated devices report it the same way (devices.underway)."""
    moving = set(UNDERWAY.values())
    seen = [(c["entity_id"], (c.get(side) or {}).get("state")) for c in recording.get("state_changes", [])
            for side in ("old", "new")]
    seen += [(e, (s or {}).get("state")) for t in recording.get("turns", [])
             for e, s in ((t.get("source") or {}).get("states") or {}).items()]
    return sorted({e for e, state in seen if state in moving and e.split(".", 1)[0] in MOVERS})


def setting_off(transitions):
    """Transitions as a reproduction compares them: a device that reports its movement by where it set off for (a
    garage door closing), not by its getting there (closed), which its travel time puts before the reply or after it;
    a script started in the turn by its starting, not its getting done, which the devices it waits on put before the
    reply or after it (a car's warm-up script ran 17 seconds while the car woke). A script stopped is compared."""
    started, out = set(), []
    for t in transitions:
        if t["entity_id"].startswith("script.") and t["new"] == "on":
            started.add(t["entity_id"])
        elif UNDERWAY.get(t["new"]) == t["old"] or (t["entity_id"] in started and t["new"] == "off"):
            continue
        out.append(t)
    return out


def same_effects(replay, recorded):
    """Whether a replayed turn's changes to a device are the recorded ones (per_entity's (old, new) pairs): neither
    changed it, or both did from the same state and it ends the turn where it did, the states the replay set coming in
    the recorded order. The recording may show more: a real device reports states of its own on its way (a heater fan
    showed fan only for a moment as it went to heat), which an emulated one doesn't."""
    if not replay or not recorded:
        return replay == recorded
    then = iter(new for _, new in recorded)
    return replay[0][0] == recorded[0][0] and replay[-1][1] == recorded[-1][1] and all(new in then for _, new in replay)


def same_brightness(attributes, recorded):
    """Whether a light's brightness is the recorded one, as the analyzer tells brightnesses apart (none for both: off
    both times)."""
    replay, then = (attributes or {}).get("brightness"), (recorded or {}).get("brightness")
    if replay is None or then is None:
        return replay is None and then is None
    return _brightness_near(attributes, round(int(then) * 100 / 255))


def _brightness_near(attributes, pct):
    """Whether a light's brightness (0-255, as HA holds it) is the percentage asked for, as the analyzer judges a goal
    reached."""
    from .analyzer import BRIGHTNESS_TOLERANCE
    brightness = attributes.get("brightness")
    return brightness is not None and abs(round(int(brightness) * 100 / 255) - pct) <= BRIGHTNESS_TOLERANCE


def per_entity(transitions):
    return {e: [(t["old"], t["new"]) for t in transitions if t["entity_id"] == e]
            for e in {t["entity_id"] for t in transitions}}
