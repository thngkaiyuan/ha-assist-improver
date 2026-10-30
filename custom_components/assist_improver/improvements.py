"""Tested improvements awaiting the user's decision: one list, reviewed in the panel (panel.py).

An improvement is one idea's changes with the test that validated them on the conversation it came from (SPEC.md
P0.10): built by the flow when the proposer sends the idea on (build_for), a verbatim copy of its round's test. The
screens read one row projected from that test (row). A decision goes through `decide`, which updates the list and the
notification together.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import re

from homeassistant.components import persistent_notification
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .cases import CaseError, digest
from .changeset import ORDER, Changesets, Conflict, plan_steps, read, readable, undo_steps, without
from .household import switched_off
from .instructions import household_line
from .const import DOMAIN, STORAGE_VERSION
from .failures import contained
from . import contracts, records
from .records import Improvement
from .trace import journal, span, timed

PENDING = ("awaiting_review", "needs_attention")  # Waiting on the user: in the notification.
# Never changed without the household's review: devices whose misuse is a safety or security problem.
SENSITIVE_DOMAINS = {"lock", "alarm_control_panel", "valve", "siren"}
SENSITIVE_COVERS = {"garage", "door", "gate"}
# A door or valve: a cover or valve, or anything named after a door, gate, garage or valve (as a switch that runs a
# garage door): by its entity ID here, or by its name in Home Assistant (physical). The review notes that their motion
# isn't simulated (panel.py), and a new name for one waits for the household's review (sensitive).
PHYSICAL = re.compile(r"\b(?:cover|valve)\.\w+"  # Covers and valves, and what drives a door or valve:
                      r"|\b(?:switch|script|button)\.(?:\w+_)?(?:doors?|gates?|garage|valves?)\d*(?:_\w+)?\b")
DRIVERS = ("switch", "script", "button")  # What can drive a door or valve, named after it.
DOOR_NAME = re.compile(r"\b(?:doors?|gates?|garage|valves?)\d*\b", re.IGNORECASE)


def physical(hass, entity_id):
    """Whether a device is a door or valve, or drives one: by its entity ID (PHYSICAL), or a switch, script or button
    whose name as Home Assistant shows it (its state's friendly name, from its registry entry) names a door, gate,
    garage or valve."""
    if PHYSICAL.fullmatch(entity_id or ""):
        return True
    if hass is None or (entity_id or "").split(".", 1)[0] not in DRIVERS:
        return False
    state = hass.states.get(entity_id)
    return bool(state and DOOR_NAME.search(str(state.attributes.get("friendly_name") or "")))


def sensitive(hass, entity_id):
    """Whether a device is security-sensitive: a lock, an alarm, a siren, or a door or valve (physical), save a cover
    Home Assistant says is something else (a blind, say). Unknown ones, and covers of no known kind, count as
    sensitive."""
    domain = (entity_id or "").split(".", 1)[0]
    if domain in SENSITIVE_DOMAINS:
        return True
    if domain == "cover":
        state = hass.states.get(entity_id)
        return state is None or state.attributes.get("device_class") in (None, *SENSITIVE_COVERS)
    return physical(hass, entity_id)


def low_risk(hass, item, steps=None):
    """Whether an improvement may be applied without review: only household lines, and new names for devices that
    aren't security-sensitive (never one that takes a name away). Never anything that lets Assist control more
    (exposing a device, a new or changed script), nor an edit to an assistant's own instructions (template code, run
    for every request: someone decides), nor one whose test made Assist noticeably wordier (people want it brief:
    someone decides). steps: what accepting writes now (changeset.plan_steps), when known: only the household
    knowledge and names added; a household line can take a line out of an assistant's own instructions."""
    from .instructions import household_line
    changes = item.get("changes") or []
    return bool(changes) and not wordier(row(item["test"])) and all(
        household_line(c)
        or (c.get("kind") == "alias" and not c.get("replaces") and not sensitive(hass, c.get("entity_id")))
        for c in changes) and all(
        s["kind"] == "household" or (s["kind"] == "alias" and all(a in s["after"] for a in s["before"] or []))
        for s in steps or [])


# Noticeably wordier: Assist's replies with the change at least this many times as long, and this many more words,
# a reply on average (counted by code, from the test's conversation and its replay).
WORDIER, MORE_WORDS = 1.5, 10


def reply_words(shown):
    """Assist's replies in words, a reply on average: before (the conversation as it went) and after (its replay with
    the change), and whether it got noticeably wordier. None without both. shown: a test's row."""
    def average(turns):
        said = [len((t.get("assistant") or "").split()) for t in turns or [] if t.get("assistant")]
        return sum(said) / len(said) if said else None
    before, after = average(shown.get("before")), average(shown.get("after"))
    if before is None or after is None:
        return None
    return {"before": round(before), "after": round(after),
            "wordier": after >= WORDIER * before and after - before >= MORE_WORDS}


def wordier(shown):
    return bool((reply_words(shown) or {}).get("wordier"))


NOTIFICATION = f"{DOMAIN}_improvements"
OUTCOME_NOTIFICATION = f"{DOMAIN}_changes"


def auto_notification(improvement_id):
    """The notification saying an improvement was applied automatically, until it's undone."""
    return f"{DOMAIN}_auto_{improvement_id}"


DECISIONS = {  # Decision: its label. Undone: back up for review, to accept again, revise or decline.
    "accept": "Accept changes", "revise": "Ask for a revision", "decline": "Decline", "undo": "Undo changes"}
# The reasons a decision can be given with, recorded beside it: declined as not an issue (it went fine, or the
# household was only trying Assist out).
REASONS = {"decline": ["not_an_issue"]}


def choices(item, revising=False):
    """The decisions open for an improvement in its status. One undone is up for review again: accept it again, ask for
    a revision, or decline it for good. While its revision's round runs (revising: flow.Flow.revising), it's open to
    accept or decline, not to revise again (P0.6)."""
    if item["status"] == "applied":
        return ["undo"]
    if item["status"] in ("conflict", "declined", "revised"):
        return []
    return ["accept", "decline"] if revising else ["accept", "revise", "decline"]


def reasons(decision):
    """The reasons that can be given with a decision."""
    return list(REASONS.get(decision, []))


class Improvements:
    """The list, its decisions and their application; the source of truth for both review screens."""

    def __init__(self, hass, recorder):
        self.hass, self.recorder = hass, recorder
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.improvements", private=True, atomic_writes=True)
        self.changesets = Changesets(hass)
        self.items: dict[str, Improvement] = {}
        self._locks = {}  # One decision at a time per improvement: two tabs, or auto-accept, never cross.
        self._notified = None  # The improvements the notification listed last (journaled when it changes).
        self._unannounced = set()  # Written up for auto-accept to take: not announced as waiting yet (unannounced).

    async def start(self):
        # An improvement belongs to the one conversation it came from (SPEC.md P0.10); one that names none is an
        # earlier version's, and starts fresh: what it applied stays in Home Assistant's configuration, without undo.
        saved = ((await self.store.async_load()) or {}).get("improvements", {})
        self.items = {iid: item for iid, item in saved.items() if item.get("conversation")}
        self._sync()

    async def recover(self):
        """Once HA has started: a change HA stopped in the middle of is undone (or flagged as needing attention)."""
        if outcome := await self.changesets.start():
            item = self.items.get(outcome["improvement_id"])
            if item is not None:
                if outcome["status"] == "needs_attention":
                    item["status"] = "needs_attention"
                item.setdefault("history", []).append({"at": utcnow().isoformat(), "event": "interrupted", **outcome})
            applying = outcome["action"] == "apply"
            what = "applying" if applying else "undoing"
            journal("warning", f"Home Assistant stopped while {what} an improvement: {outcome['status']}",
                    improvement_id=outcome["improvement_id"], stuck=len(outcome.get("stuck") or []))
            # Rolled back: an apply's changes are taken out again; an undo's are put back, so all of it is in place.
            rolled_back = ("Its changes were undone, so nothing of it is in place." if applying
                           else "Nothing was undone, so all of it is still in place.")
            persistent_notification.async_create(
                self.hass, f"Home Assistant stopped while {what} the improvement "
                f"“{display_title(item) if item else ''}”. "
                + (rolled_back if outcome["status"] == "rolled_back"
                   else "Some changes couldn't be undone: " + "; ".join(f"{s['what']} ({s['why']})"
                                                                         for s in outcome["stuck"])),
                title="Assist Improver", notification_id=OUTCOME_NOTIFICATION)
            await self._save()
            self._sync()

    async def _save(self):
        async with timed("store.improvements"):
            await self.store.async_save({"improvements": self.items})

    def get(self, improvement_id):
        if improvement_id not in self.items:
            raise CaseError("Unknown improvement")
        return deepcopy(self.items[improvement_id])

    def feedback(self):
        """The household's decisions on earlier improvements, for the generator and reviewer: each decided one's
        title, changes, latest decision (with its note and reason) and status."""
        return [{"title": item["title"], "changes": deepcopy(item["changes"]), "decision": last["decision"],
                 "note": last.get("note") or "", "reason": last.get("reason"), "status": item["status"]}
                for item in self.items.values() if item.get("decisions") for last in [item["decisions"][-1]]]

    async def write_copy(self, entity_id, improvement_ids):
        """The editor writes the headline and what-you-get for these improvements, which then go up for review. Returns
        the ids whose write-up failed: they keep no copy, and the flow retries them and holds their conversation
        (SPEC.md P0.8). One that fails unexpectedly is a problem of the run's, and the others go on."""
        from .editor import write
        failed = []
        for improvement_id in improvement_ids:
            item = self.items[improvement_id]
            async with contained("write", improvement_id) as one:
                async with span("write", improvement_id=improvement_id) as section:
                    copy = contracts.check(  # What the review shows: its contract.
                        await write(self.hass, item, entity_id), records.Copy, "The write-up")
                    section.tag(status=copy["status"])
            if one.failed or copy["status"] == "failed":
                failed.append(improvement_id)
            else:
                item["copy"] = copy
        await self._save()
        self._sync()  # The notification names improvements by their headlines.
        return failed

    def catalog(self, asking=None, detailed=False):
        """What the roles check before calling a goal fixed already or proposing a fix (their list_improvements tool):
        improvements waiting for the household's review (written up or not) and those applied (with when), each with
        its cause, changes, devices, the conversation it came from and the goals its own test fixed. The asking
        conversation's own are left out (SPEC.md P0.12: a duplicate is another conversation's improvement). detailed:
        each also carries what read_improvement shows."""
        return [self.described(item, detailed) for item in self.items.values()
                if item["status"] in (*PENDING, "applied") and item.get("conversation") != asking]

    def described(self, item, detailed=False):
        """One improvement as the roles read it (catalog)."""
        applied = item["status"] == "applied"
        return {"improvement": item["id"], "title": display_title(item),
                "status": "applied" if applied else "waiting for review",
                **({"applied": item["application"]["at"]} if applied and item.get("application") else {}),
                "cause": item.get("cause") or "", "changes": deepcopy(item["changes"]),
                "conversation": (item.get("conversation") or "")[:8], "goals": list(item["fixes"]),
                "devices": self._devices(item), **({"details": self._details(item)} if detailed else {})}

    @staticmethod
    def _details(item):
        """One improvement in full, for read_improvement: what it changes and expects, the sessions it agrees and
        disagrees with, its test (the conversation before, the replay after, what it made better), and the household's
        decisions on it."""
        hypothesis, copy, shown = item.get("hypothesis") or {}, item.get("copy") or {}, row(item["test"])
        return {"headline": copy.get("headline"), "benefit": copy.get("benefit"), "cause": item.get("cause") or "",
                "expected": hypothesis.get("expected"), "changes": deepcopy(item["changes"]),
                "support": deepcopy(hypothesis.get("support") or []),
                "conflicts": deepcopy(hypothesis.get("conflicts") or []),
                "tested_on": [{"gains": list(item["test"]["gains"]),
                               **{k: v for k, v in shown.items() if k != "failed_steps"}}],
                "decisions": [{k: d.get(k) for k in ("decision", "note", "reason") if d.get(k)}
                              for d in item.get("decisions", [])]}

    @staticmethod
    def _devices(item):
        """The devices an improvement touches: those its changes name, and those the goals it fixes want."""
        found = set(item.get("devices") or [])
        for change in item.get("changes", []):
            found.update(e for e in [change.get("entity_id"), *(s.get("entity_id") for s in change.get("steps") or [])]
                         if isinstance(e, str))
        return sorted(found)

    def pending(self):
        """Improvements waiting on the household: written up, so each reads in plain words."""
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING and i.get("copy")]

    def held(self, improvement_id):
        """Whether its round's write-up still holds it (unannounced): written up, but not up for review yet."""
        return improvement_id in self._unannounced and (self.items.get(improvement_id) or {}).get("status") in PENDING

    def unwritten(self):
        """Improvements waiting for the editor (the write-up stage's backlog), not yet up for review."""
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING and not i.get("copy")]

    def listed(self):
        """Every improvement up for review or decided, those waiting on the user first, then the most recent. One its
        round's write-up still holds (unannounced) isn't up for review yet."""
        shown = sorted((deepcopy(i) for i in self.items.values()
                        if (i["status"] not in PENDING or i.get("copy")) and not self.held(i["id"])),
                       key=lambda i: i["created"], reverse=True)
        return sorted(shown, key=lambda i: (i["status"] not in PENDING, i["status"] != "applied"))

    async def build_for(self, hypothesis, test, recording_id, round_number, checked_on=(), revises=None):
        """The improvement for an idea the proposer sent on (hypothesis), with both its stamps, for the one
        conversation it came from and was tested on (SPEC.md P0.10), in that conversation's round: a verbatim copy of
        the round's test that validated its changes, the root goals that test helped (measures.helped) and the devices
        they want. revises: the improvement the household sent back for this round (a revision, or a conflict), which
        this new version beside it replaces: marked revised while still pending or in conflict. Its id."""
        from .measures import helped
        improvement_id = digest({"changes": test["changes"], "conversation": recording_id, "round": round_number})[:16]
        roots = helped(test["before"], test["after"], test["judged"])
        built = {"id": improvement_id, "title": hypothesis.get("title") or "Improvement",
                 "cause": hypothesis.get("cause") or "", "hypothesis": deepcopy(hypothesis),
                 "changes": deepcopy(hypothesis["changes"]), "test": deepcopy(test), "round": round_number,
                 "conversation": recording_id,
                 "fixes": [f"{recording_id[:8]}:{test['judged'][root]['turn']}" for root in roots],
                 "devices": sorted({e for root in roots for e in test["judged"][root]["devices"]}),
                 "setup": deepcopy(test["setup"]), "tested": deepcopy(test["tested"]),
                 "provider": deepcopy(test["provider"]), "user_ids": [test["user_id"]],
                 "reach": [reach(change) for change in hypothesis["changes"]], "checked_on": list(checked_on)}
        if improvement_id in self.items:
            self.items[improvement_id].update(built)
        else:
            self.items[improvement_id] = {**built, "status": "awaiting_review", "created": utcnow().isoformat(),
                                          "decisions": [], "application": None, "history": []}
            if (old := self.items.get(revises)) is not None:
                last = old["decisions"][-1] if old["decisions"] else {}
                self.items[improvement_id]["revises"] = {
                    "id": old["id"], "title": old["title"],
                    "note": (last.get("note") or "") if last.get("decision") == "revise" else "",
                    **({"conflict": True} if old["status"] == "conflict" else {})}  # Sent back after its conflict.
                if old["status"] in (*PENDING, "conflict"):
                    old.update(status="revised", revised_by=improvement_id)
        contracts.check(self.items[improvement_id], records.Improvement, "The improvement")  # What Write-up reads.
        await self._save()
        self._sync()
        return improvement_id

    async def up_again(self, improvement_id):
        """One whose accept met a conflict, sent back by the household, when its new round brought no new version:
        back up for review (P0.6), for the household to decide on again; its review shows what conflicts now."""
        self.items[improvement_id]["status"] = "awaiting_review"
        await self._save()
        self._sync()

    @asynccontextmanager
    async def unannounced(self, improvement_ids):
        """While auto-accept has yet to take what it can of these (DETAILS P12.9), the notification and the sidebar's
        count leave them out: what it applies never waited on the household. One already announced (written up in an
        earlier try) stays listed. Whatever happens meanwhile (a write-up that fails, a run cut off), those still
        waiting are announced after."""
        ids = set(improvement_ids) - self._unannounced - (self._notified or set())
        if not ids:
            yield
            return
        self._unannounced |= ids
        try:
            yield
        finally:
            self._unannounced -= ids
            self._sync()

    def _sync(self):
        """The notification lists the improvements waiting on the user, each linking to its review in the panel."""
        wanted = {i["id"]: i for i in self.pending() if i["id"] not in self._unannounced}
        from .panel import set_count
        set_count(self.hass, len(wanted))  # The count beside the panel in the sidebar.
        if self._notified != set(wanted):
            if wanted or self._notified:
                journal("info", f"Notification lists {len(wanted)} improvement(s)" if wanted
                        else "Notification dismissed: nothing waits for review", improvements=sorted(wanted))
            self._notified = set(wanted)
        if not wanted:
            persistent_notification.async_dismiss(self.hass, NOTIFICATION)
            return
        # Each links straight to its review in the panel.
        from .panel import URL_PATH, review_url
        count = len(wanted)
        titles = "\n".join(f"- [{display_title(i)}]({review_url(i['id'])})" for i in wanted.values())
        these = "these improvements" if count > 1 else "this improvement"
        persistent_notification.async_create(
            self.hass, f"{titles}\n\nReview {these} in [Assist Improver](/{URL_PATH}).",
            title=f"{count} Improvement{'s' if count > 1 else ''} identified", notification_id=NOTIFICATION)

    async def decide(self, improvement_id, decision, *, note="", user_id=None, reason=None, auto=False,
                     revising=False):
        """Record a decision (with its reason, if one is given: REASONS) and act on it, one at a time per improvement
        (the second waits, then sees the first's result). Returns the improvement, with `outcome` saying what
        happened. auto: accepted automatically, only while what it writes now is low-risk (CaseError otherwise, with
        nothing recorded). revising: its revision's round runs (choices)."""
        async with self._locks.setdefault(improvement_id, asyncio.Lock()):
            return await self._decide(improvement_id, decision, note=note, user_id=user_id, reason=reason, auto=auto,
                                      revising=revising)

    async def settled(self, improvement_id):
        """Returns once no decision on it is under way: decisions take their turn (decide), so one under way has
        finished."""
        async with self._locks.setdefault(improvement_id, asyncio.Lock()):
            pass

    async def _decide(self, improvement_id, decision, *, note="", user_id=None, reason=None, auto=False,
                      revising=False):
        item = self.get(improvement_id)
        # Not while its round's write-up holds it (unannounced): it isn't up for review yet.
        if decision not in choices(item, revising) or (not auto and self.held(improvement_id)):
            raise CaseError("That decision isn't open for this improvement now")
        if reason is not None and reason not in reasons(decision):
            raise CaseError("That reason doesn't go with this decision")
        if decision == "revise" and not note.strip():
            raise CaseError("Say what to change in the note")
        record = {"decision": decision, "note": note.strip(), **({"reason": reason} if reason else {}), "by": user_id,
                  "at": utcnow().isoformat(), **({"auto": True} if auto else {}),
                  # Taken before it was announced as waiting (unannounced): it never went up for review.
                  **({"unannounced": True} if auto and improvement_id in self._unannounced else {})}
        if decision == "accept":
            outcome = await self._apply(item, user_id, auto)
            if auto and outcome["status"] in ("conflict", "rolled_back", "busy"):  # Nothing changed, nothing decided:
                # up for review, for the household (P12.9.11).
                journal("warning", f"Auto-accept: {outcome['status']}", improvement_id=improvement_id,
                        conflicts=len(outcome.get("conflicts") or []) or None)
                self.items[improvement_id] = item
                await self._save()
                self._sync()
                return {**deepcopy(item), "outcome": outcome}
        elif decision == "undo":
            outcome = await self._undo(item)
        else:  # Declined; or a revision, which leaves it up for review until a revised version replaces it (P0.6).
            if decision == "decline":
                item["status"] = "declined"
            outcome = {"status": "recorded"}
        item["decisions"].append(contracts.check({**record, "outcome": outcome["status"]}, records.Decision,
                                                 "The decision"))
        item["last_outcome"] = {**outcome, "at": record["at"]}  # Shown on the review page for a short while.
        journal("info" if outcome["status"] in ("done", "recorded") else "warning",
                f"Decision {decision}: {outcome['status']}", improvement_id=improvement_id, decision=decision,
                outcome=outcome["status"], user_id=user_id, steps=len(outcome.get("steps") or []) or None,
                conflicts=len(outcome.get("conflicts") or []) or None)
        if decision == "undo" and improvement_id in self.items:  # Its record as it is now: another undo may have
            item["application"] = self.items[improvement_id].get("application")  # handed it a line meanwhile.
        self.items[improvement_id] = item
        await self._save()
        self._sync()
        return {**deepcopy(item), "outcome": outcome}

    async def auto_accept(self, improvement_ids):
        """Accept low-risk improvements as soon as they're written up (the "auto-accept low-risk changes" setting):
        each applied all or nothing, recorded as accepted automatically, with a notification to review or undo it.
        Anything else, or one that can't be applied, stays up for review (one that fails unexpectedly too: a problem of
        the run's, and the others go on). Returns how many applied."""
        from .panel import review_url
        applied = 0
        for improvement_id in improvement_ids:
            item = self.items.get(improvement_id)
            if (not item or item["status"] not in PENDING or "accept" not in choices(item)
                    or not low_risk(self.hass, item)
                    or item.get("decisions")):  # The household decided on it already (a revision asked; undone).
                continue
            if switched_off(self.hass) and any(household_line(c) for c in item["changes"]):
                continue  # Household knowledge is off: it waits for review (it can't apply now, and says why).
            # CaseError: the household decided it meanwhile (their decision held the lock first), or what it would
            # write now isn't low-risk (a household line taking a line out of an assistant's own instructions).
            async with contained("write", improvement_id, step="accepting", expected=(CaseError,)) as one:
                result = await self.decide(improvement_id, "accept", note="Accepted automatically", auto=True)
            if one.failed:
                continue
            if result["status"] == "applied":
                applied += 1
                headline = (item.get("copy") or {}).get("headline") or item["title"]
                persistent_notification.async_create(
                    self.hass, f"Applied automatically: **{headline}**. [Review or undo it]({review_url(improvement_id)})",
                    title="Assist Improver", notification_id=auto_notification(improvement_id))
                journal("info", "Applied automatically; notified", improvement_id=improvement_id)
        await self._save()
        return applied

    async def _apply(self, item, user_id, auto=False):
        steps, already, conflicts = await plan_steps(self.hass, item)
        if conflicts:  # Home Assistant changed since it was tested: the household may send it back (P0.7).
            if not auto:  # Auto-accept's leaves it up for review, the conflict shown there (DETAILS P12.9.12).
                item["status"], item["conflicts"] = "conflict", deepcopy(conflicts)
            return {"status": "conflict", "conflicts": conflicts}
        if auto and not low_risk(self.hass, item, steps):  # Up for review instead: someone decides.
            raise CaseError("Not low-risk as it would apply now")
        try:
            result = await self.changesets.run(item["id"], "apply", steps)
        except Conflict as err:
            return {"status": "busy", "error": str(err)}
        if result["status"] == "done":
            item["status"] = "applied"
            item["application"] = {"at": utcnow().isoformat(), "by": user_id, "steps": result["steps"],
                                   "already": already, "scripts_executed": 0,
                                   "user_ids": item["user_ids"]}  # Whom its lines for the person speaking went to.
        elif result["status"] == "needs_attention":
            item["status"] = "needs_attention"
        item["history"].append({"at": utcnow().isoformat(), "event": "apply", **result})
        return {**result, "already": already}

    async def _undo(self, item):
        applied = item["application"]["steps"]
        live = {(s["kind"], s["target"]): await read(self.hass, s["kind"], s["target"]) for s in applied}
        steps, left = readable(self.hass, *undo_steps(applied, live, self._added_by_others(item)))
        try:
            result = await self.changesets.run(item["id"], "undo", steps)
        except Conflict as err:
            return {"status": "busy", "error": str(err)}
        if result["status"] == "done":  # Back up for review: the household decides again (accept, revise, decline).
            item["status"] = "awaiting_review"
            await self._hand_over(left)
        elif result["status"] == "needs_attention":
            item["status"] = "needs_attention"
        if item["status"] != "applied":  # No notice still says it was applied automatically.
            persistent_notification.async_dismiss(self.hass, auto_notification(item["id"]))
        item["history"].append({"at": utcnow().isoformat(), "event": "undo", **result, "left": left})
        return {**result, "left": left}

    def _added_by_others(self, item):
        """What the other applied improvements' own changes added, as they were applied, which undoing this one leaves
        in place (undo_steps' kept): their household lines (each where it went: everyone's, or each person's it was
        applied for), the names they give devices, and the lines they write in an assistant's own instructions."""
        from .household import EMPTY, edit
        from .instructions import is_edit
        out = {}
        for other in self.items.values():
            if other["id"] == item["id"] or other["status"] != "applied":
                continue
            owner = {"id": other["id"], "title": display_title(other)}
            # Whom it gave its lines for the person speaking: as its application says.
            lines, _, _ = edit(EMPTY, other["changes"], other["application"]["user_ids"])
            for line in lines["everyone"]:
                out.setdefault(("household", "household"), {})[("everyone", line)] = owner
            for user_id, mine in lines["people"].items():
                for line in mine:
                    out.setdefault(("household", "household"), {})[(user_id, line)] = owner
            for change in other["changes"]:
                if change.get("kind") == "alias" and (change.get("alias") or "").strip():
                    out.setdefault(("alias", change.get("entity_id")), {})[change["alias"].strip()] = owner
                elif is_edit(change) and (change.get("text") or "").strip():  # Its whole text, as instructions.edit
                    out.setdefault(("prompt", change.get("target")), {})[change["text"].strip("\n")] = owner  # adds it.
        return out

    async def _hand_over(self, left):
        """What an undo kept for another applied improvement is that one's own from now on: its undo takes it out, as
        though it had added it (its step's earlier value without it; a step of its own where it had none)."""
        for entry in left:
            owner = self.items.get(entry.get("owner") or "")
            if owner is None or owner["status"] != "applied" or not owner.get("application"):
                continue
            kind, target, steps = entry["kind"], entry["target"], owner["application"]["steps"]
            step = next((s for s in steps if (s["kind"], s["target"]) == (kind, target)), None)
            if step is None:  # It found the line there already when it was applied: from now on it's its own.
                now = await read(self.hass, kind, target)
                step = {"kind": kind, "target": target, "what": entry["what"], "before": now, "after": now}
                steps.append(step)
                steps.sort(key=lambda s: ORDER.index(s["kind"]))
            key = (entry["whose"], entry["kept"]) if "whose" in entry else entry["kept"]
            step["before"] = without(kind, step["before"], [key])




def reach(change):
    """What one change applies to, beyond the conversation it came from (SPEC.md P0.10): a script, an alias or what's
    exposed to Assist, the whole household; an assistant's instructions, every conversation with that assistant; a
    line of household knowledge, everyone, or the one person it's for."""
    kind = change.get("kind")
    if kind == "prompt":
        return {"kind": kind, "applies_to": "assistant", "assistant": change.get("assistant")}
    if kind == "household" and change.get("for") not in (None, "everyone"):
        return {"kind": kind, "applies_to": "person", "person": change.get("for")}
    return {"kind": kind, "applies_to": "household"}


KINDS = {"script": "script", "alias": "aliases", "exposure": "exposure", "prompt": "instructions",
         "household": "household", "assistant_api": "assistant"}


def display_title(item):
    """What the household reads as the improvement's name: the editor's headline, else the generator's title."""
    return ((item.get("copy") or {}).get("headline") or item["title"]).strip()


def row(test):
    """A test as the screens show it (the review, the editor, read_improvement): the root goals it helped, whether it
    validated, the turns those whose turns compare took (measures.compared), the goals newly met, the side effects
    left without the change and those gone with it, what the household objected to that the change fixed (feedback:
    their words), what it still does (open: their words, the first reply that does it, by its place among the replies with the
    change, and the judge's reason) and
    what couldn't be told (set_aside), each goal it helped (goals; with home_couldnt, its devices when the home
    couldn't do it either way), the conversation as it went beside its replay with the change (with the steps that
    failed in each turn), and the turns no longer needed."""
    from .measures import compared, helped
    before, after, judged = test["before"], test["after"], test["judged"]
    roots = set(before["turns_per_root_goal"])
    both = compared(after, before)
    newly = [r for r in roots if after["goals"].get(r) == "met" and before["goals"].get(r) != "met"]
    gone = {s["entity_id"] for s in before["side_effects_left"]} - {s["entity_id"] for s in after["side_effects_left"]}
    followed = [goal.get("quote") or "" for key, goal in judged.items() if goal.get("kind") == "conduct"
                and before["goals"].get(key) != "met" and after["goals"].get(key) == "met"]
    conduct = after.get("conduct") or {}
    replied = {r["turn"]: n for n, r in enumerate(after["replies"], 1)}  # A reply's place among those said with it.
    objected = [{"quote": judged[key].get("quote") or "", "reply": replied.get(shown["turn"]), "why": shown["why"]}
                for key, found in conduct.items() if found.get("state") == "unmet" and key in judged
                for shown in [next((a for a in found.get("answers") or [] if a["shows"]), {"turn": None, "why": ""})]]
    recorded, roots_helped = test["recorded"], helped(before, after, judged)
    counted = {n for turns in before["counted"].values() for n in turns}
    still = {n for turns in after["counted"].values() for n in turns}
    return {"summary": "; ".join(judged[r]["summary"] or "" for r in roots_helped),
            "goals": [{"goal": judged[r]["summary"] or "", **({"home_couldnt": judged[r]["devices"]} if judged[r].get(
                "home_couldnt") and after["goals"].get(r) != "met" else {})} for r in roots_helped],
            "improved": test["validated"],
            "turns_before": sum(before["turns_per_root_goal"][r] for r in both),
            "turns_after": sum(after["turns_per_root_goal"][r] for r in both),
            "newly_met": [judged[r]["summary"] for r in newly],
            "side_effects": [{k: s[k] for k in ("entity_id", "name", "start")} for s in before["side_effects_left"]],
            "side_effects_gone": sorted(gone), "feedback": list(dict.fromkeys(followed)), "open": objected,
            "set_aside": [judged[key].get("quote") or "" for key, found in conduct.items()
                          if found.get("set_aside") and key in judged],
            "before": [{"you": t["you"], "assistant": t["assist"]} for t in recorded],
            "after": [{"you": r["you"], "assistant": r["assistant"]} for r in after["replies"]],
            "failed_steps": {"before": [t.get("failed", []) for t in recorded],
                             "after": [r.get("failed", []) for r in after["replies"]]},
            "no_longer_needed": [f"“{recorded[n - 1]['you']}”" for n in sorted(counted - still)
                                 if n <= len(recorded)]}


def benefits(item):
    """(goals now met, turns no longer needed): what accepting gets the user, by its test."""
    shown = row(item["test"])
    return shown["newly_met"], shown["no_longer_needed"]


def spared(item):
    """(side effects gone, feedback followed), by its test: the devices left as they were ({name, start}), and what the
    household asked of Assist."""
    shown = row(item["test"])
    return ([{"name": s["name"], "start": s["start"]} for s in shown["side_effects"]
             if s["entity_id"] in shown["side_effects_gone"]], shown["feedback"])
