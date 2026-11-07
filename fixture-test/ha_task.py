"""Home Assistant's real AI Task path for the stand-in AI roles (kit.Model, the graph's harness/roles.Roles), offline.

ScriptedTask is an AI Task entity whose model is scripted: Home Assistant's own async_generate_data, chat log, llm API
and conversation trace run as they do for a real provider, and only the model's decisions are the stand-in's. A
stand-in calls its tools through use / use_many: one assistant message with its tool calls, streamed through the chat
log as a provider streams it. Home Assistant then starts each call as a task of its own, at once, checks no argument,
hands back only HomeAssistantError and probatio.Invalid as errors (anything else a tool raises ends the generation),
and the arguments and results cross as JSON. A call whose arguments its tool's schema refuses is the harness's slip,
unless the stand-in marks it a deliberate one (slip=True). Each role call also checks its tools as a provider would
take them (HA's Anthropic conversion: unique names it accepts, each input schema an object)."""

from contextvars import ContextVar
import json
import re
from types import SimpleNamespace

from homeassistant.components import ai_task
from homeassistant.components.ai_task.const import DATA_COMPONENT, DATA_PREFERENCES, AITaskEntityFeature
from homeassistant.components.ai_task.entity import AITaskEntity
from homeassistant.components.anthropic.entity import _format_tool
from homeassistant.components.conversation import AssistantContent, ToolResultContent
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.json import json_dumps

ENTITIES = ("ai_task.opus", "ai_task.haiku")  # The improver's models, as the tests configure them.
NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")  # A tool name the provider takes.
ANSWERING: ContextVar = ContextVar("ha_task_answering", default=None)  # (entity, chat log) a stand-in answers in.


def serve(hass, brain, entity_ids=ENTITIES):
    """The AI Task entities, answered by the stand-in brain (brain.answer(hass, role, payload, tools)): the list the
    harness's slips are kept in."""
    slips = []
    entities = {entity_id: ScriptedTask(hass, entity_id, brain, slips) for entity_id in entity_ids}
    stand_in(hass, entities.get)
    return slips


def stand_in(hass, get_entity):
    """Home Assistant's AI Task component as a task call reads it: these entities (get_entity), none preferred."""
    hass.data[DATA_COMPONENT] = SimpleNamespace(get_entity=get_entity)
    hass.data.setdefault(DATA_PREFERENCES, SimpleNamespace(gen_data_entity_id=None))


def refused(tools, serializer=None):
    """Why a provider would refuse these tools (HA's Anthropic conversion), if it would."""
    problems, names = [], [tool.name for tool in tools]
    for tool in tools:
        if not NAME.match(tool.name or ""):
            problems.append(f"{tool.name!r}: not a name the provider takes")
        try:
            schema = _format_tool(tool, serializer)["input_schema"]
        except Exception as err:  # noqa: BLE001 - whatever stops the conversion, the provider never gets it.
            problems.append(f"{tool.name}: {type(err).__name__}: {err}")
            continue
        if schema.get("type") != "object":
            problems.append(f"{tool.name}: its input schema isn't an object")
    problems += [f"{name}: two tools have this name" for name in sorted({n for n in names if names.count(n) > 1})]
    return problems


class ScriptedTask(AITaskEntity):
    """An AI Task entity at the provider's level: Home Assistant runs everything around the model, which is the
    stand-in's."""
    _attr_supported_features = AITaskEntityFeature.GENERATE_DATA

    def __init__(self, hass, entity_id, brain, slips):
        self.hass, self.entity_id, self.brain, self.slips = hass, entity_id, brain, slips
        self.platform = SimpleNamespace(domain="scripted")
        self.async_write_ha_state = lambda: None  # Not added to Home Assistant: no state to write.

    async def _async_generate_data(self, task, chat_log):
        role = task.name.split(": ", 1)[1]
        payload = json.loads(task.instructions.split("Input (JSON):\n", 1)[1])
        api = chat_log.llm_api
        tools = list(api.tools) if api else []
        if problems := refused(tools, api.custom_serializer if api else None):
            raise HomeAssistantError(f"The provider refused the tools: {'; '.join(problems)}")
        token = ANSWERING.set((self, chat_log))
        try:
            data = await self.brain.answer(self.hass, role, payload, {tool.name: tool for tool in tools})
        finally:
            ANSWERING.reset(token)
        text = json.dumps(data) if task.structure else data
        chat_log.async_add_assistant_content_without_tools(AssistantContent(agent_id=self.entity_id, content=text))
        return ai_task.GenDataTaskResult(conversation_id=chat_log.conversation_id,
                                         data=json.loads(text) if task.structure else text)


async def use_many(hass, calls, *, slip=False):
    """One assistant message with these tool calls ([(tool, args)]), as a provider streams it; each call's result, in
    order, as the provider hands it back to the model (HA's json_dumps)."""
    answering = ANSWERING.get()
    if answering is None:
        raise RuntimeError("A stand-in called a tool outside an AI Task")
    entity, chat_log = answering
    inputs = []
    for tool, args in calls:
        args = json.loads(json.dumps(args or {}))
        if not slip:
            try:
                tool.parameters(args)
            except Exception as err:  # noqa: BLE001 - any refusal: the stand-in got its own call wrong.
                entity.slips.append(f"{tool.name}: {type(err).__name__}: {err}")
        inputs.append(llm.ToolInput(tool_name=tool.name, tool_args=args))

    async def message():
        yield {"role": "assistant"}
        yield {"tool_calls": inputs}
    results = {}
    async for content in chat_log.async_add_delta_content_stream(entity.entity_id, message()):
        if isinstance(content, ToolResultContent):
            results[content.tool_call_id] = json.loads(json_dumps(content.result.data))
    return [results[call.id] for call in inputs]


async def use(hass, tool, args=None, *, slip=False):
    """One tool call, alone in its message (use_many)."""
    [result] = await use_many(hass, [(tool, args)], slip=slip)
    return result
