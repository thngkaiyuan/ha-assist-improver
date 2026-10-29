"""The review panel: improvements as a code-review page, where the user reviews and decides.

A sidebar panel (admins only) served from this integration: a web component in
www/panel.js (the modules it loads are in www/panel/, under the same ?v=VERSION)
that talks to Home Assistant over websocket commands (improvements and
decisions, runs, household knowledge, settings). Each review has its own
address, so the notification links straight to it; settings are at
/assist-improver?view=settings.
"""

import difflib
import json
from pathlib import Path

import probatio
from homeassistant.components import websocket_api
from homeassistant.core import callback

from .cases import CaseError
from .changeset import plan_steps
from .const import DOMAIN, VERSION
from .improvements import (DECISIONS, KINDS, PENDING, PHYSICAL, benefits, choices, display_title, physical, reasons,
                           reply_words, row, spared)
from .runtime import loaded

URL_PATH = "assist-improver"  # The panel's address: /assist-improver?id=<improvement>.
STATIC = f"/{DOMAIN}_static"
CONTEXT = 2  # Unchanged lines shown around each change.
PANEL_KEY = f"{DOMAIN}_panel"  # Set while the panel is registered.


def review_url(improvement_id):
    return f"/{URL_PATH}?id={improvement_id}"


def numbered_diff(old, new):
    """[{sign, old, new, text}] with each line's number before and after, as a diff viewer shows them; a gap
    between hunks is {sign: "gap"}."""
    rows = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for group in matcher.get_grouped_opcodes(CONTEXT):
        if rows:
            rows.append({"sign": "gap"})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [{"sign": " ", "old": i + 1, "new": j + 1, "text": old[i]}
                         for i, j in zip(range(i1, i2), range(j1, j2))]
                continue
            rows += [{"sign": "-", "old": i + 1, "new": None, "text": old[i]} for i in range(i1, i2)]
            rows += [{"sign": "+", "old": None, "new": j + 1, "text": new[j]} for j in range(j1, j2)]
    return rows


def _yaml(config):
    import yaml
    from annotatedyaml.dumper import FastestAvailableSafeDumper
    return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=10**6,
                     Dumper=FastestAvailableSafeDumper).replace(": null\n", ":\n").splitlines()


def change_rows(step, names=None):
    kind, before, after = step["kind"], step["before"], step["after"]
    if kind == "household":
        from .household import as_text
        return numbered_diff(as_text(before, names).splitlines(), as_text(after, names).splitlines())
    if kind == "assistant_api":
        from .household import API_ID, API_NAME
        label = lambda a: API_NAME if a == API_ID else "Assist" if a == "assist" else a
        return ([{"sign": "-", "old": None, "new": None, "text": label(a)} for a in before or [] if a not in after]
                + [{"sign": "+", "old": None, "new": None, "text": label(a)} for a in after if a not in (before or [])])
    if kind == "exposure":
        return [{"sign": "+" if after else "-", "old": None, "new": None, "text": "exposed to Assist"}]
    if kind == "alias":
        name = lambda a: a if a is not None else "(its own name)"
        return ([{"sign": "-", "old": None, "new": None, "text": name(a)} for a in before or [] if a not in after]
                + [{"sign": "+", "old": None, "new": None, "text": name(a)} for a in after if a not in (before or [])])
    if kind == "script":
        return numbered_diff(_yaml(before) if before else [], _yaml(after))
    return numbered_diff((before or "").splitlines(), (after or "").splitlines())


def test_result(test):
    """A test's outcome in a few words, from its row (improvements.row): the goal now met, fewer turns, a side effect
    gone, your feedback followed (as many as it showed), or just improved."""
    if not test["improved"]:
        return "not improved"
    shown = []
    if test["newly_met"]:
        shown.append("goal now met")
    before, after = test["turns_before"], test["turns_after"]
    if not shown and after < before:
        shown.append(f"{before} \u2192 {after} turns")
    if gone := test["side_effects_gone"]:
        shown.append("side effect gone" if len(gone) == 1 else "side effects gone")
    if test["feedback"]:
        shown.append("follows your feedback")
    return " \u00b7 ".join(shown) or "improved"


def more_words(test):
    """{more_words: Assist's words a reply, before and with the change} when the change made it noticeably wordier."""
    found = reply_words(test)
    return {"more_words": {"before": found["before"], "after": found["after"]}} if found and found["wordier"] else {}


def said(test):
    """A test's conversation as it went and its replay with the change, turn by turn, from its row
    (improvements.row), as the home page reads a conversation: what was said, Assist's reply, and the steps that failed
    in between."""
    shown = {}
    for side in ("before", "after"):
        failed = test["failed_steps"][side]
        shown[side] = [{"you": t["you"] or "", "assist": t["assistant"] or "",
                        **({"failed": failed[n]} if n < len(failed) and failed[n] else {})}
                       for n, t in enumerate(test[side])]
    return shown


def safety(steps):
    """Why it's safe, stated by code from its test and the changeset (never written by a model)."""
    facts = ["Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your home "
             "moved."]
    counts = {}
    for step in steps:
        kind = step["kind"]
        if kind == "script":
            key = "new script" if step["before"] is None else "script updated"
        elif kind == "exposure":
            key = "device or script made visible to Assist" if step["after"] else "device hidden from Assist"
        elif kind == "alias":
            key = "name added"
        elif kind == "household":
            key = "household line"
        elif kind == "assistant_api":  # An improvement applied up to 0.37, which also switched the assistant.
            key = "assistant switched to household knowledge"
        else:
            key = "instruction change"
        counts[key] = counts.get(key, 0) + 1
    if counts:
        plural = lambda n, w: f"{n} {w}" if n == 1 else f"{n} " + {"new script": "new scripts", "script updated":
            "scripts updated", "device or script made visible to Assist": "devices or scripts made visible to Assist",
            "device hidden from Assist": "devices hidden from Assist", "name added": "names added",
            "household line": "household lines", "assistant switched to household knowledge":
            "assistants switched to household knowledge", "instruction change": "instruction changes"}[w]
        facts.append("Only settings change: " + ", ".join(plural(n, w) for w, n in counts.items())
                     + ". Nothing runs when you accept.")
    facts.append("Checked against your Home Assistant first, applied all or nothing, and one click to undo.")
    return facts


def review(item, planned=None, names=None, hass=None, revising=False):
    """Everything the panel shows for one improvement, built by code. names: {user id: name} for household lines.
    hass: to tell a device that drives a door by its name in Home Assistant too (improvements.physical). revising: its
    revision's round runs (improvements.choices)."""
    enabled, removed = benefits(item)
    gone, followed = spared(item)
    shown = row(item["test"])
    steps, already, conflicts = planned or ((item.get("application") or {}).get("steps") or [], [], [])
    changes = []
    for step in steps:
        rows = change_rows(step, names)
        changes.append({"name": step["what"], "kind": KINDS[step["kind"]], "rows": rows,
                        "added": sum(r["sign"] == "+" for r in rows), "removed": sum(r["sign"] == "-" for r in rows)})
    changes += [{"name": a["what"], "kind": KINDS[a["kind"]], "rows": [], "added": 0, "removed": 0,
                 "already": True} for a in already]
    if not changes:  # Decided already and nothing applied: the changes as proposed.
        assistant = item["provider"].get("title") or "Assist"
        changes = [{"name": _change_text(c, assistant), "kind": KINDS.get(c["kind"], c["kind"]), "rows": [],
                    "added": 0, "removed": 0, "proposed": True} for c in item["changes"]]
    recent, recent_ok = None, False
    if last := item.get("last_outcome"):
        # A decision changes the sidebar count, and Home Assistant then rebuilds the panel: the outcome comes back
        # with the review for a couple of minutes, so the rebuilt panel still shows it.
        from homeassistant.util.dt import parse_datetime, utcnow
        if (at := parse_datetime(last.get("at") or "")) and (utcnow() - at).total_seconds() < 120:
            recent = outcome_text({**item, "outcome": last})
            recent_ok = last.get("status") in ("done", "recorded")
    copy = item.get("copy") or {}
    return {"id": item["id"], "title": display_title(item), "benefit": copy.get("benefit"),
            "safety": safety(steps), "status": item["status"], "enabled": enabled,
            "recent_outcome": recent, "recent_outcome_ok": recent_ok, "revises": item.get("revises"),
            "removed": removed, "conflicts": conflicts, "changes": changes, "cause": item["cause"],
            # Side effects it no longer leaves (each device, left as it was) and the feedback it follows.
            "spared": [f"{g['name']} stays {g['start']}" for g in gone], "followed": followed,
            # A door's or valve's motion isn't simulated: said where the improvement involves one (its changes, cause
            # and test as the review shows it: not every script and exposed device its replays ran with).
            "physical": bool(PHYSICAL.search(json.dumps([item.get("changes"), item.get("cause"), shown], default=str)))
            or any(physical(hass, e) for c in item.get("changes") or []
                   for e in [c.get("entity_id"), *(s.get("entity_id") for s in c.get("steps") or [])]
                   if isinstance(e, str)),
            "tests": [{"summary": shown["summary"], "improved": shown["improved"], "result": test_result(shown),
                       **said(shown), **more_words(shown)}],
            "wordier": 1 if more_words(shown) else 0,
            # Each decision open now, with the reasons it can be given with (Decline: not an issue).
            "choices": [{"value": d, "label": DECISIONS[d], **({"reasons": r} if (r := reasons(d)) else {})}
                        for d in choices(item, revising)]}


def outcome_text(item):
    """What a decision did, in a sentence or two."""
    outcome = item["outcome"]
    status = outcome["status"]
    if status == "recorded":
        decision = (item.get("decisions") or [{}])[-1].get("decision")
        if decision == "revise":
            return "Thanks. The improver is revising it now."
        if decision == "decline":
            return "Thanks. Declined, and its conversation is archived."
        return "Thanks, your decision is recorded."
    if status == "done":
        script = any(c.get("kind") == "script" for c in item.get("changes") or [])
        applied = ("Applied. Assist uses it from your next request; no script was run." if script
                   else "Applied. Assist uses it from your next request.")
        changed = [l for l in outcome.get("left") or [] if not l.get("used_by")]
        kept = [l for l in outcome.get("left") or [] if l.get("used_by")]  # Another applied improvement adds it too.
        return (applied if item["status"] == "applied" else "Undone: what was there before is back.") + (
            " Left as you changed it: " + "; ".join(l["what"] for l in changed) if changed else "") + (
            " Kept, as another improvement uses it: " + "; ".join(f"“{l['kept']}” ({l['used_by']})" for l in kept)
            if kept else "")
    if status == "conflict":
        return "Nothing was changed: " + "; ".join(f"{c['what']}: {c['why']}" for c in outcome["conflicts"])
    if status == "rolled_back":
        return (f"Nothing was changed: {outcome['failure']['what']} failed ({outcome['failure']['error']}), so the "
                "steps already done were undone. You can try again.")
    if status == "needs_attention":
        return ("Some changes couldn't be undone after a failure: "
                + "; ".join(f"{s['what']} ({s['why']})" for s in outcome["stuck"]))
    return outcome.get("error") or status


def _change_text(change, assistant):
    from .instructions import is_edit
    kind = change["kind"]
    if is_edit(change):  # One assistant's own instructions: lines added, replaced or removed there.
        name = change.get("assistant") or assistant
        replaced = ", ".join(f"“{r}”" for r in change.get("replaces") or [])
        if not (change.get("text") or "").strip():
            return f"Remove from {name}'s instructions: {replaced}"
        text = f"Add to {name}'s instructions: “{change['text'].strip()}”"
        return text + (f", replacing {replaced}" if replaced else "")
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


def _runtime(hass, connection, msg):
    """The loaded improver, or None after answering that it isn't loaded, as devview.py does: the panel can stay open
    after the integration unloads."""
    if (runtime := loaded(hass)) is None:
        connection.send_error(msg["id"], "not_loaded", "Assist Improver isn't loaded.")
        return None
    return runtime


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/improvements"})
@websocket_api.require_admin
@callback
def ws_list(hass, connection, msg):
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    items = runtime.improvements.listed()
    connection.send_result(msg["id"], {"improvements": [
        {"id": i["id"], "title": display_title(i), "status": i["status"], "pending": i["status"] in PENDING,
         "enabled": len(benefits(i)[0]), "removed": len(benefits(i)[1]), "revision": bool(i.get("revises")),
         # Side effects it no longer leaves, and feedback it follows: counted where it has any.
         **{key: n for key, n in zip(("spared", "followed"), map(len, spared(i))) if n}}
        for i in items]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/improvement",
                                  probatio.Required("improvement_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get(hass, connection, msg):
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    improvements = runtime.improvements  # Kept: the integration may unload while this waits.
    try:
        item = improvements.get(msg["improvement_id"])
    except CaseError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    planned = await plan_steps(hass, item) if "accept" in choices(item) else None  # Checked against HA now.
    names = {}
    for step in (planned or ((item.get("application") or {}).get("steps") or [], [], []))[0]:
        if step["kind"] == "household":
            for user_id in {*step["before"]["people"], *step["after"]["people"]}:
                user = await hass.auth.async_get_user(user_id) if getattr(hass, "auth", None) else None
                names[user_id] = getattr(user, "name", None) or user_id
    result = review(item, planned, names, hass, runtime.flow.revising(item["id"]))
    if item["status"] not in PENDING:  # Decided: the next one waiting is one click away.
        nxt = next((i for i in improvements.pending() if i["id"] != item["id"]), None)
        result["next_pending"] = {"id": nxt["id"], "title": display_title(nxt)} if nxt else None
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/decide",
                                  probatio.Required("improvement_id"): str, probatio.Required("decision"): str,
                                  probatio.Optional("note", default=""): str, probatio.Optional("reason"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_decide(hass, connection, msg):
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    try:
        item = await runtime.improvements.decide(msg["improvement_id"], msg["decision"], note=msg["note"],
                                                  user_id=connection.user.id, reason=msg.get("reason"),
                                                  revising=runtime.flow.revising(msg["improvement_id"]))
    except CaseError as err:
        connection.send_error(msg["id"], "decision", str(err))
        return
    # Where its conversation goes (flow.py); a revision goes straight back in at Propose, on a run of its own.
    _send_back(runtime, await runtime.flow.decided(item, msg["decision"], msg["note"]), "revision")
    conflict = item["outcome"]["status"] == "conflict"  # It says so, and asks before going back in (P0.7).
    connection.send_result(msg["id"], {"result": outcome_text(item), "status": item["status"],
                                       "ok": item["outcome"]["status"] in ("done", "recorded"),
                                       **({"asks": {"about": "conflict", "improvement_id": item["id"],
                                                    "conflicts": item["outcome"].get("conflicts") or []}}
                                          if conflict else {})})


def _send_back(runtime, rids, trigger):
    """A run of its own for each conversation sent back to Propose (a revision, or a conflict the household agreed
    to resolve): now, or right after the run in progress."""
    for rid in rids:
        runtime.pipeline.send_back(rid, trigger)


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversation/test",
                                  probatio.Required("recording_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversation_test(hass, connection, msg):
    """The household marks a conversation as only a test: it ends in Your household as marked as a test; not while an
    improvement of it waits for the household (flow.Flow.waits). Whether it was marked."""
    from .conversations import kept
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    if not kept(runtime, msg["recording_id"]):
        connection.send_error(msg["id"], "not_found", "That conversation isn't kept.")
        return
    connection.send_result(msg["id"], {"marked": await runtime.flow.mark_test(msg["recording_id"])})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conflict/resolve",
                                  probatio.Required("improvement_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conflict_resolve(hass, connection, msg):
    """The household agrees to send an improvement whose accept met a conflict back in at Propose, to resolve it."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    try:
        item = runtime.improvements.get(msg["improvement_id"])
    except CaseError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    if item["status"] != "conflict":
        connection.send_error(msg["id"], "decision", "That improvement didn't meet a conflict.")
        return
    _send_back(runtime, await runtime.flow.resolve(item), "conflict")
    connection.send_result(msg["id"], {"sent_back": True})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/household"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_household(hass, connection, msg):
    """The household knowledge (everyone's lines, then each person's), and Home Assistant's assistants."""
    from .household import KEY, normalized, pipelines
    knowledge = normalized(getattr(hass.data.get(KEY), "knowledge", None))
    lines = len(knowledge["everyone"]) + sum(len(v) for v in knowledge["people"].values())
    people = []
    for user_id, mine in sorted(knowledge["people"].items()):
        user = await hass.auth.async_get_user(user_id) if getattr(hass, "auth", None) else None
        people.append({"user_id": user_id, "name": getattr(user, "name", None) or "Someone", "lines": list(mine)})
    me = connection.user
    connection.send_result(msg["id"], {"lines": lines, "everyone": list(knowledge["everyone"]), "people": people,
                                       "me": {"user_id": me.id, "name": getattr(me, "name", None) or "You"},
                                       "pipelines": pipelines(hass)})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/household/edit",
                                  probatio.Required("scope"): str, probatio.Required("action"): probatio.In(("add", "update", "delete")),
                                  probatio.Optional("index"): int, probatio.Optional("old"): str, probatio.Optional("text"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_household_edit(hass, connection, msg):
    """Add, change or remove one household knowledge line: everyone's (scope "everyone") or one person's (their user
    ID). A line being changed or removed must still read as it did (old), so two edits never cross."""
    from .household import KEY, normalized
    household = hass.data.get(KEY)
    if household is None:
        connection.send_error(msg["id"], "not_ready", "Household knowledge isn't loaded yet.")
        return
    knowledge = normalized(household.knowledge)
    # A new line needs a person HA knows; lines of someone since removed can still be changed or deleted.
    if msg["scope"] != "everyone" and msg["scope"] not in knowledge["people"] \
            and await hass.auth.async_get_user(msg["scope"]) is None:
        connection.send_error(msg["id"], "unknown_person", "That person isn't set up in Home Assistant.")
        return
    lines = knowledge["everyone"] if msg["scope"] == "everyone" else knowledge["people"].setdefault(msg["scope"], [])
    text, index = (msg.get("text") or "").strip(), msg.get("index")
    if msg["action"] in ("update", "delete") and not (isinstance(index, int) and 0 <= index < len(lines)
                                                      and lines[index] == msg.get("old")):
        connection.send_error(msg["id"], "changed", "That line has changed since you opened it. Close and open it again.")
        return
    if msg["action"] in ("add", "update") and not text:
        connection.send_error(msg["id"], "empty", "Write the line first.")
        return
    if msg["action"] == "add":
        lines.append(text)
    elif msg["action"] == "update":
        lines[index] = text
    else:
        del lines[index]
    await household.save(knowledge)
    connection.send_result(msg["id"], {"lines": sum(len(v) for v in [knowledge["everyone"], *knowledge["people"].values()])})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/assistant/prefer",
                                  probatio.Required("pipeline_id"): str})
@websocket_api.require_admin
@callback
def ws_assistant_prefer(hass, connection, msg):
    """Make one of Home Assistant's assistants the preferred one (as Settings, Voice assistants does)."""
    from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
    try:
        hass.data[KEY_ASSIST_PIPELINE].pipeline_store.async_set_preferred_item(msg["pipeline_id"])
    except Exception:  # Unknown assistant.
        connection.send_error(msg["id"], "not_found", "That assistant isn't in Home Assistant any more.")
        return
    connection.send_result(msg["id"], {"preferred": msg["pipeline_id"]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/run"})
@websocket_api.require_admin
@callback
def ws_run_status(hass, connection, msg):
    """The run in progress (or the last one), and what Run now would do."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], runtime.pipeline.status())


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/run/start",
                                  probatio.Required("confirmed"): True,
                                  probatio.Optional("only"): probatio.In(("triage", "analyze", "propose", "write")),
                                  probatio.Optional("conversations"): probatio.All([str], probatio.Length(max=1000))})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_run_start(hass, connection, msg):
    """Start a run; the panel asks the user to confirm what it will spend first. only: Run one step. conversations
    (the home page's Run with some picked): a picked run, which admits only those it can take (conversations.row:
    runnable); one picked from Your household is sent in first (flow.Flow.sent_in), for a new round.
    Neither: a manual full run."""
    from .conversations import kept, row
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    picked = msg.get("conversations")  # None: not a picked run.
    if runtime.pipeline.running:
        connection.send_error(msg["id"], "busy", "A run is already in progress")
        return
    if reason := runtime.pipeline.refusal(msg.get("only")):  # Before anything is sent in.
        connection.send_error(msg["id"], "busy", reason)
        return
    if missing := [rid for rid in picked or [] if not kept(runtime, rid)]:
        connection.send_error(msg["id"], "not_found", f"{len(missing)} of those conversations aren't kept.")
        return
    for rid in picked or []:
        if runtime.flow.place(rid)["stage"] == "Your household" and row(runtime, rid)["runnable"]:
            await runtime.flow.sent_in(rid, by=connection.user.id)
    try:
        run = runtime.pipeline.start(trigger="manual", only=msg.get("only"), picked=picked)
    except CaseError as err:
        connection.send_error(msg["id"], "busy", str(err))
        return
    connection.send_result(msg["id"], {"run": run})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/run/cancel"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_run_cancel(hass, connection, msg):
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    await runtime.pipeline.cancel()
    connection.send_result(msg["id"], {"cancelled": True})


def _entry(hass, connection, msg):
    """The improver's config entry, or None after answering that it isn't loaded."""
    runtime = _runtime(hass, connection, msg)
    return runtime.pipeline.entry if runtime else None


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings"})
@websocket_api.require_admin
@callback
def ws_settings(hass, connection, msg):
    """The settings view: each AI role's model, the AI Tasks to choose from, and the other settings."""
    from .settings import view
    if (entry := _entry(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], view(hass, entry, connection.user.id))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings/set",
                                  probatio.Optional("role"): str, probatio.Optional("entity_id"): probatio.Any(str, None),
                                  probatio.Optional("tests_per_goal"): int, probatio.Optional("schedule"): str,
                                  probatio.Optional("stage"): str, probatio.Optional("pause"): bool,
                                  probatio.Optional("auto_accept"): bool, probatio.Optional("on_demand"): bool,
                                  probatio.Optional("household_knowledge"): bool,
                                  probatio.Optional("recording"): bool, probatio.Optional("person"): str,
                                  probatio.Optional("developer_services"): bool})
@websocket_api.require_admin
@callback
def ws_settings_set(hass, connection, msg):
    """Change one setting; it applies right away, even during a run. recording with person (a user ID) is that
    person's switch; without, the household's."""
    from .settings import SettingsError, updated, view
    if (entry := _entry(hass, connection, msg)) is None:
        return
    try:
        options = updated(hass, entry, role=msg.get("role"), entity_id=msg.get("entity_id"),
                          tests_per_goal=msg.get("tests_per_goal"), schedule=msg.get("schedule"),
                          stage=msg.get("stage"), pause=msg.get("pause"), auto_accept=msg.get("auto_accept"),
                          on_demand=msg.get("on_demand"), household_knowledge=msg.get("household_knowledge"),
                          recording=msg.get("recording"), person=msg.get("person"),
                          developer_services=msg.get("developer_services"))
    except SettingsError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], view(hass, entry, connection.user.id))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings/models",
                                  probatio.Required("entry_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings_models(hass, connection, msg):
    """The models a provider offers for a new AI Task, from its own add-AI-Task flow."""
    from .settings import provider_models
    try:
        connection.send_result(msg["id"], await provider_models(hass, msg["entry_id"]))
    except Exception as err:  # The provider's flow refused; say why instead of failing silently.
        connection.send_error(msg["id"], "provider", f"The provider couldn't list its models ({err}).")


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings/create",
                                  probatio.Required("entry_id"): str, probatio.Required("model"): str,
                                  probatio.Optional("name"): str, probatio.Required("role"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings_create(hass, connection, msg):
    """Add an AI Task through the provider's own flow, with the chosen model, and use it for one role."""
    from .ai_setup import create_ai_task
    from .settings import ROLES, SettingsError, updated, view
    if (entry := _entry(hass, connection, msg)) is None:  # Before adding anything at the provider.
        return
    if msg["role"] not in ROLES:  # Checked first, so nothing is added at the provider for nothing.
        connection.send_error(msg["id"], "invalid", f"Unknown role {msg['role']}")
        return
    entity_id, reason = await create_ai_task(hass, msg["entry_id"], "chosen", model=msg["model"], name=msg.get("name"))
    if entity_id is None:
        connection.send_error(msg["id"], "create_failed", reason)
        return
    try:
        hass.config_entries.async_update_entry(entry, options=updated(hass, entry, role=msg["role"],
                                                                      entity_id=entity_id))
    except SettingsError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], view(hass, entry, connection.user.id))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings/recommended"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings_recommended(hass, connection, msg):
    """The recommended setup for every step: AI Tasks that fit are reused, the rest added through the provider's own
    flow. The view comes back with notes on anything that couldn't be done."""
    from .settings import SettingsError, recommended_options, view
    if (entry := _entry(hass, connection, msg)) is None:
        return
    try:
        options, notes = await recommended_options(hass, entry)
    except SettingsError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], {**view(hass, entry, connection.user.id), "notes": notes})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/practice", probatio.Required("on"): bool})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_practice(hass, connection, msg):
    """Practice mode for whoever is signed in (as the switch does): on or off."""
    from .practice import MODE_KEY
    from .settings import view
    if (entry := _entry(hass, connection, msg)) is None:
        return
    if (mode := hass.data.get(MODE_KEY)) is None:
        connection.send_error(msg["id"], "not_loaded", "Assist Improver isn't loaded.")
        return
    user = connection.user
    if msg["on"]:
        await mode.async_turn_on(user.id, user.name or "someone")
    else:
        await mode.async_turn_off(user.id)
    connection.send_result(msg["id"], view(hass, entry, user.id))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/log"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_log(hass, connection, msg):
    """What the improver did, newest first: runs (by trigger; picked: a manual run on picked conversations),
    improvements going up for review, and decisions on them."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    names = {}

    async def name(user_id):
        if user_id not in names:
            user = await hass.auth.async_get_user(user_id) if user_id and getattr(hass, "auth", None) else None
            names[user_id] = getattr(user, "name", None)
        return names[user_id]
    events = []
    for run in getattr(runtime.role_runs, "runs", []):
        if run.get("kind") == "pipeline" and run.get("status") != "running":
            events.append({"at": run.get("finished") or run.get("started"), "kind": "run", "status": run.get("status"),
                           "trigger": run.get("trigger"), "only": run.get("only"), "progress": run.get("progress") or {},
                           "error": run.get("error"),
                           **({"picked": True} if run.get("trigger") == "manual" and "picked" in run else {})})
    for item in runtime.improvements.items.values():
        title = display_title(item)
        if item.get("copy"):
            events.append({"at": item.get("created"), "kind": "posted", "id": item["id"], "title": title})
        for decision in item.get("decisions", []):
            events.append({"at": decision.get("at"), "kind": "decision", "id": item["id"], "title": title,
                           "decision": decision["decision"], "reason": decision.get("reason"),
                           "outcome": decision.get("outcome"),
                           "auto": decision.get("auto", False), "by": await name(decision.get("by"))})
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    connection.send_result(msg["id"], {"events": events[:100]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversations",
                                  probatio.Optional("query"): str,
                                  probatio.Optional("days"): probatio.Any(None, probatio.All(int, probatio.Range(min=1))),
                                  probatio.Optional("order"): probatio.In(["newest", "oldest", "effort"]),
                                  probatio.Optional("offset", default=0): probatio.All(int, probatio.Range(min=0)),
                                  probatio.Optional("hidden", default=False): bool,
                                  probatio.Optional("statuses"): [str],
                                  probatio.Optional("ids"): probatio.All([str], probatio.Length(max=5000))})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversations(hass, connection, msg):
    """A page of the kept conversations, found by what was said and when (conversations.listed); hidden ones too if
    asked for, only those standing as asked (statuses), and where each of ids stands, whatever the filters."""
    from .conversations import listed
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    kept = await runtime.transcripts.all()
    connection.send_result(msg["id"], listed(runtime, kept, msg.get("query"), msg.get("days"), msg.get("order"),
                                             msg.get("offset", 0), hidden=msg.get("hidden", False),
                                             statuses=msg.get("statuses"), ids=msg.get("ids", ())))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversation",
                                  probatio.Required("recording_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversation(hass, connection, msg):
    """One conversation to read: what was said, turn by turn, and where it stands."""
    from .conversations import kept, shown
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    if not kept(runtime, msg["recording_id"]):
        connection.send_error(msg["id"], "not_found", "That conversation isn't kept.")
        return
    connection.send_result(msg["id"], shown(runtime, await runtime.transcripts.all(), msg["recording_id"]))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversation/send",
                                  probatio.Required("recording_id"): str,
                                  probatio.Optional("note", default=""): probatio.All(str, probatio.Length(max=500))})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversation_send(hass, connection, msg):
    """Send a conversation in (flow.Flow.sent_in), as Assist's file_improvement tool does, so the pipeline takes it in
    (at its next run; right away with on-demand improvements on), past the quick check with a note, or sent in again
    after an earlier round looked at it (flow.deliberate). One in the pipeline already isn't sent in again. One in
    Your household goes in again: it gets one fresh full analysis that reads the note as feedback, and a new round
    with fresh tests. It answers where it stands now (status), and whether the list leaves it out (hidden)."""
    from .conversations import kept, row
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    rid = msg["recording_id"]
    if not kept(runtime, rid):
        connection.send_error(msg["id"], "not_found", "That conversation isn't kept.")
        return
    if not row(runtime, rid)["sendable"]:  # In the pipeline already (Your household, or new, can be sent in).
        connection.send_error(msg["id"], "already_in", "The improver already has that conversation.")
        return
    await runtime.flow.sent_in(rid, msg.get("note", ""), by=connection.user.id)
    if runtime.recorder.index[rid].get("status") != "active":  # Ended: on-demand, it gets its run now.
        runtime.pipeline.request(rid)
    now = row(runtime, rid)
    connection.send_result(msg["id"], {"status": now["status"], "on_demand": runtime.pipeline.on_demand,
                                       "hidden": now["hidden"]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversation/hide",
                                  probatio.Required("recording_id"): str,
                                  probatio.Optional("hidden", default=True): bool})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversation_hide(hass, connection, msg):
    """Archive a conversation, out of the pipeline and the list, or (hidden: false) unarchive it: back in the list,
    and kept there while it stands where it does (flow.Flow.archive). It's kept, to check later changes against. One
    an improvement of which waits for the household isn't archived (flow.Flow.waits). Whether the list leaves it out
    now (hidden)."""
    from .conversations import kept, row
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    if not kept(runtime, msg["recording_id"]):
        connection.send_error(msg["id"], "not_found", "That conversation isn't kept.")
        return
    await runtime.flow.archive(msg["recording_id"], archived=msg.get("hidden", True))
    connection.send_result(msg["id"], {"hidden": row(runtime, msg["recording_id"])["hidden"]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversation/delete",
                                  probatio.Required("recording_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversation_delete(hass, connection, msg):
    """Delete a conversation for good (Recorder.forget): its recording and what the improver derived from it. During
    a run it's queued (Recorder.forget_later): gone from view at once, and deleted when the run ends."""
    from .conversations import kept
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    if not kept(runtime, msg["recording_id"]):
        connection.send_error(msg["id"], "not_found", "That conversation isn't kept.")
        return
    queued = runtime.pipeline.running
    await (runtime.recorder.forget_later if queued else runtime.recorder.forget)(msg["recording_id"])
    connection.send_result(msg["id"], {"deleted": msg["recording_id"], **({"queued": True} if queued else {})})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversations/hide",
                                  probatio.Required("recording_ids"): probatio.All([str], probatio.Length(max=5000)),
                                  probatio.Optional("hidden", default=True): bool})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversations_hide(hass, connection, msg):
    """Archive many conversations at once (the home page's selection), or (hidden: false) unarchive them; those no
    longer kept are left out, and so are those an improvement of which waits for the household, from an archive
    (flow.Flow.waits). How many changed."""
    from .conversations import kept
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    keys = [rid for rid in dict.fromkeys(msg["recording_ids"]) if kept(runtime, rid)]
    changed = await runtime.flow.archive(*keys, archived=msg.get("hidden", True)) if keys else []
    connection.send_result(msg["id"], {"changed": len(changed)})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/conversations/delete",
                                  probatio.Required("recording_ids"): probatio.All([str], probatio.Length(max=5000))})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversations_delete(hass, connection, msg):
    """Delete many conversations at once (the home page's selection), as conversation/delete does one: during a run
    they're queued. Those no longer kept are left out."""
    from .conversations import kept
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    keys = [rid for rid in dict.fromkeys(msg["recording_ids"]) if kept(runtime, rid)]
    queued = runtime.pipeline.running
    if keys:
        await (runtime.recorder.forget_later if queued else runtime.recorder.forget)(*keys)
    connection.send_result(msg["id"], {"deleted": keys, **({"queued": True} if queued else {})})


def register_commands(hass):
    for handler in (ws_list, ws_get, ws_decide, ws_household, ws_run_status, ws_run_start,
                    ws_run_cancel, ws_settings, ws_settings_set, ws_settings_models, ws_settings_create,
                    ws_settings_recommended, ws_practice, ws_assistant_prefer, ws_log, ws_household_edit,
                    ws_conversations, ws_conversation, ws_conversation_send, ws_conversation_hide,
                    ws_conversation_delete, ws_conversations_hide, ws_conversations_delete, ws_conflict_resolve,
                    ws_conversation_test):
        websocket_api.async_register_command(hass, handler)
    from .devview import COMMANDS  # The developer view's: they answer only in developer mode.
    for handler in COMMANDS:
        websocket_api.async_register_command(hass, handler)


async def async_setup_panel(hass, pending=0):
    """The sidebar panel and its script, where Home Assistant's frontend runs (not in offline tests)."""
    register_commands(hass)
    if "frontend" not in hass.config.components or getattr(hass, "http", None) is None:
        return False
    from homeassistant.components.http import StaticPathConfig
    if not hass.data.get(f"{DOMAIN}_static"):  # Static paths can't be removed, so only once per run.
        await hass.http.async_register_static_paths([StaticPathConfig(
            STATIC, str(Path(__file__).parent / "www"), cache_headers=False)])
        hass.data[f"{DOMAIN}_static"] = True
    hass.data[PANEL_KEY] = True
    set_count(hass, pending, update=False)
    return True


def set_count(hass, count, *, update=True):
    """The panel's sidebar title, with how many improvements wait for review: HA's sidebar has no badges for
    integrations' panels, but a panel's title can change. Registered as panel_custom registers panels, so the
    title can be updated in place."""
    if not hass.data.get(PANEL_KEY):
        return
    from homeassistant.components import frontend
    frontend.async_register_built_in_panel(
        hass, component_name="custom", sidebar_title=f"Assist Improver ({count})" if count else "Assist Improver",
        sidebar_icon="mdi:auto-fix", frontend_url_path=URL_PATH, require_admin=True, update=update,
        config={"_panel_custom": {"name": "assist-improver-panel", "embed_iframe": False, "trust_external": False,
                                  "handle_safe_area": False, "module_url": f"{STATIC}/panel.js?v={VERSION}"}})


def remove_panel(hass):
    from homeassistant.components import frontend
    hass.data.pop(PANEL_KEY, None)
    frontend.async_remove_panel(hass, URL_PATH, warn_if_unknown=False)
