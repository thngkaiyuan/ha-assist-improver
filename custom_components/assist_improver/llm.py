"""What the improver adds to Assist: the household knowledge, and the tool that files an improvement.

Home Assistant's llm integration gathers each integration's tools and prompt from its llm platform (this module) for
the Assist API, so every assistant on Assist gets both:
- the household's lines (household.py);
- a tool to file the conversation it's in, when the user asks ("file an improvement"); the improver takes it in as
  soon as it ends (pipeline.py). The improver's own AI roles never get it; a replay of a conversation that had it gets
  one that files nothing.
"""

import probatio
# Not exported by the conversation root; the only way a tool finds the conversation it's called in.
from homeassistant.components.conversation.chat_log import current_chat_log
from homeassistant.components.llm import LLMTools
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.util.hass_dict import HassKey

from . import household
from .const import DOMAIN
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

    async def async_call(self, hass, tool_input, llm_context):
        runtime, chat_log = loaded(hass), current_chat_log.get()
        user_id = getattr(getattr(llm_context, "context", None), "user_id", None)
        key = await runtime.recorder.file(chat_log.conversation_id, tool_input.tool_args.get("what_to_improve"),
                                          user_id) if runtime is not None and chat_log is not None else None
        if key is None:
            return llm.ToolResult(data={"filed": False,
                                        "why": "This conversation isn't being recorded, so it can't be filed."})
        return llm.ToolResult(data={"filed": True, "next": "The Assist Improver will study this conversation once it ends."})


# In a sandbox's own instance: the recorded Assist could file (sandbox.seed).
REPLAYED_FILING: HassKey[bool] = HassKey(f"{DOMAIN}_replayed_filing")


class ReplayedFiling(FileImprovement):
    """In a replay, the filing tool the recorded Assist had: it answers as filing does and files nothing. That the
    replay filed is what it shows (DETAILS P6.6.2)."""

    async def async_call(self, hass, tool_input, llm_context):
        return llm.ToolResult(data={"filed": True, "next": "The Assist Improver will study this conversation once it ends."})


@callback
def async_get_tools(hass, llm_context, api_id):
    """For the Assist API: the household's lines as its prompt, and the file_improvement tool while the improver is
    loaded. None when there's neither."""
    if api_id != llm.LLM_API_ASSIST:
        return None
    pipeline = getattr(loaded(hass), "pipeline", None)  # None while the improver starts, and in a sandbox.
    files = pipeline is not None and not role_active()
    tools = [FileImprovement()] if files else [ReplayedFiling()] if hass.data.get(REPLAYED_FILING) else []
    prompt = household.prompt(hass, llm_context)
    return LLMTools(tools=tools, prompt=prompt) if tools or prompt else None


def role_active():
    """Whether one of the improver's own AI roles is calling (they, and replays, never file anything). Imported here:
    a sandbox's worker reads this module for the household's lines and never loads the AI roles."""
    from .reflective import ROLE_ACTIVE
    return ROLE_ACTIVE.get()
