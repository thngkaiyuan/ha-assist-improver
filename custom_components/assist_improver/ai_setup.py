"""Guided choice of the AI Task entity the improver's roles call.

Reads the chosen entity's model from its device, as its integration names it,
warns when it or its settings look too weak for these roles, and can create a
new entry by driving that integration's own "Add AI Task" flow. Claude Opus 5.5
is recommended where the household can reach it (Anthropic, or OpenRouter
when its account lists the model); OpenAI and Google get an entry with their
own default settings. If a flow asks for anything unexpected, it stops and the
user is told why.
"""

import asyncio
import re

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from . import touchpoints

RECOMMENDED = {"chat_model": "claude-opus-5-5", "max_tokens": 16000, "effort": "high"}
RECOMMENDED_NAME = "Assist Improver"
CREATED_KEY = "assist_improver.created"  # The AI Tasks the improver set up in providers, removed when uninstalled.
RECOMMENDED_MODEL = re.compile(r"claude-opus-5[.-]5")
MIN_MAX_TOKENS = 8000
WEAK_EFFORTS = {"low"}
WEAK_MODEL_WORDS = ("haiku", "mini", "nano", "flash", "lite")
APPEAR_TIMEOUT = 10  # Seconds a provider gets to add a new AI Task's entity (it reloads to add it).
# Providers the improver can add an AI Task to, and how.
PROVIDERS = {"anthropic": ("Anthropic", "recommended"), "open_router": ("OpenRouter", "recommended"),
             "openai_conversation": ("OpenAI", "default"),
             "google_generative_ai_conversation": ("Google Gemini", "default")}
_PICK_RECOMMENDED = object()  # Answer with the list option naming Claude Opus 5.5.


def ai_task_entities(hass):
    return sorted(s.entity_id for s in hass.states.async_all("ai_task"))


def _provider_settings(hass, entity_id):
    entry = er.async_get(hass).async_get(entity_id)
    if not entry or not entry.config_entry_id:
        return None, None, None
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if not config_entry:
        return None, None, None
    subentry = config_entry.subentries.get(entry.config_subentry_id) if entry.config_subentry_id else None
    return config_entry.domain, config_entry, dict(subentry.data) if subentry else None


def is_recommended(assessment):
    return assessment["ok"] and bool(RECOMMENDED_MODEL.search(assessment.get("model") or ""))


def _device_model(hass, entity_id):
    """The model an AI Task runs, as its integration names it on the AI Task's device: its ID, else its name."""
    entry = er.async_get(hass).async_get(entity_id)
    device = dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
    return (device.model_id or device.model) if device else None


def assess(hass, entity_id):
    """What the improver can tell about an AI Task entity, and any warnings: its model (_device_model), and an
    Anthropic AI Task's effort and output limit where its settings set them."""
    if not entity_id:
        return {"entity_id": None, "ok": False, "warnings": ["No AI Task entity is chosen yet."]}
    if hass.states.get(entity_id) is None:
        return {"entity_id": entity_id, "ok": False, "warnings": [f"{entity_id} is not available."]}
    provider, _, data = _provider_settings(hass, entity_id)
    result = {"entity_id": entity_id, "provider": provider, "warnings": []}
    if provider == "open_router" and data is not None:  # OpenRouter names no model on its device: its settings do.
        result.update(model=data.get("model"), ok=True)
        if not RECOMMENDED_MODEL.search(result["model"] or ""):
            result["warnings"].append("It works, but Claude Opus 5.5 is recommended for the improver.")
        return result
    model = result["model"] = _device_model(hass, entity_id) if provider else None  # None: its provider is gone.
    if provider != "anthropic" or data is None:  # Effort and output limit differ by provider: only the model is judged.
        result["ok"] = True
        if not model:
            result["warnings"].append("The improver can't read this provider's settings. A strong model such as "
                                      "Claude Opus 5.5 with a high output limit is recommended.")
        elif any(word in model.lower() for word in WEAK_MODEL_WORDS):
            result["warnings"].append(f"It uses {model}, a small model. Claude Opus 5.5 is recommended for the "
                                      "improver.")
        elif not RECOMMENDED_MODEL.search(model):
            result["warnings"].append("It works, but Claude Opus 5.5 is recommended for the improver.")
        return result
    effort, max_tokens = data.get("effort"), data.get("max_tokens")
    result.update(effort=effort, max_tokens=max_tokens)
    if model and any(word in model.lower() for word in WEAK_MODEL_WORDS):
        result["warnings"].append(f"It uses {model}, a small model. Claude Opus 5.5 is recommended for the improver.")
    if effort in WEAK_EFFORTS:
        result["warnings"].append(f"Its effort is {effort}. High effort is recommended.")
    if isinstance(max_tokens, (int, float)) and max_tokens < MIN_MAX_TOKENS:
        result["warnings"].append(f"Its output limit is {int(max_tokens)} tokens, which can cut the improver's "
                                  f"answers short. {RECOMMENDED['max_tokens']} is recommended.")
    result["ok"] = not result["warnings"]
    if data.get("recommended", False):  # Said, not judged: what they stand for isn't public.
        result["uses_recommended_defaults"] = True
        result["warnings"].append("It uses Anthropic's recommended settings.")
    return result


def create_options(hass):
    """New-AI-Task choices for loaded providers: [(entry_id, kind, label)], recommended ones first."""
    options = []
    for domain, (name, kind) in PROVIDERS.items():
        for entry in hass.config_entries.async_entries(domain):
            if getattr(entry, "state", ConfigEntryState.LOADED) is not ConfigEntryState.LOADED:
                continue
            label = ("Claude Opus 5.5 · new · recommended" if domain == "anthropic"
                     else f"Claude Opus 5.5 via {name} · new · recommended" if kind == "recommended"
                     else f"{name}'s default model · new")
            options.append((entry.entry_id, kind, label))
    return sorted(options, key=lambda o: o[1] != "recommended")


def _answers(domain, kind):
    if domain == "open_router":
        return {"model": _PICK_RECOMMENDED}
    if kind == "recommended":
        return {"name": RECOMMENDED_NAME, "recommended": False, **RECOMMENDED}
    return {"name": RECOMMENDED_NAME, "recommended": True}  # The provider's own defaults.


def _pick_recommended(key):
    options = getattr(getattr(key, "config", None), "get", lambda *_: None)("options") or []
    for option in options:
        value = option["value"] if isinstance(option, dict) else option
        if RECOMMENDED_MODEL.search(str(value)):
            return value
    return None


async def create_ai_task(hass, config_entry_id, kind, model=None, name=None):
    """Drive the provider's own add-AI-Task flow, with the recommended settings (kind "recommended"), the provider's
    defaults ("default"), or a chosen model and name (anything else at the provider's defaults). Returns
    (entity_id, None) or (None, reason)."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    wanted = (_answers(entry.domain, kind) if model is None else
              {"name": name or RECOMMENDED_NAME, "recommended": False, "chat_model": model, "model": model})
    before = set(entry.subentries)
    manager = hass.config_entries.subentries
    result, answered = {}, {}
    try:
        result = await manager.async_init((config_entry_id, "ai_task_data"), context={"source": "user"})
        for _ in range(6):
            if result["type"] == "create_entry":
                break
            if result["type"] != "form" or result.get("errors"):
                return None, f"The provider's setup stopped: {result.get('reason') or result.get('errors')}"
            schema = result["data_schema"].schema if result.get("data_schema") else {}
            answer = {}
            for key, value in schema.items():
                name = str(key)
                if name not in wanted:
                    continue
                if wanted[name] is _PICK_RECOMMENDED:
                    picked = _pick_recommended(value)
                    if picked is None:
                        return None, "Claude Opus 5.5 isn't available through this provider account."
                    answer[name] = picked
                else:
                    answer[name] = wanted[name]
            answered.update(answer)
            result = await manager.async_configure(result["flow_id"], answer)
        else:
            return None, "The provider's setup took more steps than expected"
    except Exception as err:  # noqa: BLE001 - a provider's flow may raise anything; never left half-done silently.
        return None, f"The provider's setup failed ({type(err).__name__})"
    finally:
        if result.get("flow_id") and result.get("type") not in ("create_entry", "abort"):  # Stopped midway: closed.
            try:
                manager.async_abort(result["flow_id"])
            except UnknownFlow:  # Already closed.
                pass
    if result["type"] != "create_entry":
        return None, f"The provider's setup ended with {result['type']}"
    entry = hass.config_entries.async_get_entry(config_entry_id)
    created = [sid for sid, sub in entry.subentries.items()
               if sub.subentry_type == "ai_task_data" and sid not in before]
    await remember_created(hass, config_entry_id, created)
    if created:
        _read_back(hass, [entry.subentries[sid] for sid in created], wanted, answered, chosen=model is not None)
    if entity_id := await _appeared(hass, config_entry_id, created):
        return entity_id, None
    return None, "The entry was created, but its AI Task entity has not appeared yet; choose it after a moment"


def _read_back(hass, subentries, wanted, answered, chosen):
    """A new AI Task's settings against its flow's answers: one the flow no longer asks for, or didn't keep, is Home
    Assistant having changed it (reported; the AI Task is still used)."""
    meant = set(wanted) - {"name"}  # The name is the AI Task's title.
    if chosen:  # A chosen model, the rest at the provider's defaults: each provider names the model its own way.
        meant = {key for key in ("chat_model", "model") if key in answered} or {"chat_model"}
    meant |= set(answered) - {"name"}
    if lost := sorted(key for key in meant
                      if key not in answered or any(sub.data.get(key) != answered[key] for sub in subentries)):
        touchpoints.report(hass, "ai_task_flow", f"a new AI Task didn't keep {', '.join(lost)}")
    else:
        touchpoints.ok(hass, "ai_task_flow")


async def _appeared(hass, config_entry_id, created):
    """The new AI Task's entity once the provider has added it (it reloads to add it) and it has a state, or None
    after a while."""
    registry = er.async_get(hass)

    def found():
        return next((item.entity_id for item in er.async_entries_for_config_entry(registry, config_entry_id)
                     if item.domain == "ai_task" and item.config_subentry_id in created
                     and hass.states.get(item.entity_id) is not None), None)

    if (entity_id := found()) is not None:
        return entity_id
    added = asyncio.Event()

    @callback
    def changed(_event):
        if found() is not None:
            added.set()

    @callback
    def ai_task(data):
        return data["entity_id"].startswith("ai_task.")

    unsubscribers = [hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, changed),
                     hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=ai_task)]
    try:
        async with asyncio.timeout(APPEAR_TIMEOUT):
            await added.wait()
    except TimeoutError:
        pass
    finally:
        for unsubscribe in unsubscribers:
            unsubscribe()
    return found()


def _created(hass):
    return Store(hass, 1, CREATED_KEY, private=True, atomic_writes=True)


async def remember_created(hass, entry_id, subentry_ids):
    """Keep which AI Tasks the improver set up, so uninstalling removes them (and only them)."""
    store = _created(hass)
    saved = (await store.async_load()) or {"ai_tasks": []}
    known = {(t["entry_id"], t["subentry_id"]) for t in saved["ai_tasks"]}
    saved["ai_tasks"] += [{"entry_id": entry_id, "subentry_id": sid} for sid in subentry_ids if (entry_id, sid) not in known]
    await store.async_save(saved)


async def remove_created(hass):
    """Uninstalling: the AI Tasks the improver set up go too, only those it kept a record of (remember_created), still
    there. Any other AI Task stays, whatever its name. Returns how many it removed."""
    saved = (await _created(hass).async_load()) or {}
    removed = 0
    for task in saved.get("ai_tasks", []):
        entry = hass.config_entries.async_get_entry(task["entry_id"])
        sub = entry.subentries.get(task["subentry_id"]) if entry else None
        if sub is not None and sub.subentry_type == "ai_task_data":
            hass.config_entries.async_remove_subentry(entry, task["subentry_id"])
            removed += 1
    return removed
