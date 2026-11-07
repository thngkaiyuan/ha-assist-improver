"""Test-only, for the end-to-end suite's throwaway Home Assistant (tools/e2e); never shipped.

Home Assistant ends a chat session after five idle minutes (homeassistant/helpers/chat_session.py,
CONVERSATION_TIMEOUT), and Assist Improver's recorder closes a conversation when its chat session ends. Journeys
can't wait five minutes for each conversation to end, so this sets that timeout to `conversation_timeout` seconds
(configuration.yaml). Home Assistant reads the constant each time it schedules or runs its clean-up, so setting it
before the first conversation is enough.

On HA OS (tools/bench/haos.sh), whose Supervisor starts Home Assistant with an environment of its own, `model_url`
points the anthropic library at the stand-in model, as ANTHROPIC_BASE_URL in Home Assistant's environment does
elsewhere: set here, before the replay workers that inherit it start, and before the anthropic integration makes its
client, or, if it was set up first, by setting it up again. Nothing else changes.
"""

from datetime import timedelta
import os

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import chat_session

DOMAIN = "e2e_support"


async def async_setup(hass, config):
    options = config.get(DOMAIN) or {}
    if seconds := options.get("conversation_timeout"):
        chat_session.CONVERSATION_TIMEOUT = timedelta(seconds=float(seconds))
    if url := options.get("model_url"):
        os.environ["ANTHROPIC_BASE_URL"] = url
        for entry in hass.config_entries.async_entries("anthropic"):
            if entry.state is not ConfigEntryState.NOT_LOADED:  # Its client made already: made again.
                await hass.config_entries.async_reload(entry.entry_id)
    return True
