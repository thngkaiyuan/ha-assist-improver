"""Summary only; raw conversations require the explicit admin export action."""

async def async_get_config_entry_diagnostics(hass, entry):
    return entry.runtime_data.status()
