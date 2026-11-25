"""Home Assistant's own tools for the proposer and reviewer: Kai approved an "Assist Improver" system user that
gives the roles that propose and review Home Assistant's read-only admin tools, and (2026-11-25) every one of them:
"expose those tools to our system". The bundle holds every tool Home Assistant ships and marks read-only, run for real
here, whatever this Home Assistant offers: one it adds reaches the roles with no list to keep. One that acts, or a
custom integration's, never does."""

import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import homeassistant.components
import pytest
from homeassistant.auth import auth_manager_from_config
from homeassistant.const import EVENT_COMPONENT_LOADED, KEY_DATA_LOGGING
from homeassistant.core import Context
from homeassistant.helpers import intent, llm
from homeassistant.setup import async_setup_component

from custom_components.assist_improver import ha_tools
from custom_components.assist_improver.exploration import SessionAPI

# Every built-in integration with an llm platform, found rather than named: one that starts giving the Home Assistant
# API a tool shows here. The Supervisor's loads only where its library is installed.
PROVIDERS = sorted({path.parent.name for path in Path(homeassistant.components.__path__[0]).glob("*/llm.py")}
                   - {"hassio"})


async def home_assistant(hass, supervisor=False):
    from conftest import load_registries
    await load_registries(hass)
    hass.auth = await auth_manager_from_config(hass, [], [])
    hass.data[KEY_DATA_LOGGING] = hass.config.path("home-assistant.log")  # Its log file: a log reader's tool comes.
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    for component in PROVIDERS + (["hassio"] if supervisor else []):  # As Home Assistant loads their llm platforms.
        importlib.import_module(f"homeassistant.components.{component}.llm")  # Home Assistant skips one that fails.
        hass.config.components.add(component)
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": component})
    await hass.async_block_till_done()
    return await hass.auth.async_create_system_user("Assist Improver", group_ids=["system-admin"], local_only=True)


def context(user_id=None):
    return llm.LLMContext(platform="ai_task", context=Context(user_id=user_id), language="en", assistant=None,
                          device_id=None)


class Lookalike(llm.Tool):
    """A custom integration's tool, named and marked like Home Assistant's own."""
    name, description, integration = "history__get_history", "Looks like Home Assistant's.", "history"
    annotations = llm.ToolAnnotations(read_only=True)


class CustomActs(llm.Tool):
    name, description, integration = "custom__open_everything", "A custom integration's tool that acts.", "custom"
    annotations = llm.ToolAnnotations(read_only=False)


class OpensTheDoor(intent.IntentHandler):
    intent_type, description = "CustomOpenDoor", "A custom integration's intent: it acts."


def marked_read_only(name):
    """A custom integration's intent as one of Home Assistant's helper classes makes it, marked read-only by its
    maker."""
    return llm.IntentTool(name, OpensTheDoor(), integration="custom", annotations=llm.ToolAnnotations(read_only=True))


def with_a_custom_integrations_tools(monkeypatch):
    """Home Assistant's API offering a custom integration's tools besides its own, as one with an llm platform does:
    those it offers."""
    from homeassistant.components import llm as llm_component
    real = llm_component.async_get_tools
    custom = [Lookalike(), CustomActs(), marked_read_only("custom__open_door")]

    async def offered(hass, llm_context, api_id):
        found = await real(hass, llm_context, api_id)
        return llm_component.LLMTools(tools=[*found.tools, *custom], prompt=found.prompt)
    monkeypatch.setattr(llm_component, "async_get_tools", offered)
    return custom


@pytest.mark.parametrize("supervisor", [False, True])  # Its log reader is the Supervisor's there, the core's here.
@pytest.mark.asyncio
async def test_every_read_only_tool_home_assistant_ships_is_kept_and_none_that_acts_nor_a_custom_integrations(
        hass, supervisor, caplog, monkeypatch):
    if supervisor:  # Its tools load only where Home Assistant's Supervisor library is installed.
        pytest.importorskip("aiohasupervisor")
    user = await home_assistant(hass, supervisor)
    custom = with_a_custom_integrations_tools(monkeypatch)
    everything = await llm.async_get_api(hass, llm.LLM_API_HOME_ASSISTANT, context(user.id))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # No platform failed giving its tools.
    assert all(any(t is c for t in everything.tools) for c in custom)  # Offered beside Home Assistant's.
    shipped = [t for t in everything.tools if not any(t is c for c in custom)]
    reads = {t.name for t in shipped if t.annotations and t.annotations.read_only}
    acts = {t.name for t in shipped} - reads
    kept = {t.name for t in everything.tools if ha_tools.kept(t)}
    assert kept == reads and "homeassistant__render_template" in kept  # Every one, whatever this version adds.
    assert acts and not kept & acts  # Home Assistant offers tools that act: none is kept.
    assert not any(ha_tools.kept(t) for t in custom)
    assert supervisor == any(name.startswith("hassio__") for name in kept)  # Where the Supervisor runs, its too.


@pytest.mark.asyncio
async def test_the_roles_bundle_holds_every_read_only_tool_this_home_assistant_offers_run_as_the_improvers_user(hass):
    user = await home_assistant(hass)
    hass.states.async_set("light.fixture_lamp", "on")
    offered = await llm.async_get_api(hass, llm.LLM_API_HOME_ASSISTANT, context(user.id))
    instance = await SessionAPI(hass, tools=[], user_id=user.id).async_get_api_instance(context())
    theirs = {t.name for t in instance.tools if "__" in t.name}  # Only Home Assistant's are named domain__tool.
    assert theirs == {t.name for t in offered.tools if ha_tools.kept(t)}
    render = next(t for t in instance.tools if t.name == "homeassistant__render_template")
    result = await render.async_call(hass, llm.ToolInput(tool_name=render.name, tool_args={
        "template": "{{ states.light | count }} {{ states('light.fixture_lamp') }}"}), context())  # As an AI Task calls.
    assert "1 on" in str(getattr(result, "data", result))
    without = await SessionAPI(hass, tools=[]).async_get_api_instance(context())
    assert not [t for t in without.tools if "__" in t.name]  # No user: none of Home Assistant's.
    person = await hass.auth.async_create_system_user("Someone", group_ids=["system-users"])  # No administrator.
    assert await ha_tools.tools(hass, person.id) == []


def test_the_roles_instructions_name_no_tool_and_none_of_ours_is_named_like_home_assistants():
    """The proposer's and reviewer's instructions say where each kind of answer comes from, naming no tool: Home
    Assistant's are those named domain__tool, which read the live home (hypotheses/prompts.py SOURCES). So none of the
    improver's own may be named so."""
    from custom_components.assist_improver import analyzer, editor, stamps  # noqa: F401 - their tools, defined.
    from custom_components.assist_improver.exploration import RoleTool
    from custom_components.assist_improver.hypotheses import tools  # noqa: F401
    from custom_components.assist_improver.hypotheses.prompts import SOURCES

    def every(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from every(sub)
    names = {getattr(cls, "name", None) for cls in every(RoleTool)} - {None}
    assert {"read_turn", "list_areas", "submit_hypotheses", "test_idea", "submit_copy"} <= names
    assert not [name for name in names if "__" in name]
    assert "domain__tool" in SOURCES and not [name for name in names if name in SOURCES]
    assert SOURCES.count("__") == 1  # No tool of Home Assistant's named by hand.


def test_a_custom_integrations_tool_is_never_kept_whatever_its_name_and_marks():
    assert Lookalike.__module__ == __name__ and not ha_tools.kept(Lookalike())
    assert not ha_tools.kept(llm.NamespacedTool("history", SimpleNamespace(
        name="get_history", title=None, description="", parameters=None, integration="history",
        annotations=llm.ToolAnnotations(read_only=True))))
    assert not ha_tools.kept(marked_read_only("history__open_door"))  # Home Assistant's class, the custom's code.
    assert not ha_tools.kept(llm.NamespacedTool("history", marked_read_only("open_door")))


@pytest.mark.asyncio
async def test_a_failure_building_them_leaves_the_role_without_them_and_shows_the_notice(hass, monkeypatch):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.assist_improver.const import DOMAIN
    user = await home_assistant(hass)

    async def changed(*args, **kwargs):
        raise AttributeError("Home Assistant changed it")
    monkeypatch.setattr(llm, "async_get_api", changed)
    assert await ha_tools.tools(hass, user.id) == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, "update_ha_tools") is not None
