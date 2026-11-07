"""Only test transport uses SDK event types. Production recorder has no SDK."""

import asyncio
from copy import deepcopy
from datetime import datetime, UTC
from types import SimpleNamespace

from anthropic.types import ModelInfo, MessageStreamEvent
from pydantic import TypeAdapter
from homeassistant.components.anthropic.conversation import AnthropicConversationEntity

from test_interception import API_ID

OPTIONS = {"chat_model": "fixture-no-model", "max_tokens": 64, "prompt_caching": "off",
           "thinking_budget": 0, "thinking_type": "disabled"}


def events_of(blocks):
    """One model message as the Messages API streams it: its events, in order."""
    events = [{"type": "message_start", "message": {
        "id": "fixture-message", "type": "message", "role": "assistant",
        "model": "fixture-no-model", "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}]
    for i, block in enumerate(blocks):
        # Thinking text/signatures arrive in deltas in a real stream. HA
        # deliberately does not recover them from content_block_start.
        if block["type"] == "thinking":
            events.extend([
                {"type": "content_block_start", "index": i,
                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": i,
                 "delta": {"type": "thinking_delta", "thinking": block["thinking"]}},
                {"type": "content_block_delta", "index": i,
                 "delta": {"type": "signature_delta", "signature": block["signature"]}},
                {"type": "content_block_stop", "index": i}])
        else:
            events.extend([{"type": "content_block_start", "index": i, "content_block": block},
                           {"type": "content_block_stop", "index": i}])
    events.extend([{"type": "message_delta", "delta": {
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "stop_sequence": None}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"}])
    return events


class EventStream:
    def __init__(self, blocks):
        self.events = [TypeAdapter(MessageStreamEvent).validate_python(e) for e in events_of(blocks)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.events.pop(0)


class AnthropicTransport:
    def __init__(self, tool_name, tool_args):
        self.requests = []
        self.blocks = [
            [{"type": "tool_use", "id": "fixture-tool", "name": tool_name, "input": tool_args}],
            [{"type": "text", "text": "Fixture finished."}],
        ]
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        assert self.blocks, "Unexpected inference call"
        return EventStream(self.blocks.pop(0))


def anthropic_agent(hass, tool_name="FixtureSetOption", tool_args=None):
    if tool_args is None:
        tool_args = {"entity_id": "select.fixture_heater_level", "option": "high"}
    client = AnthropicTransport(tool_name, tool_args)
    info = ModelInfo(id="fixture-no-model", type="model", display_name="Fixture",
                     created_at=datetime(1970, 1, 1, tzinfo=UTC), lifecycle="active")
    coordinator = SimpleNamespace(client=client, data=[info], last_update_success=True,
        get_model_info=lambda model: (info, True), async_set_updated_data=lambda data: None,
        mark_connection_healthy=lambda: None)
    subentry = SimpleNamespace(subentry_id="fixture-anthropic", title="Fixture Claude",
        data={**OPTIONS, "prompt": "Synthetic recording fixture.", "llm_hass_api": API_ID})
    entry = SimpleNamespace(entry_id="fixture-entry", runtime_data=coordinator)
    agent = AnthropicConversationEntity(entry, subentry)
    agent.hass = hass
    agent.entity_id = "conversation.fixture_claude"
    return agent, client, subentry
