"""What happened in a turn, read the same way on both sides of a test (DETAILS P9.10.5): Assist's tool calls, each with
Home Assistant's result (cases.calls_in), and the turn's call log, every action call under its request, a script's
own included, each {action, data, error?, error_type?} (recorder.Actions: kept in recordings since P8.2.10; a replay's
sandbox keeps one too). A recording from before has none (None): what a script it ran did can't be told from it."""

import re

WORDS = re.compile(r"^(.+_)?message$")  # A field that carries words: message, start_message.
MEDIA = re.compile(r"^(.+_)?media_id$|^media_content_id$|^media$")  # One that carries media to play.


def tts_message(media):
    """The words a text-to-speech media source speaks (media-source://tts/<engine>?message=...), else None."""
    from yarl import URL
    if not isinstance(media, str) or not media.startswith("media-source://tts"):
        return None
    return URL(media).query.get("message")


def carried(data):
    """What a call's data says or plays: {message, media} (its words, a text-to-speech source's among them, else the
    media it plays, as a media selector gives it too); None when it carries neither."""
    words = next((v for k, v in data.items() if WORDS.match(k) and isinstance(v, str) and v.strip()), None)
    media = next((v for k, v in data.items() if MEDIA.match(k) and v), None)
    media = media.get("media_content_id") if isinstance(media, dict) else media
    words = words or tts_message(media)
    media = media if isinstance(media, str) and media.strip() and tts_message(media) is None else None
    return {"message": words or "", "media": media} if words or media else None


def _raised(name):
    """The exception class Home Assistant raises by this name: its own (homeassistant.exceptions) or its schemas'
    (probatio); None for another's (an integration's own) or none."""
    import probatio
    from homeassistant import exceptions
    found = getattr(exceptions, name, None) or getattr(probatio, name, None) if name else None
    return found if isinstance(found, type) and issubclass(found, Exception) else None


def uncalled(error_type):
    """Whether Home Assistant raised an error before announcing any call, so it's no logged call's: its schema refused
    the data (probatio.Invalid), or it has no such action (ServiceNotFound)."""
    import probatio
    from homeassistant.exceptions import ServiceNotFound
    return issubclass(_raised(error_type) or Exception, (probatio.Invalid, ServiceNotFound))


def _ids(value):
    """IDs as a call names them: one, a comma-separated string, or a list."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def named(action, data, source):
    """The entities a call is for (source: the home as recorded): the one its action is named after (a script's own
    action), those its data names (entity_id, or a field ending in _entity_id: tts.speak's media players), and its
    domain's entities on the devices, areas, floors and labels it names, as Home Assistant resolves a call's target."""
    domain, entities = action.split(".", 1)[0], source.get("entities") or {}
    out = {e for k, v in data.items() if k == "entity_id" or k.endswith("_entity_id") for e in _ids(v)}
    if action in entities or action in (source.get("states") or {}):
        out.add(action)
    if "all" in out:
        out |= {e for e in source.get("states") or {} if e.split(".", 1)[0] == domain}
    devices, labels = set(_ids(data.get("device_id"))), set(_ids(data.get("label_id")))
    floors = set(_ids(data.get("floor_id")))
    areas = set(_ids(data.get("area_id"))) | {a for a, found in (source.get("areas") or {}).items()
                                              if found.get("floor_id") in floors}
    if devices or areas or labels:
        for entity_id, entry in entities.items():
            device = (source.get("devices") or {}).get(entry.get("device_id") or "") or {}
            if entity_id.split(".", 1)[0] == domain and (
                    entry.get("device_id") in devices or (entry.get("area_id") or device.get("area_id")) in areas
                    or labels & {*(entry.get("labels") or []), *(device.get("labels") or [])}):
                out.add(entity_id)
    return out - {"all"}


def _results(call, *keys):
    """The entities a tool call's result names under these keys (an intent's success or failed targets)."""
    result = call.get("result") if isinstance(call.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {t.get("id") for k in keys for t in inner.get(k) or [] if isinstance(t, dict) and t.get("id")}


def succeeded(call):
    """The entities a tool call's result says it acted on (an intent's success targets); none for a failed call."""
    return set() if (call.get("result") or {}).get("error") else _results(call, "success")


def announcements(calls, actions, source):
    """What a turn said or played on devices: [{message, media, targets}], from each tool call whose arguments carry
    words or media (targets: the devices its result names) and each call of its call log that carries them and didn't
    fail (targets: the entities it's for)."""
    out = [{**said, "targets": succeeded(call)} for call in calls if (said := carried(call.get("args") or {}))]
    return out + [{**said, "targets": named(a["action"], a.get("data") or {}, source)} for a in actions or []
                  if not a.get("error") and (said := carried(a.get("data") or {}))]


def reached(calls, actions, source):
    """The entities a turn's calls reached: those its tool calls' results name, and those each call of its call log
    that didn't fail is for."""
    out = set().union(*(succeeded(c) for c in calls))
    return out.union(*(named(a["action"], a.get("data") or {}, source) for a in actions or [] if not a.get("error")))


def touched(calls, actions, source):
    """The entities a turn's calls were at, done or not: those its tool calls' results name (acted on or failed on),
    and those each call of its call log is for."""
    out = set().union(*(_results(c, "success", "failed") for c in calls))
    return out.union(*(named(a["action"], a.get("data") or {}, source) for a in actions or []))


def ran_scripts(calls):
    """The scripts these tool calls ran, each by its tool's name (script__name), in order, once each."""
    return list(dict.fromkeys(c["tool"] for c in calls if re.search(r"(^|__)script__", c.get("tool") or "")))


def unseen(calls, actions):
    """Whether what a turn did can't all be told: a script ran in it, and it kept no call log (a recording from
    before P8.2.10), so what the script did isn't recorded beyond the changes."""
    return actions is None and bool(ran_scripts(calls))
