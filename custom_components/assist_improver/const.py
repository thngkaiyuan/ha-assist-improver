"""Recording, case-audit and bounded replay pilot constants."""

DOMAIN = "assist_improver"
NAME = "Assist Improver"
VERSION = "0.5.0"
SUPPORTED_CORE = "2026.10.2"
STORAGE_VERSION = 1
CONF_AI_TASK = "ai_task_entity_id"
CONF_ACCEPTED_WEAK = "ai_task_accepted_weak"  # Entity the user chose despite warnings.
SAVE_DELAY = 1
ATTRIBUTES = {
    "friendly_name", "supported_features", "device_class", "unit_of_measurement",
    "temperature", "current_temperature", "target_temp_high", "target_temp_low",
    "min_temp", "max_temp", "hvac_modes", "hvac_action", "preset_modes", "preset_mode",
    "fan_modes", "fan_mode", "percentage", "percentage_step", "options",
    "brightness", "color_temp_kelvin", "min_color_temp_kelvin", "max_color_temp_kelvin",
    "color_mode", "supported_color_modes", "rgb_color", "hs_color", "effect", "effect_list",
    "current_position", "current_tilt_position", "assumed_state", "attribution",
}
PROVIDER_OPTIONS = {
    "prompt", "llm_hass_api", "model", "chat_model", "max_tokens", "temperature",
    "top_p", "thinking_budget", "thinking_effort", "prompt_caching", "code_execution",
    "tool_search", "web_search", "web_fetch", "recommended",
    "web_search_max_uses", "web_fetch_max_uses", "user_location",
    "city", "region", "country", "timezone",
}
