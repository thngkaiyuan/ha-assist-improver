"""Repairs: fix the AI Task notice, or review an improvement, with the same steps as Configure."""

from homeassistant.components.repairs import RepairsFlow

from .config_flow import _AiTaskSteps
from .const import CONF_AI_TASK
from .improvements import ISSUE_PREFIX
from .review_flow import _ReviewSteps


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


class ImprovementRepairFlow(_ReviewSteps, RepairsFlow):
    """The improvement's review; the list keeps its Repairs item in sync, so the flow always ends by aborting."""

    def __init__(self, improvement_id):
        self._improvement_id = improvement_id

    async def async_step_init(self, user_input=None):
        return await self.async_step_review()


async def async_create_fix_flow(hass, issue_id, data):
    if issue_id.startswith(ISSUE_PREFIX):
        return ImprovementRepairFlow(data["improvement_id"])
    return AiTaskRepairFlow(data["entry_id"])
