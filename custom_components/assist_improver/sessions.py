"""The sessions the proposer and reviewer work over: a short brief of each, always at hand, and its full recording,
loaded only when a tool or a check opens it.

Why each piece is there (what it helps the roles do):
- A brief (when, the user's requests, the devices, names and areas its calls touched, whether it can be replayed)
  lets code list, filter and search sessions, and find the ones a change would apply to, without loading them.
  Briefs are kept between runs (BriefCache), so a recording is read in full once to make its brief.
- A full recording (about 9 MB in memory) is what read_session, read_session_turn, device_details and the
  sandboxes need. At most LOADED stay loaded; the household's snapshot (the newest replayable session) and the
  assistant's configuration stay for the whole run.
- where_it_applies: the sessions whose recorded calls or requests touch a change's devices, their names or areas.
  Sessions without friction come first: they're where a rule could misfire on requests that already work. The
  regression replays use the same test, so a session no change could affect isn't replayed.
"""

from collections import Counter, OrderedDict

from homeassistant.helpers.storage import Store

from .cases import CaseError, replayable_turns
from .const import DOMAIN, STORAGE_VERSION

LOADED = 12  # Full recordings loaded at once, besides the household's and the assistant's.
PAGE = 20  # Sessions per list_sessions page.
APPLIES_SHOWN = 12  # Sessions where a change applies, listed with a draft; the count covers the rest.
STATES = ("open_friction", "friction_handled", "without_friction", "not_analysed")


def short(recording_id):
    return recording_id[:8]


def brief(view, recording_id):
    """What code needs to list, search and match a session, from its full recording."""
    turns = view.digest["turns"]
    calls = [s for t in turns for s in t["assistant"] if s.get("calls")]
    words, entities, domains = set(), set(), set()
    for call in calls:
        for key, value in (call.get("args") or {}).items():
            for item in value if isinstance(value, list) else [value]:
                if not isinstance(item, str):
                    continue
                if key == "domain":
                    domains.add(item.casefold())
                elif "." in item and " " not in item:
                    entities.add(item)
                else:
                    words.add(item.casefold())
    try:
        entities |= view.devices_touched()
    except (KeyError, IndexError, TypeError):
        pass
    try:
        replayable = bool(replayable_turns(view.recording))
    except CaseError:
        replayable = False
    return {"recording_id": recording_id, "session": short(recording_id), "started": view.digest["started"],
            "turns": len(turns), "requests": [(t.get("user_says") or "").strip()[:300] for t in turns],
            "words": sorted(words), "entities": sorted(entities), "domains": sorted(domains),
            "live_context": any(str(c["calls"]).endswith("GetLiveContext") for c in calls),
            "replayable": replayable, "assistant": view.assistant is not None,
            "assist_tools": [t["name"] for t in view.digest.get("capabilities", {}).get("assist_tools", [])]}


class BriefCache:
    """Briefs kept between runs, by recording; one is made again when its recording changes (its index entry)."""

    def __init__(self, hass):
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.session_briefs", private=True, atomic_writes=True)
        self.briefs, self.loaded = {}, False

    async def _load(self):
        if not self.loaded:
            self.briefs = ((await self.store.async_load()) or {}).get("briefs", {})
            self.loaded = True

    async def get(self, recorder, recording_id):
        await self._load()
        stamp = _stamp(recorder.index.get(recording_id, {}))
        kept = self.briefs.get(recording_id)
        if kept and kept.get("stamp") == stamp:
            return kept["brief"], None
        from .exploration import SessionView
        view = SessionView(await recorder.export(recording_id))
        made = brief(view, recording_id)
        self.briefs[recording_id] = {"stamp": stamp, "brief": made}
        return made, view

    async def save(self, recorder):
        self.briefs = {k: v for k, v in self.briefs.items() if k in recorder.index}
        await self.store.async_save({"briefs": self.briefs})

    async def drop(self, recorder, *recording_ids):
        """Deleted recordings' briefs go from the store too, loaded first if no proposal has read it yet."""
        await self._load()
        for recording_id in recording_ids:
            self.briefs.pop(recording_id, None)
        await self.save(recorder)


def _stamp(summary):
    return [summary.get("status"), summary.get("user_turns"), summary.get("stored", "full"), summary.get("updated")]


async def catalog(recorder, recording_ids):
    """(briefs, the household's view, the assistant's view) for these recordings, reading in full only those with
    no brief yet (one at a time) and the two views the whole run needs."""
    from .exploration import SessionView
    cache = getattr(recorder, "brief_cache", None)
    if cache is None:
        cache = recorder.brief_cache = BriefCache(recorder.hass)
    briefs, kept = [], {}
    for recording_id in recording_ids:
        try:
            made, view = await cache.get(recorder, recording_id)
        except (CaseError, ValueError, KeyError):
            continue  # Still being recorded, or not a conversation the tools can read.
        briefs.append(made)
        if view is not None:
            kept[recording_id] = view
    await cache.save(recorder)
    newest = sorted(briefs, key=lambda b: b["started"] or "", reverse=True)
    household_id = next((b["recording_id"] for b in newest if b["replayable"]), newest[0]["recording_id"] if newest else None)
    assistant_id = next((b["recording_id"] for b in newest if b["assistant"]), None)

    async def view_of(recording_id):
        if recording_id is None:
            return None
        return kept.get(recording_id) or SessionView(await recorder.export(recording_id))
    household = await view_of(household_id)
    assistant = household if assistant_id == household_id else await view_of(assistant_id)
    return briefs, household, assistant


class _Views:
    """sessions.views: the loaded sessions' views by session (all of them when built from views)."""

    def __init__(self, sessions):
        self._sessions = sessions

    def get(self, session, default=None):
        return self._sessions._view(session) or default

    def __getitem__(self, session):
        view = self._sessions._view(session)
        if view is None:
            raise KeyError(session)
        return view

    def __contains__(self, session):
        return session in self._sessions.briefs

    def __iter__(self):
        return iter(sorted(self._sessions.briefs))

    def __len__(self):
        return len(self._sessions.briefs)

    def items(self):
        return [(s, v) for s in sorted(self._sessions.briefs) if (v := self._sessions._view(s)) is not None]

    def values(self):
        return [v for _, v in self.items()]


class SessionSet:
    """Analyses and briefs of every session, with full views loaded on demand. Built from views (offline tests and
    one-off checks: every view stays loaded) or from briefs with a loader (a run)."""

    def __init__(self, analyses, views=(), *, briefs=None, loader=None, household=None, assistant=None):
        self.analyses = {short(a["recording_id"]): a for a in analyses}
        self._loaded, self._loader = OrderedDict(), loader
        if views:
            self._loaded.update({short(a["recording_id"]): v for a, v in zip(analyses, views)})
            self.briefs = {short(a["recording_id"]): brief(v, a["recording_id"]) for a, v in zip(analyses, views)}
            household = max(views, key=lambda v: (bool(_replayable(v)), v.digest["started"]))
            assistant = next((v for v in sorted(views, key=lambda v: v.digest["started"], reverse=True)
                              if v.assistant), None)
        else:
            self.briefs = {b["session"]: b for b in briefs or [] if b["session"] in self.analyses}
        self.household, self.assistant_view = household, assistant
        self._pinned = {short(v.recording["id"]): v for v in (household, assistant) if v is not None}
        self.views = _Views(self)

    def _view(self, session):
        if session in self._loaded:
            self._loaded.move_to_end(session)
            return self._loaded[session]
        return self._pinned.get(session)

    async def load(self, session):
        """A session's full view, loaded if need be; None if there's no such session or it can't be read."""
        session = (session or "")[:8]
        if (view := self._view(session)) is not None:
            return view
        if session not in self.briefs or self._loader is None:
            return None
        try:
            view = await self._loader(self.briefs[session]["recording_id"])
        except (CaseError, ValueError):
            return None
        self._loaded[session] = view
        while len(self._loaded) > LOADED:
            self._loaded.popitem(last=False)
        return view

    def state(self, session, settled):
        """open_friction, friction_handled, without_friction or not_analysed."""
        from .hypotheses import friction_by_goal
        analysis = self.analyses.get(session) or {}
        goals = friction_by_goal(analysis)
        if goals:
            return "friction_handled" if all((session, t) in settled for t in goals) else "open_friction"
        return "not_analysed" if analysis.get("not_analysed") else "without_friction"

    def counts(self, settled):
        return dict(Counter(self.state(s, settled) for s in self.briefs))

    def page(self, settled, *, page=1, state=None, device=None, area=None, words=None, names=None):
        """One page of sessions, newest first, filtered: by state, a device (its entity_id, or a name: names maps
        entity ids to their names and aliases), an area, or words in the user's requests."""
        wanted = []
        needle = (words or "").casefold().strip()
        device_ids = _matching_devices(device, names) if device else None
        place = (area or "").casefold().strip()
        for session, b in sorted(self.briefs.items(), key=lambda kv: kv[1]["started"] or "", reverse=True):
            if state and state != "all" and self.state(session, settled) != state:
                continue
            if device_ids is not None and not (device_ids & set(b["entities"]) or any(
                    n in w for n in _names_of(device_ids, names) for w in b["words"])):
                continue
            if place and not any(place in w for w in b["words"]) and not any(place in r.casefold() for r in b["requests"]):
                continue
            if needle and not any(needle in r.casefold() for r in b["requests"]):
                continue
            wanted.append(session)
        pages = max(1, -(-len(wanted) // PAGE))
        page = min(max(1, page), pages)
        rows = [self.row(s, settled) for s in wanted[(page - 1) * PAGE:page * PAGE]]
        return {"sessions": rows, "page": page, "pages": pages, "total": len(wanted),
                **({"next": f"list_sessions with page {page + 1} (same filters)"} if page < pages else {})}

    def row(self, session, settled):
        b = self.briefs[session]
        requests = b["requests"]
        return {"session": session, "started": (b["started"] or "")[:16], "turns": b["turns"],
                "first_request": requests[0][:160] if requests else "",
                **({"more_requests": len(requests) - 1} if len(requests) > 1 else {}),
                "state": self.state(session, settled)}

    def where_it_applies(self, footprint, settled, exclude=()):
        """The sessions a change would apply to, without friction first, then newest: (rows, how many in all)."""
        found = []
        for session, b in self.briefs.items():
            if session in exclude:
                continue
            why = applies(b, footprint)
            if why:
                found.append((self.state(session, settled) == "open_friction", b["started"] or "", session, why))
        found.sort(key=lambda f: f[1], reverse=True)  # Newest first...
        found.sort(key=lambda f: f[0])  # ...within: sessions without open friction, then those with it.
        rows = [{**self.row(s, settled), "touches": why} for _, _, s, why in found[:APPLIES_SHOWN]]
        return rows, len(found)


def _replayable(view):
    try:
        return replayable_turns(view.recording)
    except CaseError:
        return []


def _matching_devices(device, names):
    device = device.strip()
    if "." in device and " " not in device:
        return {device}
    wanted = device.casefold()
    return {e for e, known in (names or {}).items() if any(wanted == n or wanted in n for n in known)}


def _names_of(entity_ids, names):
    return {n for e in entity_ids for n in (names or {}).get(e, ()) if len(n) >= 3}


def footprint(changes, household):
    """What a set of changes touches, for matching sessions: devices, their names and aliases (and new ones), their
    areas and domains, and whether it changes what Assist can see."""
    from .exploration import change_devices
    entities = set(change_devices(changes))
    names, areas = set(), set()
    for change in changes:
        for key in ("alias", "script_name"):
            if isinstance(change.get(key), str) and change[key].strip():
                names.add(change[key].casefold().strip().replace("_", " "))
    for entity_id in entities:
        names |= set(device_names(household, entity_id))
        try:
            card = household._card(entity_id)
        except (KeyError, AttributeError, TypeError):
            card = {}
        if card.get("area"):
            areas.add(str(card["area"]).casefold())
    return {"entities": entities, "names": {n for n in names if len(n) >= 3}, "areas": areas,
            "domains": {e.split(".", 1)[0] for e in entities},
            "exposure": any(c.get("kind") == "exposure" or (c.get("kind") == "script" and c.get("expose") is not False)
                            for c in changes)}


def device_names(household, entity_id):
    """A device's name and aliases, casefolded."""
    out = set()
    try:
        card = household._card(entity_id)
        if card.get("name"):
            out.add(str(card["name"]).casefold())
        out |= {str(a).casefold() for a in household.aliases(entity_id) or []}
    except (KeyError, AttributeError, TypeError):
        pass
    return out


def applies(b, fp):
    """Why a change could apply to a session (what of it the session's calls or requests touch), or None."""
    if fp["entities"] & set(b["entities"]):
        return "its devices"
    text = b["words"] + [r.casefold() for r in b["requests"]]
    if any(n in t for n in fp["names"] for t in text):
        return "their names"
    if any(a in w for a in fp["areas"] for w in b["words"]):
        return "their areas"
    if fp["exposure"] and (b["live_context"] or fp["domains"] & set(b["domains"])):
        return "what Assist can see"
    return None
