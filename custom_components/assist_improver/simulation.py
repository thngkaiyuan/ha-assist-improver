"""What a recording shows the assistant did, as a replay compares itself with it.

Replays themselves run in a sandboxed HA instance (sandbox.py, replay.py).
"""

from .cases import CaseError
from .devices import MOVERS, UNDERWAY


# The conversation machinery's own entities change with every turn; they aren't the home.
MACHINERY = {"conversation", "ai_task", "tts", "stt", "wake_word", "assist_satellite"}


class UnsupportedSimulation(CaseError):
    pass


def _request_changes(recording, turn):
    """The changes the assistant made during one recorded turn, attributes too: practice's simulated changes, or the
    real changes HA attributes to the turn's request."""
    start, end = turn["source"]["at"], turn["ended"]
    practice = recording.get("practice")
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
    garage door closing), not by its getting there (closed), which its travel time puts before the reply or after it."""
    return [t for t in transitions if UNDERWAY.get(t["new"]) != t["old"]]


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
