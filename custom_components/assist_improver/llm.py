"""What the improver adds to Assist: the household knowledge, and the tool for on-demand improvements.

Home Assistant's llm integration gathers each integration's tools and prompt from its llm platform (this module) for
the Assist API, so every assistant on Assist gets both:
- the household's lines (household.py), while the Household knowledge switch is on;
- with on-demand improvements on, a tool to file the conversation it's in, when the user asks ("file an improvement");
  the improver takes it in as soon as it ends (pipeline.py). The improver's own AI roles and replays never get it.
"""

import probatio
from homeassistant.components.llm import LLMTools
from homeassistant.core import callback
from homeassistant.helpers import llm

from . import household
from .const import CONF_ON_DEMAND, DOMAIN
from .runtime import loaded


class FileImprovement(llm.Tool):
    """Marks the conversation for the Assist Improver; nothing else changes."""

    name = f"{DOMAIN}__file_improvement"
    title = "File an improvement"
    description = ("File this conversation with the Assist Improver, which studies it once the conversation ends and "
                   "proposes changes so requests like these go better next time. Use it when the user asks to file an "
                   "improvement, or says this should be improved (for example: \"file an improvement\", \"that didn't "
                   "go well, file it\"). Give what the user wants improved in what_to_improve, in their words, when they "
                   "say it. Afterwards, tell the user it's filed.")
    parameters = probatio.Schema({probatio.Optional("what_to_improve"): str})
    annotations = llm.ToolAnnotations(destructive=False, idempotent=True, open_world=False)
    integration = DOMAIN
    improver_own = True  # It only writes the improver's own record: sandbox mode lets it run (practice.py).

    async def async_call(self, hass, tool_input, llm_context):
        from homeassistant.components.conversation.chat_log import current_chat_log
        runtime, chat_log = loaded(hass), current_chat_log.get()
        user_id = getattr(getattr(llm_context, "context", None), "user_id", None)
        key = runtime.recorder.file(chat_log.conversation_id, tool_input.tool_args.get("what_to_improve"), user_id) \
            if runtime is not None and chat_log is not None else None
        if key is None:
            return llm.ToolResult(data={"filed": False,
                                        "why": "This conversation isn't being recorded, so it can't be filed."})
        return llm.ToolResult(data={"filed": True, "next": "The Assist Improver will study this conversation once it ends."})


@callback
def async_get_tools(hass, llm_context, api_id):
    """For the Assist API: the household's lines as its prompt, and the file_improvement tool while on-demand
    improvements are on. None when there's neither."""
    if api_id != llm.LLM_API_ASSIST:
        return None
    pipeline = getattr(loaded(hass), "pipeline", None)  # None while the improver starts, and in a sandbox: no tool.
    files = pipeline is not None and pipeline.entry.options.get(CONF_ON_DEMAND) and not role_active()
    prompt = household.prompt(hass, llm_context)
    return LLMTools(tools=[FileImprovement()] if files else [], prompt=prompt) if files or prompt else None


def role_active():
    """Whether one of the improver's own AI roles is calling (they, and replays, never file anything). Imported here:
    a sandbox's worker reads this module for the household's lines and never loads the AI roles."""
    from .reflective import ROLE_ACTIVE
    return ROLE_ACTIVE.get()
