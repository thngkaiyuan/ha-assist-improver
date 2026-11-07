"""Demo AI: a made-up AI provider for Assist Improver's demo household (screenshots, never a real home).

It offers AI Tasks (with an Add AI Task flow) and conversation assistants that control the home through Assist, so the improver's setup
and review screens have something to show. Nothing here calls a model. demo_ai.add_assistant adds another
assistant later, as a household might.
"""

from homeassistant.config_entries import ConfigSubentry

DOMAIN = "demo_ai"


async def async_setup_entry(hass, entry):
    await hass.config_entries.async_forward_entry_setups(entry, ["ai_task", "conversation"])
    entry.async_on_unload(entry.add_update_listener(_reload))  # A new AI Task entry adds its entity.

    async def add_assistant(call):
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="conversation", title=call.data.get("name", "Bedroom assistant"), unique_id=None,
            data={"llm_hass_api": ["assist"], "prompt": "You are the voice assistant for the demo home."}))

    hass.services.async_register(DOMAIN, "add_assistant", add_assistant)
    return True


async def _reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    hass.services.async_remove(DOMAIN, "add_assistant")
    return await hass.config_entries.async_unload_platforms(entry, ["ai_task", "conversation"])
