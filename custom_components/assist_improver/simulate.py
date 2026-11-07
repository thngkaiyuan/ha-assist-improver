"""Simulate (SPEC.md P0.9, DETAILS P6): an idea tested on the conversation it's proposed for.

The conversation is replayed in a sandbox (sandbox.py, replay.py) set up as the household's Home Assistant is today,
with its own assistant as set up now and the user's recorded words, in order, from the moment it started (its devices'
states and time). Without the change (the baseline), the whole conversation is replayed up to three times, until a try
goes as the conversation went (DETAILS P6.6.5): Assist varies from run to run, so the trouble may not come up in one.
With the change (the test), it branches from that baseline where the change has to make the difference: the turns
before are the baseline's, its home and its history; from there it says the turns DETAILS P6.4 doesn't let it skip.
Code judges both by P0.1.1's measures (measures.py); a small model judges only whether an Assist turn shows what the
household objected to (a conduct goal), one turn at a time, with what it said and did, or whether a reply gives what
the user asked to know. Each replay runs once for its plan, which names the setup it runs on, and is kept (Replays): a
settled one is reused, never resampled, while the setup is unchanged; one the machinery cut off runs again.
"""

import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

import probatio
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from . import happened, measures, targets, touchpoints
from .cases import (CaseError, calls_in, digest, recording_turns, replay_readiness, replayed, started_from,
                    transcript)
from .changes import setup_from_changes
from .const import DOMAIN, STORAGE_VERSION, TESTED_PROVIDERS
from .digest import _clip, _naming
from .failures import closing
from .instructions import target
from .replay import SandboxHome, today as setup_today
from .sandbox import seeded
from .serialization import plain
from .simulation import (_request_changes, assist_settings, assist_transitions, moved_by_others, per_entity,
                         same_effects, same_settings, setting_off)
from .snapshot import assist_config, at_moment, on_today, renamed, renaming
from .trace import span

# How a replay can end. Settled: its result stands for its plan (never run again). Transient: the machinery's doing
# (a provider or worker fault, a restart, a cancelled run): it says nothing, and runs again.
SETTLED = ("reproduced", "not_reproducible", "done", "unsupported")
TRANSIENT = ("running", "infrastructure_failure", "interrupted", "cancelled")
NOT_REACHED = "It only changes the instructions of an assistant this conversation didn't use."
ASKED_IN_NOTE = "They asked again in the note they sent it in with."
NOTHING_AGAIN = "nothing the household had trouble with happened again"
TRIES = 3  # The baseline's tries on one setup (DETAILS P6.6.6).
# The judge's input, cut for size where it's cut, saying how much (digest._clip): a tool call's result to
# digest.RESULT_CHARS, a call's data and a failed call's error in what the home did to CALL_CHARS.
CALL_CHARS = 200
# Whether a replay's Assist turns show what the household objected to: a small model judges only that, one turn at a
# time, with the turn they objected to as the example (DETAILS P6.1.9, P6.1.11). Its model is set where the feedback
# check's was (settings' role key).
CONDUCT_ROLE = "feedback_check"
CONDUCT_INSTRUCTIONS = """A household objected to something their home assistant ("Assist") said or did in one turn of a conversation, or had to check on a result because Assist's reply there left them unsure (then what they objected to is a reply that leaves the result unclear). Each objection below has their own words (quote) and, as the example of what they objected to, that turn as it was recorded (objected_to: its turn number, then what the user said, what Assist did, each tool call with Home Assistant's result, what it replied, and what the home did: each message spoken and on which devices, each device it changed, and each script it ran); or "Assist in general" when they named no one turn. Then the same conversation replayed, turn by turn, in the same form, what the home did there being each message spoken or sound played and on which devices, each command its devices were sent, and each device whose state or settings changed: a turn with none of these did nothing of the kind. The replay's turn with the same number is the same request asked again. For each objection, judge each replay turn it lists (judge): does that turn show what the household objected to? Answer for each with shows (true or false) and a short reason (why). Judge only what they objected to, from what the turn said and did: the tool calls, their results and what the home did are the facts of what Assist did. A turn that doesn't do it, or does something they didn't object to, doesn't show it."""
CONDUCT_STRUCTURE = probatio.Schema({probatio.Required("answers"): [{
    probatio.Required("goal"): str, probatio.Required("turn"): int, probatio.Required("shows"): bool,
    probatio.Required("why"): str}]})
# Whether a reply gives what the user asked to know (DETAILS P9.7.4): the same small model judges it, one turn at a
# time, with the home as it was then (the devices the question is about), what Assist's tools returned in that turn,
# and the form they asked for.
FACT_INSTRUCTIONS = """A household asked their home assistant ("Assist") something they wanted to know. Each question below has what they asked (asked: the turn and their words), what the answer needs (needs), the form they asked for the answer in, if any (form: their own words), the turn whose reply to judge (turn), and, when it's about the home, the devices it's about as the home had them when Assist answered (home: each device's name, its state and attributes as that turn started, and what changed in it before the reply). Then each turn to judge (turns), as it went: what the user said, what Assist did (each tool call with Home Assistant's result), what it replied, and what the home did (each message spoken or sound played and on which devices, each device changed, and in a replay each command its devices were sent; a replayed turn with none of these did nothing of the kind, while a recorded one names each script it ran). For each question, does that turn's reply give what the answer needs, true to the home as given and to what that turn's tool calls returned (what Assist saw before replying), in the form asked for (when given)? A question neither speaks to is beyond what's recorded: judge only whether the reply answers it, in the form asked for when given, never whether its content is true. Answer for each with gives (true or false) and a short reason (why). Judge only the question: a reply that gives it with more besides still gives it; one that is wrong about the home or a tool's result, leaves out what the answer needs, or ignores the form asked for doesn't."""
FACT_STRUCTURE = probatio.Schema({probatio.Required("answers"): [{
    probatio.Required("goal"): str, probatio.Required("turn"): int, probatio.Required("gives"): bool,
    probatio.Required("why"): str}]})
# What of a goal decides which turns a replay with the change may skip.
GOAL_FIELDS = ("key", "label", "kind", "entity_id", "entity_ids", "state", "states", "attributes", "brightness_pct",
               "serves", "about", "quote", "fact", "outcome")


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


def agent_of(recording):
    """The assistant that answered a conversation (its entity id), and what limits its replay (its notes,
    cases.replay_readiness): Untestable (conversation) when it can't be replayed."""
    if "practice" in recording:  # Left by earlier versions: its devices were simulated, so there's nothing to match.
        raise Untestable("It can't be replayed: it was recorded in sandbox mode", conversation=True)
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
    return agents.pop(), readiness["notes"]


# What of an entity Assist can't see a replay doesn't depend on: how it's named, and whether it has a state now.
UNSEEN = ("name", "original_name", "aliases")


def setup_digest(today):
    """What a plan carries of the setup its replay runs on (DETAILS P6.7.1): a digest of what a replay depends on in it
    (replay.today's): what the sandbox is seeded with besides the devices' states (sandbox.seeded, and setup_of's
    entries, helpers and actions), not how what Assist can't see is named; and the assistant's provider and settings
    (its subentry's data), never its provider entry's data or options, which hold its credentials."""
    home, states = seeded(today["home"]), today["home"].get("states") or {}
    exposed = {e for e, on in (home.get("exposure") or {}).items() if on}
    entities = {e: {k: v for k, v in x.items() if e in exposed or k not in UNSEEN} for e, x in home["entities"].items()}
    seen = {(entities.get(e) or {}).get("device_id") for e in exposed}
    devices = {d: x if d in seen else {"area_id": x.get("area_id")} for d, x in home["devices"].items()}
    named = {e: ((states.get(e) or {}).get("attributes") or {}).get("friendly_name") for e in exposed}
    entry = today["assistant"]["entry"]
    return digest({"home": {**{k: v for k, v in home.items() if k not in ("exposure", "entities", "devices")},
                            "entities": entities, "devices": devices, "exposed": named,
                            "all": sorted({*entities, *states})},
                   "assistant": plain({"domain": entry["domain"], "settings": entry["subentries"][0].get("data")}),
                   **{k: today[k] for k in ("tools", "timers")},
                   **{k: today.get(k) for k in ("entries", "helpers", "services")}})


def before_plan(recording, today, attempt=1):
    """The replay without a change: the whole recorded conversation, said to its assistant in today's setup (today:
    replay.today's). It doesn't depend on the goals, so a conversation has one per setup and try (attempt: 1 to TRIES,
    P6.6.6). Untestable (conversation) when it can't be replayed."""
    agent, notes = agent_of(recording)
    platform, options = today["assistant"]["entry"]["domain"], today["provider"]["options"]
    if platform not in TESTED_PROVIDERS:
        notes = [*notes, "provider_not_yet_tested_for_replay"]
    if any(options.get(k) not in (None, False, "off") for k in ("code_execution", "web_fetch", "web_search")):
        notes = [*notes, "hosted_tools_kept_on; provider_execution_is_not_intercepted_by_HA"]
    plan = {"kind": "before", "try": attempt, "recording_id": recording["id"], "recording_digest": digest(recording),
            "core_version": CORE_VERSION, "replay_engine": REPLAY_ENGINE, "setup": setup_digest(today),
            "agent_id": agent, "provider": deepcopy(today["provider"]), "platform": platform,
            "provider_tested": platform in TESTED_PROVIDERS,
            "expected_effects": "recorded_changes",  # What a turn's effects are compared with.
            "user_inputs": [t["messages"][0]["content"] for t in recording_turns(recording)], "notes": notes}
    plan["id"] = digest(plan)
    return plan


def tried_again(before):
    """The plan of the baseline's next try, after this one (before_plan's)."""
    plan = {**{k: v for k, v in before.items() if k != "id"}, "try": before["try"] + 1}
    plan["id"] = digest(plan)
    return plan


async def planned(hass, recording):
    """(before, today): today's setup for replaying a conversation (replay.today), read once for its baseline and
    the tests compared with it, and its replay without a change on it (before_plan). Untestable (conversation) when
    it can't be replayed."""
    agent, _ = await hass.async_add_executor_job(agent_of, recording)
    try:
        today = await setup_today(hass, agent)
    except CaseError as err:
        raise Untestable(str(err), conversation=True) from err
    return await hass.async_add_executor_job(before_plan, recording, today), today  # Its digest reads it all.


def after_plan(before, setup, goals, aside=(), judge=None, branch=1):
    """The replay with a change: the baseline it branches from (before: its plan) at its branch turn, the change as
    the sandbox runs it (setup_for), and the goals, which decide the turns it may skip, with those set aside (aside:
    keys), whose turns it says, and the model that judges the objections and questions it may skip on (P6.7: it decides
    which turns are said too)."""
    plan = {"kind": "after", "before": before["id"], "branch": branch, "setup": deepcopy(setup),
            "goals": digest([[g.get(k) for k in GOAL_FIELDS] for g in goals]),
            **({"aside": sorted(aside)} if aside else {}),
            **({"judge": judge} if any(g.get("kind") == "conduct" or g.get("kind") == "fact" and g.get("serves") is None
                                       for g in goals if g["key"] not in aside) else {})}
    plan["id"] = digest(plan)
    return plan


def setup_for(hypothesis, recording, today, hass):
    """(setup, tested, provider) of an idea on a conversation: its changes as the sandbox runs them, on today's setup
    (today: replay.today's) and its assistant's instructions now, for the person speaking; what it was tested against
    (each item, registry field, exposure, instructions and LLM APIs it changes, as Home Assistant has them now:
    applying it later finds a conflict if one changed since); the assistant it ran on."""
    if not isinstance(hypothesis, dict) or not hypothesis.get("changes"):
        raise CaseError("The idea has no changes to test.")
    turn = recording_turns(recording)[0]
    source, config = at_moment(today["home"], turn["source"]), today["provider"]
    scripts = source.get("scripts") or {}
    setup = setup_from_changes(hypothesis["changes"], source=source, prompt=config["options"].get("prompt"),
                               scripts=scripts, user_id=(turn["prepared"].get("context") or {}).get("context", {})
                               .get("user_id"), assistant=target(config))
    return setup, tested_against(hass, hypothesis, setup, source), \
        {k: config.get(k) for k in ("config_entry_id", "subentry_id", "title")}


def tested_against(hass, hypothesis, setup, source):
    """What a change was tested against, as the home has it (source): each item it saves (None: a new one), each
    automation it turns on or off, each registry field it sets, the exposure it changes; and, as Home Assistant has
    them, the instructions and LLM APIs it edits."""
    from .changes import apis_now
    states, registry = source.get("states") or {}, setup.get("registry") or {}
    items = {"scripts": source.get("scripts") or {},
             **{key: {str(i.get("id")): i for i in source.get(key) or [] if i.get("id") is not None}
                for key in ("automations", "scenes")}}
    tested = {key: found for key in ("scripts", "automations", "scenes") if (found := {
        i: deepcopy(items[key][i]) for i in sorted(setup.get(key) or {}) if i in items[key]})}
    tested["exposure"] = {e: bool((source.get("exposure") or {}).get(e)) for e in sorted(setup["exposure"])}
    if setup.get("automation_on"):
        tested["automation_on"] = {e: (states.get(e) or {}).get("state") == "on" for e in setup["automation_on"]}
    if registry:
        tested["registry"] = {key: {i: {f: ((source.get(key) or {}).get(i) or {}).get(f) for f in fields}
                                    for i, fields in found.items()} for key, found in registry.items()}
    if prompts := instructions_now(hass, hypothesis):
        tested["prompts"] = prompts
    if apis := {c["target"]: apis_now(hass, c["target"]) for c in hypothesis.get("changes") or []
                if c.get("kind") == "assistant" and "llm_hass_api" in c and c.get("target")}:
        tested["apis"] = apis
    return tested


def reaches(hypothesis, recording):
    """Whether an idea changes anything for this conversation (DETAILS P6.8.5): an assistant change reaches only a
    conversation with that assistant, and a recommendation none (it's never replayed); everything else does."""
    from .instructions import ran_with
    changes = hypothesis.get("changes") or []
    if not changes or any(c.get("kind") != "assistant" for c in changes):
        return True
    used = (ran_with(recording) or {}).get("target")
    return bool(used) and any(c.get("target") == used and ({"text", "replaces", "llm_hass_api"} & set(c))
                              for c in changes)


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


def _home_did(items, actions, source, changes):
    """What the home did in a turn, read the same way recorded or replayed (happened.py), by its call log, else (a
    recording from before one was kept) by Assist's tool calls' results: each message said or sound played on devices,
    and on which (one unavailable as the turn began, by source, reached nothing); each other call of its call log, its
    data and a failed one's error clipped (CALL_CHARS); each change (entity_id, old, new, attributes_changed) of a
    device's settable state, its state and every setting one of its actions sets (targets.settable), a reading aside;
    and, where the turn kept no call log, each script it ran, saying what that did isn't recorded beyond any change
    listed. items: what Assist added to the conversation in the turn."""
    states, calls = source.get("states") or {}, calls_in(items)
    spoke = [{"message": s["message"], **({"media": s["media"]} if s["media"] else {}),
              "on": sorted(e for e in s["targets"] if (states.get(e) or {}).get("state") != "unavailable")}
             for s in happened.announcements(calls if actions is None else [], actions, source) if s["targets"]]
    clip = lambda text: _clip(text, CALL_CHARS)  # noqa: E731
    called = [f"{a['action']}" + (f" {clip(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))}"
                                  if (data := a.get("data")) else "")
              + (f": failed, {clip(str(a['error']))}" if a.get("error") else "")
              for a in actions or [] if a.get("error") or not (  # Said on devices: in spoke, once.
                  happened.carried(a.get("data") or {}) and happened.named(a["action"], a.get("data") or {}, source))]
    changed = []
    for change in changes:
        settable = targets.settable(change["entity_id"].split(".", 1)[0])
        said = [f"{change['old']} → {change['new']}"] * (change["old"] != change["new"]) + [
            f"{k} {old} → {new}" for k, (old, new) in (change.get("attributes_changed") or {}).items()
            if k in settable]
        if said:
            changed.append(f"{change['entity_id']}: {', '.join(said)}")
    return {"spoke": spoke, "called": called, "changed": changed, "ran_scripts": [
        f"{s}: what it did isn't recorded, beyond any change listed" for s in happened.ran_scripts(calls)
        if actions is None]}


def _replayed_turn(step, turns):
    """A replay's turn as the judge reads it (_turn), with what the home did in it (_home_did): its call log, read
    against the recorded turn's home as it began, as a recorded turn's and measures' are (an announcement to an area
    reaches its satellites; one unavailable reaches nothing), and each change its sandbox logged. turns: the
    recording's (cases.recording_turns)."""
    return _turn(step["turn"], step["input"], step.get("output") or [], _home_did(
        step.get("output") or [], step.get("actions") or [], turns[step["turn"] - 1]["source"],
        step.get("changes") or []))


def _recorded_turn(recording, recorded, number):
    """A recorded turn as the judge reads it (_turn), with what the home did in it (_home_did): its call log where the
    recording keeps one (P8.2.10), and each change of a device's state or settings the turn's request made."""
    turn = recorded[number - 1]
    raw = (recording.get("turns") or [])[number - 1] if number <= len(recording.get("turns") or []) else {}
    changes = [{"entity_id": c["entity_id"], "old": c["old"].get("state"), "new": c["new"].get("state"),
                "attributes_changed": {k: [(c["old"].get("attributes") or {}).get(k), v]
                                       for k, v in (c["new"].get("attributes") or {}).items()
                                       if (c["old"].get("attributes") or {}).get(k) != v}}
               for c in _request_changes(recording, turn)]
    return _turn(number, turn["messages"][0]["content"], turn["messages"][1:], _home_did(
        turn["messages"][1:], raw.get("actions") if "actions" in raw else None, turn["source"], changes))


def judgeable(goal, view, goals):
    """Whether a fact can be judged (DETAILS P9.10.4): its truth is in the record, as the home (it names a device the
    recording holds a state of) or what a tool reported to Assist in a turn of its run (analyzer.question_run;
    happened.reported: an action's report of what it did tells nothing of a question), or the user asked for a form;
    a question beyond both with no form asked holds nothing to check."""
    from .analyzer import question_run
    recorded, calls = view.source.get("states") or {}, getattr(view, "calls", None) or {}
    return goal.get("kind") == "fact" and (
        any(e in recorded for e in goal.get("entity_ids") or [])
        or any(happened.reported(c) for t in question_run(goals, goal) for c in calls.get(t) or [])
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
    start = (view.sources[turn - 1].get("states") or {}) if 1 <= turn <= len(view.sources) else {}
    replied = view.replies[turn - 1] if 1 <= turn <= len(view.replies) else None
    changes, home = (world or view).changes, []
    for e in [e for e in goal.get("entity_ids") or [] if e in start]:
        state = start[e].get("state")
        if world is not None:
            before = [c for c in changes.get(e, []) if c["during_turn"] < turn]
            state = before[-1]["new"] if before else ((view.sources[0].get("states") or {}).get(e) or {}).get("state")
        during = [c for c in changes.get(e, []) if c["during_turn"] == turn
                  and (world is not None and c["by"] == "assistant" or "between turns" not in c["by"] and (
                      replied is None or datetime.fromisoformat(c["at"]) <= datetime.fromisoformat(replied)))]
        changed = [{k: c[k] for k in ("old", "new", "by")} for c in during if not c.get("reading")]
        if readings := [c for c in during if c.get("reading")]:  # One line, last: from the first to the last.
            changed.append({"old": readings[0]["old"], "new": readings[-1]["new"], "by": readings[-1]["by"],
                            "readings": len(readings)})
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
                       if judgeable(g, view, goals) and g["turn"] <= len(recorded)}
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
    return [g for g in goals if g.get("serves") is None and judgeable(g, view, goals) and g["key"] not in aside]


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
                                  [_replayed_turn(said[t], recorded) for t in sorted({t for _, t in missing})])
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


async def check_conduct(hass, entity_id, objections, steps, turns):
    """The small model's answers on whether replay turns show what the household objected to: {status, answers,
    error, usage}, completed only with an answer for each turn every objection lists. Without a model it can't say
    (status not_run). objections: [{goal, quote, objected_to, judge: [turns]}]; steps: the replay's; turns: the
    recording's."""
    from .reflective import run_role
    if not entity_id:
        return {"status": "not_run", "answers": [], "error": {"message": "No model to check with"}}
    record = await run_role(hass, role=CONDUCT_ROLE, entity_id=entity_id, instructions=CONDUCT_INSTRUCTIONS,
                            payload={"objections": objections, "replay": [_replayed_turn(s, turns) for s in steps]},
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
        heard = await check_conduct(hass, model, asked, steps, recorded)
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
    """One replay of a conversation under its plan, on today's setup (today: replay.today's, the one its plan names),
    the user's recorded words in order (through the turn `through`, for a developer's replay): the before, from the
    first turn, each turn's effects on the devices the recorded Assist changed matching the recording's
    (not_reproducible at the first that doesn't), or the after, with the change in the sandbox, from its branch turn
    on (branch: its plan's), the turns before its baseline's (baseline: that replay, as kept): its steps, its home after
    the turn before and its history, saying only the turns it can't skip (DETAILS P6.4), with the judge (judge: its
    model) asked about the objections as it goes (session: the recording as the analysis read it,
    exploration.SessionView, where the caller has one). It runs once; its sandbox closes however it ends."""

    def __init__(self, hass, plan, recording, today, *, goals=None, setup=None, aside=(), judge=None, through=None,
                 session=None, baseline=None):
        self.hass, self.plan, self.recording, self.today = hass, plan, recording, today
        self.goals, self.setup, self.after, self.aside = goals or [], setup or {}, plan["kind"] == "after", set(aside)
        self.baseline, self.branch = baseline, plan.get("branch", 1)
        self.judge, self.states, self.facts = judge, {}, {}  # The judge's model; its answers so far.
        self.turns = recording_turns(recording)
        self.through = through or len(self.turns)
        # The devices the recorded Assist changed itself, in any turn, their state or a setting: what a before is judged
        # on.
        self.mine = {t["entity_id"] for turn in self.turns for t in assist_transitions(recording, turn)} | {
            e for turn in self.turns for e in assist_settings(recording, turn)}
        self.home, self.agent, self.session = None, None, session
        self.conversation_id, self.usage = f"improver-replay-{uuid4()}", []  # In its sandbox only.
        self.unheard = False  # A turn the model finished reported no usage.
        self.entry = {"plan": deepcopy(plan), "recording_id": recording["id"], "at": utcnow().isoformat(),
                      "status": "running", "error": None, "notes": list(plan.get("notes") or []), "usage": None,
                      "said": [], "steps": [], "verdicts": {}}

    async def run(self):
        """The household's assistant runs each turn in the replay's sandbox, loaded from its own config entry as it's
        set up now (a change's instructions in place of its own): a turn runs there whole, the model seeing what Home
        Assistant builds there."""
        entry, turns, saying = self.entry, self.turns, None
        try:
            for turn in turns[self.branch - 1:self.through]:  # Each said as its user: else it can't start (P6.2.14).
                identity = turn["prepared"]["context"].get("context") or {}
                if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
                    raise CaseError("Cannot backfill missing historical identity")
            if self.after and self.session is None:  # What the replay with the change is judged on.
                from .exploration import session_view
                self.session = await session_view(self.hass, self.recording, setup=False)
            said = self.branch - 1  # The turns before the branch: the baseline's (DETAILS P6.2).
            carried = [s for s in self.baseline["steps"] if s["turn"] <= said] if said else []
            self.home = home = SandboxHome(
                self.hass, self.recording, turns[0], self.conversation_id, self.today, change=self.setup,
                watch=sorted(self.mine),
                start=carried[-1]["home"] if carried else None, at=turns[said]["source"].get("at"))
            self.agent = await home.load_assistant()
            if carried:
                await home.carry(carried)
                entry["said"], entry["steps"] = [s["turn"] for s in carried], deepcopy(carried)
                entry["verdicts"] = before_the_branch(self.baseline.get("verdicts"), self.branch)
            for turn in turns[said:self.through]:
                number = saying = turn["number"]
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
            # The turn it was at, once it was saying its turns (DETAILS P6.6.5); without one, it couldn't start.
            entry["error"] = {"type": type(err).__name__, "message": str(err)[:500],
                              **({"turn": saying} if saying else {})}
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
        questions = [q for q in questions_in(goals, self.session, self.aside) if measures.under(goals, q) & hangs]
        if (asked or questions) and not measures.needed(  # It decides: asked, all met would skip the turn.
                goals, self._view(), number, self.aside, {**self.states, **{g["key"]: "met" for g in asked}},
                {**self.facts, **{q["key"]: [{"turn": 0, "gives": True}] for q in questions}}):
            if asked:
                found, self.entry["verdicts"] = await judged(self.hass, self.judge, self.recording,
                                                             self.entry["steps"], asked, self.entry["verdicts"])
                self.states.update({k: state_of(answers) for k, answers in found.items()})
            if questions:
                found, self.entry["verdicts"] = await judged_facts(
                    self.hass, self.judge, self.recording, self.session, self.entry["steps"], questions, goals,
                    self.entry["verdicts"], upto=number)
                self.facts.update(found)
        return measures.needed(goals, self._view(), number, self.aside, self.states, self.facts)

    def _view(self):
        """What the home did in this replay so far, as measures judge it."""
        return measures.replay_view(self.turns, self.session, self.entry["steps"])

    async def _turn(self, turn):
        """Replay one turn and keep it as a step; False when the before's turn didn't go as recorded."""
        number, home = turn["number"], self.home
        async with span("replay.turn", replay=self.plan["kind"], turn=number):
            found = await home.turn(turn, self.agent, self.conversation_id)
        self.usage += found["usage"]
        new = found["content"]
        # Hosted tools run at the provider, and calls beyond the home are stood in (sandbox.outside): outside
        # receipts (DETAILS P11.7), as is what wasn't simulated (unemulated), the turn's own.
        external = [{"name": c["tool_name"], "args": deepcopy(c["tool_args"]), "provenance": "provider_execution"}
                    for m in new if m.get("role") == "assistant" for c in m.get("tool_calls") or [] if c.get("external")]
        if found["reply"].get("response_type") == "error":  # The provider or HA failed it: the machinery's, not the run's.
            raise HomeAssistantError(((found["reply"].get("speech") or {}).get("plain") or {}).get("speech") or "error")
        if not new or new[-1].get("role") != "assistant" or not new[-1].get("content") or new[-1].get("tool_calls"):
            raise CaseError("The assistant didn't finish this turn")
        self.unheard = self.unheard or not found["usage"]
        transitions, attributes = found["transitions"], found["attributes"]
        step = {"turn": number, "input": turn["messages"][0]["content"], "reply": new[-1]["content"], "output": new,
                "transitions": transitions, "changes": found["changes"],
                "external_calls": external, "actions": found["actions"], "commands": found["commands"],
                "outside": found["outside"], "not_simulated": found["unemulated"]}
        self.entry["said"].append(number)
        self.entry["steps"].append(step)
        if self.after:
            return True
        # The conversation goes as it went (DETAILS P6.6.1): the devices the recorded Assist changed get the same
        # effects, however the model phrased its calls: their states, the settings the recorded Assist set on them
        # (simulation.same_settings), and each command that failed at home failing again. It may vary elsewhere (a
        # question, another device): kept as notes.
        expected = assist_transitions(self.recording, turn)
        mine = self.mine
        actual_by, expected_by = per_entity(setting_off(transitions)), per_entity(setting_off(expected))
        asked, source = assist_settings(self.recording, turn), turn["source"]
        failed = lambda actions: {e for a in actions or [] if happened.at_home(a)  # noqa: E731
                                  for e in happened.named(a["action"], a.get("data") or {}, source)}
        step.update(home=found["home"], recorded_transitions=expected,  # Its home: where a test may branch from.
                    matched=all(same_effects(actual_by.get(e, []), expected_by.get(e, [])) for e in mine)
                    and all(same_settings(e.split(".", 1)[0], settings, attributes.get(e) or {},
                                          targets.shown(source, e)) for e, settings in asked.items())
                    and failed(turn.get("actions")) <= failed(found["actions"]),
                    differences_elsewhere={e: {"replay": [list(c) for c in actual_by.get(e, [])],
                                               "recorded": [list(c) for c in expected_by.get(e, [])]}
                                           for e in sorted((set(actual_by) | set(expected_by)) - mine)
                                           if not same_effects(actual_by.get(e, []), expected_by.get(e, []))})
        if not step["matched"]:
            self.entry["status"] = "not_reproducible"
        return step["matched"]


def before_the_branch(verdicts, branch):
    """The judge's answers kept with a baseline on its turns before a test's branch, which the test carries: what was
    said of those turns stands, never asked again."""
    out = {}
    for name, check in (verdicts or {}).items():
        answers = {goal: kept for goal, found in (check.get("answers") or {}).items()
                   if (kept := [a for a in found if a["turn"] < branch])}
        if answers:
            out[name] = {**check, "answers": answers, "usage": None}  # Paid for by the baseline.
    return out


def usage_report(events):
    """The provider-neutral statistics HA's assistant traced for a replay's turns: absence is unknown, never zero
    usage or a billing guarantee."""
    return {"status": "provider_reported_partial" if events else "unavailable", "events": deepcopy(events),
            "billing_total": None, "provider_request_count": None,
            "limits": "HA trace statistics may omit cache writes, tool fees and retries; events are not request counts"}


async def _settled(hass, replays, plan, recording, today, **kwargs):
    """The plan's replay, on the setup it names (today): a settled one as kept (never resampled); otherwise run now,
    kept from before its first inference and closed however it ends. Transient when the machinery cut it off."""
    if (kept := replays.get(plan["id"])) and kept["status"] in SETTLED:
        return kept
    trial = Trial(hass, plan, recording, today, **kwargs)
    async with closing(replays.save, deepcopy(trial.entry)) as keep:
        async with span("replay", kind=plan["kind"], plan_id=plan["id"]) as section:
            found = await keep(await trial.run())
            section.tag(status=found["status"])
    _heard(hass, replays, found, today["assistant"]["entry"]["domain"], trial.unheard)
    if found["status"] in TRANSIENT:
        raise Transient(f"The replay couldn't finish: {(found.get('error') or {}).get('message') or found['status']}")
    return found


def _heard(hass, replays, replay, platform, unheard):
    """A replay's usage, as its provider (platform: its assistant's integration) reports it on each turn's chat log: a
    turn the model finished with none (unheard), from a provider that reported it on this replay's other turns or on
    its replays before, means the provider stopped reporting it there. Some providers never report it: no break."""
    if not unheard:
        if replay["usage"]["events"]:
            touchpoints.ok(hass, "provider_usage")
    elif replay["usage"]["events"] or any(r["plan"].get("platform") == platform and (r.get("usage") or {}).get("events")
                                          for r in replays.items.values()):
        touchpoints.report(hass, "provider_usage", f"{platform} reported no usage for a replay's turn")


def _why(replay):
    """Why a replay that ran into what it can't replay can't be judged."""
    return (replay.get("error") or {}).get("message") or "It couldn't be replayed."


class Unlike(Exception):
    """A try of the baseline that didn't go as the conversation went (DETAILS P6.6.1, P6.6.2), or stopped as
    unsupported once it was saying its turns (P6.6.5): why, in a few words."""


def unlike(whys):
    """Why the conversation is not reproducible once none of its baseline's tries went as it went (DETAILS P6.6.5):
    how many ran, and why each didn't, said once."""
    return f"Tried {len(whys)} times without the change: {'; '.join(dict.fromkeys(whys))}."


def _devices(goal):
    """The devices a goal wants something from."""
    return {e for e in [goal.get("entity_id"), *(goal.get("entity_ids") or [])] if e}


def _replay(replay, judged, heard, told, aside, source, seeded, setup):
    """A replay as its test keeps it: how it went (its replies) and how it fulfilled the goals, from the moment it
    started (source: the recording's first snapshot) and on what (seeded: today's setup at that moment), with each
    conduct goal's and question's state, what the judge said of it (heard, told) and whether it was set aside."""
    replies = [{"turn": step["turn"], "you": said["you"], "assistant": said["assist"],
                **({"failed": said["failed"]} if said.get("failed") else {})}
               for step, said in zip(replay["steps"], replayed(replay["steps"]))]
    return {"plan": replay["plan"]["id"], "status": replay["status"], "said": list(replay["said"]), "replies": replies,
            "goals": judged["goals"], "goals_met": judged["goals_met"],
            "turns_per_root_goal": judged["turns_per_root_goal"], "counted": judged["counted"],
            "home_couldnt": judged["home_couldnt"],
            "side_effects_left": [{"entity_id": s["entity_id"], "name": _naming(seeded, s["entity_id"])[0],
                                   **{k: s[k] for k in ("start", "end", "turn", "settings") if k in s}}
                                  for s in judged["side_effects_left"]],
            "started_from": started_from(source), "config": assist_config(seeded, setup),
            "conduct": {k: {"state": judged["goals"][k], "answers": (heard.get(k) or {}).get("answers", []),
                            **({"set_aside": True} if k in aside else {})} for k in heard},
            "facts": {k: {"state": judged["goals"][k], "answers": (told.get(k) or {}).get("answers", []),
                          **({"set_aside": True} if k in aside else {})} for k in told}}


async def test(hass, entry, runtime, rid, goals, hypothesis):
    """One test of an idea on its conversation (P0.1, P0.9): the replays without and with the change, each judged by
    P0.1.1's measures, and whether the change made the goals strictly easier (validated). goals: the conversation's,
    as measures.goals_of gives them. Untestable: why it can't be tested (conversation: no idea can be, on this
    conversation); Transient: the machinery's doing, and it can run again."""
    from .exploration import session_view
    replays = runtime.replays
    try:
        recording = await runtime.recorder.export(rid)
    except CaseError as err:
        raise Untestable(str(err), conversation=True) from err
    if not reaches(hypothesis, recording):
        raise Untestable(NOT_REACHED)
    # One setup for the baseline and the test compared with it: once it has changed, the baseline runs again. A device
    # whose entity ID changed since is the same device under its new one, in the conversation and its goals alike.
    before, today = await planned(hass, recording)
    renames = renamed(recording, today["home"])
    recording, goals = renaming(recording, renames), renaming(goals, renames)
    turns, session = recording_turns(recording), await session_view(hass, recording, setup=False)
    went = measures.judge(goals, session, range(1, len(turns) + 1), measures.CONVERSATION)
    model = judge_model(entry)
    # The baseline: up to TRIES whole replays without the change, each under a plan of its own and kept, until one goes
    # as the conversation went; a try the machinery cut off isn't one, and runs again (Transient).
    whys = []
    while True:
        without = await _settled(hass, replays, before, recording, today)
        try:
            baseline = await _baseline(hass, model, replays, without, recording, goals, session, went)
            break
        except Unlike as err:
            whys.append(str(err))
        if before["try"] == TRIES:
            raise Untestable(unlike(whys), conversation=True)
        before = tried_again(before)
    b, heard_before, told_before, aside = (baseline[k] for k in ("judged", "conduct", "facts", "aside"))
    try:
        setup, tested, provider = setup_for(hypothesis, recording, today, hass)
    except CaseError as err:
        raise Untestable(str(err)) from err
    # The test branches from the baseline where the change has to make the difference (DETAILS P6.2).
    branch = measures.branch(goals, b, baseline["home"], aside, {
        k: [a["turn"] for a in v["answers"] if a["shows"]] for k, v in heard_before.items()})
    with_change = await _settled(hass, replays, after_plan(before, setup, goals, aside, model, branch), recording,
                                 today, goals=goals, setup=setup, aside=aside, judge=model, session=session,
                                 baseline=without)
    if with_change["status"] != "done":  # It can't run with this change: the idea's, not counted (P6.3.5).
        raise Untestable(_why(with_change))
    heard_after = {k: {} for k in heard_before if k in aside}  # Set aside: not judged again.
    heard_after.update(await conduct(hass, model, replays, with_change, recording,
                                     [g for g in goals if g["key"] not in aside]))
    told_after = {k: {} for k in told_before if k in aside}
    told_after.update(await questions(hass, model, replays, with_change, recording,
                                      [q for q in baseline["asking"] if q["key"] not in aside], goals, session))
    a = measures.judge(goals, measures.replay_view(turns, session, with_change["steps"]), with_change["said"],
                       {k: v["state"] for k, v in heard_after.items() if v}, aside,
                       {k: v["answers"] for k, v in told_after.items() if v})
    source = turns[0]["source"]
    seeded = at_moment(today["home"], source)
    return {"idea": hypothesis.get("title"), "changes": digest(hypothesis.get("changes")),
            "hypothesis": deepcopy(hypothesis), "at": utcnow().isoformat(),
            "judged": measures.judged_of(goals, _devices),
            "before": _replay(without, b, heard_before, told_before, aside, source, seeded, None),
            "after": _replay(with_change, a, heard_after, told_after, aside, source, seeded, setup),
            "gains": measures.gains(a, b), "validated": measures.easier(a, b),
            "setup": setup, "tested": tested, "provider": provider,
            "user_id": (turns[0]["prepared"]["context"].get("context") or {}).get("user_id"),
            "recorded": transcript(recording), "notes": list(before["notes"])}


async def _baseline(hass, model, replays, without, recording, goals, session, went):
    """A try of the baseline, judged (without: its replay, settled): {judged, conduct, facts, aside, asking, home} once
    it went as the conversation went (went: judged; DETAILS P6.6.1, P6.6.2); Unlike, with why, when it didn't, or
    stopped as unsupported once it was saying its turns, as another try may go otherwise (P6.6.5); Untestable
    (conversation) when it couldn't start, as no try could."""
    if without["status"] == "not_reproducible":
        raise Unlike(f"turn {without['steps'][-1]['turn']} didn't do what it did in the conversation")
    if without["status"] == "unsupported" and (without["error"] or {}).get("turn"):
        raise Unlike(_why(without))
    if without["status"] != "reproduced":
        raise Untestable(_why(without), conversation=True)
    turns = recording_turns(recording)
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
        raise Unlike(why)
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
        raise Unlike(why)
    if (aside or {k for k in untold if went["goals"].get(k) == "unmet"}) and not b["side_effects_left"] and not any(
            v == "unmet" for k, v in b["goals"].items() if k not in aside | measures.told_by(goals, untold)):
        raise Unlike(NOTHING_AGAIN)  # What's set aside left nothing to remove (P6.6.2).
    return {"judged": b, "conduct": heard_before, "facts": told_before, "aside": aside, "asking": asking,
            "home": home}


async def replay_recording(hass, recorder, recording_id, last_turn=1):
    """A recording's first turns replayed as a before would (its assistant, a sandbox in the replay worker, today's
    setup), each checked against what the recorded assistant did: for checking the replay engine end to end (DETAILS
    P6.12, a developer service). Paid: the assistant's own model. Nothing is kept."""
    recording = await recorder.export(recording_id)
    plan, today = await planned(hass, recording)
    found = await Trial(hass, plan, on_today(recording, today["home"]), today, through=last_turn).run()
    return {"agent_id": plan["agent_id"], "model": (plan["provider"].get("options") or {}).get("chat_model"),
            **{k: v for k, v in found.items() if k != "plan"}}
