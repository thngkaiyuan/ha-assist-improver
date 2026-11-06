"""What the improver relies on in Home Assistant beyond its public API: the names, signatures and shapes below, each
loaded where it's used, so one that moved or changed stops only the feature it serves.

A break is never silent: it's logged as an error once, and Repairs shows one notice for its feature, saying the
improver needs an update for this Home Assistant version. A notice goes once a check of what broke passes again (a
missing name comes back only with an update, which restarts Home Assistant). In a replay's worker, a break stops the
replay as the machinery's fault, never as a verdict; the real instance reports it (worker.Worker.call).
fixture-test/test_touchpoints.py checks every touchpoint on the installed Home Assistant."""

from annotationlib import Format, get_annotations
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from enum import Enum
import importlib
import inspect
import logging
from typing import NamedTuple

from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class Touchpoint(NamedTuple):
    path: str  # module:attribute
    feature: str  # Whose Repairs notice names it (FEATURES).
    used_in: str
    needs: tuple[str, ...] = ()  # Its parameters, attributes, keys or enum values the improver relies on.


FEATURES = ("recording", "replays", "ai_models", "changes", "usage", "ha_tools")
ISSUES = tuple(f"update_{feature}" for feature in FEATURES)  # Each feature's Repairs notice (strings.json).
_CHAT_LOG = "homeassistant.components.conversation.chat_log"
_EXPOSE = ("homeassistant.components.homeassistant.exposed_entities:async_expose_entity",
           ("hass", "assistant", "entity_id", "should_expose"))
TOUCHPOINTS = {
    # Recording: how the recorder hears Assist (and what its events carry: recorder.CARRIES), and what a recording
    # keeps of the home. A replay's sandbox hears its turn's tool calls so (sandbox._tool_windows).
    "chat_log_subscribe": Touchpoint(f"{_CHAT_LOG}:async_subscribe_chat_logs", "recording",
                                     "recorder.py, sandbox.py", ("hass", "callback_func")),
    "chat_log_events": Touchpoint(f"{_CHAT_LOG}:ChatLogEventType", "recording", "recorder.py, sandbox.py",
                                  ("created", "updated", "content_added", "deleted")),
    "current_chat_log": Touchpoint(f"{_CHAT_LOG}:current_chat_log", "recording", "recorder.py, llm.py", ("get",)),
    "chat_log": Touchpoint("homeassistant.components.conversation:ChatLog", "recording", "recorder.py, sandbox.py",
                           ("conversation_id", "content", "llm_api", "llm_input_provided_index", "as_dict")),
    "pipeline_debug": Touchpoint("homeassistant.components.assist_pipeline.pipeline:KEY_ASSIST_PIPELINE",
                                 "recording", "recorder.py"),
    "pipeline_runs": Touchpoint("homeassistant.components.assist_pipeline.runtime:PipelineData", "recording",
                                "recorder.py", ("pipeline_debug",)),
    # Another integration's entities (scripts, automations, template entities), through its own entity platforms:
    # each core caller asks only for its own.
    "entity_platforms": Touchpoint("homeassistant.helpers.entity_platform:async_get_platforms", "recording",
                                   "snapshot.py", ("hass", "integration_name")),
    "automation_configs": Touchpoint("homeassistant.components.automation.entity:AutomationEntity", "recording",
                                     "snapshot.py", ("raw_config",)),
    "script_configs": Touchpoint("homeassistant.components.script.entity:ScriptEntity", "recording",
                                 "snapshot.py", ("raw_config",)),
    # The scenes' definitions, by the load Home Assistant's own scene reload runs: the helper, for another
    # integration's domain.
    "scene_configs": Touchpoint("homeassistant.helpers.reload:async_integration_yaml_config", "recording",
                                "snapshot.py", ("hass", "integration_name")),
    # Replays: Home Assistant's engine started in a worker, and today's home seeded in it.
    "loader": Touchpoint("homeassistant.loader:async_setup", "replays", "sandbox.py", ("hass",)),
    "base_functionality": Touchpoint("homeassistant.bootstrap:async_load_base_functionality", "replays",
                                     "sandbox.py", ("hass",)),
    "config_entries": Touchpoint("homeassistant.config_entries:ConfigEntries", "replays", "sandbox.py",
                                 ("hass", "hass_config", "_entries")),
    "config_entry": Touchpoint("homeassistant.config_entries:ConfigEntry", "replays", "sandbox.py",
                               ("created_at", "data", "disabled_by", "discovery_keys", "domain", "entry_id",
                                "minor_version", "modified_at", "options", "pref_disable_new_entities",
                                "pref_disable_polling", "source", "state", "subentries_data", "title", "unique_id",
                                "version")),
    "running_state": Touchpoint("homeassistant.core:HomeAssistant", "replays", "sandbox.py", ("set_state",)),
    "skip_pip": Touchpoint("homeassistant.core_config:Config", "replays", "sandbox.py", ("skip_pip",)),
    # Today's registries, written in Home Assistant's own storage form for it to load whole, and the states its own
    # integrations restore, in its storage folder; each storage helper's store there too, named for its domain, read
    # at home (replay._helpers) and written as it is.
    "entity_storage": Touchpoint("homeassistant.helpers.entity_registry:RegistryEntry", "replays", "sandbox.py",
                                 ("as_storage_fragment",)),
    "device_storage": Touchpoint("homeassistant.helpers.device_registry:DeviceEntry", "replays", "sandbox.py",
                                 ("as_storage_fragment",)),
    "restore_storage": Touchpoint("homeassistant.helpers.restore_state:StoredState", "replays", "sandbox.py",
                                  ("as_dict",)),
    "storage_folder": Touchpoint("homeassistant.helpers.storage:STORAGE_DIR", "replays", "replay.py, sandbox.py"),
    "sandbox_exposure": Touchpoint(_EXPOSE[0], "replays", "sandbox.py", _EXPOSE[1]),
    # Replaced on the instance: HA's timer manager's start_timer, and each turn's chat log's async_trace.
    "timers": Touchpoint("homeassistant.components.intent.const:TIMER_DATA", "replays", "sandbox.py"),
    "timer_start": Touchpoint("homeassistant.components.intent.timers:TimerManager.start_timer", "replays",
                              "sandbox.py", ("device_id", "conversation_command", "conversation_agent_id")),
    "timer_device": Touchpoint("homeassistant.components.intent.timers:TimerManager.is_timer_device", "replays",
                               "sandbox.py", ("device_id",)),
    # The person speaking, as the sandbox's stand-in for Home Assistant's accounts reports them.
    "auth_user": Touchpoint("homeassistant.auth.models:User", "replays", "replay.py, sandbox.py",
                            ("name", "is_admin", "groups", "permissions")),
    # How long after a request Home Assistant puts what a device reports down to it (a recording's turn ends then).
    "recent_context": Touchpoint("homeassistant.helpers.entity:CONTEXT_RECENT_TIME_SECONDS", "replays",
                                 "simulation.py"),
    "chat_log_trace": Touchpoint("homeassistant.components.conversation:ChatLog.async_trace", "replays",
                                 "sandbox.py", ("agent_details",)),
    # AI models: a provider's add-AI-Task flow keeping the settings the improver answers it with, by the names each
    # provider gives them (checked on each AI Task the improver sets up).
    "ai_task_flow": Touchpoint("homeassistant.config_entries:ConfigSubentryFlowManager", "ai_models", "ai_setup.py",
                               ("async_init", "async_configure", "async_abort")),
    # Changes: exposing a device to Assist, and the files Home Assistant's script, automation and scene editors keep
    # their items in.
    "exposure": Touchpoint(_EXPOSE[0], "changes", "changeset.py", _EXPOSE[1]),
    "script_config_path": Touchpoint("homeassistant.config:SCRIPT_CONFIG_PATH", "changes", "changes.py"),
    "automation_config_path": Touchpoint("homeassistant.config:AUTOMATION_CONFIG_PATH", "changes", "changes.py"),
    "scene_config_path": Touchpoint("homeassistant.config:SCENE_CONFIG_PATH", "changes", "changes.py"),
    # The lock a config entry's reload holds: an assistant's change waits on it for the reload its update starts.
    "entry_setup_lock": Touchpoint("homeassistant.config_entries:ConfigEntry", "changes", "changeset.py",
                                   ("setup_lock",)),
    # The schemas Home Assistant's script and scene integrations check an item with (its automation's is made from
    # public pieces, changes.automation_schema).
    "script_schema": Touchpoint("homeassistant.components.script.config:SCRIPT_ENTITY_SCHEMA", "changes",
                                "changes.py"),
    "scene_schema": Touchpoint("homeassistant.components.homeassistant.scene:PLATFORM_SCHEMA", "changes",
                               "changes.py"),
    # Usage: what a provider reports for the improver's own calls, and for a replay's turns on each turn's chat log
    # (chat_log_trace): a provider whose replays reported it before and reports none now stopped reporting it there.
    "conversation_trace": Touchpoint("homeassistant.components.conversation.trace:async_conversation_trace", "usage",
                                     "reflective.py"),
    "provider_usage": Touchpoint("homeassistant.components.conversation:ChatLog.async_trace", "usage", "simulate.py",
                                 ("agent_details",)),
    # Home Assistant's own tools for the proposer and reviewer: the improver's system user, which they run as (built-in
    # integrations make system users this way; it isn't documented for others).
    "system_users": Touchpoint("homeassistant.auth:AuthManager", "ha_tools", "service_user.py",
                               ("async_get_users", "async_create_system_user", "async_remove_user")),
    "system_user_create": Touchpoint("homeassistant.auth:AuthManager.async_create_system_user", "ha_tools",
                                     "service_user.py", ("name", "group_ids", "local_only")),
    # And what marks one of Home Assistant's tools read-only (ha_tools.kept).
    "llm_tools": Touchpoint("homeassistant.helpers.llm:ToolAnnotations", "ha_tools", "ha_tools.py", ("read_only",)),
}
# Not listed, as nothing at run time can tell they broke: a voice device's Assistant select, found by its translation
# key (recorder.PIPELINE_SELECT): a miss is still found in the voice assistant's own runs (recorder._pipeline_ran); and
# the worker's custom_components (worker.BOOT), where Home Assistant's loader finds the improver and a household's
# custom providers: a miss is a sandbox that can't set them up.
BROKEN: HassKey[dict[str, str]] = HassKey(f"{DOMAIN}_broken")  # Touchpoint -> why, while it's broken.


class Broken(Exception):
    """A touchpoint Home Assistant no longer has as the improver relies on it."""

    def __init__(self, name, reason):
        super().__init__(f"Home Assistant changed what Assist Improver relies on ({name}): {reason}")
        self.name, self.reason = name, reason


def resolve(name):
    """The touchpoint as Home Assistant has it now; Broken when it's missing or lacks what's relied on."""
    point = TOUCHPOINTS[name]
    module, _, attribute = point.path.partition(":")
    try:
        found = importlib.import_module(module)
        for part in attribute.split("."):
            found = getattr(found, part)
    except (ImportError, AttributeError):
        raise Broken(name, f"{point.path} is missing") from None
    if lacking := [part for part in point.needs if not _has(found, part)]:
        raise Broken(name, f"{point.path} has no {', '.join(lacking)}")
    return found


def _has(found, part):
    """Whether found still has part: a key, an enum value, an attribute (a dataclass field, one a class's __init__
    sets, or one it declares), or a parameter."""
    if isinstance(found, dict):
        return part in found
    if isinstance(found, type) and issubclass(found, Enum):
        return part in {member.value for member in found}
    if hasattr(found, part) or (is_dataclass(found) and part in {f.name for f in fields(found)}):
        return True
    if isinstance(found, type) and any(part in code.co_names for code in (
            getattr(vars(k).get("__init__"), "__code__", None) for k in found.__mro__) if code is not None):
        return True
    if isinstance(found, type) and any(part in get_annotations(k, format=Format.STRING) for k in found.__mro__):
        return True
    try:  # Annotations as written: evaluating them could fail on names imported only for type checking.
        return part in inspect.signature(found, annotation_format=Format.STRING).parameters
    except (TypeError, ValueError):
        return False


@contextmanager
def relied_on(name):
    """A use of a touchpoint: an AttributeError, ImportError or TypeError inside is Home Assistant having changed it
    (Broken)."""
    try:
        yield
    except (AttributeError, ImportError, TypeError) as err:
        raise Broken(name, f"{TOUCHPOINTS[name].path} failed with {type(err).__name__}: {err}") from err


def get(hass, name):
    """resolve(), with a break reported."""
    try:
        return resolve(name)
    except Broken as err:
        report(hass, name, err.reason)
        raise


@contextmanager
def guarded(hass, name):
    """A read through a touchpoint that falls back when it fails: the failure is reported (only its type: a message
    can carry what the household has), and what follows the block runs; a read that works clears it."""
    try:
        yield
    except Broken:  # A touchpoint get() found broken, and reported.
        pass
    except Exception as err:  # noqa: BLE001 - whatever stops the read, the caller falls back.
        report(hass, name, f"reading it failed with {type(err).__name__}")
    else:
        ok(hass, name)


def report(hass, name, reason):
    """A touchpoint broke: logged once, and its feature's Repairs notice shown."""
    broken, point = hass.data.setdefault(BROKEN, {}), TOUCHPOINTS[name]
    if name not in broken:
        _LOGGER.error("%s, which %s uses, stopped working on Home Assistant %s: %s. Assist Improver needs an update "
                      "for this Home Assistant version", name, point.used_in, CORE_VERSION, reason)
    broken[name] = reason
    ir.async_create_issue(hass, DOMAIN, f"update_{point.feature}", is_fixable=False,
                          severity=ir.IssueSeverity.ERROR, translation_key=f"update_{point.feature}")


def ok(hass, name):
    """A check of a touchpoint passed: it isn't broken any more, unless it's still missing; once nothing of its
    feature is, the feature's notice goes."""
    broken = hass.data.get(BROKEN) or {}
    if name not in broken:
        return
    try:
        resolve(name)
    except Broken:
        return
    del broken[name]
    feature = TOUCHPOINTS[name].feature
    if not any(TOUCHPOINTS[other].feature == feature for other in broken):
        ir.async_delete_issue(hass, DOMAIN, f"update_{feature}")
