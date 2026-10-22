"""Explicit JSON conversion with secret-key filtering and no object reprs."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import math
import re

from homeassistant.core import Context

SECRET = re.compile(r"(?:password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|private[_-]?key)", re.I)


def plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Context):
        # Context uses slots, not dataclass fields. Keep its public identity
        # metadata without traversing origin_event or internal cached objects.
        return plain(value.as_dict())
    if isinstance(value, Mapping):
        return {str(k): "[redacted]" if SECRET.search(str(k)) else plain(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in value]
    if is_dataclass(value):
        return plain({f.name: getattr(value, f.name) for f in fields(value)})
    return {"omitted_type": type(value).__name__}
