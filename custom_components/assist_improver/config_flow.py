"""Normal HA installation; single local recorder, no credentials.

Setup and Configure also choose the AI Task entity the improver's own roles
call. One list offers a new recommended entry (created through the provider's
own flow) and the household's existing AI Tasks; a weak choice is confirmed.
"""

import probatio
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (EntitySelector, EntitySelectorConfig, NumberSelector, NumberSelectorConfig,
                                            NumberSelectorMode, SelectOptionDict, SelectSelector, SelectSelectorConfig,
                                            SelectSelectorMode)

from . import ai_setup
from .const import (CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_CHECK_AI_TASK, CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL,
                    DOMAIN, NAME)
SETTINGS = (CONF_TESTS_PER_GOAL, CONF_CHECK_AI_TASK)  # The improver's settings beside the AI model, in Configure.

CREATE = "create"  # List values "create:<config entry>:<recommended|default>" add a new AI Task.
USE_ANYWAY = "use_anyway"


class _AiTaskSteps:
    """Shared by first setup, Configure and the Repairs fix: choose, create, then confirm warnings."""
    _choice = None
    _accepted = False
    _settings = {}
    _with_settings = False  # Configure shows all the improver's settings; first setup only the AI model.

    def _options(self, base, choice):
        """Options with the choice; remembers an accepted weak choice so Repairs stops asking."""
        options = {k: v for k, v in base.items() if k not in (CONF_AI_TASK, CONF_ACCEPTED_WEAK, *self._settings)
                   and not (self._settings and k == "tests_per_cycle")}  # The setting's name before 0.17.4.
        options.update({k: v for k, v in self._settings.items() if v not in (None, "")})
        if choice:
            options[CONF_AI_TASK] = choice
            if self._accepted:
                options[CONF_ACCEPTED_WEAK] = choice
        return options

    def _choices(self, current):
        """List options, the default and guidance. Offers new AI Tasks only until a recommended one exists."""
        existing = []
        for entity_id in ai_setup.ai_task_entities(self.hass):
            state = self.hass.states.get(entity_id)
            assessment = ai_setup.assess(self.hass, entity_id)
            label = state.attributes.get("friendly_name", entity_id) if state else entity_id
            if assessment.get("model"):
                label += f" ({assessment['model']})"
            if ai_setup.is_recommended(assessment):
                label += ", recommended"
            elif not assessment["ok"]:
                label += ", not recommended"
            existing.append((entity_id, assessment, label))
        recommended = [e for e, a, _ in existing if ai_setup.is_recommended(a)]
        creates = [] if recommended else ai_setup.create_options(self.hass)
        options = [SelectOptionDict(value=f"{CREATE}:{entry_id}:{kind}", label=label) for entry_id, kind, label in creates]
        options += [SelectOptionDict(value=e, label=label) for e, _, label in existing]
        current = current or ai_setup.suggested_entity(self.hass)
        if current and ai_setup.assess(self.hass, current)["ok"]:  # Never preselect creating over a working choice.
            return options, current, "Your current AI Task is selected. You can switch to another one from the list."
        if recommended or (creates and creates[0][1] == "recommended"):
            default = recommended[0] if recommended else options[0]["value"]
            return options, default, ("The recommended option is selected for you. To use one of your own AI Tasks "
                                      "instead (not recommended), pick it from the list.")
        if creates:
            return options, options[0]["value"], ("A new AI Task with your provider's default settings is selected. "
                                                  "It works, but Claude Opus 5.5 gives better results.")
        if options:
            return options, current, "Pick the AI Task to use. Claude Opus 5.5 is recommended."
        return options, None, ("No AI provider that can run AI Tasks is set up yet. Add Anthropic, OpenAI, Google "
                               "Gemini or OpenRouter in Settings → Devices & services → Add integration, then come "
                               "back here.")

    def _choose_schema(self, options, default):
        selector = SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))
        fields = {probatio.Optional(CONF_AI_TASK, **({"default": default} if default else {})): selector}
        if self._with_settings:
            current = self.config_entry.options
            fields[probatio.Optional(CONF_TESTS_PER_GOAL, default=current.get(
                CONF_TESTS_PER_GOAL, current.get("tests_per_cycle", DEFAULT_TESTS_PER_GOAL)))] = \
                NumberSelector(NumberSelectorConfig(min=1, max=20, step=1, mode=NumberSelectorMode.BOX))
            check = current.get(CONF_CHECK_AI_TASK)
            fields[probatio.Optional(CONF_CHECK_AI_TASK, **({"default": check} if check else {}))] = \
                EntitySelector(EntitySelectorConfig(domain="ai_task"))
        return probatio.Schema(fields)

    def _placeholders(self, extra=None):
        found = ai_setup.ai_task_entities(self.hass)
        return {"found": ", ".join(found) if found else "none", "recommended": ai_setup.RECOMMENDED["chat_model"],
                "link": "/config/integrations/dashboard", **(extra or {})}

    async def _choose(self, user_input, current, step_id):
        errors, extra = {}, {}
        if user_input is not None:
            choice = user_input.get(CONF_AI_TASK)
            self._settings = {k: user_input.get(k) for k in SETTINGS} if self._with_settings else {}
            if self._settings.get(CONF_TESTS_PER_GOAL) is not None:
                self._settings[CONF_TESTS_PER_GOAL] = int(self._settings[CONF_TESTS_PER_GOAL])
            if choice and choice.startswith(f"{CREATE}:"):
                _, entry_id, kind = choice.split(":", 2)
                choice, reason = await ai_setup.create_ai_task(self.hass, entry_id, kind)
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
    _with_settings = True

    async def async_step_init(self, user_input=None):
        return await self._choose(user_input, self.config_entry.options.get(CONF_AI_TASK), "init")

    async def _back(self):
        return await self.async_step_init()

    def _finish(self, choice):
        return self.async_create_entry(data=self._options(self.config_entry.options, choice))
