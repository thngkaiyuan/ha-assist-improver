"""Home Assistant's conventions: each action is drawn from the icons file, the panel's commands answer administrators
only and answer cleanly once the integration has unloaded, and every AI role's tools take arguments as Home Assistant
hands them over and convert to a provider's tool format."""

import asyncio
import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import probatio
import pytest
from homeassistant import config_entries, loader
from homeassistant.components.conversation import ToolResultContent
from homeassistant.components.conversation.chat_log import ChatLog
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import icon, llm

from ha_task import refused
from kit import Model, improver, tools_of_a_round  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import (DEVELOPER_SERVICES, SERVICES, async_setup_entry, async_unload_entry,
                                               ai_setup, devview, panel)
import custom_components.assist_improver as integration
from custom_components.assist_improver.const import DOMAIN
from custom_components.assist_improver.exploration import SessionAPI
from test_changeset import _ws
from test_milestone1 import fake_entry, recording_hass  # noqa: F401  (fixture)

STRINGS = "custom_components/assist_improver/strings.json"
EN = "custom_components/assist_improver/translations/en.json"
NOT_LOADED = ("error", 1, "not_loaded", "Assist Improver isn't loaded.")


def _strings():
    strings = json.load(open(STRINGS))
    assert strings == json.load(open(EN))  # English is strings.json, as HA builds it.
    return strings


def test_every_module_is_imported_with_the_integration_off_the_event_loop():
    """Home Assistant imports an integration and its platforms in its import executor; a module first imported inside
    a coroutine would be read from disk on the event loop. So each is imported at the top of one imported then."""
    import ast
    from pathlib import Path
    folder = Path(integration.__file__).parent
    modules = {".".join(p.relative_to(folder).with_suffix("").parts).removesuffix("__init__").rstrip("."): p
               for p in folder.rglob("*.py") if "__pycache__" not in p.parts}

    def imported_at_top(name):
        package = name if modules[name].name == "__init__.py" else name.rpartition(".")[0]
        found, statements = set(), list(ast.parse(modules[name].read_text()).body)
        while statements:
            node = statements.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # Run later, maybe on the event loop.
            statements += [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)]
            if isinstance(node, ast.ImportFrom) and node.level:
                base = package.split(".") if package else []
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
                found |= {target} | {f"{target}.{a.name}".lstrip(".") for a in node.names}
        parents = {".".join(m.split(".")[:n]) for m in found for n in range(len(m.split(".")))}
        return (found | parents) & set(modules)

    reached, waiting = set(), ["", "config_flow", "diagnostics", "llm", "repairs"]  # The package, its platforms.
    while waiting:
        if (name := waiting.pop()) not in reached:
            reached.add(name)
            waiting += imported_at_top(name)
    assert set(modules) - reached == set()


def test_each_flows_strings_have_the_steps_hassfest_requires():
    """hassfest, which HACS's catalog runs, requires a step mapping in each flow's strings, even one whose flow only
    aborts (the options flow, pointing to the panel): v0.40.1 failed it on 'options.step'."""
    strings = _strings()
    for flow in ("config", "options"):
        assert isinstance(strings[flow].get("step"), dict), flow


async def _improver_entry(hass):
    """A real config entry for the improver, added to a real ConfigEntries but not set up."""
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    hass.config_entries.async_setup = AsyncMock(return_value=True)
    entry = config_entries.ConfigEntry(data={}, discovery_keys=MappingProxyType({}), domain=DOMAIN, entry_id="improver",
                                       minor_version=1, options={}, source="user", subentries_data=None,
                                       title="Assist Improver", unique_id=None, version=1)
    await hass.config_entries.async_add(entry)
    return entry


@pytest.mark.asyncio
async def test_each_action_is_drawn_from_icons(recording_hass):
    hass = recording_hass
    await _improver_entry(hass)
    actions = await icon.async_get_icons(hass, "services", {DOMAIN})
    assert set(actions[DOMAIN]) == set(SERVICES + DEVELOPER_SERVICES)
    assert all(drawn["service"].startswith("mdi:") for drawn in actions[DOMAIN].values())


def _connection(sent):
    return SimpleNamespace(user=SimpleNamespace(id="alex", name="Alex", is_admin=True),
                           send_result=lambda i, r: sent.append(("ok", i, r)),
                           send_error=lambda i, code, text: sent.append(("error", i, code, text)))


# Commands that work without the improver loaded: household knowledge, HA's own assistants and providers, and the
# panel's script saver (it holds nothing of the improver).
INDEPENDENT = {panel.ws_household, panel.ws_household_edit, panel.ws_settings_models, panel.ws_editors,
               panel.ws_editors_saved}
MESSAGE = {"id": 1, "improvement_id": "imp1", "decision": "accept", "note": "", "confirmed": True, "force": False,
           "entry_id": "provider", "model": "m", "role": "main", "query": "x", "trace_id": "x"}


def _registered(monkeypatch):
    registered = []
    monkeypatch.setattr(panel.websocket_api, "async_register_command", lambda hass, handler: registered.append(handler))
    panel.register_commands(None)
    assert set(devview.COMMANDS) <= set(registered)
    return registered


def test_every_command_answers_administrators_only(monkeypatch):
    """Records are private (DETAILS P8.10.5): every command the panel registers refuses anyone who isn't an
    administrator, before it touches hass (here there is none)."""
    guest = SimpleNamespace(user=SimpleNamespace(id="guest", is_admin=False))
    let_in = []
    for handler in _registered(monkeypatch):
        try:
            handler(None, guest, {"id": 1})
        except Unauthorized:
            continue
        except Exception:  # noqa: BLE001 - It got past the check and ran into the missing hass.
            pass
        let_in.append(handler._ws_command)  # Two commands' handlers share a name (ws_conversations).
    assert let_in == []


@pytest.mark.asyncio
async def test_every_command_that_needs_the_improver_answers_not_loaded_after_unload(recording_hass, monkeypatch):
    hass = recording_hass
    added = []

    async def create_ai_task(*args, **kwargs):  # Settings must not add an AI Task for an improver that isn't there.
        added.append(args)
        return "ai_task.new", None
    monkeypatch.setattr(ai_setup, "create_ai_task", create_ai_task)
    commands = [handler for handler in _registered(monkeypatch) if handler not in INDEPENDENT]
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    await async_unload_entry(hass, entry)
    assert DOMAIN not in hass.data
    for handler in commands:
        sent = []
        await _ws(handler, hass, _connection(sent), dict(MESSAGE))
        assert sent == [NOT_LOADED], handler.__name__
    assert added == []


CONTEXT = llm.LLMContext(platform="test", context=None, language=None, assistant="ai_task", device_id=None)


def slips(tool, args):
    """Calls off the tool's schema, made from a call that kept it (or none): a field it doesn't have, a required field
    missing, a value of the wrong type."""
    keys = [str(key) for key in tool.parameters.schema]
    required = [str(key) for key in tool.parameters.schema if isinstance(key, probatio.Required)]
    out = {"a field it doesn't have": {**args, "not_a_field": "x"}}
    if required:
        out["a required field missing"] = {k: v for k, v in args.items() if k != required[0]}
    if keys:
        out["a value of the wrong type"] = {**args, (required or keys)[0]: {"wrong": "type"}}
    return out


async def handed_back(hass, api, tool, args):
    """What the model gets back for one call, streamed through Home Assistant's chat log as a provider streams it; or
    what happened instead."""
    chat_log = ChatLog(hass, "slips")
    chat_log.llm_api = api
    call = llm.ToolInput(tool_name=tool.name, tool_args=args)

    async def message():
        yield {"role": "assistant"}
        yield {"tool_calls": [call]}
    try:
        async with asyncio.timeout(5):
            return [content.result.data async for content in chat_log.async_add_delta_content_stream(
                "ai_task.slips", message()) if isinstance(content, ToolResultContent)][0]
    except TimeoutError:
        return "it never came back"
    except Exception as err:  # noqa: BLE001 - whatever ends the generation.
        return f"it ended the generation: {type(err).__name__}: {err}"[:200]


@pytest.mark.asyncio
async def test_every_role_tool_hands_arguments_off_its_schema_back_to_the_model_as_an_error(improver, monkeypatch):
    """Home Assistant hands a tool the model's arguments unchecked, and gives back to the model only an argument error
    (probatio.Invalid) or a HomeAssistantError: anything else a tool raises ends the model's whole generation. Every
    tool any role gets (the analyzer's, the proposer's with ask_reviewer and test_idea, the reviewer's with
    wait_for_draft and submit_review, the editor's), called through HA's chat log with a field it doesn't have, a
    required field missing, or a value of the wrong type, answers with the argument error."""
    use, kept = Model.use, {}

    async def watched(hass, tool, args=None, **kwargs):
        kept.setdefault(tool.name, args or {})
        return await use(hass, tool, args, **kwargs)
    monkeypatch.setattr(Model, "use", staticmethod(watched))
    wrong = {}
    for role, tools in (await tools_of_a_round(improver)).items():
        api = await SessionAPI(improver.hass, tools=list(tools.values())).async_get_api_instance(CONTEXT)
        for name, tool in tools.items():
            for slip, args in slips(tool, kept.get(name, {})).items():
                answer = await handed_back(improver.hass, api, tool, args)
                if not (isinstance(answer, dict) and "Invalid" in str(answer.get("error"))):
                    wrong[f"{role}: {name}, {slip}"] = str(answer)[:120]
    assert not wrong, "\n".join(f"{call}: {answer}" for call, answer in wrong.items())


@pytest.mark.asyncio
async def test_every_role_tool_converts_to_anthropics_tool_format(improver):
    """Every tool any role gets converts with HA's Anthropic integration (its entity's _format_tool): each input schema
    is an object, and the names are unique and of the kind the provider takes. (The harness checks this on every role
    call; this keeps the claim.)"""
    by_role = await tools_of_a_round(improver)
    assert {"journey_analyzer", "hypothesis_generator", "hypothesis_reviewer", "editor"} <= set(by_role)
    problems = {}
    for role, tools in by_role.items():
        api = await SessionAPI(improver.hass, tools=list(tools.values())).async_get_api_instance(CONTEXT)
        if found := refused(api.tools, api.custom_serializer):
            problems[role] = found
    assert problems == {}
