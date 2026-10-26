"""The sessions a role works over (sessions.py), and what the generator and the reviewer get upfront."""

import json

from ..cases import CaseError
from ..const import NO_FIX
from ..exploration import related_improvements
from ..failures import aside, contained
from ..sessions import SessionSet, footprint
from .assistants import Assistants
from .goals import (FRICTION_KINDS, capabilities, friction, friction_by_goal, goal_devices, goal_summary, scenario,
                    short)

# The open friction the generator gets as full scripts upfront (newest first), in characters and sessions; the
# rest of the open goals come one line each, and read_session reads any session.
SCENARIO_BUDGET, SCENARIOS_SHOWN = 24000, 12
CHOICES_SHOWN = 40  # Known choices (what the user picked when a setting was left open) the reviewer gets, newest.


class Sessions(Assistants, SessionSet):
    """The sessions a role works over (sessions.py: briefs at hand, full recordings loaded when opened), the
    assistants they used (assistants.py), and what the generator and the reviewer get upfront."""

    def __init__(self, analyses, views=(), **lazy):
        super().__init__(analyses, views, **lazy)
        self.evidence, self.capability_facts = None, None
        self.feedback = []  # The household's decisions on earlier improvements.
        # {(session, goal turn): title} of settled goals: an improvement's title, or NO_FIX (no fix was found for it).
        self.settled = {}
        self.existing = []  # Improvements waiting for review or applied (Improvements.catalog), for list_improvements.
        self._upfront = None  # The open friction the generator gets in full (prepare).

    async def load_evidence(self, definitions):
        """Facts from HA's own engine: what Assist can do with each device in open friction, and an Evidence for
        checking drafts. definitions: the household's scripts and automations, for recordings without them. Facts
        the household's conversation can't give are left out (each device's are unknown; a problem of the run's
        unless the sandbox said it can't); the rest goes on."""
        from ..evidence import Evidence, capabilities as probe
        self.evidence, self.capability_facts = Evidence(self, definitions), {}
        session = short(self.household.recording["id"])
        if aside("propose", session, step=PROBE):
            return
        async with contained("propose", session, step=PROBE, expected=(CaseError,)):  # CaseError: it can't.
            self.capability_facts = await probe(self.household.recording, self._entities_in_friction(), definitions)

    def open_friction(self):
        """[(session, its goal turns with friction no improvement handles)], newest first: what the generator has
        to place, each goal in a hypothesis, already_addressed or not_fixed."""
        out = []
        for session in sorted(self.analyses, key=lambda s: (self.briefs.get(s) or {}).get("started") or "", reverse=True):
            turns = [t for t in sorted(friction_by_goal(self.analyses[session])) if (session, t) not in self.settled]
            if turns:
                out.append((session, turns))
        return out

    async def prepare(self):
        """Load the sessions the generator gets in full: the newest open friction, within SCENARIO_BUDGET."""
        self._upfront = self._select(await self._views_of([s for s, _ in self.open_friction()[:SCENARIOS_SHOWN]]))

    async def _views_of(self, sessions):
        return {s: v for s in sessions if (v := await self.load(s)) is not None}

    def _select(self, views):
        chosen, used = [], 0
        for session, _ in self.open_friction():
            if session not in views or len(chosen) >= SCENARIOS_SHOWN:
                continue
            script = scenario_of(self.analyses[session], views[session])
            if script is None:  # Its analysis and recording don't read together: one line in more_friction instead.
                continue
            if assistant := self.assistant_of(session):  # Which assistant's instructions it ran with.
                script["assistant"] = assistant["name"]
            size = len(json.dumps(script, ensure_ascii=False))
            if chosen and used + size > SCENARIO_BUDGET:
                break
            chosen.append(script)
            used += size
        return chosen

    def _goal_line(self, session, turn):
        analysis = self.analyses[session]
        return {"turn": turn, "summary": goal_summary(analysis, turn),
                "friction": sorted({f["kind"] for f in friction_by_goal(analysis).get(turn, [])})}

    def overview(self):
        """Background; the newest open friction as scripts (within a budget) and the rest of it one line per goal;
        what's settled and decided; counts, and how to reach every other session."""
        upfront = self._upfront if self._upfront is not None else self._select(
            {s: v for s, _ in self.open_friction() if (v := self.views.get(s)) is not None})
        shown = {u["session"] for u in upfront}
        more = [{"session": s, "started": ((self.briefs.get(s) or {}).get("started") or "")[:16],
                 "goals_with_friction": [self._goal_line(s, t) for t in turns]}
                for s, turns in self.open_friction() if s not in shown]
        counts = self.counts(self.settled)
        return {"home": self.household.digest["home"], "time_zone": self.household.digest["time_zone"],
                "areas": self.household.list_areas(), "assist_tools": self.assist_tools(),
                **({"assistants": rows} if (rows := self.assistant_rows()) else {}),
                "devices_in_friction": self.devices_in_friction(),
                "friction_scenarios": upfront,
                **({"more_friction": more} if more else {}),
                "other_sessions": (f"{counts.get('friction_handled', 0)} sessions whose friction already has an "
                                   f"improvement (or no fix), {counts.get('without_friction', 0)} without friction, "
                                   f"{counts.get('not_analysed', 0)} not analysed: list_sessions finds them"),
                **({"household_decisions": decisions(self.feedback)} if self.feedback else {}),
                **({"already_handled": handled} if (handled := [
                    {"session": s, "goal_turn": t, "improvement": title} for (s, t), title in sorted(self.settled.items())
                    if title != NO_FIX]) else {}),
                **({"related_improvements": related} if (related := self.related(
                    [(s, t) for s, turns in self.open_friction() for t in turns])) else {})}

    def related(self, goals, devices=()):
        """Existing improvements (waiting for review, or applied) that touch the same devices as these goals
        ([(session, goal turn)]) or the given devices: found by code, for the roles to judge whether one already
        addresses them. Each lists the goals it may address and the devices they share."""
        wanted = {f"{s}:{t}": goal_devices(self.analyses[s], t) for s, t in goals if s in self.analyses}
        return related_improvements(self.existing, set().union(*wanted.values(), devices) if wanted else set(devices),
                                    wanted)

    def listing(self, **filters):
        """One page of sessions (list_sessions), newest first."""
        return self.page(self.settled, names=self._names(), **filters)

    def _names(self):
        from ..sessions import device_names
        return {e: device_names(self.household, e) for e in self.household._entities()}

    def assist_tools(self):
        """Assist's tools, from the newest session that recorded them (a locally handled one has none)."""
        for b in sorted(self.briefs.values(), key=lambda b: b["started"] or "", reverse=True):
            if b["assist_tools"]:
                return b["assist_tools"]
        return []

    def _entities_in_friction(self):
        """The devices in open friction: those its goals name, and those its sessions' calls touched."""
        entities, known = set(), set(self.household._entities())
        for session, _ in self.open_friction():
            for goal in self.analyses[session].get("goals", []):
                entities.update([goal["entity_id"]] if goal.get("entity_id") else goal.get("entity_ids") or [])
            entities.update((self.briefs.get(session) or {}).get("entities", []))
        return sorted(entities & known)

    def devices_in_friction(self):
        """What Assist can and can't do with each device in open friction, so causes are checkable."""
        return [capabilities(self.household, e, self.capability_facts) for e in self._entities_in_friction()]

    def goal_friction(self, hypothesis):
        """The friction of each goal a draft names, by kind (FRICTION_KINDS says what each means), for the reviewer
        to check the draft removes all of it."""
        out = []
        for ref in hypothesis.get("goals") or []:
            analysis = self.analyses.get(str(ref.get("session"))[:8])
            if analysis is not None:
                items = friction_by_goal(analysis).get(ref.get("goal_turn"), [])
                out.append({"session": ref["session"], "goal_turn": ref["goal_turn"],
                            "summary": goal_summary(analysis, ref["goal_turn"]), "friction": items})
        kinds = {f["kind"] for g in out for f in g["friction"]}
        return {"goals": out, "kinds": {k: v for k, v in FRICTION_KINDS.items() if k in kinds}}

    def where_it_applies_to(self, hypothesis):
        """The sessions a draft's changes would apply to, besides its own goals' (sessions.where_it_applies)."""
        mine = {g["session"][:8] for g in hypothesis.get("goals") or []}
        rows, total = self.where_it_applies(footprint(self.with_targets(hypothesis.get("changes")), self.household),
                                            self.settled, exclude=mine)
        out = {"sessions": rows, "count": total}
        if total > len(rows):
            out["more"] = f"{total - len(rows)} more: list_sessions filtered by a device or area finds them"
        return out

    def summary(self):
        """For the reviewer: the home, how many sessions there are by state, the user's known choices (newest
        first) and the household's decisions. Each draft brings the sessions where it would apply."""
        chosen = [{"session": s, "started": ((self.briefs.get(s) or {}).get("started") or "")[:16],
                   **{k: f[k] for k in ("summary", "user_chose")}}
                  for s, a in self.analyses.items() for f in friction(a) if f["kind"] == "ask"]
        chosen.sort(key=lambda c: c["started"], reverse=True)
        return {"home": self.household.digest["home"], "sessions": self.counts(self.settled),
                **({"assistants": rows} if (rows := self.assistant_rows()) else {}),
                "known_choices": chosen[:CHOICES_SHOWN],
                **({"more_known_choices": len(chosen) - CHOICES_SHOWN} if len(chosen) > CHOICES_SHOWN else {}),
                **({"household_decisions": decisions(self.feedback)} if self.feedback else {})}


PROBE = "capability probe"  # What load_evidence tries on the household's conversation (failures.contained's step).


def scenario_of(analysis, view):
    """A session as a script (goals.scenario), or None when its analysis and recording don't read together: a
    problem of the run's, and the generator gets the session one line per goal instead."""
    with contained("propose", short(analysis.get("recording_id") or ""), step="scenario", setback=False):
        return scenario(analysis, view)
    return None


def decisions(feedback):
    """The household's decisions, as the roles need them to respect them: what was decided on which change (the
    lines in full, the target of anything else), with the note; read_improvement shows an improvement in full."""
    out = []
    for item in feedback:
        changes = []
        for change in item.get("changes") or []:
            what = {"kind": change.get("kind")}
            for key in ("text", "for", "assistant", "entity_id", "alias", "script_name", "expose"):
                if change.get(key) not in (None, "", []):
                    what[key] = change[key]
            changes.append(what)
        out.append({"title": item.get("title"), "decision": item.get("decision"), "status": item.get("status"),
                    **({"note": item["note"]} if item.get("note") else {}), "changes": changes})
    return out
