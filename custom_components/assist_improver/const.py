"""Constants and setting keys."""

DOMAIN = "assist_improver"
NAME = "Assist Improver"
VERSION = "0.39.2"
# How replays run and are judged; changing it makes earlier baselines a different test (a new plan), not a resample.
REPLAY_ENGINE = "2026-10-24.5"  # .2: a fix commanding what the baseline also changed isn't unintended. .3: baselines
# are judged on the goal's devices. .4: a goal newly met counts, not only fewer turns. .5: controls the user would
# accept being set aren't unintended.
# Conversation providers whose replays are tested end to end; others replay with a "not yet tested" label.
TESTED_PROVIDERS = {"anthropic", "open_router"}
STORAGE_VERSION = 1
CONF_AI_TASK = "ai_task_entity_id"
CONF_ACCEPTED_WEAK = "ai_task_accepted_weak"  # Entity the user chose despite warnings.
# The coarse cost control: how many replays with a fix (counterfactuals) one goal instance may get in a cycle: a goal
# as it came up in one conversation (session and turn), since the same goal phrased differently must be fixed in
# every conversation where it came up. Each replays
# a conversation on the user's own assistant, so it's a multiplier on their normal assistant spend: 3 means
# testing costs about 3 times those conversations, plus each goal's one baseline replay.
CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL = "tests_per_goal", 3
# A goal's title once no fix was found for it (improvements.py): settled, like one with an improvement.
NO_FIX = "No fix found"
# A small model that checks the replay's scripted follow-ups still fit the replies around them; unset skips it.
CONF_CHECK_AI_TASK = "check_ai_task_entity_id"
# A small model that checks each session first; a session it's sure was smooth skips the analyzer. Unset analyses all.
CONF_TRIAGE_AI_TASK = "triage_ai_task_entity_id"
# Runs on their own: off (the default), daily or weekly (Sundays), at 03:00. Each run can cost money.
CONF_SCHEDULE = "schedule"
# Stages the household paused (gates): a run skips them and their backlog waits; later stages still work on theirs.
CONF_PAUSED = "paused_stages"
# Improvements are accepted (applied) as soon as they're written up, without waiting for the household. Off by default.
CONF_AUTO_ACCEPT = "auto_accept"
# On-demand improvements, the fast lane: Assist gets a tool to file a conversation when the user asks ("file an
# improvement"), and a filed conversation (or one sent in from the panel) gets a run of its own as soon as it ends. Full
# runs still take in every conversation. On for a new entry (config_flow); an older one without it reads as off.
CONF_ON_DEMAND = "on_demand"
# Privacy controls: recording on or off for the household (on by default), and the people whose switch is off (an
# admin sets each; settings.updated keeps the two in step).
CONF_RECORDING = "recording"
CONF_OPT_OUT = "recording_opt_out"
# Household knowledge: whether Assist reads the household's lines (household.py), for every assistant; on by default.
CONF_HOUSEHOLD = "household_knowledge"
CONF_DEVELOPER = "developer_services"  # Hidden: registers the developer services (__init__.py).
SAVE_DELAY = 1
ATTRIBUTES = {
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
