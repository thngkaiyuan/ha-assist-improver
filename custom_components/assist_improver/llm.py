"""Assist's tool for on-demand improvements: with the setting on, Assist can file the conversation it's in, when the
user asks ("file an improvement"), and the improver takes it in as soon as it ends (pipeline.py).

Home Assistant's llm integration gathers each integration's tools from its llm platform (this module) for the Assist
API, so every assistant on Assist, or on Assist with household knowledge, gets it. The improver's own AI roles and
replays never do.
"""

import probatio
from homeassistant.components.llm import LLMTools
from homeassistant.core import callback
from homeassistant.helpers import llm

from .const import CONF_ON_DEMAND, DOMAIN
from .reflective import ROLE_ACTIVE
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
            return {"filed": False, "why": "This conversation isn't being recorded, so it can't be filed."}
        return {"filed": True, "next": "The Assist Improver will study this conversation once it ends."}


@callback
def async_get_tools(hass, llm_context, api_id):
    """The file_improvement tool, for the Assist API while on-demand improvements are on."""
    runtime = loaded(hass)
    if api_id != llm.LLM_API_ASSIST or runtime is None or not runtime.pipeline.entry.options.get(CONF_ON_DEMAND):
        return None
    if ROLE_ACTIVE.get():  # The improver's own AI roles and replays never file anything.
        return None
    return LLMTools(tools=[FileImprovement()])
