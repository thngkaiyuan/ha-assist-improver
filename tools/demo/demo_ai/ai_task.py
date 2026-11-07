"""The demo's AI Tasks, one per AI Task entry: listed in the improver's settings; they answer nothing."""

from homeassistant.components.ai_task import AITaskEntity, AITaskEntityFeature
from homeassistant.exceptions import HomeAssistantError


async def async_setup_entry(hass, entry, async_add_entities):
    for sub in entry.subentries.values():
        if sub.subentry_type == "ai_task_data":
            async_add_entities([DemoAITask(entry, sub)], config_subentry_id=sub.subentry_id)


class DemoAITask(AITaskEntity):
    _attr_supported_features = AITaskEntityFeature.GENERATE_DATA

    def __init__(self, entry, sub):
        self._attr_unique_id = f"{entry.entry_id}-{sub.subentry_id}"
        self._attr_name = sub.title

    async def _async_generate_data(self, task, chat_log):
        raise HomeAssistantError("Demo AI has no model behind it")
