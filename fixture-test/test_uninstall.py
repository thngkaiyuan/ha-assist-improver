"""Uninstalling leaves only the applied improvements behind: the AI Tasks the improver set up (those it kept a record
of) and everything it stored go; other AI Tasks, whatever their names, and other integrations' data and settings stay
as they are (the household's lines are left in a notification: test_spec_household.py)."""

import os
from types import MappingProxyType, SimpleNamespace

import pytest
from homeassistant import config_entries, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.assist_improver import ai_setup, async_remove_entry, household


def provider(*subentries):
    entry = ConfigEntry(domain="anthropic", data={}, title="Claude", source="user", version=1, minor_version=1,
                        options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                        state=ConfigEntryState.NOT_LOADED,
                        subentries_data=[{"subentry_type": kind, "data": data, "title": title, "unique_id": None,
                                          "subentry_id": sid} for sid, kind, title, data in subentries])
    return entry


@pytest.mark.asyncio
async def test_uninstalling_removes_what_the_improver_set_up_and_stored_and_nothing_else(hass):
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    for registry in (dr, er):
        await registry.async_load(hass, load_empty=True)
    claude = provider(("made", "ai_task_data", "Claude Opus 5.5", {}),  # Set up by the improver, recorded.
                      ("named", "ai_task_data", "Assist Improver (Haiku)", {}),  # The household's own, named like ours.
                      ("theirs", "ai_task_data", "My summaries", {}),  # The household's own.
                      ("chat", "conversation", "Claude conversation", {"prompt": "Hi.", "llm_hass_api": ["assist"]}))
    hass.config_entries._entries[claude.entry_id] = claude
    await ai_setup.remember_created(hass, claude.entry_id, ["made"])
    await ai_setup.remember_created(hass, "removed-provider", ["gone"])  # Its provider was removed since.
    await (await household.async_setup(hass)).save({"everyone": ["Doors close at 10."], "people": {}})
    storage = hass.config.path(".storage")
    os.makedirs(storage, exist_ok=True)
    for name in ("assist_improver.session_abc", "assist_improver.role_runs", "assist_improver.trace_day_20261025",
                 "someone_else.data"):
        open(os.path.join(storage, name), "w").write("{}")
    await hass.async_block_till_done()

    await async_remove_entry(hass, SimpleNamespace(options={}))
    await hass.async_block_till_done()

    assert set(claude.subentries) == {"named", "theirs", "chat"}  # Only the AI Tasks it recorded setting up are gone.
    assert claude.subentries["chat"].data == {"prompt": "Hi.", "llm_hass_api": ["assist"]}  # As it was.
    left = set(os.listdir(storage))
    assert "someone_else.data" in left and not any(n.startswith("assist_improver.") for n in left)
