"""Constants and setting keys."""

DOMAIN = "assist_improver"
NAME = "Assist Improver"
# The manifest's version (build_package.py keeps them equal), read where nothing can be awaited: a recording's stamp,
# the panel script's ?v=. Home Assistant's loader gives it only by an await.
VERSION = "0.40.31"
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
