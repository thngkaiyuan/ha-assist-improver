"""Facts from HA's own engine about a hypothesis, so the AI roles judge evidence instead of guessing it.

All of it runs in sandboxes seeded from recorded sessions; no model is called and no device is touched.

- implementation: HA loads the hypothesis's scripts, each runs from the state its goals started in, and the goal's
  devices are compared with the goal; a new alias is matched by HA's own name matching.
- resolution signals: every session's recorded HA tool calls are replayed without and with the change, and the calls
  HA now resolves differently are listed. They are signals to weigh, not regressions: a change that folds several
  flows into one more general script makes the old calls resolve differently while the new script covers them.
- capabilities: each of HA's Assist tools is tried on each device, and what HA carries out is what Assist can do.
"""

import asyncio
from copy import deepcopy

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .analyzer import top_level
from .cases import CaseError, recording_turns
from .changes import setup_from_changes
from .sandbox import SANDBOX_KEY, Sandbox, observe, sandbox_context, seed, seed_options
from .simulation import recorded_transitions

CONCURRENT = 4  # Sandboxes started at once.
SIGNALS_SHOWN = 15  # Resolution signals listed in full; the count covers the rest.
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
    found = await _replay(recording, definitions, setup)
    return found and found[0]


async def recorded_call_effects(recording, definitions):
    """Each turn's effects on the home when today's engine runs that turn's recorded tool calls: {turn: changes}.
    For a practice recording, this is what a baseline is compared with: its notes come from the practice engine of
    the day it was recorded, which may have emulated less (a sleeping car's climate, say)."""
    found = await _replay(recording, definitions)
    return found and found[1]


async def _replay(recording, definitions, setup=None):
    turns = recording_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return None
    sandbox = await _start(recording, turns[0], definitions, setup)
    assistant = {t["entity_id"] for turn in turns for t in recorded_transitions(recording, turn)}
    out, effects = [], {}
    try:
        for index, turn in enumerate(turns):
            if index:
                await sandbox.run(lambda hass: observe(hass, turn["source"], assistant))
            context = _context(turn) or context
            calls = [c for c in turn["calls"] if not c.get("external")]
            before = len(sandbox.transitions)
            effects[turn["number"]] = []
            if not calls:
                continue

            async def run(hass, calls=calls, context=context, turn=turn):
                api = await llm.async_get_api(hass, turn["prepared"].get("api_id") or "assist",
                                              sandbox_context(hass, context))
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
        view = self.sessions.views[session] if session else self.sessions.household
        turn = recording_turns(view.recording)[0]
        options = turn["prepared"]["provider_config"][0]["options"]
        return setup_from_changes(hypothesis.get("changes") or [], source=turn["source"],
                                  prompt=options.get("prompt"), scripts=self._scripts(turn["source"]))

    def _scripts(self, source):
        return deepcopy(source["scripts"]) if "scripts" in source else deepcopy(self.definitions["scripts"])

    async def check(self, hypothesis):
        """Both kinds of facts for a draft; problems with the change itself (HA can't load it) come first."""
        try:
            setup = self.setup(hypothesis)
        except CaseError as err:
            return {"problems": [str(err)]}
        implementation = await self.implementation(hypothesis, setup)
        signals = await self.resolution_signals(setup) if (setup["exposure"] or setup["aliases"] or setup["scripts"]) \
            else {"note": "Only instruction lines change: HA resolves devices as before; their effect needs a replay "
                          "with the model."}
        return {"implementation": implementation, "resolution_signals": signals}

    async def implementation(self, hypothesis, setup):
        """Each new or edited script, run from the state each named goal started in; new aliases, matched by HA."""
        facts = []
        for ref in hypothesis.get("goals") or []:
            session, turn_no = ref["session"][:8], ref["goal_turn"]
            analysis, view = self.sessions.analyses.get(session), self.sessions.views.get(session)
            if analysis is None:
                continue
            turns = recording_turns(view.recording)
            if not 1 <= turn_no <= len(turns):
                continue
            targets = goal_targets(analysis, turn_no)
            # New and edited scripts, and existing ones the change exposes: what Assist would now run.
            exposed = [e.split(".", 1)[1] for e, on in setup["exposure"].items() if on and e.startswith("script.")]
            scripts = sorted(set(setup["scripts"]) | set(exposed))
            if scripts:
                facts.append(await self._try_scripts(view.recording, turns[turn_no - 1], setup, scripts, targets,
                                                     f"{session}:{turn_no}"))
        for entity_id, aliases in setup["aliases"].items():
            facts.append(await self._match_aliases(setup, entity_id, aliases))
        return facts

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

    async def _run_scripts(self, recording, turn, setup, scripts, targets):
        names = [f"script.{s}" for s in scripts]
        try:
            sandbox = await _start(recording, turn, self.definitions, setup)
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

    async def _match_aliases(self, setup, entity_id, aliases):
        from homeassistant.helpers import intent
        view = self.sessions.household
        turn = recording_turns(view.recording)[0]
        sandbox = await _start(view.recording, turn, self.definitions, setup)
        try:
            async def match(hass):
                out = {}
                for alias in aliases:
                    found = intent.async_match_targets(hass, intent.MatchTargetsConstraints(
                        name=alias, assistant="conversation", allow_duplicate_names=True))
                    out[alias] = sorted(s.entity_id for s in found.states) if found.is_match else []
                return out
            matched = await sandbox.run(match)
        finally:
            await sandbox.stop()
        return {"alias_for": entity_id, "matches": matched,
                "ambiguous": [a for a, found in matched.items() if len(found) > 1]}

    async def resolution_signals(self, setup):
        """The recorded calls HA now resolves differently, across all sessions, with the change applied: signals for
        the roles to weigh, not regressions in themselves."""
        gate = asyncio.Semaphore(CONCURRENT)

        async def one(session, view):
            async with gate:
                async with self._lock:
                    cached = session in self._baselines
                if not cached:
                    before = await replay_calls(view.recording, self.definitions)
                    async with self._lock:
                        self._baselines[session] = before
                after = await replay_calls(view.recording, self.definitions, setup)
                return session, self._baselines[session], after
        results = await asyncio.gather(*(one(s, v) for s, v in sorted(self.sessions.views.items())),
                                       return_exceptions=True)
        changed, skipped = [], []
        for item in results:
            if isinstance(item, BaseException):
                skipped.append(str(item)[:200])
                continue
            session, before, after = item
            if before is None or after is None:
                skipped.append(f"{session}: can't be replayed")
                continue
            for (turn, tool, args, was), (_, _, _, now) in zip(before, after):
                if was != now:
                    changed.append({"session": session, "turn": turn, "tool": tool, "args": args,
                                    **_difference(was, now)})
        return {"calls_resolving_differently": changed[:SIGNALS_SHOWN], "count": len(changed),
                **({"more": f"{len(changed) - SIGNALS_SHOWN} more not shown"} if len(changed) > SIGNALS_SHOWN else {}),
                "sessions_checked": len(results) - len(skipped), "skipped": skipped, "how_to_read": SIGNALS_NOTE}


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


async def capabilities(recording, entity_ids, definitions):
    """What Assist can do with each device: each Assist tool tried on it by name in a sandbox, and what HA did."""
    import probatio
    turns = recording_turns(recording)
    if not turns or (context := _context(turns[0])) is None:
        return {}
    sandbox = await _start(recording, turns[0], definitions)
    try:
        async def probe(hass):
            from homeassistant.components.homeassistant.exposed_entities import async_should_expose
            api = await llm.async_get_api(hass, turns[0]["prepared"].get("api_id") or "assist",
                                          sandbox_context(hass, context))
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
