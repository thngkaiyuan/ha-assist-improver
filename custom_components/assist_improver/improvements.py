"""Tested improvements awaiting the user's decision: one list, reviewed in the panel (panel.py).

An improvement is one idea's changes with the test that validated them on the conversation it came from (SPEC.md
P0.10): built by the flow when the proposer sends the idea on (build_for), a verbatim copy of its round's test. The
screens read one row projected from that test (row). A decision goes through `decide`, which updates the list and the
notification together.
"""

import asyncio
from copy import deepcopy

from homeassistant.components import persistent_notification
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .analyzer import REACTIONS, couldnt
from .cases import CaseError, digest
from .changes import referenced
from .changeset import ORDER, Changesets, Conflict, plan_steps, read, readable, undo_steps, without
from .const import DOMAIN, STORAGE_VERSION
from .failures import contained
from . import contracts, editor, evidence, records
from .household import EMPTY, edit
from .instructions import is_edit
from .measures import compared, helped
from .records import Improvement
from .trace import journal, span, timed

PENDING = ("awaiting_review", "needs_attention")  # Waiting on the user: in the notification.
# What moves something physical whose motion a replay takes as done once its command succeeds (DETAILS P11.5): by
# domain, and a cover by its device class. The review notes it (panel.py, moves).
MOVERS = ("valve", "lock", "alarm_control_panel")
DOORS = ("garage", "door", "gate")  # A cover's device classes that move a door.


def physical(hass, entity_id):
    """Whether a device moves something physical: a valve, lock or alarm panel, or a garage, door or gate cover (its
    device class as Home Assistant has it: its state's, else its registry entry's)."""
    domain = (entity_id or "").split(".", 1)[0]
    if domain in MOVERS:
        return True
    if domain != "cover" or hass is None:
        return False
    state = hass.states.get(entity_id)
    device_class = state.attributes.get("device_class") if state is not None else None
    if device_class is None and (entry := er.async_get(hass).async_get(entity_id)) is not None:
        device_class = entry.device_class or entry.original_device_class
    return device_class in DOORS


def sentence(text):
    """Text as a sentence of its own (an error's words): ending as a sentence ends, never inside parentheses."""
    text = (text or "").strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def stuck_text(stuck):
    """What couldn't be put back after a failure, each with why: "{what}: {why}." """
    return " ".join(f"{s['what']}: {sentence(s['why'])}" for s in stuck)


def moves(hass, item):
    """Whether an improvement involves motion a replay takes as done once its command succeeds (DETAILS P11.5): a
    device it involves moves something physical (physical), or a goal it helped is a sequence goal (P9.7.5: presses
    standing for a motion nobody can check, such as a relay-driven door's)."""
    test = item.get("test") or {}
    judged = test.get("judged") or {}
    roots = set(helped(test["before"], test["after"], judged)) if judged else set()
    return any(physical(hass, e) for e in item.get("devices") or []) or any(
        g.get("kind") == "sequence" and g.get("root") in roots for g in judged.values())


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
        self._locks = {}  # One decision at a time per improvement: two tabs never cross.
        self._notified = None  # What the notification listed last (journaled when it changes).
        # Whether its revision's round runs (flow.Flow.revising, once the flow is loaded): then it waits on the improver.
        self.revising = lambda improvement_id: False
        # The questions waiting for the household (flow.Flow.questions, once the flow is loaded): {conversation: it}.
        self.asking = lambda: {}

    async def start(self):
        # An improvement belongs to the one conversation it came from (SPEC.md P0.10); one that names none is an
        # earlier version's, and starts fresh: what it applied stays in Home Assistant's configuration, without undo.
        saved = ((await self.store.async_load()) or {}).get("improvements", {})
        self.items = {iid: item for iid, item in saved.items() if item.get("conversation")}
        await self.changesets.load()  # A change left open refuses others until it's finished (recover).
        await self.changesets.hold()  # Any automation it saved, off before HA arms triggers.
        self._sync()

    async def recover(self, editors=None, late=None):
        """A change left open (HA stopped in the middle of it, or the panel went away) is undone from where it stands:
        once HA has started, as far as the improver writes itself; its scripts, automations and scenes by the panel
        (editors: its saver) when it's next opened, once no save a panel didn't answer in time may still land (late).
        Once final, the outcome is in the improvement's history and a notification (message), and what the
        improvement's own review says (brief). Returns it: None if none was open; status waiting while an editor item
        waits for the panel."""
        outcome = await self.changesets.resume(editors, late)
        if outcome is None or outcome["status"] == "waiting":
            if outcome:
                journal("info", "An interrupted change waits for the panel to finish its editor items",
                        improvement_id=outcome["improvement_id"])
            return outcome
        item = self.items.get(outcome["improvement_id"])
        if item is not None:
            if outcome["status"] == "needs_attention":
                item["status"] = "needs_attention"
            item.setdefault("history", []).append({"at": utcnow().isoformat(), "event": "interrupted", **outcome})
        applying = outcome["action"] == "apply"
        what = "Applying" if applying else "Undoing"
        journal("warning", f"{what} an improvement was interrupted: {outcome['status']}",
                improvement_id=outcome["improvement_id"], stuck=len(outcome.get("stuck") or []))
        # Rolled back: an apply's changes are taken out again; an undo's are put back, so all of it is in place.
        rolled_back = ("Its changes were undone, so nothing of it is in place." if applying
                       else "Nothing was undone, so all of it is still in place.")
        subject = f"{what} the improvement “{display_title(item) if item else ''}”"
        said = (rolled_back if outcome["status"] == "rolled_back"
                else f"Some changes couldn't be undone. {stuck_text(outcome['stuck'])}")
        message = f"{subject} was interrupted. {said}"
        persistent_notification.async_create(self.hass, message, title="Assist Improver",
                                             notification_id=OUTCOME_NOTIFICATION)
        await self._save()
        self._sync()
        return {**outcome, "message": message, "brief": f"It was interrupted. {said}"}  # Under its title: not again.

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
        failed = []
        for improvement_id in improvement_ids:
            item = self.items[improvement_id]
            async with contained("write", improvement_id) as one:
                async with span("write", improvement_id=improvement_id) as section:
                    copy = contracts.check(  # What the review shows: its contract.
                        await editor.write(self.hass, item, entity_id), records.Copy, "The write-up")
                    section.tag(status=copy["status"])
            if one.failed or copy["status"] == "failed":
                failed.append(improvement_id)
            else:
                item["copy"] = copy
        await self._save()
        self._sync()  # The notification names improvements by their headlines.
        return failed

    async def add_weeks(self, improvement_ids, recorder):
        """Each improvement's simulated week (add_week), side by side: a run waits about one week's budget, not one for
        each."""
        await asyncio.gather(*(self.add_week(improvement_id, recorder) for improvement_id in improvement_ids))

    async def add_week(self, improvement_id, recorder):
        """An improvement that changes an automation gets its simulated week (evidence.improvement_week), once, for its
        review (weeks.py): before it's written up. One that couldn't run is said so, and the improvement goes on."""
        item = self.items[improvement_id]
        setup = item.get("setup") or {}
        if "week" in item or not (setup.get("automations") or setup.get("automation_on")):
            return
        async with span("week", improvement_id=improvement_id) as section:
            try:
                item["week"] = await evidence.improvement_week(self.hass, recorder, item)
            except Exception as err:  # noqa: BLE001 - the week informs the review; it never holds the improvement.
                item["week"] = {"failed": str(err) or type(err).__name__, "automations": []}
            if failed := item["week"].get("failed"):
                journal("warning", "An automation's week couldn't run", improvement_id=improvement_id, error=failed)
            section.tag(failed=bool(failed))
        await self._save()

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
        """The devices an improvement touches: those its changes touch (build_for), and those the goals it fixes
        want."""
        return sorted(item.get("devices") or [])

    def pending(self):
        """Improvements up for review: written up, so each reads in plain words."""
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING and i.get("copy")]

    def waiting(self):
        """The improvements waiting on the household, beside its questions in the notification and the sidebar's
        count: up for review, but not one being revised, which waits on the improver (DETAILS P12.8.6)."""
        return [i for i in self.pending() if not self.revising(i["id"])]

    def count(self):
        """The sidebar's count: the questions and the improvements waiting on the household."""
        return len(self.asking()) + len(self.waiting())

    def refresh(self):
        """What waits on the household changed outside the improvements (a question asked or gone, a revision's round
        started or ended): the notification and the sidebar's count follow."""
        self._sync()

    def unwritten(self):
        """Improvements waiting for the editor (the write-up stage's backlog), not yet up for review."""
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING and not i.get("copy")]

    def listed(self):
        """Every improvement up for review or decided, those waiting on the user first, then the most recent."""
        shown = sorted((deepcopy(i) for i in self.items.values() if i["status"] not in PENDING or i.get("copy")),
                       key=lambda i: i["created"], reverse=True)
        return sorted(shown, key=lambda i: (i["status"] not in PENDING, i["status"] != "applied"))

    async def build_for(self, hypothesis, test, recording_id, round_number, checked_on=(), revises=None):
        """The improvement for an idea the proposer sent on (hypothesis), with both its stamps, for the one
        conversation it came from and was tested on (SPEC.md P0.10), in that conversation's round: a verbatim copy of
        the round's test that validated its changes, the root goals that test helped (measures.helped) and the devices
        they want. revises: the improvement the household sent back for this round (a revision, or a conflict), which
        this new version beside it replaces: marked revised while still pending or in conflict. Its id."""
        improvement_id = digest({"changes": test["changes"], "conversation": recording_id, "round": round_number})[:16]
        roots = helped(test["before"], test["after"], test["judged"])
        touched = referenced(self.hass, hypothesis["changes"], {})["entities"]  # As Home Assistant reads them.
        built = {"id": improvement_id, "title": hypothesis.get("title") or "Improvement",
                 "cause": hypothesis.get("cause") or "", "hypothesis": deepcopy(hypothesis),
                 "changes": deepcopy(hypothesis["changes"]), "test": deepcopy(test), "round": round_number,
                 "conversation": recording_id,
                 "fixes": [f"{recording_id[:8]}:{test['judged'][root]['turn']}" for root in roots],
                 "devices": sorted(touched | {e for root in roots for e in test["judged"][root]["devices"]}),
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

    async def unrevised(self, improvement_id, note, why):
        """A revision of this one whose round brought no new version: the note it asked with and why not (the
        proposer's reason), shown with it as it stays up for review (P0.6), until the household decides again."""
        self.items[improvement_id]["unrevised"] = {"note": note, "why": why}
        await self._save()

    def _sync(self):
        """The notification lists what waits on the user: the questions first, each linking to the panel's home page,
        where it's answered; then the improvements, each linking to its review in the panel."""
        asked, wanted = self.asking(), {i["id"]: i for i in self.waiting()}
        from .panel import set_count
        set_count(self.hass, len(asked) + len(wanted))  # The count beside the panel in the sidebar.
        if self._notified != {*asked, *wanted}:
            if asked or wanted or self._notified:
                journal("info", f"Notification lists {len(asked)} question(s), {len(wanted)} improvement(s)"
                        if asked or wanted else "Notification dismissed: nothing waits for the user",
                        questions=sorted(asked), improvements=sorted(wanted))
            self._notified = {*asked, *wanted}
        if not asked and not wanted:
            persistent_notification.async_dismiss(self.hass, NOTIFICATION)
            return
        from .panel import URL_PATH, review_url
        home, count, s = f"/{URL_PATH}", len(wanted), lambda n: "s" if n > 1 else ""  # noqa: E731
        message = "\n".join(f"- [{display_title(i)}]({review_url(i['id'])})" for i in wanted.values())
        if wanted:  # Each links straight to its review in the panel; then where to review them.
            these = "these improvements" if count > 1 else "this improvement"
            message += f"\n\nReview {these} in [Assist Improver]({home})."
        title = ", ".join([f"{len(asked)} question{s(len(asked))} for you"] * bool(asked)
                          + [f"{count} improvement{s(count)} to review"] * bool(wanted))  # The home page's words.
        if asked:  # Above the improvements; another bullet makes a list of their own.
            questions = "\n".join(f"* [{' '.join(q.split())}]({home})" for q in asked.values())
            message = "\n\n".join(filter(None, [questions, message]))
        persistent_notification.async_create(self.hass, message, title=title, notification_id=NOTIFICATION)

    async def decide(self, improvement_id, decision, *, note="", user_id=None, reason=None, revising=False,
                     editors=None):
        """Record a decision (with its reason, if one is given: REASONS) and act on it, one at a time per improvement
        (the second waits, then sees the first's result). Returns the improvement, with `outcome` saying what
        happened. revising: its revision's round runs (choices). editors: the panel's saver
        (panel.EditorChannel.save), which writes its scripts, automations and scenes; without it, a change with one is
        refused. NoPanel: the panel went away mid-change, which is left open (recover), with nothing recorded."""
        async with self._locks.setdefault(improvement_id, asyncio.Lock()):
            return await self._decide(improvement_id, decision, note=note, user_id=user_id, reason=reason,
                                      revising=revising, editors=editors)

    async def settled(self, improvement_id):
        """Returns once no decision on it is under way: decisions take their turn (decide), so one under way has
        finished."""
        async with self._locks.setdefault(improvement_id, asyncio.Lock()):
            pass

    async def _decide(self, improvement_id, decision, *, note="", user_id=None, reason=None, revising=False,
                      editors=None):
        item = self.get(improvement_id)
        if decision not in choices(item, revising):
            raise CaseError("That decision isn't open for this improvement now")
        if reason is not None and reason not in reasons(decision):
            raise CaseError("That reason doesn't go with this decision")
        if decision == "revise" and not note.strip():
            raise CaseError("Say what to change in the note")
        item.pop("unrevised", None)  # Decided again: what an earlier revision brought no longer stands.
        record = {"decision": decision, "note": note.strip(), **({"reason": reason} if reason else {}), "by": user_id,
                  "at": utcnow().isoformat()}
        if decision == "accept":
            outcome = await self._apply(item, user_id, editors)
        elif decision == "undo":
            outcome = await self._undo(item, editors)
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

    async def _apply(self, item, user_id, editors=None):
        steps, already, conflicts = await plan_steps(self.hass, item)
        if conflicts:  # Home Assistant changed since it was tested: the household may send it back (P0.7).
            item["status"], item["conflicts"] = "conflict", deepcopy(conflicts)
            return {"status": "conflict", "conflicts": conflicts}
        try:
            result = await self.changesets.run(item["id"], "apply", steps, editors)
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

    async def _undo(self, item, editors=None):
        applied = item["application"]["steps"]
        live = {(s["kind"], s["target"]): await read(self.hass, s["kind"], s["target"]) for s in applied}
        steps, left = readable(self.hass, *undo_steps(applied, live, self._added_by_others(item)))
        try:
            result = await self.changesets.run(item["id"], "undo", steps, editors)
        except Conflict as err:
            return {"status": "busy", "error": str(err)}
        if result["status"] == "done":  # Back up for review: the household decides again (accept, revise, decline).
            item["status"] = "awaiting_review"
            await self._hand_over(left)
        elif result["status"] == "needs_attention":
            item["status"] = "needs_attention"
        item["history"].append({"at": utcnow().isoformat(), "event": "undo", **result, "left": left})
        return {**result, "left": left}

    def _added_by_others(self, item):
        """What the other applied improvements' own changes added, as they were applied, which undoing this one leaves
        in place (undo_steps' kept): their household lines (each where it went: everyone's, or each person's it was
        applied for), the names they give devices, and the lines they write in an assistant's own instructions."""
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
                if change.get("kind") == "registry" and change.get("entity_id"):  # The names it gives a device.
                    for name in change.get("aliases") or []:
                        out.setdefault(("alias", change["entity_id"]), {})[name] = owner
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
    """What one change applies to, beyond the conversation it came from (SPEC.md P0.10): an editor item or a
    registry entry, the whole household; an assistant change, every conversation with that assistant; a line of
    household knowledge, everyone, or the one person it's for."""
    kind = change.get("kind")
    if kind == "assistant":
        return {"kind": kind, "applies_to": "assistant", "assistant": change.get("assistant")}
    if kind == "preference" and change.get("for") not in (None, "everyone"):
        return {"kind": kind, "applies_to": "person", "person": change.get("for")}
    return {"kind": kind, "applies_to": "household"}


# Each step's kind (changeset.ORDER) as the review labels it.
KINDS = {"script": "script", "automation": "automation", "automation_on": "automation", "scene": "scene",
         "alias": "aliases", "exposure": "exposure", "entity": "device", "device": "device", "area": "area",
         "prompt": "instructions", "household": "household", "assistant_api": "assistant"}


def display_title(item):
    """What the household reads as the improvement's name: the editor's headline, else the generator's title."""
    return ((item.get("copy") or {}).get("headline") or item["title"]).strip()


def row(test):
    """A test as the screens show it (the review, the editor, read_improvement): the root goals it helped, whether it
    validated, the turns those whose turns compare took (measures.compared), the goals newly met, the parts of the
    others (parts_met) and those done before a step asked again (sooner), the side effects left without the change and
    those gone with it, what the household objected to that the change fixed (feedback: their words), what it still does
    (open: their words, the first reply that does it, by its place among the replies with the change, and the judge's
    reason) and what couldn't be told (set_aside), each goal it helped (goals; with home_couldnt, its devices when the
    home couldn't do it either way, and because: failed or unavailable), those still unmet with it the home could do
    (unmet), the conversation as it went
    beside its replay with the change (with the steps that failed in each turn), and the turns no longer needed."""
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
            "goals": [{"goal": judged[r]["summary"] or "", **({"home_couldnt": judged[r]["devices"], "because": couldnt(
                judged[r]["home_couldnt"])} if judged[r].get("home_couldnt") and after["goals"].get(r) != "met" else {})}
                      for r in roots_helped],
            "unmet": [judged[r]["summary"] or "" for r in roots_helped
                      if after["goals"].get(r) == "unmet" and not judged[r].get("home_couldnt")],
            "improved": test["validated"],
            "turns_before": sum(before["turns_per_root_goal"][r] for r in both),
            "turns_after": sum(after["turns_per_root_goal"][r] for r in both),
            "newly_met": [judged[r]["summary"] for r in newly],
            "parts_met": [g["summary"] or "" for k, g in judged.items() if k != g["root"] and g["root"] not in newly
                          and g.get("kind") not in REACTIONS and after["goals"].get(k) == "met"
                          and before["goals"].get(k) != "met"],
            "sooner": list(dict.fromkeys(judged[g["root"]]["summary"] or "" for k, g in judged.items()
                                         if g.get("kind") == "step" and g["root"] not in newly
                                         and after["goals"].get(k) == "met" and before["goals"].get(k) != "met")),
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


def gains(item):
    """What accepting gets the household, by its test, counted: goals now met (enabled), turns no longer needed
    (removed), side effects gone (spared), what they pointed out fixed (followed)."""
    shown = row(item["test"])
    return {"enabled": len(shown["newly_met"]), "removed": len(shown["no_longer_needed"]),
            "spared": sum(s["entity_id"] in shown["side_effects_gone"] for s in shown["side_effects"]),
            "followed": len(shown["feedback"])}
