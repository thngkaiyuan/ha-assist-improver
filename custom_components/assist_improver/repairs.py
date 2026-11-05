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
        if (entry := self._entry) is None:  # The improver was removed while this was open.
            return self.async_abort(reason="not_set_up")
        return await self._choose(user_input, entry.options.get(CONF_AI_TASK), "choose")

    async def _back(self):
        return await self.async_step_choose()

    def _finish(self, choice):
        if (entry := self._entry) is None:
            return self.async_abort(reason="not_set_up")
        if not choice:
            return self.async_abort(reason="no_choice")  # The notice stays.
        # Saving reloads the entry, which re-checks the notice.
        self.hass.config_entries.async_update_entry(entry, options=self._options(entry.options, choice))
        return self.async_create_entry(data={})


async def async_create_fix_flow(hass, issue_id, data):
    return AiTaskRepairFlow(data["entry_id"])
