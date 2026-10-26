"""Per-run recorded HA context, with HA's own tools acting in a sandbox seeded as the recorded home.

The model sees the recorded prompt. Its tools are the ones HA builds in the
sandbox (sandbox.py) from the recorded setup, and each call runs there: HA's own
intents, scripts and automations act on the sandbox's copy of the home, and
only device commands are emulated. The sandbox runs in the replay worker's
process (worker.py); the chat log and the household's assistant stay here. No
provider client or model-run service is created here; offline tests pass this
log to HA's existing conversation adapter with a scripted transport.
"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import probatio
from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .cases import CaseError, recording_turns, replayed_turns
from .sandbox import seed_options
from .snapshot import live_definitions
from .serialization import plain
from .simulation import UnsupportedSimulation, recorded_transitions
from .trace import span, tag
from .worker import RemoteSandbox, WorkerError, context_data


class Precomputed:
    """A tool's parameters as the JSON schema HA's serializer gave in the sandbox; providers read it back unchanged
    through the replay's serializer."""

    def __init__(self, schema):
        self.schema = schema


def serializer(value):
    return value.schema if isinstance(value, Precomputed) else llm.selector_serializer(value)


class SandboxTool(llm.Tool):
    """A tool HA built in the sandbox; a call runs there, in the sandbox's own loop."""

    def __init__(self, tool, home):
        self.name, self.description = tool["name"], tool["description"]
        self.parameters = probatio.Schema(Precomputed(tool["parameters"]))
        self.integration = tool.get("integration")
        self.home = home

    @span("replay.tool")
    async def async_call(self, hass, tool_input, llm_context):
        home = self.home
        before = len(home.sandbox.unsimulated)
        tag(tool=tool_input.tool_name)
        try:
            found = await home.sandbox.call("tool", tool_name=tool_input.tool_name, tool_args=tool_input.tool_args,
                                            call_id=tool_input.id, external=tool_input.external)
            result = llm.ToolResult(data=found["data"], error=found["error"])
        except HomeAssistantError:
            raise  # HA's own answer to the model, as in real use (no match, say).
        except CaseError as err:
            home.unsupported.append(str(err))
            raise UnsupportedSimulation(str(err)) from err
        finally:
            await home.refresh()
        if len(home.sandbox.unsimulated) > before:
            message = "No emulation for " + ", ".join(home.sandbox.unsimulated[before:])
            home.unsupported.append(message)
            # HA would turn a tool failure into an answer and ask the model again; an unsupported simulation must
            # end this run instead.
            raise UnsupportedSimulation(message)
        home.dispatched.append({"name": tool_input.tool_name, "args": deepcopy(tool_input.tool_args)})
        return result


class RecordedAPIInstance(llm.APIInstance):
    async def async_call_tool(self, tool_input):
        if not any(tool.name == tool_input.tool_name for tool in self.tools):
            message = "Tool is absent from the sandbox's tools"
            self.api.unsupported.append(message)
            raise UnsupportedSimulation(message)
        return await super().async_call_tool(tool_input)


class SandboxHome(llm.API):
    """One replay's sandbox, started on first use from the recording's first turn, and what it did.

    world holds the sandbox's states, the replay's state changes (transitions) and device commands (calls),
    refreshed after every tool call.
    """

    def __init__(self, hass, recording, turn, run_id, *, scripts=None, automations=None, exposure_changes=None,
                 alias_changes=None, alias_removals=None, prompt_template=None, household=None):
        super().__init__(hass=hass, id=run_id, name="Recorded home")
        self.recording, self.first_turn = recording, turn
        self.scripts, self.automations, self.exposure_changes = scripts, automations, exposure_changes or {}
        self.alias_changes, self.prompt_template = alias_changes or {}, prompt_template
        self.alias_removals = alias_removals or {}
        self.household = household  # A change's household knowledge, which Assist reads.
        self.sandbox = None
        self.tools = None  # Assist's tools in the sandbox, once it built them.
        self.unsupported, self.dispatched = [], []
        self.world = SimpleNamespace(states={}, transitions=[], calls=[])
        self.prompt = None  # The system prompt HA builds in the sandbox, for comparison with the recorded one.

    async def start(self):
        if self.sandbox is not None:
            return
        source, prepared = self.first_turn["source"], self.first_turn["prepared"]
        # As recorded; a recording made before definitions were kept replays with today's (nothing else to use).
        recorded = "scripts" in source
        scripts = self.scripts if self.scripts is not None else (source["scripts"] if recorded else None)
        automations = self.automations if self.automations is not None else source.get("automations")
        if scripts is None or automations is None:
            live_scripts, live_automations = live_definitions(self.hass)
            scripts = live_scripts if scripts is None else scripts
            automations = live_automations if automations is None else automations
        self.definitions_recorded = recorded
        user_name = None
        if (user_id := prepared["context"].get("context", {}).get("user_id")) and getattr(self.hass, "auth", None):
            user = await self.hass.auth.async_get_user(user_id)
            user_name = getattr(user, "name", None)
        self.sandbox = await RemoteSandbox.start(source.get("time_zone") or self.hass.config.time_zone)
        try:
            async with span("sandbox.seed"):
                await self.sandbox.call(
                    "seed", source=source, user_name=user_name, options=seed_options(self.first_turn), scripts=scripts,
                    automations=automations, exposure_changes=self.exposure_changes,
                    alias_changes=self.alias_changes, alias_removals=self.alias_removals)
        except Exception as err:
            await self.stop()
            raise UnsupportedSimulation(f"The sandbox could not be seeded as the recorded home: {err}") from err
        await self.refresh()

    async def api_instance(self, turn, llm_context):
        await self.start()
        options = turn["prepared"]["provider_config"][0]["options"]
        template = self.prompt_template if self.prompt_template is not None else options.get("prompt")
        from .household import API_ID, api_ids, render
        apis = api_ids(turn["prepared"].get("api_id") or "assist")
        # Recorded through the household API (up to 0.37): replayed through it, so the prompt is the one recorded.
        legacy = API_ID in apis
        if (legacy or "assist" in apis) and (self.household is not None or reads_household(turn)):
            # The household knowledge Assist read then, or as a change would make it.
            knowledge = self.household if self.household is not None else turn["source"].get("household")
            await self.sandbox.call("household", knowledge=knowledge, legacy=legacy)
            lines = render(knowledge, getattr(llm_context.context, "user_id", None))
        else:
            lines = ""
        built = await self.sandbox.call("prompt", context=context_data(llm_context),
                                        api_id=apis if len(apis) > 1 else apis[0], template=template)
        self.prompt = built["prompt"]
        if built["api_prompt"] is None:
            raise UnsupportedSimulation("Home Assistant's Assist tools are unavailable in the sandbox")
        if lines not in built["api_prompt"]:  # Never a test without the lines it's meant to read: the sandbox's fault.
            raise WorkerError("Assist didn't read the household knowledge in the sandbox")
        self.tools = [SandboxTool(t, self) for t in built["tools"]]
        return RecordedAPIInstance(api=self, api_prompt=built["api_prompt"], llm_context=llm_context,
                                   tools=self.tools, custom_serializer=serializer)

    async def observe(self, source, exclude=()):
        await self.sandbox.call("observe", source=source, exclude=sorted(exclude))
        await self.refresh()

    async def refresh(self):
        found = await self.sandbox.call("snapshot")
        self.world.states, self.world.transitions = found["states"], found["transitions"]
        self.world.calls = found["commands"]

    async def stop(self):
        if self.sandbox is not None:
            sandbox, self.sandbox, self.tools = self.sandbox, None, None
            await sandbox.stop()

    async def async_get_api_instance(self, llm_context):
        raise UnsupportedSimulation("A replay's tools come from its sandbox")


def reads_household(turn):
    """Whether Assist read the household knowledge in this recorded turn: the switch was on (recorded since the lines
    came with Assist), or the assistant used the household API (up to 0.37)."""
    from .household import API_ID, api_ids
    return bool(turn["source"].get("household_on")) or API_ID in api_ids(turn["prepared"].get("api_id"))


async def register(hass, api):
    """In the sandbox: serve this API there (the household API up to 0.37, with a replay's knowledge)."""
    from homeassistant.helpers import llm
    apis = llm._async_get_apis(hass)
    apis.pop(api.id, None)  # A sandbox serves one replay's knowledge at a time.
    llm.async_register_api(hass, api)


class FrozenTurnLog(conversation.ChatLog):
    """Use the exact prepared prefix, never later user turns or today's context."""
    def __init__(self, hass, case, recording, *, turn_number=None, scripts=None, automations=None,
                 exposure_changes=None, alias_changes=None, alias_removals=None, prompt_template=None,
                 use_sandbox_prompt=False, household=None):
        turns = replayed_turns(case, recording)
        turn = next((t for t in turns if t["number"] == (turn_number or turns[0]["number"])), None)
        if turn is None:
            raise CaseError("Turn is outside this journey")
        ctx = turn["prepared"]["context"]
        identity = ctx.get("context", {})
        if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
            raise CaseError("Cannot backfill missing historical identity")
        history = turn["history"]
        if any("provider_native_history_omitted" in x for x in recording.get("limitations", [])) and any(m["role"] == "assistant" for m in history):
            raise CaseError("Provider-native history is missing at this cutoff")
        content = []
        for m in history:
            if m["role"] == "system":
                content.append(conversation.SystemContent(content=m["content"]))
            elif m["role"] == "user":
                content.append(conversation.UserContent(content=m["content"]))
            elif m["role"] == "assistant" and not (m.get("thinking_content") or m.get("native")):
                content.append(conversation.AssistantContent(agent_id=m["agent_id"], content=m.get("content"),
                    tool_calls=[llm.ToolInput(**c) for c in m.get("tool_calls", [])] or None))
            elif m["role"] == "tool_result":
                content.append(conversation.ToolResultContent(agent_id=m["agent_id"],
                    tool_call_id=m["tool_call_id"], tool_name=m["tool_name"], result=llm.ToolResult(**m["result"])))
            else:
                raise CaseError("Unsupported historical content")
        run_id = f"improver-replay-{uuid4()}"
        super().__init__(hass, run_id, content=content)
        self.replay_api = SandboxHome(hass, recording, turns[0], run_id, scripts=scripts,
                                      automations=automations, exposure_changes=exposure_changes,
                                      alias_changes=alias_changes, alias_removals=alias_removals,
                                      prompt_template=prompt_template, household=household)
        # A candidate whose change shows in Assist's instructions (a preference, an alias) uses the prompt HA builds
        # in the sandbox with the change; otherwise the model sees exactly the recorded prompt.
        self.use_sandbox_prompt = use_sandbox_prompt
        self.turn, self.pending_source = turn, None
        # What the recorded assistant changed: the replay's own run decides those, not the recording's snapshots.
        self.assistant_entities = {t["entity_id"] for case_turn in turns
                                   for t in recorded_transitions(recording, case_turn)}
        self.recorded_context = llm.LLMContext(**{**ctx, "context": Context(**identity)})
        self.input = conversation.ConversationInput(text=history[-1]["content"],
            context=Context(**identity), conversation_id=run_id, device_id=ctx.get("device_id"),
            satellite_id=None, language=ctx.get("language") or "en", agent_id="conversation.replay")
        self.turn_number = turn["number"]
        self.external_calls = []
        self.usage_events = []

    def async_trace(self, agent_details):
        # Capture the provider-neutral statistics HA already emits. Providers
        # differ; absence is unknown, never zero usage or a billing guarantee.
        if isinstance(agent_details.get("stats"), dict):
            self.usage_events.append(plain(agent_details["stats"]))
        super().async_trace(agent_details)

    def usage_report(self):
        return {"status": "provider_reported_partial" if self.usage_events else "unavailable",
                "events": deepcopy(self.usage_events),
                "billing_total": None, "provider_request_count": None,
                "limits": "HA trace statistics may omit cache writes, tool fees and retries; events are not request counts"}

    async def async_provide_llm_data(self, llm_context, user_llm_hass_api=None,
                                   user_llm_prompt=None, user_extra_system_prompt=None):
        if self.pending_source is not None:  # Others moved things in the home between the recorded turns.
            await self.replay_api.observe(self.pending_source, self.assistant_entities)
            self.pending_source = None
        self.llm_api = await self.replay_api.api_instance(self.turn, self.recorded_context)
        if self.use_sandbox_prompt:
            self.content[0] = conversation.SystemContent(content=self.replay_api.prompt)
        self.llm_input_provided_index = len(self.content)

    @property
    def prompt_matches_recording(self):
        """Whether HA, in the sandbox, built the same system prompt the model saw when recorded."""
        return self.replay_api.prompt == self.turn["history"][0]["content"] if self.replay_api.prompt else None

    async def close(self):
        await self.replay_api.stop()

    def advance(self, turn, check_sources=True):
        """Retain this trial's actual responses/native history, never paste old replies. check_sources compares the
        replay's devices with the next recorded snapshot; a practice session's snapshots hold the practice engine
        of the recording day, so its baseline relies on the per-turn comparison with today's engine instead."""
        if turn["number"] != self.turn_number + 1:
            raise CaseError("Baseline continuation must use the next original input")
        if self.unresponded_tool_results or self.replay_api.unsupported:
            raise CaseError("Cannot continue an incomplete or unsupported replay")
        world = self.replay_api.world
        for entity in ({c["entity_id"] for c in world.calls} if check_sources else ()):
            if entity not in turn["source"]["states"] or world.states.get(entity) != turn["source"]["states"][entity]["state"]:
                raise CaseError("Trial effects differ from the next recorded source state")
        # Unrelated observations move to their recorded values when the next turn starts, while the sandbox keeps
        # its own controlled-state trajectory.
        self.pending_source, self.turn = turn["source"], turn
        ctx = turn["prepared"]["context"]
        identity = ctx["context"]
        if not {"id", "user_id", "parent_id"} <= identity.keys():
            raise CaseError("Cannot backfill missing historical identity")
        self.recorded_context = llm.LLMContext(**{**ctx, "context": Context(**identity)})
        self.content[0] = conversation.SystemContent(content=turn["history"][0]["content"])
        self.async_add_user_content(conversation.UserContent(content=turn["messages"][0]["content"]))
        self.input = conversation.ConversationInput(text=turn["messages"][0]["content"],
            context=Context(**identity), conversation_id=self.conversation_id,
            device_id=ctx.get("device_id"), satellite_id=None,
            language=ctx.get("language") or "en", agent_id="conversation.replay")
        self.turn_number = turn["number"]

    def continue_with(self, turn):
        """A candidate's next turn: the user's next recorded message, whatever the candidate changed so far."""
        if turn["number"] != self.turn_number + 1:
            raise CaseError("A follow-up must be the next original input")
        if self.unresponded_tool_results or self.replay_api.unsupported:
            raise CaseError("Cannot continue an incomplete or unsupported replay")
        self.pending_source, self.turn = turn["source"], turn
        ctx = turn["prepared"]["context"]
        identity = ctx["context"]
        self.recorded_context = llm.LLMContext(**{**ctx, "context": Context(**identity)})
        if not self.use_sandbox_prompt:
            self.content[0] = conversation.SystemContent(content=turn["history"][0]["content"])
        self.async_add_user_content(conversation.UserContent(content=turn["messages"][0]["content"]))
        self.input = conversation.ConversationInput(text=turn["messages"][0]["content"],
            context=Context(**identity), conversation_id=self.conversation_id,
            device_id=ctx.get("device_id"), satellite_id=None,
            language=ctx.get("language") or "en", agent_id="conversation.replay")
        self.turn_number = turn["number"]

    async def async_add_delta_content_stream(self, agent_id, stream):
        async def observed_stream():
            async for delta in stream:
                for call in delta.get("tool_calls", []):
                    if not call.external:
                        continue
                    self.external_calls.append({"name": call.tool_name, "args": deepcopy(call.tool_args),
                                                "provenance": "provider_execution"})
                    if call.tool_name != "tool_search_tool_bm25":
                        # A server tool may already have executed remotely when
                        # reported. This stops evaluation, NOT that remote action.
                        # All HA controls still resolve only to our simulator.
                        message = "Provider tool is outside catalogue-search evaluation coverage"
                        self.replay_api.unsupported.append(message)
                        raise UnsupportedSimulation(message)
                yield delta
        async for content in super().async_add_delta_content_stream(agent_id, observed_stream()):
            yield content


async def check_fidelity(hass, recording, turn_number=1):
    """Seed a sandbox from a recording's first turn and compare what HA builds there with what was recorded:
    the system prompt and the tools. No model call. Differences point at gaps in recording or seeding."""
    import difflib
    from time import monotonic
    turn = recording_turns(recording)[turn_number - 1]
    ctx = turn["prepared"]["context"]
    identity = ctx.get("context", {})
    if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
        raise CaseError("Cannot backfill missing historical identity")
    started = monotonic()
    home = SandboxHome(hass, recording, turn, f"improver-fidelity-{uuid4()}")
    try:
        instance = await home.api_instance(turn, llm.LLMContext(**{**ctx, "context": Context(**identity)}))
        seeded = round(monotonic() - started, 2)
        recorded_prompt = turn["history"][0]["content"]
        recorded_tools = [t["name"] for t in turn["prepared"]["tools"]]
        tools = [t.name for t in instance.tools]
        report = {"recording_id": recording["id"], "prompt_matches": home.prompt == recorded_prompt,
                "tools_match": tools == recorded_tools,
                "missing_tools": sorted(set(recorded_tools) - set(tools)),
                "extra_tools": sorted(set(tools) - set(recorded_tools)),
                "prompt_diff": list(difflib.unified_diff(recorded_prompt.splitlines(), home.prompt.splitlines(),
                                                         "recorded", "sandbox", lineterm="", n=0))[:60],
                "seconds_to_seed": seeded, "paid_inference_performed": False,
                # What HA logged while setting up the recorded home (an automation it couldn't set up, say).
                "sandbox_log": list(home.sandbox.log)[:40]}
    finally:
        stopping = monotonic()
        await home.stop()
    report["seconds_to_stop"] = round(monotonic() - stopping, 2)
    return report
