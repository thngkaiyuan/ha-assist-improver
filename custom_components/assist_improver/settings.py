"""The improver's settings as the panel shows them: which AI model each role uses, beside the other settings.

Each AI role reads its model from its own option, an AI Task entity; unset, it falls back as ROLES says (an
optional role is then off). Everything lives in the config entry's options; the panel is the one place to change
them (Configure links there). Changes to these settings apply live; the others reload the integration (__init__.py).

The recommended setup: Claude Opus 5.5 for the steps that need judgement, a small model (Claude Haiku, or the
provider's own small one) for Triage and Write up. AI Tasks that fit are reused; the rest are created through the
provider's own flow.
"""

import re

from homeassistant.config_entries import ConfigEntryState

from . import ai_setup
from .const import (CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_AUTO_ACCEPT, CONF_CHECK_AI_TASK, CONF_DEVELOPER,
                    CONF_ON_DEMAND, CONF_PAUSED, CONF_SCHEDULE, CONF_TESTS_PER_GOAL, CONF_TRIAGE_AI_TASK,
                    DEFAULT_TESTS_PER_GOAL)

# Large roles need judgement (a strong model); small ones a quick, cheap one.
ROLES = {
    "triage": {"key": CONF_TRIAGE_AI_TASK, "fallback": (), "size": "small"},
    "analyzer": {"key": "analyzer_ai_task_entity_id", "fallback": (CONF_AI_TASK,), "size": "large"},
    "generator": {"key": "generator_ai_task_entity_id", "fallback": (CONF_AI_TASK,), "size": "large"},
    "reviewer": {"key": "reviewer_ai_task_entity_id", "fallback": (CONF_AI_TASK,), "size": "large"},
    "followup_check": {"key": CONF_CHECK_AI_TASK, "fallback": (), "size": "small"},
    "editor": {"key": "editor_ai_task_entity_id", "fallback": (CONF_TRIAGE_AI_TASK, CONF_CHECK_AI_TASK, CONF_AI_TASK),
               "size": "small"},
}
OPTIONAL = {role for role, spec in ROLES.items() if not spec["fallback"]}  # Off while unset.
LIVE = {spec["key"] for spec in ROLES.values()} | {CONF_AI_TASK, CONF_ACCEPTED_WEAK, CONF_TESTS_PER_GOAL, CONF_SCHEDULE,
                                                   CONF_PAUSED, CONF_AUTO_ACCEPT, CONF_ON_DEMAND}  # Read when used.
GATES = ("triage", "analyze", "propose", "test", "write")  # Stages that can be paused, in order.
STAGE_ROLES = {"triage": ("triage",), "analyze": ("analyzer",), "propose": ("generator", "reviewer"),
               "test": ("followup_check",), "write": ("editor",)}  # The roles each stage calls.
TITLES = {"triage": "Triage", "analyzer": "Analyze", "generator": "Propose", "reviewer": "Review",
          "followup_check": "Simulate", "editor": "Write up"}  # As the panel names the steps.
LARGE = tuple(role for role, spec in ROLES.items() if spec["size"] == "large")
SMALL = ("triage", "editor")  # The quick steps the recommended setup gives a small model.
SMALL_MODELS = (re.compile("haiku", re.I), re.compile("flash|mini|small|lite|nano", re.I))  # In order of preference.
SCHEDULES = ("off", "daily", "weekly")
MAX_TESTS_PER_GOAL = 20
CLAUDE = re.compile(r"claude-(?P<family>[a-z]+)-(?P<major>\d+)[.-](?P<minor>\d+)")
MODEL_KEYS = ("chat_model", "model")  # Where providers' add-AI-Task flows ask for the model.


class SettingsError(ValueError):
    """A setting the panel can't save, with the reason to show."""


def role_model(entry, role):
    """The AI Task entity a role calls, or None when the role is off."""
    spec = ROLES[role]
    return next((entry.options[key] for key in (spec["key"], *spec["fallback"]) if entry.options.get(key)), None)


def paused(entry):
    """The stages the household paused."""
    return {stage for stage in entry.options.get(CONF_PAUSED, []) if stage in GATES}


def problem(hass, entity_id):
    """Why a chosen AI Task can't run now: "missing" (gone from Home Assistant), "unavailable", or None."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return "missing" if state is None else "unavailable" if state.state == "unavailable" else None


def problems(hass, entry):
    """{role: problem} for each role that can't run now: its model is missing or unavailable, or a role that needs
    one has none ("none")."""
    found = {}
    for role in ROLES:
        entity_id = role_model(entry, role)
        if issue := ("none" if entity_id is None and role not in OPTIONAL else problem(hass, entity_id)):
            found[role] = issue
    return found


def listed(names):
    """Names as a sentence lists them: "A", "A and B", "A, B and C"."""
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def blocked_stages(hass, entry, only=None):
    """{stage: [role titles]} for the stages a run would use (just `only`, else the ones not paused) whose model
    can't run now."""
    issues, stages = problems(hass, entry), [only] if only else [g for g in GATES if g not in paused(entry)]
    return {stage: [TITLES[r] for r in STAGE_ROLES[stage] if r in issues] for stage in stages
            if any(r in issues for r in STAGE_ROLES[stage])}


def model_label(hass, entity_id):
    """A short name for an AI Task's model: "Opus 5.5" for a Claude model, else the AI Task's own name."""
    model = ai_setup.assess(hass, entity_id).get("model") or ""
    if found := CLAUDE.search(model):
        return f"{found['family'].title()} {found['major']}.{found['minor']}"
    state = hass.states.get(entity_id)
    name = state.attributes.get("friendly_name") if state else None
    if name and ": " in name:
        name = name.split(": ", 1)[1]  # OpenRouter names its AI Tasks "Maker: Model".
    return name or model or entity_id


def providers(hass):
    """Loaded integrations that can add an AI Task: [{entry_id, title, domain}]."""
    return [{"entry_id": e.entry_id, "title": e.title, "domain": e.domain}
            for e in hass.config_entries.async_entries()
            if getattr(e, "state", None) is ConfigEntryState.LOADED and "ai_task_data" in e.supported_subentry_types]


def view(hass, entry, user_id=None):
    """Everything the settings view shows: each role's model, the AI Tasks to choose from, and the rest."""
    from .practice import MODE_KEY
    tasks = []
    for entity_id in ai_setup.ai_task_entities(hass):
        assessment = ai_setup.assess(hass, entity_id)
        state = hass.states.get(entity_id)
        model = (assessment.get("model") or "").lower()
        tasks.append({"entity_id": entity_id, "label": model_label(hass, entity_id),
                      "title": state.attributes.get("friendly_name", entity_id) if state else entity_id,
                      "provider": assessment.get("provider"), "warnings": assessment["warnings"],
                      "strong": ai_setup.is_recommended(assessment),
                      "weak": not assessment["ok"] or any(w in model for w in ai_setup.WEAK_MODEL_WORDS),
                      "small": any(w in model for w in ai_setup.WEAK_MODEL_WORDS), "known": bool(model)})
    mode = hass.data.get(MODE_KEY)
    issues = problems(hass, entry)
    users = dict(getattr(mode, "users", {}))
    return {"roles": {role: {"entity_id": role_model(entry, role), "size": spec["size"], "optional": role in OPTIONAL,
                             "problem": issues.get(role)} for role, spec in ROLES.items()},
            "ai_tasks": tasks, "providers": providers(hass), "recommended": is_recommended_setup(hass, entry),
            "tests_per_goal": int(entry.options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL)),
            "schedule": entry.options.get(CONF_SCHEDULE, "off"), "paused": sorted(paused(entry)),
            "auto_accept": bool(entry.options.get(CONF_AUTO_ACCEPT, False)),
            "on_demand": bool(entry.options.get(CONF_ON_DEMAND, False)),
            "developer": bool(entry.options.get(CONF_DEVELOPER)),
            "practice": {"on": bool(user_id and user_id in users),
                         "others": sorted(name for uid, name in users.items() if uid != user_id)}}


def updated(hass, entry, *, role=None, entity_id=None, tests_per_goal=None, schedule=None, stage=None,
            pause=None, auto_accept=None, on_demand=None, developer_services=None):
    """The entry's options with one setting changed, checked first."""
    options = dict(entry.options)
    if role is not None:
        if role not in ROLES:
            raise SettingsError(f"Unknown role {role}")
        if entity_id is None:
            if role not in OPTIONAL:
                raise SettingsError("This step needs an AI model.")
            options.pop(ROLES[role]["key"], None)
        elif hass.states.get(entity_id) is None or not entity_id.startswith("ai_task."):
            raise SettingsError(f"{entity_id} isn't an AI Task in this Home Assistant.")
        else:
            options[ROLES[role]["key"]] = entity_id
    if tests_per_goal is not None:
        if not 1 <= int(tests_per_goal) <= MAX_TESTS_PER_GOAL:
            raise SettingsError(f"Tests per goal goes from 1 to {MAX_TESTS_PER_GOAL}.")
        options[CONF_TESTS_PER_GOAL] = int(tests_per_goal)
    if schedule is not None:
        if schedule not in SCHEDULES:
            raise SettingsError(f"Unknown schedule {schedule}")
        options[CONF_SCHEDULE] = schedule
    if stage is not None:
        if stage not in GATES:
            raise SettingsError(f"Unknown stage {stage}")
        stages = paused(entry) | {stage} if pause else paused(entry) - {stage}
        options[CONF_PAUSED] = [s for s in GATES if s in stages]
    if auto_accept is not None:
        options[CONF_AUTO_ACCEPT] = bool(auto_accept)
    if on_demand is not None:  # Only conversations the user files go to the pipeline (Assist's file_improvement).
        options[CONF_ON_DEMAND] = bool(on_demand)
    if developer_services is not None:  # Hidden (no control in the panel): set for debugging and evaluation rounds.
        options[CONF_DEVELOPER] = bool(developer_services)
    return options


def _options(selector):
    config = getattr(selector, "config", None) or {}
    return [{"value": o["value"], "label": o.get("label") or o["value"]} if isinstance(o, dict)
            else {"value": o, "label": o} for o in config.get("options") or []]


async def provider_models(hass, config_entry_id):
    """The models a provider offers for a new AI Task, read from its own add-AI-Task flow, which is then closed:
    {models: [{value, label}], default, named: whether the flow takes a name, suggested: {large, small}}."""
    manager = hass.config_entries.subentries
    result = await manager.async_init((config_entry_id, "ai_task_data"), context={"source": "user"})
    found = {"models": [], "default": None, "named": False}
    try:
        for _ in range(6):
            if result["type"] != "form" or result.get("errors"):
                break
            keys = {str(k): (k, v) for k, v in (result["data_schema"].schema if result.get("data_schema") else {}).items()}
            found["named"] = found["named"] or "name" in keys
            model_key = next((k for k in MODEL_KEYS if k in keys), None)
            if model_key:
                key, selector = keys[model_key]
                default = key.default() if callable(getattr(key, "default", None)) else None
                found.update(models=_options(selector), default=default if isinstance(default, str) else None)
                break
            answer = {k: v for k, v in {"name": ai_setup.RECOMMENDED_NAME, "recommended": False}.items() if k in keys}
            if not answer:
                break
            result = await manager.async_configure(result["flow_id"], answer)
    finally:
        if result.get("type") == "form" and result.get("flow_id"):
            try:
                manager.async_abort(result["flow_id"])
            except Exception:  # Already closed.
                pass
    found["suggested"] = {size: (pick(found["models"], size) or {}).get("value") for size in ("large", "small")}
    return found


def _version(model):
    found = CLAUDE.search(str(model))
    return (int(found["major"]), int(found["minor"])) if found else (0, 0)


def pick(models, size):
    """The model to suggest from a provider's list: Claude Opus 5.5 for a large role; for a small one, the newest
    Claude Haiku, else the provider's own small model (Flash, mini, ...). None when nothing fits."""
    for pattern in (ai_setup.RECOMMENDED_MODEL,) if size == "large" else SMALL_MODELS:
        fits = [m for m in models if pattern.search(m["value"]) or pattern.search(m["label"])]
        if fits:
            return max(fits, key=lambda m: _version(m["value"]))  # max keeps the first of equals: the list's order.
    return None


def is_small(hass, entity_id):
    """Whether an available AI Task runs a small model."""
    model = (ai_setup.assess(hass, entity_id).get("model") or "").lower()
    return problem(hass, entity_id) is None and any(w in model for w in ai_setup.WEAK_MODEL_WORDS)


def _existing(hass, entry):
    """The AI Tasks the recommended setup would reuse: (large, small), the ones in use first; None where none fits."""
    tasks = ai_setup.ai_task_entities(hass)
    in_use = [role_model(entry, r) for r in (*LARGE, *SMALL)]
    tasks = sorted(tasks, key=lambda e: (e not in in_use, "haiku" not in (ai_setup.assess(hass, e).get("model") or "")))
    large = next((e for e in tasks if ai_setup.is_recommended(ai_setup.assess(hass, e))), None)
    small = next((e for e in tasks if is_small(hass, e)), None)
    return large, small


def is_recommended_setup(hass, entry):
    """Whether every step already uses what the recommended setup would give it; None when it can't be offered (no
    Claude Opus 5.5 AI Task, and no provider that can add one)."""
    large, small = _existing(hass, entry)
    if large is None and provider_for(hass, opus=True) is None:
        return None
    return bool(large and small) and all(role_model(entry, r) == large for r in LARGE) and \
        all(role_model(entry, r) == small for r in SMALL)


def provider_for(hass, entity_id=None, opus=False):
    """The provider to add new AI Tasks to: the one behind entity_id when it can add them, else the first that can
    (Anthropic first, then OpenRouter, then the rest). opus: only one that can add Claude Opus 5.5."""
    order = list(ai_setup.PROVIDERS)
    found = sorted((p for p in providers(hass) if not opus or ai_setup.PROVIDERS.get(p["domain"], ("", ""))[1]
                    == "recommended"), key=lambda p: order.index(p["domain"]) if p["domain"] in order else len(order))
    _, config_entry, _ = ai_setup._provider_settings(hass, entity_id) if entity_id else (None, None, None)
    mine = next((p for p in found if config_entry and p["entry_id"] == config_entry.entry_id), None)
    return mine or (found[0] if found else None)


async def small_ai_task(hass, entry_id):
    """An AI Task with a small model on this provider, created through its own flow: (entity_id, None), or
    (None, why not)."""
    try:
        found = await provider_models(hass, entry_id)
    except Exception as err:  # The provider's flow refused.
        return None, f"The provider couldn't list its models ({err})."
    model = pick(found["models"], "small")
    if model is None:
        return None, "The provider doesn't offer a small model."
    return await ai_setup.create_ai_task(hass, entry_id, "chosen", model=model["value"],
                                         name=f"{ai_setup.RECOMMENDED_NAME} ({model['label']})")


async def recommended_options(hass, entry):
    """The entry's options with the recommended setup: (options, notes on anything it couldn't do). Reuses AI Tasks
    that fit, and creates the rest: Claude Opus 5.5 on a provider that offers it, the small model beside it."""
    large, small = _existing(hass, entry)
    notes = []
    if large is None and (provider := provider_for(hass, role_model(entry, "analyzer"), opus=True)):
        large, reason = await ai_setup.create_ai_task(hass, provider["entry_id"], "recommended")
        notes += [f"Claude Opus 5.5 couldn't be added: {reason}"] if reason else []
    if small is None and (provider := provider_for(hass, large or role_model(entry, "analyzer"))):
        small, reason = await small_ai_task(hass, provider["entry_id"])
        notes += [f"A small model couldn't be added: {reason}"] if reason else []
    large = large or role_model(entry, "analyzer")
    if large is None:
        raise SettingsError(" ".join(notes) or "Add an AI provider, such as Anthropic, in Settings → Devices & "
                            "services first.")
    options = {k: v for k, v in entry.options.items() if k not in {ROLES[r]["key"] for r in LARGE}}
    options[CONF_AI_TASK] = large
    for role in SMALL:
        if small:
            options[ROLES[role]["key"]] = small
    return options, notes
