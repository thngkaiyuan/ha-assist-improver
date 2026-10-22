"""Runtime for the improver's own AI roles: HA's public AI Task interface only.

Every call needs explicit paid-inference consent. The improver's own model
calls are marked so the recorder never captures them as household sessions.
Usage is what the provider reports through HA's conversation trace, if any.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json

from homeassistant.components import ai_task
from homeassistant.components.conversation.trace import (
    ConversationTraceEventType, async_conversation_trace,
)
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .cases import CaseError, digest
from .const import DOMAIN, STORAGE_VERSION
from .serialization import plain

ROLE_ACTIVE: ContextVar[str | None] = ContextVar("assist_improver_role", default=None)


@contextmanager
def role_marker(role):
    token = ROLE_ACTIVE.set(role)
    try:
        yield
    finally:
        ROLE_ACTIVE.reset(token)


async def run_role(hass, *, role, entity_id, instructions, payload, structure, allow_paid_inference=False):
    """One AI Task call. Returns a run record; never retries or resamples."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    if not entity_id:
        raise CaseError("No AI Task entity is configured for the improver; open its Configure screen")
    text = f"{instructions}\n\nInput (JSON):\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    record = {"role": role, "entity_id": entity_id, "started": utcnow().isoformat(),
              "instructions_sha256": digest(instructions), "input_sha256": digest(payload),
              "input_chars": len(text), "status": "running", "output": None, "error": None}
    with role_marker(role), async_conversation_trace() as trace:
        try:
            result = await ai_task.async_generate_data(
                hass, task_name=f"Assist Improver: {role}", entity_id=entity_id,
                instructions=text, structure=structure)
            record.update(status="completed", output=plain(result.data), conversation_id=result.conversation_id)
        except Exception as err:
            record.update(status="failed", error={"type": type(err).__name__, "message": str(err)[:500]})
    stats = [plain(e["data"]["stats"]) for e in trace.as_dict()["events"]
             if e["event_type"] == ConversationTraceEventType.AGENT_DETAIL
             and isinstance(e["data"], dict) and "stats" in e["data"]]
    record["usage"] = {"status": "provider_reported_partial" if stats else "unavailable", "events": stats,
                       "billing_total": None,
                       "limits": "Provider-reported token counts where available; not an exact bill"}
    record["finished"] = utcnow().isoformat()
    return record


class RoleRuns:
    """Private record of every AI role call, kept for scoring and as offline fixtures."""
    def __init__(self, hass):
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.role_runs", private=True, atomic_writes=True)
        self.runs = []
        self.lock = asyncio.Lock()

    async def start(self):
        saved = await self.store.async_load()
        self.runs = saved.get("runs", []) if saved else []
        for run in self.runs:
            if run["status"] == "running":
                run.update(status="interrupted", error={"message": "Reload during the call; not repeated"})
        await self.store.async_save({"runs": self.runs})

    async def record(self, run):
        async with self.lock:
            self.runs = [r for r in self.runs if r.get("id") != run["id"]] + [deepcopy(run)]
            await self.store.async_save({"runs": self.runs})
        return deepcopy(run)
