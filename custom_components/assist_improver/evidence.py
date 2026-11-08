"""Facts from HA's own engine about a hypothesis, so the AI roles judge evidence instead of guessing it.

All of it runs in sandboxes set up as the household's Home Assistant is today, at a recorded session's moment
(today: replay.setup_now's; snapshot.at_moment), the setup replays run on; no model is called and no device is
touched.

- implementation: HA loads the hypothesis's scripts, each runs from the state its goals started in, and the goal's
  devices are compared with the goal; a new alias is matched by HA's own name matching.
- resolution signals: the recorded HA tool calls of every session the change could affect (sessions.applies: its calls
  or requests touch the change's devices, their names or areas, or what Assist can see) are replayed without and with
  the change, and the calls HA now resolves differently are listed. A session none of whose calls touch any of that
  resolves exactly as before, so it isn't replayed; how many weren't, and why, is said. They are signals to weigh, not
  regressions: a change that folds several flows into one more general script makes the old calls resolve differently
  while the new script covers them.
- capabilities: each of HA's Assist tools is tried on each device, and what HA carries out is what Assist can do.
- instructions: an assistant's instructions as an assistant change edits them, rendered by HA for that assistant's newest
  conversation as its chat log renders them (ha_name, user_name, llm_context): what Assist would read, or HA's error;
  and for a request without a signed-in person or an asking device, which HA renders them for too.
- an automation's week: the home as the conversation ended, an automation change on top, run through a simulated week
  on the sandbox's clock: each automation run, when, and what it changed; for a change to one the home has, without
  the change too (DETAILS P6.13).

The sandboxes run in replay workers (worker.py): each job below that starts one (JOBS) runs whole in a fresh worker
of its own.
"""

import asyncio
from copy import deepcopy

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from . import clock, happened, targets
from .analyzer import done, reached
from .cases import CaseError, recording_turns, replayable_turns
from .changes import RESOLVING, changed, setup_from_changes
from .failures import contained
from .instructions import assistant_config, is_edit, target
from .replay import today as setup_today
from .sandbox import SANDBOX_KEY, Sandbox, observe, seed, seed_options, setup_of
from .serialization import plain
from .sessions import applies, footprint
from .simulate import agent_of
from .simulation import ended, moved_by_others, recorded
from .snapshot import at_moment, on_today, renamed, renaming
from .touchpoints import Broken
from .weeks import week
from .worker import job

CONCURRENT = 4  # Workers running the jobs below at once, per instance (worker.job).
SIGNALS_SHOWN = 15  # Resolution signals listed in full; the count covers the rest.
JOBS = {"replay": "_replay", "run_scripts": "run_scripts", "match_aliases": "match_aliases",
        "capabilities": "probe_capabilities", "render_instructions": "render_instructions",
        "week": "run_week"}  # What a worker runs here, by name: each starts its own sandbox.
WEEK_DAYS = 7  # An automation's week (automation_week): simulated days,
WEEK_SECONDS = 60  # within this many real seconds.
UNCHECKED = "Home Assistant couldn't check this: "  # A fact the sandbox couldn't give (failures.contained).
NOT_REPLAYABLE = "can't be replayed"
RENDERED_SHOWN = 2000  # Characters of rendered instructions shown with a draft.
SIGNALS_NOTE = ("Each is a recorded tool call that Home Assistant resolves differently with the change: a signal to "
                "weigh, not proof of a regression. It may be a problem (a name now matches another device, a device "
                "is no longer reachable), or expected (a more general script or rule now covers what the old call "
                "did). Judge each against what the user wanted in that session.")


def goal_targets(analysis, turn):
    """What the goals asked for at a turn want, each as the implementation check compares it (run_scripts) by the
    analysis's own readers (analyzer.reached and done), for every world-state goal there: the goal and its parts, or a
    repair of an earlier turn's (DETAILS P6.9's whole goal). A state goal's target State (targets.target: its state and
    each setting asked for), a sequence goal's states, an action's outcome and devices, and what the user chose for
    settings they left open."""
    out = []
    for g in analysis.get("goals", []):
        if g["turn"] != turn:
            continue
        if g["kind"] == "state":
            out.append(_target(g))
        elif g["kind"] == "sequence":
            out.append({"kind": "sequence", "entity_id": g["entity_id"], "states": g["states"]})
        elif g["kind"] == "action":
            out.append({"kind": "action", "outcome": g.get("outcome"), "entity_ids": g.get("entity_ids") or []})
        elif g["kind"] == "ask":
            out += [_target(c) for c in g.get("user_chose", [])]
    return out


def _target(goal):
    """A device's target State, as a state goal names it (targets.target)."""
    state, settings = targets.target(goal)
    return {"kind": "state", "entity_id": goal["entity_id"], "state": state,
            **({"attributes": settings} if settings else {})}


def _context(turn):
    ctx = turn["prepared"]["context"]
    identity = ctx.get("context", {})
    if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
        return None
    return llm.LLMContext(**{**ctx, "context": Context(**identity)})


async def _start(recording, turn, today, setup=None):
    """A sandbox seeded as today's home at this turn's moment (snapshot.at_moment), the person speaking with their
    rights today (today's person, worker.job), with a hypothesis's setup (setup_from_changes's) on top."""
    setup = setup or {}
    source = changed(at_moment(today["home"], turn["source"]), setup)
    sandbox = await Sandbox.start(source.get("time_zone") or "UTC", source, setup_of(today))
    try:
        await sandbox.run(lambda hass: seed(hass, person=today.get("person"), **seed_options(today, turn),
                                            scripts=deepcopy(source.get("scripts") or {}),
                                            recorded=recorded(recording, today["tools"]),
                                            automations=source.get("automations") or [],
                                            exposure_changes=setup.get("exposure")))
    except BaseException:
        await sandbox.stop()
        raise
    return sandbox


def normalize(data):
    """What a tool call resolved to, comparable across runs: the devices acted on, or the error."""
    if not isinstance(data, dict):
        return {"result": str(data)[:200]}
    if "error" in data and len(data) <= 2:
        return {"error": data.get("error"), "detail": str(data.get("error_text", ""))[:200]}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if "response_type" in data or "response_type" in inner:
        body = inner if "response_type" in inner else data
        targets = body.get("data") or {}
        return {"response": body.get("response_type"),
                "acted_on": sorted(t.get("id") for t in targets.get("success", []) if t.get("type") == "entity"),
                "failed": sorted(t.get("id") for t in targets.get("failed", []) if isinstance(t, dict))}
    if isinstance(data.get("result"), str):
        return {"result": data["result"]}
    return {"result": data.get("result", data)}


async def _call(hass, api, name, args):
    try:
        result = await api.async_call_tool(llm.ToolInput(tool_name=name, tool_args=deepcopy(args)))
    except HomeAssistantError as err:
        return {"error": type(err).__name__, "detail": str(err)[:200]}
    except Exception as err:  # noqa: BLE001 - a call HA can't run here is a fact too, not a crash.
        return {"error": type(err).__name__, "detail": str(err)[:200]}
    return normalize(result.data if isinstance(result, llm.ToolResult) else result)


async def replay_calls(recording, today, setup=None):
    """Each recorded HA tool call of a session, replayed in order without the model: [(turn, tool, args, result)]."""
    return await job("replay", recording=recording, today=today, setup=setup)


async def capabilities(recording, entity_ids, today):
    """What Assist can do with each device: each Assist tool tried on it by name in a sandbox, and what HA did."""
    return await job("capabilities", recording=recording, entity_ids=sorted(entity_ids), today=today)


async def _replay(recording, today, setup=None):
    """In the worker: replay_calls."""
    recording = on_today(recording, today["home"])
    turns = replayable_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return None
    sandbox = await _start(recording, turns[0], today, setup)
    out = []
    try:
        for index, turn in enumerate(turns):
            if index:
                moves = [moved_by_others(recording, turns, turn)]
                await sandbox.run(lambda hass: observe(hass, turn["source"], moves))
            context = _context(turn) or context
            calls = [c for c in turn["calls"] if not c.get("external")]
            if not calls:
                continue

            async def run(hass, calls=calls, context=context, turn=turn):
                api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)
                return [(turn["number"], c["tool_name"], c["tool_args"], await _call(hass, api, c["tool_name"],
                                                                                  c["tool_args"])) for c in calls]
            out += await sandbox.run(run)
            await sandbox.run(lambda hass: hass.async_block_till_done())
    finally:
        await sandbox.stop()
    return out


class Evidence:
    """Facts for one generator run's sessions, on today's setup (today: replay.setup_now's); the replays without a
    change are reused across hypotheses."""

    def __init__(self, sessions, today):
        self.sessions, self.today = sessions, today
        self._baselines, self._lock = {}, asyncio.Lock()

    def setup(self, hypothesis, session=None):
        """A hypothesis's changes as a sandbox runs them, on today's setup and the instructions of the session's
        assistant now (the household's newest session's by default)."""
        view = self.sessions.views[session] if session else self.sessions.household
        turn = recording_turns(view.recording)[0]
        assistant = target(assistant_config(turn["prepared"]["provider_config"]))
        record = self.sessions.assistants().get(assistant)
        return setup_from_changes(self.sessions.with_targets(hypothesis.get("changes")),
                                  source=at_moment(self.today["home"], turn["source"]),
                                  prompt=self.sessions.current_template(record) if record else None,
                                  scripts=self._scripts(),
                                  user_id=(turn["prepared"].get("context") or {}).get("context", {}).get("user_id"),
                                  assistant=assistant)

    async def render(self, view, templates, user_name=None, quiet_only=False):
        """Each template rendered as an assistant's instructions for this session's first request, and for one
        without a signed-in person or an asking device, in a sandbox of its home: [{"rendered"} or {"error"}], or
        None when it can't seed one (render_instructions; quiet_only: the latter only)."""
        return await job("render_instructions", recording=view.recording, today=self.today,
                         templates=list(templates), user_name=user_name, quiet_only=quiet_only)

    async def instructions(self, hypothesis, hass=None):
        """Each assistant's instructions as the hypothesis's assistant changes edit them: rendered by HA (what Assist
        would read), or why not."""
        changes, facts = self.sessions.with_targets(hypothesis.get("changes")), []
        for target in sorted({c["target"] for c in changes if is_edit(c) and c.get("target")}):
            record = self.sessions.assistants()[target]
            template, problems = self.sessions.edited(target, changes)
            fact = {"assistant": record["name"]}
            if problems:
                fact["problems"] = problems
            rendered = await self.sessions.render(hass, target, template)
            if rendered is None:
                fact["renders"] = None
                fact["note"] = "No conversation with it can seed a sandbox, so it wasn't rendered."
            elif "error" in rendered:
                fact.update(renders=False, error=rendered["error"])
            else:
                fact["renders"] = True
                if "rendered" in rendered:
                    fact["assist_reads"] = rendered["rendered"][:RENDERED_SHOWN]
                    fact["rendered_for"] = f"its newest conversation that can be replayed ({record['replayable']})"
                elif rendered.get("renders_quietly"):
                    fact["note"] = ("No conversation with it can seed a sandbox: rendered only for a request without a "
                                    "signed-in person or an asking device.")
            facts.append(fact)
        return facts

    def _scripts(self):
        return deepcopy(self.today["home"].get("scripts") or {})

    async def check(self, hypothesis, hass=None):
        """The facts for a draft; problems with the change itself (Home Assistant doesn't accept it, or can't load
        it) come first."""
        await self.sessions.check_changes(hass, [hypothesis])
        found = [f"Change {n} ({c.get('kind')}): {p}" for n, c in enumerate(hypothesis.get("changes") or [], 1)
                 for p in self.sessions.checked_of(c) or []]
        try:
            setup = self.setup(hypothesis)
        except CaseError as err:
            found.append(str(err))
        if found:
            return {"problems": found}
        implementation = await self.implementation(hypothesis, setup)
        edited = await self.instructions(hypothesis, hass)
        if any(setup.get(k) for k in RESOLVING):
            # An edit of an assistant's instructions doesn't change how HA resolves a recorded call.
            changes = hypothesis.get("changes") or []
            fp = {**footprint(changes, self.sessions.household, self.sessions.devices_of(changes)), "assistants": set()}
            replayable = {s for s, b in self.sessions.briefs.items() if b["replayable"]}
            affected = {s for s in replayable if applies(self.sessions.briefs[s], fp)}
            signals = await self.resolution_signals(setup, affected)
            if replayable - affected:
                signals["not_replayed"] = {
                    "count": len(replayable - affected),
                    "why": "None of their recorded calls or requests touch the change's devices, their names or "
                           "areas, or what Assist can see, so they resolve as before."}
        else:
            signals = {"note": "Only instruction lines change: HA resolves devices as before; their effect needs a "
                               "replay with the model."}
        from .hypotheses.checks import speech
        talk = speech(hypothesis, self.today["home"].get("scripts"))  # What it would have Assist say, for the reviewer.
        return {"implementation": implementation, "resolution_signals": signals, **({"speech": talk} if talk else {}),
                **({"instructions": edited} if edited else {})}

    async def implementation(self, hypothesis, setup):
        """Each new or edited script, run from the state each named goal started in; new aliases and names, matched
        by HA."""
        facts = []
        for ref in hypothesis.get("goals") or []:
            session, turn_no = ref["session"][:8], ref["goal_turn"]
            analysis, view = self.sessions.analyses.get(session), await self.sessions.load(session)
            if analysis is None or view is None:
                continue
            renames = renamed(view.recording, self.today["home"])  # Its devices under today's entity IDs.
            recording = renaming(view.recording, renames)
            turns = replayable_turns(recording)
            if not 1 <= turn_no <= len(turns):  # Not there, or a conversation that can't be replayed.
                continue
            targets = renaming(goal_targets(analysis, turn_no), renames)
            # New and edited scripts, and existing ones the change exposes: what Assist would now run.
            exposed = [e.split(".", 1)[1] for e, on in setup["exposure"].items() if on and e.startswith("script.")]
            scripts = sorted(set(setup["scripts"]) | set(exposed))
            if not scripts:
                continue
            # A goal HA couldn't check is said so (a problem of the run's, unless it's a case it can't replay); the
            # other facts still come.
            async with contained("propose", f"{session}:{turn_no}", step="script check", expected=(CaseError,)) as one:
                facts.append(await self._try_scripts(recording, turns[turn_no - 1], setup, scripts, targets,
                                                     f"{session}:{turn_no}"))
            if one.failed:
                facts.append({"goal": f"{session}:{turn_no}", "reaches_goal": None, "note": UNCHECKED + one.error})
        names = {e: list(a) for e, a in setup["aliases"].items()}  # New aliases and names: what Assist matches.
        for entity_id, fields in ((setup.get("registry") or {}).get("entities") or {}).items():
            if fields.get("name"):
                names.setdefault(entity_id, []).append(fields["name"])
        for entity_id, aliases in names.items():
            async with contained("propose", entity_id, step="alias match", expected=(CaseError,)) as one:
                facts.append(await self._match_aliases(setup, entity_id, aliases))
            if one.failed:
                facts.append({"alias_for": entity_id, "matches": None, "note": UNCHECKED + one.error})
        return facts

    async def _run_scripts(self, recording, turn, setup, scripts, targets):
        return await job("run_scripts", recording=recording, turn=turn, today=self.today, setup=setup,
                         scripts=scripts, goals=targets)

    async def _try_scripts(self, recording, turn, setup, scripts, targets, goal):
        """Each script alone from the goal's starting state; if none reaches the goal alone, all of them together
        (one goal may need several, such as a script per door). Order never decides the result. A script that takes
        inputs from Assist would run here with nothing given, so it's left to the replays."""
        defined = {**self._scripts(), **setup["scripts"]}
        taking = [s for s in scripts if (defined.get(s) or {}).get("fields")]
        left = {"left_to_the_replays": [f"script.{s} takes inputs that Assist gives when it runs it"
                                        for s in taking]} if taking else {}
        if not (scripts := [s for s in scripts if s not in taking]):
            return {"goal": goal, "goal_devices_wanted": targets, "reaches_goal": None, **left}
        alone = [await self._run_scripts(recording, turn, setup, [s], targets) for s in scripts]
        out = {"goal": goal, "goal_devices_wanted": targets, "each_script_alone": alone, **left}
        if not targets:
            out.update(reaches_goal=None, note="This goal has nothing to check here.")
            return out
        reached_by = [r["scripts"][0] for r in alone if r.get("reaches_goal")]
        if reached_by:
            out.update(reaches_goal=True, reached_by=reached_by)
        elif len(scripts) > 1:
            together = await self._run_scripts(recording, turn, setup, scripts, targets)
            out.update(together=together, reaches_goal=together.get("reaches_goal", False),
                       reached_by=["all together"] if together.get("reaches_goal") else [])
        else:
            out.update(reaches_goal=False, reached_by=[])
        if taking and out["reaches_goal"] is False:  # One left to the replays may reach it.
            out["reaches_goal"] = None
        return out

    async def _match_aliases(self, setup, entity_id, aliases):
        matched = await job("match_aliases", recording=self.sessions.household.recording, today=self.today,
                            setup=setup, aliases=aliases)
        return {"alias_for": entity_id, "matches": matched,
                "ambiguous": [a for a, found in matched.items() if len(found) > 1]}

    async def resolution_signals(self, setup, sessions=None):
        """The recorded calls HA now resolves differently with the change applied, across the given sessions (all
        by default): signals for the roles to weigh, not regressions in themselves."""
        gate = asyncio.Semaphore(CONCURRENT)  # Recordings loaded at once; the workers' own limit is worker.job's.
        chosen = sorted(self.sessions.briefs if sessions is None else sessions)

        async def one(session):
            async with gate:
                view = await self.sessions.load(session)
                if view is None:
                    return session, None, None, None
                async with self._lock:
                    cached = session in self._baselines
                # A session HA couldn't replay is skipped, with why (a problem of the run's, unless it's one it
                # can't replay); the others go on.
                async with contained("propose", session, step="replay", expected=(CaseError,)) as attempt:
                    if not cached:
                        before = await replay_calls(view.recording, self.today)
                        async with self._lock:
                            self._baselines[session] = before
                    after = await replay_calls(view.recording, self.today, setup)
                    return session, self._baselines[session], after, None
                return session, None, None, attempt.error
        results = await asyncio.gather(*(one(s) for s in chosen))
        changed, skipped = [], []
        for session, before, after, why in results:
            if before is None or after is None:
                skipped.append(f"{session}: {why or NOT_REPLAYABLE}"[:200])
                continue
            for (turn, tool, args, was), (_, _, _, now) in zip(before, after):
                if was != now:
                    changed.append({"session": session, "turn": turn, "tool": tool, "args": args,
                                    **_difference(was, now)})
        return {"calls_resolving_differently": changed[:SIGNALS_SHOWN], "count": len(changed),
                **({"more": f"{len(changed) - SIGNALS_SHOWN} more not shown"} if len(changed) > SIGNALS_SHOWN else {}),
                "sessions_checked": len(results) - len(skipped), "skipped": skipped, "how_to_read": SIGNALS_NOTE}


async def run_scripts(recording, turn, today, setup, scripts, goals):
    """In the worker: each script run in a sandbox at the goal's starting state, and each goal (goal_targets) checked
    against what they did."""
    names = [f"script.{s}" for s in scripts]
    try:
        sandbox = await _start(recording, turn, today, setup)
    except Broken:  # The machinery's, not the change's.
        raise
    except Exception as err:  # noqa: BLE001 - HA couldn't load the change: that's the fact.
        return {"scripts": names, "loads": False, "why": str(err)[:300]}
    try:
        runs = []
        for object_id in scripts:
            before, called = len(sandbox.transitions), len(sandbox.calls)

            async def run(hass, object_id=object_id):
                await hass.services.async_call("script", object_id, blocking=True)
                await hass.async_block_till_done()
            try:
                await sandbox.run(run)
                said = happened.announcements([], [{"action": f"{d}.{s}", "data": data} for d, s, data
                                                   in sandbox.calls[called:]], sandbox.home)  # What its calls carried.
                runs.append({"script": f"script.{object_id}", "devices_changed": [
                    f"{t['entity_id']}: {t['old']}→{t['new']}" for t in sandbox.transitions[before:]],
                    **({"spoken": [f"{s['message'] or s.get('media') or ''!r} on "  # Its words, or its audio.
                                   f"{', '.join(sorted(s['targets'])) or 'no device'}" for s in said]} if said else {})})
            except Exception as err:  # noqa: BLE001
                runs.append({"script": f"script.{object_id}", "error": str(err)[:300]})
        final = await sandbox.run(lambda hass: _states(hass, [g["entity_id"] for g in goals if g["kind"] == "state"]))
        view = _ran(sandbox)
        checks = [_checked(view, goal, final) for goal in goals]
        return {"scripts": names, "loads": True, "runs": runs, "goal_devices": checks,
                "reaches_goal": bool(checks) and all(c["reached"] for c in checks),
                "unmodelled_commands": list(sandbox.unsimulated)}
    finally:
        await sandbox.stop()


def _ran(sandbox):
    """What the scripts did in a sandbox, as the analysis reads a turn (turn 1 of a view: analyzer.reached and done):
    the home as it was seeded, each change since, by Assist, and every call since as the turn's call log."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    start, changes = datetime.fromisoformat(sandbox.home.get("at") or "2000-01-01T00:00:00+00:00"), {}
    for n, change in enumerate(sandbox.changes, 1):
        changes.setdefault(change["entity_id"], []).append({
            **change, "at": (start + timedelta(milliseconds=n)).isoformat(), "during_turn": 1, "by": "assistant"})
    return SimpleNamespace(source=sandbox.home, sources=[sandbox.home], calls={1: []}, changes=changes,
                           actions={1: [{"action": f"{d}.{s}", "data": data} for d, s, data in sandbox.calls]})


def _checked(view, goal, final):
    """A goal (goal_targets') checked against what the scripts did (_ran), as the analysis judges one: it, with what
    its device shows now (a state goal's state and the settings it asks for, as its device shows them; a sequence's
    states gone through), and whether it's reached. final: the state goals' devices now (_states)."""
    found = {**goal, "turn": 1}
    if goal["kind"] == "action":
        return {**goal, "reached": done(view, found, [1])}
    if goal["kind"] == "sequence":
        return {**goal, "went_through": [c["new"] for c in view.changes.get(goal["entity_id"], [])
                                         if c["old"] != c["new"]], "reached": reached(view, found)}
    domain, now = goal["entity_id"].split(".", 1)[0], final.get(goal["entity_id"]) or {}
    shows = {a for name in goal.get("attributes") or {} for a in targets.representing(domain, name)}
    return {**goal, "now": now.get("state"), **({"settings_now": {a: v for a, v in (now.get("attributes") or {}).items()
                                                                  if a in shows}} if shows else {}),
            "reached": reached(view, found)}


async def match_aliases(recording, today, setup, aliases):
    """In the worker: what each alias matches through HA's own name matching, with the change applied."""
    from homeassistant.helpers import intent
    turn = recording_turns(recording)[0]
    sandbox = await _start(recording, turn, today, setup)
    try:
        async def match(hass):
            out = {}
            for alias in aliases:
                found = intent.async_match_targets(hass, intent.MatchTargetsConstraints(
                    name=alias, assistant="conversation", allow_duplicate_names=True))
                out[alias] = sorted(s.entity_id for s in found.states) if found.is_match else []
            return out
        return await sandbox.run(match)
    finally:
        await sandbox.stop()


async def render_instructions(recording, today, templates, user_name=None, quiet_only=False):
    """In the worker: each template rendered as an assistant's instructions are for the recording's first request, as
    HA's chat log renders them (ha_name, user_name, llm_context), in a sandbox seeded as its home: {"rendered"}, or
    {"error"}. HA renders them for requests without a signed-in person or an asking device too (a voice satellite's),
    and a template that fails there fails those requests: each is rendered that way as well. quiet_only: that way
    only ({"renders_quietly"}), when the recording is another assistant's."""
    from dataclasses import replace
    from homeassistant.helpers.template import Template
    turns = replayable_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return None
    sandbox = await _start(recording, turns[0], today)
    try:
        async def render(hass):
            recorded = context
            quiet = replace(recorded, context=Context(), device_id=None)

            def one(template, name, llm_context):
                return Template(template, hass).async_render(
                    {"ha_name": hass.config.location_name, "user_name": name, "llm_context": llm_context},
                    parse_result=False)
            out = []
            for template in templates:
                try:
                    found = {"renders_quietly": True} if quiet_only else {"rendered": one(template, user_name, recorded)}
                except Exception as err:  # noqa: BLE001 - a template HA can't render is the fact.
                    out.append({"error": _render_error(err)})
                    continue
                try:
                    one(template, None, quiet)
                except Exception as err:  # noqa: BLE001 - a template HA can't render is the fact.
                    found = {"error": f"for a request without a signed-in person or an asking device: "
                                      f"{_render_error(err)}"}
                out.append(found)
            return out
        return await sandbox.run(render)
    finally:
        await sandbox.stop()


def _render_error(err):
    return str(err)[:300] or type(err).__name__


async def _states(hass, entity_ids):
    """Each entity's state and attributes now, in plain form; none for one with no state."""
    await hass.async_block_till_done()
    return {e: {"state": found.state, "attributes": plain(dict(found.attributes))} for e in entity_ids
            if (found := hass.states.get(e)) is not None}


def _difference(was, now):
    """Before and after; for a text result (Assist's live context), only the lines that changed."""
    if isinstance(was.get("result"), str) and isinstance(now.get("result"), str):
        import difflib
        lines = [line for line in difflib.unified_diff(was["result"].splitlines(), now["result"].splitlines(),
                                                       lineterm="", n=0) if line[:1] in "+-" and line[:3] not in
                 ("+++", "---")]
        return {"result_lines_changed": lines[:12]}
    return {"before": was, "after": now}


def _sample(schema):
    """A sample value for a required field, from the tool's own schema."""
    kind = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if kind in ("integer", "number"):
        low, high = schema.get("minimum", 0), schema.get("maximum", 100)
        value = (low + high) / 2
        return int(value) if kind == "integer" else value
    if kind == "boolean":
        return True
    if kind == "array":
        return [_sample(schema.get("items") or {"type": "string"})]
    return "sample"


async def probe_capabilities(recording, entity_ids, today):
    """In the worker: what Assist can do with each device: each Assist tool tried on it by name, and what HA did."""
    import probatio
    turns = replayable_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return {}
    sandbox = await _start(recording, turns[0], today)
    try:
        async def probe(hass):
            from homeassistant.components.homeassistant import async_should_expose
            api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)
            out, commands = {}, hass.data[SANDBOX_KEY].commands
            for entity_id in entity_ids:
                state = hass.states.get(entity_id)
                if state is None:
                    continue
                exposed = async_should_expose(hass, "conversation", entity_id)
                can = []
                for tool in api.tools if exposed else []:
                    schema = probatio.to_openapi(tool.parameters, custom_serializer=api.custom_serializer)
                    props = schema.get("properties") or {}
                    if "name" not in props:
                        continue
                    args = {"name": state.attributes.get("friendly_name") or entity_id}
                    if "domain" in props:
                        args["domain"] = [state.domain]
                    for field in schema.get("required") or []:
                        args.setdefault(field, _sample(props.get(field) or {}))
                    before = len(commands)
                    result = await _call(hass, api, tool.name, args)
                    reached = any(c["entity_id"] == entity_id for c in commands[before:])  # A command got there.
                    if reached or (result.get("response") == "query_answer" and entity_id in result.get("acted_on", [])):
                        can.append({"tool": tool.name, "what": (tool.description or "").split(".")[0]})
                if exposed:
                    can.append({"tool": "GetLiveContext", "what": "Read its current state"})
                out[entity_id] = {"exposed": exposed, "assist_can": can}
            return out
        return await sandbox.run(probe)
    finally:
        await sandbox.stop()


async def automation_week(recording, today, change, seconds=WEEK_SECONDS):
    """An automation change's week (DETAILS P6.13): change: setup_from_changes's, its automations ({id: its whole
    config}: new, or in place of the home's) and automation_on ({automation entity: on or off}) on top of today's
    setup. {"with": the week with the change, "without": the week without it, for a change to an automation the home
    has (None for a new one)}, each run_week's, in a fresh worker of its own, side by side, within seconds."""
    ids = {str(a.get("id")) for a in (today["home"].get("automations") or [])}
    edits = change.get("automation_on") or any(i in ids for i in change.get("automations") or {})
    week = lambda **more: job("week", recording=recording, today=today, seconds=seconds, **more)  # noqa: E731
    weeks = await asyncio.gather(week(change=change), *([week()] if edits else []))
    return {"with": weeks[0], "without": weeks[1] if edits else None}


async def improvement_week(hass, recorder, item):
    """An improvement's automations as its review shows them (weeks.reviewed), with their week: each it changes, by
    id: its configuration (as proposed, or for one turned on or off as the home has it), whether it's new, and its
    week (weeks.week) of the improvement's whole setup on top of today's (what it saves with the automation, a scene it
    turns on, say), on the conversation it came from. One whose week couldn't run says why (failed)."""
    recording = await recorder.export(item["conversation"])
    agent, _ = await hass.async_add_executor_job(agent_of, recording)
    today = await setup_today(hass, agent)
    home = {str(a.get("id")): a for a in today["home"].get("automations") or [] if a.get("id") is not None}
    automations = [{"id": c["id"], "on": c["on"], "config": deepcopy(home.get(str(c["id"])) or {})} if "on" in c else
                   {"id": c["id"], "config": deepcopy(c["config"]), "new": str(c["id"]) not in home}
                   for c in item["changes"] if c.get("kind") == "automation"]
    try:
        found = await automation_week(recording, today, item["setup"])
    except Exception as err:  # noqa: BLE001 - the week informs the review; it never holds the improvement.
        return {"failed": str(err) or type(err).__name__, "automations": automations}
    return {"automations": [{**a, "week": week(found, a)} for a in automations]}


async def run_week(recording, today, change=None, seconds=WEEK_SECONDS):
    """In the worker: the home as the conversation ended (simulation.ended), today's setup, the change on top, run
    through WEEK_DAYS simulated days within seconds (real), waits passing at once (clock.Window): {"days": whole days
    run, "runs": each automation run, {automation, id, at, changed: [{entity_id, old, new, attributes changed}]}}.
    Devices don't act on their own: only what Home Assistant's own time (time, sun, timers) and the automations
    themselves set off happens. The change's automations are saved as applying saves them, last and as the week
    starts (_save), so what their save sets off runs too."""
    from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
    from homeassistant.const import EVENT_STATE_CHANGED
    from homeassistant.core import callback
    change = change or {}
    saving = {k: change[k] for k in ("automations", "automation_on") if change.get(k)}
    source = changed(at_moment(today["home"], ended(recording)), {k: v for k, v in change.items() if k not in saving})
    turn = recording_turns(recording)[-1]
    sandbox = await Sandbox.start(source.get("time_zone") or "UTC", source, setup_of(today))
    try:
        await sandbox.run(lambda hass: seed(hass, person=today.get("person"), **seed_options(today, turn),
                                            scripts=deepcopy(source.get("scripts") or {}),
                                            automations=source.get("automations") or [],
                                            recorded=recorded(recording, today["tools"])))

        async def week(hass):
            runs, by_context = [], {}

            @callback
            def triggered(event):
                state = hass.states.get(event.data.get("entity_id"))
                runs.append({"automation": event.data.get("entity_id"),
                             "id": state.attributes.get("id") if state else None,
                             "at": event.time_fired.isoformat(), "changed": []})
                by_context[event.context.id] = runs[-1]

            @callback
            def changed(event):
                old, new = event.data.get("old_state"), event.data.get("new_state")
                if new is None or (run := by_context.get(new.context.id) or by_context.get(
                        new.context.parent_id)) is None or new.entity_id == run["automation"]:
                    return
                attributes = {k: v for k, v in new.attributes.items() if old is None or old.attributes.get(k) != v}
                run["changed"].append({"entity_id": new.entity_id, "old": old.state if old else None,
                                       "new": new.state, **({"attributes": plain(attributes)} if attributes else {})})
            listening = [hass.bus.async_listen(EVENT_AUTOMATION_TRIGGERED, triggered),
                         hass.bus.async_listen(EVENT_STATE_CHANGED, changed)]
            start = hass.loop.clock.now()
            try:
                await _save(hass, source.get("automations") or [], saving)
                await hass.loop.settle(start + WEEK_DAYS * 86400, clock.REAL_MONOTONIC() + seconds)
            finally:
                for unlisten in listening:
                    unlisten()
            return {"days": min(WEEK_DAYS, int((hass.loop.clock.now() - start) // 86400)), "runs": runs}
        return await sandbox.run(week)
    finally:
        await sandbox.stop()


async def _save(hass, automations, saving):
    """A change's automations (saving: setup_from_changes's automations and automation_on) saved in the sandbox as
    applying saves them: each through what Home Assistant's automation editor does once it has written its file (its
    automations, the home's with the change's in place or added), its reload of that automation; then each turned on or
    off through its service."""
    from homeassistant.setup import async_setup_component
    from homeassistant.util.yaml import dump
    if new := saving.get("automations"):
        kept = [a for a in automations if a.get("id") is None or str(a["id"]) not in new]
        files = {"configuration.yaml": "automation: !include automations.yaml\n",
                 "automations.yaml": dump(deepcopy(kept + list(new.values())))}

        def write():
            for name, text in files.items():
                with open(hass.config.path(name), "w", encoding="utf-8") as file:
                    file.write(text)
        await hass.async_add_executor_job(write)
        if "automation" not in hass.config.components and not await async_setup_component(hass, "automation", {}):
            raise RuntimeError("The sandbox could not load the automations")
        for automation_id in new:
            await hass.services.async_call("automation", "reload", {"id": automation_id}, blocking=True)
    for entity_id, on in (saving.get("automation_on") or {}).items():
        await hass.services.async_call("automation", "turn_on" if on else "turn_off", {"entity_id": entity_id},
                                       blocking=True)
