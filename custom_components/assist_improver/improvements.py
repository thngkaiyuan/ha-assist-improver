"""Tested improvements awaiting the user's decision: one list, reviewed in the panel (panel.py).

An improvement is one hypothesis's changes with every test of them (one per goal
instance): built by code from the candidates store whenever tests finish. A
decision goes through `decide`, which updates the list and the notification
together.
"""

import asyncio
from copy import deepcopy

from homeassistant.components import persistent_notification
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .cases import CaseError, digest, recording_turns, replayed, transcript
from .changeset import Changesets, Conflict, plan_steps, read, undo_steps
from .const import DOMAIN, STORAGE_VERSION, VERSION
from .records import Improvement
from .trace import journal, span, timed

PENDING = ("awaiting_review", "needs_attention")  # Waiting on the user: in the notification.
# Never changed without the household's review: devices whose misuse is a safety or security problem.
SENSITIVE_DOMAINS = {"lock", "alarm_control_panel", "valve", "siren"}
SENSITIVE_COVERS = {"garage", "door", "gate"}


def sensitive(hass, entity_id):
    """Whether a device is security-sensitive (a lock, an alarm, a garage door...); unknown ones count as sensitive."""
    domain = (entity_id or "").split(".", 1)[0]
    if domain in SENSITIVE_DOMAINS:
        return True
    if domain == "cover":
        state = hass.states.get(entity_id)
        return state is None or state.attributes.get("device_class") in SENSITIVE_COVERS
    return False


def low_risk(hass, item):
    """Whether an improvement may be applied without review: only household lines, and new names for devices that
    aren't security-sensitive. Never anything that lets Assist control more (exposing a device, a new or changed
    script)."""
    changes = item.get("changes") or []
    return bool(changes) and all(
        c.get("kind") in ("preference", "prompt")
        or (c.get("kind") == "alias" and not sensitive(hass, c.get("entity_id"))) for c in changes)
NOTIFICATION = f"{DOMAIN}_improvements"
OUTCOME_NOTIFICATION = f"{DOMAIN}_changes"
DECISIONS = {  # Decision: (status, label).
    "accept": ("applied", "Accept changes"),
    "revise": ("revision_requested", "Ask for a revision"),
    "decline": ("declined", "Decline"),
    "undo": ("undone", "Undo changes"),
    # No longer offered, but kept: the household chose them from the review's earlier menu of six.
    "needs_improvement": ("revision_requested", "The change needs improvement"),
    "fine_as_is": ("declined", "Things were fine as they were"),
    "was_test": ("ignored", "That conversation was a test"),
}
# A reason given with a decision, and the decision it's recorded as. Declined as not an issue (it went fine, or the
# household was only trying Assist out): the trouble the improver saw wasn't real, so it's recorded as fine as it was,
# which the proposer and reviewer are told, and its goals stay settled.
REASONS = {("decline", "not_an_issue"): "fine_as_is"}


def choices(item):
    """The decisions open for an improvement in its status."""
    if item["status"] == "applied":
        return ["undo"]
    if item["status"] in ("undone", "conflict", "declined", "revision_requested", "ignored", "revised"):
        return ["accept"] if item["status"] == "undone" else []
    return ["accept", "revise", "decline"]


def reasons(decision):
    """The reasons that can be given with a decision."""
    return [reason for (given, reason) in REASONS if given == decision]


def _turns(recording):
    return recording_turns(recording, replay=False) if recording.get("turns") else []


def _conversation(recording, first, last):
    """What the user said and Assist replied, turn by turn, as recorded."""
    out = []
    for turn in _turns(recording)[first - 1:last]:
        said = next((m["content"] for m in turn["messages"] if m["role"] == "user"), "")
        replies = [m.get("content") for m in turn["messages"] if m["role"] == "assistant" and m.get("content")]
        out.append({"you": said, "assistant": replies[-1] if replies else ""})
    return out


class Improvements:
    """The list, its decisions and their application; the source of truth for both review screens."""

    def __init__(self, hass, recorder, casebook, candidates):
        self.hass, self.recorder, self.casebook, self.candidates = hass, recorder, casebook, candidates
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.improvements", private=True, atomic_writes=True)
        self.changesets = Changesets(hass)
        self.items: dict[str, Improvement] = {}
        # {plan id: digest of what its test was derived from}: a test derived from the same is reused (refresh).
        self._derived = {}
        self._locks = {}  # One decision at a time per improvement: two tabs, or auto-accept, never cross.
        self._notified = None  # The improvements the notification listed last (journaled when it changes).

    async def start(self):
        saved = await self.store.async_load()
        self.items = (saved or {}).get("improvements", {})
        self._derived = (saved or {}).get("derived", {})
        await self.refresh()

    async def recover(self):
        """Once HA has started: a change HA stopped in the middle of is undone (or flagged as needing attention)."""
        if outcome := await self.changesets.start():
            item = self.items.get(outcome["improvement_id"])
            if item is not None:
                if outcome["status"] == "needs_attention":
                    item["status"] = "needs_attention"
                item.setdefault("history", []).append({"at": utcnow().isoformat(), "event": "interrupted", **outcome})
            what = "applying" if outcome["action"] == "apply" else "undoing"
            journal("warning", f"Home Assistant stopped while {what} an improvement: {outcome['status']}",
                    improvement_id=outcome["improvement_id"], stuck=len(outcome.get("stuck") or []))
            persistent_notification.async_create(
                self.hass, f"Home Assistant stopped while {what} the improvement "
                f"“{display_title(item) if item else ''}”. "
                + ("Its changes were undone, so nothing of it is in place." if outcome["status"] == "rolled_back"
                   else "Some changes couldn't be undone: " + "; ".join(f"{s['what']} ({s['why']})"
                                                                         for s in outcome["stuck"])),
                title="Assist Improver", notification_id=OUTCOME_NOTIFICATION)
            await self._save()
            self._sync()

    async def _save(self):
        async with timed("store.improvements"):
            await self.store.async_save({"improvements": self.items, "derived": self._derived})

    def get(self, improvement_id):
        if improvement_id not in self.items:
            raise CaseError("Unknown improvement")
        return deepcopy(self.items[improvement_id])

    def feedback(self):
        """The household's decisions on earlier improvements, for the generator and reviewer: what was declined, what
        was fine as it was, what to revise (with the note), and what was accepted."""
        out = []
        for item in self.items.values():
            if not item.get("decisions"):
                continue
            last = item["decisions"][-1]
            out.append({"title": item["title"], "changes": deepcopy(item["changes"]), "decision": last["decision"],
                        "note": last.get("note") or "", "status": item["status"]})
        return out

    async def write_copy(self, entity_id, improvement_ids=None):
        """The editor writes the headline and what-you-get for the improvements waiting for it (or the ones given),
        which then go up for review. Returns {improvement id: copy}; an improvement whose copy failed goes up with
        the generator's title."""
        from .editor import write
        out = {}
        waiting = {i["id"] for i in self.unwritten()}
        for item in list(self.items.values()):
            if improvement_ids is not None and item["id"] not in improvement_ids:
                continue
            if improvement_ids is None and item["id"] not in waiting:
                continue
            async with span("write", improvement_id=item["id"]) as section:
                item["copy"] = out[item["id"]] = await write(self.hass, item, entity_id)
                section.tag(status=item["copy"]["status"])
            await self._save()
        self._sync()  # The notification names improvements by their headlines.
        return out

    def settled_goals(self):
        """{"session:turn": title} of goal instances that already have an improvement waiting, applied or decided: a
        run leaves them out. A goal whose improvement the household asked to revise (or that changed since it was
        tested) is open again, with the note."""
        out = {}
        for item in self.items.values():
            if item["status"] in ("revision_requested", "conflict", "revised"):
                continue
            for goal in self._covers(item):
                out.setdefault(goal, item["title"])
        return out

    @staticmethod
    def _covers(item):
        """The goal instances ("session:turn") an improvement covers: tested, untested, and found already addressed."""
        return list(dict.fromkeys([t["goal"] for t in item.get("tests", [])] + list(item.get("untested_goals", []))
                                  + [g["goal"] for g in item.get("also_addressed", [])]))

    def catalog(self, detailed=False):
        """What the proposer and reviewer check before proposing a fix (their list_improvements tool): improvements
        waiting for the household's review (written up or not) and those applied (with when), each with its cause,
        changes and the goals it covers. detailed: each also carries what read_improvement shows."""
        out = []
        for item in self.items.values():
            if item["status"] not in (*PENDING, "applied"):
                continue
            applied = item["status"] == "applied"
            out.append({"improvement": item["id"], "title": (item.get("copy") or {}).get("headline") or item["title"],
                        "status": "applied" if applied else "waiting for review",
                        **({"applied": item["application"]["at"]} if applied and item.get("application") else {}),
                        "cause": item.get("cause") or "", "changes": deepcopy(item["changes"]),
                        "goals": self._covers(item), "devices": self._devices(item),
                        **({"details": self._details(item)} if detailed else {})})
        return out

    @staticmethod
    def _details(item):
        """One improvement in full, for read_improvement: what it changes and expects, the sessions it agrees and
        disagrees with, each test (the conversation before, the replay after, the result), and the goals found
        already addressed by it."""
        hypothesis, copy = item.get("hypothesis") or {}, item.get("copy") or {}
        return {"headline": copy.get("headline"), "benefit": copy.get("benefit"), "cause": item.get("cause") or "",
                "expected": hypothesis.get("expected"), "changes": deepcopy(item["changes"]),
                "support": deepcopy(hypothesis.get("support") or []),
                "conflicts": deepcopy(hypothesis.get("conflicts") or []),
                "tested_on": [{k: deepcopy(t.get(k)) for k in ("goal", "summary", "status", "turns_before",
                                                                "turns_after", "goal_newly_met", "before", "after")}
                              for t in item.get("tests", [])],
                "untested_goals": list(item.get("untested_goals", [])),
                "also_addressed": [{k: g[k] for k in ("goal", "why")} for g in item.get("also_addressed", [])],
                "decisions": [{k: d.get(k) for k in ("decision", "note")} for d in item.get("decisions", [])]}

    def _devices(self, item):
        """The devices an improvement touches: those its changes name, and those its tested goals wanted."""
        found = set()
        for change in item.get("changes", []):
            found.update(e for e in [change.get("entity_id"), *(s.get("entity_id") for s in change.get("steps") or [])]
                         if isinstance(e, str))
        for test in item.get("tests", []):
            try:
                criteria = self._case(test["case_id"])["destination"].get("criteria") or {}
            except (CaseError, KeyError):
                continue
            found.update(criteria.get("states") or criteria.get("sequences") or {})
        return sorted(found)

    async def note_addressed(self, found):
        """Goals the proposer found an existing improvement already addresses ([{session, goal_turn, improvement,
        why}]): kept with that improvement, so later runs leave them out, as they do its own goals."""
        changed = 0
        for entry in found or []:
            item, goal = self.items.get(entry["improvement"]), f"{entry['session']}:{entry['goal_turn']}"
            if item is not None and goal not in self._covers(item):
                item.setdefault("also_addressed", []).append({"goal": goal, "why": entry["why"],
                                                              "at": utcnow().isoformat()})
                changed += 1
        if changed:
            await self._save()
        return changed

    def pending(self):
        """Improvements waiting on the household: written up, so each reads in plain words."""
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING and i.get("copy")]

    def unwritten(self):
        """Improvements waiting for the editor (the write-up stage's backlog), not yet up for review."""
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING and not i.get("copy")]

    def listed(self):
        """Every improvement up for review or decided, those waiting on the user first, then the most recent."""
        shown = sorted((deepcopy(i) for i in self.items.values() if i["status"] not in PENDING or i.get("copy")),
                       key=lambda i: i["created"], reverse=True)
        return sorted(shown, key=lambda i: (i["status"] not in PENDING, i["status"] != "applied"))

    async def refresh(self):
        """Build improvements from the candidates store: each hypothesis with a test that improved a goal. A test
        derived before from the same candidate, case and recording is reused as it was, without reading its recording
        again; only new or changed ones are derived."""
        groups = {}  # One improvement per set of changes (what gets applied), however its idea was worded.
        for item in getattr(self.candidates, "items", {}).values():
            plan = item["plan"]
            if plan.get("kind") == "hypothesis_changes" and item.get("result"):
                groups.setdefault(digest(plan["hypothesis"]["changes"]), []).append(item)
        earlier = {t["plan_id"]: t for i in self.items.values() for t in i.get("tests", []) if t.get("plan_id")}
        sources, built_ids = {}, set()
        for key, items in groups.items():
            # A goal tested again under a newer replay engine (how replays are judged): the newest test stands.
            newest = {}
            for i in sorted(items, key=lambda i: i["plan"]["baseline_plan"].get("replay_engine") or ""):
                newest[self._goal(i["plan"]["evaluation_case_id"])] = i
            items = list(newest.values())
            improved = [i for i in items if i["status"] == "awaiting_review"]
            if not improved:
                continue
            improvement_id = key[:16]
            plan = improved[-1]["plan"]
            existing = self.items.get(improvement_id)
            try:
                derivations = [await self._test(i, earlier)
                               for i in sorted(items, key=lambda i: i["plan"]["evaluation_case_id"])]
            except CaseError:  # A case it was tested on is gone; it can't be shown honestly.
                continue
            tests = [test for test, _ in derivations]
            sources.update({test["plan_id"]: source for test, source in derivations if source is not None})
            hypothesis = plan["hypothesis"]
            built = {"id": improvement_id, "title": hypothesis.get("title") or "Improvement",
                     "cause": hypothesis.get("cause") or "", "hypothesis": deepcopy(hypothesis),
                     "changes": deepcopy(hypothesis["changes"]),
                     "setup": {k: deepcopy(plan["setup"].get(k, {})) for k in
                               ("scripts", "exposure", "aliases", "alias_removals", "household")},
                     # What the replays started from: changed since, and the change is a conflict.
                     "tested": {"scripts": {e.split(".", 1)[1]: deepcopy(d) for e, d in plan["definitions"].items()},
                                "exposure": {e["entity_id"]: e["before"] for e in plan["edits"]}},
                     "provider": {k: plan["baseline_plan"]["provider"].get(k) for k in
                                  ("config_entry_id", "subentry_id", "title")},
                     "user_id": tests[0].get("user_id"), "tests": tests,
                     "untested_goals": self._untested(hypothesis, tests)}
            built_ids.add(improvement_id)
            if existing:
                existing.update(built)
            else:
                self.items[improvement_id] = {**built, "status": "awaiting_review", "created": utcnow().isoformat(),
                                              "decisions": [], "application": None, "history": []}
                self._link_revision(self.items[improvement_id])
        # An undecided improvement whose tests now belong to another (regrouped) is dropped; one whose tests are
        # simply gone from the candidates store stays: pruning records never loses a pending review.
        regrouped = {i["plan"]["id"] for items in groups.values() for i in items}
        for improvement_id, item in list(self.items.items()):
            if (improvement_id not in built_ids and not item["decisions"] and item["status"] == "awaiting_review"
                    and any(t.get("plan_id") in regrouped for t in item.get("tests", []))):
                del self.items[improvement_id]
        self._derived = sources
        await self._save()
        self._sync()

    def _case(self, case_id):
        """A case, read in place (the casebook's get copies it): CaseError if it's gone."""
        book = self.casebook
        if (cases := getattr(book, "cases", None)) is None:
            return book.get(case_id)
        if case_id not in cases:
            raise CaseError("Unknown case ID")
        return cases[case_id]

    def _goal(self, case_id):
        """The goal instance a case tests (session and turn); each run may have made its own case for it."""
        try:
            case = self._case(case_id)
        except CaseError:
            return case_id
        evidence = case["destination"].get("evidence") or {}
        return f"{evidence.get('session', case['recording_id'][:8])}:{evidence.get('goal_turn', case['turns'][0])}"

    def _link_revision(self, new):
        """A new improvement for goals whose earlier one the household asked to revise is that revision: it says so
        (with their note), and the earlier one is marked revised, so only the new one waits for review."""
        goals = {t["goal"] for t in new["tests"]} | set(new["untested_goals"])
        for old in self.items.values():
            if old is new or old["status"] != "revision_requested":
                continue
            if goals & ({t["goal"] for t in old.get("tests", [])} | set(old.get("untested_goals", []))):
                note = next((d.get("note") for d in reversed(old["decisions"]) if d["decision"] in
                             ("revise", "needs_improvement")), "") or ""
                new["revises"] = {"id": old["id"], "title": old["title"], "note": note}
                old["status"] = "revised"
                old["revised_by"] = new["id"]
                return

    def _source(self, item, case):
        """A digest of everything a test is derived from: the candidate (its result and status), its case, whether its
        recording is there, and this version's code. None if it can't be told."""
        index, result = getattr(self.recorder, "index", None), item["result"]
        try:
            return digest({"code": VERSION, "plan": item["plan"]["id"], "status": item["status"],
                           "result": {k: result.get(k) for k in ("assessment", "followup_check", "turns")},
                           "case": {k: case.get(k) for k in ("id", "recording_id", "turns", "title", "destination")},
                           "recording": None if index is None else case["recording_id"] in index})
        except (TypeError, ValueError):
            return None

    async def _test(self, item, earlier=None):
        """One test as the review screens show it (the conversation before, from its recording, and the replay after),
        with the digest of what it was derived from. earlier: {plan id: test} built before, reused while that digest
        is the one it was derived from."""
        plan, result = item["plan"], item["result"]
        case = self._case(plan["evaluation_case_id"])
        source = self._source(item, case)
        if source is not None and self._derived.get(plan["id"]) == source and plan["id"] in (earlier or {}):
            return deepcopy(earlier[plan["id"]]), source
        try:
            recording = await self.recorder.export(case["recording_id"])
        except (CaseError, ValueError):  # The recording is gone: the test stands, without its conversation.
            recording = {"turns": []}
        first, last = case["turns"][0], case["turns"][-1]
        assessment = result.get("assessment") or {}
        prepared = (_turns(recording)[first - 1:first] or [{}])[0].get("prepared") or {}
        user = (prepared.get("context") or {}).get("context", {}).get("user_id")
        return {"goal": self._goal(case["id"]),
                "case_id": case["id"], "plan_id": plan["id"], "status": item["status"],
                "summary": case["destination"].get("summary") or case.get("title", ""), "user_id": user,
                "turns_before": assessment.get("original_user_turns"), "turns_after": assessment.get("candidate_user_turns"),
                "goal_newly_met": assessment.get("goal_newly_met"), "baseline_met": assessment.get("baseline_met"),
                "followups_fit": (result.get("followup_check") or {}).get("all_fit"),
                "before": _conversation(recording, first, last),
                "after": [{"you": t["input"], "assistant": t["reply"]} for t in result.get("turns", [])],
                # The steps that failed in each turn of those (cases.step), for the review to show where they happened.
                "failed_steps": {"before": [t.get("failed", []) for t in transcript(recording)[first - 1:last]],
                                 "after": [t.get("failed", []) for t in replayed(result.get("turns", []))]}}, source

    @staticmethod
    def _untested(hypothesis, tests):
        tested = {t["goal"] for t in tests}
        return sorted(name for g in hypothesis.get("goals", [])
                      if (name := f"{g['session'][:8]}:{g['goal_turn']}") not in tested)

    def _sync(self):
        """The notification lists the improvements waiting on the user, each linking to its review in the panel."""
        wanted = {i["id"]: i for i in self.pending()}
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

    async def decide(self, improvement_id, decision, *, note="", user_id=None, reason=None):
        """Record a decision (with its reason, if one is given: REASONS) and act on it, one at a time per improvement
        (the second waits, then sees the first's result). Returns the improvement, with `outcome` saying what
        happened."""
        async with self._locks.setdefault(improvement_id, asyncio.Lock()):
            return await self._decide(improvement_id, decision, note=note, user_id=user_id, reason=reason)

    async def _decide(self, improvement_id, decision, *, note="", user_id=None, reason=None):
        item = self.get(improvement_id)
        if decision not in choices(item):
            raise CaseError("That decision isn't open for this improvement now")
        if reason is not None:
            if (decision, reason) not in REASONS:
                raise CaseError("That reason doesn't go with this decision")
            decision = REASONS[(decision, reason)]
        if decision == "revise" and not note.strip():
            raise CaseError("Say what to change in the note")
        record = {"decision": decision, "note": note.strip(), "by": user_id, "at": utcnow().isoformat()}
        if decision == "accept":
            outcome = await self._apply(item, user_id)
        elif decision == "undo":
            outcome = await self._undo(item)
        else:
            item["status"] = DECISIONS[decision][0]
            outcome = {"status": "recorded"}
        item["decisions"].append({**record, "outcome": outcome["status"]})
        item["last_outcome"] = {**outcome, "at": record["at"]}  # Shown on the review page for a short while.
        journal("info" if outcome["status"] in ("done", "recorded") else "warning",
                f"Decision {decision}: {outcome['status']}", improvement_id=improvement_id, decision=decision,
                outcome=outcome["status"], user_id=user_id, steps=len(outcome.get("steps") or []) or None,
                conflicts=len(outcome.get("conflicts") or []) or None)
        self.items[improvement_id] = item
        await self._save()
        self._sync()
        return {**deepcopy(item), "outcome": outcome}

    async def auto_accept(self, improvement_ids):
        """Accept low-risk improvements as soon as they're written up (the "auto-accept low-risk changes" setting):
        each applied all or nothing, recorded as accepted automatically, with a notification to review or undo it.
        Anything else, or one that can't be applied, stays up for review. Returns how many applied."""
        from .panel import review_url
        applied = 0
        for improvement_id in improvement_ids:
            item = self.items.get(improvement_id)
            if (not item or item["status"] not in PENDING or "accept" not in choices(item)
                    or not low_risk(self.hass, item)):
                continue
            try:
                result = await self.decide(improvement_id, "accept", note="Accepted automatically")
            except CaseError:  # The household decided it meanwhile (their decision held the lock first).
                continue
            self.items[improvement_id]["decisions"][-1]["auto"] = True
            if result["status"] == "applied":
                applied += 1
                headline = (item.get("copy") or {}).get("headline") or item["title"]
                persistent_notification.async_create(
                    self.hass, f"Applied automatically: **{headline}**. [Review or undo it]({review_url(improvement_id)})",
                    title="Assist Improver", notification_id=f"{DOMAIN}_auto_{improvement_id}")
                journal("info", "Applied automatically; notified", improvement_id=improvement_id)
        await self._save()
        return applied

    async def _apply(self, item, user_id):
        steps, already, conflicts = await plan_steps(self.hass, item)
        if conflicts:
            item["status"] = "conflict"
            return {"status": "conflict", "conflicts": conflicts}
        try:
            result = await self.changesets.run(item["id"], "apply", steps)
        except Conflict as err:
            return {"status": "busy", "error": str(err)}
        if result["status"] == "done":
            item["status"] = "applied"
            item["application"] = {"at": utcnow().isoformat(), "by": user_id, "steps": result["steps"],
                                   "already": already, "scripts_executed": 0}
        elif result["status"] == "needs_attention":
            item["status"] = "needs_attention"
        item["history"].append({"at": utcnow().isoformat(), "event": "apply", **result})
        return {**result, "already": already}

    async def _undo(self, item):
        applied = item["application"]["steps"]
        live = {(s["kind"], s["target"]): await read(self.hass, s["kind"], s["target"]) for s in applied}
        steps, left = undo_steps(applied, live)
        try:
            result = await self.changesets.run(item["id"], "undo", steps)
        except Conflict as err:
            return {"status": "busy", "error": str(err)}
        if result["status"] == "done":
            item["status"] = "undone"
        elif result["status"] == "needs_attention":
            item["status"] = "needs_attention"
        item["history"].append({"at": utcnow().isoformat(), "event": "undo", **result, "left": left})
        return {**result, "left": left}


KINDS = {"script": "script", "alias": "aliases", "exposure": "exposure", "prompt": "instructions",
         "household": "household", "assistant_api": "assistant"}


def display_title(item):
    """What the household reads as the improvement's name: the editor's headline, else the generator's title."""
    return ((item.get("copy") or {}).get("headline") or item["title"]).strip()


def benefits(item):
    """(goals now met, turns no longer needed) across the tests that improved: what accepting gets the user."""
    improved = [t for t in item["tests"] if t["status"] == "awaiting_review"]
    enabled = [t["summary"] for t in improved if t.get("goal_newly_met")]
    removed = [f"\u201c{turn['you'].strip()}\u201d" for t in improved
               if (t.get("turns_after") or 0) < (t.get("turns_before") or 0) for turn in t["before"][t["turns_after"]:]]
    return enabled, removed
