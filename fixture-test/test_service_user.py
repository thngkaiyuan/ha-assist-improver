"""The improver's own user: Kai approved an "Assist Improver" system user that gives the roles that propose and
review Home Assistant's read-only admin tools; uninstall removes it. On Home Assistant's own accounts and config
entries."""

import pytest
from homeassistant.auth import auth_manager_from_config
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.helpers import issue_registry as ir

import custom_components.assist_improver as integration
from custom_components.assist_improver import household, service_user
from custom_components.assist_improver.const import DOMAIN


async def accounts(hass):
    from conftest import load_registries
    await load_registries(hass)  # Home Assistant's config entries, and the registries they need.
    hass.auth = await auth_manager_from_config(hass, [], [])


async def installed(hass, data=None):
    entry = ConfigEntry(domain=DOMAIN, title="Assist Improver", data=data or {}, options={}, source="user", version=1,
                        minor_version=1, unique_id=None, discovery_keys={}, subentries_data=None)
    await hass.config_entries.async_add(entry)
    return entry


async def ours(hass):
    return [u for u in await hass.auth.async_get_users() if u.name == service_user.NAME]


@pytest.mark.asyncio
async def test_the_service_user_is_made_once_and_kept_across_restarts(hass, monkeypatch):
    monkeypatch.setattr(ConfigEntries, "async_setup", lambda *args, **kwargs: _true())  # Added, not set up here.
    await accounts(hass)
    entry = await installed(hass)
    made = await service_user.async_ensure(hass, entry)
    assert made.system_generated and made.is_admin and made.local_only and not made.credentials
    assert entry.data[service_user.KEY] == made.id
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)  # Home Assistant stopping writes its accounts out...
    await hass.async_block_till_done()
    hass.auth = await auth_manager_from_config(hass, [], [])  # ...and starting again reads them back.
    again = await service_user.async_ensure(hass, entry)
    assert again.id == made.id and [u.id for u in await ours(hass)] == [made.id]


@pytest.mark.asyncio
async def test_a_reinstall_leaves_exactly_one(hass, monkeypatch):
    """An install whose user was left behind (Home Assistant's settings can't delete a system user): installing again
    removes it and makes one of its own."""
    monkeypatch.setattr(ConfigEntries, "async_setup", lambda *args, **kwargs: _true())
    await accounts(hass)
    left = await hass.auth.async_create_system_user(service_user.NAME, group_ids=["system-admin"], local_only=True)
    person = await hass.auth.async_create_user("Someone")
    entry = await installed(hass)
    made = await service_user.async_ensure(hass, entry)
    assert [u.id for u in await ours(hass)] == [made.id] != [left.id]
    assert await hass.auth.async_get_user(person.id) is not None  # Only the improver's own.


@pytest.mark.asyncio
async def test_uninstall_removes_it_even_when_a_later_step_fails(hass, monkeypatch):
    monkeypatch.setattr(ConfigEntries, "async_setup", lambda *args, **kwargs: _true())
    await accounts(hass)
    entry = await installed(hass)
    await service_user.async_ensure(hass, entry)

    async def failing(hass):
        raise RuntimeError("a later step failed")
    monkeypatch.setattr(household, "async_remove", failing)
    with pytest.raises(RuntimeError):
        await integration.async_remove_entry(hass, entry)
    assert await ours(hass) == []


@pytest.mark.asyncio
async def test_a_changed_accounts_call_shows_its_repairs_notice(hass, monkeypatch):
    """Home Assistant changing the call at call time (here, a parameter it no longer takes): no user, and the notice
    that the roles can't look things up in Home Assistant."""
    monkeypatch.setattr(ConfigEntries, "async_setup", lambda *args, **kwargs: _true())
    await accounts(hass)
    entry = await installed(hass)

    async def changed(*args, **kwargs):
        raise TypeError("unexpected keyword argument")
    monkeypatch.setattr(hass.auth, "async_create_system_user", changed)
    assert await service_user.async_ensure(hass, entry) is None and service_user.KEY not in entry.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, "update_ha_tools") is not None


async def _true():
    return True
