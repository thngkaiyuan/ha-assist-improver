"""Read-only tools for the agentic analyzer: read a session turn by turn and explore the household.

Everything answers from the recording (its conversation and the home snapshot
taken at its first turn), never from the live home, so analysing a session
can't change or reveal anything beyond what was recorded.
"""

from datetime import datetime
import json

import probatio
from homeassistant.helpers import llm

from .cases import recording_turns
from .digest import ACTION_DOMAINS, NOISE_DOMAINS, _clip, _entity_card, _naming, _text, build_digest

API_ID = "assist_improver_session"
MAX_MATCHES = 25
# Recorder notes meant for replays, not for analysing a session.
INTERNAL_NOTES = ("provider_native_history_omitted",)
LATE = 30  # Seconds after the assistant's reply beyond which a change is flagged as late.
FOLLOW_ON = 10  # Seconds within which a same-device change counts as a follow-on of an assistant change.


class SessionView:
    """One recording, sliced for the tools."""

    def __init__(self, recording):
        self.recording = recording
        self.digest = build_digest(recording)
        turns = recording_turns(recording, replay=False)
        self.source = turns[0]["source"]
        self.starts = [t["messages"][0]["created"] for t in turns]
        self.replies = [max((m["created"] for m in t["messages"][1:] if m.get("role") == "assistant"), default=None)
                        for t in turns]
        self.contexts = [((t.get("prepared") or {}).get("context") or {}).get("context") or {} for t in turns]
        self.heard_by = [((t.get("prepared") or {}).get("context") or {}).get("device_id") for t in turns]
        # The assistant's configuration as of this session (its instructions template among it), where recorded.
        self.assistant = next(((t["prepared"].get("provider_config") or [None])[0] for t in turns
                               if t.get("prepared") and t["prepared"].get("provider_config")), None)
        practice = recording.get("practice")
        self.changes = self._changes(practice["simulated_changes"] if practice else recording.get("state_changes", []))

    def _turn_of(self, at):
        return sum(1 for start in self.starts if datetime.fromisoformat(at) >= datetime.fromisoformat(start))

    def _changes(self, raw):
        """Real (or practice-simulated) changes by entity: turn, old and new state, changed attributes, and who."""
        out = {}
        for change in raw:
            entity_id, old, new = change["entity_id"], change.get("old") or {}, change.get("new") or {}
            if entity_id.split(".", 1)[0] in NOISE_DOMAINS or "unavailable" in (old.get("state"), new.get("state")):
                continue
            before, after = old.get("attributes") or {}, new.get("attributes") or {}
            attributes = {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
                          if before.get(k) != after.get(k) and k != "friendly_name"}
            if old.get("state") == new.get("state") and not attributes:
                continue
            turn = self._turn_of(change["at"])
            entry = {"at": change["at"], "during_turn": turn, "old": old.get("state"), "new": new.get("state"),
                     "by": self._who(entity_id, turn, new.get("context") or {})}
            if attributes:
                if entity_id.startswith("light.") and "brightness" in attributes:
                    attributes["brightness_pct"] = [_pct(v) for v in attributes["brightness"]]
                entry["attributes_changed"] = attributes
            reply = self.replies[turn - 1] if 0 < turn <= len(self.replies) else None
            if reply and (late := (datetime.fromisoformat(change["at"]) - datetime.fromisoformat(reply)).total_seconds()) > LATE:
                entry["seconds_after_assistant_reply"] = round(late)
            out.setdefault(entity_id, []).append(entry)
        self._mark_follow_ons(out)
        return out

    def _device(self, entity_id):
        return (self.source.get("entities", {}).get(entity_id) or {}).get("device_id")

    def _mark_follow_ons(self, changes):
        """A change right after an assistant change on the same physical device is a follow-on, not someone else."""
        by_assistant = [(self._device(e), datetime.fromisoformat(c["at"])) for e, cs in changes.items() for c in cs
                        if c["by"] == "assistant" and self._device(e)]
        for entity_id, cs in changes.items():
            device = self._device(entity_id)
            for c in cs:
                if c["by"] != "assistant" and device and any(
                        d == device and 0 <= (datetime.fromisoformat(c["at"]) - at).total_seconds() <= FOLLOW_ON
                        for d, at in by_assistant):
                    c["by"] = "follow-on of an assistant change on the same physical device"

    def _who(self, entity_id, turn, context):
        """Assist, a signed-in person, or something else; recordings before identity capture use Assist's results."""
        turn_context = self.contexts[turn - 1] if 0 < turn <= len(self.contexts) else {}
        if turn_context.get("id") and turn_context["id"] in (context.get("id"), context.get("parent_id")):
            return "assistant"
        found = next((t for t in self.digest["turns"] if t["turn"] == turn), None)
        if found and any(entity_id in str(step.get("result", "")) for step in found["assistant"]):
            return "assistant"
        if context.get("user_id"):
            return "a person (signed in to Home Assistant)"
        return "not the assistant (a person at the device or app, an automation, or the device itself)"

    def _card(self, entity_id, detailed=False):
        card = _entity_card(self.source, entity_id, detailed)
        card["state_at_start"] = card.pop("state")
        changes = self.changes.get(entity_id)
        card["state_at_end"] = changes[-1]["new"] if changes else "unchanged"
        if detailed and "attributes" in card:
            card["attributes_at_start"] = card.pop("attributes")
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
            for entity_id in self._entities():
                if self._wanted(entity_id, False) and _entity_card(self.source, entity_id, False).get("exposed"):
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
        """What the user and the assistant said in each turn, and what Assist's calls acted on (as HA's responses
        say), without the full tool calls, results or state changes."""
        acted = self.acted_on()
        return [{"turn": t["turn"], "at": t["at"], "user_says": t["user_says"],
                 "assistant_says": " ".join(s["assistant_says"] for s in t["assistant"] if s.get("assistant_says")),
                 **({"assist_acted_on": acted[t["turn"]]} if acted.get(t["turn"]) else {})}
                for t in self.digest["turns"]]

    def acted_on(self):
        """{turn: [each of Assist's calls that acted on something: the call, and the devices HA's response says
        it acted on or failed on, or the error it returned]}. Calls that only read (live context) are left out."""
        out = {}
        for turn in recording_turns(self.recording, replay=False):
            calls = {c["id"]: c for m in turn["messages"] if m["role"] == "assistant" for c in m.get("tool_calls") or []}
            for message in turn["messages"]:
                if message["role"] != "tool_result" or not isinstance(message.get("result"), dict):
                    continue
                result, name = message["result"], message.get("tool_name")
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                inner = data.get("data") if isinstance(data.get("data"), dict) else {}
                entry = {"call": name, "args": (calls.get(message.get("tool_call_id")) or {}).get("tool_args", {})}
                if inner.get("success") or inner.get("failed"):
                    for key, label in (("success", "acted_on"), ("failed", "failed_on")):
                        if inner.get(key):
                            entry[label] = [f"{t.get('name')} ({t.get('id')})" for t in inner[key]]
                elif result.get("error") or data.get("error"):
                    entry["error"] = _clip(_text(data.get("error") or data.get("error_text") or result.get("error")), 200)
                else:
                    continue
                out.setdefault(turn["number"], []).append(entry)
        return out

    def read_turn(self, turn):
        found = next((t for t in self.digest["turns"] if t["turn"] == turn), None)
        if found is None:
            return {"error": f"There is no turn {turn}; turns are 1 to {len(self.digest['turns'])}."}
        steps = []
        for step in found["assistant"]:
            if "result" in step:  # Tool results are JSON text; give them back as objects when they are complete.
                try:
                    step = {**step, "result": json.loads(step["result"])}
                except ValueError:
                    pass
            steps.append(step)
        changed, drift = [], []
        for e, cs in self.changes.items():
            for c in cs:
                if c["during_turn"] != turn:
                    continue
                if c["old"] == c["new"] and c["by"] != "assistant":
                    drift.append(e)  # Readings ticking over on devices nobody acted on.
                else:
                    changed.append({"entity_id": e, "name": _naming(self.source, e)[0], **c})
        changed.sort(key=lambda c: c["at"])
        result = {**found, "assistant": steps, "state_changes_during_turn": changed}
        if drift:
            result["attribute_only_updates_elsewhere"] = sorted(set(drift))
        return result

    def read_turns(self, turns):
        return {"turns": [self.read_turn(t) for t in turns]}

    def _entities(self):
        return sorted(set(self.source.get("states", {})) | set(self.source.get("entities", {})))

    def _wanted(self, entity_id, include_sensors):
        domain = entity_id.split(".", 1)[0]
        entry = self.source.get("entities", {}).get(entity_id) or {}
        if entry.get("disabled_by"):  # Disabled entities do nothing; listing them is noise.
            return False
        return domain not in NOISE_DOMAINS or (include_sensors and domain in ("sensor", "binary_sensor"))

    def list_areas(self):
        counts = {}
        for entity_id in self._entities():
            if entity_id.split(".", 1)[0] not in NOISE_DOMAINS:
                area = _naming(self.source, entity_id)[1] or "No area"
                counts[area] = counts.get(area, 0) + 1
        aliases = {a["name"]: a.get("aliases") or [] for a in self.source.get("areas", {}).values()}
        return [{"area": area, "aliases": aliases.get(area, []), "devices": n} for area, n in sorted(counts.items())]

    def devices_in_area(self, area, include_sensors=False):
        wanted = area.casefold()
        names = {a["name"] for a in self.source.get("areas", {}).values()
                 if wanted in [a["name"].casefold(), *(x.casefold() for x in a.get("aliases") or [])]}
        if not names and wanted != "no area":
            return {"error": f"No area called {area!r}. Areas: {', '.join(a['area'] for a in self.list_areas())}"}
        cards = [self._card(e) for e in self._entities() if self._wanted(e, include_sensors)
                 and (_naming(self.source, e)[1] or "No area") in (names or {"No area"})]
        return {"area": area, "devices": cards}

    def find_devices(self, query, include_sensors=False):
        texts = {}
        for entity_id in self._entities():
            if self._wanted(entity_id, include_sensors):
                name, area, aliases = _naming(self.source, entity_id)
                texts[entity_id] = " ".join([entity_id, name or "", area or "", *aliases])
        found, matched = _match(texts, query)
        cards = [self._card(e) for e in found]
        return {"query": query, "matched": matched, "devices": cards[:MAX_MATCHES], "more": max(0, len(cards) - MAX_MATCHES)}

    def device_details(self, entity_id):
        if entity_id not in self._entities():
            return {"error": f"No device {entity_id!r} in this household's snapshot."}
        device_id = (self.source.get("entities", {}).get(entity_id) or {}).get("device_id")
        siblings = [e for e in self._entities() if device_id and e != entity_id and self._wanted(e, False)
                    and (self.source.get("entities", {}).get(e) or {}).get("device_id") == device_id]
        return {**self._card(entity_id, detailed=True), "changes_during_session": self.changes.get(entity_id, []),
                "same_physical_device": [self._card(e) for e in siblings]}

    def list_scripts_and_scenes(self, query=""):
        texts = {e: f"{e} {_naming(self.source, e)[0]}" for e in self._entities() if e.split(".", 1)[0] in ACTION_DOMAINS}
        found, matched = _match(texts, query)
        return {"query": query, "matched": matched, "total_in_household": len(texts),
                "scripts_scenes_automations": [self._card(e) for e in found][:MAX_MATCHES * 2]}


def _pct(brightness):
    return None if brightness is None else round(int(brightness) * 100 / 255)


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


class _SessionTool(llm.Tool):
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, view):
        self.view = view

    async def async_call(self, hass, tool_input, llm_context):
        return llm.ToolResult(data=self.run(**self.parameters(tool_input.tool_args)))


class ReadTurn(_SessionTool):
    name = "read_turn"
    description = ("Read turns of the session: what the user said, what the assistant said and every tool it called "
                   "with the result, and the state changes during the turn, each with who made it (the assistant or "
                   "not) and any attributes that changed. Give one turn, or several as a list.")
    parameters = probatio.Schema({probatio.Optional("turn"): int, probatio.Optional("turns"): [int]})

    def run(self, turn=None, turns=None):
        if turns:
            return self.view.read_turns(turns)
        return self.view.read_turn(turn or 1)


class ListAreas(_SessionTool):
    name = "list_areas"
    description = "List the household's areas (rooms), their aliases and how many devices each has."
    parameters = probatio.Schema({})

    def run(self):
        return {"areas": self.view.list_areas()}


class DevicesInArea(_SessionTool):
    name = "devices_in_area"
    description = ("List the devices in an area with their state at the start of the session (and at the end, if it "
                   "changed), what they can do, and whether the assistant may use them (exposed). Sensors only on "
                   "request.")
    parameters = probatio.Schema({probatio.Required("area"): str, probatio.Optional("include_sensors"): bool})

    def run(self, area, include_sensors=False):
        return self.view.devices_in_area(area, include_sensors)


class FindDevices(_SessionTool):
    name = "find_devices"
    description = ("Find devices whose name, alias, area or entity ID contains all the given words (plurals match "
                   "too). Sensors and diagnostics only on request.")
    parameters = probatio.Schema({probatio.Required("query"): str, probatio.Optional("include_sensors"): bool})

    def run(self, query, include_sensors=False):
        return self.view.find_devices(query, include_sensors)


class DeviceDetails(_SessionTool):
    name = "device_details"
    description = ("One device in detail: its state and attributes at the start, what it can do, whether the "
                   "assistant may use it, every change to it during the session (with who made it), and the other "
                   "entities of the same physical device.")
    parameters = probatio.Schema({probatio.Required("entity_id"): str})

    def run(self, entity_id):
        return self.view.device_details(entity_id)


class ListScriptsAndScenes(_SessionTool):
    name = "list_scripts_and_scenes"
    description = ("List scripts, scenes and automations, optionally filtered by words, with whether the "
                   "assistant may use each (exposed).")
    parameters = probatio.Schema({probatio.Optional("query"): str})

    def run(self, query=""):
        return self.view.list_scripts_and_scenes(query)


TOOLS = (ReadTurn, ListAreas, DevicesInArea, FindDevices, DeviceDetails, ListScriptsAndScenes)


class SessionAPI(llm.API):
    """The tools for one recording (or a given list of tools), handed to the AI Task for one role call."""

    def __init__(self, hass, view=None, extra_tools=(), tools=None):
        super().__init__(hass=hass, id=API_ID, name="Recorded session")
        self.tools = list(tools) if tools is not None else [tool(view) for tool in TOOLS] + list(extra_tools)

    async def async_get_api_instance(self, llm_context):
        return llm.APIInstance(api=self, api_prompt="", llm_context=llm_context, tools=self.tools)
