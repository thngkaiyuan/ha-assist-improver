"""What a recording shows the assistant did, as a replay compares itself with it.

Replays themselves run in a sandboxed HA instance (sandbox.py, replay.py).
"""

from datetime import datetime, timedelta

from homeassistant.core import async_get_hass_or_none

from . import touchpoints
from .cases import CaseError, calls_in
from .digest import kept, machinery
from .happened import at_home
from .standin import MOVERS, UNDERWAY


class UnsupportedSimulation(CaseError):
    pass


def _request_changes(recording, turn, until=None):
    """The changes the assistant made during one recorded turn, attributes too: the real changes HA attributes to the
    turn's request, until as long after the reply as HA puts what a device reports down to a command it got
    (recent_context): a real device may report a little after the reply (a relay's press, its last step).
    until: a later end."""
    start = turn["source"]["at"]
    if until is None:
        hass = async_get_hass_or_none()  # None in a worker, which hands a break to the real instance to report.
        recent = touchpoints.get(hass, "recent_context") if hass else touchpoints.resolve("recent_context")
        until = (datetime.fromisoformat(turn["ended"]) + timedelta(seconds=recent)).isoformat()
    end = until
    request = (turn.get("prepared") or {}).get("context", {}).get("context", {}).get("id")
    gears = machinery(recording)  # The voice assistant's own entities change with every turn; they aren't the home.
    changes = [c for c in recording.get("state_changes", [])
               if request and request in ((c.get("new") or {}).get("context") or {}).values()]
    return [c for c in changes if start <= c["at"] <= end and c.get("old") and c.get("new")
            and kept(c, (request,), gears) != "machinery"]


def recorded_transitions(recording, turn):
    """The state changes the assistant's request made during one recorded turn: the real changes HA attributes to the
    turn's request, a device's own follow-ons among them."""
    return [{"entity_id": c["entity_id"], "old": c["old"]["state"], "new": c["new"]["state"]}
            for c in _request_changes(recording, turn) if c["old"]["state"] != c["new"]["state"]]


def assist_transitions(recording, turn):
    """The state changes Assist made itself during one recorded turn, as a replay's are compared with them (DETAILS
    P6.6.1): recorded_transitions without a device's own follow-ons (follow_ons)."""
    follow = follow_ons(turn)
    return [t for t in recorded_transitions(recording, turn) if t["entity_id"] not in follow]


def moved_by_others(recording, turns, turn):
    """What others in the home moved between the recorded turn before this one and this one: each state and attribute
    this turn's snapshot shows otherwise than the turn before's left it, Assist's own changes on it: those under any
    recorded turn's request (a script it started may act turns later), and a device it set moving getting there (a
    door closed, past what Home Assistant puts down to the request). A replay applies them on top of what it did
    itself (sandbox.observe). {entity_id: {"state"?: new, "attributes": {name: new}, "removed": [name]}}"""
    before = turns[turn["number"] - 2]
    start, end = before["source"]["at"], turn["source"]["at"]
    requests = {(t["prepared"]["context"].get("context") or {}).get("id") for t in turns} - {None}
    left, moving = dict(before["source"]["states"]), {}  # moving: Assist's last state for each device, from the start.
    changes = (c for c in recording.get("state_changes", []) if c["at"] < end and c.get("old") and c.get("new"))
    for change in sorted(changes, key=lambda c: c["at"]):
        entity_id, old, new = change["entity_id"], change["old"]["state"], change["new"]["state"]
        context = change["new"].get("context") or {}
        own = requests & {context.get("id"), context.get("parent_id")} or (
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


def ended(recording):
    """The home as a recording's conversation ended: its last turn's snapshot, each state as the recorded changes after
    it left it, at the conversation's end (its last recorded activity, else its last change)."""
    last = recording["turns"][-1]["source"]
    states, at = dict(last.get("states") or {}), last.get("at")
    for change in sorted((c for c in recording.get("state_changes", []) if c["at"] >= (at or "")),
                         key=lambda c: c["at"]):
        if change.get("new") is None:
            states.pop(change["entity_id"], None)
        else:
            states[change["entity_id"]] = {**change["new"], "entity_id": change["entity_id"]}
        at = max(at or "", change["at"])
    return {**last, "states": states, "at": recording.get("last_activity") or at}


# What exists to act on other entities: an action of one of these is the request's own, not a device's.
ACTS_ON_OTHERS = {"script", "scene", "automation", "homeassistant"}


def _targets(action):
    """The entities an action call names (its entity_id, one or a list)."""
    named = (action.get("data") or {}).get("entity_id") or []
    return {e.strip() for e in (named.split(",") if isinstance(named, str) else named) if isinstance(e, str)}


def recorded(recording, tools=()):
    """What a replay's sandbox answers as the recording did (sandbox.seed): the tools the assistant had beyond Home
    Assistant's Assist today (tools: today's names), as recorded; each tool call's result; each action that failed at
    home (happened.at_home: one Home Assistant refused as invalid, it refuses again itself), with its error, by the
    entities it named and the turn's moment (its at); and what each media search played, by the media players it
    played on."""
    turns, specs = recording.get("turns") or [], {}
    for turn in turns:
        for spec in (turn.get("prepared") or {}).get("tools") or []:
            if spec["name"] not in tools:
                specs.setdefault(spec["name"], spec)
    calls = calls_in((recording.get("chat_log") or {}).get("content") or [])
    actions = [a for turn in turns for a in turn.get("actions") or []]
    failed = [{"action": a["action"], "entity_ids": sorted(_targets(a)), "error": a["error"],
               "at": (turn.get("source") or {}).get("at")} for turn in turns for a in turn.get("actions") or []
              if at_home(a)]
    played = [a for a in actions if a["action"] == "media_player.play_media"]
    media = [found for c in calls if isinstance(c["result"], dict) and isinstance(
        found := ((c["result"].get("data") or {}).get("speech_slots") or {}).get("media"), dict)]
    return {"tools": list(specs.values()),
            "results": [{"tool_name": c["tool"], "tool_args": c["args"], "result": c["result"]} for c in calls],
            "failed": failed,
            "media": [{"entity_ids": sorted({e for a in played if (a.get("data") or {}).get("media_content_id")
                                             == m.get("media_content_id") for e in _targets(a)}), "media": m}
                      for m in media]}


def follow_ons(turn):
    """What a device one turn's request called did with its own actions (a template light switching its helper; the
    turn's recorded action calls, DETAILS P8.2.10): the entities called inside a call to a device (any domain but
    those that act on others: scripts, scenes, automations, homeassistant's), but those the request called itself.
    Home Assistant carries them out as it runs that device, so they're its follow-on, not changes Assist made itself;
    a replay's sandbox stands in for the device instead (P11.1)."""
    actions = turn.get("actions") or []

    def in_a_device(index):
        while (index := actions[index].get("under")) is not None:
            if actions[index]["action"].split(".", 1)[0] not in ACTS_ON_OTHERS:
                return True
        return False
    called = {e for a in actions if a.get("under") is None for e in _targets(a)}
    return {e for i, a in enumerate(actions) if in_a_device(i) for e in _targets(a)} - called


def assist_settings(recording, turn):
    """What the recorded Assist set on devices' settings in one turn, as a replay's are compared with it (DETAILS
    P6.6.1): {entity: {setting: value}}, as the turn's call log asked it (each call that didn't fail, on the entities it
    names but a device's own follow-ons, its fields that name a setting: targets.known), else, in a recording from
    before one was kept, each setting the request's own changes moved, as they left it. What a device reports beyond
    what it was asked (the track a playlist it was asked to play is on) isn't Assist's doing."""
    from . import happened, targets
    follow, out = follow_ons(turn), {}
    if "actions" in ((recording.get("turns") or [])[turn["number"] - 1:turn["number"]] or [{}])[0]:
        for action in turn.get("actions") or []:
            data = action.get("data") or {}
            for entity in set() if action.get("error") else happened.named(action["action"], data, turn["source"]):
                domain = entity.split(".", 1)[0]
                if entity not in follow:
                    out.setdefault(entity, {}).update({k: v for k, v in data.items() if targets.known(domain, k)})
    else:
        for change in _request_changes(recording, turn):
            entity, old = change["entity_id"], change["old"].get("attributes") or {}
            settable = targets.settable(entity.split(".", 1)[0])
            if entity not in follow:
                out.setdefault(entity, {}).update({k: v for k, v in (change["new"].get("attributes") or {}).items()
                                                   if k in settable and v is not None and old.get(k) != v})
    return {e: settings for e, settings in out.items() if settings}


def same_settings(domain, asked, attributes, found):
    """Whether a replayed device holds the settings the recorded Assist set on it (assist_settings), as targets.holds
    reads them, each where the replay shows that setting (a light switched on with none named comes on at the level
    it remembers, which only the real light knew)."""
    from . import targets
    return targets.holds(domain, {k: v for k, v in asked.items() if any(
        attributes.get(a) is not None for a in targets.representing(domain, k))}, attributes, found)


def reports_movement(recording):
    """The devices the recording shows reporting their movement (a garage door "closing", a lock "locking"): their
    stand-ins in a replay report it the same way (standin.StandIn.hold)."""
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
    reply or after it (one waiting on a device slow to wake). A script stopped is compared."""
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
    the recorded order. The recording may show more: a real device reports states of its own on its way (a climate
    device passing through another mode), which an emulated one doesn't."""
    if not replay or not recorded:
        return replay == recorded
    then = iter(new for _, new in recorded)
    return replay[0][0] == recorded[0][0] and replay[-1][1] == recorded[-1][1] and all(new in then for _, new in replay)


def per_entity(transitions):
    return {e: [(t["old"], t["new"]) for t in transitions if t["entity_id"] == e]
            for e in {t["entity_id"] for t in transitions}}
