"""JSON copies of what the improver keeps, written as Home Assistant writes its own, and what an export leaves out."""

from collections.abc import Mapping
from decimal import Decimal
import re

from homeassistant.components.diagnostics import REDACTED
from homeassistant.helpers.json import json_bytes, json_encoder_default
from homeassistant.util.json import json_loads


def plain(value):
    """A JSON copy of a value, by Home Assistant's own encoder (helpers.json): what it can't write is left out as
    {"omitted_type": its type}, never as the object's repr; a Decimal is kept as the text HA's live context shows
    Assist. Nothing is filtered: a recording keeps what Home Assistant has (it stays in the household's Home
    Assistant)."""
    try:
        return json_loads(json_bytes(value))
    except TypeError:  # Something in it HA's encoder can't write: copied piece by piece.
        return _piecewise(value)


def _key(key):
    if isinstance(key, str):
        return key
    try:  # A number or an enum, as HA's encoder writes it as a key.
        return next(iter(json_loads(json_bytes({key: None}))))
    except TypeError:
        return f"omitted_type:{type(key).__name__}"


def _piecewise(value):
    if isinstance(value, Mapping):
        return {_key(k): _piecewise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_piecewise(v) for v in value]
    if isinstance(value, Decimal):  # As Home Assistant's live context shows one to Assist.
        return str(value)
    try:
        return json_loads(json_bytes(value))
    except TypeError:
        try:
            return _piecewise(json_encoder_default(value))
        except TypeError:
            return {"omitted_type": type(value).__name__}


# A token in a URL's query (a camera's picture: ?token=...), and the separator after it, if another parameter follows.
TOKEN = re.compile(r"([?&])(?:access_)?token=[^&#]*(&)?")
# An export cuts a key with a word of CREDENTIALS in it, or one of LOCATIONS (location: a phone's [lat, lon]).
CREDENTIALS = ("password", "secret", "api_key", "apikey", "access_key", "authorization", "cookie", "private_key")
LOCATIONS = ("location", "latitude", "longitude")


def exported(value):
    """A recording as Export recording gives it, which may leave Home Assistant and be posted publicly (with a bug
    report): in every mapping, the value under a key naming a credential or a location (cut) becomes Home Assistant's
    redaction marker, as in its diagnostics (None and "", with nothing to hide, stay), and a token in a URL's query (a
    camera's picture) is dropped. Everything else goes as kept, an AI setting (model, max_tokens, temperature) too:
    words with a space in them aren't a URL. The improver's store keeps the recording whole."""
    if isinstance(value, dict):
        return {k: REDACTED if cut(k) and v not in (None, "") else exported(v) for k, v in value.items()}
    if isinstance(value, list):
        return [exported(v) for v in value]
    if isinstance(value, str) and "token=" in value and not any(c.isspace() for c in value):
        return TOKEN.sub(lambda m: m[1] if m[2] else "", value)
    return value


def cut(key):
    """Whether a key names a credential (a word of CREDENTIALS in it, a hyphen or space read as an underscore, as in
    X-API-Key; or token at its end: access_token, never max_tokens) or a location (one of LOCATIONS)."""
    key = key.lower().replace("-", "_").replace(" ", "_") if isinstance(key, str) else ""
    return key.endswith("token") or key in LOCATIONS or any(word in key for word in CREDENTIALS)
