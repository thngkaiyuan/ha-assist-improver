"""The demo provider with its assistants and AI Tasks, and an "Add AI Task" flow like real providers have."""

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

ASSISTANTS = ("Living room assistant", "Kitchen assistant")
# Named like Claude's models so the improver tells the strong one from the small one, as with a real provider; the
# AI Tasks themselves are called Demo Large and Demo Small.
MODELS = [{"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}, {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5"}]
TASKS = {"claude-opus-5-5": "Demo Large", "claude-haiku-5-5": "Demo Small"}


class DemoAIConfigFlow(ConfigFlow, domain="demo_ai"):
    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {"ai_task_data": AITaskFlow}

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("demo_ai")
        self._abort_if_unique_id_configured()
        assistants = [{"subentry_type": "conversation", "title": name, "unique_id": None,
                       "data": {"llm_hass_api": ["assist"], "prompt": "You are the voice assistant for the demo home."}}
                      for name in ASSISTANTS]
        tasks = [{"subentry_type": "ai_task_data", "title": TASKS[m["value"]], "unique_id": None,
                  "data": {"chat_model": m["value"]}} for m in MODELS]
        return self.async_create_entry(title="Demo AI", data={}, subentries=assistants + tasks)


class AITaskFlow(ConfigSubentryFlow):
    """Name and model, as real providers ask."""

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data={"chat_model": user_input["chat_model"]})
        return self.async_show_form(step_id="user", data_schema=probatio.Schema({
            probatio.Required("name", default="Demo AI Task"): str,
            probatio.Required("chat_model", default="claude-opus-5-5"): SelectSelector(SelectSelectorConfig(options=MODELS))}))
