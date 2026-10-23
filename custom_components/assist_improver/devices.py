"""Emulated device drivers for practice: what a device does when HA sends it a command.

HA's own logic decides which devices a request targets and which command each
gets; practice catches the command (for example light.turn_on with brightness
128) and applies its effect to the simulated state, the way the device would
report back. Commands without a model here are reported as done and flagged.
"""

ON_OFF = {"light", "switch", "fan", "input_boolean", "siren", "remote", "humidifier", "media_player",
          "water_heater", "automation"}
TOGGLE_OFF = {"off", "closed", "unlocked", "idle", "standby", "paused", "unknown"}
COLOUR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}
# A sleeping device (a parked car, say) shows as unknown; a command wakes it like any off device.
ASLEEP = {"off", "unknown", "unavailable"}
# Read-only queries; practice passes them to the real service. Everything else is emulated.
QUERY_PREFIXES = ("get_", "search_")


def is_query(service):
    return service.startswith(QUERY_PREFIXES)


def _brightness(attrs, data):
    if "brightness" in data:
        return int(data["brightness"])
    if "brightness_pct" in data:
        return round(float(data["brightness_pct"]) * 255 / 100)
    current = attrs.get("brightness") or 0
    if "brightness_step" in data:
        return max(0, min(255, current + int(data["brightness_step"])))
    if "brightness_step_pct" in data:
        return max(0, min(255, current + round(float(data["brightness_step_pct"]) * 255 / 100)))
    return None


def _light(service, attrs, data):
    if service == "turn_off":
        return "off", {k: v for k, v in attrs.items() if k not in ("brightness", "color_temp_kelvin", "rgb_color",
                                                                    "hs_color", "xy_color", "color_name")}
    brightness = _brightness(attrs, data)
    if brightness is not None:
        attrs["brightness"] = brightness
    # Like HA, a light ignores colour it can't show (brightness-only lights, say).
    modes = attrs.get("supported_color_modes")
    colour = modes is None or bool(set(modes) & COLOUR_MODES)
    white = modes is None or "color_temp" in modes or colour
    for key in ("rgb_color", "hs_color", "xy_color", "color_name"):
        if key in data and colour:
            attrs[key] = data[key]
    if "effect" in data:
        attrs["effect"] = data["effect"]
    if ("color_temp_kelvin" in data or "kelvin" in data) and white:
        attrs["color_temp_kelvin"] = data.get("color_temp_kelvin", data.get("kelvin"))
    return ("off" if brightness == 0 else "on"), attrs


def _climate(service, state, attrs, data):
    modes = [m for m in attrs.get("hvac_modes", []) if m != "off"]
    if service == "turn_off":
        return "off", attrs
    if service == "turn_on":
        return (state if state not in ASLEEP else (modes[0] if modes else None)), attrs
    if service == "set_hvac_mode":
        return data.get("hvac_mode"), attrs
    if service == "set_temperature":
        for key in ("temperature", "target_temp_high", "target_temp_low"):
            if key in data:
                attrs[key] = data[key]
        return data.get("hvac_mode", state), attrs
    key = {"set_fan_mode": "fan_mode", "set_preset_mode": "preset_mode", "set_humidity": "humidity",
           "set_swing_mode": "swing_mode"}.get(service)
    if key and key in data:
        attrs[key] = data[key]
        return state, attrs
    return None


def _media(service, state, attrs, data):
    level = attrs.get("volume_level") or 0
    effects = {"turn_on": "on", "turn_off": "off", "media_play": "playing", "media_pause": "paused",
               "media_stop": "idle", "media_play_pause": "paused" if state == "playing" else "playing"}
    if service in effects:
        return effects[service], attrs
    if service == "volume_set" and "volume_level" in data:
        attrs["volume_level"] = float(data["volume_level"])
    elif service in ("volume_up", "volume_down"):
        attrs["volume_level"] = round(max(0.0, min(1.0, level + (0.1 if service == "volume_up" else -0.1))), 2)
    elif service == "volume_mute" and "is_volume_muted" in data:
        attrs["is_volume_muted"] = bool(data["is_volume_muted"])
    elif service == "select_source" and "source" in data:
        attrs["source"] = data["source"]
    else:
        return None
    return state, attrs


def _fan(service, state, attrs, data):
    if service == "turn_off":
        return "off", attrs
    if "percentage" in data:
        attrs["percentage"] = int(data["percentage"])
    if "preset_mode" in data:
        attrs["preset_mode"] = data["preset_mode"]
    if service in ("increase_speed", "decrease_speed"):
        step = int(data.get("percentage_step") or attrs.get("percentage_step") or 10)
        attrs["percentage"] = max(0, min(100, (attrs.get("percentage") or 0) + (step if service == "increase_speed" else -step)))
    elif service == "oscillate" and "oscillating" in data:
        attrs["oscillating"] = bool(data["oscillating"])
        return state, attrs
    elif service == "set_direction" and "direction" in data:
        attrs["direction"] = data["direction"]
        return state, attrs
    elif service not in ("turn_on", "set_percentage", "set_preset_mode"):
        return None
    return ("off" if attrs.get("percentage") == 0 else "on"), attrs


def _cover_like(service, state, attrs, data, position_key):
    if service.startswith("open"):
        if position_key in attrs:
            attrs[position_key] = 100
        return "open", attrs
    if service.startswith("close"):
        if position_key in attrs:
            attrs[position_key] = 0
        return "closed", attrs
    if service.startswith("set_") and "position" in data:
        attrs[position_key] = int(data["position"])
        return ("closed" if attrs[position_key] == 0 else "open"), attrs
    if service.startswith("stop"):
        return state, attrs
    return None


def effect(domain, service, state, data):
    """The device's new (state, attributes) after a command, or None if it isn't modelled."""
    attrs = dict(state.attributes)
    if service == "toggle":
        service = "turn_on" if state.state in TOGGLE_OFF else "turn_off"
    if domain == "light" and service in ("turn_on", "turn_off"):
        if service == "turn_off" and state.state == "off":
            return "off", attrs  # Already off: nothing changes.
        return _light(service, attrs, data)
    if domain == "climate":
        return _climate(service, state.state, attrs, data)
    if domain == "media_player":
        return _media(service, state.state, attrs, data)
    if domain == "fan":
        return _fan(service, state.state, attrs, data)
    if domain == "cover":
        return _cover_like(service, state.state, attrs, data, "current_position")
    if domain == "valve":
        return _cover_like(service, state.state, attrs, data, "current_position")
    if domain == "lock" and service in ("lock", "unlock", "open"):
        return {"lock": "locked", "unlock": "unlocked", "open": "open"}[service], attrs
    if domain == "vacuum":
        target = {"start": "cleaning", "pause": "paused", "stop": "idle", "return_to_base": "returning"}.get(service)
        return (target, attrs) if target else None
    if domain in ("input_number", "number") and service == "set_value" and "value" in data:
        return str(data["value"]), attrs
    if domain in ("input_select", "select") and service == "select_option" and "option" in data:
        return data["option"], attrs
    if domain in ("humidifier", "water_heater") and service in ("set_humidity", "set_temperature", "set_mode",
                                                                "set_operation_mode"):
        for key in ("humidity", "temperature", "mode", "operation_mode"):
            if key in data:
                attrs[key] = data[key]
        return state.state, attrs
    if domain in ON_OFF and service in ("turn_on", "turn_off"):
        return ("on" if service == "turn_on" else "off"), attrs
    return None
