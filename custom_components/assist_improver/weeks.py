"""An automation change as its review states it (DETAILS P12.2.24): when it runs, its triggers and conditions in
words, stated by code from Home Assistant's own configuration (its schema's normal form); and its simulated week
(evidence.automation_week, DETAILS P6.13) in counts and times first, each run on demand. For a change to an automation
the home has, or one turned on or off, the week without and with the change, as runs added and dropped.

A trigger or condition of a kind not worded here reads as Home Assistant's own YAML of it, on one line.
"""

from collections import Counter
import json

import yaml
from homeassistant.util import dt as dt_util

from .changes import automation_schema

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIMES_SHOWN = 3  # Times of day a week's runs are listed by; more read as a count.
RUNS_KEPT = 100  # Runs a review keeps to show, each; past them, how many more.
UNITS = ("hours", "minutes", "seconds")  # A time pattern's, largest first.
LARGER = {"hours": "day", "minutes": "hour", "seconds": "minute"}  # The unit a time pattern's fixed value repeats in.


def _listed(words):
    """Words as a list is said: "A and B", "A, B and C"."""
    return " and ".join(words) if len(words) < 3 else ", ".join(words[:-1]) + " and " + words[-1]


def _names(hass, value):
    """Entity IDs as Home Assistant names them now, joined: "Desk lamp and Porch light"."""
    ids = value if isinstance(value, list) else [value]

    def name(entity_id):
        state = hass.states.get(entity_id) if hass is not None and isinstance(entity_id, str) else None
        return (state.attributes.get("friendly_name") if state is not None else None) or str(entity_id)
    return _listed([name(e) for e in ids])


def _time(value):
    """A time of day as said: 21:00, or 21:00:30."""
    text = str(value)
    return text[:5] if text.endswith(":00") and len(text) == 8 else text


def _yaml(item):
    return yaml.dump(item, default_flow_style=True, allow_unicode=True, sort_keys=False, width=10**6,
                     Dumper=getattr(yaml, "CSafeDumper", yaml.SafeDumper)).strip().strip("{}")


def _period(value):
    """How long, as written: "00:05:00", or "5 minutes" for {minutes: 5}."""
    if isinstance(value, dict):
        return " ".join(f"{n} {unit if n != 1 else unit.rstrip('s')}" for unit, n in value.items() if n)
    return str(value)


def _weekdays(value):
    days = value if isinstance(value, list) else [value]
    return ", ".join(d[:1].upper() + d[1:3] for d in days)


def _span(item):
    """A number's bounds: "above 20 and below 25"."""
    return " and ".join(f"{bound} {item[bound]}" for bound in ("above", "below") if item.get(bound) is not None)


def _pattern(item):
    """A time pattern as said: "Every 5 minutes" for /5, "At minute 0 of every hour" for 0; None if it sets
    nothing."""
    every, at = [], []
    for unit in UNITS:
        value = str(item.get(unit, "*"))
        if value.startswith("/"):
            every.append(f"every {unit[:-1]}" if value == "/1" else f"every {value[1:]} {unit}")
        elif value != "*":
            at.append(f"{unit[:-1]} {value}")
    if not every and not at:
        return None
    largest = next(u for u in UNITS if str(item.get(u, "*")) != "*")
    said = (", ".join(every) + (f" at {', '.join(at)}" if at else "")) if every else (
        f"at {', '.join(at)} of every {LARGER[largest]}")
    return said[:1].upper() + said[1:]


def trigger(hass, item):
    """One trigger in words."""
    kind = item.get("trigger") or item.get("platform")
    who = _names(hass, item["entity_id"]) if item.get("entity_id") else None
    lasting = f" for {_period(item['for'])}" if item.get("for") else ""
    if kind == "time":
        ats = item["at"] if isinstance(item["at"], list) else [item["at"]]
        said = " and ".join(_names(hass, a) if "." in str(a) else _time(a) for a in ats)
        return f"At {said}" + (f" on {_weekdays(item['weekday'])}" if item.get("weekday") else "")
    if kind == "sun":
        return f"At {item['event']}" + (f", offset {item['offset']}" if item.get("offset") else "")
    if kind == "state" and who:
        what = f"{who}'s {item['attribute']}" if item.get("attribute") else who
        moves = (f" from {item['from']}" if item.get("from") is not None else "") + (
            f" to {item['to']}" if item.get("to") is not None else "")
        return f"When {what} changes{moves}{lasting}"
    if kind == "numeric_state" and who:
        what = f"{who}'s {item['attribute']}" if item.get("attribute") else who
        return f"When {what} is {_span(item)}{lasting}"
    if kind == "homeassistant":
        return "When Home Assistant starts" if item.get("event") == "start" else "When Home Assistant shuts down"
    if kind == "time_pattern" and (said := _pattern(item)):
        return said
    if kind == "event":
        return f"When the event {item['event_type']} fires"
    if kind == "template":
        return f"When {item['value_template']} becomes true{lasting}"
    if kind == "zone" and who:
        return f"When {who} {'enters' if item.get('event') == 'enter' else 'leaves'} {_names(hass, item['zone'])}"
    if kind == "conversation":
        commands = item["command"] if isinstance(item["command"], list) else [item["command"]]
        return "When someone says " + " or ".join(f"“{c}”" for c in commands)
    return f"When {_yaml({'trigger': kind, **{k: v for k, v in item.items() if k not in ('trigger', 'platform')}})}"


def condition(hass, item):
    """One condition in words."""
    kind = item.get("condition")
    who = _names(hass, item["entity_id"]) if item.get("entity_id") else None
    if kind in ("and", "or", "not"):  # Not: none of them holds.
        inner = [condition(hass, c) for c in item.get("conditions") or []]
        joined = (" and " if kind == "and" else " or ").join(inner)
        joined = f"({joined})" if len(inner) > 1 else joined
        return f"not {joined}" if kind == "not" else joined
    if kind == "state" and who:
        states = item["state"] if isinstance(item["state"], list) else [item["state"]]
        what = f"{who}'s {item['attribute']}" if item.get("attribute") else who
        lasting = f" for {_period(item['for'])}" if item.get("for") else ""
        return f"{what} is {' or '.join(map(str, states))}{lasting}"
    if kind == "numeric_state" and who:
        return f"{who} is {_span(item)}"
    if kind in ("time", "sun"):
        said = _time if kind == "time" else str
        out = [f"{bound} {said(item[bound])}" for bound in ("after", "before") if item.get(bound)]
        days = f" on {_weekdays(item['weekday'])}" if item.get("weekday") else ""
        return f"it's {' and '.join(out)}{days}" if out else f"it's{days}"
    if kind == "template":
        return f"{item['value_template']} is true"
    if kind == "zone" and who:
        return f"{who} is in {_names(hass, item['zone'])}"
    if kind == "trigger":
        return f"it was trigger {item['id']}"
    return _yaml(item)


def words(hass, config):
    """{when: its triggers in words, if: its conditions (None without)} of an automation's configuration, read in its
    schema's normal form (the plural keys, each trigger's own key); as written when Home Assistant's schema refuses
    it."""
    try:
        config = automation_schema()({k: v for k, v in config.items() if k != "id"})
    except Exception:  # noqa: BLE001 - a configuration HA refuses never applies; it's described as written.
        pass
    triggers = config.get("triggers", config.get("trigger")) or []
    conditions = config.get("conditions", config.get("condition")) or []
    triggers, conditions = (v if isinstance(v, list) else [v] for v in (triggers, conditions))
    said = [trigger(hass, t) for t in triggers if isinstance(t, dict)]  # Any of them: "At 23:00 or when …".
    return {"when": " or ".join([*said[:1], *(t[:1].lower() + t[1:] for t in said[1:])]),
            "if": "If " + " and ".join(condition(hass, c) for c in conditions if isinstance(c, dict))
            if conditions else None}


def _among(runs, counted):
    """The runs counted (a Counter of their keys), in the order they ran."""
    left, out = Counter(counted), []
    for run in runs:
        if left[_key(run)] > 0:
            left[_key(run)] -= 1
            out.append(run)
    return out


def _key(run):
    """A run as compared across two weeks: which automation, the minute it ran, and what it changed (each entity's new
    state and the attributes it set)."""
    changed = tuple(sorted((c["entity_id"], str(c.get("new")), json.dumps(c.get("attributes") or {}, sort_keys=True,
                                                                          default=str))
                           for c in run.get("changed") or []))
    return run.get("automation"), (run.get("at") or "")[:16], changed


def _run(run, sign=" "):
    """One run as the review keeps it: when (the weekday and time, in the home's time zone) and what it changed."""
    at = dt_util.parse_datetime(run.get("at") or "")
    local = dt_util.as_local(at) if at else None
    return {"sign": sign, "at": f"{DAYS[local.weekday()]} {local.strftime('%H:%M')}" if local else "",
            "changed": [{k: c.get(k) for k in ("entity_id", "old", "new", "attributes") if k in c}
                        for c in run.get("changed") or []]}


def _times(runs):
    """When runs ran, by time of day: "at 21:00", "at 07:00 and 21:00", "at 06:30, 07:00 and 21:00", or "at 9
    different times"."""
    times = sorted({r["at"].split(" ", 1)[-1] for r in runs if r["at"]})
    if not times:
        return ""
    if len(times) > TIMES_SHOWN:
        return f" at {len(times)} different times"
    return " at " + _listed(times)


def week(found, automation):
    """An automation change's week as its review keeps it: {days, result: the counts and times, runs: each run, the
    first RUNS_KEPT (more: how many past those)}. found: evidence.automation_week's; automation: one the improvement
    changes ({id, ...}). A new automation's own runs; for an edit or one turned on or off, the runs the change adds (+)
    and drops (-), of every automation the week ran (what it sets off counts too)."""
    with_, without = found.get("with") or {}, found.get("without")
    days = min([w.get("days", 0) for w in (with_, without or {}) if w] or [0])
    if without is None:
        runs = [_run(r) for r in with_.get("runs") or [] if str(r.get("id")) == str(automation["id"])]
        result = f"{len(runs)} run{'s' * (len(runs) != 1)}{_times(runs)}" if runs else "no runs"
    else:
        ran, was = with_.get("runs") or [], without.get("runs") or []
        before, after = Counter(map(_key, was)), Counter(map(_key, ran))
        added = [_run(r, "+") for r in _among(ran, after - before)]
        dropped = [_run(r, "-") for r in _among(was, before - after)]
        runs = added + dropped
        result = ", ".join([f"{len(added)} run{'s' * (len(added) != 1)} added{_times(added)}"] * bool(added) + [
            f"{len(dropped)}{(' run' + 's' * (len(dropped) != 1)) * (not added)} dropped{_times(dropped)}"] * bool(
            dropped)) or "the same runs"
    return {"days": days, "result": result, "runs": runs[:RUNS_KEPT], "more": max(0, len(runs) - RUNS_KEPT)}


def _changed(hass, change):
    """What a run changed on one entity, as said: "Porch light: off → on" (or the attributes it changed)."""
    name = _names(hass, change["entity_id"])
    if change.get("old") != change.get("new"):
        return f"{name}: {change.get('old')} → {change.get('new')}"
    return f"{name}: {', '.join(sorted(change.get('attributes') or {})) or change.get('new')}"


def _known(hass, automation_id):
    """Whether Home Assistant has an automation with this configuration ID now."""
    return hass is not None and any(str(s.attributes.get("id")) == str(automation_id)
                                    for s in hass.states.async_all("automation"))


def reviewed(hass, item):
    """Each automation an improvement changes, as its review shows it: {name, change (new, edited, on or off), when,
    if, and its week (week) once it ran, or failed: true when it couldn't}. Before its week ran (or for one that
    couldn't start), from its changes as proposed."""
    found = item.get("week") or {}
    automations = found.get("automations") or [
        {"id": c.get("id"), **({"on": c["on"]} if "on" in c else {"config": c.get("config") or {}})}
        for c in item.get("changes") or [] if c.get("kind") == "automation"]
    out = []
    for automation in automations:
        config = automation.get("config") or {}
        new = automation["new"] if "new" in automation else not _known(hass, automation["id"])
        change = ("on" if automation["on"] else "off") if "on" in automation else "new" if new else "edited"
        shown = {"name": config.get("alias") or str(automation["id"]), "change": change,
                 **(words(hass, config) if config else {"when": "", "if": None})}
        if found.get("failed"):
            shown["failed"] = True
        elif week_ := automation.get("week"):
            shown["week"] = {**week_, "runs": [{**r, "changed": "; ".join(_changed(hass, c) for c in r["changed"])
                                                or "nothing changed"} for r in week_["runs"]]}
        out.append(shown)
    return out
