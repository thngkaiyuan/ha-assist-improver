"""Tested improvements awaiting the user's decision: one list, shown in Configure and in Repairs, kept in sync.

An improvement is one hypothesis's changes with every test of them (one per goal
instance): built by code from the candidates store whenever tests finish. A
decision in either place goes through `decide`, which updates the list, the
Repairs items and the notification together.
"""

from copy import deepcopy
import difflib
import html
import textwrap

from homeassistant.components import persistent_notification
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .cases import CaseError, digest, recording_turns
from .changeset import Changesets, Conflict, plan_steps, read, undo_steps
from .const import DOMAIN, STORAGE_VERSION

PENDING = ("awaiting_review", "needs_attention")  # Waiting on the user: in Repairs and the notification.
NOTIFICATION = f"{DOMAIN}_improvements"
OUTCOME_NOTIFICATION = f"{DOMAIN}_changes"
ISSUE_PREFIX = "improvement_"
DECISIONS = {  # Decision: (status, label).
    "accept": ("applied", "Accept changes"),
    "revise": ("revision_requested", "Ask for a revision"),
    "needs_improvement": ("revision_requested", "The change needs improvement"),
    "decline": ("declined", "Decline"),
    "fine_as_is": ("declined", "Things were fine as they were"),
    "was_test": ("ignored", "Ignore: that conversation was a test"),
    "undo": ("undone", "Undo changes"),
}


def choices(item):
    """The decisions open for an improvement in its status."""
    if item["status"] == "applied":
        return ["undo"]
    if item["status"] in ("undone", "conflict", "declined", "revision_requested", "ignored"):
        return ["accept"] if item["status"] == "undone" else []
    return ["accept", "revise", "needs_improvement", "decline", "fine_as_is", "was_test"]


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

    def __init__(self, hass, recorder):
        self.hass, self.recorder = hass, recorder
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.improvements", private=True, atomic_writes=True)
        self.changesets = Changesets(hass)
        self.items = {}

    async def start(self):
        saved = await self.store.async_load()
        self.items = (saved or {}).get("improvements", {})
        if outcome := await self.changesets.start():  # HA stopped mid-change: it was undone (or needs attention).
            item = self.items.get(outcome["improvement_id"])
            if item is not None:
                if outcome["status"] == "needs_attention":
                    item["status"] = "needs_attention"
                item.setdefault("history", []).append({"at": utcnow().isoformat(), "event": "interrupted", **outcome})
            what = "applying" if outcome["action"] == "apply" else "undoing"
            persistent_notification.async_create(
                self.hass, f"Home Assistant stopped while {what} the improvement "
                f"“{(item or {}).get('title', '')}”. "
                + ("Its changes were undone, so nothing of it is in place." if outcome["status"] == "rolled_back"
                   else "Some changes couldn't be undone: " + "; ".join(f"{s['what']} ({s['why']})"
                                                                         for s in outcome["stuck"])),
                title="Assist Improver", notification_id=OUTCOME_NOTIFICATION)
        await self.refresh()

    async def _save(self):
        await self.store.async_save({"improvements": self.items})

    def get(self, improvement_id):
        if improvement_id not in self.items:
            raise CaseError("Unknown improvement")
        return deepcopy(self.items[improvement_id])

    def pending(self):
        return [deepcopy(i) for i in self.items.values() if i["status"] in PENDING]

    def listed(self):
        """Every improvement, those waiting on the user first, then the most recent."""
        return sorted((deepcopy(i) for i in self.items.values()),
                      key=lambda i: (i["status"] not in PENDING, i["status"] != "applied", i["created"]), reverse=False)

    async def refresh(self):
        """Build improvements from the candidates store: each hypothesis with a test that improved a goal."""
        groups = {}  # One improvement per set of changes (what gets applied), however its idea was worded.
        for item in getattr(self.recorder.candidates, "items", {}).values():
            plan = item["plan"]
            if plan.get("kind") == "hypothesis_changes" and item.get("result"):
                groups.setdefault(digest(plan["hypothesis"]["changes"]), []).append(item)
        built_ids = set()
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
                tests = [await self._test(i) for i in sorted(items, key=lambda i: i["plan"]["evaluation_case_id"])]
            except CaseError:  # A case it was tested on is gone; it can't be shown honestly.
                continue
            hypothesis = plan["hypothesis"]
            built = {"id": improvement_id, "title": hypothesis.get("title") or "Improvement",
                     "cause": hypothesis.get("cause") or "", "hypothesis": deepcopy(hypothesis),
                     "changes": deepcopy(hypothesis["changes"]),
                     "setup": {k: deepcopy(plan["setup"].get(k, {})) for k in
                               ("scripts", "exposure", "aliases", "alias_removals")},
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
        for improvement_id, item in list(self.items.items()):  # Built differently before, and never decided.
            if improvement_id not in built_ids and not item["decisions"] and item["status"] == "awaiting_review":
                del self.items[improvement_id]
        await self._save()
        self._sync()

    def _goal(self, case_id):
        """The goal instance a case tests (session and turn); each run may have made its own case for it."""
        try:
            case = self.recorder.casebook.get(case_id)
        except CaseError:
            return case_id
        evidence = case["destination"].get("evidence") or {}
        return f"{evidence.get('session', case['recording_id'][:8])}:{evidence.get('goal_turn', case['turns'][0])}"

    async def _test(self, item):
        plan, result = item["plan"], item["result"]
        case = self.recorder.casebook.get(plan["evaluation_case_id"])
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
                "after": [{"you": t["input"], "assistant": t["reply"]} for t in result.get("turns", [])]}

    @staticmethod
    def _untested(hypothesis, tests):
        tested = {t["goal"] for t in tests}
        return sorted(name for g in hypothesis.get("goals", [])
                      if (name := f"{g['session'][:8]}:{g['goal_turn']}") not in tested)

    def _sync(self):
        """Repairs has one item per improvement waiting on the user; the notification counts them."""
        registry = ir.async_get(self.hass)
        wanted = {ISSUE_PREFIX + i["id"]: i for i in self.items.values() if i["status"] in PENDING}
        for (domain, issue_id) in list(registry.issues):
            if domain == DOMAIN and issue_id.startswith(ISSUE_PREFIX) and issue_id not in wanted:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, item in wanted.items():
            attention = item["status"] == "needs_attention"
            ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=True, severity=ir.IssueSeverity.WARNING,
                                  translation_key="improvement_attention" if attention else "improvement_ready",
                                  translation_placeholders={"title": item["title"]},
                                  data={"improvement_id": item["id"]})
        if not wanted:
            persistent_notification.async_dismiss(self.hass, NOTIFICATION)
            return
        count = len(wanted)
        persistent_notification.async_create(
            self.hass, f"{count} tested improvement{'s' if count > 1 else ''} to Assist "
            f"{'are' if count > 1 else 'is'} ready for your review. Open "
            f"[Assist Improver](/config/integrations/integration/{DOMAIN}) and choose Configure → Improvements, "
            f"or review {'them' if count > 1 else 'it'} in [Repairs](/config/repairs).",
            title="Assist Improver", notification_id=NOTIFICATION)

    async def decide(self, improvement_id, decision, *, note="", user_id=None):
        """Record a decision and act on it. Returns the improvement, with `outcome` saying what happened."""
        item = self.get(improvement_id)
        if decision not in choices(item):
            raise CaseError("That decision isn't open for this improvement now")
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
        self.items[improvement_id] = item
        await self._save()
        self._sync()
        return {**deepcopy(item), "outcome": outcome}

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


GREEN, RED = "#1a7f37", "#cf222e"  # A diff viewer's added and removed lines.
KINDS = {"script": "script", "alias": "aliases", "exposure": "exposure", "prompt": "instructions"}
WRAP = 56  # Characters per diff line.


def _n(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _esc(text):
    return html.escape(str(text), quote=False)


def _section(title, body, open_=False):
    return f"<details{' open' if open_ else ''}><summary>{title}</summary>\n\n{body}\n\n</details>"


def _box(kind, lines):
    """Lines in a tinted box: success is green, error is red."""
    return f'<ha-alert alert-type="{kind}">' + "<br>".join(_esc(l) for l in lines) + "</ha-alert>"


def benefits(item):
    """(goals now met, turns no longer needed) across the tests that improved: what accepting gets the user."""
    improved = [t for t in item["tests"] if t["status"] == "awaiting_review"]
    enabled = [t["summary"] for t in improved if t.get("goal_newly_met")]
    removed = [f"\u201c{turn['you'].strip()}\u201d" for t in improved
               if (t.get("turns_after") or 0) < (t.get("turns_before") or 0) for turn in t["before"][t["turns_after"]:]]
    return enabled, removed


def describe(item, planned=None):
    """The improvement the way a code review shows a change: what it gets you, then the goals it enables (green)
    and the turns it saves (red), the changeset one line per change (each opening to its diff), and why.

    planned: (steps, already, conflicts) from checking it against Home Assistant now; otherwise the applied steps."""
    enabled, removed = benefits(item)
    headline = " \u00b7 ".join(p for p in (enabled and f"Enables {_n(len(enabled), 'task')}",
                                           removed and f"{_n(len(removed), 'fewer turn')}") if p)
    parts = [f"**{headline or 'Tested on your conversations'}**"]  # The improvement's name is the dialog's title.
    steps, already, conflicts = planned or ((item.get("application") or {}).get("steps") or [], [], [])
    if conflicts:
        parts += ["", _box("error", ["Can't apply it now:"] + [f"{c['what']}: {c['why']}" for c in conflicts])]
    if enabled:
        parts += ["", _section(f"Tasks now enabled ({len(enabled)})", _box("success", enabled))]
    if removed:
        parts += ["", _section(f"Turns no longer needed ({len(removed)})", _box("error", removed))]
    changes = [_change_line(step) for step in steps] + [
        _section(f"{_esc(a['what'])} \u00b7 {KINDS[a['kind']]} \u00b7 already in place", "Nothing to change.")
        for a in already]
    if not changes:  # Not checked against Home Assistant (decided already): the changes as proposed.
        assistant = item["provider"].get("title") or "Assist"
        changes = ["<br>".join(_esc(_change_text(c, assistant)) for c in item["changes"])]
    parts += ["", _section(f"Changeset ({len(steps) + len(already) or len(item['changes'])})", "\n\n".join(changes))]
    tested = []
    for test in item["tests"]:
        result = {"awaiting_review": "improved", "not_improved": "not improved"}.get(test["status"], test["status"])
        tested.append(f"{_esc(test['summary'])}: {result}")
    why = [_esc(item["cause"]), "", "Tested on " + _n(len(item["tests"]), "conversation") + ":<br>" + "<br>".join(tested)]
    if item["untested_goals"]:
        why += ["", f"Expected to help {_n(len(item['untested_goals']), 'more conversation')} too (not tested)."]
    why += ["", "Replays ran in a sandboxed copy of your home: nothing in the house moved, and physical positions "
            "aren't simulated. Accepting changes only Home Assistant settings; no script runs."]
    parts += ["", _section("Why", "\n".join(why))]
    return "\n".join(parts)


def _change_line(step):
    """One change, collapsed to its name, kind and +/- counts; open, its diff."""
    lines = _diff(step)
    added, gone = sum(1 for sign, _ in lines if sign == "+"), sum(1 for sign, _ in lines if sign == "-")
    counts = " ".join(c for c in (added and f'<font color="{GREEN}">+{added}</font>',
                                  gone and f'<font color="{RED}">\u2212{gone}</font>') if c)
    rows = []
    for sign, text in lines:  # Wrapped, keeping the indent: the dialog doesn't scroll sideways.
        indent = " " * (len(text) - len(text.lstrip()))
        for part in textwrap.wrap(text, WRAP, initial_indent="", subsequent_indent=indent + "  ",
                                  drop_whitespace=False) or [""]:
            rows.append(f'<font color="{GREEN if sign == "+" else RED}">{sign} {_esc(part)}</font>'
                        if sign in "+-" else f"  {_esc(part)}")
    body = "<pre>" + "\n".join(rows) + "</pre>"
    return _section(f"{_esc(step['what'])} \u00b7 {KINDS[step['kind']]} {counts}".rstrip(), body)


def _diff(step):
    """[(sign, line)]: + added, - removed, space for context (one line around each change)."""
    kind, before, after = step["kind"], step["before"], step["after"]
    if kind == "exposure":
        return [("+" if after else "-", "exposed to Assist")]
    if kind == "alias":
        name = lambda a: a if a is not None else "(its own name)"
        return ([("-", name(a)) for a in before or [] if a not in after]
                + [("+", name(a)) for a in after if a not in (before or [])])
    if kind == "script":
        import yaml
        from annotatedyaml.dumper import FastestAvailableSafeDumper
        def dump(config):  # As HA writes scripts, but unfolded: long lines are wrapped once, for the dialog.
            return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=10**6,
                             Dumper=FastestAvailableSafeDumper).replace(": null\n", ":\n").splitlines()
        old, new = (dump(before) if before else []), dump(after)
    else:
        old, new = (before or "").splitlines(), (after or "").splitlines()
    out = []
    for line in difflib.unified_diff(old, new, n=1, lineterm=""):
        if line.startswith(("---", "+++")):
            continue
        out.append((" ", "\u2026") if line.startswith("@@") else (line[0], line[1:]))
    return out[1:] if out and out[0] == (" ", "\u2026") else out


def _change_text(change, assistant):
    kind = change["kind"]
    if kind in ("preference", "prompt"):  # The instructions of the assistant the conversations used.
        who = "for you" if change.get("for") == "this_user" else "for everyone"
        text = f"Add to {assistant}'s instructions ({who}): “{change.get('text', '').strip()}”"
        if change.get("replaces"):
            text += ", replacing " + ", ".join(f"“{r}”" for r in change["replaces"])
        return text
    if kind == "alias":
        text = f"Give {change['entity_id']} the alias “{change.get('alias')}”"
        return text + (", replacing " + ", ".join(change["replaces"]) if change.get("replaces") else "")
    if kind == "exposure":
        return f"{'Expose' if change.get('expose', True) else 'Hide'} {change['entity_id']} " \
               f"{'to' if change.get('expose', True) else 'from'} Assist"
    if kind == "script":
        steps = ", ".join(s.get("stop") and f"stop: {s['stop']}" or f"{s['service']} {s['entity_id']}"
                          for s in change.get("steps") or [])
        if change.get("entity_id"):
            return f"Change script {change['entity_id']}" + (f": {steps}" if steps else "") + \
                   (f" (description: {change['text']})" if change.get("text") else "")
        return f"New script “{change.get('script_name')}”: {steps}"
    return kind
