"""Cases: one goal as it came up in one recording (its turns, destination and criteria), kept for its baseline and
its tests; no inference and no change to the home."""

import asyncio
from copy import deepcopy

from homeassistant.helpers.storage import Store

from .cases import CaseError, create_case, verify_case
from .const import DOMAIN, STORAGE_VERSION
from .records import Case
from .simulation import audit_recorded_case


class CaseBook:
    def __init__(self, hass, recorder):
        self.recorder = recorder
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.cases", private=True, atomic_writes=True)
        self.cases: dict[str, Case] = {}
        self.lock = asyncio.Lock()

    async def start(self):
        saved = await self.store.async_load()
        self.cases = saved.get("cases", {}) if saved else {}
        # Opaque provider-native history lives only in the active trial. Never
        # rebuild it from lossy exports or silently repeat a paid conversation.
        # (awaiting_review and ready_for_next_turn: the pauses between turns earlier versions kept.)
        for case in list(self.cases.values()):
            if case["baseline"]["status"] in {"running", "awaiting_review", "ready_for_next_turn"}:
                interrupted = deepcopy(case)
                interrupted["baseline"].update(status="interrupted",
                    error={"message": "In-memory provider history lost on reload; attempt preserved, no automatic restart"})
                await self._save(interrupted)

    def summaries(self):
        return [{"id": c["id"], "title": c["title"], "turns": c["turns"],
                 "destination_status": c["destination"]["status"], "revision": c["revision"],
                 "question": c["question"], "baseline_status": c["baseline"]["status"],
                 "audit_status": c["audit"]["status"] if c["audit"] else "not_run"}
                for c in self.cases.values()]

    def get(self, case_id):
        if case_id not in self.cases:
            raise CaseError("Unknown case ID")
        return deepcopy(self.cases[case_id])

    async def _save(self, case):
        updated = {**self.cases, case["id"]: deepcopy(case)}
        await self.store.async_save({"cases": updated})
        self.cases = updated  # Do not advertise a revision if persistence failed.
        return deepcopy(case)

    async def create(self, recording_id, **grounding):
        recording = await self.recorder.export(recording_id)
        case = create_case(recording, **grounding)
        async with self.lock:
            if case["id"] in self.cases:
                existing = self.get(case["id"])  # Repeated import cannot overwrite feedback.
                # A case built before a field existed takes it from this fresh build; nothing it had is replaced.
                upgrade = {k: case[k] for k in ("goal_unmet", "acceptable_devices")
                           if case[k] and not existing.get(k)}
                return await self._save({**existing, **upgrade}) if upgrade else existing
            return await self._save(case)

    async def feedback(self, case_id, *, expected_revision, feedback,
                       destination=None, criteria=None, evidence=None,
                       outcome="revision_requested"):
        async with self.lock:
            old = self.get(case_id)
            if old["revision"] != expected_revision:
                raise CaseError("Case changed; refresh before submitting feedback")
            if outcome not in {"revision_requested", "destination_confirmed", "fine_as_is"}:
                raise CaseError("Unsupported feedback outcome")
            if not feedback.strip():
                raise CaseError("Feedback must describe the observation or requested correction")
            if outcome == "destination_confirmed":
                recording = await self.recorder.export(old["recording_id"])
                grounded = create_case(recording, title=old["title"], first_turn=old["turns"][0],
                                      last_turn=old["turns"][-1], destination=destination,
                                      criteria=criteria, evidence=evidence,
                                      intervention_turns=old["intervention_turns"])
                verify_case(grounded, recording)
                old["destination"], old["question"] = grounded["destination"], None
            elif outcome == "revision_requested":
                old["destination"]["status"] = "needs_feedback"
                old["question"] = "Help your assistant learn: what would you have liked to happen?"
            else:
                old["destination"]["status"] = "fine_as_is"
                old["question"] = None
            old["feedback"].append({"outcome": outcome, "text": feedback,
                                    "previous_revision": old["revision"],
                                    "previous_destination": self.cases[case_id]["destination"],
                                    "previous_baseline": old["baseline"], "previous_audit": old["audit"]})
            old["revision"] += 1
            old["audit"] = None
            # Feedback is not permission to resample an unreproduced assistant.
            # Preserve trial status/attempts as well as its prior audit trail.
            return await self._save(old)

    async def drop(self, case_ids):
        """Forget cases (a deleted conversation's), with their baselines."""
        async with self.lock:
            updated = {k: v for k, v in self.cases.items() if k not in case_ids}
            if len(updated) < len(self.cases):
                await self.store.async_save({"cases": updated})
                self.cases = updated

    async def audit(self, case_id):
        async with self.lock:
            case = self.get(case_id)
            recording = await self.recorder.export(case["recording_id"])
            case["audit"] = audit_recorded_case(case, recording)
            return await self._save(case)
