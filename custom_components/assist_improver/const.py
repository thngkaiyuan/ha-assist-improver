"""Constants and setting keys."""

DOMAIN = "assist_improver"
NAME = "Assist Improver"
# The manifest's version (build_package.py keeps them equal), read where nothing can be awaited: a recording's stamp,
# the panel script's ?v=. Home Assistant's loader gives it only by an await.
VERSION = "0.40.16"
# Conversation providers whose replays are tested end to end; others replay with a "not yet tested" label.
TESTED_PROVIDERS = {"anthropic", "open_router"}
STORAGE_VERSION = 1
CONF_AI_TASK = "ai_task_entity_id"
CONF_ACCEPTED_WEAK = "ai_task_accepted_weak"  # Entity the user chose despite warnings.
# The coarse cost control: how many replays with a change (counterfactuals) a conversation gets each round (P0.9).
# Each replays the conversation on the user's own assistant, so it's a multiplier on their normal assistant spend: 3
# means testing costs about 3 times those conversations, plus each conversation's one replay without a change.
CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL = "tests_per_goal", 3
# A small model that checks whether a replay's replies follow the household's feedback; unset, the write-up model's.
CONF_CHECK_AI_TASK = "check_ai_task_entity_id"
# A small model that checks each session first; a session it's sure was smooth skips the analyzer. Unset analyses all.
CONF_TRIAGE_AI_TASK = "triage_ai_task_entity_id"
# Runs on their own: off (the default), daily or weekly (Sundays), at 03:00. Each run can cost money.
CONF_SCHEDULE = "schedule"
# Stages the household paused (gates): a run skips them and their backlog waits; later stages still work on theirs.
CONF_PAUSED = "paused_stages"
CONF_DEVELOPER = "developer_services"  # Hidden: registers the developer services (__init__.py).
SAVE_DELAY = 1
# The state attributes Home Assistant 2026.10's live context (GetLiveContext) shows Assist: interesting_attributes in
# homeassistant/components/homeassistant/llm.py.
LIVE_CONTEXT_ATTRIBUTES = frozenset({
    "temperature", "current_temperature", "temperature_unit", "brightness", "humidity", "unit_of_measurement",
    "device_class", "current_position", "percentage", "volume_level", "media_title", "media_artist",
    "media_album_name",
})
# The state attributes a recording keeps: those the replay engine and the analyzer read (names, what HA's intent
# matching checks, the modes, ranges and options the emulated devices and the analyzer's allowed states use, what a
# date/time helper holds), and the live context's. The improver's roles see them all on a device's card (digest.py).
ATTRIBUTES = LIVE_CONTEXT_ATTRIBUTES | {
    "friendly_name", "supported_features", "device_class", "unit_of_measurement",
    "temperature", "current_temperature", "target_temp_high", "target_temp_low",
    "min_temp", "max_temp", "hvac_modes", "hvac_action", "preset_modes", "preset_mode",
    "fan_modes", "fan_mode", "percentage", "percentage_step", "options",
    "brightness", "color_temp_kelvin", "min_color_temp_kelvin", "max_color_temp_kelvin",
    "color_mode", "supported_color_modes", "rgb_color", "hs_color", "effect", "effect_list",
    "current_position", "current_tilt_position", "assumed_state", "attribution",
    # input_datetime: what time triggers and Assist read from a date/time helper.
    "has_date", "has_time", "timestamp", "year", "month", "day", "hour", "minute", "second",
}
PROVIDER_OPTIONS = {
    "prompt", "llm_hass_api", "model", "chat_model", "max_tokens", "temperature",
    "top_p", "thinking_budget", "thinking_effort", "prompt_caching", "code_execution",
    "tool_search", "web_search", "web_fetch", "recommended",
    "web_search_max_uses", "web_fetch_max_uses", "user_location",
    "city", "region", "country", "timezone",
}
