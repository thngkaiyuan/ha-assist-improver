"""Facts from HA's own engine about a hypothesis, so the AI roles judge evidence instead of guessing it.

All of it runs in sandboxes seeded from recorded sessions; no model is called and no device is touched.

- implementation: HA loads the hypothesis's scripts, each runs from the state its goals started in, and the goal's
  devices are compared with the goal; a new alias is matched by HA's own name matching.
- resolution signals: the recorded HA tool calls of every session the change could affect (sessions.applies: its calls
  or requests touch the change's devices, their names or areas, or what Assist can see) are replayed without and with
  the change, and the calls HA now resolves differently are listed. A session none of whose calls touch any of that
  resolves exactly as before, so it isn't replayed; how many weren't, and why, is said. They are signals to weigh, not regressions: a change that folds several
  flows into one more general script makes the old calls resolve differently while the new script covers them.
- capabilities: each of HA's Assist tools is tried on each device, and what HA carries out is what Assist can do.
- instructions: an assistant's instructions as a prompt change edits them, rendered by HA for that assistant's newest
  conversation as its chat log renders them (ha_name, user_name, llm_context): what Assist would read, or HA's error;
  and for a request without a signed-in person or an asking device, which HA renders them for too.

The sandboxes run in the replay worker (worker.py): the jobs below that start one (JOBS) run there whole, and what
the roles' tools call here asks the worker for them.
"""

import asyncio
from copy import deepcopy

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .analyzer import top_level
from .cases import CaseError, recording_turns, replayable_turns
from .changes import setup_from_changes
from .failures import contained
from .sandbox import SANDBOX_KEY, Sandbox, observe, sandbox_context, seed, seed_options
from .simulation import moved_by_others
from .worker import worker

CONCURRENT = 4  # Sandboxes started at once.
SIGNALS_SHOWN = 15  # Resolution signals listed in full; the count covers the rest.
JOBS = {"replay": "_replay", "run_scripts": "run_scripts", "match_aliases": "match_aliases",
        "capabilities": "probe_capabilities",
        "render_instructions": "render_instructions"}  # What the worker runs here, by name: each starts its own sandbox.
UNCHECKED = "Home Assistant couldn't check this: "  # A fact the sandbox couldn't give (failures.contained).
NOT_REPLAYABLE = "can't be replayed"
RENDERED_SHOWN = 2000  # Characters of rendered instructions shown with a draft.
SIGNALS_NOTE = ("Each is a recorded tool call that Home Assistant resolves differently with the change: a signal to "
                "weigh, not proof of a regression. It may be a problem (a name now matches another device, a device "
                "is no longer reachable), or expected (a more general script or rule now covers what the old call "
                "did). Judge each against what the user wanted in that session.")


def goal_targets(analysis, turn):
    """The device states a goal wants: its state and sequence goals, and what the user chose for settings they
    left open."""
    targets = []
    for g in top_level(analysis.get("goals", [])):
        if g["turn"] != turn:
            continue
        if g["kind"] == "state":
            targets.append({"entity_id": g["entity_id"], "state": g["state"]})
        elif g["kind"] == "sequence":
            targets.append({"entity_id": g["entity_id"], "states": g["states"]})
        elif g["kind"] == "ask":
            targets += [{"entity_id": c["entity_id"], "state": c["state"]} for c in g.get("user_chose", [])]
    return targets


def _context(turn):
    ctx = turn["prepared"]["context"]
    identity = ctx.get("context", {})
    if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
        return None
    return llm.LLMContext(**{**ctx, "context": Context(**identity)})


def _tools_api(turn):
    """The API whose tools a recorded turn used: the household API (up to 0.37) has Assist's own tools, so Assist's
    stand in for it (the evidence sandbox carries no household knowledge, and needs none to try a tool); a merged id
    (a|b) is its APIs."""
    from .household import as_assist
    ids = as_assist(turn["prepared"].get("api_id") or "assist")
    return ids[0] if len(ids) == 1 else ids


async def _start(recording, turn, definitions, setup=None):
    """A sandbox seeded as the home at this turn, with a hypothesis's setup (exposure, aliases, scripts) on top."""
    source = turn["source"]
    scripts = deepcopy(source["scripts"]) if "scripts" in source else deepcopy(definitions["scripts"])
    automations = source.get("automations") if "automations" in source else definitions["automations"]
    setup = setup or {}
    scripts.update(setup.get("scripts") or {})
    sandbox = await Sandbox.start(source.get("time_zone") or "UTC")
    try:
        await sandbox.run(lambda hass: seed(hass, source, **seed_options(turn), scripts=scripts,
                                            automations=automations, exposure_changes=setup.get("exposure"),
                                            alias_changes=setup.get("aliases"),
                                            alias_removals=setup.get("alias_removals")))
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


async def replay_calls(recording, definitions, setup=None):
    """Each recorded HA tool call of a session, replayed in order without the model: [(turn, tool, args, result)]."""
    found = await worker().call("evidence", job="replay", recording=recording, definitions=definitions, setup=setup)
    return found and found[0]


async def recorded_call_effects(recording, definitions):
    """Each turn's effects on the home when today's engine runs that turn's recorded tool calls: {turn: changes}.
    For a practice recording, this is what a replay without a change is compared with: its notes come from the
    practice engine of the day it was recorded, which may have emulated less (a sleeping car's climate, say)."""
    found = await worker().call("evidence", job="replay", recording=recording, definitions=definitions)
    return found and {int(turn): changes for turn, changes in found[1].items()}  # JSON keys are text.


async def capabilities(recording, entity_ids, definitions):
    """What Assist can do with each device: each Assist tool tried on it by name in a sandbox, and what HA did."""
    return await worker().call("evidence", job="capabilities", recording=recording, entity_ids=sorted(entity_ids),
                               definitions=definitions)


async def _replay(recording, definitions, setup=None):
    """In the worker: replay_calls and recorded_call_effects, from one replay."""
    turns = replayable_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return None
    sandbox = await _start(recording, turns[0], definitions, setup)
    out, effects = [], {}
    try:
        for index, turn in enumerate(turns):
            if index:
                moves = [moved_by_others(recording, turns, turn)]
                await sandbox.run(lambda hass: observe(hass, turn["source"], moves))
            context = _context(turn) or context
            calls = [c for c in turn["calls"] if not c.get("external")]
            before = len(sandbox.transitions)
            effects[turn["number"]] = []
            if not calls:
                continue

            async def run(hass, calls=calls, context=context, turn=turn):
                api = await llm.async_get_api(hass, _tools_api(turn), sandbox_context(hass, context))
                return [(turn["number"], c["tool_name"], c["tool_args"], await _call(hass, api, c["tool_name"],
                                                                                  c["tool_args"])) for c in calls]
            out += await sandbox.run(run)
            await sandbox.run(lambda hass: hass.async_block_till_done())
            effects[turn["number"]] = list(sandbox.transitions[before:])
    finally:
        await sandbox.stop()
    return out, effects


class Evidence:
    """Facts for one generator run's sessions; the replays without a change are reused across hypotheses."""

    def __init__(self, sessions, definitions):
        self.sessions, self.definitions = sessions, definitions
        self._baselines, self._lock = {}, asyncio.Lock()

    def setup(self, hypothesis, session=None):
        from .instructions import assistant_config, target
        view = self.sessions.views[session] if session else self.sessions.household
        turn = recording_turns(view.recording)[0]
        configs = turn["prepared"]["provider_config"]
        return setup_from_changes(self.sessions.with_targets(hypothesis.get("changes")), source=turn["source"],
                                  prompt=configs[0]["options"].get("prompt"), scripts=self._scripts(turn["source"]),
                                  user_id=(turn["prepared"].get("context") or {}).get("context", {}).get("user_id"),
                                  assistant=target(assistant_config(configs)))

    async def render(self, view, templates, user_name=None, quiet_only=False):
        """Each template rendered as an assistant's instructions for this session's first request, and for one
        without a signed-in person or an asking device, in a sandbox of its home: [{"rendered"} or {"error"}], or
        None when it can't seed one (render_instructions; quiet_only: the latter only)."""
        return await worker().call("evidence", job="render_instructions", recording=view.recording,
                                   definitions=self.definitions, templates=list(templates), user_name=user_name,
                                   quiet_only=quiet_only)

    async def instructions(self, hypothesis, hass=None):
        """Each assistant's instructions as the hypothesis's prompt changes edit them: rendered by HA (what Assist
        would read), or why not."""
        from .instructions import is_edit
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

    def _scripts(self, source):
        return deepcopy(source["scripts"]) if "scripts" in source else deepcopy(self.definitions["scripts"])

    async def check(self, hypothesis, hass=None):
        """The facts for a draft; problems with the change itself (HA can't load it) come first."""
        try:
            setup = self.setup(hypothesis)
        except CaseError as err:
            return {"problems": [str(err)]}
        implementation = await self.implementation(hypothesis, setup)
        edited = await self.instructions(hypothesis, hass)
        if setup["exposure"] or setup["aliases"] or setup["scripts"]:
            from .sessions import applies, footprint
            # An edit of an assistant's instructions doesn't change how HA resolves a recorded call.
            fp = {**footprint(hypothesis.get("changes") or [], self.sessions.household), "assistants": set()}
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
        talk = speech(hypothesis, self.definitions.get("scripts"))  # What it would have Assist say, for the reviewer.
        return {"implementation": implementation, "resolution_signals": signals, **({"speech": talk} if talk else {}),
                **({"instructions": edited} if edited else {})}

    async def implementation(self, hypothesis, setup):
        """Each new or edited script, run from the state each named goal started in; new aliases, matched by HA."""
        facts = []
        for ref in hypothesis.get("goals") or []:
            session, turn_no = ref["session"][:8], ref["goal_turn"]
            analysis, view = self.sessions.analyses.get(session), await self.sessions.load(session)
            if analysis is None or view is None:
                continue
            turns = replayable_turns(view.recording)
            if not 1 <= turn_no <= len(turns):  # Not there, or a conversation that can't be replayed.
                continue
            targets = goal_targets(analysis, turn_no)
            # New and edited scripts, and existing ones the change exposes: what Assist would now run.
            exposed = [e.split(".", 1)[1] for e, on in setup["exposure"].items() if on and e.startswith("script.")]
            scripts = sorted(set(setup["scripts"]) | set(exposed))
            if not scripts:
                continue
            # A goal HA couldn't check is said so (a problem of the run's, unless it's a case it can't replay); the
            # other facts still come.
            async with contained("propose", f"{session}:{turn_no}", step="script check", expected=(CaseError,)) as one:
                facts.append(await self._try_scripts(view.recording, turns[turn_no - 1], setup, scripts, targets,
                                                     f"{session}:{turn_no}"))
            if one.failed:
                facts.append({"goal": f"{session}:{turn_no}", "reaches_goal": None, "note": UNCHECKED + one.error})
        for entity_id, aliases in setup["aliases"].items():
            async with contained("propose", entity_id, step="alias match", expected=(CaseError,)) as one:
                facts.append(await self._match_aliases(setup, entity_id, aliases))
            if one.failed:
                facts.append({"alias_for": entity_id, "matches": None, "note": UNCHECKED + one.error})
        return facts

    async def _run_scripts(self, recording, turn, setup, scripts, targets):
        return await worker().call("evidence", job="run_scripts", recording=recording, turn=turn,
                                   definitions=self.definitions, setup=setup, scripts=scripts, targets=targets)

    async def _try_scripts(self, recording, turn, setup, scripts, targets, goal):
        """Each script alone from the goal's starting state; if none reaches the goal alone, all of them together
        (one goal may need several, such as a script per door). Order never decides the result."""
        alone = [await self._run_scripts(recording, turn, setup, [s], targets) for s in scripts]
        out = {"goal": goal, "goal_devices_wanted": targets, "each_script_alone": alone}
        if not targets:
            out.update(reaches_goal=None, note="This goal has no device states to check.")
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
        return out

    async def _match_aliases(self, setup, entity_id, aliases):
        matched = await worker().call("evidence", job="match_aliases", recording=self.sessions.household.recording,
                                      definitions=self.definitions, setup=setup, aliases=aliases)
        return {"alias_for": entity_id, "matches": matched,
                "ambiguous": [a for a, found in matched.items() if len(found) > 1]}

    async def resolution_signals(self, setup, sessions=None):
        """The recorded calls HA now resolves differently with the change applied, across the given sessions (all
        by default): signals for the roles to weigh, not regressions in themselves."""
        gate = asyncio.Semaphore(CONCURRENT)
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
                        before = await replay_calls(view.recording, self.definitions)
                        async with self._lock:
                            self._baselines[session] = before
                    after = await replay_calls(view.recording, self.definitions, setup)
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


async def run_scripts(recording, turn, definitions, setup, scripts, targets):
    """In the worker: each script run in a sandbox at the goal's starting state, and the goal's devices after."""
    names = [f"script.{s}" for s in scripts]
    try:
        sandbox = await _start(recording, turn, definitions, setup)
    except Exception as err:  # noqa: BLE001 - HA couldn't load the change: that's the fact.
        return {"scripts": names, "loads": False, "why": str(err)[:300]}
    try:
        runs = []
        for object_id in scripts:
            before = len(sandbox.transitions)

            async def run(hass, object_id=object_id):
                await hass.services.async_call("script", object_id, blocking=True)
                await hass.async_block_till_done()
            try:
                await sandbox.run(run)
                runs.append({"script": f"script.{object_id}", "devices_changed": [
                    f"{t['entity_id']}: {t['old']}→{t['new']}" for t in sandbox.transitions[before:]]})
            except Exception as err:  # noqa: BLE001
                runs.append({"script": f"script.{object_id}", "error": str(err)[:300]})
        final = await sandbox.run(lambda hass: _states(hass, [t["entity_id"] for t in targets]))
        checks = []
        for target in targets:
            entity = target["entity_id"]
            if "state" in target:
                checks.append({"entity_id": entity, "wanted": target["state"], "now": final.get(entity),
                               "reached": final.get(entity) == target["state"]})
            else:
                seen = [t["new"] for t in sandbox.transitions if t["entity_id"] == entity]
                checks.append({"entity_id": entity, "wanted": target["states"], "went_through": seen,
                               "reached": seen == target["states"]})
        return {"scripts": names, "loads": True, "runs": runs, "goal_devices": checks,
                "reaches_goal": bool(checks) and all(c["reached"] for c in checks),
                "unmodelled_commands": list(sandbox.unsimulated)}
    finally:
        await sandbox.stop()


async def match_aliases(recording, definitions, setup, aliases):
    """In the worker: what each alias matches through HA's own name matching, with the change applied."""
    from homeassistant.helpers import intent
    turn = recording_turns(recording)[0]
    sandbox = await _start(recording, turn, definitions, setup)
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


async def render_instructions(recording, definitions, templates, user_name=None, quiet_only=False):
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
    sandbox = await _start(recording, turns[0], definitions)
    try:
        async def render(hass):
            recorded = sandbox_context(hass, context)
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
    await hass.async_block_till_done()
    return {e: getattr(hass.states.get(e), "state", None) for e in entity_ids}


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


async def probe_capabilities(recording, entity_ids, definitions):
    """In the worker: what Assist can do with each device: each Assist tool tried on it by name, and what HA did."""
    import probatio
    turns = replayable_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return {}
    sandbox = await _start(recording, turns[0], definitions)
    try:
        async def probe(hass):
            from homeassistant.components.homeassistant.exposed_entities import async_should_expose
            api = await llm.async_get_api(hass, _tools_api(turns[0]), sandbox_context(hass, context))
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
