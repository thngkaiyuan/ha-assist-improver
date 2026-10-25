"""Hypothesis generator and reviewer roles.

The generator reads the friction across analysed sessions (extra turns with the
sub-goals they served, settings the user had to choose, side effects left
behind), groups it into themes and proposes one coherent change per theme,
preferring the least specific change that works. It answers by calling
submit_hypotheses, which checks every hypothesis against the sessions and the
household and rejects it with the reasons until it is valid. The reviewer is a
separate role: it keeps, revises or drops one hypothesis, with a regression
check against a summary of the household's sessions, and answers through
submit_review. Both are agentic, with read-only tools over the recordings.
"""

import asyncio
import json

import probatio
from homeassistant.helpers import llm

from .analyzer import top_level
from .candidates import live_script_definitions
from .cases import CaseError
from .exploration import (TOOLS, DeviceDetails, DevicesInArea, ListImprovements, ReadImprovement, ReadTurn,
                          SessionAPI, _SessionTool, applied_too_early, change_devices, related_improvements)
from .reflective import run_role
from .sessions import STATES, SessionSet, footprint
from .trace import span

GENERATOR_ROLE = "hypothesis_generator"
REVIEWER_ROLE = "hypothesis_reviewer"
# How often the generator may ask the reviewer, per run and per idea.
REVIEWS_PER_RUN, REVIEWS_PER_IDEA = 15, 5
# The open friction the generator gets as full scripts upfront (newest first), in characters and sessions; the
# rest of the open goals come one line each, and read_session reads any session.
SCENARIO_BUDGET, SCENARIOS_SHOWN = 24000, 12
CHOICES_SHOWN = 40  # Known choices (what the user picked when a setting was left open) the reviewer gets, newest.
KINDS = ("preference", "prompt", "alias", "exposure", "script")

# Foundational Home Assistant knowledge both roles need, given upfront. Generic, not about any household; checked
# against the HA 2026.10 sources.
HA_PRIMER = """How Home Assistant's Assist works (Home Assistant 2026.10):
- Exposure: Assist (the AI) only sees and controls entities exposed to it. An unexposed device still exists, and a script can control it, but Assist can't name, read or change it directly, and may say it doesn't exist.
- Assist's tools are fixed per device type: turn on or off (also opens and closes covers and valves, locks and unlocks locks, presses buttons, runs scripts and scenes); light brightness and colour; climate target temperature; fan speed; cover position; media pause, skip and volume; vacuum start, return and clean an area; timers, to-do lists and broadcasts; live state reading. There is no tool to choose an option on a select (such as a seat heater level), set a number, or change a climate or fan mode: exposing such a device doesn't make it settable. A script can do it.
- Turning a climate device on resumes its last mode; a target temperature can be set; its mode (heat, cool, fan only) can't be chosen by Assist.
- Scripts: each exposed script becomes its own Assist tool, named after the script and described by its description and aliases. Its steps run regardless of what is exposed. A step calls an action on a device, with its data, for example select.select_option with option: high, climate.set_hvac_mode with hvac_mode: fan_only, switch.turn_on, or script.turn_on to run another script. Scripts proposed here take no parameters, so each sets fixed values. One script per occasion solves that occasion only; a preference that teaches Assist to use existing tools or scripts generalises better.
- Names and aliases: Assist finds devices by name, alias and area. Two exposed devices with the same name or alias make requests ambiguous and can fail. Aliases are extra names on a device.
- Preferences: household facts and preferences go into the household knowledge, lines every assistant reads with its instructions on every request (so a line applies to every conversation, whichever assistant handles it): lines for everyone, and lines for one person (this_user), which only that person's requests see. It's where household facts and preferences go: what the user means by a word, which device they usually mean, how a device is operated. Each assistant also has its own instructions (a template), which may already hold such lines from before; a change can replace those too.
- Momentary relays: some switches act like buttons (a door opener moves when its relay goes off, then on). Switching such a relay doesn't report the door's position.
- Local handling: with "prefer handling locally" on, simple commands that match Home Assistant's own sentences are handled without the AI, so instruction changes don't affect them.

Home Assistant's own published best practices (home-assistant.io, voice_control pages, 2026.10):
- Expose only the bare minimum used by voice: more names make matching slower and AI requests costlier. Exposure is opt-in so that sensitive devices such as locks and garage doors aren't controlled by accident.
- Name entities "<area> [<descriptor>] [<domain>]" (e.g. "Living room lamp"); put devices in areas and areas on floors, since people ask by room ("turn off the office lights").
- Add an alias wherever people naturally say something else ("TV" for "Television"); area-less aliases ("Lamp") work, and a shared alias is matched together with the area.
- The domain sets the verbs: a switch called "main valve" can't be opened by voice, but a valve can. Change the device type (switch_as_x, or a template entity) when a switch is really a valve, cover, lock, fan or light, and set a proper device class (a window sensor needs device class window to answer "is it open?").
- A script's description is the most important part of exposing it: say what it does, when to use it, and what to tell the user afterwards; keep it concise. Describe each field (what, format, limits). End with a stop action carrying a success or error message, so the AI doesn't make up a response. At most 128 exposed scripts and tools in total."""

GENERATOR_INSTRUCTIONS = """You help one household get the most out of Home Assistant's voice assistant ("Assist"). How Assist works is summarised at the end. Your input names the home, its areas and Assist's tools, and what Assist can and can't do with each device involved below (what Home Assistant carried out when each of Assist's tools was tried on it).

The user seems to be encountering friction in the scenarios in your input: the newest sessions whose friction no improvement handles yet. Each is a session written as a script, turn by turn: what the user and Assist said, the user's goals marked at the turn they started, and the friction marked where it happened: extra turns the user spent on a goal (with what each served) and settings the user's words left open (with what the user chose). Side effects Assist left behind are listed too. more_friction lists the other goals with friction no improvement handles, one line each: read_session shows any of them in full. list_sessions finds every other session, a page at a time, filtered by state (open_friction, friction_handled, without_friction, not_analysed), device, area or words in the user's requests; read_session reads one, read_session_turn shows a turn's tool calls and state changes, and the other tools show the household. They answer from the recordings, except read_script, which reads a script's current definition.

Your task: propose personalisations to the user's Home Assistant setup that make their long-horizon experience with Assist as seamless as possible. The friction is your evidence; aim for changes that keep helping in future requests, not just in these sessions.

How to work:
1. Find the causes, with the tools. Then check whether an existing improvement already addresses them: your input's related_improvements lists those (waiting for the household's review, or already applied) that touch the same devices as goals with friction, list_improvements lists them all, and read_improvement shows one in full (its changes, and the conversations it was tested on). A goal whose friction one of them already addresses goes in already_addressed, with that improvement and why, instead of into a new idea: there's no need to fix it twice. An improvement applied before the session took place didn't stop its friction, so it doesn't address it.
2. Group goals whose friction has the same cause into one theme, across sessions. For each theme, form one coherent idea: the change that would have got those goals done without friction. Name the goals it fixes (session and goal turn); all of a goal's friction counts as removed. Prefer the least specific change that works, in this order:
   - prompt: make Assist itself reason better, written as a general behaviour instruction. This is in scope and often the most general fix: how it works out what the user means (from devices' current states, the time, what the user is doing or just did), when to act and when to ask, checking its own results, and stating them firmly when the data supports it. It applies to every request, so weigh what else it could change;
   - preference: a fact about this household or the user's preferences, written as a line for Assist's instructions (what the user meant by a word, which devices they mean, how a device is operated);
   - alias (a name for a device), exposure (letting Assist see and control a device), or the household's other setup;
   - script: a new script, only when nothing above would work.
   An idea may combine several changes when they serve the same theme, such as a preference and the exposure it relies on.
   Prefer the simplest rule that fits every session, not only the ones it fixes. Test each rule against all the sessions where it would apply, including sessions without friction: check_hypothesis lists the sessions whose recorded calls or requests touch your change's devices, names or areas (sessions without friction first), and list_sessions with read_session finds more. A rule that explains them all in one idea beats one with special cases. A special case, or a rule built from a single session, needs a reason the sessions show. Record what you found: in support, the sessions the rule agrees with; in conflicts, any it contradicts.
3. Read what is already there before changing it: Assist's current instructions and the household knowledge (read_assistant_instructions), a device's aliases, a script's steps. Prefer editing what exists over adding to it: rewrite or merge existing lines into a more general one (give the lines a change replaces in replaces), replace an alias, rework a script.
4. Check each idea with Home Assistant itself (check_hypothesis): your scripts run in a sandbox of the recorded home from the state each goal started in, showing whether the goal's devices end up as the goal wants, and new aliases are matched the way Assist matches names. It also replays the recorded tool calls of every session your change could affect (those whose calls touch its devices, their names or areas) and lists those that now resolve differently: weigh each against what the user wanted there; it may be a problem, or expected when a more general change covers what the old call did.
5. Discuss each idea with the reviewer, a separate model, using ask_reviewer: send it a draft hypothesis, and it answers with review comments backed by household evidence; any blocking comment requests changes. Take its points seriously: revise, or find another fix for the same goals, and ask again, answering each blocking comment (resolved, or wont_fix with a note).
6. Implement: make the changes concrete and submit all hypotheses with submit_hypotheses. Every goal with friction needs a fix in some hypothesis, a place in already_addressed (an existing improvement covers it), or a place in not_fixed with why its friction is outside the assistant's control. The submit tool checks this and everything else against the sessions and the household; if it reports problems, fix them and submit again. When it accepts your answer, finish with one short sentence.

Your input may include household_decisions: the household's decisions on earlier improvements. Never propose again what they declined or said was fine as it was; when they asked for a revision, address their note; build on what they accepted. Goals in already_handled already have an improvement (waiting for review, applied or decided): leave them out.

Respect the timing and separation of the user's requests: what they asked for separately, at different times, stays separate; what they asked for together can be done together. Speech recognition mishearing the user is in scope too, when the sessions show it happened (for example, an alias for a name that's often misheard). Use only devices and scripts the tools show you.

""" + HA_PRIMER

REVIEWER_INSTRUCTIONS = """You review one proposed change to a household's Home Assistant voice assistant ("Assist"), before any money is spent testing it, the way a careful code reviewer reviews a pull request. The change may be a draft the generator is still working on, or a finished hypothesis; review it the same way. You get the hypothesis (the goals whose friction it targets, its cause and its changes), the sessions where it would apply (those whose recorded calls or requests touch its devices, names or areas; sessions without friction first, since that's where a rule could misfire on requests that already work), the household's known choices and decisions, and, when the generator asks again, your previous review with its responses. list_sessions finds any other session (filter by state, device, area or words), read_session reads one, and the other tools explore the household.

Write your review as comments in the Conventional Comments style. Each comment has:
- label: issue (a problem the change has), suggestion (a concrete improvement), todo (a small necessary change), nitpick (trivial, preference-level), question (something you need answered), thought (an idea to consider), or praise (something done well);
- decorations: blocking if the change shouldn't go ahead until this is addressed, otherwise non-blocking;
- subject: the comment in one sentence;
- discussion: why, and what you'd do instead;
- evidence: the household facts that support it: sessions and turns (what the user said or did there), devices (names, aliases, exposure, states), or patterns across sessions (as a note). Every comment except praise needs at least one; a comment without household evidence is opinion.
Test every rule against all the sessions where it would apply, not only the ones it fixes, and check the hypothesis's support and conflicts against them. Prefer the simplest rule consistent with all of them; a rule with special cases, or one resting on a single session, needs a reason the sessions show, and a simpler rule that fits as well is a blocking comment with those sessions as evidence. Each draft comes with checks from Home Assistant itself, run in a sandbox of the recorded home: implementation (whether each script, run from the state its goal started in, leaves the goal's devices as the goal wants; what a new alias matches) and resolution_signals (recorded tool calls that resolve differently with the change). Use them as evidence. A call that resolves differently is a signal to weigh, not proof of a regression: it may misfire (a name now matching another device), or be expected (a more general script covering what the old call did); judge it against what the user wanted in that session. Look for: a wrong cause (including a claimed mishearing that the sessions don't show); combining requests the user made separately, at different times; a change that would break, confuse or misfire on other things the household does, or won't carry over to the cases it should cover; and a simpler, more general or more polished version. The outcome follows from the comments: any blocking comment requests changes, otherwise the change is approved with your comments. So mark a comment blocking only when the evidence supports it, and put a revised hypothesis in revised when you know the better version. When you get responses to your previous comments, check each: comment again if a resolution didn't resolve it, and don't repeat comments that were resolved.

A hypothesis's changes: preference and prompt lines say who they're for (everyone, or this_user: that person's own lines in the household knowledge); an exposure change exposes a device unless expose is false, which hides it; a script change makes a new script (script_name, steps; exposed to Assist unless expose is false) or edits an existing one (its entity_id, with a new description in text and/or new steps), and its steps may end with a stop step carrying the message for the user.

Your input may include household_decisions: the household's decisions on earlier improvements. A proposal that repeats what they declined or said was fine as it was, or ignores their revision note, gets a blocking comment.

Check existing improvements too: a draft lists related_improvements (those waiting for the household's review, or already applied, that touch the same devices), list_improvements lists them all, and read_improvement shows one in full. A change that repeats or overlaps one of them gets a blocking comment naming it: its goals belong in already_addressed, or the change should build on that improvement.

Answer only by calling submit_review. When it accepts your answer, finish with one short sentence.

""" + HA_PRIMER

REVIEWER_LOOP = """
You review drafts the generator sends while it works, in one conversation, so you keep what you've learnt from one review to the next. Call wait_for_draft to get the next draft (it waits until one arrives); review it and call submit_review; then call wait_for_draft again. A draft that follows up an earlier one about the same idea carries the generator's responses to your blocking comments. Stop when wait_for_draft says you're finished."""

_CHANGE = {
    probatio.Required("kind"): probatio.In(list(KINDS)),
    probatio.Optional("text"): str,
    probatio.Optional("for"): probatio.In(["everyone", "this_user"]),
    probatio.Optional("replaces"): [str],
    probatio.Optional("entity_id"): str,
    probatio.Optional("alias"): str,
    probatio.Optional("expose"): bool,
    probatio.Optional("script_name"): str,
    probatio.Optional("steps"): [probatio.Any(
        {probatio.Required("service"): str, probatio.Required("entity_id"): str,
         probatio.Optional("data"): {str: probatio.Any(str, int, float, bool)}},
        {probatio.Required("stop"): str})],  # A closing stop step: the message Assist relays (HA's advice).
}
_EVIDENCE_REF = {probatio.Required("session"): str, probatio.Optional("turn"): int, probatio.Required("note"): str}
_HYPOTHESIS = {
    probatio.Required("title"): str,
    probatio.Required("goals"): [{probatio.Required("session"): str, probatio.Required("goal_turn"): int}],
    probatio.Required("cause"): str,
    probatio.Required("changes"): [_CHANGE],
    # The sessions the rule agrees with, and any it contradicts (each with what happened there).
    probatio.Required("support"): [_EVIDENCE_REF],
    probatio.Required("conflicts"): [_EVIDENCE_REF],
    probatio.Required("expected"): str,
}
HYPOTHESES = probatio.Schema({
    probatio.Required("hypotheses"): [_HYPOTHESIS],
    # Goals with friction left unfixed, each with why the friction is outside the assistant's control.
    probatio.Optional("not_fixed"): [{probatio.Required("session"): str, probatio.Required("goal_turn"): int,
                                      probatio.Required("why"): str}],
    # Goals with friction an existing improvement (waiting for review, or applied) already addresses.
    probatio.Optional("already_addressed"): [{probatio.Required("session"): str, probatio.Required("goal_turn"): int,
                                              probatio.Required("improvement"): str, probatio.Required("why"): str}],
})
# Review comments in the Conventional Comments style (conventionalcomments.org), outcomes as in GitHub reviews.
LABELS = ("issue", "suggestion", "todo", "nitpick", "question", "thought", "praise")
_EVIDENCE = {probatio.Optional("session"): str, probatio.Optional("turn"): int, probatio.Optional("entity_id"): str,
             probatio.Optional("note"): str}
REVIEW = probatio.Schema({
    probatio.Required("comments"): [{
        probatio.Required("label"): probatio.In(list(LABELS)),
        probatio.Required("decorations"): [probatio.In(["blocking", "non-blocking"])],
        probatio.Required("subject"): str,
        probatio.Optional("discussion"): str,
        probatio.Optional("evidence"): [_EVIDENCE],
    }],
    probatio.Optional("revised"): _HYPOTHESIS,
})


def blocking(review):
    return [c for c in (review or {}).get("comments", []) if "blocking" in c.get("decorations", [])]


def verdict(review):
    """A review's outcome, derived from its comments so the two can't disagree: request_changes or approve."""
    return "request_changes" if blocking(review) else "approve"


def short(recording_id):
    return recording_id[:8]


TRIAGED_NOTE = ("A quick check found every request here fulfilled smoothly, so its goals weren't extracted; read its "
                "turns for what was asked and what Assist did.")
UNANALYSED_NOTE = ("Not analysed, so its goals and friction weren't extracted; read its turns for what was asked and "
                   "what Assist did.")


def friction(analysis):
    """The turns with friction in one analysed session: extra turns (with the sub-goals they served) and asks."""
    goals = analysis.get("goals", [])
    out = []
    for turn in analysis.get("extra_turns", []):
        served = [g for g in goals if g.get("serves") is not None and g["turn"] == turn]
        out.append({"turn": turn, "kind": "extra turn",
                    "served": [{"summary": g["summary"], "for_goal_at_turn": g["serves"]} for g in served]})
    for goal in goals:
        if goal["kind"] == "ask" and goal.get("serves") is None:
            out.append({"turn": goal["turn"], "kind": "ask", "left_open": goal.get("entity_ids", []),
                        "summary": goal["summary"], "user_chose": goal.get("user_chose", [])})
    return sorted(out, key=lambda f: f["turn"])


def goal_devices(analysis, turn):
    """The devices a goal (and the sub-goals serving it) wanted something from."""
    found = set()
    for goal in analysis.get("goals", []):
        if goal["turn"] == turn or goal.get("serves") == turn:
            found.update([goal["entity_id"]] if goal.get("entity_id") else goal.get("entity_ids") or [])
    return found


def friction_by_goal(analysis):
    """Each goal turn that had friction, with that friction. Stage 3 settled which goal a turn served: an extra
    turn belongs to the goal its sub-goal serves, or else to the latest goal before it; an ask is its own."""
    tops = sorted({g["turn"] for g in top_level(analysis.get("goals", []))})
    groups = {}
    for item in friction(analysis):
        if item["kind"] == "ask":
            owner = item["turn"]
        else:
            served = [s["for_goal_at_turn"] for s in item["served"]]
            earlier = [t for t in tops if t < item["turn"]]
            owner = served[0] if served else (earlier[-1] if earlier else item["turn"])
        groups.setdefault(owner, []).append(item)
    return groups


def removes(sessions, hypothesis):
    """The (session, turn) friction a hypothesis removes: every friction turn of each goal it names."""
    out = set()
    for ref in hypothesis.get("goals", []):
        analysis = sessions.analyses.get(ref["session"])
        if analysis:
            out |= {(ref["session"], f["turn"]) for f in friction_by_goal(analysis).get(ref["goal_turn"], [])}
    return out


def scenario(analysis, view):
    """One session as a script: each turn's words, the goals it started, and the friction it carried."""
    goals = analysis.get("goals", [])
    owner = {f["turn"]: t for t, items in friction_by_goal(analysis).items() for f in items if f["kind"] != "ask"}
    turns = []
    zone = view.digest.get("time_zone")
    for line in view.conversation():
        n = line["turn"]
        turn = {"turn": n, "at": _local(line["at"], zone), "user": line["user_says"].strip(),
                "assist": line["assistant_says"]}
        heard = view.heard_by[n - 1] if n <= len(view.heard_by) else None
        device = (view.source.get("devices", {}).get(heard or "") or {}) if heard else {}
        if device:
            turn["heard_by"] = device.get("name_by_user") or device.get("name")
        if line.get("assist_acted_on"):
            turn["assist_acted_on"] = line["assist_acted_on"]
        started = [{k: g[k] for k in ("summary", "kind", "entity_id", "state", "states", "brightness_pct", "reached")
                    if k in g} for g in top_level(goals) if g["turn"] == n and g["kind"] != "ask"]
        if started:
            turn["goals_started"] = started
        asks = [{"left_open": g.get("entity_ids", []), "summary": g["summary"], "user_chose": g.get("user_chose", [])}
                for g in top_level(goals) if g["turn"] == n and g["kind"] == "ask"]
        if asks:
            turn["settings_left_open"] = asks
        if n in owner:
            turn["extra_turn_for_goal_at_turn"] = owner[n]
            served = [g["summary"] for g in goals if g.get("serves") is not None and g["turn"] == n]
            if served:
                turn["served"] = served
        turns.append(turn)
    out = {"session": short(analysis["recording_id"]), "turns": turns,
           "goals_with_friction": sorted(friction_by_goal(analysis))}
    if analysis.get("triaged"):  # A quick check found it smooth, so the analyzer didn't extract its goals.
        out["not_analysed"] = TRIAGED_NOTE
    elif analysis.get("not_analysed"):
        out["not_analysed"] = UNANALYSED_NOTE
    if analysis.get("side_effects_left"):
        out["side_effects_left"] = analysis["side_effects_left"]
    return out


def _local(at, zone):
    """A turn's time in the household's time zone, as people read it (weekday and time)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        moment = datetime.fromisoformat(at).astimezone(ZoneInfo(zone)) if zone else datetime.fromisoformat(at)
    except (ValueError, KeyError):
        return at
    return moment.strftime("%a %Y-%m-%d %H:%M")


# What Assist can do with an exposed device, by domain, as (tool name, what it does); see HA_PRIMER.
def capabilities(view, entity_id, found):
    """What Assist can and can't do with one device: what HA carried out when each Assist tool was tried on it."""
    card = view._card(entity_id)
    out = {"entity_id": entity_id, "name": card["name"], "area": card.get("area"), "exposed": card.get("exposed"),
           "aliases": view.aliases(entity_id)}
    facts = (found or {}).get(entity_id)
    if facts is None:
        out["assist_can"] = "unknown"
    elif not facts["exposed"]:
        out["assist_can"] = "nothing directly: it isn't exposed (a script could control it)"
    else:
        out["assist_can"] = [f"{c['what']} ({c['tool']})" for c in facts["assist_can"]] or [
            "nothing: no Assist tool carried out a command on it (a script could)"]
    return out


class Sessions(SessionSet):
    """The sessions a role works over (sessions.py: briefs at hand, full recordings loaded when opened), and what
    the generator and the reviewer get upfront."""

    def __init__(self, analyses, views=(), **lazy):
        super().__init__(analyses, views, **lazy)
        self.evidence, self.capability_facts = None, None
        self.feedback = []  # The household's decisions on earlier improvements.
        self.settled = {}  # {(session, goal turn): title} of goals that already have an improvement.
        self.existing = []  # Improvements waiting for review or applied (Improvements.catalog), for list_improvements.
        self._upfront = None  # The open friction the generator gets in full (prepare).

    async def load_evidence(self, definitions):
        """Facts from HA's own engine: what Assist can do with each device in open friction, and an Evidence for
        checking drafts. definitions: the household's scripts and automations, for recordings without them."""
        from .evidence import Evidence, capabilities as probe
        self.evidence = Evidence(self, definitions)
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
            script = scenario(self.analyses[session], views[session])
            size = len(json.dumps(script, ensure_ascii=False))
            if chosen and used + size > SCENARIO_BUDGET:
                break
            chosen.append(script)
            used += size
        return chosen

    def _goal_line(self, session, turn):
        goals = top_level(self.analyses[session].get("goals", []))
        return {"turn": turn, "summary": "; ".join(dict.fromkeys(g["summary"] for g in goals if g["turn"] == turn))}

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
                "devices_in_friction": self.devices_in_friction(),
                "friction_scenarios": upfront,
                **({"more_friction": more} if more else {}),
                "other_sessions": (f"{counts.get('friction_handled', 0)} sessions whose friction already has an "
                                   f"improvement, {counts.get('without_friction', 0)} without friction, "
                                   f"{counts.get('not_analysed', 0)} not analysed: list_sessions finds them"),
                **({"household_decisions": decisions(self.feedback)} if self.feedback else {}),
                **({"already_handled": [{"session": s, "goal_turn": t, "improvement": title}
                                        for (s, t), title in sorted(self.settled.items())]} if self.settled else {}),
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
        from .sessions import device_names
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

    def where_it_applies_to(self, hypothesis):
        """The sessions a draft's changes would apply to, besides its own goals' (sessions.where_it_applies)."""
        mine = {g["session"][:8] for g in hypothesis.get("goals") or []}
        rows, total = self.where_it_applies(footprint(hypothesis.get("changes") or [], self.household),
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
                "known_choices": chosen[:CHOICES_SHOWN],
                **({"more_known_choices": len(chosen) - CHOICES_SHOWN} if len(chosen) > CHOICES_SHOWN else {}),
                **({"household_decisions": decisions(self.feedback)} if self.feedback else {})}


def decisions(feedback):
    """The household's decisions, as the roles need them to respect them: what was decided on which change (the
    lines in full, the target of anything else), with the note; read_improvement shows an improvement in full."""
    out = []
    for item in feedback:
        changes = []
        for change in item.get("changes") or []:
            what = {"kind": change.get("kind")}
            for key in ("text", "for", "entity_id", "alias", "script_name", "expose"):
                if change.get(key) not in (None, "", []):
                    what[key] = change[key]
            changes.append(what)
        out.append({"title": item.get("title"), "decision": item.get("decision"), "status": item.get("status"),
                    **({"note": item["note"]} if item.get("note") else {}), "changes": changes})
    return out


def problems(sessions, answer, schema=HYPOTHESES):
    """What is wrong with a generator answer, checked against the sessions and the household; empty means valid."""
    try:
        schema(answer)
    except probatio.Invalid as err:
        return [f"The answer does not match the format: {err}"]
    known, found = set(sessions.household._entities()), []
    template = known_lines(sessions)
    items = answer["hypotheses"] if schema is HYPOTHESES else [answer["revised"]] if answer.get("revised") else []
    for n, hypothesis in enumerate(items, 1):
        where = f"Hypothesis {n} ({hypothesis['title']!r})"
        for ref in hypothesis["goals"]:
            analysis = sessions.analyses.get(ref["session"])
            if analysis is None:
                found.append(f"{where}: there is no session {ref['session']!r}; list_sessions shows them.")
            elif ref["goal_turn"] not in friction_by_goal(analysis):
                found.append(f"{where}: session {ref['session']} has no goal with friction at turn {ref['goal_turn']}; "
                             f"its goals with friction start at turns {sorted(friction_by_goal(analysis)) or 'none'}.")
            elif (title := sessions.settled.get((ref["session"], ref["goal_turn"]))) is not None:
                found.append(f"{where}: {ref['session']}:{ref['goal_turn']} already has an improvement ({title!r}); "
                             "leave it out.")
        if not hypothesis["goals"]:
            found.append(f"{where}: name the goals it fixes (session and goal turn).")
        if not hypothesis["support"]:
            found.append(f"{where}: list in support the sessions the rule agrees with.")
        for field in ("support", "conflicts"):
            for ref in hypothesis[field]:
                known_session = sessions.briefs.get(ref["session"])
                if known_session is None:
                    found.append(f"{where}: {field} names no session {ref['session']!r}.")
                elif ref.get("turn") is not None and not 1 <= ref["turn"] <= known_session["turns"]:
                    found.append(f"{where}: {field}: session {ref['session']} has no turn {ref['turn']}.")
        for change in hypothesis["changes"]:
            found += _change_problems(where, change, known, sessions.household, template)
    if schema is HYPOTHESES:
        found += _addressed_problems(sessions, answer) + _open_goals(sessions, answer)
    return found


def _addressed_problems(sessions, answer):
    """already_addressed, checked: an improvement list_improvements shows, a goal with friction no hypothesis also
    fixes, and an improvement applied only after the session (one applied before it didn't stop the friction)."""
    existing, found = {i["improvement"]: i for i in sessions.existing}, []
    fixed = {(r["session"], r["goal_turn"]) for h in answer.get("hypotheses", []) for r in h["goals"]}
    for ref in answer.get("already_addressed") or []:
        where, item = f"already_addressed {ref['session']}:{ref['goal_turn']}", existing.get(ref["improvement"])
        analysis, known_session = sessions.analyses.get(ref["session"]), sessions.briefs.get(ref["session"])
        if analysis is None or ref["goal_turn"] not in friction_by_goal(analysis):
            found.append(f"{where}: there's no goal with friction there.")
        elif item is None:
            found.append(f"{where}: there's no improvement {ref['improvement']!r}; list_improvements shows "
                         f"{', '.join(sorted(existing)) or 'none'}.")
        elif (ref["session"], ref["goal_turn"]) in fixed:
            found.append(f"{where}: a hypothesis fixes it too; keep it in one place.")
        elif not ref["why"].strip():
            found.append(f"{where}: say why {item['title']!r} addresses it.")
        elif known_session is not None and (why := applied_too_early(item, known_session["started"])):
            found.append(f"{where}: {why} Fix it, or list it in not_fixed with why.")
    return found


def open_goals(sessions, answer):
    """Goals with friction that neither a hypothesis nor not_fixed addresses."""
    addressed = {(r["session"], r["goal_turn"]) for h in answer.get("hypotheses", []) for r in h["goals"]}
    addressed |= {(r["session"], r["goal_turn"]) for r in answer.get("not_fixed") or [] if r["why"].strip()}
    addressed |= {(r["session"], r["goal_turn"]) for r in answer.get("already_addressed") or [] if r["why"].strip()}
    return [(s, turn) for s, analysis in sorted(sessions.analyses.items())
            for turn in sorted(friction_by_goal(analysis)) if (s, turn) not in addressed and (s, turn) not in sessions.settled]


def _open_goals(sessions, answer):
    left = open_goals(sessions, answer)
    if not left:
        return []
    names = [f"{s}:{t} ({'; '.join(dict.fromkeys(g['summary'] for g in top_level(sessions.analyses[s].get('goals', []))
                                                  if g['turn'] == t))})" for s, t in left]
    return [f"These goals still have friction that no hypothesis fixes: {', '.join(names)}. Find a fix for each "
            "(an idea the reviewer rejected may have a different fix), list it in already_addressed if an existing "
            "improvement covers it, or in not_fixed with why the friction is outside the assistant's control."]


def household_knowledge(sessions):
    """The household knowledge as last recorded (lines every assistant on the household API reads)."""
    from .household import normalized
    return normalized(sessions.household.source.get("household"))


def known_lines(sessions):
    """Every instruction line a change can replace: the assistant's instructions and the household knowledge."""
    template, _ = instructions(sessions)
    knowledge = household_knowledge(sessions)
    lines = knowledge["everyone"] + [l for mine in knowledge["people"].values() for l in mine]
    return (template or "") + "\n" + "\n".join(lines) if template is not None or lines else None


def instructions(sessions):
    """The assistant's instructions template as last recorded, or None."""
    assistant = getattr(sessions.assistant_view, "assistant", None)
    return (assistant or {}).get("options", {}).get("prompt"), assistant


def _change_problems(where, change, known, household, template=None):
    kind, found = change["kind"], []
    if kind in ("preference", "prompt") and not (change.get("text") or "").strip():
        found.append(f"{where}: a {kind} change needs text: the line for Assist's instructions.")
    if kind in ("preference", "prompt") and not change.get("for"):
        found.append(f"{where}: say who a {kind} line is for: everyone (household facts), or this_user (the person "
                     "in the sessions: their own lines in the household knowledge).")
    for line in change.get("replaces") or []:
        if kind in ("preference", "prompt") and (template is None or line.strip() not in template):
            found.append(f"{where}: replaces {line!r}, which isn't in Assist's current instructions or the household "
                         "knowledge; quote lines exactly as read_assistant_instructions shows them.")
    if kind in ("alias", "exposure"):
        entity = change.get("entity_id")
        if entity not in known:
            found.append(f"{where}: an {kind} change needs the entity_id of a device in this household; {entity!r} isn't one.")
        elif kind == "alias":
            if not (change.get("alias") or "").strip():
                found.append(f"{where}: an alias change needs alias: the new name.")
            missing = [a for a in change.get("replaces") or [] if a not in household.aliases(entity)]
            if missing:
                found.append(f"{where}: {entity} has no alias {', '.join(map(repr, missing))} to replace; its aliases are "
                             f"{household.aliases(entity) or 'none'}.")
        elif kind == "exposure":
            expose, exposed = change.get("expose", True), household._card(entity).get("exposed")
            if expose and exposed:
                found.append(f"{where}: {entity} is already exposed to Assist.")
            if not expose and not exposed:
                found.append(f"{where}: {entity} isn't exposed to Assist, so there's nothing to hide.")
    if kind == "script":
        existing = change.get("entity_id")
        if existing:  # Editing a script that exists: its description (text) and/or its steps.
            if not existing.startswith("script.") or existing not in known:
                found.append(f"{where}: {existing!r} isn't a script in this household; omit entity_id for a new script.")
            elif not (change.get("text") or "").strip() and not change.get("steps"):
                found.append(f"{where}: editing {existing} needs a new description (text) or new steps.")
        elif not (change.get("script_name") or "").strip() or not change.get("steps"):
            found.append(f"{where}: a new script needs script_name and steps (or give entity_id to edit an existing one).")
        steps = change.get("steps") or []
        for n, step in enumerate(steps, 1):
            if "stop" in step:
                if n != len(steps) or not step["stop"].strip():
                    found.append(f"{where}: a stop step comes last and carries the message for the user.")
                continue
            domain, _, service = step["service"].partition(".")
            if not service or step["entity_id"] not in known:
                found.append(f"{where}: script step {step['service']!r} on {step['entity_id']!r}: use domain.service "
                             "and a device in this household.")
            elif domain not in (step["entity_id"].split(".")[0], "homeassistant"):
                found.append(f"{where}: script step {step['service']!r} doesn't match {step['entity_id']}'s domain.")
    return found


class _Submit(_SessionTool):
    """A role's only way to answer. Keeps every submission so the best one can be used."""

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions, self.submissions = sessions, []

    def check(self, answer):
        raise NotImplementedError

    def run(self, **answer):
        found = self.check(answer)
        self.submissions.append({"answer": answer, "problems": found})
        if found:
            return {"accepted": False, "problems": found}
        return {"accepted": True, "message": "Accepted. Finish with one short sentence."}

    def best(self):
        """(status, answer, problems): the last accepted submission, else the one with fewest problems."""
        if not self.submissions:
            return "no_answer_submitted", None, []
        accepted = [s for s in self.submissions if not s["problems"]]
        if accepted:
            return "accepted", accepted[-1]["answer"], []
        best = min(reversed(self.submissions), key=lambda s: len(s["problems"]))
        return "best_rejected", best["answer"], best["problems"]


class SubmitHypotheses(_Submit):
    name = "submit_hypotheses"
    description = ("Submit your answer: one hypothesis per theme, each with the goals it fixes (session and goal "
                   "turn, from goals_with_friction), its cause, its changes and the expected effect. A change's text "
                   "is the line for Assist's instructions (preference, prompt; say who it's for: everyone, or "
                   "this_user) or the script's description (script, which Assist reads to decide when to run it). A "
                   "script change makes a new script (script_name, steps; exposed to Assist unless expose is false), "
                   "or edits an existing one when it gives that script's entity_id (a new description in text, "
                   "and/or new steps; editing doesn't expose it, so add an exposure change if Assist should run an "
                   "unexposed one); its steps may end with a stop step carrying the message Assist tells the "
                   "user. An exposure change exposes a device unless expose is false, which hides it. Goals an existing "
                   "improvement already addresses go in already_addressed (from list_improvements), not in a "
                   "hypothesis. It is checked against "
                   "the sessions and the household; if problems are reported, fix them and submit again.")
    parameters = HYPOTHESES

    def check(self, answer):
        return problems(self.sessions, answer)


class SubmitReview(_Submit):
    name = "submit_review"
    description = ("Submit your review: your comments (label, blocking or non-blocking, subject, discussion, household "
                   "evidence) and, when you know a better version, the revised hypothesis. The outcome follows from "
                   "the comments: any blocking comment requests changes.")
    parameters = REVIEW

    def check(self, answer):
        found = problems(self.sessions, answer, REVIEW)
        known = set(self.sessions.household._entities())
        for n, comment in enumerate(answer.get("comments", []), 1):
            if len(comment.get("decorations", [])) != 1:
                found.append(f"Comment {n}: decorate it blocking or non-blocking (one of them).")
            if comment.get("label") != "praise" and not comment.get("evidence"):
                found.append(f"Comment {n}: give household evidence (a session and turn, a device, or a pattern).")
            for item in comment.get("evidence") or []:
                session, turn, entity = item.get("session"), item.get("turn"), item.get("entity_id")
                known_session = self.sessions.briefs.get(session) if session else None
                if session and known_session is None:
                    found.append(f"Comment {n}: there is no session {session!r}.")
                elif known_session is not None and turn is not None and not 1 <= turn <= known_session["turns"]:
                    found.append(f"Comment {n}: session {session} has no turn {turn}.")
                if entity and entity not in known:
                    found.append(f"Comment {n}: there is no device {entity!r} in this household.")
                if not (session or entity or (item.get("note") or "").strip()):
                    found.append(f"Comment {n}: each piece of evidence names a session, a device or a pattern (note).")
        return found


class ReadSessionTurn(_SessionTool):
    name = "read_session_turn"
    description = ("Read turns of one session: what the user and the assistant said, every tool the assistant "
                   "called with the result, and the state changes during the turn. Give the session and one turn "
                   "or several.")
    parameters = probatio.Schema({probatio.Required("session"): str, probatio.Optional("turn"): int,
                                  probatio.Optional("turns"): [int]})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        await self.sessions.load(args["session"])
        return llm.ToolResult(data=self.run(**args))

    def run(self, session, turn=None, turns=None):
        view = self.sessions.views.get(session)
        if view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return view.read_turns(turns) if turns else view.read_turn(turn or 1)


class DeviceDetailsAt(_SessionTool):
    """device_details, optionally as of one session: the household snapshot is the newest session's."""
    name = "device_details"
    description = ("Everything about one device: its states and attributes, aliases, whether Assist can see it, its "
                   "changes, and other entities on the same physical device. By default as of the newest session; "
                   "give session to see it as it was in that session.")
    parameters = probatio.Schema({probatio.Required("entity_id"): str, probatio.Optional("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        if args.get("session"):
            await self.sessions.load(args["session"])
        return llm.ToolResult(data=self.run(**args))

    def run(self, entity_id, session=None):
        view = self.sessions.views.get(session) if session else self.sessions.household
        if view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return {"as_of_session": session or "the newest session", **view.device_details(entity_id)}


class DevicesInAreaNow(DevicesInArea):
    """devices_in_area over the household snapshot, which is the newest session's (the analyzer's tool reads one)."""
    description = ("List the devices in an area with their state as of the newest session, what they can do, and "
                   "whether Assist may use them (exposed). Use device_details with a session for an earlier state. "
                   "Sensors only on request.")


class ListSessions(_SessionTool):
    name = "list_sessions"
    description = ("List recorded sessions, newest first, a page at a time: when, how many turns, the first request, "
                   "and the state (open_friction: friction no improvement handles yet; friction_handled; "
                   "without_friction; not_analysed). Filter by state, by a device (its entity_id or a name), by an "
                   "area, or by words in the user's requests, to find the sessions where a rule would apply.")
    parameters = probatio.Schema({probatio.Optional("page", default=1): int,
                                  probatio.Optional("state"): probatio.In(["all", *STATES]),
                                  probatio.Optional("device"): str, probatio.Optional("area"): str,
                                  probatio.Optional("words"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self, page=1, state=None, device=None, area=None, words=None):
        return self.sessions.listing(page=page, state=state, device=device, area=area, words=words)


class ReadSession(_SessionTool):
    name = "read_session"
    description = ("Read one session as a script: each turn's words, the goals it started, and any friction. Use "
                   "read_session_turn for a turn's tool calls and state changes.")
    parameters = probatio.Schema({probatio.Required("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        await self.sessions.load(args["session"])
        return llm.ToolResult(data=self.run(**args))

    def run(self, session):
        view = self.sessions.views.get(session)
        if session not in self.sessions.analyses or view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return scenario(self.sessions.analyses[session], view)


class ReviewChannel:
    """Drafts from the generator to one long-running reviewer, and reviews back. The reviewer keeps its own
    conversation across reviews, waiting on a tool until the generator sends the next draft."""
    CLOSED = object()

    def __init__(self):
        self.queue, self.current, self.reviewer = asyncio.Queue(), None, None

    def __deepcopy__(self, memo):  # HA's conversation trace deep-copies tools; the channel is shared, not copied.
        return self

    async def ask(self, draft):
        """Send a draft and wait for its review; None if the reviewer stopped."""
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((draft, future))
        waits = [future] + ([self.reviewer] if self.reviewer else [])
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        return future.result() if future.done() else None

    def close(self):
        self.queue.put_nowait((self.CLOSED, None))


class WaitForDraft(llm.Tool):
    """The reviewer's side of the channel: blocks until the generator sends a draft."""
    name = "wait_for_draft"
    description = "Wait for the generator's next draft hypothesis and return it, or say that you're finished."
    parameters = probatio.Schema({})
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, channel):
        self.channel = channel

    async def async_call(self, hass, tool_input, llm_context):
        draft, future = await self.channel.queue.get()
        if draft is ReviewChannel.CLOSED:
            return llm.ToolResult(data={"finished": "No more drafts. You're done; finish with one short sentence."})
        self.channel.current = (draft, future)
        return llm.ToolResult(data={"draft": draft})


class ChannelSubmitReview(_Submit):
    """submit_review in the loop: an accepted review answers the draft the generator is waiting on."""
    name = "submit_review"
    description = ("Submit your review of the current draft: your comments (label, blocking or non-blocking, subject, "
                   "discussion, household evidence) and, when you know a better version, the revised hypothesis. The "
                   "outcome follows from the comments: any blocking comment requests changes. Then call "
                   "wait_for_draft for the next draft.")
    parameters = REVIEW

    def __init__(self, sessions, channel):
        super().__init__(sessions)
        self.channel = channel

    def check(self, answer):
        if self.channel.current is None:
            return ["There's no draft to review; call wait_for_draft first."]
        return SubmitReview(self.sessions).check(answer)

    def run(self, **answer):
        result = super().run(**answer)
        if result["accepted"]:
            _, future = self.channel.current
            self.channel.current = None
            if not future.done():
                future.set_result(answer)
            result["message"] = "Sent to the generator. Call wait_for_draft for the next draft."
        return result


class AskReviewer(llm.Tool):
    """The generator's way to discuss a draft with the reviewer: a separate model, never its own judgement."""
    name = "ask_reviewer"
    description = ("Ask the reviewer, a separate model, about one draft hypothesis (same fields as in "
                   "submit_hypotheses). It answers with numbered review comments backed by household evidence, and an "
                   "outcome that follows from them (any blocking comment requests changes). When asking again about "
                   "the same idea (same title), answer each previous blocking comment in responses: resolved, or "
                   "wont_fix with a note.")
    parameters = probatio.Schema({**_HYPOTHESIS, probatio.Optional("responses"): [{
        probatio.Required("comment"): int, probatio.Required("resolution"): probatio.In(["resolved", "wont_fix"]),
        probatio.Required("note"): str}]})
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, sessions, channel):
        self.sessions, self.channel, self.reviews = sessions, channel, []

    async def async_call(self, hass, tool_input, llm_context):
        args = dict(tool_input.tool_args)
        hypothesis = {k: v for k, v in args.items() if k != "responses"}
        earlier = [r for r in self.reviews if r["hypothesis"].get("title") == hypothesis.get("title")]
        if len(self.reviews) >= REVIEWS_PER_RUN or len(earlier) >= REVIEWS_PER_IDEA:
            return llm.ToolResult(data={"error": "The review budget for this run or idea is used up; submit your best version."})
        checks = None
        if self.sessions.evidence:
            async with span("evidence.check"):
                checks = await self.sessions.evidence.check(hypothesis)
        related = self.sessions.related([(g["session"], g["goal_turn"]) for g in hypothesis.get("goals", [])],
                                        change_devices(hypothesis.get("changes", [])))
        applies = self.sessions.where_it_applies_to(hypothesis)
        answer = await self.channel.ask({**args, **({"checks": checks} if checks else {}),
                                         **({"related_improvements": related} if related else {}),
                                         "sessions_where_it_would_apply": applies})
        self.reviews.append({"hypothesis": hypothesis, "responses": args.get("responses"), "review": answer,
                             "verdict": verdict(answer) if answer else None, "checks": checks})
        if answer is None:
            return llm.ToolResult(data={"error": "The reviewer stopped; decide yourself."})
        return llm.ToolResult(data={**review_result(hypothesis, answer), **({"checks": checks} if checks else {})})


async def run_generator(hass, sessions, entity_id, seeded=(), reviewer_entity_id=None):
    """The generator and one long-running reviewer, side by side: (generator run, its submit tool, ask tool,
    reviewer run, seeded reviews). The reviewer runs on its own model if given, else the generator's. Seeded cases
    (a test of the reviewer) go through the same reviewer afterwards."""
    from .snapshot import live_definitions
    scripts, automations = live_definitions(hass)
    async with span("evidence.load"):
        await sessions.prepare()
        await sessions.load_evidence({"scripts": scripts, "automations": automations})
    channel = ReviewChannel()
    review_submit = ChannelSubmitReview(sessions, channel)
    reviewer = hass.async_create_task(run_role(
        hass, role=REVIEWER_ROLE, entity_id=reviewer_entity_id or entity_id,
        instructions=REVIEWER_INSTRUCTIONS + REVIEWER_LOOP,
        payload={"household": sessions.summary()}, structure=None, allow_paid_inference=True,
        llm_api=SessionAPI(hass, tools=reviewer_tools(sessions, review_submit) + [WaitForDraft(channel)])))
    channel.reviewer = reviewer
    submit, ask = SubmitHypotheses(sessions), AskReviewer(sessions, channel)
    try:
        generator = await run_role(hass, role=GENERATOR_ROLE, entity_id=entity_id,
                                   instructions=GENERATOR_INSTRUCTIONS, payload=sessions.overview(), structure=None,
                                   allow_paid_inference=True, llm_api=SessionAPI(hass, tools=tools(sessions, submit, ask)))
        seeded_reviews = {}
        for name, hypothesis in seeded:
            checks = await sessions.evidence.check(hypothesis)
            answer = await channel.ask({**hypothesis, "checks": checks,
                                        "sessions_where_it_would_apply": sessions.where_it_applies_to(hypothesis)})
            seeded_reviews[name] = {"review": answer, "verdict": verdict(answer) if answer else None}
    except BaseException:  # Failed or cancelled: the reviewer never waits for a draft that won't come.
        channel.close()
        reviewer.cancel()
        raise
    channel.close()
    reviewer_run = await reviewer
    return generator, submit, ask, reviewer_run, seeded_reviews


def review_result(hypothesis, answer):
    """What the generator gets back from a review: numbered comments, the outcome, and the next step if blocked."""
    result = {"outcome": verdict(answer),
              "comments": [{"comment": n, **c} for n, c in enumerate(answer["comments"], 1)],
              **({"revised": answer["revised"]} if answer.get("revised") else {})}
    open_ = [n for n, c in enumerate(answer["comments"], 1) if "blocking" in c.get("decorations", [])]
    if open_:  # Like a goal in an agent harness: the friction stays open until fixed.
        goals = ", ".join(f"{g['session']}:{g['goal_turn']}" for g in hypothesis.get("goals", []))
        result["next_step"] = (
            f"Blocking comments {', '.join(map(str, open_))} mean goals {goals} still need a fix. Consider other "
            "ideas for them" + (", including the reviewer's revised version" if answer.get("revised") else "")
            + ". To ask again about this idea, keep its title and answer each blocking comment in responses "
            "(resolved, or wont_fix with a note). Only if the friction is outside the assistant's control, "
            "list those goals in not_fixed with why.")
    return result


class CheckHypothesis(llm.Tool):
    """Facts from Home Assistant itself about a draft hypothesis, run in sandboxes: no model, no device."""
    name = "check_hypothesis"
    description = ("Check a draft hypothesis (same fields as submit_hypotheses) with Home Assistant itself, in a "
                   "sandbox of the recorded home. implementation: for each named goal, each new, edited or newly "
                   "exposed script runs alone from the state the goal started in (and all of them together if none "
                   "reaches it alone), showing whether the goal's devices end up as the goal wants, including what "
                   "the user chose for settings left open; new aliases are matched by Home Assistant's own name "
                   "matching. resolution_signals: the recorded tool calls of every session the change could affect (its calls "
                   "touch the change's devices, their names or areas) are replayed with it, listing those that now "
                   "resolve differently: signals to weigh, not proof of a regression. "
                   "Instruction lines (preference, prompt) don't change how Home Assistant resolves devices; their "
                   "effect shows only when the conversation is replayed with the model, later.")
    parameters = probatio.Schema(_HYPOTHESIS)
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, sessions):
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        hypothesis = dict(tool_input.tool_args)
        return llm.ToolResult(data={**await self.sessions.evidence.check(hypothesis),
                                    "sessions_where_it_would_apply": self.sessions.where_it_applies_to(hypothesis)})


class ReadAssistantInstructions(_SessionTool):
    name = "read_assistant_instructions"
    description = ("Read Assist's current instructions (its prompt template, as configured), the household knowledge "
                   "(the lines every assistant reads: for everyone, and for each person), and which assistant and "
                   "model it uses. Preference and prompt changes add lines to the household knowledge.")
    parameters = probatio.Schema({})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self):
        template, assistant = instructions(self.sessions)
        if template is None:
            return {"error": "No recorded session shows the assistant's instructions."}
        knowledge = household_knowledge(self.sessions)
        return {"assistant": assistant.get("title"), "model": assistant.get("options", {}).get("model"),
                "instructions_template": template,
                "household_knowledge": {"everyone": knowledge["everyone"],
                                        "the_person_in_the_sessions": [l for mine in knowledge["people"].values()
                                                                       for l in mine]}}


class ReadScript(llm.Tool):
    """A script's current definition, read live (read-only): recordings only hold script names.

    Offline (in tests), definitions come from a copy of the household's scripts instead.
    """
    name = "read_script"
    description = "Read a script's current definition: its name and steps. Give the script's entity_id."
    parameters = probatio.Schema({probatio.Required("entity_id"): str})
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, definitions=None):
        self.definitions = definitions  # {entity_id: definition}, offline only.

    def run(self, entity_id):
        definition = (self.definitions or {}).get(entity_id)
        if definition is None:
            return {"error": f"Script {entity_id} has no readable configuration"}
        return {"entity_id": entity_id, "definition": definition}

    async def async_call(self, hass, tool_input, llm_context):
        entity_id = tool_input.tool_args["entity_id"]
        if self.definitions is not None:
            return llm.ToolResult(data=self.run(entity_id))
        try:
            definitions, _ = live_script_definitions(hass, [entity_id])
        except CaseError as err:
            return llm.ToolResult(data={"error": str(err)})
        return llm.ToolResult(data={"entity_id": entity_id, "definition": definitions[entity_id]})


def reviewer_tools(sessions, submit, scripts=None):
    """The household tools over the newest snapshot, reading sessions, instructions and scripts, and the submit tool.
    scripts: offline script definitions for read_script; live from Home Assistant when None."""
    return ([cls(sessions.household) for cls in TOOLS if cls not in (ReadTurn, DeviceDetails, DevicesInArea)]
            + [DevicesInAreaNow(sessions.household), DeviceDetailsAt(sessions), ListSessions(sessions), ReadSession(sessions), ReadSessionTurn(sessions),
               ReadAssistantInstructions(sessions), ReadScript(scripts),
               ListImprovements(sessions.household, sessions.existing),
               ReadImprovement(sessions.household, sessions.existing)]
            + ([CheckHypothesis(sessions)] if sessions.evidence else []) + [submit])


def tools(sessions, submit, reviewer=None, scripts=None):
    """The generator's tools: the reviewer's, plus ask_reviewer when given."""
    return reviewer_tools(sessions, submit, scripts) + ([reviewer] if reviewer else [])
