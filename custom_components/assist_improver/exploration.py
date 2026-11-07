"""Read-only tools for the roles: read a session turn by turn and explore the household.

What they show of the home comes from the recording (its conversation and the home snapshot taken at its first turn),
never from the live home, so analysing a session can't change or reveal anything beyond what was recorded. Only what
an entity can do (its actions) and its integration's name are Home Assistant's now (Setup). For the proposer and
reviewer, SessionAPI adds Home Assistant's own read-only tools, which read the live home (ha_tools.py).
"""

from contextlib import suppress
from datetime import datetime
import json
import logging
import math

import probatio
from homeassistant.helpers import llm
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration
from homeassistant.util import dt as dt_util

from . import happened
from .actions import catalogue, controls, find_actions, operations
from .cases import calls_in, recording_turns
from .digest import (_clip, _naming, _text, async_time_zone, build_digest, device_of, entity_card, filled, labelled,
                     readings, time_zone)
from .ha_tools import tools as ha_tools
from .simulation import follow_ons
from .targets import TOLERANCE
from .trace import traced_tool

_LOGGER = logging.getLogger(__name__)
API_ID = "assist_improver_session"
PAGE = 25  # Entities on a page of a listing; each says how many there are in all.
# A registry record's fields search leaves out: its IDs, the IDs of the records it names, and its times (as Home
# Assistant's registries have them, and had them), whose letters and digits match any word. A text field whose name
# ends in _id, such as a device's model_id, is searched.
UNSEARCHED = frozenset({
    "id", "unique_id", "previous_unique_id", "identifiers", "connections", "entity_id", "device_id", "via_device_id",
    "parent_device_id", "composite_device_id", "area_id", "floor_id", "label_id", "humidity_entity_id",
    "temperature_entity_id", "config_entry_id", "config_subentry_id", "config_entries", "config_entries_subentries",
    "primary_config_entry", "composite_primary_config_entry", "created_at", "modified_at", "split_at",
    "orphaned_timestamp"})
# Recorder notes meant for replays, not for analysing a session.
INTERNAL_NOTES = ("provider_native_history_omitted",)
LATE = 30  # Seconds after the assistant's reply beyond which a change is flagged as late.
FOLLOW_ON = 10  # Seconds within which a same-device change counts as a follow-on of an assistant change.
SETTLES = {("opening", "open"), ("closing", "closed"), ("locking", "locked"), ("unlocking", "unlocked")}
TARGETS = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}  # Whom an action call is for.
NOBODY = "not the assistant (a person at the device or app, an automation, or the device itself)"


def _brightness(change):
    return ((change.get("attributes_changed") or {}).get("brightness_pct") or [None, None])[1]


def _point(value):
    """A number, or a list of numbers (a colour), as coordinates; None for anything else."""
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    return values if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values) else None


def _set_back(was, left, now):
    """Whether an attribute Assist changed from was to left now reads as set back, rather than as left where Assist
    put it: a number or a colour nearer where it was than where Assist left it (a device rounding a setpoint or a
    colour temperature, a blind on its way, stays Assist's); anything else, whatever isn't Assist's value. With
    nothing there before, nothing can be set back."""
    if was is None or now == left:
        return False
    points = [_point(v) for v in (was, left, now)]
    if all(points) and len({len(p) for p in points}) == 1:
        return math.dist(points[2], points[0]) < math.dist(points[2], points[1])
    return True


def _carries(assists, later, named=lambda turn: None):
    """Whether a later change on an entity Assist changed only carries Assist's change through (assists: Assist's
    changes to it before, oldest first): the state Assist's latest set, or settling from it, with a brightness Assist
    set near it and nothing else it set set back. What the device reports on its own and what Assist never set may
    move with it: what Assist set is what its calls in that turn named (named(turn): their settings), or every
    attribute its change moved, where a turn's calls weren't recorded (None)."""
    assist = assists[-1]
    if later["new"] != assist["new"] and (assist["new"], later["new"]) not in SETTLES:
        return False
    set_to = next((b for b in map(_brightness, reversed(assists)) if b is not None), None)
    now = _brightness(later)
    if not (set_to is None or now is None or abs(now - set_to) <= TOLERANCE * 100):  # Points of its percentage.
        return False
    was, left = {}, {}  # Each attribute Assist set: as it was before, and as Assist last left it.
    for change in assists:
        keys = named(change["during_turn"])
        for k, (old, new) in (change.get("attributes_changed") or {}).items():
            if (keys is None or k in keys) and k not in ("brightness", "brightness_pct"):  # A brightness: above.
                was.setdefault(k, old)
                left[k] = new
    return not any(_set_back(was[k], left[k], now) for k, (_, now) in (later.get("attributes_changed") or {}).items()
                   if k in left)


async def session_view(hass, recording, setup=True):
    """A SessionView for code on the event loop, made off it (it reads every turn of a recording of megabytes): its
    time zone (digest.time_zone) first and, with setup, what it reads of Home Assistant (Setup)."""
    first = (recording.get("turns") or [{}])[0].get("source") or {}
    zone = await async_time_zone(first.get("time_zone"))
    return await hass.async_add_executor_job(SessionView, recording, zone, await Setup.read(hass) if setup else None)


class Setup:
    """What a view reads of Home Assistant itself, for what an entity can do and what its integration is called: its
    actions (actions.catalogue), the entity domains they change (controls), and its integrations' names."""

    def __init__(self, found=None, integrations=None):
        self.actions, self.integrations = found or {}, integrations or {}
        self.controls = controls(self.actions)

    @classmethod
    async def read(cls, hass):
        """What Home Assistant has now; none, as offline, when reading it fails (logged): the views go on without."""
        try:
            names = {}
            for domain in hass.config.top_level_components:
                with suppress(IntegrationNotLoaded):
                    names[domain] = async_get_loaded_integration(hass, domain).name
            return cls(await catalogue(hass), names)
        except Exception:  # noqa: BLE001 - an analysis or a proposal goes on without what an entity can do.
            _LOGGER.exception("Couldn't read Home Assistant's actions and integrations")
            return cls()


class SessionView:
    """One recording, sliced for the tools. zone: its time zone, which code on the event loop resolves first
    (session_view); setup: what it reads of Home Assistant (Setup), none offline."""

    def __init__(self, recording, zone=None, setup=None):
        self.recording = recording
        self.turns = turns = recording_turns(recording, replay=False)  # As analysis reads them; never changed.
        self.source = turns[0]["source"]
        self.zone = zone or time_zone(self.source.get("time_zone"))
        self.setup = setup or Setup()
        self.digest = build_digest(recording)
        self.starts = [t["messages"][0]["created"] for t in turns]
        self.replies = [max((m["created"] for m in t["messages"][1:] if m.get("role") == "assistant"), default=None)
                        for t in turns]
        self.contexts = [((t.get("prepared") or {}).get("context") or {}).get("context") or {} for t in turns]
        self.sources = [t["source"] for t in turns]
        self.heard_by = [((t.get("prepared") or {}).get("context") or {}).get("device_id") for t in turns]
        # The assistant's configuration as of this session (its instructions template among it), where recorded.
        self.assistant = next(((t["prepared"].get("provider_config") or [None])[0] for t in turns
                               if t.get("prepared") and t["prepared"].get("provider_config")), None)
        # {(entity, at): 1 if Assist's request is only its context's parent, 2 if only Assist's results name it} (_who).
        self._indirect = {}
        self.changes = self._changes()
        self.notes = {}  # Its notes, where the analyzer hears them, by their turns after those said (read_turn).
        # What happened in each turn (happened.py): its tool calls with Home Assistant's results, and its call log
        # (None: a recording from before it kept one, P8.2.10).
        raw = recording.get("turns") or []
        self.calls = {t["number"]: calls_in(t["messages"][1:]) for t in turns}
        self.actions = {t["number"]: raw[t["number"] - 1].get("actions") if "actions" in raw[t["number"] - 1] else None
                        for t in turns}

    def _turn_of(self, at):
        return sum(1 for start in self.starts if datetime.fromisoformat(at) >= datetime.fromisoformat(start))

    def _changes(self):
        """The changes by entity, as the keep rule labels them (digest.labelled: the voice assistant's own machinery
        isn't the home): turn, old and new state, changed attributes, who, and reading: true for a reading, which the
        views list last. A reading that moved its state (a sensor's value) is never Assist's: nothing it calls sets
        one, so its results naming it say nothing."""
        out = {}
        for change, label in labelled(self.recording):
            entity_id, old, new = change["entity_id"], change.get("old") or {}, change.get("new") or {}
            before, after = old.get("attributes") or {}, new.get("attributes") or {}
            attributes = {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
                          if before.get(k) != after.get(k) and k != "friendly_name"}
            turn = self._turn_of(change["at"])
            by = (NOBODY if label == "reading" and old.get("state") != new.get("state")
                  else self._who(entity_id, turn, new.get("context") or {}, change["at"]))
            entry = {"at": change["at"], "during_turn": turn, "old": old.get("state"), "new": new.get("state"),
                     "by": by, **({"reading": True} if label == "reading" else {})}
            if attributes:
                entry["attributes_changed"] = with_percent(entity_id, attributes)
            reply = self.replies[turn - 1] if 0 < turn <= len(self.replies) else None
            if reply and (late := (datetime.fromisoformat(change["at"]) - datetime.fromisoformat(reply)).total_seconds()) > LATE:
                entry["seconds_after_assistant_reply"] = round(late)
            out.setdefault(entity_id, []).append(entry)
        self._mark_follow_ons(out)
        return out

    def _named(self, turn):
        """The settings a turn's action calls named (their data, but whom it's for): what Assist set in it. None
        where its calls weren't recorded."""
        raw = self.recording.get("turns", [])
        if not 0 < turn <= len(raw) or "actions" not in raw[turn - 1]:
            return None
        return {k for a in raw[turn - 1]["actions"] for k in a.get("data") or {}} - TARGETS

    def _device(self, entity_id):
        return (self.source.get("entities", {}).get(entity_id) or {}).get("device_id")

    def _mark_follow_ons(self, changes):
        """Assist's own follow-ons, and its results read rightly. A change by no one signed in, within FOLLOW_ON seconds
        of Assist's change, is Assist's when it only carries that change through (_carries): on an entity Assist
        changed (ever before it in the session), the state it set, reported again or settling (a blind opening, then
        open), with nothing it set set back; on another entity of the same physical device, one Assist never changed,
        the device's own reaction (a fan's climate turning off with it). Anything else is someone's, a correction
        above all. One credited to Assist only indirectly (_who) is held to carrying through Assist's change to it
        earlier in that turn when it comes after a more direct one (an automation Assist's change set off) or, named
        only by Assist's results, after the turn's reply: else it's someone's, putting it back. What a device Assist
        called does with its own actions, under the same request (a template light's helper), is that device's
        follow-on (simulation.follow_ons)."""
        for number, turn in enumerate(self.turns, 1):
            for entity_id in follow_ons(turn):
                for c in changes.get(entity_id, []):
                    if c["during_turn"] == number and c["by"] == "assistant":
                        c["by"] = "follow-on of an assistant change on the same physical device"
        stamped = sorted(((datetime.fromisoformat(c["at"]), e, c) for e, cs in changes.items() for c in cs),
                         key=lambda x: x[0])
        assists = {}  # {entity: Assist's changes to it so far, oldest first}
        near = []  # [(device, when)] of Assist's changes so far
        for at, entity_id, c in stamped:
            device, own = self._device(entity_id), assists.get(entity_id, [])
            how = self._indirect.get((entity_id, c["at"]))
            late = how == 2 and at > datetime.fromisoformat(self.replies[c["during_turn"] - 1] or c["at"])
            if c["by"] == "assistant" and how and any(
                    a["during_turn"] == c["during_turn"] and (late or self._indirect.get((entity_id, a["at"]), 0) < how)
                    for a in own) and not _carries(own, c, self._named):
                c["by"] = NOBODY
            if c["by"] == "assistant":
                assists.setdefault(entity_id, []).append(c)
                if device:
                    near.append((device, at))
                continue
            if c["by"] == "a person (signed in to Home Assistant)":
                continue
            if own:  # Assist changed this entity: only carrying that through is its follow-on.
                follows = (at - datetime.fromisoformat(own[-1]["at"])).total_seconds() <= FOLLOW_ON and _carries(own, c, self._named)
            else:
                follows = bool(device) and any(d == device and 0 <= (at - t).total_seconds() <= FOLLOW_ON
                                               for d, t in near)
            if follows:
                c["by"] = "follow-on of an assistant change on the same physical device"

    def _who(self, entity_id, turn, context, at=None):
        """Assist, a signed-in person, or something else; recordings before identity capture use Assist's results."""
        turn_context = self.contexts[turn - 1] if 0 < turn <= len(self.contexts) else {}
        if turn_context.get("id") and turn_context["id"] in (context.get("id"), context.get("parent_id")):
            if context.get("id") != turn_context["id"]:  # What its request set off (a sentence trigger, an automation).
                self._indirect[(entity_id, at)] = 1
            return "assistant"
        # A person's own action, in the app or panel; before identity capture Assist's own carried the person's too.
        if turn_context.get("id") and context.get("user_id") and not context.get("parent_id"):
            return "a person (signed in to Home Assistant)"
        found = next((t for t in self.digest["turns"] if t["turn"] == turn), None)
        if found and any(entity_id in str(step.get("result", "")) for step in found["assistant"]):
            self._indirect[(entity_id, at)] = 2  # Only its results say so (_mark_follow_ons reads them again).
            return "assistant"
        if context.get("user_id"):
            return "a person (signed in to Home Assistant)"
        return NOBODY

    def _entry(self, entity_id):
        return self.source.get("entities", {}).get(entity_id) or {}

    def _card(self, entity_id, detailed=False):
        """An entity's card (digest.entity_card) in this session: its state at the start (whole, when detailed) and at
        the end; detailed, what it can do (actions.operations, from Home Assistant's actions, where read)."""
        state = self.source.get("states", {}).get(entity_id)
        found = (operations(self.setup.actions, entity_id, self._entry(entity_id), state)
                 if detailed and self.setup.actions else None)
        card = entity_card(self.source, entity_id, detailed, found)
        card["state_at_start"] = card.pop("state")
        changes = self.changes.get(entity_id)
        card["state_at_end"] = changes[-1]["new"] if changes else "unchanged"
        if detailed:
            card["aliases"] = self.aliases(entity_id)
        if card.get("exposed"):
            twins = [e for e in self._exposed_names().get(card["name"], []) if e != entity_id]
            if twins:  # Assist finds devices by name, so a shared name makes requests ambiguous.
                card["same_name_as"] = twins
        return card

    def _exposed_names(self):
        if not hasattr(self, "_names"):
            self._names = {}
            for entity_id, exposed in (self.source.get("exposure") or {}).items():
                if exposed is True:
                    self._names.setdefault(_naming(self.source, entity_id)[0], []).append(entity_id)
        return self._names

    def aliases(self, entity_id):
        """The names Assist knows a device by besides its own name."""
        entry = self.source.get("entities", {}).get(entity_id) or {}
        names = {entry.get("name"), entry.get("original_name"), _naming(self.source, entity_id)[0]}
        return [a for a in entry.get("aliases") or [] if a and a not in names]

    def overview(self):
        """What the analyzer starts with: the whole conversation (what was said), the rooms and Assist's tools."""
        return {"recording_id": self.digest["recording_id"], "home": self.digest["home"],
                "time_zone": self.digest["time_zone"], "conversation": self.conversation(), "areas": self.list_areas(), "heard_by": self._heard_by(),
                "assist_tools": [t["name"] for t in self.digest["capabilities"]["assist_tools"]],
                "limitations": [n for n in self.digest["limitations"] if not n.startswith(INTERNAL_NOTES)]}

    def _heard_by(self):
        """The device each request came through (a phone, a speaker), where the recording names one."""
        names = []
        for device_id in self.heard_by:
            device = self.source.get("devices", {}).get(device_id or "") or {}
            names.append(device.get("name_by_user") or device.get("name") or device_id)
        return sorted({n for n in names if n})

    def conversation(self):
        """What the user and the assistant said in each turn, and each of Assist's tool calls with Home Assistant's
        result, clipped (read_turn has them whole, with the turn's call log and state changes)."""
        return [{"turn": t["turn"], "at": t["at"], "user_says": t["user_says"],
                 "assistant_says": " ".join(s["assistant_says"] for s in t["assistant"] if s.get("assistant_says")),
                 **({"assist_called": [{"call": c["tool"], "args": c["args"], "result": _clip(_text(c["result"]), 200)}
                                       for c in calls]} if (calls := self.calls.get(t["turn"])) else {})}
                for t in self.digest["turns"]]

    def devices_touched(self):
        """The devices this session touched: those its calls were at (happened.touched: its tool calls' results and
        its call log), and those that changed."""
        homes = self.sources or [self.source]
        found = set().union(*(happened.touched(calls, self.actions.get(n), homes[min(n, len(homes)) - 1])
                              for n, calls in self.calls.items()))
        return found | {c["entity_id"] for t in self.digest["turns"]
                        for c in self.read_turn(t["turn"])["state_changes_during_turn"]}

    def read_turn(self, turn):
        found = next((t for t in self.digest["turns"] if t["turn"] == turn), None)
        last = len(self.digest["turns"]) + len(self.notes)
        if found is None and turn in self.notes:  # A note: words only, with nothing Assist did.
            return dict(self.notes[turn])
        if found is None:
            return {"error": f"There is no turn {turn}; turns are 1 to {last}."}
        steps = []
        for step in found["assistant"]:
            if "result" in step:  # Tool results are JSON text; give them back as objects when they are complete.
                try:
                    step = {**step, "result": json.loads(step["result"])}
                except ValueError:
                    pass
            steps.append(step)
        changed, found_readings = [], []
        acted = {e for e, cs in self.changes.items() for c in cs if c["during_turn"] <= turn and c["by"] == "assistant"}
        for e, cs in self.changes.items():
            for c in cs:
                if c["during_turn"] != turn:
                    continue
                if c.get("reading") and c["by"] != "assistant" and e not in acted:  # Listed last (digest.readings).
                    found_readings.append((e, {k: c[k] for k in ("at", "old", "new", "attributes_changed") if k in c}))
                else:
                    changed.append({"entity_id": e, "name": _naming(self.source, e)[0], **c})
        changed.sort(key=lambda c: c["at"])
        result = {**found, "assistant": steps, "state_changes_during_turn": changed}
        lines, more = readings(sorted(found_readings, key=lambda r: r[1]["at"]))
        if lines:
            result["readings_elsewhere"] = lines
        if more:
            result["more_entities_with_readings"] = more
        return result

    def read_turns(self, turns):
        return {"turns": [self.read_turn(t) for t in turns]}

    def read_readings(self, entity_id=None, turn=None, after=None, page=1):
        """The session's readings (what devices reported on their own, kept: digest.kept), 25 to a page with the
        total: without an entity, one line per entity (digest.readings: how many, the first and the last), those with
        the most first; with one, its readings in time order. turn: only those during it; after: only those after a
        time (ISO 8601; in the household's time zone if it names none)."""
        since = None
        if after is not None:
            if (since := dt_util.parse_datetime(after)) is None:
                return {"error": f"{after!r} isn't a time; give one like 2026-11-06T08:00:00."}
            since = since if since.tzinfo else since.replace(tzinfo=self.zone)
        found = sorted(((e, {k: c[k] for k in ("at", "during_turn", "old", "new", "attributes_changed") if k in c})
                        for e, cs in self.changes.items() for c in cs if c.get("reading")
                        and (turn is None or c["during_turn"] == turn)
                        and (since is None or datetime.fromisoformat(c["at"]) > since)), key=lambda r: r[1]["at"])
        page = max(page, 1)
        start = (page - 1) * PAGE
        if entity_id:
            rows, key = [r for e, r in found if e == entity_id], "readings"
        else:
            rows, key = readings(found, len(found))[0], "entities"
        return {**({"entity_id": entity_id} if entity_id else {"readings_in_all": len(found)}), "total": len(rows),
                "page": page, key: rows[start:start + PAGE],
                **({"next_page": page + 1} if start + PAGE < len(rows) else {})}

    def _entities(self):
        return sorted(set(self.source.get("states", {})) | set(self.source.get("entities", {})))

    def _rank(self, entity_id):
        """How listings order entities: those Assist may use (exposed) first, then controls (what an action changes),
        then the rest; within each, those with no entity category before diagnostic and configuration ones, and
        enabled, visible ones before the others."""
        entry = self._entry(entity_id)
        return (self.source.get("exposure", {}).get(entity_id) is not True,
                entity_id.split(".", 1)[0] not in self.setup.controls, bool(entry.get("entity_category")),
                bool(entry.get("disabled_by") or entry.get("hidden_by")), entity_id)

    def _page(self, entities, page, **about):
        """One page of a listing, the entities ranked (_rank), with how many there are in all."""
        ranked, page = sorted(entities, key=self._rank), max(page, 1)
        start = (page - 1) * PAGE
        return {**about, "total": len(ranked), "page": page,
                "devices": [self._card(e) for e in ranked[start:start + PAGE]],
                **({"next_page": page + 1} if start + PAGE < len(ranked) else {})}

    def _search_text(self, entity_id):
        """What search reads of an entity: its ID, names and aliases, and every text field of its registry entry, its
        device's (a child device's maker and model are its parent's), its area's and labels', and its integration's
        domain and name."""
        if not hasattr(self, "_texts"):
            self._texts = {}
        if entity_id not in self._texts:
            entry = self._entry(entity_id)
            device = device_of(self.source, entry.get("device_id"))
            name, area, aliases = _naming(self.source, entity_id)
            area_record = self.source.get("areas", {}).get(entry.get("area_id") or device.get("area_id") or "") or {}
            labels = [self.source.get("labels", {}).get(label) or {}
                      for label in {*(entry.get("labels") or []), *(device.get("labels") or [])}]
            parts = [entity_id, name, area, *aliases, *_strings(entry), *_strings(device), *_strings(area_record),
                     *(s for label in labels for s in _strings(label)),
                     self.setup.integrations.get(entry.get("platform") or "")]
            self._texts[entity_id] = " ".join(p for p in parts if isinstance(p, str))
        return self._texts[entity_id]

    def list_areas(self):
        """Every area, with its aliases and how many entities it has, then the entities in none (No area)."""
        counts = {}
        for entity_id in self._entities():
            area = _naming(self.source, entity_id)[1] or "No area"
            counts[area] = counts.get(area, 0) + 1
        areas = sorted(self.source.get("areas", {}).values(), key=lambda a: a.get("name") or "")
        return ([{"area": a["name"], "aliases": list(a.get("aliases") or []), "entities": counts.get(a["name"], 0)}
                 for a in areas]
                + ([{"area": "No area", "aliases": [], "entities": counts["No area"]}] if "No area" in counts else []))

    def devices_in_area(self, area, page=1):
        wanted = area.casefold()
        names = {a["name"] for a in self.source.get("areas", {}).values()
                 if wanted in [a["name"].casefold(), *(x.casefold() for x in a.get("aliases") or [])]}
        if not names and wanted != "no area":
            return {"error": f"No area called {area!r}. Areas: {', '.join(a['area'] for a in self.list_areas())}"}
        return self._page([e for e in self._entities() if (_naming(self.source, e)[1] or "No area")
                           in (names or {"No area"})], page, area=area)

    def find_devices(self, query, page=1):
        found, matched = _match({e: self._search_text(e) for e in self._entities()}, query)
        return self._page(found, page, query=query, matched=matched)

    def device_details(self, entity_id, page=1):
        """One entity in detail, with a page of the other entities of its device (25 to a page)."""
        if entity_id not in self._entities():
            return {"error": f"No device {entity_id!r} in this household's snapshot."}
        device_id = self._entry(entity_id).get("device_id")
        siblings = self._page([e for e in self._entities() if device_id and e != entity_id
                               and self._entry(e).get("device_id") == device_id], page)
        changes = self.changes.get(entity_id, [])
        apart = not any(c["by"] == "assistant" for c in changes)  # Its readings, where Assist never changed it.
        lines, _ = readings([(entity_id, c) for c in changes if apart and c.get("reading")])
        return {**self._card(entity_id, detailed=True),
                "changes_during_session": [c for c in changes if not (apart and c.get("reading"))],
                **({"readings": lines[0]} if lines else {}),  # One line, last: how many, the first and the last.
                "same_physical_device": siblings["devices"],
                **({"same_physical_device_total": siblings["total"], "page": siblings["page"]}
                   if siblings["total"] > PAGE else {}),
                **({"next_page": siblings["next_page"]} if "next_page" in siblings else {})}

    def assistant_setup(self):
        """The assistant as this session recorded it: every option of its settings (its subentry's data, where the
        recording says which assistant answered), the tools Assist had with their parameters, the provider-hosted
        tools it called, and the voice assistant (pipeline) the requests ran through."""
        prepared = next((t["prepared"] for t in self.turns if t.get("prepared")), None) or {}
        configs = prepared.get("provider_config") or []
        hosted = {c["tool_name"] for t in self.turns for m in t["messages"] if m.get("role") == "assistant"
                  for c in m.get("tool_calls") or [] if c.get("external")}
        return filled({"assistant": configs[0].get("title") if len(configs) == 1 else None,
                       "options": configs[0].get("options") if len(configs) == 1 else None,
                       "tools": prepared.get("tools"), "hosted_tools_called": sorted(hosted),
                       "pipeline": next((t["pipeline"] for t in self.recording.get("turns") or [] if t.get("pipeline")),
                                        None)})

    def read_definition(self, entity_id):
        """A script's, automation's or scene's definition, as Home Assistant held it when this view's home was read
        (a script by its object ID; an automation or scene by the configuration ID its state names, else its name)."""
        domain, _, object_id = entity_id.partition(".")
        attributes = (self.source.get("states", {}).get(entity_id) or {}).get("attributes") or {}
        if domain == "script":
            found = (self.source.get("scripts") or {}).get(object_id)
        elif domain in ("automation", "scene"):
            items = self.source.get(f"{domain}s") or []
            found = (next((i for i in items if attributes.get("id") and i.get("id") == attributes["id"]), None)
                     or next((i for i in items if attributes.get("friendly_name")
                              and attributes["friendly_name"] in (i.get("alias"), i.get("name"))), None))
        else:
            return {"error": "read_definition reads a script, an automation or a scene: give its entity_id."}
        if found is None:
            return {"error": f"Home Assistant gave no definition for {entity_id}: it isn't there, or an integration "
                             "other than Home Assistant's own editors made it."}
        # Its id in its editor, which a change to it names.
        return {"entity_id": entity_id, "id": object_id if domain == "script" else found.get("id"), "definition": found}


def _strings(record):
    """A registry record's text: its strings, alone or in a list, but its IDs and times (UNSEARCHED)."""
    out = []
    for key, value in (record or {}).items():
        if key not in UNSEARCHED:
            out.extend(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str))
    return out


def _pct(brightness):
    return None if brightness is None else round(int(brightness) * 100 / 255)


def with_percent(entity_id, attributes):
    """A change's attributes ({attribute: [old, new]}), a light's brightness as a percentage too (brightness_pct)."""
    if entity_id.startswith("light.") and "brightness" in attributes:
        return {**attributes, "brightness_pct": [_pct(v) for v in attributes["brightness"]]}
    return attributes


def _match(texts, query):
    """Entities whose text has every query word (plurals too); failing that, any word, and say which."""
    words = [w.rstrip("s") or w for w in query.casefold().split()]  # "doors" finds "Door".
    if not words:
        return sorted(texts), "everything"
    every = [e for e, t in sorted(texts.items()) if all(w in t.casefold() for w in words)]
    if every:
        return every, "all words"
    some = [e for e, t in sorted(texts.items()) if any(w in t.casefold() for w in words)]
    return some, ("any word (nothing had all of them)" if some else "nothing")


class RoleTool(llm.Tool):
    """Every tool the improver's AI roles get. Home Assistant hands a tool the model's arguments unchecked, and hands
    back to the model only an argument error (probatio.Invalid) or a HomeAssistantError: anything else a tool raises
    ends the model's whole generation. So the arguments are checked first. Its conversation trace deep-copies every
    tool a model is given: a tool is shared, never copied (one holding the flow, and so Home Assistant, can't be)."""
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, view):
        self.view = view

    def __deepcopy__(self, memo):
        return self

    async def async_call(self, hass, tool_input, llm_context):
        return llm.ToolResult(data=await self.answer(hass, self.parameters(tool_input.tool_args)))

    async def answer(self, hass, args):
        """What the model gets back for arguments its schema took."""
        return self.run(**args)


class ReadTurn(RoleTool):
    name = "read_turn"
    description = ("Read turns of the session: what the user said, what the assistant said and every tool it called "
                   "with the result, and the state changes during the turn, each with who made it (the assistant or "
                   "not) and any attributes that changed, readings last (read_readings sifts them all). Give one "
                   "turn, or several as a list.")
    parameters = probatio.Schema({probatio.Optional("turn"): int, probatio.Optional("turns"): [int]})

    def run(self, turn=None, turns=None):
        if turns:
            return self.view.read_turns(turns)
        return self.view.read_turn(turn or 1)


class ListAreas(RoleTool):
    name = "list_areas"
    description = "List the household's areas (rooms), their aliases and how many entities each has."
    parameters = probatio.Schema({})

    def run(self):
        return {"areas": self.view.list_areas()}


LISTING = ("Every entity, 25 to a page with the total: those the assistant may use (exposed) and controls first, each "
           "with its state at the start of the session (and at the end, if it changed) and its labels (entity "
           "category, disabled, hidden) where set.")


class DevicesInArea(RoleTool):
    name = "devices_in_area"
    description = "List the entities in an area. " + LISTING
    parameters = probatio.Schema({probatio.Required("area"): str, probatio.Optional("page", default=1): int})

    def run(self, area, page=1):
        return self.view.devices_in_area(area, page)


class FindDevices(RoleTool):
    name = "find_devices"
    description = ("Find entities whose text holds all the given words (plurals match too): names, aliases, area, "
                   "labels, entity ID, and the entity's, its device's and its integration's details. " + LISTING)
    parameters = probatio.Schema({probatio.Required("query"): str, probatio.Optional("page", default=1): int})

    def run(self, query, page=1):
        return self.view.find_devices(query, page)


class DeviceDetails(RoleTool):
    name = "device_details"
    description = ("One entity in detail: its state at the start, whole; its entity and device registry entries; what "
                   "it can do (the actions that target its domain or integration, with their fields; not those any "
                   "entity takes); whether the assistant may use it; every change to it during the session (with who "
                   "made it); and the other entities of the same physical device, 25 to a page.")
    parameters = probatio.Schema({probatio.Required("entity_id"): str, probatio.Optional("page", default=1): int})

    def run(self, entity_id, page=1):
        return self.view.device_details(entity_id, page)


class ReadReadings(RoleTool):
    name = "read_readings"
    description = ("Sift through the session's readings: what devices reported on their own (a sensor's values, "
                   "updates nobody caused). Without entity_id, one line per entity: how many, the first and the last, "
                   "most first; with entity_id, its readings in time order. 25 to a page with the total; turn or after "
                   "(a time) narrows them.")
    parameters = probatio.Schema({probatio.Optional("entity_id"): str, probatio.Optional("turn"): int,
                                  probatio.Optional("after"): str, probatio.Optional("page", default=1): int})

    def run(self, **args):
        return self.view.read_readings(**args)


class ReadDefinition(RoleTool):
    name = "read_definition"
    description = ("Read a script's, automation's or scene's definition, in Home Assistant's own format: its steps, "
                   "its triggers and conditions, or its entities' states, and the id a change to it names. "
                   "Give its entity_id.")
    parameters = probatio.Schema({probatio.Required("entity_id"): str})

    def run(self, entity_id):
        return self.view.read_definition(entity_id)


class FindActions(RoleTool):
    name = "find_actions"
    description = ("Find Home Assistant's actions (what a script or automation step can call) whose ID, name, "
                   "description or fields hold all the given words (any, if none has all): each with its description, "
                   "what it targets and its fields, 25 to a page with the total.")
    parameters = probatio.Schema({probatio.Required("query"): str, probatio.Optional("page", default=1): int})

    def run(self, query, page=1):
        return find_actions(self.view.setup.actions, query, page)


class ReadAssistant(RoleTool):
    name = "read_assistant"
    description = ("The assistant as this session recorded it: every option of its settings, the tools Assist had "
                   "with their parameters, the provider-hosted tools it called, and its voice assistant (pipeline).")
    parameters = probatio.Schema({})

    def run(self):
        return self.view.assistant_setup()


TOOLS = (ReadTurn, ListAreas, DevicesInArea, FindDevices, DeviceDetails, ReadReadings, ReadDefinition, ReadAssistant)


# Existing improvements (Improvements.catalog: waiting for the household's review, or applied), which every AI role
# that could propose a fix checks first, so nothing is fixed twice.

def applied_too_early(item, started):
    """Why an improvement can't answer for friction in a session that started at `started`, or None: one applied
    before the session didn't stop its friction."""
    from homeassistant.util.dt import parse_datetime
    if item.get("applied") and parse_datetime(started) >= parse_datetime(item["applied"]):
        return f"{item['title']!r} was applied before this session, and the friction still happened, so it doesn't " \
               "address it."
    return None


def related_improvements(existing, devices, goals=None):
    """The existing improvements that touch any of these devices, found by code for a role to judge: each as
    list_improvements shows it, with the devices it shares and (given goals as {"session:turn": devices}) the goals
    it may address."""
    out = []
    for item in existing:
        touched = set(item.get("devices") or [])
        if shared := sorted(touched & set(devices)):
            may = [goal for goal, found in (goals or {}).items() if found & touched]
            out.append({**{k: v for k, v in item.items() if k not in ("devices", "details")},
                        **({"may_address": may} if may else {}), "shared_devices": shared})
    return out


class ListImprovements(RoleTool):
    name = "list_improvements"
    description = ("List the improvements that already exist: those waiting for the household's review, and those "
                   "applied (with when). Each has its cause, its changes, the conversation it came from, and the goals "
                   "(session:goal turn) its test fixed there. "
                   "Check it before proposing a fix: an improvement may already address the friction.")
    parameters = probatio.Schema({})

    def __init__(self, view, existing):
        super().__init__(view)
        self.existing = existing

    def run(self):
        listed = [{k: v for k, v in i.items() if k not in ("details", "devices")} for i in self.existing]
        return {"improvements": listed} if listed else {
            "improvements": [], "note": "No improvements yet: every goal's friction is new."}


class ReadImprovement(RoleTool):
    name = "read_improvement"
    description = ("Read one existing improvement in full: what it changes and was expected to do, the sessions it "
                   "agrees and disagrees with, each test (the goal, the conversation before, the replay with the "
                   "change after, the result), and the household's decisions on it. Use it when the list doesn't "
                   "settle whether it addresses a goal.")
    parameters = probatio.Schema({probatio.Required("improvement"): str})

    def __init__(self, view, existing):
        super().__init__(view)
        self.existing = existing

    def run(self, improvement):
        found = next((i for i in self.existing if i["improvement"] == improvement), None)
        if found is None:
            return {"error": f"There's no improvement {improvement!r}; list_improvements shows "
                             f"{', '.join(i['improvement'] for i in self.existing) or 'none'}."}
        return {**{k: v for k, v in found.items() if k not in ("details", "devices")}, **(found.get("details") or {})}


class SessionAPI(llm.API):
    """The tools for one recording (or a given list of tools), handed to the AI Task for one role call. Each call
    of a tool gets a span in the current trace (trace.py)."""

    def __init__(self, hass, view=None, extra_tools=(), tools=None, user_id=None):
        super().__init__(hass=hass, id=API_ID, name="Recorded session")
        tools = list(tools) if tools is not None else [tool(view) for tool in TOOLS] + list(extra_tools)
        self.tools = [traced_tool(tool) for tool in tools]
        self.user_id = user_id  # The improver's user, for Home Assistant's own tools (the proposer's and reviewer's).

    async def async_get_api_instance(self, llm_context):
        own = [traced_tool(tool) for tool in await ha_tools(self.hass, self.user_id)]
        return llm.APIInstance(api=self, api_prompt="", llm_context=llm_context, tools=self.tools + own)
