"""Simulate (SPEC.md P0.1, DETAILS P6): an idea tested on the conversation it's proposed for.

The whole conversation is replayed in a sandbox (sandbox.py, replay.py) with the household's own assistant and the
user's recorded words, in order, from the home and time it started from: once without the change (the before), which
must go as the conversation went, and once with it (the after), leaving out the turns DETAILS P6.4 lets it skip. Code
judges both by P0.1.1's measures (measures.py); a small model judges only whether an Assist turn shows what the
household objected to (a conduct goal), one turn at a time, with what it said and did. Each replay runs once for its
plan and is kept (Replays): a settled one is reused, never resampled; one the machinery cut off runs again.
"""

import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

import probatio
from homeassistant.components.conversation.trace import async_conversation_trace
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from . import measures
from .analyzer import SPEAKING
from .cases import (CaseError, as_seen, calls_in, digest, recording_turns, replay_readiness, replayed,
                    started_from, transcript)
from .changes import setup_from_changes
from .const import DOMAIN, STORAGE_VERSION, TESTED_PROVIDERS
from .digest import _clip, _naming
from .failures import closing
from .instructions import target
from .replay import SandboxHome, assistant_of
from .simulation import (UnsupportedSimulation, _request_changes, assist_transitions, moved_by_others, per_entity,
                         recorded_attributes, same_brightness, same_effects, setting_off)
from .snapshot import assist_config
from .trace import span

# How a replay can end. Settled: its result stands for its plan (never run again). Transient: the machinery's doing
# (a provider or worker fault, a restart, a cancelled run): it says nothing, and runs again.
SETTLED = ("reproduced", "not_reproducible", "done", "unsupported")
TRANSIENT = ("running", "infrastructure_failure", "interrupted", "cancelled")
NOT_REACHED = "It only changes the instructions of an assistant this conversation didn't use."
ASKED_IN_NOTE = "They asked again in the note they sent it in with."
NOTHING_AGAIN = ("Without the change, nothing the household had trouble with happened again, so a change can't be "
                 "shown to remove it.")
# Whether a replay's Assist turns show what the household objected to: a small model judges only that, one turn at a
# time, with the turn they objected to as the example (DETAILS P6.1.9, P6.1.11). Its model is set where the feedback
# check's was (settings' role key).
CONDUCT_ROLE = "feedback_check"
CONDUCT_INSTRUCTIONS = """A household objected to something their home assistant ("Assist") said or did in one turn of a conversation, or had to check on a result because Assist's reply there left them unsure (then what they objected to is a reply that leaves the result unclear). Each objection below has their own words (quote) and, as the example of what they objected to, that turn as it was recorded (objected_to: its turn number, then what the user said, what Assist did, each tool call with Home Assistant's result, what it replied, and what the home did: each message spoken and on which devices, each device it changed, and each script it ran); or "Assist in general" when they named no one turn. Then the same conversation replayed, turn by turn, in the same form, what the home did there being each message spoken and on which devices, each command its devices were sent, and each device whose state changed: a turn with none of these did nothing of the kind. The replay's turn with the same number is the same request asked again. For each objection, judge each replay turn it lists (judge): does that turn show what the household objected to? Answer for each with shows (true or false) and a short reason (why). Judge only what they objected to, from what the turn said and did: the tool calls, their results and what the home did are the facts of what Assist did. A turn that doesn't do it, or does something they didn't object to, doesn't show it."""
CONDUCT_STRUCTURE = probatio.Schema({probatio.Required("answers"): [{
    probatio.Required("goal"): str, probatio.Required("turn"): int, probatio.Required("shows"): bool,
    probatio.Required("why"): str}]})
# Whether a reply gives what the user asked to know (DETAILS P9.7.4): the same small model judges it, one turn at a
# time, with the home as it was then (the devices the question is about) and the form they asked for.
FACT_INSTRUCTIONS = """A household asked their home assistant ("Assist") something they wanted to know. Each question below has what they asked (asked: the turn and their words), what the answer needs (needs), the form they asked for the answer in, if any (form: their own words), the turn whose reply to judge (turn), and, when it's about the home, the devices it's about as the home had them when Assist answered (home: each device's name, its state and attributes as that turn started, and what changed in it before the reply). Then each turn to judge (turns), as it went: what the user said, what Assist did (each tool call with Home Assistant's result), what it replied, and what the home did (each message spoken and on which devices, each device changed, and in a replay each command its devices were sent; a replayed turn with none of these did nothing of the kind, while a recorded one names each script it ran). For each question, does that turn's reply give what the answer needs, true to the home as given, in the form asked for (when given)? A question with no home is beyond what the home records: judge only whether the reply answers it in the form asked for, never whether its content is true. Answer for each with gives (true or false) and a short reason (why). Judge only the question: a reply that gives it with more besides still gives it; one that is wrong about the home, leaves out what the answer needs, or ignores the form asked for doesn't."""
FACT_STRUCTURE = probatio.Schema({probatio.Required("answers"): [{
    probatio.Required("goal"): str, probatio.Required("turn"): int, probatio.Required("gives"): bool,
    probatio.Required("why"): str}]})
# What of a goal decides which turns a replay with the change may skip.
GOAL_FIELDS = ("key", "label", "kind", "entity_id", "entity_ids", "state", "states", "brightness_pct", "serves",
               "about", "quote", "fact", "outcome", "message")


class Untestable(CaseError):
    """Why an idea can't be tested on its conversation: this idea alone, or (conversation) any idea on it."""

    def __init__(self, why, *, conversation=False):
        super().__init__(why)
        self.conversation = conversation


class Transient(Exception):
    """A test the machinery cut off (a provider or worker fault, a restart, a check model that couldn't answer): it
    says nothing about the idea, and can run again."""


def engine_version(folder):
    """How replays run and are judged, as a digest of the integration's own code (every module but const.py, whose
    version an install changes whatever else it changes): a change to the code makes earlier replays a different test
    (a new plan, DETAILS P6.7.1) by itself, with nobody having to remember to say so, and an install that leaves the
    code as it was (the panel's alone, say) reuses what's kept."""
    return digest({str(p.relative_to(folder)): p.read_text() for p in sorted(Path(folder).rglob("*.py"))
                   if p.name != "const.py" and "__pycache__" not in p.parts})


REPLAY_ENGINE = engine_version(Path(__file__).parent)  # Read once, as the integration loads (off HA's event loop).


def before_plan(recording):
    """The replay without a change: the whole recorded conversation, as its assistant ran it. It doesn't depend on
    the goals, so a conversation has one (P6.6.6). Untestable (conversation) when it can't be replayed."""
    try:
        readiness = replay_readiness(recording)
        turns = recording_turns(recording)
    except CaseError as err:
        raise Untestable(str(err), conversation=True) from err
    if not readiness["model_baseline_ready"]:
        raise Untestable("It can't be replayed: " + ", ".join(readiness["gaps"]), conversation=True)
    agents = {m["agent_id"] for t in turns for m in t["messages"] if m["role"] == "assistant"}
    if len(agents) != 1:
        raise Untestable("Which assistant answered this conversation is missing or ambiguous.", conversation=True)
    first = turns[0]
    platform = first["prepared"]["context"].get("platform")
    plan = {"kind": "before", "recording_id": recording["id"], "recording_digest": digest(recording),
            "core_version": CORE_VERSION, "replay_engine": REPLAY_ENGINE, "agent_id": agents.pop(),
            "provider": deepcopy(first["prepared"]["provider_config"][0]), "platform": platform,
            "provider_tested": platform in TESTED_PROVIDERS,
            # What a turn's effects are compared with: the real changes HA recorded; for a practice session, what
            # today's engine does with the recorded calls (the practice engine of the recording day may have
            # emulated less).
            "expected_effects": "recorded_calls_rerun" if recording.get("practice") else "recorded_changes",
            "user_inputs": [t["messages"][0]["content"] for t in turns],
            "prompt_digests": [digest(t["history"][0]["content"]) for t in turns],
            "tool_digests": [digest(t["prepared"]["tools"]) for t in turns],
            "definitions": digest({k: first["source"][k] for k in ("scripts", "automations")}),
            "notes": readiness["fidelity_notes"]}
    plan["id"] = digest(plan)
    return plan


def after_plan(before, setup, goals, aside=(), judge=None):
    """The replay with a change: its before's, the change as the sandbox runs it (setup_for), and the goals, which
    decide the turns it may skip, with those set aside (aside: keys), whose turns it says, and the model that judges
    the objections and questions it may skip on (P6.7: it decides which turns are said too)."""
    plan = {"kind": "after", "before": before["id"], "setup": deepcopy(setup),
            "goals": digest([[g.get(k) for k in GOAL_FIELDS] for g in goals]),
            **({"aside": sorted(aside)} if aside else {}),
            **({"judge": judge} if any(g.get("kind") == "conduct" or g.get("kind") == "fact" and g.get("serves") is None
                                       for g in goals if g["key"] not in aside) else {})}
    plan["id"] = digest(plan)
    return plan


def setup_for(hypothesis, recording, hass):
    """(setup, tested, provider) of an idea on a conversation: its changes as the sandbox runs them, on the first
    turn's scripts and its assistant's instructions, for the person speaking; what it was tested against (the
    recorded configuration of the scripts it touches and of the exposure it changes, and the instructions it edits as
    Home Assistant has them now: applying it later finds a conflict if one changed since); the assistant it ran on."""
    if not isinstance(hypothesis, dict) or not hypothesis.get("changes"):
        raise CaseError("The idea has no changes to test.")
    turn = recording_turns(recording)[0]
    source, config = turn["source"], turn["prepared"]["provider_config"][0]
    scripts = source["scripts"]
    setup = setup_from_changes(hypothesis["changes"], source=source, prompt=config["options"].get("prompt"),
                               scripts=scripts, user_id=(turn["prepared"].get("context") or {}).get("context", {})
                               .get("user_id"), assistant=target(config))
    touched = {c["entity_id"].split(".", 1)[1] for c in hypothesis["changes"]
               if c.get("kind") in ("script", "exposure") and (c.get("entity_id") or "").startswith("script.")}
    tested = {"scripts": {oid: deepcopy(scripts[oid]) for oid in sorted(touched) if oid in scripts},
              "exposure": {e: bool(source["exposure"].get(e)) for e in sorted(setup["exposure"])}}
    if prompts := instructions_now(hass, hypothesis):
        tested["prompts"] = prompts
    return setup, tested, {k: config.get(k) for k in ("config_entry_id", "subentry_id", "title")}


def reaches(hypothesis, recording):
    """Whether an idea changes anything for this conversation (DETAILS P6.8.5): anything but edits of assistants'
    instructions does; those do only when the conversation used one of those assistants."""
    from .instructions import is_edit, ran_with
    changes = hypothesis.get("changes") or []
    if not changes or not all(is_edit(c) for c in changes):
        return True
    used = (ran_with(recording) or {}).get("target")
    return bool(used) and any(c.get("target") == used for c in changes)


def instructions_now(hass, hypothesis):
    """{target: template} of the assistants whose instructions the hypothesis edits, as Home Assistant has them while
    it's tested: applying it later finds a conflict if one changed since."""
    from .instructions import is_edit, live
    found = {}
    for change in hypothesis.get("changes") or []:
        if is_edit(change) and change.get("target") and (now := live(hass, change["target"])) is not None:
            found[change["target"]] = now["template"]
    return found


def _turn(number, user, items, home=None):
    """One turn as the judge reads it, in order (a list: the judge's input is sent with its keys sorted): its number,
    what the user said, what Assist did (each tool call with Home Assistant's result, clipped), what it replied, and
    what the home did, if anything (home: _replayed_turn's or _recorded_turn's). items: what Assist added to the
    conversation in it, as recorded or replayed."""
    calls, did, reply = {}, [], ""
    for item in items:
        if item.get("role") == "assistant":
            calls.update({call.get("id"): call for call in item.get("tool_calls") or []})
            if item.get("content") and not item.get("tool_calls"):
                reply = item["content"]
        elif item.get("role") == "tool_result":
            call = calls.get(item.get("tool_call_id")) or {}
            found = item["result"] if "result" in item else item.get("tool_result")  # tool_result: before 2026.10.
            did.append({"called": item.get("tool_name") or call.get("tool_name"), "with": call.get("tool_args"),
                        "result": _clip(json.dumps(found, ensure_ascii=False, default=str))})
    home = {k: v for k, v in (home or {}).items() if v}
    return [{"turn": number}, {"user_said": user}, {"assistant_did": did}, {"assistant_replied": reply},
            *([{"home_did": home}] if home else [])]


def _spoke(spoken):
    return [{"message": s["message"], "on": sorted(s["targets"])} for s in spoken]


def _replayed_turn(step):
    """A replay's turn as the judge reads it (_turn), with what its sandbox logged the home doing: each message spoken
    and on which devices, each command its devices were sent, and each device whose state changed."""
    return _turn(step["turn"], step["input"], step.get("output") or [], {
        "spoke": _spoke(step.get("spoken") or []),
        "commanded": [f"{c['service']} {c['entity_id']}"  # What was spoken is in spoke, once.
                      + (f" {_clip(json.dumps(c['data'], ensure_ascii=False, sort_keys=True, default=str), 200)}"
                         if c.get("data") else "") for c in step.get("commands") or []
                      if c["service"] not in SPEAKING],
        "changed": [f"{t['entity_id']}: {t['old']} → {t['new']}" for t in step.get("transitions") or []]})


def _recorded_turn(recording, recorded, number):
    """A recorded turn as the judge reads it (_turn), with what the recording shows the home doing: what a broadcast
    reached (a device available as the turn began), each device the turn's request changed, its state or its
    settings (a recording keeps no commands), and each script it ran, saying the rest of what that did isn't kept."""
    from .analyzer import ran_scripts, spoken
    turn = recorded[number - 1]
    states, calls = turn["source"].get("states") or {}, calls_in(turn["messages"][1:])
    heard = [{**s, "targets": {e for e in s["targets"] if (states.get(e) or {}).get("state") != "unavailable"}}
             for s in spoken(calls)]
    changed = []
    for change in _request_changes(recording, turn):
        old, new = change["old"], change["new"]
        if old.get("state") != new.get("state"):
            changed.append(f"{change['entity_id']}: {old.get('state')} → {new.get('state')}")
        elif settings := [k for k, v in (new.get("attributes") or {}).items()
                          if (old.get("attributes") or {}).get(k) != v][:3]:
            changed.append(f"{change['entity_id']}: " + ", ".join(
                f"{k} {(old.get('attributes') or {}).get(k)} → {new['attributes'][k]}" for k in settings))
    return _turn(number, turn["messages"][0]["content"], turn["messages"][1:],
                 {"spoke": _spoke(heard), "changed": changed, "ran_scripts": [
                     f"{s}: what it did isn't recorded, beyond any change listed" for s in ran_scripts(calls)]})


def judgeable(goal, view):
    """Whether a fact can be judged: its truth is in the record (it names a device the recording holds a state of), or
    the user asked for a form; a question beyond the home with no form asked holds nothing to check (the goal design,
    2026-10-29)."""
    recorded = view.source.get("states") or {}
    return goal.get("kind") == "fact" and (any(e in recorded for e in goal.get("entity_ids") or [])
                                           or bool((goal.get("quote") or "").strip()))


def question_of(goal, view, turn, user_said, world=None):
    """A fact as the judge reads it, for one turn's reply: what was asked, what the answer needs, the form asked for,
    and the home as it was as that turn started (the devices the question is about that the recording holds, as
    Assist saw them), with what changed in that turn before the reply. world: a replay's (measures.replay_view), whose
    changes before the turn decide the states (a recorded attribute stays only where they match the recording) and
    whose changes in it are what that replay's turn did."""
    out = {"asked": [{"turn": goal["turn"]}, {"user_said": user_said}], "needs": goal.get("fact") or "", "turn": turn}
    if (goal.get("quote") or "").strip():
        out["form"] = goal["quote"]
    start = as_seen(view.recording, view.sources[turn - 1]) if 1 <= turn <= len(view.sources) else {}
    replied = view.replies[turn - 1] if 1 <= turn <= len(view.replies) else None
    changes, home = (world or view).changes, []
    for e in [e for e in goal.get("entity_ids") or [] if e in start]:
        state = start[e].get("state")
        if world is not None:
            before = [c for c in changes.get(e, []) if c["during_turn"] < turn]
            state = before[-1]["new"] if before else (as_seen(view.recording, view.sources[0]).get(e) or {}).get(
                "state")
        changed = [{k: c[k] for k in ("old", "new", "by")} for c in changes.get(e, []) if c["during_turn"] == turn
                   and (world is not None and c["by"] == "assistant" or "between turns" not in c["by"] and (
                       replied is None or datetime.fromisoformat(c["at"]) <= datetime.fromisoformat(replied)))]
        home.append({"device": _naming(view.source, e)[0], "entity_id": e, "state": state,
                     **({"attributes": start[e].get("attributes")} if start[e].get("state") == state else {}),
                     **({"during_the_turn": changed} if changed else {})})
    if home:
        out["home"] = home
    return out


async def check_facts(hass, entity_id, questions, turns):
    """The small model's answers on whether turns' replies give what the user asked to know: {status, answers, error,
    usage}, completed only with a well-formed answer for each question. questions: [{goal, asked, needs, form?,
    home?, turn}]; turns: each judged turn as the judge reads it (_turn)."""
    from .reflective import run_role
    if not entity_id:
        return {"status": "not_run", "answers": [], "error": {"message": "No model to check with"}}
    record = await run_role(hass, role=CONDUCT_ROLE, entity_id=entity_id, instructions=FACT_INSTRUCTIONS,
                            payload={"questions": questions, "turns": turns}, structure=FACT_STRUCTURE,
                            allow_paid_inference=True)
    given = {(a.get("goal"), a.get("turn")): a for a in _well_formed(FACT_STRUCTURE, record.get("output"))}
    judged = record["status"] == "completed" and all((q["goal"], q["turn"]) in given for q in questions)
    return {"status": "completed" if judged else "failed",
            "answers": [given[k] for k in ((q["goal"], q["turn"]) for q in questions) if k in given],
            "error": record.get("error"), "usage": record["usage"]}


async def answered(hass, model, view, goals):
    """({a fact root's place among the goals: {turn, gives, why}}, the judge's record): each judgeable fact root of the
    conversation as it went, judged once, at analysis, on the reply that ends its run of steps (its own when none
    follows; a reply a step follows is unmet by the step itself, as is the last when the step is in the note). A root
    that can't be judged, or whose judge didn't answer, has none, and the analysis goes on either way."""
    try:
        recorded = recording_turns(view.recording, replay=False)
    except CaseError:
        return {}, None
    from .analyzer import question_run
    roots = {n: g for n, g in enumerate(goals) if g.get("serves") is None and g.get("kind") == "fact"}
    found, runs = {}, {n: question_run(goals, g) for n, g in roots.items()  # Not one in the note: no reply.
                       if judgeable(g, view) and g["turn"] <= len(recorded)}
    for n, run in runs.items():
        if run[-1] > len(recorded):  # Asked again in the note: its last reply said gave nothing either.
            found[n] = {"turn": max(t for t in run if t <= len(recorded)), "gives": False, "why": ASKED_IN_NOTE}
    said = lambda turn: recorded[turn - 1]["messages"][0]["content"]
    questions = [{"goal": str(n), **question_of(roots[n], view, run[-1], said(roots[n]["turn"]))}
                 for n, run in runs.items() if n not in found]
    if not questions:
        return found, None
    turns = [_recorded_turn(view.recording, recorded, t) for t in sorted({q["turn"] for q in questions})]
    heard = await check_facts(hass, model, questions, turns)
    found |= {int(a["goal"]): {k: a[k] for k in ("turn", "gives", "why")} for a in heard["answers"]
              if heard["status"] == "completed"}
    return found, heard


def _well_formed(structure, output):
    """The judge's answers that hold the shape asked for (the provider may not enforce it; keys it adds are dropped):
    none unless all do."""
    try:
        return probatio.Schema(structure.schema, extra=probatio.REMOVE_EXTRA)(output)["answers"]
    except (probatio.Invalid, TypeError):
        return []


def questions_in(goals, view, aside=()):
    """The questions a replay judges: the judgeable fact roots not set aside."""
    return [g for g in goals if g.get("serves") is None and judgeable(g, view) and g["key"] not in aside]


async def judged_facts(hass, model, recording, view, steps, questions, goals, verdicts, upto=None):
    """({key: [{turn, gives, why}]}, verdicts): the judge's answer for each question (a judgeable fact root, among the
    conversation's goals) on each turn of its run among these steps (upto: only those before that turn), with the home
    as the replay had it, kept with the replay as the objections' are: each asked once, one call for those not kept
    yet. Unjudged when the judge can't answer."""
    said, recorded = {s["turn"]: s for s in steps}, recording_turns(recording)
    named = {g["key"]: digest({**{k: g.get(k) for k in ("label", "fact", "entity_ids", "quote")}, "model": model,
                               "question": True}) for g in questions}
    known = _answers(verdicts)
    wanted = {g["key"]: [t for t in measures.run_of(goals, g) if t in said and (upto is None or t < upto)]
              for g in questions}
    if missing := [(g, t) for g in questions for t in wanted[g["key"]] if t not in known.get(named[g["key"]], {})]:
        world = measures.replay_view(recorded, view, steps)
        user = lambda n: recorded[n - 1]["messages"][0]["content"]
        heard = await check_facts(hass, model, [{"goal": str(n), **question_of(g, view, t, user(g["label"]), world)}
                                                for n, (g, t) in enumerate(missing, 1)],
                                  [_replayed_turn(said[t]) for t in sorted({t for _, t in missing})])
        if heard["status"] != "completed":
            raise Unjudged("The replies couldn't be checked against what the user asked to know.")
        found = {}
        for n, (g, _) in enumerate(missing, 1):
            found.setdefault(named[g["key"]], []).extend(
                {k: a[k] for k in ("turn", "gives", "why")} for a in heard["answers"] if a["goal"] == str(n))
        verdicts = {**verdicts, digest([[named[g["key"]], t] for g, t in missing]): {
            "status": "completed", "model": model, "answers": found, "usage": heard.get("usage")}}
        known = _answers(verdicts)
    return {g["key"]: [known[named[g["key"]]][t] for t in wanted[g["key"]]] for g in questions}, verdicts


def _judged_turns(steps, goal):
    """The replay turns a conduct goal is judged on: each said before its own turn; every one for the note's and a
    revision note's (never said)."""
    return [s["turn"] for s in steps if not isinstance(goal.get("label"), int) or s["turn"] < goal["label"]]


async def check_conduct(hass, entity_id, objections, steps):
    """The small model's answers on whether replay turns show what the household objected to: {status, answers,
    error, usage}, completed only with an answer for each turn every objection lists. Without a model it can't say
    (status not_run). objections: [{goal, quote, objected_to, judge: [turns]}]; steps: the replay's."""
    from .reflective import run_role
    if not entity_id:
        return {"status": "not_run", "answers": [], "error": {"message": "No model to check with"}}
    record = await run_role(hass, role=CONDUCT_ROLE, entity_id=entity_id, instructions=CONDUCT_INSTRUCTIONS,
                            payload={"objections": objections, "replay": [_replayed_turn(s) for s in steps]},
                            structure=CONDUCT_STRUCTURE, allow_paid_inference=True)
    given = {(a.get("goal"), a.get("turn")): a for a in _well_formed(CONDUCT_STRUCTURE, record.get("output"))}
    asked = sorted((o["goal"], t) for o in objections for t in o["judge"])
    judged = record["status"] == "completed" and all(k in given for k in asked)
    return {"status": "completed" if judged else "failed", "answers": [given[k] for k in asked if k in given],
            "error": record.get("error"), "usage": record["usage"]}


class Unjudged(Exception):
    """The judge couldn't answer (no model, or no answer for every turn it was asked about): the machinery's doing.
    Never a CaseError, which would settle a replay it cut off as unsupported."""


def judge_model(entry):
    """The model that judges replies: its own setting, else the write-up model's."""
    from .settings import role_model
    return role_model(entry, CONDUCT_ROLE) or role_model(entry, "editor")


async def judged(hass, model, recording, steps, goals, verdicts):
    """({key: [{turn, shows, why}]}, verdicts): the judge's answer for each conduct goal on each turn it's judged on
    among these steps (_judged_turns), and the verdicts kept with them. Each answer, for one goal (by its words, the
    turn it's about and the model) and one replay turn, is asked once and kept: one call for those not kept yet.
    Unjudged when the judge can't answer."""
    named = {g["key"]: digest({**{k: g.get(k) for k in ("label", "about", "quote")}, "model": model}) for g in goals}
    known = _answers(verdicts)
    wanted = {g["key"]: _judged_turns(steps, g) for g in goals}
    if missing := [(g, turns) for g in goals
                   if (turns := [t for t in wanted[g["key"]] if t not in known.get(named[g["key"]], {})])]:
        recorded = recording_turns(recording)
        asked = [{"goal": str(n), "quote": g.get("quote") or "", "judge": turns,
                  "objected_to": _recorded_turn(recording, recorded, g["about"])
                  if isinstance(g.get("about"), int) else "Assist in general"}
                 for n, (g, turns) in enumerate(missing, 1)]
        heard = await check_conduct(hass, model, asked, steps)
        if heard["status"] != "completed":
            raise Unjudged("The replies couldn't be checked against what the household objected to.")
        found = {named[g["key"]]: [{k: a[k] for k in ("turn", "shows", "why")} for a in heard["answers"]
                                   if a["goal"] == str(n)] for n, (g, _) in enumerate(missing, 1)}
        verdicts = {**verdicts, digest([[named[g["key"]], turns] for g, turns in missing]): {
            "status": "completed", "model": model, "answers": found, "usage": heard.get("usage")}}
        known = _answers(verdicts)
    return {g["key"]: [known[named[g["key"]]][t] for t in wanted[g["key"]]] for g in goals}, verdicts


def _answers(verdicts):
    """{goal's digest: {turn: answer}}, from the verdicts kept with a replay."""
    out = {}
    for check in (verdicts or {}).values():
        for name, answers in (check.get("answers") or {}).items():
            out.setdefault(name, {}).update({a["turn"]: a for a in answers})
    return out


def state_of(answers):
    """A conduct goal's state from the judge's answers on the turns it's judged on: unmet when one shows what the
    household objected to, met when none does, indeterminate with no turn to judge."""
    return "unmet" if any(a["shows"] for a in answers) else "met" if answers else "indeterminate"


async def conduct(hass, model, replays, replay, recording, goals):
    """{key: {state, answers: [{turn, shows, why}]}} for each conduct goal in this replay, judged once the replay is
    done (state_of): those it judged as it went (Trial) are kept with it; the rest, the note's and revision notes'
    among them, get one call, never a new replay, and are kept too (DETAILS P6.3.5.1). Transient when the judge can't
    answer. model: the judge's (judge_model), the one the replay with the change was judged with as it went."""
    objections = [g for g in goals if g.get("kind") == "conduct"]
    try:
        found, verdicts = await judged(hass, model, recording, replay["steps"], objections,
                                       replay.get("verdicts") or {})
    except Unjudged as err:
        raise Transient(str(err)) from err
    if verdicts != (replay.get("verdicts") or {}):  # Kept with the replay the caller holds too, for what's judged next.
        replay["verdicts"] = verdicts
        await replays.save(replay)
    return {k: {"state": state_of(answers), "answers": answers} for k, answers in found.items()}


async def questions(hass, model, replays, replay, recording, asking, goals, view):
    """{key: {state, answers: [{turn, gives, why}]}} for each question asked of this replay (asking: judgeable fact
    roots among the goals), judged on each turn of its run it said (DETAILS P9.10.4), once the replay is done: those
    judged as it went (Trial) are kept with it, the rest get one call and are kept too. Its state is the latest
    reply's (measures.question_state). Transient when the judge can't answer."""
    try:
        found, verdicts = await judged_facts(hass, model, recording, view, replay["steps"], asking, goals,
                                             replay.get("verdicts") or {})
    except Unjudged as err:
        raise Transient(str(err)) from err
    if verdicts != (replay.get("verdicts") or {}):  # Kept with the replay the caller holds too.
        replay["verdicts"] = verdicts
        await replays.save(replay)
    keyed = {g["key"]: g for g in asking}
    return {k: {"state": measures.question_state(keyed[k], None, {k: answers}), "answers": answers}
            for k, answers in found.items()}


class Replays:
    """The replays store ({plan id: replay}): each plan's replay, kept from before its first inference. A settled one
    stands; one a restart cut off is marked interrupted, and runs again when asked; a deleted conversation's go."""

    def __init__(self, hass):
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.replays", private=True, atomic_writes=True)
        self.items = {}

    async def start(self):
        saved = await self.store.async_load()
        self.items = (saved or {}).get("replays", {})
        if running := [e for e in self.items.values() if e["status"] == "running"]:
            for item in running:  # A restart lost the run mid-flight: kept, never resumed.
                self.items[item["plan"]["id"]] = {**item, "status": "interrupted"}
            await self.store.async_save({"replays": self.items})

    def get(self, plan_id):
        return deepcopy(self.items.get(plan_id))

    async def save(self, item):
        updated = {**self.items, item["plan"]["id"]: deepcopy(item)}
        await self.store.async_save({"replays": updated})
        self.items = updated
        return deepcopy(item)

    async def drop(self, recording_ids):
        """Forget deleted conversations' replays."""
        updated = {k: v for k, v in self.items.items() if v["recording_id"] not in set(recording_ids)}
        if len(updated) < len(self.items):
            await self.store.async_save({"replays": updated})
            self.items = updated

    def recordings(self):
        """The conversations replays were kept for."""
        return {item["recording_id"] for item in self.items.values()}


class Trial:
    """One replay of a conversation under its plan, from the first turn, the user's recorded words in order (through
    the turn `through`, for a developer's replay): the before, each turn's effects on the devices the recorded Assist
    changed matching the recording's (not_reproducible at the first that doesn't), or the after, with the change in
    the sandbox, saying only the turns it can't skip (DETAILS P6.4), with the judge (judge: its model) asked about
    the objections as it goes. It runs once; its sandbox closes however it ends."""

    def __init__(self, hass, plan, recording, *, before=None, goals=None, setup=None, aside=(), judge=None,
                 through=None):
        self.hass, self.plan, self.recording = hass, plan, recording
        self.assistant = before or plan  # Whose plan names the assistant and its recorded settings: the before's.
        self.goals, self.setup, self.after, self.aside = goals or [], setup or {}, plan["kind"] == "after", set(aside)
        self.judge, self.states, self.facts = judge, {}, {}  # The judge's model; its answers so far.
        self.turns = recording_turns(recording)
        self.through = through or len(self.turns)
        # The devices the recorded Assist changed itself, in any turn: what a before is judged on.
        self.mine = {t["entity_id"] for turn in self.turns for t in assist_transitions(recording, turn)}
        self.home, self.agent, self.session, self._rerun = None, None, None, None
        self.conversation_id, self.usage = f"improver-replay-{uuid4()}", []  # In its sandbox only.
        self.entry = {"plan": deepcopy(plan), "recording_id": recording["id"], "at": utcnow().isoformat(),
                      "status": "running", "error": None, "notes": list(plan.get("notes") or []), "usage": None,
                      "said": [], "steps": [], "verdicts": {}}

    async def run(self):
        # In a conversation trace of its own: what the replay's model and tools report is the replay's, never the
        # caller's (the proposer's, when a test runs as its tool).
        with async_conversation_trace():
            return await self._run()

    async def _run(self):
        """The household's assistant runs each turn in the replay's sandbox, loaded from its own config entry with the
        recorded provider settings (a change's instructions in their place): a turn runs there whole, the model seeing
        what Home Assistant builds there, which must be the recorded prompt and tools (SandboxHome.faithful)."""
        entry, setup, turns = self.entry, self.setup, self.turns
        source = turns[0]["source"]
        try:
            self.home = home = SandboxHome(
                self.hass, self.recording, turns[0], self.conversation_id,
                scripts={**source["scripts"], **(setup.get("scripts") or {})}, automations=source["automations"],
                exposure_changes=setup.get("exposure"), alias_changes=setup.get("aliases"),
                alias_removals=setup.get("alias_removals"), household=setup.get("household"),
                watch=[e for e in source["states"] if e.startswith("light.")])
            provider = deepcopy(self.assistant["provider"])
            if setup.get("prompt") is not None:
                provider["options"] = {**provider["options"], "prompt": setup["prompt"]}
            self.agent = await home.load_assistant(self.assistant["agent_id"], provider)
            said = 0
            for turn in turns[:self.through]:
                number = turn["number"]
                if number > 1:
                    if self.after and not await self._needed(number):
                        continue
                    # Others moved things in the home between the recorded turns.
                    await home.observe(turn["source"], [moved_by_others(self.recording, turns, t)
                                                        for t in turns[said:number]])
                said = number
                if not await self._turn(turn):
                    break
            else:
                entry["status"] = "done" if self.after else "reproduced"
        except asyncio.CancelledError:  # A cancelled run still closes its sandbox.
            entry["status"] = "cancelled"
            raise
        except Exception as err:
            entry["status"] = "unsupported" if isinstance(err, CaseError) else "infrastructure_failure"
            entry["error"] = {"type": type(err).__name__, "message": str(err)[:500]}
        finally:
            entry["usage"] = usage_report(self.usage)
            if self.home is not None:
                await self.home.stop()
        return entry

    async def _needed(self, number):
        """Whether the replay with the change says this turn (measures.needed), the judge asked first, over the
        replies so far, about this turn's objections and, at a wrap-up's turn, its goal's note's and revision notes'
        and any earlier one of its goal's not judged yet (its turn was said anyway), and about each question a step or
        filing on this turn hangs on (DETAILS P6.4), when the answers decide the turn. What the judge answers is part
        of this replay: when it can't answer, the replay is cut off and runs again (Unjudged, P6.3.5)."""
        goals = self.goals
        roots = {measures.root_of(goals, g) for g in goals if g["label"] == number and g.get("kind") == "wrap-up"}
        asked = [g for g in goals if g.get("kind") == "conduct" and g["key"] not in self.aside and (
            g["label"] == number or measures.root_of(goals, g) in roots and (
                not isinstance(g["label"], int) or g["label"] < number and g["key"] not in self.states))]
        hangs = {g["key"] for g in goals if g["label"] == number and g.get("kind") in ("step", "wrap-up")}
        questions = [q for q in questions_in(goals, self._session(), self.aside) if measures.under(goals, q) & hangs]
        if (asked or questions) and not measures.needed(  # It decides: asked, all met would skip the turn.
                goals, self._view(), number, self.aside, {**self.states, **{g["key"]: "met" for g in asked}},
                {**self.facts, **{q["key"]: [{"turn": 0, "gives": True}] for q in questions}}):
            if asked:
                found, self.entry["verdicts"] = await judged(self.hass, self.judge, self.recording,
                                                             self.entry["steps"], asked, self.entry["verdicts"])
                self.states.update({k: state_of(answers) for k, answers in found.items()})
            if questions:
                found, self.entry["verdicts"] = await judged_facts(
                    self.hass, self.judge, self.recording, self._session(), self.entry["steps"], questions, goals,
                    self.entry["verdicts"], upto=number)
                self.facts.update(found)
        return measures.needed(goals, self._view(), number, self.aside, self.states, self.facts)

    def _session(self):
        """The recording as the analysis read it (exploration.SessionView), made once."""
        if self.session is None:
            from .exploration import SessionView
            self.session = SessionView(self.recording)
        return self.session

    def _view(self):
        """What the home did in this replay so far, as measures judge it."""
        return measures.replay_view(self.turns, self._session(), self.entry["steps"])

    async def _turn(self, turn):
        """Replay one turn and keep it as a step; False when the before's turn didn't go as recorded."""
        number, home = turn["number"], self.home
        identity = turn["prepared"]["context"].get("context") or {}
        if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
            raise CaseError("Cannot backfill missing historical identity")
        recorded_context = llm.LLMContext(**{**turn["prepared"]["context"], "context": Context(**identity)})
        if not self.after:  # The replay with a change says turns its before has shown are rebuilt exactly.
            if not await home.faithful(turn, recorded_context):
                raise UnsupportedSimulation("The sandbox doesn't rebuild this turn's instructions and tools exactly")
        else:  # The household knowledge Assist reads: the change's, or as recorded.
            await home.prepare(turn, recorded_context)
        async with span("replay.turn", replay=self.plan["kind"], turn=number):
            found = await home.turn(turn, self.agent, self.conversation_id)
        self.usage += found["usage"]
        new = found["content"]
        external = [{"name": c["tool_name"], "args": deepcopy(c["tool_args"]), "provenance": "provider_execution"}
                    for m in new if m.get("role") == "assistant" for c in m.get("tool_calls") or [] if c.get("external")]
        if any(c["name"] != "tool_search_tool_bm25" for c in external):
            # A server tool may already have executed remotely: the replay can't tell what it did.
            raise UnsupportedSimulation("Provider tool is outside catalogue-search evaluation coverage")
        if found["unemulated"]:  # The turn's own commands, never what others' moves set off before it.
            raise UnsupportedSimulation("No emulation for " + ", ".join(found["unemulated"]))
        if found["reply"].get("response_type") == "error":  # The provider or HA failed it: the machinery's, not the run's.
            raise HomeAssistantError(((found["reply"].get("speech") or {}).get("plain") or {}).get("speech") or "error")
        if not new or new[-1].get("role") != "assistant" or not new[-1].get("content") or new[-1].get("tool_calls"):
            raise CaseError("The assistant didn't finish this turn")
        transitions, attributes = found["transitions"], found["attributes"]
        brightness = {e: [(found["before"].get(e) or {}).get("brightness"), (attributes.get(e) or {}).get("brightness")]
                      for e in home.watch}
        step = {"turn": number, "input": turn["messages"][0]["content"], "reply": new[-1]["content"], "output": new,
                "transitions": transitions, "brightness": {e: v for e, v in brightness.items() if v[0] != v[1]},
                "external_calls": external, "spoken": found["spoken"], "commands": found["commands"],
                "prompt_matches_recording": found["given"]["prompt"] == turn["history"][0]["content"]}
        self.entry["said"].append(number)
        self.entry["steps"].append(step)
        if self.after:
            return True
        # The conversation goes as it went: the devices the recorded Assist changed get the same effects, however the
        # model phrased its calls. It may vary elsewhere (a question, another device): kept as notes.
        expected = await self._expected(turn)
        mine = self.mine
        actual_by, expected_by = per_entity(setting_off(transitions)), per_entity(setting_off(expected))
        # Their lights' brightness too, where the replay knows it: a light switched on with none named comes on at the
        # level it remembers, which only the real light knew.
        lights = [] if self.recording.get("practice") else sorted(
            e for e in mine if e.startswith("light.") and (attributes.get(e) or {}).get("brightness") is not None)
        then = recorded_attributes(self.recording, turn, lights)
        step.update(recorded_transitions=expected,
                    matched=all(same_effects(actual_by.get(e, []), expected_by.get(e, [])) for e in mine)
                    and all(same_brightness(attributes.get(e), then[e]) for e in lights),
                    differences_elsewhere={e: {"replay": [list(c) for c in actual_by.get(e, [])],
                                               "recorded": [list(c) for c in expected_by.get(e, [])]}
                                           for e in sorted((set(actual_by) | set(expected_by)) - mine)
                                           if not same_effects(actual_by.get(e, []), expected_by.get(e, []))})
        if not step["matched"]:
            self.entry["status"] = "not_reproducible"
        return step["matched"]

    async def _expected(self, turn):
        if self.plan["expected_effects"] != "recorded_calls_rerun":
            return assist_transitions(self.recording, turn)
        if self._rerun is None:
            from .evidence import recorded_call_effects
            source = self.turns[0]["source"]
            self._rerun = await recorded_call_effects(self.recording, {"scripts": source["scripts"],
                                                                       "automations": source["automations"]}) or {}
        return self._rerun.get(turn["number"], [])


def usage_report(events):
    """The provider-neutral statistics HA's assistant traced for a replay's turns: absence is unknown, never zero
    usage or a billing guarantee."""
    return {"status": "provider_reported_partial" if events else "unavailable", "events": deepcopy(events),
            "billing_total": None, "provider_request_count": None,
            "limits": "HA trace statistics may omit cache writes, tool fees and retries; events are not request counts"}


async def _settled(hass, replays, plan, before, recording, **kwargs):
    """The plan's replay: a settled one as kept (never resampled); otherwise run now, kept from before its first
    inference and closed however it ends. Transient when the machinery cut it off; Untestable (nothing kept) when the
    conversation's assistant can't run it."""
    if (kept := replays.get(plan["id"])) and kept["status"] in SETTLED:
        return kept
    try:
        if assistant_of(hass, before["agent_id"], before["provider"]) is None:
            raise CaseError("The assistant this conversation used is no longer set up, so it can't be replayed.")
    except CaseError as err:
        raise Untestable(str(err), conversation=True) from err
    trial = Trial(hass, plan, recording, before=before, **kwargs)
    async with closing(replays.save, deepcopy(trial.entry)) as keep:
        async with span("replay", kind=plan["kind"], plan_id=plan["id"]) as section:
            found = await keep(await trial.run())
            section.tag(status=found["status"])
    if found["status"] in TRANSIENT:
        raise Transient(f"The replay couldn't finish: {(found.get('error') or {}).get('message') or found['status']}")
    return found


def _why(replay):
    """Why a settled replay can't be judged."""
    if replay["status"] == "not_reproducible":
        return (f"Without the change, turn {replay['steps'][-1]['turn']} didn't do what it did in the conversation, "
                "so a change can't be judged against it.")
    return (replay.get("error") or {}).get("message") or "It couldn't be replayed."


def _devices(goal):
    """The devices a goal wants something from."""
    return {e for e in [goal.get("entity_id"), *(goal.get("entity_ids") or [])] if e}


def _replay(replay, judged, heard, told, aside, source, setup):
    """A replay as its test keeps it: how it went (its replies) and how it fulfilled the goals, from where, with each
    conduct goal's and question's state, what the judge said of it (heard, told) and whether it was set aside."""
    replies = [{"turn": step["turn"], "you": said["you"], "assistant": said["assist"],
                **({"failed": said["failed"]} if said.get("failed") else {})}
               for step, said in zip(replay["steps"], replayed(replay["steps"]))]
    return {"plan": replay["plan"]["id"], "status": replay["status"], "said": list(replay["said"]), "replies": replies,
            "goals": judged["goals"], "goals_met": judged["goals_met"],
            "turns_per_root_goal": judged["turns_per_root_goal"], "counted": judged["counted"],
            "home_couldnt": judged["home_couldnt"],
            "side_effects_left": [{"entity_id": s["entity_id"], "name": _naming(source, s["entity_id"])[0],
                                   "start": s["start"], "end": s["end"], "turn": s["turn"]}
                                  for s in judged["side_effects_left"]],
            "started_from": started_from(source), "config": assist_config(source, setup),
            "conduct": {k: {"state": judged["goals"][k], "answers": (heard.get(k) or {}).get("answers", []),
                            **({"set_aside": True} if k in aside else {})} for k in heard},
            "facts": {k: {"state": judged["goals"][k], "answers": (told.get(k) or {}).get("answers", []),
                          **({"set_aside": True} if k in aside else {})} for k in told}}


async def test(hass, entry, runtime, rid, goals, hypothesis):
    """One test of an idea on its conversation (P0.1, P0.9): the replays without and with the change, each judged by
    P0.1.1's measures, and whether the change made the goals strictly easier (validated). goals: the conversation's,
    as measures.goals_of gives them. Untestable: why it can't be tested (conversation: no idea can be, on this
    conversation); Transient: the machinery's doing, and it can run again."""
    from .exploration import SessionView
    replays = runtime.replays
    try:
        recording = await runtime.recorder.export(rid)
    except CaseError as err:
        raise Untestable(str(err), conversation=True) from err
    if not reaches(hypothesis, recording):
        raise Untestable(NOT_REACHED)
    before = before_plan(recording)
    without = await _settled(hass, replays, before, before, recording)
    if without["status"] != "reproduced":
        raise Untestable(_why(without), conversation=True)
    turns, session = recording_turns(recording), SessionView(recording)
    went = measures.judge(goals, session, range(1, len(turns) + 1), measures.CONVERSATION)
    home = measures.replay_view(turns, session, without["steps"])
    # What code judges first: a before that didn't go as the conversation went costs no judge's call. What it can't
    # test is set aside (open for saying turns, indeterminate in both replays): a wrap-up it finds met, then an
    # objection none of its turns shows (DETAILS P6.6.2).
    objections = {g["key"] for g in goals if g.get("kind") == "conduct"}
    asking = questions_in(goals, session)  # The questions, and what hangs on their answers: the judge's to tell.
    hanging = {q["key"]: measures.under(goals, q) for q in asking}
    theirs = set().union(*hanging.values())
    found = measures.judge(goals, home, without["said"], {})
    aside = measures.set_aside(found, went, goals) - objections - theirs  # Those are the judge's to tell, next.
    actions = measures.told_by(goals, {g["key"] for g in goals if g.get("kind") == "action"})  # From calls.
    if why := measures.went_as_it_went(found, went, leave_out=objections | theirs | aside | actions):
        raise Untestable(why, conversation=True)
    model = judge_model(entry)
    heard_before = await conduct(hass, model, replays, without, recording, goals)
    states = {k: v["state"] for k, v in heard_before.items()}
    told_before = await questions(hass, model, replays, without, recording, asking, goals, session)
    off = measures.answered_otherwise(goals, {k: v["answers"] for k, v in told_before.items()})
    aside |= set().union(*(hanging[k] for k in off))
    facts = {k: v["answers"] for k, v in told_before.items() if k not in aside}
    aside |= measures.set_aside(measures.judge(goals, home, without["said"], states, aside, facts), went,
                                goals) & objections
    # A filing as the replay with the change decides it, at its own turn: the objections judged on the replies before.
    aside |= {g["key"] for g in goals if g.get("kind") == "wrap-up" and went["goals"].get(g["key"]) == "unmet"
              and measures.wrap_up(goals, home, g, {k: state_of([a for a in v["answers"] if a["turn"] < g["turn"]])
                                                    for k, v in heard_before.items()}, aside, facts) == "met"}
    b = measures.judge(goals, home, without["said"], states, aside, facts)
    # An action one side can't tell (a script in the recording, whose own calls it doesn't keep) isn't compared, nor
    # what's told from it.
    untold = {g["key"] for g in goals if g.get("kind") == "action" and "indeterminate" in (
        b["goals"].get(g["key"]), went["goals"].get(g["key"]))}
    if why := measures.went_as_it_went(b, went, leave_out=aside | measures.told_by(goals, untold)):
        raise Untestable(why, conversation=True)
    if (aside or {k for k in untold if went["goals"].get(k) == "unmet"}) and not b["side_effects_left"] and not any(
            v == "unmet" for k, v in b["goals"].items() if k not in aside | measures.told_by(goals, untold)):
        raise Untestable(NOTHING_AGAIN, conversation=True)  # What's set aside left nothing to remove (P6.6.5).
    try:
        setup, tested, provider = setup_for(hypothesis, recording, hass)
    except CaseError as err:
        raise Untestable(str(err)) from err
    with_change = await _settled(hass, replays, after_plan(before, setup, goals, aside, model), before, recording,
                                 goals=goals, setup=setup, aside=aside, judge=model)
    if with_change["status"] != "done":  # It can't run with this change: the idea's, not counted (P6.3.5).
        raise Untestable(_why(with_change))
    heard_after = {k: {} for k in heard_before if k in aside}  # Set aside: not judged again.
    heard_after.update(await conduct(hass, model, replays, with_change, recording,
                                     [g for g in goals if g["key"] not in aside]))
    told_after = {k: {} for k in told_before if k in aside}
    told_after.update(await questions(hass, model, replays, with_change, recording,
                                      [q for q in asking if q["key"] not in aside], goals, session))
    a = measures.judge(goals, measures.replay_view(turns, session, with_change["steps"]), with_change["said"],
                       {k: v["state"] for k, v in heard_after.items() if v}, aside,
                       {k: v["answers"] for k, v in told_after.items() if v})
    source = turns[0]["source"]
    return {"idea": hypothesis.get("title"), "changes": digest(hypothesis.get("changes")),
            "hypothesis": deepcopy(hypothesis), "at": utcnow().isoformat(),
            "judged": measures.judged_of(goals, _devices),
            "before": _replay(without, b, heard_before, told_before, aside, source, None),
            "after": _replay(with_change, a, heard_after, told_after, aside, source, setup),
            "gains": measures.gains(a, b), "validated": measures.easier(a, b),
            "setup": setup, "tested": tested, "provider": provider,
            "user_id": (turns[0]["prepared"]["context"].get("context") or {}).get("user_id"),
            "recorded": transcript(recording), "notes": list(before["notes"])}


async def replay_recording(hass, recorder, recording_id, last_turn=1):
    """A recording's first turns replayed as a before would (its assistant, a sandbox in the replay worker), each
    checked against what the recorded assistant did: for checking the replay engine end to end (DETAILS P6.12, a
    developer service). Paid: the assistant's own model. Nothing is kept."""
    recording = await recorder.export(recording_id)
    plan = before_plan(recording)
    found = await Trial(hass, plan, recording, through=last_turn).run()
    return {"agent_id": plan["agent_id"], "model": (plan["provider"].get("options") or {}).get("chat_model"),
            **{k: v for k, v in found.items() if k != "plan"}}
