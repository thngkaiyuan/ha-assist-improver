"""A state goal's target State (DETAILS P9.7.1): a state plus any settable attribute, and whether a device holds it.

A settable attribute is one a field of one of its domain's services sets, as Home Assistant describes them (the
domain's services.yaml), mapped to the attribute through the stand-in's table (standin.attribute_of). Valid values are
Home Assistant's: for the state, the entity's own options where it lists them (a thermostat's modes, a select's
options), else its domain's enum (CoverState and the like; on and off for a toggle); for an attribute, its options and
range as the entity shows them (its capability attributes), else as the field's selector gives them. One setting may
be given in any of its representations, compared by Home Assistant's own conversions (homeassistant.util.color): a
colour as hs, rgb, xy, rgbw, rgbww, a name or a colour temperature; a brightness as brightness or brightness_pct.
Numbers match within TOLERANCE of the setting's range (as the entity shows it, else as the field's selector gives it),
else of the value asked for. A temperature is the entity's own unit, as it shows it.
"""

from enum import StrEnum
import functools
import importlib
from pathlib import Path

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import color as color_util

from .standin import attribute_of, colour_modes, held

TOLERANCE = 0.05  # Of a setting's range: 5 points of a brightness's 0 to 100.
BRIGHTNESS = ("brightness", "brightness_pct")  # One setting: 0 to 255, or a percentage.


@functools.cache
def _services(domain):
    """{service: {field: spec}} as Home Assistant describes a domain's services (its services.yaml), a section's fields
    listed with the others; {} for a domain it doesn't describe."""
    try:
        from homeassistant.util.yaml import load_yaml_dict
        module = importlib.import_module(f"homeassistant.components.{domain}")
        found = load_yaml_dict(Path(module.__file__).parent / "services.yaml")
    except (ImportError, OSError, HomeAssistantError):
        return {}
    out = {}
    for service, described in found.items():
        if service.startswith(".") or not isinstance(described, dict):  # An anchor's holder, not a service.
            continue
        fields = {}
        for name, spec in (described.get("fields") or {}).items():
            fields.update((spec.get("fields") or {}) if "fields" in (spec or {}) else {name: spec or {}})
        out[service] = fields
    return out


def _sets_state(spec):
    """Whether a field sets the entity's state (its selector takes one of the entity's states: a thermostat's mode)."""
    chosen = (spec.get("selector") or {}).get("state")
    return isinstance(chosen, dict) and not chosen.get("attribute")


@functools.cache
def settable(domain):
    """{attribute: the field's spec}: each attribute a field of the domain's services sets (standin.attribute_of) that
    its entities hold (standin.held: a light's brightness, not its transition), the state aside."""
    out = {}
    try:
        for fields in _services(domain).values():
            for field, spec in fields.items():
                if not _sets_state(spec) and held(domain, name := attribute_of(domain, field)):
                    out.setdefault(name, spec)
    except (ImportError, ValueError):  # A domain with no one entity class: none.
        return {}
    return out


def _known(domain, name):
    """Whether a goal may name a setting: a settable attribute, or another representation of one (brightness_pct for
    brightness, a colour's name for its colour)."""
    return bool(representing(domain, name) & set(settable(domain)))


def shown(source, entity_id):
    """What an entity shows of itself: its state's attributes, with its registry entry's capability attributes."""
    attributes = ((source.get("states") or {}).get(entity_id) or {}).get("attributes") or {}
    capabilities = ((source.get("entities") or {}).get(entity_id) or {}).get("capabilities") or {}
    return {**attributes, **capabilities}


def _options(name, found):
    """The values an entity lists for a setting, by Home Assistant's naming of them (fan_mode: fan_modes; effect:
    effect_list; operation_mode: operation_list; mode: available_modes); None where it lists none."""
    stem = name.rsplit("_", 1)[0]
    for key in (f"{name}s", f"{name}_list", f"{stem}_list", f"available_{name}s"):
        if isinstance(found.get(key), list):
            return found[key]
    return None


@functools.cache
def _enum(domain):
    """A domain's own states: its StrEnum of them (CoverState, VacuumActivity), on and off for a toggle; None for
    neither."""
    from homeassistant.helpers.entity import ToggleEntity
    from .standin import entity_class
    package = f"homeassistant.components.{domain}"
    try:
        module = importlib.import_module(package)
    except ImportError:
        return None
    for value in vars(module).copy().values():
        if (isinstance(value, type) and issubclass(value, StrEnum) and value.__module__.startswith(package)
                and value.__name__.endswith(("State", "Activity"))):
            return frozenset(value)
    try:
        return frozenset((STATE_ON, STATE_OFF)) if issubclass(entity_class(domain), ToggleEntity) else None
    except (ImportError, ValueError):  # No one class its entities derive from.
        return None


def states(source, entity_id):
    """The states an entity can be in: its own options for its state where it lists them, else its domain's enum;
    None when neither tells."""
    domain, found = entity_id.split(".", 1)[0], shown(source, entity_id)
    for fields in _services(domain).values():
        for field, spec in fields.items():
            if _sets_state(spec) and (listed := _options(field, found)):
                return set(listed)
    return set(found) if (found := _enum(domain)) else None


def target(goal):
    """A state goal's target: its state and its settings ({attribute: value}, each named as its device shows it); a
    stored goal's brightness_pct is one of its settings."""
    domain = (goal.get("entity_id") or "").split(".", 1)[0]
    wanted = {attribute_of(domain, k): v for k, v in (goal.get("attributes") or {}).items()}
    if goal.get("brightness_pct") is not None:
        wanted.setdefault("brightness_pct", goal["brightness_pct"])
    return goal.get("state"), wanted


def representing(domain, name):
    """The attributes a device shows a setting in: a brightness in brightness, a colour in its colour mode's."""
    if name in BRIGHTNESS:
        return {"brightness"}
    if name in colour_modes() or name == "color_name":
        return {*colour_modes(), "color_mode"}
    return {attribute_of(domain, name)}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _range(domain, name, found):
    """A setting's (low, high): its min_ and max_ as the entity shows them (min_temp for a temperature, min and max
    for a number), else its field's selector's; None for neither."""
    pairs = [k[4:] for k in found if k.startswith("min_") and _number(found[k]) and _number(found.get(f"max_{k[4:]}"))]
    if found_for := max((k for k in pairs if k in name), key=len, default=None):
        return found[f"min_{found_for}"], found[f"max_{found_for}"]
    selector = (settable(domain).get(name) or {}).get("selector") or {}
    bounds = next((s for s in selector.values() if isinstance(s, dict) and _number(s.get("min"))), {})
    return (bounds["min"], bounds["max"]) if _number(bounds.get("max")) else None


def _close(asked, now, bounds):
    span = (bounds[1] - bounds[0]) if bounds else abs(asked)
    return abs(asked - now) <= TOLERANCE * span


def _percent(name, value):
    """A brightness as a percentage (Home Assistant's 0-255 as a light reports it)."""
    return value if name == "brightness_pct" else round(value * 100 / 255)


def _hs(name, value, found):
    """A colour as hue and saturation, by Home Assistant's conversions; None for none."""
    try:
        if name == "hs_color":
            return tuple(value)
        if name == "rgb_color":
            return color_util.color_RGB_to_hs(*value)
        if name == "xy_color":
            return color_util.color_xy_to_hs(*value)
        if name == "rgbw_color":
            return color_util.color_RGB_to_hs(*color_util.color_rgbw_to_rgb(*value))
        if name == "rgbww_color":
            from homeassistant.components.light import DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN
            return color_util.color_RGB_to_hs(*color_util.color_rgbww_to_rgb(
                *value, found.get("min_color_temp_kelvin") or DEFAULT_MIN_KELVIN,
                found.get("max_color_temp_kelvin") or DEFAULT_MAX_KELVIN))
        if name == "color_name":
            return color_util.color_RGB_to_hs(*color_util.color_name_to_rgb(value))
        if name == "color_temp_kelvin":
            return color_util.color_temperature_to_hs(value)
    except (TypeError, ValueError):
        return None
    return None


def _colour(attributes):
    """The colour a light shows: (attribute, value) of its colour mode, else the first colour it shows."""
    modes = {mode: name for name, mode in colour_modes().items()}
    name = modes.get(attributes.get("color_mode"))
    if name is None or attributes.get(name) is None:
        name = next((n for n in colour_modes() if attributes.get(n) is not None), None)
    return (name, attributes[name]) if name else (None, None)


def holds(domain, wanted, attributes, found=None):
    """Whether a device's attributes hold each setting wanted ({attribute: value}, target's). found: what the entity
    shows of itself (shown), for its ranges."""
    found = {**(found or {}), **attributes}
    for name, asked in wanted.items():
        if name in BRIGHTNESS:
            now = attributes.get("brightness")
            if not (_number(asked) and _number(now)
                    and _close(_percent(name, asked), _percent("brightness", now), (0, 100))):
                return False
        elif name in colour_modes() or name == "color_name":
            shows, now = _colour(attributes)
            if name == shows == "color_temp_kelvin":
                if not (_number(asked) and _number(now)
                        and _close(asked, now, _range(domain, name, found))):
                    return False
                continue
            mine, theirs = _hs(name, asked, found), _hs(shows, now, found) if shows else None
            if mine is None or theirs is None:
                return False
            hue = abs(mine[0] - theirs[0]) % 360
            if min(hue, 360 - hue) > TOLERANCE * 360 or abs(mine[1] - theirs[1]) > TOLERANCE * 100:
                return False
        elif _number(asked):
            now = attributes.get(name)
            if not (_number(now) and _close(asked, now, _range(domain, name, found))):
                return False
        elif attributes.get(name) != asked:
            return False
    return True


def problems(source, entity_id, state, asked):
    """What's wrong with a target for this entity (asked: its settings as a goal names them): a state it can't be in,
    a setting its domain's services don't set, a value outside the options or range it shows. Empty when it can hold
    it."""
    domain, found, out = entity_id.split(".", 1)[0], shown(source, entity_id), []
    allowed = states(source, entity_id)
    if state is not None and allowed is not None and state not in allowed:
        out.append(f"{entity_id} has no state {state!r}; its states are {', '.join(sorted(allowed))}.")
    known = settable(domain)
    for name, value in asked.items():
        if not _known(domain, name):
            out.append(f"{entity_id} has no setting {name!r}; its settings are {', '.join(sorted(known)) or 'none'}.")
            continue
        held_as = attribute_of(domain, name)
        listed = _options(held_as, found) or next((s.get("options") for s in ((known.get(held_as) or {}).get(
            "selector") or {}).values() if isinstance(s, dict) and isinstance(s.get("options"), list)), None)
        bounds = (0, 100) if name == "brightness_pct" else _range(domain, held_as, found)
        if listed and not _number(value) and value not in [o.get("value", o) if isinstance(o, dict) else o
                                                           for o in listed]:
            out.append(f"{entity_id}'s {name} can't be {value!r}; it takes {', '.join(map(str, listed))}.")
        elif _number(value) and bounds and not bounds[0] <= value <= bounds[1]:
            out.append(f"{entity_id}'s {name} can't be {value!r}; it takes {bounds[0]} to {bounds[1]}.")
    return out
