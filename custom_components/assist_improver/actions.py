"""Home Assistant's actions, as the roles read them: the catalogue every set-up integration describes (the public
helpers.service.async_get_all_descriptions, with the names and descriptions in Home Assistant's language), what one
entity can do (operations), and a search over the whole catalogue (find_actions)."""

from homeassistant.core import SupportsResponse
from homeassistant.helpers import translation
from homeassistant.helpers.service import async_get_all_descriptions

PAGE = 25  # Actions on a page of find_actions; the total says how many matched.


async def catalogue(hass):
    """{action: {action, name, description, target, fields, response_only}}: every action Home Assistant has now, as
    its integration describes it. A field in a section (light.turn_on's advanced fields) is listed with the others."""
    descriptions = await async_get_all_descriptions(hass)
    words = await translation.async_get_translations(hass, hass.config.language, "services", list(descriptions))
    out = {}
    for domain, services in descriptions.items():
        for service, described in services.items():
            key = f"component.{domain}.services.{service}"
            fields = {}
            for field, config in (described.get("fields") or {}).items():
                for name, spec in ((config.get("fields") or {}).items() if "fields" in config else [(field, config)]):
                    fields[name] = {k: v for k, v in {
                        "name": words.get(f"{key}.fields.{name}.name"),
                        "description": words.get(f"{key}.fields.{name}.description"),
                        "required": spec.get("required") or None, "example": spec.get("example"),
                        "selector": spec.get("selector"), "filter": spec.get("filter")}.items() if v is not None}
            out[f"{domain}.{service}"] = {k: v for k, v in {
                "action": f"{domain}.{service}", "name": words.get(f"{key}.name"),
                "description": words.get(f"{key}.description"), "target": described.get("target"),
                "fields": fields or None,
                "response_only": hass.services.supports_response(domain, service) is SupportsResponse.ONLY or None,
            }.items() if v is not None}
    return out


def controls(found):
    """The entity domains some action changes: one that targets them by domain and gives more than a response."""
    return {domain for action in (found or {}).values() if not action.get("response_only")
            for f in ((action.get("target") or {}).get("entity") or []) for domain in f.get("domain") or []}


def _features(value, wanted):
    """Whether supported features (a number) hold every bit of any one of wanted, as Home Assistant checks an entity
    service's required features."""
    return any((value or 0) & f == f for f in wanted)


def _matches(spec, entry, state):
    """Whether an entity (its registry entry and state) is one a target's entity filter takes."""
    attributes = (state or {}).get("attributes") or {}
    device_class = attributes.get("device_class") or entry.get("device_class") or entry.get("original_device_class")
    return ((not spec.get("domain") or (entry.get("entity_id") or "").split(".", 1)[0] in spec["domain"])
            and (not spec.get("integration") or entry.get("platform") == spec["integration"])
            and (not spec.get("device_class") or device_class in spec["device_class"])
            and (not spec.get("supported_features")
                 or _features(attributes.get("supported_features"), spec["supported_features"])))


def _applies(field, state):
    """Whether a field of an action applies to an entity with this state, by the field's filter (its supported
    features, or values one of its attributes must hold)."""
    spec, attributes = field.get("filter") or {}, (state or {}).get("attributes") or {}
    if spec.get("supported_features") and not _features(attributes.get("supported_features"),
                                                        spec["supported_features"]):
        return False
    for name, values in (spec.get("attribute") or {}).items():
        have = attributes.get(name)
        if not set(have if isinstance(have, list) else [have]) & set(values):
            return False
    return True


def operations(found, entity_id, entry, state):
    """What one entity can do: each action whose target takes it by its domain or integration, with the fields that
    apply to it (their selectors). Actions that take any entity (homeassistant.turn_on, update_entity) are left out
    here; find_actions has them."""
    entry = {**(entry or {}), "entity_id": entity_id}
    out = []
    for action in (found or {}).values():
        specs = (action.get("target") or {}).get("entity") or []
        if any((s.get("domain") or s.get("integration")) and _matches(s, entry, state) for s in specs):
            fields = {k: {x: v for x, v in f.items() if x in ("required", "selector")}
                      for k, f in (action.get("fields") or {}).items() if _applies(f, state)}
            out.append({"action": action["action"], **({"description": action["description"]}
                                                       if action.get("description") else {}),
                        **({"fields": fields} if fields else {}),
                        **({"response_only": True} if action.get("response_only") else {})})
    return out


def find_actions(found, query, page=1):
    """The actions whose ID, name, description or fields hold the query's words (every word, else any), a page at a
    time, with the total."""
    from .exploration import _match
    texts = {a: " ".join([a, d.get("name") or "", d.get("description") or "",
                          *(f"{k} {f.get('name') or ''} {f.get('description') or ''}"
                            for k, f in (d.get("fields") or {}).items())]) for a, d in (found or {}).items()}
    hits, matched = _match(texts, query)
    page = max(page, 1)
    start = (page - 1) * PAGE
    return {"query": query, "matched": matched, "total": len(hits), "page": page,
            "actions": [found[a] for a in hits[start:start + PAGE]],
            **({"next_page": page + 1} if start + PAGE < len(hits) else {})}
