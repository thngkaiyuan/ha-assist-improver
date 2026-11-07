"""The demo's assistants, one per assistant entry: Assist can use them; they answer with a fixed line (no model).

Like a real assistant, each prepares Assist's tools and prompt (or the household's) for its request, so a recording of
it has them (demo.py seed builds its made-up conversations on one)."""

from homeassistant.components import conversation
from homeassistant.helpers import intent


async def async_setup_entry(hass, entry, async_add_entities):
    for sub in entry.subentries.values():
        if sub.subentry_type == "conversation":
            async_add_entities([DemoAssistant(entry, sub)], config_subentry_id=sub.subentry_id)


class DemoAssistant(conversation.ConversationEntity):
    @property
    def supported_languages(self):
        return "*"

    def __init__(self, entry, sub):
        self._attr_unique_id = f"{entry.entry_id}-{sub.subentry_id}"
        self._attr_name = sub.title
        self._data = sub.data

    async def _async_handle_message(self, user_input, chat_log):
        try:
            await chat_log.async_provide_llm_data(user_input.as_llm_context("demo_ai"), self._data.get("llm_hass_api"),
                                                  self._data.get("prompt"), user_input.extra_system_prompt)
        except conversation.ConverseError as err:
            return err.as_conversation_result()
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech("This is the demo home's assistant; there's no model behind it.")
        return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)
