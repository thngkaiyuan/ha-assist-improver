"""Home Assistant's own tools for the proposer and reviewer (DETAILS P7.6.14): every tool its Home Assistant API
(llm.LLM_API_HOME_ASSISTANT, admin only) offers that Home Assistant ships itself and marks read-only, as the improver's
own user (service_user.py), with no list of ours to keep: one Home Assistant adds reaches the roles as it ships. They
read the live home. Never a tool that acts, never a custom integration's, and never the Assist API: under an AI Task's
context it writes an exposure option into every entity."""

import probatio
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from . import touchpoints
from .const import DOMAIN

def _built_in(tool):
    """Whether Home Assistant itself ships a tool (a custom integration's is never trusted, whatever it's called or
    marked)."""
    while isinstance(tool, llm.NamespacedTool):
        tool = tool.tool
    return type(tool).__module__.startswith(("homeassistant.components.", "homeassistant.helpers."))


def kept(tool):
    """Whether a role gets one of Home Assistant's tools: Home Assistant ships it and marks it read-only. Its marks
    default to acting, so a tool it doesn't mark read-only acts, and is never kept."""
    return _built_in(tool) and bool(tool.annotations and tool.annotations.read_only)


async def tools(hass, user_id):
    """The tools kept (kept), each called as the improver's user; none without that user (service_user.async_ensure)
    or Home Assistant's llm integration, or when building them fails (reported: touchpoints)."""
    if not user_id:
        return []
    with touchpoints.guarded(hass, "llm_tools"):
        context = llm.LLMContext(platform=DOMAIN, context=Context(user_id=user_id), language=hass.config.language,
                                 assistant=None, device_id=None)
        try:
            instance = await llm.async_get_api(hass, llm.LLM_API_HOME_ASSISTANT, context)
        except HomeAssistantError:  # No llm integration, or the user is gone: the role goes on without them.
            return []
        return [AsUser(tool, context) for tool in instance.tools if kept(tool)]
    return []


class AsUser(llm.Tool):
    """One of Home Assistant's tools, called with the improver user's context whatever context the role's call comes
    with. Like the improver's own tools (exploration.RoleTool), anything it raises but an argument error reaches the
    model as a HomeAssistantError, so it never ends the role's generation."""

    def __init__(self, tool, context):
        self.tool, self.context = tool, context
        self.name, self.description, self.parameters = tool.name, tool.description, tool.parameters
        self.annotations, self.integration = tool.annotations, tool.integration

    def __deepcopy__(self, memo):
        return self

    async def async_call(self, hass, tool_input, llm_context):
        self.parameters(tool_input.tool_args)
        try:
            return await self.tool.async_call(hass, tool_input, self.context)
        except (HomeAssistantError, probatio.Invalid):
            raise
        except Exception as err:  # noqa: BLE001 - the model hears it; its generation goes on.
            raise HomeAssistantError(f"{type(err).__name__}: {err}") from err
