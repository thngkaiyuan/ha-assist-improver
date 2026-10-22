"""Normal HA installation; single local recorder, no credentials.

Setup and Configure also choose the AI Task entity the improver's own roles
call. One list offers a new recommended entry (created through the provider's
own flow) and the household's existing AI Tasks; a weak choice is confirmed.
"""

import probatio
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from . import ai_setup
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, DOMAIN, NAME

CREATE = "create_recommended"  # The list's value for "create a new recommended one".
USE_ANYWAY = "use_anyway"


class _AiTaskSteps:
    """Shared by first setup, Configure and the Repairs fix: choose, create, then confirm warnings."""
    _choice = None
    _accepted = False

    def _options(self, base, choice):
        """Options with the choice; remembers an accepted weak choice so Repairs stops asking."""
        options = {k: v for k, v in base.items() if k not in (CONF_AI_TASK, CONF_ACCEPTED_WEAK)}
        if choice:
            options[CONF_AI_TASK] = choice
            if self._accepted:
                options[CONF_ACCEPTED_WEAK] = choice
        return options

    def _choices(self, current):
        """List options and the default: keep a good current choice, else preselect creating the recommended one."""
        can_create = bool(ai_setup.anthropic_entries(self.hass))
        options = []
        if can_create:
            options.append(SelectOptionDict(value=CREATE, label=f"New: {ai_setup.RECOMMENDED_NAME} AI Task with "
                                                               f"Claude Opus 5.5 (recommended)"))
        for entity_id in ai_setup.ai_task_entities(self.hass):
            state = self.hass.states.get(entity_id)
            assessment = ai_setup.assess(self.hass, entity_id)
            label = state.attributes.get("friendly_name", entity_id) if state else entity_id
            if assessment.get("model"):
                label += f" ({assessment['model']})"
            if not assessment["ok"]:
                label += ", not recommended"
            options.append(SelectOptionDict(value=entity_id, label=label))
        current = current or ai_setup.suggested_entity(self.hass)
        if current and ai_setup.assess(self.hass, current)["ok"]:
            return options, current, "Your current AI Task is selected. You can switch to another one from the list."
        if can_create:
            return options, CREATE, ("The recommended option is selected for you. To use one of your own AI Tasks "
                                     "instead (not recommended), pick it from the list.")
        return options, current, "Pick the AI Task to use. Claude Opus 5.5 with a high output limit is recommended."

    def _choose_schema(self, options, default):
        selector = SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))
        return probatio.Schema({probatio.Optional(CONF_AI_TASK, **({"default": default} if default else {})): selector})

    def _placeholders(self, extra=None):
        found = ai_setup.ai_task_entities(self.hass)
        return {"found": ", ".join(found) if found else "none", "recommended": ai_setup.RECOMMENDED["chat_model"],
                "link": "/config/integrations/dashboard", **(extra or {})}

    async def _choose(self, user_input, current, step_id):
        errors, extra = {}, {}
        if user_input is not None:
            choice = user_input.get(CONF_AI_TASK)
            if choice == CREATE:
                entry = ai_setup.anthropic_entries(self.hass)[0]
                choice, reason = await ai_setup.create_recommended(self.hass, entry.entry_id)
                if choice is None:
                    errors["base"] = "create_failed"
                    extra["reason"] = reason
            if not errors:
                self._choice, self._accepted = choice, False
                # No AI Task yet still finishes setup: recording works, and Repairs reminds.
                if choice and not ai_setup.assess(self.hass, choice)["ok"]:
                    return await self.async_step_confirm()
                return self._finish(choice)
        options, default, guidance = self._choices(current)
        return self.async_show_form(step_id=step_id, data_schema=self._choose_schema(options, default), errors=errors,
                                    description_placeholders=self._placeholders({"reason": "", "guidance": guidance,
                                                                                 **extra}))

    async def async_step_confirm(self, user_input=None):
        assessment = ai_setup.assess(self.hass, self._choice)
        if user_input is not None:
            if user_input.get(USE_ANYWAY):
                self._accepted = True
                return self._finish(self._choice)
            return await self._back()
        return self.async_show_form(
            step_id="confirm", data_schema=probatio.Schema({probatio.Required(USE_ANYWAY, default=False): bool}),
            description_placeholders=self._placeholders({
                "entity": self._choice or "none",
                "warnings": "\n".join(f"- {w}" for w in assessment["warnings"])}))


class AssistantImproverConfigFlow(_AiTaskSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AssistantImproverOptionsFlow()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._choose(user_input, None, "user")

    async def _back(self):
        return await self.async_step_user()

    def _finish(self, choice):
        return self.async_create_entry(title=NAME, data={}, options=self._options({}, choice))


class AssistantImproverOptionsFlow(_AiTaskSteps, OptionsFlow):
    async def async_step_init(self, user_input=None):
        return await self._choose(user_input, self.config_entry.options.get(CONF_AI_TASK), "init")

    async def _back(self):
        return await self.async_step_init()

    def _finish(self, choice):
        return self.async_create_entry(data=self._options(self.config_entry.options, choice))
