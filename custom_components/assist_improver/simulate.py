"""Simulate (SPEC.md P0.1, DETAILS P6): an idea tested on the conversation it's proposed for.

The whole conversation is replayed in a sandbox (sandbox.py, replay.py) with the household's own assistant and the
user's recorded words, in order, from the home and time it started from: once without the change (the before), which
must go as the conversation went, and once with it (the after), leaving out the turns DETAILS P6.4 lets it skip. Code
judges both by P0.1.1's measures (measures.py); a small model judges only whether replies follow the household's
feedback. Each replay runs once for its plan and is kept (Replays): a settled one is reused, never resampled; one the
machinery cut off runs again.
"""

import asyncio
from copy import copy, deepcopy
from dataclasses import is_dataclass, replace
from types import MappingProxyType

import probatio
from homeassistant.components import conversation
from homeassistant.components.conversation.trace import async_conversation_trace
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from . import measures
from .cases import CaseError, digest, recording_turns, replay_readiness, replayed, started_from, transcript
from .changes import setup_from_changes
from .const import DOMAIN, REPLAY_ENGINE, STORAGE_VERSION, TESTED_PROVIDERS
from .digest import _naming
from .failures import closing
from .instructions import target
from .replay import FrozenTurnLog, check_fidelity
from .serialization import plain
from .simulation import (UnsupportedSimulation, assist_transitions, per_entity, recorded_attributes, same_brightness,
                         setting_off)
from .snapshot import assist_config
from .trace import span

# How a replay can end. Settled: its result stands for its plan (never run again). Transient: the machinery's doing
# (a provider or worker fault, a restart, a cancelled run): it says nothing, and runs again.
SETTLED = ("reproduced", "not_reproducible", "done", "unsupported")
TRANSIENT = ("running", "infrastructure_failure", "interrupted", "cancelled")
NOT_REACHED = "It only changes the instructions of an assistant this conversation didn't use."
# Whether a replay's replies follow what the household said about how Assist behaved: a small model judges only that.
FEEDBACK_ROLE = "feedback_check"
FEEDBACK_INSTRUCTIONS = """A household told their home assistant how it should behave (feedback, below). The same conversation was then replayed: you see what the user said in each turn and what the assistant replied. For each piece of feedback, say whether the replies follow it (followed), with a short reason. Judge only what the feedback asks for, from the replies' words; whether the devices did the right thing is checked separately. When a reply does what the feedback asked against, or the feedback asks for something the replies don't show, it is not followed."""
FEEDBACK_STRUCTURE = probatio.Schema({probatio.Required("verdicts"): [{
    probatio.Required("feedback"): str, probatio.Required("followed"): bool, probatio.Required("why"): str}]})
# What of a goal decides which turns a replay with the change may skip, and whether its replies are checked.
GOAL_FIELDS = ("key", "label", "kind", "entity_id", "entity_ids", "state", "states", "brightness_pct", "serves")


class Untestable(CaseError):
    """Why an idea can't be tested on its conversation: this idea alone, or (conversation) any idea on it."""

    def __init__(self, why, *, conversation=False):
        super().__init__(why)
        self.conversation = conversation


class Transient(Exception):
    """A test the machinery cut off (a provider or worker fault, a restart, a check model that couldn't answer): it
    says nothing about the idea, and can run again."""


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


def after_plan(before, setup, goals):
    """The replay with a change: its before's, the change as the sandbox runs it (setup_for), and the goals, which
    decide the turns it may skip and whether its replies are checked against feedback."""
    plan = {"kind": "after", "before": before["id"], "setup": deepcopy(setup),
            "goals": digest([[g.get(k) for k in GOAL_FIELDS] + [bool(g.get("feedback"))] for g in goals])}
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


def clone_provider(agent, plan):
    """Copy per-run options onto the user's own assistant; keep HA's provider code and client.

    Anthropic and OpenRouter are tested. Other providers get the same copy and run labelled as not yet tested
    (plan["provider_tested"]); a provider that caches options outside its subentry may not see a candidate's change.
    """
    provider = plan["provider"]
    if agent is None:  # Removed, or its provider isn't loaded.
        raise CaseError("The assistant this conversation used is no longer set up, so it can't be replayed.")
    if (agent.entry.entry_id != provider["config_entry_id"]
            or agent.subentry.subentry_id != provider["subentry_id"]):
        raise CaseError("Selected HA agent does not match the recorded provider entry")
    options = deepcopy(provider["options"])
    clone = copy(agent)
    if is_dataclass(agent.subentry):
        clone.subentry = replace(agent.subentry, data=MappingProxyType(options))
    else:  # In-process fixture subentry; production uses HA's frozen dataclass.
        clone.subentry = copy(agent.subentry)
        clone.subentry.data = MappingProxyType(options)
    module = type(agent).__module__
    if module == "homeassistant.components.anthropic.conversation":
        from homeassistant.components.anthropic.const import CONF_CHAT_MODEL, DEFAULT
        # As HA's own entity picks it: recommended settings name no model, and HA runs those on its default.
        model = options.get(CONF_CHAT_MODEL, DEFAULT[CONF_CHAT_MODEL])
        if not model:
            raise CaseError("Recorded model is missing")
        model_info, found = agent.entry.runtime_data.get_model_info(model)
        if not found:
            raise CaseError("Recorded model has no cached provider metadata; do not silently substitute a model")
        clone.model_info = model_info
    elif module == "homeassistant.components.open_router.conversation":
        if not options.get("model"):
            raise CaseError("Recorded model is missing")
        clone.model = options["model"]  # OpenRouter reads its model at setup, not from the subentry per message.
    return clone


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


async def check_feedback(hass, entity_id, feedback, turns):
    """The small model's verdict on whether a replay's replies follow each piece of the household's feedback:
    {status, verdicts, followed (all of it), error, usage}. Without a model it can't say (status not_run). turns:
    [{turn, input, reply}]."""
    from .reflective import run_role
    if not entity_id:
        return {"status": "not_run", "verdicts": [], "followed": None, "error": {"message": "No model to check with"}}
    record = await run_role(hass, role=FEEDBACK_ROLE, entity_id=entity_id, instructions=FEEDBACK_INSTRUCTIONS,
                            payload={"feedback": list(feedback),
                                     "replay": [{"turn": t["turn"], "user": t["input"], "assistant": t["reply"]}
                                                for t in turns]},
                            structure=FEEDBACK_STRUCTURE, allow_paid_inference=True)
    verdicts = (record.get("output") or {}).get("verdicts", [])
    judged = record["status"] == "completed" and len(verdicts) >= len(feedback)
    return {"status": "completed" if judged else "failed", "verdicts": verdicts, "error": record.get("error"),
            "followed": judged and all(v["followed"] for v in verdicts), "usage": record["usage"]}


async def verdicts(hass, entry, replays, replay, goals):
    """{text: followed} for each feedback text the goals ask for, in this replay's replies. Each text is checked once
    and kept with the replay (P6.3.5.1): one check for those not kept yet, never a new replay. Transient when the
    check can't run."""
    from .settings import role_model
    texts = sorted({t for g in goals for t in g.get("feedback") or []})
    known = {t: f for check in (replay.get("verdicts") or {}).values() for t, f in check["followed"].items()}
    if missing := [t for t in texts if t not in known]:
        heard = await check_feedback(hass, role_model(entry, "feedback_check") or role_model(entry, "editor"),
                                     missing, replay["steps"])
        if heard["status"] != "completed":
            raise Transient("The replies couldn't be checked against the household's feedback.")
        by_text = {v.get("feedback"): v.get("followed") for v in heard["verdicts"]}
        followed = {t: bool(by_text[t] if t in by_text else heard["verdicts"][i]["followed"])
                    for i, t in enumerate(missing)}
        await replays.save({**replay, "verdicts": {**(replay.get("verdicts") or {}), digest(missing): {
            "status": "completed", "followed": followed, "verdicts": heard["verdicts"], "usage": heard.get("usage")}}})
        known.update(followed)
    return {t: known[t] for t in texts}


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
    the sandbox, saying only the turns it can't skip (DETAILS P6.4). It runs once; its sandbox closes however it
    ends."""

    def __init__(self, hass, plan, recording, agent, *, goals=None, setup=None, through=None):
        self.hass, self.plan, self.recording, self.agent = hass, plan, recording, agent
        self.goals, self.setup, self.after = goals or [], setup or {}, plan["kind"] == "after"
        self.turns = recording_turns(recording)
        self.through = through or len(self.turns)
        # The devices the recorded Assist changed itself, in any turn: what a before is judged on.
        self.mine = {t["entity_id"] for turn in self.turns for t in assist_transitions(recording, turn)}
        self.log, self.session, self._rerun = None, None, None
        self.entry = {"plan": deepcopy(plan), "recording_id": recording["id"], "at": utcnow().isoformat(),
                      "status": "running", "error": None, "notes": list(plan.get("notes") or []), "usage": None,
                      "said": [], "steps": [], "verdicts": {}}

    async def run(self):
        # In a conversation trace of its own: what the replay's model and tools report is the replay's, never the
        # caller's (the proposer's, when a test runs as its tool).
        with async_conversation_trace():
            return await self._run()

    async def _run(self):
        entry, setup, source = self.entry, self.setup, self.turns[0]["source"]
        try:
            self.log = FrozenTurnLog(self.hass, self.recording,
                                     scripts={**source["scripts"], **(setup.get("scripts") or {})},
                                     automations=source["automations"], exposure_changes=setup.get("exposure"),
                                     alias_changes=setup.get("aliases"), alias_removals=setup.get("alias_removals"),
                                     prompt_template=setup.get("prompt"),
                                     use_sandbox_prompt=bool(setup.get("prompt_changed")),
                                     household=setup.get("household"))
            if setup.get("prompt_changed"):
                # The change shows in Assist's instructions, so the model sees the prompt HA builds in the sandbox.
                # That is faithful only if, unchanged, the sandbox builds exactly the recorded prompt.
                report = await check_fidelity(self.hass, self.recording)
                if not report["prompt_matches"]:
                    raise UnsupportedSimulation("The sandbox doesn't rebuild this conversation's instructions exactly: "
                                                + " | ".join(report["prompt_diff"][2:8]))
            for turn in self.turns[:self.through]:
                if turn["number"] > 1:
                    if self.after and measures.skippable(self.goals, self._view(), turn["number"]):
                        continue
                    self.log.say(turn)
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
            if self.log is not None:
                entry["usage"] = self.log.usage_report()
                await self.log.close()
        return entry

    def _view(self):
        """What the home did in this replay so far, as measures judge it."""
        if self.session is None:
            from .exploration import SessionView
            self.session = SessionView(self.recording)
        return measures.replay_view(self.turns, self.session, self.entry["steps"])

    async def _turn(self, turn):
        """Replay one turn and keep it as a step; False when the before's turn didn't go as recorded."""
        log, number = self.log, turn["number"]
        start, moved, called = len(log.content), len(log.replay_api.world.transitions), len(log.external_calls)
        async with span("replay.turn", replay=self.plan["kind"], turn=number):
            await self.agent._async_handle_message(log.input, log)
        new = log.content[start:]
        if (not new or log.unresponded_tool_results or new[-1].role != "assistant" or not new[-1].content
                or new[-1].tool_calls):
            raise CaseError("The assistant didn't finish this turn")
        world = log.replay_api.world
        transitions = deepcopy(world.transitions[moved:])
        brightness = {e: [(log.turn_start_attributes.get(e) or {}).get("brightness"),
                          (world.attributes.get(e) or {}).get("brightness")] for e in log.replay_api.watch}
        step = {"turn": number, "input": log.input.text, "reply": new[-1].content,
                "output": plain([c.as_dict() for c in new]), "transitions": transitions,
                "brightness": {e: v for e, v in brightness.items() if v[0] != v[1]},
                "external_calls": deepcopy(log.external_calls[called:]),
                "prompt_matches_recording": log.prompt_matches_recording}
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
            e for e in mine if e.startswith("light.") and (world.attributes.get(e) or {}).get("brightness") is not None)
        then = recorded_attributes(self.recording, turn, lights)
        step.update(recorded_transitions=expected,
                    matched=all(actual_by.get(e, []) == expected_by.get(e, []) for e in mine)
                    and all(same_brightness(world.attributes.get(e), then[e]) for e in lights),
                    differences_elsewhere={e: {"replay": [list(c) for c in actual_by.get(e, [])],
                                               "recorded": [list(c) for c in expected_by.get(e, [])]}
                                           for e in sorted((set(actual_by) | set(expected_by)) - mine)
                                           if actual_by.get(e, []) != expected_by.get(e, [])})
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


async def _settled(hass, replays, plan, before, recording, **kwargs):
    """The plan's replay: a settled one as kept (never resampled); otherwise run now, kept from before its first
    inference and closed however it ends. Transient when the machinery cut it off; Untestable (nothing kept) when the
    conversation's assistant can't run it."""
    if (kept := replays.get(plan["id"])) and kept["status"] in SETTLED:
        return kept
    try:
        agent = clone_provider(conversation.async_get_agent(hass, before["agent_id"]), before)
    except CaseError as err:
        raise Untestable(str(err), conversation=True) from err
    trial = Trial(hass, plan, recording, agent, **kwargs)
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


def _replay(replay, judged, followed, source, setup):
    """A replay as its test keeps it: how it went (its replies) and how it fulfilled the goals, from where."""
    replies = [{"turn": step["turn"], "you": said["you"], "assistant": said["assist"],
                **({"failed": said["failed"]} if said.get("failed") else {})}
               for step, said in zip(replay["steps"], replayed(replay["steps"]))]
    return {"plan": replay["plan"]["id"], "status": replay["status"], "said": list(replay["said"]), "replies": replies,
            "goals": judged["goals"], "goals_met": judged["goals_met"],
            "turns_per_root_goal": judged["turns_per_root_goal"], "counted": judged["counted"],
            "side_effects_left": [{"entity_id": s["entity_id"], "name": _naming(source, s["entity_id"])[0],
                                   "start": s["start"], "end": s["end"], "turn": s["turn"]}
                                  for s in judged["side_effects_left"]],
            "started_from": started_from(source), "config": assist_config(source, setup), "feedback": followed}


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
    heard_before = await verdicts(hass, entry, replays, without, goals)
    b = measures.judge(goals, measures.replay_view(turns, session, without["steps"]), without["said"], heard_before)
    went = measures.judge(goals, session, range(1, len(turns) + 1), measures.CONVERSATION)
    if why := measures.went_as_it_went(b, went):
        raise Untestable(why, conversation=True)
    try:
        setup, tested, provider = setup_for(hypothesis, recording, hass)
    except CaseError as err:
        raise Untestable(str(err)) from err
    with_change = await _settled(hass, replays, after_plan(before, setup, goals), before, recording, goals=goals,
                                 setup=setup)
    if with_change["status"] != "done":  # It can't run with this change: the idea's, not counted (P6.3.5).
        raise Untestable(_why(with_change))
    heard_after = await verdicts(hass, entry, replays, with_change, goals)
    a = measures.judge(goals, measures.replay_view(turns, session, with_change["steps"]), with_change["said"],
                       heard_after)
    source = turns[0]["source"]
    return {"idea": hypothesis.get("title"), "changes": digest(hypothesis.get("changes")),
            "hypothesis": deepcopy(hypothesis), "at": utcnow().isoformat(),
            "judged": measures.judged_of(goals, _devices),
            "before": _replay(without, b, heard_before, source, None),
            "after": _replay(with_change, a, heard_after, source, setup),
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
    agent = clone_provider(conversation.async_get_agent(hass, plan["agent_id"]), plan)
    found = await Trial(hass, plan, recording, agent, through=last_turn).run()
    return {"agent_id": plan["agent_id"], "model": (plan["provider"].get("options") or {}).get("chat_model"),
            **{k: v for k, v in found.items() if k != "plan"}}
