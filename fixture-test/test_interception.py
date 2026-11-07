"""Offline mechanism proof, not a replay of a household incident.

Runs the installed HA code. Only the model transport and tool implementation
are fakes. No live HA connection, credentials, or device integrations are loaded.
"""

import asyncio
from copy import deepcopy
import json
import socket
import subprocess
from types import SimpleNamespace

import probatio as vol
import pytest

from homeassistant.components import conversation
from homeassistant.components.open_router.conversation import OpenRouterConversationEntity
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm


TARGET = "select.fixture_heater_level"
API_ID = "fixture_replay"


class FixtureSelectTool(llm.Tool):
    name = "FixtureSetOption"
    description = "Set a selector option in the recorded fixture world."
    integration = "fixture_improver"
    parameters = vol.Schema({vol.Required("entity_id"): str, vol.Required("option"): str})

    def __init__(self, world, calls):
        self.world = world
        self.calls = calls

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        entity = args["entity_id"]
        option = args["option"]
        if entity not in self.world or option not in ("off", "low", "medium", "high"):
            raise HomeAssistantError("Unsupported fixture target or option")
        before = self.world[entity]
        self.world[entity] = option
        self.calls.append((entity, before, option))
        return llm.ToolResult(data={"entity_id": entity, "before": before, "state": option})


class FixtureAPI(llm.API):
    def __init__(self, hass):
        super().__init__(hass=hass, id=API_ID, name="Offline fixture replay")
        self.worlds = {}
        self.calls = {}

    async def async_get_api_instance(self, llm_context):
        run_id = llm_context.context.id
        # An unregistered run cannot obtain tools or fall through to a live API.
        if run_id not in self.worlds:
            raise HomeAssistantError("No fixture for this run")
        return llm.APIInstance(
            api=self,
            api_prompt="Synthetic fixture world. Use the provided tools.",
            llm_context=llm_context,
            tools=[FixtureSelectTool(self.worlds[run_id], self.calls[run_id])],
        )


def tool_message(name="FixtureSetOption", args=None):
    return SimpleNamespace(
        role="assistant", content=None,
        tool_calls=[SimpleNamespace(
            type="function", id="fixture-call-1",
            function=SimpleNamespace(name=name, arguments=json.dumps(args or {
                "entity_id": TARGET, "option": "high"
            })),
        )],
    )


def completion(message, stream=False):
    """A scripted model message as OpenAI's SDK hands it to OpenRouter's integration: the completion, or one chunk
    carrying all of it when asked to stream (Home Assistant 2026.11 on)."""
    if not stream:
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])
    calls = [SimpleNamespace(index=i, id=call.id, function=call.function)
             for i, call in enumerate(message.tool_calls or [])]
    delta = SimpleNamespace(role=message.role, content=message.content, tool_calls=calls, model_extra={})

    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=delta,
                                                       finish_reason="tool_calls" if calls else "stop")])
    return chunks()


class ScriptedTransport:
    """SDK-shaped in-process transport. Never constructs a real API client."""
    def __init__(self, first_message):
        self.messages = [first_message, SimpleNamespace(
            role="assistant", content="Fixture response complete.", tool_calls=None
        )]
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        assert len(self.requests) <= 2, "Unexpected model iteration"
        await asyncio.sleep(0)  # Exercise interleaving of independent replay runs.
        return completion(self.messages.pop(0), kwargs.get("stream"))


def make_run(hass, api, *, initial="off", message=None):
    context = Context()
    api.worlds[context.id] = {TARGET: initial}
    api.calls[context.id] = []
    transport = ScriptedTransport(message or tool_message())
    subentry = SimpleNamespace(
        subentry_id=context.id, title="Fixture replay",
        data={"model": "fixture-no-model", "llm_hass_api": API_ID,
              "prompt": "Synthetic offline mechanism test."},
    )
    agent = OpenRouterConversationEntity(SimpleNamespace(runtime_data=transport), subentry)
    agent.hass = hass
    agent.entity_id = "conversation.fixture_replay"
    user_input = conversation.ConversationInput(
        text="Set the fixture heater to high.", context=context,
        conversation_id=None, device_id=None, satellite_id=None, language="en",
        agent_id=agent.entity_id,
    )
    return agent, user_input, transport


def register_api(hass):
    api = FixtureAPI(hass)
    llm.async_register_api(hass, api)
    return api


def returned_tool_result(transport):
    assert len(transport.requests) == 2
    results = [m for m in transport.requests[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "fixture-call-1"
    return json.loads(results[0]["content"])


@pytest.mark.asyncio
async def test_actual_openrouter_loop_round_trip(hass, effect_guards):
    api = register_api(hass)
    # HA's own state intentionally disagrees with the replay fixture.
    hass.states.async_set(TARGET, "low")
    agent, request, transport = make_run(hass, api)
    result = await agent.async_process(request)
    assert api.worlds[request.context.id][TARGET] == "high"
    assert api.calls[request.context.id] == [(TARGET, "off", "high")]
    assert hass.states.get(TARGET).state == "low"
    assert returned_tool_result(transport) == {
        "data": {"entity_id": TARGET, "before": "off", "state": "high"}, "error": False
    }
    schemas = transport.requests[0]["extra_body"]["tools"]
    assert [t["function"]["name"] for t in schemas] == ["FixtureSetOption"]
    assert result.response.speech["plain"]["speech"] == "Fixture response complete."
    assert effect_guards == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    tool_message("HassTurnOn"),
    tool_message(args={"entity_id": "switch.not_in_fixture", "option": "high"}),
    tool_message(args={"entity_id": TARGET, "option": "unsupported"}),
    tool_message(args={"entity_id": TARGET}),
])
async def test_unsupported_calls_fail_closed(hass, effect_guards, message):
    api = register_api(hass)
    agent, request, transport = make_run(hass, api, message=message)
    await agent.async_process(request)
    assert returned_tool_result(transport)["error"] is True
    assert api.worlds[request.context.id] == {TARGET: "off"}
    assert api.calls[request.context.id] == []
    assert effect_guards == []


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_share_worlds(hass, effect_guards):
    api = register_api(hass)
    runs = [make_run(hass, api, initial=value) for value in ("off", "medium")]
    results = await asyncio.gather(*(a.async_process(r) for a, r, _ in runs))
    assert results[0].conversation_id != results[1].conversation_id
    assert [returned_tool_result(t)["data"]["before"] for _, _, t in runs] == ["off", "medium"]
    assert len({id(world) for world in api.worlds.values()}) == 2
    assert effect_guards == []


@pytest.mark.asyncio
async def test_streamed_call_is_intercepted_before_stream_finishes(hass, effect_guards):
    api = register_api(hass)
    _, request, _ = make_run(hass, api)
    log = conversation.ChatLog(hass, "stream-fixture")
    await log.async_provide_llm_data(request.as_llm_context("fixture"), API_ID, "Fixture")

    async def deltas():
        yield {"role": "assistant", "content": ""}
        yield {"tool_calls": [llm.ToolInput(
            tool_name="FixtureSetOption", tool_args={"entity_id": TARGET, "option": "high"}
        )]}
        await hass.async_block_till_done()
        # HA may dispatch before the final assistant response is complete.
        assert api.worlds[request.context.id][TARGET] == "high"
        yield {"content": "Completed."}

    content = [part async for part in log.async_add_delta_content_stream("conversation.fixture", deltas())]
    assert any(isinstance(part, conversation.ToolResultContent) for part in content)
    assert len(api.calls[request.context.id]) == 1
    assert effect_guards == []


@pytest.mark.asyncio
async def test_guards_reject_effect_attempts(hass, effect_guards):
    # Deliberate negative controls: these attempts must be blocked, never executed.
    with pytest.raises(AssertionError, match="HA service"):
        await hass.services.async_call("select", "select_option", {})
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="socket.connect"):
            sock.connect(("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="subprocess"):
        subprocess.Popen(["fixture-do-not-run"])
    assert effect_guards == ["HA service", "socket.connect", "subprocess"]
