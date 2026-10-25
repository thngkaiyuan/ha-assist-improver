"""Diagnostics for a bug report or a performance question: versions and settings, how much is stored and how, the
last runs with where their time went and what went wrong, the AI roles' calls, and the replay sandboxes' timings.
No conversation content: a recording itself needs the explicit export_recording action."""

import os
from collections import Counter, defaultdict

from homeassistant.const import __version__ as CORE_VERSION

from .const import DOMAIN, VERSION
from .worker import STATS
from .settings import ROLES, role_model

RUNS_SHOWN = 10
CALLS_SHOWN = 200


def _storage(path):
    """Sizes of the improver's stores, in bytes; recordings summed (count, total, largest), and the developer view's
    traces (count, total: their sizes only, never their content)."""
    stores, sessions, traces = {}, [], []
    for name in sorted(os.listdir(path)) if os.path.isdir(path) else []:
        if name.startswith(f"{DOMAIN}."):
            size = os.path.getsize(os.path.join(path, name))
            if name.startswith(f"{DOMAIN}.session_"):
                sessions.append(size)
            elif name.startswith(f"{DOMAIN}.trace_"):
                traces.append(size)
            else:
                stores[name.removeprefix(f"{DOMAIN}.")] = size
    return {"stores": stores, "recordings": {"count": len(sessions), "bytes": sum(sessions),
                                             "largest": max(sessions, default=0)},
            "traces": {"count": len(traces), "bytes": sum(traces)}}


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    recorder = runtime.recorder
    runs = getattr(runtime.role_runs, "runs", [])
    pipeline_runs = [r for r in runs if r.get("kind") == "pipeline"][-RUNS_SHOWN:]
    calls = defaultdict(lambda: {"calls": 0, "failed": 0, "seconds": 0.0, "slowest": 0.0, "errors": []})
    for run in [r for r in runs if r.get("role") and r.get("started")][-CALLS_SHOWN:]:
        role = calls[run["role"]]
        role["calls"] += 1
        role["seconds"] = round(role["seconds"] + (run.get("seconds") or 0), 1)
        role["slowest"] = max(role["slowest"], run.get("seconds") or 0)
        if run.get("status") == "failed":
            role["failed"] += 1
            role["errors"] = (role["errors"] + [(run.get("error") or {}).get("message")])[-3:]
    index = recorder.index.values()
    household = hass.data.get(f"{DOMAIN}_household")
    knowledge = getattr(household, "knowledge", {}) or {}
    return {
        "versions": {"integration": VERSION, "home_assistant": CORE_VERSION},
        "settings": dict(entry.options),
        "models": {role: role_model(entry, role) for role in ROLES},
        "storage": await hass.async_add_executor_job(_storage, hass.config.path(".storage")),
        "recordings": {"by_status": dict(Counter(s.get("status") for s in index)),
                       "by_storage": dict(Counter(s.get("stored", "full") for s in index)),
                       "with_no_turns": sum(1 for s in index if s.get("user_turns") == 0)},
        "retention": runtime.retention,
        "runs": [{k: r.get(k) for k in ("started", "finished", "status", "trigger", "only", "stage", "progress",
                                         "error")} for r in pipeline_runs],
        "role_calls": dict(calls),
        "sandboxes": dict(STATS),
        "improvements": dict(Counter(i.get("status") for i in runtime.improvements.items.values())),
        "household": {"lines": len(knowledge.get("everyone", [])) + sum(len(v) for v in knowledge.get("people", {})
                                                                         .values())},
        "recorder": recorder.status(),
    }
