"""Test-only, for the end-to-end suite's throwaway Home Assistant (tools/e2e); never shipped.

Home Assistant ends a chat session after five idle minutes (homeassistant/helpers/chat_session.py,
CONVERSATION_TIMEOUT), and Assist Improver's recorder closes a conversation when its chat session ends. Journeys
can't wait five minutes for each conversation to end, and a shorter timeout would also end one between its turns on
a busy machine. So the timeout stays as it is, and a journey ends its conversations once they're over: the action
`e2e_support.end_conversations` ends each chat session not answering a request now, as Home Assistant's clean-up
does once one has idled out, and answers how many still are (the journey asks again until none is).

On HA OS (tools/bench/haos.sh), whose Supervisor starts Home Assistant with an environment of its own, `model_url`
points the anthropic library at the stand-in model, as ANTHROPIC_BASE_URL in Home Assistant's environment does
elsewhere: set here, before the replay workers that inherit it start, and before the anthropic integration makes its
client, or, if it was set up first, by setting it up again. Nothing else changes.
"""

import os

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse, callback
from homeassistant.helpers import chat_session

DOMAIN = "e2e_support"


async def async_setup(hass, config):
    options = config.get(DOMAIN) or {}

    @callback
    def end_conversations(_call):
        sessions = hass.data.get(chat_session.DATA_CHAT_SESSION, {})
        for conversation_id, session in list(sessions.items()):
            if not session.in_use:  # As chat_session's clean-up: one answering a request ends after it.
                del sessions[conversation_id]
                session.async_cleanup()
        return {"in_use": len(sessions)}
    hass.services.async_register(DOMAIN, "end_conversations", end_conversations,
                                 supports_response=SupportsResponse.ONLY)
    if url := options.get("model_url"):
        os.environ["ANTHROPIC_BASE_URL"] = url
        for entry in hass.config_entries.async_entries("anthropic"):
            if entry.state is not ConfigEntryState.NOT_LOADED:  # Its client made already: made again.
                await hass.config_entries.async_reload(entry.entry_id)
    return True
