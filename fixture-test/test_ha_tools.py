"""Home Assistant's own tools for the proposer and reviewer: Kai approved an "Assist Improver" system user that
gives the roles that propose and review Home Assistant's read-only admin tools. The bundle holds only the pinned
read-only tools Home Assistant ships, run for real here; a tool Home Assistant adds or renames fails the first test
until it's pinned or left out with a reason (ha_tools.py)."""

import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import homeassistant.components
import pytest
from homeassistant.auth import auth_manager_from_config
from homeassistant.const import EVENT_COMPONENT_LOADED, KEY_DATA_LOGGING
from homeassistant.core import Context
from homeassistant.helpers import llm
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


@pytest.mark.parametrize("supervisor", [False, True])  # Its log reader is the Supervisor's there, the core's here.
@pytest.mark.asyncio
async def test_every_tool_of_home_assistants_api_is_pinned_left_out_or_never(hass, supervisor, caplog):
    if supervisor:  # Its tools load only where Home Assistant's Supervisor library is installed.
        pytest.importorskip("aiohasupervisor")
    user = await home_assistant(hass, supervisor)
    everything = await llm.async_get_api(hass, llm.LLM_API_HOME_ASSISTANT, context(user.id))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # No platform failed giving its tools.
    names = {t.name for t in everything.tools}
    other = "system_log__get_logs" if supervisor else "hassio__get_logs"
    assert names == (ha_tools.PINNED | set(ha_tools.LEFT_OUT) | ha_tools.NEVER) - {other}
    assert all(not t.annotations.read_only for t in everything.tools if t.name in ha_tools.NEVER)
    assert not ha_tools.PINNED & (set(ha_tools.LEFT_OUT) | ha_tools.NEVER)


@pytest.mark.asyncio
async def test_the_roles_bundle_holds_only_the_pinned_read_only_tools_run_as_the_improvers_user(hass):
    user = await home_assistant(hass)
    hass.states.async_set("light.fixture_lamp", "on")
    instance = await SessionAPI(hass, tools=[], user_id=user.id).async_get_api_instance(context())
    assert {t.name for t in instance.tools} == ha_tools.PINNED
    render = next(t for t in instance.tools if t.name == "homeassistant__render_template")
    result = await render.async_call(hass, llm.ToolInput(tool_name=render.name, tool_args={
        "template": "{{ states.light | count }} {{ states('light.fixture_lamp') }}"}), context())  # As an AI Task calls.
    assert "1 on" in str(getattr(result, "data", result))
    assert (await SessionAPI(hass, tools=[]).async_get_api_instance(context())).tools == []  # No user: none.
    person = await hass.auth.async_create_system_user("Someone", group_ids=["system-users"])  # No administrator.
    assert await ha_tools.tools(hass, person.id) == []


def test_a_custom_integrations_tool_is_never_kept_whatever_its_name_and_marks():
    class Lookalike(llm.Tool):
        name, description = "history__get_history", "Looks like Home Assistant's."
        annotations = llm.ToolAnnotations(read_only=True)
    assert Lookalike.__module__ == __name__ and not ha_tools.kept(Lookalike())
    assert not ha_tools.kept(llm.NamespacedTool("history", SimpleNamespace(
        name="get_history", title=None, description="", parameters=None, integration="history",
        annotations=llm.ToolAnnotations(read_only=True))))


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
