"""Guided choice of the AI Task entity the improver's roles call.

Reads the chosen entity's settings where its integration exposes them, warns
when they look too weak for these roles, and can create a recommended entry
by driving that integration's own "Add AI Task" flow. If the flow asks for
anything unexpected, it stops and the user is linked to HA's screen instead.
"""

import asyncio

from homeassistant.helpers import entity_registry as er

RECOMMENDED = {"chat_model": "claude-opus-5-5", "max_tokens": 16000, "thinking_effort": "high"}
RECOMMENDED_NAME = "Assist Improver"
MIN_MAX_TOKENS = 8000
WEAK_EFFORTS = {"low"}
WEAK_MODEL_WORDS = ("haiku", "mini", "nano", "flash", "lite")


def ai_task_entities(hass):
    return sorted(s.entity_id for s in hass.states.async_all("ai_task"))


def suggested_entity(hass):
    preferred = None
    try:
        from homeassistant.components.ai_task.const import DATA_PREFERENCES
        preferred = hass.data[DATA_PREFERENCES].gen_data_entity_id
    except (ImportError, KeyError, AttributeError):
        preferred = None
    entities = ai_task_entities(hass)
    return preferred if preferred in entities else (entities[0] if entities else None)


def _provider_settings(hass, entity_id):
    entry = er.async_get(hass).async_get(entity_id)
    if not entry or not entry.config_entry_id:
        return None, None, None
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if not config_entry:
        return None, None, None
    subentry = config_entry.subentries.get(entry.config_subentry_id) if entry.config_subentry_id else None
    return config_entry.domain, config_entry, dict(subentry.data) if subentry else None


def assess(hass, entity_id):
    """What the improver can tell about an AI Task entity, and any warnings."""
    if not entity_id:
        return {"entity_id": None, "ok": False, "warnings": ["No AI Task entity is chosen yet."]}
    if hass.states.get(entity_id) is None:
        return {"entity_id": entity_id, "ok": False, "warnings": [f"{entity_id} is not available."]}
    provider, _, data = _provider_settings(hass, entity_id)
    result = {"entity_id": entity_id, "provider": provider, "warnings": []}
    if provider != "anthropic" or data is None:
        result["warnings"].append("The improver can't read this provider's settings. A strong model such as "
                                  "Claude Opus 5.5 with a high output limit is recommended.")
        result["ok"] = True
        return result
    if data.get("recommended", False):
        try:
            from homeassistant.components.anthropic.const import DEFAULT
        except ImportError:
            DEFAULT = {}
        model, effort, max_tokens = DEFAULT.get("chat_model"), DEFAULT.get("thinking_effort"), DEFAULT.get("max_tokens")
        result["uses_recommended_defaults"] = True
    else:
        model, effort, max_tokens = data.get("chat_model"), data.get("thinking_effort"), data.get("max_tokens")
    result.update(model=model, effort=effort, max_tokens=max_tokens)
    if model and any(word in model.lower() for word in WEAK_MODEL_WORDS):
        result["warnings"].append(f"It uses {model}, a small model. Claude Opus 5.5 is recommended for the improver.")
    if effort in WEAK_EFFORTS:
        result["warnings"].append(f"Its effort is {effort}. High effort is recommended.")
    if isinstance(max_tokens, (int, float)) and max_tokens < MIN_MAX_TOKENS:
        result["warnings"].append(f"Its output limit is {int(max_tokens)} tokens, which can cut the improver's "
                                  f"answers short. {RECOMMENDED['max_tokens']} is recommended.")
    result["ok"] = not result["warnings"]
    return result


def anthropic_entries(hass):
    return [e for e in hass.config_entries.async_entries("anthropic")]


async def create_recommended(hass, config_entry_id):
    """Drive the provider's own add-AI-Task flow. Returns (entity_id, None) or (None, reason)."""
    manager = hass.config_entries.subentries
    wanted = {"name": RECOMMENDED_NAME, "recommended": False, **RECOMMENDED}
    try:
        result = await manager.async_init((config_entry_id, "ai_task_data"), context={"source": "user"})
        for _ in range(6):
            if result["type"] == "create_entry":
                break
            if result["type"] != "form" or result.get("errors"):
                return None, f"The provider's setup stopped: {result.get('reason') or result.get('errors')}"
            fields = {str(key): key for key in (result["data_schema"].schema if result.get("data_schema") else {})}
            answer = {name: wanted[name] for name in fields if name in wanted}
            result = await manager.async_configure(result["flow_id"], answer)
        else:
            return None, "The provider's setup took more steps than expected"
    except Exception as err:  # Never leave setup half-done silently.
        return None, f"The provider's setup failed ({type(err).__name__})"
    if result["type"] != "create_entry":
        return None, f"The provider's setup ended with {result['type']}"
    entry = hass.config_entries.async_get_entry(config_entry_id)
    created = [sid for sid, sub in entry.subentries.items()
               if sub.subentry_type == "ai_task_data" and sub.title == RECOMMENDED_NAME]
    registry = er.async_get(hass)
    for _ in range(20):  # The provider reloads to add the entity; give it a moment.
        for item in er.async_entries_for_config_entry(registry, config_entry_id):
            if item.domain == "ai_task" and item.config_subentry_id in created:
                return item.entity_id, None
        await asyncio.sleep(0.5)
    return None, "The entry was created, but its AI Task entity has not appeared yet; choose it after a moment"
