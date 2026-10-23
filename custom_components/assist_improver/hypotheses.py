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

import probatio
from homeassistant.helpers import llm

from .analyzer import top_level
from .candidates import live_script_definitions
from .cases import CaseError
from .exploration import TOOLS, DeviceDetails, DevicesInArea, ReadTurn, SessionAPI, _SessionTool
from .reflective import run_role

GENERATOR_ROLE = "hypothesis_generator"
REVIEWER_ROLE = "hypothesis_reviewer"
# How often the generator may ask the reviewer, per run and per idea.
REVIEWS_PER_RUN, REVIEWS_PER_IDEA = 15, 5
KINDS = ("preference", "prompt", "alias", "exposure", "script")

# Foundational Home Assistant knowledge both roles need, given upfront. Generic, not about any household; checked
# against the HA 2026.10 sources.
HA_PRIMER = """How Home Assistant's Assist works (Home Assistant 2026.10):
- Exposure: Assist (the AI) only sees and controls entities exposed to it. An unexposed device still exists, and a script can control it, but Assist can't name, read or change it directly, and may say it doesn't exist.
- Assist's tools are fixed per device type: turn on or off (also opens and closes covers and valves, locks and unlocks locks, presses buttons, runs scripts and scenes); light brightness and colour; climate target temperature; fan speed; cover position; media pause, skip and volume; vacuum start, return and clean an area; timers, to-do lists and broadcasts; live state reading. There is no tool to choose an option on a select (such as a seat heater level), set a number, or change a climate or fan mode: exposing such a device doesn't make it settable. A script can do it.
- Turning a climate device on resumes its last mode; a target temperature can be set; its mode (heat, cool, fan only) can't be chosen by Assist.
- Scripts: each exposed script becomes its own Assist tool, named after the script and described by its description and aliases. Its steps run regardless of what is exposed. A step calls an action on a device, with its data, for example select.select_option with option: high, climate.set_hvac_mode with hvac_mode: fan_only, switch.turn_on, or script.turn_on to run another script. Scripts proposed here take no parameters, so each sets fixed values. One script per occasion solves that occasion only; a preference that teaches Assist to use existing tools or scripts generalises better.
- Names and aliases: Assist finds devices by name, alias and area. Two exposed devices with the same name or alias make requests ambiguous and can fail. Aliases are extra names on a device.
- Preferences: Assist's instructions are a template rendered for every request (it can use Home Assistant templates, such as the current time or a device's state), so a line there applies to every conversation. A block in the template can limit lines to one person (by their user ID); put a person's own preferences inside their block, and household facts that hold for everyone outside it. It's where household facts and preferences go: what the user means by a word, which device they usually mean, how a device is operated.
- Momentary relays: some switches act like buttons (a door opener moves when its relay goes off, then on). Switching such a relay doesn't report the door's position.
- Local handling: with "prefer handling locally" on, simple commands that match Home Assistant's own sentences are handled without the AI, so instruction changes don't affect them.

Home Assistant's own published best practices (home-assistant.io, voice_control pages, 2026.10):
- Expose only the bare minimum used by voice: more names make matching slower and AI requests costlier. Exposure is opt-in so that sensitive devices such as locks and garage doors aren't controlled by accident.
- Name entities "<area> [<descriptor>] [<domain>]" (e.g. "Living room lamp"); put devices in areas and areas on floors, since people ask by room ("turn off the office lights").
- Add an alias wherever people naturally say something else ("TV" for "Television"); area-less aliases ("Lamp") work, and a shared alias is matched together with the area.
- The domain sets the verbs: a switch called "main valve" can't be opened by voice, but a valve can. Change the device type (switch_as_x, or a template entity) when a switch is really a valve, cover, lock, fan or light, and set a proper device class (a window sensor needs device class window to answer "is it open?").
- A script's description is the most important part of exposing it: say what it does, when to use it, and what to tell the user afterwards; keep it concise. Describe each field (what, format, limits). End with a stop action carrying a success or error message, so the AI doesn't make up a response. At most 128 exposed scripts and tools in total."""

GENERATOR_INSTRUCTIONS = """You help one household get the most out of Home Assistant's voice assistant ("Assist"). How Assist works is summarised at the end. Your input names the home, its areas and Assist's tools, and what Assist can and can't do with each device involved below.

The user seems to be encountering friction in the scenarios in your input. Each is a session written as a script, turn by turn: what the user and Assist said, the user's goals marked at the turn they started, and the friction marked where it happened: extra turns the user spent on a goal (with what each served) and settings the user's words left open (with what the user chose). Side effects Assist left behind are listed too. You can look at every other session with list_sessions and read_session, at a turn's tool calls and state changes with read_session_turn, and at the household with the other tools. They answer from the recordings, except read_script, which reads a script's current definition.

Your task: propose personalisations to the user's Home Assistant setup that make their long-horizon experience with Assist as seamless as possible. The friction is your evidence; aim for changes that keep helping in future requests, not just in these sessions.

How to work:
1. Find the causes, with the tools.
2. Group goals whose friction has the same cause into one theme, across sessions. For each theme, form one coherent idea: the change that would have got those goals done without friction. Name the goals it fixes (session and goal turn); all of a goal's friction counts as removed. Prefer the least specific change that works, in this order:
   - prompt: make Assist itself reason better, written as a general behaviour instruction. This is in scope and often the most general fix: how it works out what the user means (from devices' current states, the time, what the user is doing or just did), when to act and when to ask, checking its own results, and stating them firmly when the data supports it. It applies to every request, so weigh what else it could change;
   - preference: a fact about this household or the user's preferences, written as a line for Assist's instructions (what the user meant by a word, which devices they mean, how a device is operated);
   - alias (a name for a device), exposure (letting Assist see and control a device), or the household's other setup;
   - script: a new script, only when nothing above would work.
   An idea may combine several changes when they serve the same theme, such as a preference and the exposure it relies on.
   Prefer the simplest rule that fits every session, not only the ones it fixes. Test each rule against all the sessions where it would apply, including sessions without friction (list_sessions, read_session): a rule that explains them all in one idea beats one with special cases. A special case, or a rule built from a single session, needs a reason the sessions show. Record what you found: in support, the sessions the rule agrees with; in conflicts, any it contradicts.
3. Read what is already there before changing it: Assist's current instructions (read_assistant_instructions), a device's aliases, a script's steps. Prefer editing what exists over adding to it: rewrite or merge existing lines into a more general one (give the lines a change replaces in replaces), replace an alias, rework a script.
4. Discuss each idea with the reviewer, a separate model, using ask_reviewer: send it a draft hypothesis, and it answers with review comments backed by household evidence; any blocking comment requests changes. Take its points seriously: revise, or find another fix for the same goals, and ask again, answering each blocking comment (resolved, or wont_fix with a note).
5. Implement: make the changes concrete and submit all hypotheses with submit_hypotheses. Every goal with friction needs a fix in some hypothesis, or a place in not_fixed with why its friction is outside the assistant's control. The submit tool checks this and everything else against the sessions and the household; if it reports problems, fix them and submit again. When it accepts your answer, finish with one short sentence.

Respect the timing and separation of the user's requests: what they asked for separately, at different times, stays separate; what they asked for together can be done together. Speech recognition mishearing the user is in scope too, when the sessions show it happened (for example, an alias for a name that's often misheard). Use only devices and scripts the tools show you.

""" + HA_PRIMER

REVIEWER_INSTRUCTIONS = """You review one proposed change to a household's Home Assistant voice assistant ("Assist"), before any money is spent testing it, the way a careful code reviewer reviews a pull request. The change may be a draft the generator is still working on, or a finished hypothesis; review it the same way. You get the hypothesis (the goals whose friction it targets, its cause and its changes), a summary of the household's sessions (what the user usually asks for, what worked, their known choices), and, when the generator asks again, your previous review with its responses. Read sessions and explore the household with the tools.

Write your review as comments in the Conventional Comments style. Each comment has:
- label: issue (a problem the change has), suggestion (a concrete improvement), todo (a small necessary change), nitpick (trivial, preference-level), question (something you need answered), thought (an idea to consider), or praise (something done well);
- decorations: blocking if the change shouldn't go ahead until this is addressed, otherwise non-blocking;
- subject: the comment in one sentence;
- discussion: why, and what you'd do instead;
- evidence: the household facts that support it: sessions and turns (what the user said or did there), devices (names, aliases, exposure, states), or patterns across sessions (as a note). Every comment except praise needs at least one; a comment without household evidence is opinion.
Test every rule against all the sessions where it would apply, not only the ones it fixes, and check the hypothesis's support and conflicts against them. Prefer the simplest rule consistent with all of them; a rule with special cases, or one resting on a single session, needs a reason the sessions show, and a simpler rule that fits as well is a blocking comment with those sessions as evidence. Look for: a wrong cause (including a claimed mishearing that the sessions don't show); combining requests the user made separately, at different times; a change that would break, confuse or misfire on other things the household does, or won't carry over to the cases it should cover; and a simpler, more general or more polished version. The outcome follows from the comments: any blocking comment requests changes, otherwise the change is approved with your comments. So mark a comment blocking only when the evidence supports it, and put a revised hypothesis in revised when you know the better version. When you get responses to your previous comments, check each: comment again if a resolution didn't resolve it, and don't repeat comments that were resolved.

A hypothesis's changes: preference and prompt lines say who they're for (everyone, or this_user, inside that person's block in the instructions); an exposure change exposes a device unless expose is false, which hides it; a script change makes a new script (script_name, steps; exposed to Assist unless expose is false) or edits an existing one (its entity_id, with a new description in text and/or new steps), and its steps may end with a stop step carrying the message for the user.

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


def session_brief(analysis, view):
    """One session for the overview: what was said, the goals, and the goals that had friction."""
    goals = [{k: g[k] for k in ("turn", "summary", "kind", "entity_id", "state", "states", "reached") if k in g}
             for g in top_level(analysis.get("goals", [])) if g["kind"] != "ask"]
    brief = {"session": short(analysis["recording_id"]), "conversation": view.conversation(), "goals": goals,
             "goals_with_friction": [
                 {"goal_turn": turn, "goals": [g["summary"] for g in top_level(analysis.get("goals", []))
                                               if g["turn"] == turn], "friction": items}
                 for turn, items in sorted(friction_by_goal(analysis).items())]}
    if analysis.get("side_effects_left"):
        brief["side_effects_left"] = analysis["side_effects_left"]
    return brief


# What Assist can do with an exposed device, by domain, as (tool name, what it does); see HA_PRIMER.
ASSIST_CAN = {
    "light": [("intent__HassTurnOn", "turn on"), ("intent__HassTurnOff", "turn off"),
              ("light__HassLightSet", "set brightness or colour")],
    "climate": [("intent__HassTurnOn", "turn on (resumes its last mode)"), ("intent__HassTurnOff", "turn off"),
                ("climate__HassClimateSetTemperature", "set the target temperature")],
    "fan": [("intent__HassTurnOn", "turn on"), ("intent__HassTurnOff", "turn off"), ("fan__HassFanSetSpeed", "set speed")],
    "cover": [("intent__HassTurnOn", "open"), ("intent__HassTurnOff", "close"), ("intent__HassSetPosition", "set position")],
    "valve": [("intent__HassTurnOn", "open"), ("intent__HassTurnOff", "close")],
    "lock": [("intent__HassTurnOn", "lock"), ("intent__HassTurnOff", "unlock")],
    "media_player": [("intent__HassTurnOn", "turn on"), ("intent__HassTurnOff", "turn off"),
                     ("media_player__HassMediaPause", "pause"), ("media_player__HassMediaUnpause", "resume"),
                     ("media_player__HassSetVolume", "set volume"), ("media_player__HassMediaNext", "skip")],
    "vacuum": [("vacuum__HassVacuumStart", "start"), ("vacuum__HassVacuumReturnToBase", "return to base"),
               ("vacuum__HassVacuumCleanArea", "clean an area")],
    "script": [("intent__HassTurnOn", "run")], "scene": [("intent__HassTurnOn", "activate")],
    "button": [("intent__HassTurnOn", "press")],
    **dict.fromkeys(("switch", "input_boolean", "humidifier", "siren", "automation"),
                    [("intent__HassTurnOn", "turn on"), ("intent__HassTurnOff", "turn off")]),
}


def capabilities(view, entity_id, tools):
    """What Assist can and can't do with one device, given the tools it had."""
    card = view._card(entity_id)
    can = [what for tool, what in ASSIST_CAN.get(entity_id.split(".")[0], []) if tool in tools]
    out = {"entity_id": entity_id, "name": card["name"], "area": card.get("area"), "exposed": card.get("exposed"),
           "aliases": view.aliases(entity_id)}
    if not card.get("exposed"):
        out["assist_can"] = "nothing directly: it isn't exposed (a script could control it)"
    else:
        out["assist_can"] = can or ["nothing: no Assist tool can change this kind of device (a script could)"]
    return out


class Sessions:
    """The analysed sessions a role works over, with the newest one's snapshot standing in for the household."""

    def __init__(self, analyses, views):
        self.analyses = {short(a["recording_id"]): a for a in analyses}
        self.views = {short(a["recording_id"]): v for a, v in zip(analyses, views)}
        self.household = max(views, key=lambda v: v.digest["started"])

    def overview(self):
        """Background, then only the sessions with friction, as scripts; the rest are read on demand."""
        rough = [s for s in sorted(self.analyses) if friction(self.analyses[s])]
        return {"home": self.household.digest["home"], "time_zone": self.household.digest["time_zone"],
                "areas": self.household.list_areas(), "assist_tools": self.assist_tools(),
                "devices_in_friction": self.devices_in_friction(),
                "friction_scenarios": [scenario(self.analyses[s], self.views[s]) for s in rough],
                "other_sessions": f"{len(self.analyses) - len(rough)} sessions without friction; see list_sessions"}

    def listing(self):
        """Every session in one line each."""
        return [{"session": s, "first_request": self.views[s].conversation()[0]["user_says"].strip(),
                 "turns": len(self.views[s].digest["turns"]), "friction": bool(friction(a))}
                for s, a in sorted(self.analyses.items())]

    def assist_tools(self):
        """Assist's tools, from the newest session that recorded them (a locally handled one has none)."""
        for view in sorted(self.views.values(), key=lambda v: v.digest["started"], reverse=True):
            if view.digest["capabilities"]["assist_tools"]:
                return [t["name"] for t in view.digest["capabilities"]["assist_tools"]]
        return []

    def devices_in_friction(self):
        """What Assist can and can't do with each device in a session with friction, so causes are checkable."""
        tools, entities = set(self.assist_tools()), set()
        known = set(self.household._entities())
        for s, analysis in self.analyses.items():
            if friction(analysis):
                for goal in analysis.get("goals", []):
                    entities.update([goal["entity_id"]] if goal.get("entity_id") else goal.get("entity_ids") or [])
                for turn in self.views[s].digest["turns"]:
                    for step in turn["assistant"]:
                        entities.update(e for e in (step.get("args") or {}).values() if isinstance(e, str) and e in known)
        return [capabilities(self.household, e, tools) for e in sorted(entities & known)]

    def summary(self):
        """For the reviewer: what each session asked for and how it went, and the user's known choices."""
        sessions = []
        for s in sorted(self.analyses):
            a, view = self.analyses[s], self.views[s]
            sessions.append({"session": s, "requests": [t["user_says"].strip() for t in view.conversation()],
                             "goals_reached": [g.get("reached") for g in top_level(a.get("goals", []))
                                               if g["kind"] in ("state", "sequence")],
                             "extra_turns": a.get("extra_turns", [])})
        chosen = [{"session": s, **{k: f[k] for k in ("summary", "user_chose")}}
                  for s, a in self.analyses.items() for f in friction(a) if f["kind"] == "ask"]
        return {"home": self.household.digest["home"], "sessions": sessions, "known_choices": chosen}


def problems(sessions, answer, schema=HYPOTHESES):
    """What is wrong with a generator answer, checked against the sessions and the household; empty means valid."""
    try:
        schema(answer)
    except probatio.Invalid as err:
        return [f"The answer does not match the format: {err}"]
    known, found = set(sessions.household._entities()), []
    template, _ = instructions(sessions)
    items = answer["hypotheses"] if schema is HYPOTHESES else [answer["revised"]] if answer.get("revised") else []
    for n, hypothesis in enumerate(items, 1):
        where = f"Hypothesis {n} ({hypothesis['title']!r})"
        for ref in hypothesis["goals"]:
            analysis = sessions.analyses.get(ref["session"])
            if analysis is None:
                found.append(f"{where}: there is no session {ref['session']!r}; sessions are {', '.join(sorted(sessions.analyses))}.")
            elif ref["goal_turn"] not in friction_by_goal(analysis):
                found.append(f"{where}: session {ref['session']} has no goal with friction at turn {ref['goal_turn']}; "
                             f"its goals with friction start at turns {sorted(friction_by_goal(analysis)) or 'none'}.")
        if not hypothesis["goals"]:
            found.append(f"{where}: name the goals it fixes (session and goal turn).")
        if not hypothesis["support"]:
            found.append(f"{where}: list in support the sessions the rule agrees with.")
        for field in ("support", "conflicts"):
            for ref in hypothesis[field]:
                view = sessions.views.get(ref["session"])
                if view is None:
                    found.append(f"{where}: {field} names no session {ref['session']!r}.")
                elif ref.get("turn") is not None and not 1 <= ref["turn"] <= len(view.digest["turns"]):
                    found.append(f"{where}: {field}: session {ref['session']} has no turn {ref['turn']}.")
        for change in hypothesis["changes"]:
            found += _change_problems(where, change, known, sessions.household, template)
    if schema is HYPOTHESES:
        found += _open_goals(sessions, answer)
    return found


def open_goals(sessions, answer):
    """Goals with friction that neither a hypothesis nor not_fixed addresses."""
    addressed = {(r["session"], r["goal_turn"]) for h in answer.get("hypotheses", []) for r in h["goals"]}
    addressed |= {(r["session"], r["goal_turn"]) for r in answer.get("not_fixed") or [] if r["why"].strip()}
    return [(s, turn) for s, analysis in sorted(sessions.analyses.items())
            for turn in sorted(friction_by_goal(analysis)) if (s, turn) not in addressed]


def _open_goals(sessions, answer):
    left = open_goals(sessions, answer)
    if not left:
        return []
    names = [f"{s}:{t} ({'; '.join(dict.fromkeys(g['summary'] for g in top_level(sessions.analyses[s].get('goals', []))
                                                  if g['turn'] == t))})" for s, t in left]
    return [f"These goals still have friction that no hypothesis fixes: {', '.join(names)}. Find a fix for each "
            "(an idea the reviewer rejected may have a different fix), or list it in not_fixed with why the friction "
            "is outside the assistant's control."]


def instructions(sessions):
    """The assistant's instructions template as last recorded, or None."""
    assistant = next((v.assistant for v in sorted(sessions.views.values(), key=lambda v: v.digest["started"],
                                                  reverse=True) if v.assistant), None)
    return (assistant or {}).get("options", {}).get("prompt"), assistant


def _change_problems(where, change, known, household, template=None):
    kind, found = change["kind"], []
    if kind in ("preference", "prompt") and not (change.get("text") or "").strip():
        found.append(f"{where}: a {kind} change needs text: the line for Assist's instructions.")
    if kind in ("preference", "prompt") and not change.get("for"):
        found.append(f"{where}: say who a {kind} line is for: everyone (household facts), or this_user (the person "
                     "in the sessions, inside their block in the instructions).")
    for line in change.get("replaces") or []:
        if kind in ("preference", "prompt") and (template is None or line.strip() not in template):
            found.append(f"{where}: replaces {line!r}, which isn't in Assist's current instructions; quote "
                         "lines exactly as read_assistant_instructions shows them.")
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
                   "and/or new steps); its steps may end with a stop step carrying the message Assist tells the "
                   "user. An exposure change exposes a device unless expose is false, which hides it. It is checked against "
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
                view = self.sessions.views.get(session) if session else None
                if session and view is None:
                    found.append(f"Comment {n}: there is no session {session!r}.")
                elif view is not None and turn is not None and not 1 <= turn <= len(view.digest["turns"]):
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

    def run(self, session, turn=None, turns=None):
        view = self.sessions.views.get(session)
        if view is None:
            return {"error": f"There is no session {session!r}; sessions are {', '.join(sorted(self.sessions.views))}."}
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

    def run(self, entity_id, session=None):
        view = self.sessions.views.get(session) if session else self.sessions.household
        if view is None:
            return {"error": f"There is no session {session!r}; sessions are {', '.join(sorted(self.sessions.views))}."}
        return {"as_of_session": session or "the newest session", **view.device_details(entity_id)}


class DevicesInAreaNow(DevicesInArea):
    """devices_in_area over the household snapshot, which is the newest session's (the analyzer's tool reads one)."""
    description = ("List the devices in an area with their state as of the newest session, what they can do, and "
                   "whether Assist may use them (exposed). Use device_details with a session for an earlier state. "
                   "Sensors only on request.")


class ListSessions(_SessionTool):
    name = "list_sessions"
    description = "List every recorded session: its first request, how many turns, and whether it had friction."
    parameters = probatio.Schema({})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self):
        return {"sessions": self.sessions.listing()}


class ReadSession(_SessionTool):
    name = "read_session"
    description = ("Read one session as a script: each turn's words, the goals it started, and any friction. Use "
                   "read_session_turn for a turn's tool calls and state changes.")
    parameters = probatio.Schema({probatio.Required("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self, session):
        if session not in self.sessions.analyses:
            return {"error": f"There is no session {session!r}; sessions are {', '.join(sorted(self.sessions.analyses))}."}
        return scenario(self.sessions.analyses[session], self.sessions.views[session])


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
        answer = await self.channel.ask(args)
        self.reviews.append({"hypothesis": hypothesis, "responses": args.get("responses"), "review": answer,
                             "verdict": verdict(answer) if answer else None})
        if answer is None:
            return llm.ToolResult(data={"error": "The reviewer stopped; decide yourself."})
        return llm.ToolResult(data=review_result(hypothesis, answer))


async def run_generator(hass, sessions, entity_id, seeded=()):
    """The generator and one long-running reviewer, side by side: (generator run, its submit tool, ask tool,
    reviewer run, seeded reviews). Seeded cases (a test of the reviewer) go through the same reviewer afterwards."""
    channel = ReviewChannel()
    review_submit = ChannelSubmitReview(sessions, channel)
    reviewer = hass.async_create_task(run_role(
        hass, role=REVIEWER_ROLE, entity_id=entity_id, instructions=REVIEWER_INSTRUCTIONS + REVIEWER_LOOP,
        payload={"household": sessions.summary()}, structure=None, allow_paid_inference=True,
        llm_api=SessionAPI(hass, tools=reviewer_tools(sessions, review_submit) + [WaitForDraft(channel)])))
    channel.reviewer = reviewer
    submit, ask = SubmitHypotheses(sessions), AskReviewer(sessions, channel)
    generator = await run_role(hass, role=GENERATOR_ROLE, entity_id=entity_id, instructions=GENERATOR_INSTRUCTIONS,
                               payload=sessions.overview(), structure=None, allow_paid_inference=True,
                               llm_api=SessionAPI(hass, tools=tools(sessions, submit, ask)))
    seeded_reviews = {}
    for name, hypothesis in seeded:
        answer = await channel.ask(hypothesis)
        seeded_reviews[name] = {"review": answer, "verdict": verdict(answer) if answer else None}
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


class ReadAssistantInstructions(_SessionTool):
    name = "read_assistant_instructions"
    description = ("Read Assist's current instructions (its prompt template, as configured) and which assistant and "
                   "model it uses. Preference and prompt changes edit these instructions.")
    parameters = probatio.Schema({})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self):
        template, assistant = instructions(self.sessions)
        if template is None:
            return {"error": "No recorded session shows the assistant's instructions."}
        return {"assistant": assistant.get("title"), "model": assistant.get("options", {}).get("model"),
                "instructions_template": template}


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
               ReadAssistantInstructions(sessions), ReadScript(scripts), submit])


def tools(sessions, submit, reviewer=None, scripts=None):
    """The generator's tools: the reviewer's, plus ask_reviewer when given."""
    return reviewer_tools(sessions, submit, scripts) + ([reviewer] if reviewer else [])
