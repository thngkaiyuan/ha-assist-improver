"""The "Sandbox mode" switch (practice mode in the code): sandboxes your own conversations with your regular assistant."""

from homeassistant.components import persistent_notification
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME, VERSION
from .practice import MODE_KEY


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PracticeModeSwitch(entry)])


def _notification_id(user_id):
    return f"assist_improver_practice_{user_id}"


class PracticeModeSwitch(SwitchEntity):
    """On for whoever turned it on: their Assist conversations use a simulated home. Others are unaffected."""

    _attr_has_entity_name = False
    _attr_name = "Sandbox mode"
    _attr_icon = "mdi:school-outline"

    def __init__(self, entry):
        self._attr_unique_id = f"{entry.entry_id}-practice-mode"
        # A service device gives Assist Improver a page where the switch shows as a toggle.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=NAME,
                                            entry_type=DeviceEntryType.SERVICE, sw_version=VERSION)
        self._notified = set()

    @property
    def mode(self):
        return self.hass.data[MODE_KEY]

    async def async_added_to_hass(self):
        self.mode.listeners.append(self._on_change)
        self.async_on_remove(lambda: self.mode.listeners.remove(self._on_change))
        self._on_change()  # Practice mode survives restarts; so does its reminder.

    @callback
    def _on_change(self):
        for user_id in set(self.mode.users) - self._notified:
            persistent_notification.async_create(
                self.hass, f"Sandbox mode is on for {self.mode.users[user_id]}. Their Assist conversations use a "
                           "simulated home, so nothing real changes. It stays on until they turn it off.",
                title="Assist Improver: sandbox mode", notification_id=_notification_id(user_id))
        for user_id in self._notified - set(self.mode.users):
            persistent_notification.async_dismiss(self.hass, _notification_id(user_id))
        self._notified = set(self.mode.users)
        self.async_write_ha_state()

    @property
    def is_on(self):
        return bool(self.mode.users)

    @property
    def extra_state_attributes(self):
        return {"practising": sorted(self.mode.users.values())}

    def _user_id(self):
        user_id = self._context.user_id if self._context else None
        if not user_id:
            raise HomeAssistantError("Sandbox mode is per person: turn it on while signed in to Home Assistant.")
        return user_id

    async def async_turn_on(self, **kwargs):
        user_id = self._user_id()
        user = await self.hass.auth.async_get_user(user_id)
        await self.mode.async_turn_on(user_id, user.name if user else "someone")

    async def async_turn_off(self, **kwargs):
        await self.mode.async_turn_off(self._user_id())
