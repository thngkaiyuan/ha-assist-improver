"""The review panel: improvements as a code-review page, where the user reviews and decides.

A sidebar panel (admins only) served from this integration: a web component in
www/panel.js that talks to Home Assistant over websocket commands (improvements
and decisions, runs, household knowledge, settings). Each review has its own
address, so the notification links straight to it; settings are at
/assist-improver?view=settings.
"""

import difflib
from pathlib import Path

import probatio
from homeassistant.components import websocket_api
from homeassistant.core import callback

from .cases import CaseError
from .changeset import plan_steps
from .const import DOMAIN, VERSION
from .improvements import DECISIONS, KINDS, PENDING, benefits, choices, display_title

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
    """A test's outcome in a few words: the goal now met, fewer turns, or just improved."""
    if test.get("status") != "awaiting_review":
        return "not improved"
    if test.get("goal_newly_met"):
        return "goal now met"
    before, after = test.get("turns_before"), test.get("turns_after")
    if isinstance(before, int) and isinstance(after, int) and after < before:
        return f"{before} \u2192 {after} turns"
    return "improved"


def safety(item, steps, already):
    """Why it's safe, stated by code from the tests and the changeset (never written by a model)."""
    facts = []
    if item.get("tests"):
        facts.append("Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your "
                     "home moved.")
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
        elif kind == "assistant_api":
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


def review(item, planned=None, names=None):
    """Everything the panel shows for one improvement, built by code. names: {user id: name} for household lines."""
    enabled, removed = benefits(item)
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
            "safety": safety(item, steps, already), "status": item["status"], "enabled": enabled,
            "recent_outcome": recent, "recent_outcome_ok": recent_ok, "revises": item.get("revises"),
            "removed": removed, "conflicts": conflicts, "changes": changes, "cause": item["cause"],
            "tests": [{"summary": t["summary"], "improved": t["status"] == "awaiting_review",
                       "result": test_result(t)} for t in item["tests"]],
            "untested": len(item["untested_goals"]),
            "choices": [{"value": d, "label": DECISIONS[d][1]} for d in choices(item)]}


def outcome_text(item):
    """What a decision did, in a sentence or two."""
    outcome = item["outcome"]
    status = outcome["status"]
    if status == "recorded":
        return "Thanks, your decision is recorded."
    if status == "done":
        return ("Applied. Assist uses it from your next request; no script was run."
                if item["status"] == "applied" else "Undone: what was there before is back.") + (
            " Left as you changed it: " + "; ".join(l["what"] for l in outcome["left"]) if outcome.get("left") else "")
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


def _improvements(hass):
    return hass.data[DOMAIN].improvements


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/improvements"})
@websocket_api.require_admin
@callback
def ws_list(hass, connection, msg):
    items = _improvements(hass).listed()
    connection.send_result(msg["id"], {"improvements": [
        {"id": i["id"], "title": display_title(i), "status": i["status"], "pending": i["status"] in PENDING,
         "enabled": len(benefits(i)[0]), "removed": len(benefits(i)[1])} for i in items]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/improvement",
                                  probatio.Required("improvement_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get(hass, connection, msg):
    try:
        item = _improvements(hass).get(msg["improvement_id"])
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
    result = review(item, planned, names)
    if item["status"] not in PENDING:  # Decided: the next one waiting is one click away.
        nxt = next((i for i in _improvements(hass).pending() if i["id"] != item["id"]), None)
        result["next_pending"] = {"id": nxt["id"], "title": display_title(nxt)} if nxt else None
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/decide",
                                  probatio.Required("improvement_id"): str, probatio.Required("decision"): str,
                                  probatio.Optional("note", default=""): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_decide(hass, connection, msg):
    try:
        item = await _improvements(hass).decide(msg["improvement_id"], msg["decision"], note=msg["note"],
                                                user_id=connection.user.id)
    except CaseError as err:
        connection.send_error(msg["id"], "decision", str(err))
        return
    connection.send_result(msg["id"], {"result": outcome_text(item), "status": item["status"],
                                       "ok": item["outcome"]["status"] in ("done", "recorded")})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/household"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_household(hass, connection, msg):
    """The household knowledge (everyone's lines, then each person's), and which assistants read it."""
    from .household import KEY, assistants, normalized, pipelines, update_issues
    update_issues(hass)  # Repairs follows what the panel shows.
    knowledge = normalized(getattr(hass.data.get(KEY), "knowledge", None))
    lines = len(knowledge["everyone"]) + sum(len(v) for v in knowledge["people"].values())
    people = []
    for user_id, mine in sorted(knowledge["people"].items()):
        user = await hass.auth.async_get_user(user_id) if getattr(hass, "auth", None) else None
        people.append({"user_id": user_id, "name": getattr(user, "name", None) or "Someone", "lines": list(mine)})
    me = connection.user
    connection.send_result(msg["id"], {"lines": lines, "everyone": list(knowledge["everyone"]), "people": people,
                                       "me": {"user_id": me.id, "name": getattr(me, "name", None) or "You"},
                                       "pipelines": pipelines(hass), "assistants": [
        {"target": f"{entry.entry_id}/{sub.subentry_id}", "title": sub.title, "using": using}
        for entry, sub, using in assistants(hass)]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/household/enable",
                                  probatio.Required("target"): str, probatio.Optional("on", default=True): bool})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_household_enable(hass, connection, msg):
    """Switch one assistant from Assist to Assist with household knowledge, or back (nothing else in it changes; the
    lines stay in the household store)."""
    from .changeset import read, write
    from .household import API_ID
    current = await read(hass, "assistant_api", msg["target"])
    if current is None or "assist" not in current and API_ID not in current:
        connection.send_error(msg["id"], "not_found", "That assistant doesn't control Home Assistant through Assist.")
        return
    on = msg.get("on", True)
    old, new = ("assist", API_ID) if on else (API_ID, "assist")
    if old in current:
        await write(hass, "assistant_api", msg["target"], list(dict.fromkeys(new if a == old else a for a in current)))
    connection.send_result(msg["id"], {"using": on})


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
    connection.send_result(msg["id"], hass.data[DOMAIN].pipeline.status())


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/run/start",
                                  probatio.Required("confirmed"): True, probatio.Optional("force", default=False): bool,
                                  probatio.Optional("only"): probatio.In(("triage", "analyze", "propose", "test", "write"))})
@websocket_api.require_admin
@callback
def ws_run_start(hass, connection, msg):
    """Start a run; the panel asks the user to confirm what it will spend first."""
    try:
        run = hass.data[DOMAIN].pipeline.start(trigger="manual", force=msg["force"], only=msg.get("only"))
    except CaseError as err:
        connection.send_error(msg["id"], "busy", str(err))
        return
    connection.send_result(msg["id"], {"run": run})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/run/cancel"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_run_cancel(hass, connection, msg):
    await hass.data[DOMAIN].pipeline.cancel()
    connection.send_result(msg["id"], {"cancelled": True})


def _entry(hass):
    return hass.data[DOMAIN].pipeline.entry


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings"})
@websocket_api.require_admin
@callback
def ws_settings(hass, connection, msg):
    """The settings view: each AI role's model, the AI Tasks to choose from, and the other settings."""
    from .settings import view
    connection.send_result(msg["id"], view(hass, _entry(hass), connection.user.id))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/settings/set",
                                  probatio.Optional("role"): str, probatio.Optional("entity_id"): probatio.Any(str, None),
                                  probatio.Optional("tests_per_goal"): int, probatio.Optional("schedule"): str,
                                  probatio.Optional("stage"): str, probatio.Optional("pause"): bool,
                                  probatio.Optional("auto_accept"): bool})
@websocket_api.require_admin
@callback
def ws_settings_set(hass, connection, msg):
    """Change one setting; it applies right away, even during a run."""
    from .settings import SettingsError, updated, view
    entry = _entry(hass)
    try:
        options = updated(hass, entry, role=msg.get("role"), entity_id=msg.get("entity_id"),
                          tests_per_goal=msg.get("tests_per_goal"), schedule=msg.get("schedule"),
                          stage=msg.get("stage"), pause=msg.get("pause"), auto_accept=msg.get("auto_accept"))
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
    from .settings import SettingsError, updated, view
    entity_id, reason = await create_ai_task(hass, msg["entry_id"], "chosen", model=msg["model"], name=msg.get("name"))
    if entity_id is None:
        connection.send_error(msg["id"], "create_failed", reason)
        return
    entry = _entry(hass)
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
    entry = _entry(hass)
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
    mode, user = hass.data[MODE_KEY], connection.user
    if msg["on"]:
        await mode.async_turn_on(user.id, user.name or "someone")
    else:
        await mode.async_turn_off(user.id)
    connection.send_result(msg["id"], view(hass, _entry(hass), user.id))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/log"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_log(hass, connection, msg):
    """What the improver did, newest first: runs, improvements going up for review, and decisions on them."""
    recorder = hass.data[DOMAIN]
    names = {}

    async def name(user_id):
        if user_id not in names:
            user = await hass.auth.async_get_user(user_id) if user_id and getattr(hass, "auth", None) else None
            names[user_id] = getattr(user, "name", None)
        return names[user_id]
    events = []
    for run in getattr(recorder.role_runs, "runs", []):
        if run.get("kind") == "pipeline" and run.get("status") != "running":
            events.append({"at": run.get("finished") or run.get("started"), "kind": "run", "status": run.get("status"),
                           "trigger": run.get("trigger"), "only": run.get("only"), "progress": run.get("progress") or {},
                           "error": run.get("error")})
    for item in recorder.improvements.items.values():
        title = display_title(item)
        if item.get("copy"):
            events.append({"at": item.get("created"), "kind": "posted", "id": item["id"], "title": title})
        for decision in item.get("decisions", []):
            events.append({"at": decision.get("at"), "kind": "decision", "id": item["id"], "title": title,
                           "decision": decision["decision"], "outcome": decision.get("outcome"),
                           "auto": decision.get("auto", False), "by": await name(decision.get("by"))})
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    connection.send_result(msg["id"], {"events": events[:100]})


def register_commands(hass):
    for handler in (ws_list, ws_get, ws_decide, ws_household, ws_household_enable, ws_run_status, ws_run_start,
                    ws_run_cancel, ws_settings, ws_settings_set, ws_settings_models, ws_settings_create,
                    ws_settings_recommended, ws_practice, ws_assistant_prefer, ws_log, ws_household_edit):
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
