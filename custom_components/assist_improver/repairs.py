"""Fix the AI Task notice in place: the same steps as Configure, inside Repairs."""

from homeassistant.components.repairs import RepairsFlow

from .config_flow import _AiTaskSteps
from .const import CONF_AI_TASK


class AiTaskRepairFlow(_AiTaskSteps, RepairsFlow):
    def __init__(self, entry_id):
        self._entry_id = entry_id

    @property
    def _entry(self):
        return self.hass.config_entries.async_get_entry(self._entry_id)

    async def async_step_init(self, user_input=None):
        # HA starts a fix flow with the issue's data as input, so the form lives in its own step.
        return await self.async_step_choose()

    async def async_step_choose(self, user_input=None):
        return await self._choose(user_input, self._entry.options.get(CONF_AI_TASK), "choose")

    async def _back(self):
        return await self.async_step_choose()

    def _finish(self, choice):
        if not choice:
            return self.async_abort(reason="no_choice")  # The notice stays.
        # Saving reloads the entry, which re-checks the notice.
        self.hass.config_entries.async_update_entry(self._entry, options=self._options(self._entry.options, choice))
        return self.async_create_entry(data={})


class HouseholdRepairFlow(RepairsFlow):
    """Let an assistant read the household knowledge: switch it from Assist to Assist with household knowledge."""

    def __init__(self, target, assistant):
        self._target, self._assistant = target, assistant

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        from .changeset import read, write
        from .household import API_ID, update_issues
        if user_input is not None:
            current = await read(self.hass, "assistant_api", self._target)
            if current is None:
                return self.async_abort(reason="assistant_gone")
            if API_ID not in current:
                await write(self.hass, "assistant_api", self._target, [API_ID if a == "assist" else a for a in current])
            update_issues(self.hass)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", description_placeholders={"assistant": self._assistant})


async def async_create_fix_flow(hass, issue_id, data):
    from .household import ISSUE_PREFIX
    if issue_id.startswith(ISSUE_PREFIX):
        return HouseholdRepairFlow(data["target"], data["assistant"])
    return AiTaskRepairFlow(data["entry_id"])
