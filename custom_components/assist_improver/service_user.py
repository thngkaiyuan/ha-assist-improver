"""The improver's own Home Assistant user (DETAILS P7.6.15): a system user named "Assist Improver", in the
administrators' group and local only, with no credentials, so no one can sign in as it. The proposer and reviewer use
Home Assistant's own read-only tools as it (ha_tools.py). Its ID is kept in the config entry's data."""

import logging

from homeassistant.auth.const import GROUP_ID_ADMIN

from . import touchpoints

NAME = "Assist Improver"
KEY = "service_user"  # Its user ID, in the config entry's data.
_LOGGER = logging.getLogger(__name__)


def user_id(entry):
    """The service user's ID, as the config entry keeps it; None while it keeps none."""
    return (getattr(entry, "data", None) or {}).get(KEY)


def _leftover(user, kept):
    """A system user of the improver's name that isn't the one the entry keeps (Home Assistant's settings can't
    delete a system user, so an install that left one behind would leave it for good)."""
    return user.system_generated and user.name == NAME and user.id != kept


async def async_ensure(hass, entry):
    """At setup: the service user, created when the entry keeps none (or the one it keeps is gone), and any leftover
    removed. None when Home Assistant's accounts can't make one (reported: touchpoints)."""
    kept, found = user_id(entry), None
    with touchpoints.guarded(hass, "system_users"):
        touchpoints.get(hass, "system_users"), touchpoints.get(hass, "system_user_create")
        for user in await hass.auth.async_get_users():
            if _leftover(user, kept):
                await hass.auth.async_remove_user(user)
            elif user.id == kept:
                found = user
        if found is None:
            found = await hass.auth.async_create_system_user(NAME, group_ids=[GROUP_ID_ADMIN], local_only=True)
    if found is not None and found.id != kept:
        hass.config_entries.async_update_entry(entry, data={**entry.data, KEY: found.id})
    return found


async def async_remove(hass, entry):
    """On uninstall: the service user goes, with any leftover. A failure is logged, never raised: uninstalling goes
    on."""
    kept = user_id(entry)
    try:
        for user in await hass.auth.async_get_users():
            if user.id == kept or _leftover(user, kept):
                await hass.auth.async_remove_user(user)
    except Exception:  # noqa: BLE001 - the rest of uninstalling runs whatever stops this.
        _LOGGER.exception("Couldn't remove the %s user", NAME)
