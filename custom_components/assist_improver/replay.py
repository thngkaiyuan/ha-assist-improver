"""Per-run recorded HA context and receipt-bound tools.

No provider client or model-run service is created here. Offline tests pass this
log to HA's existing conversation adapter with a scripted transport. A future
model baseline must separately pass readiness checks and inference authorization.
"""

from copy import deepcopy
from uuid import uuid4

import probatio
from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.helpers import llm

from .cases import CaseError, verify_case
from .serialization import plain
from .simulation import RecordedWorld, UnsupportedSimulation


class RecordedParameters:
    def __init__(self, schema):
        self.schema = deepcopy(schema)

    def __call__(self, args):
        if not isinstance(args, dict):
            raise probatio.Invalid("Tool arguments must be an object")
        return args


def schema_serializer(value):
    if isinstance(value, RecordedParameters):
        return deepcopy(value.schema)
    raise TypeError("Unknown recorded schema object")


class ReceiptTool(llm.Tool):
    def __init__(self, definition, replay_api):
        self.name = definition["name"]
        self.description = definition["description"]
        self.integration = definition["integration"]
        self.parameters = probatio.Schema(RecordedParameters(definition["parameters"]))
        self.replay_api = replay_api

    async def async_call(self, hass, tool_input, llm_context):
        api = self.replay_api
        try:
            binding = next((b for b in api.bindings
                            if b["call"]["tool_name"] == tool_input.tool_name
                            and b["call"]["tool_args"] == tool_input.tool_args), None)
            if binding is None:
                raise UnsupportedSimulation("No applicable recorded tool behavior for these arguments")
            result = api.world.apply_recorded(binding["call"], binding["receipt"], api.source)
            api.dispatched.append({"name": tool_input.tool_name, "args": deepcopy(tool_input.tool_args)})
            return llm.ToolResult(**result)
        except CaseError as err:
            api.unsupported.append(str(err))
            # HA converts HomeAssistantError into a tool error and asks the
            # model again. An unsupported simulation must instead end this run.
            raise UnsupportedSimulation(str(err)) from err


class RecordedAPIInstance(llm.APIInstance):
    async def async_call_tool(self, tool_input):
        if not any(tool.name == tool_input.tool_name for tool in self.tools):
            message = "Tool is absent from the recorded schema"
            self.api.unsupported.append(message)
            raise UnsupportedSimulation(message)
        return await super().async_call_tool(tool_input)


class RecordedAPI(llm.API):
    def __init__(self, hass, turn, run_id):
        super().__init__(hass=hass, id=run_id, name="Recorded context")
        self.world = RecordedWorld(turn["source"])
        self.unsupported, self.dispatched = [], []
        self.load_turn(turn)

    def load_turn(self, turn):
        self.source = deepcopy(turn["source"])
        receipts = {r["tool_call_id"]: r for r in turn["results"]}
        self.bindings = [{"call": deepcopy(c), "receipt": deepcopy(receipts[c["id"]])}
                         for c in turn["calls"] if not c.get("external")]
        self.tools = [ReceiptTool(d, self) for d in turn["prepared"]["tools"]]

    async def async_get_api_instance(self, context):
        return RecordedAPIInstance(api=self, api_prompt="", llm_context=context,
                                   tools=self.tools, custom_serializer=schema_serializer)


class FrozenTurnLog(conversation.ChatLog):
    """Use the exact prepared prefix, never later user turns or today's context."""
    def __init__(self, hass, case, recording, *, turn_number=None):
        turns = verify_case(case, recording)
        turn = next((t for t in turns if t["number"] == (turn_number or case["turns"][0])), None)
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
        self.replay_api = RecordedAPI(hass, turn, run_id)
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
        self.llm_api = await self.replay_api.async_get_api_instance(self.recorded_context)
        self.llm_input_provided_index = len(self.content)

    def advance(self, turn):
        """Retain this trial's actual responses/native history, never paste old replies."""
        if turn["number"] != self.turn_number + 1:
            raise CaseError("Baseline continuation must use the next original input")
        if self.unresponded_tool_results or self.replay_api.unsupported:
            raise CaseError("Cannot continue an incomplete or unsupported replay")
        touched = {c["entity_id"] for c in self.replay_api.world.calls}
        for entity in touched:
            if (entity not in turn["source"]["states"] or
                    self.replay_api.world.states[entity] != turn["source"]["states"][entity]["state"]):
                raise CaseError("Trial effects differ from the next recorded source state")
        # Advance unrelated observations to their recorded values, while
        # preserving the simulation's own controlled-state trajectory.
        for entity, state in turn["source"]["states"].items():
            if entity not in touched:
                self.replay_api.world.states[entity] = state["state"]
        self.replay_api.load_turn(turn)
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
