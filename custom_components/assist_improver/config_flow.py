"""Normal HA installation; single local recorder, no credentials.

Setup (and the Repairs fix) choose the AI Task entity the improver's own roles
call. One list offers a new recommended entry (created through the provider's
own flow) and the household's existing AI Tasks; a weak choice is confirmed.
First setup also gives Triage and Write up a small model: an AI Task that has
one, or a new one beside a new recommended entry. Every other setting lives in
the panel; Configure links there.
"""

import probatio
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from . import ai_setup, settings
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, DOMAIN, NAME

CREATE = "create"  # List values "create:<config entry>:<recommended|default>" add a new AI Task.
USE_ANYWAY = "use_anyway"


class _AiTaskSteps:
    """Shared by first setup and the Repairs fix: choose, create, then confirm warnings."""
    _choice = None
    _accepted = False
    _small = None  # First setup: the small model for Triage and Write up.
    _first_setup = False

    def _options(self, base, choice):
        """Options with the choice; remembers an accepted weak choice so Repairs stops asking."""
        options = {k: v for k, v in base.items() if k not in (CONF_AI_TASK, CONF_ACCEPTED_WEAK)}
        for role in settings.SMALL if self._small else ():
            options[settings.ROLES[role]["key"]] = self._small
        if choice:
            options[CONF_AI_TASK] = choice
            if self._accepted:
                options[CONF_ACCEPTED_WEAK] = choice
        return options

    def _choices(self, current):
        """List options, the default and guidance (none when the choice is already the right one). Offers new AI
        Tasks only until a recommended one exists. Each option names its model first, in plain words."""
        existing = []
        for entity_id in ai_setup.ai_task_entities(self.hass):
            state = self.hass.states.get(entity_id)
            assessment = ai_setup.assess(self.hass, entity_id)
            name = state.attributes.get("friendly_name", entity_id) if state else entity_id
            model = settings.model_label(self.hass, entity_id)
            model = f"Claude {model}" if settings.CLAUDE.search(assessment.get("model") or "") else model
            label = f"{model} · {name}" if model != name else name
            if ai_setup.is_recommended(assessment):
                label += " · recommended"
            elif not assessment["ok"]:
                label += " · not recommended"
            existing.append((entity_id, assessment, label))
        recommended = [e for e, a, _ in existing if ai_setup.is_recommended(a)]
        creates = [] if recommended else ai_setup.create_options(self.hass)
        options = [SelectOptionDict(value=f"{CREATE}:{entry_id}:{kind}", label=label) for entry_id, kind, label in creates]
        options += [SelectOptionDict(value=e, label=label) for e, _, label in existing]
        current = current or ai_setup.suggested_entity(self.hass)
        if current and ai_setup.assess(self.hass, current)["ok"]:  # Never preselect creating over a working choice.
            return options, current, ""
        if recommended or (creates and creates[0][1] == "recommended"):
            return options, recommended[0] if recommended else options[0]["value"], ""
        if creates:
            return options, options[0]["value"], ("Claude Opus 5.5 gives the best results, through Anthropic or "
                                                  "OpenRouter.")
        if options:
            return options, current, "Claude Opus 5.5 is recommended."
        return options, None, "First add an AI provider, such as Anthropic, in Settings → Devices & services."

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
            choice, created_on = user_input.get(CONF_AI_TASK), None
            if choice and choice.startswith(f"{CREATE}:"):
                _, created_on, kind = choice.split(":", 2)
                choice, reason = await ai_setup.create_ai_task(self.hass, created_on, kind)
                if choice is None:
                    errors["base"] = "create_failed"
                    extra["reason"] = reason
            if not errors and self._first_setup and choice:
                await self._choose_small(created_on)
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

    async def _choose_small(self, created_on):
        """Triage and Write up get a small model: an AI Task that has one, or, beside a newly created entry, a new
        one on the same provider. Never stops setup: without one, Triage passes everything on to Analyze."""
        found = [e for e in ai_setup.ai_task_entities(self.hass) if settings.is_small(self.hass, e)]
        found.sort(key=lambda e: "haiku" not in (ai_setup.assess(self.hass, e).get("model") or ""))
        if found:
            self._small = found[0]
        elif created_on:
            self._small, _ = await settings.small_ai_task(self.hass, created_on)

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

    _first_setup = True

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._choose(user_input, None, "user")

    async def _back(self):
        return await self.async_step_user()

    def _finish(self, choice):
        return self.async_create_entry(title=NAME, data={}, options=self._options({}, choice))


class AssistantImproverOptionsFlow(OptionsFlow):
    """Configure: the settings live in the panel, where the whole pipeline is shown; this links there."""

    async def async_step_init(self, user_input=None):
        from .panel import URL_PATH
        return self.async_abort(reason="settings_in_panel",
                                description_placeholders={"link": f"/{URL_PATH}?view=settings"})
