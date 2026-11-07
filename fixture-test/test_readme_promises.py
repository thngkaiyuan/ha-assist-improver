"""The README's promises (README.md) that no other test held closely: a whole run changes nothing in your home until
you accept; a schedule sends every conversation to the improver; nothing comes to us; conversations are kept in your
Home Assistant, condensed after 30 days but never deleted, and the improver's findings about one stay with it; the
improver's own logs are kept a year at most; what it learns about your household reaches every assistant;
uninstalling after real use leaves only what you accepted; the package installs through HACS and Add integration as
Getting started says; and Actions names the integration's actions as Home Assistant shows them. A test holds each
README promise true (DETAILS P8.12.4)."""

import ast
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re

import pytest
from homeassistant.util.dt import utcnow

from recordings import before, make_recording
from kit import FIRST, candidate_turn, improver, runtime_with_flow, variant  # noqa: F401 - improver: a fixture.
from test_changeset import tested as test_record
from test_conversations import ask
from test_milestone1 import recording_hass  # noqa: F401 - fixture.
from test_spec_household import home  # noqa: F401 - a fixture: Home Assistant with Assist and two people.
from test_spec_retention_recording import arrive, kept, newest, recording  # noqa: F401 - kept is a fixture.
from test_trace import write_trace
from custom_components.assist_improver import async_remove_entry, async_unload_entry, household, panel, retention, trace
from custom_components.assist_improver.cases import recording_turns
from custom_components.assist_improver.const import DOMAIN

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / DOMAIN
README = (ROOT / "README.md").read_text()


def your_home(hass):
    """What an improvement could change in Home Assistant: every state, scripts.yaml, each entity's name, aliases and
    exposure, devices' and areas' names, the assistants' configuration, and the household knowledge Assist reads."""
    from homeassistant.components.homeassistant.exposed_entities import async_get_assistant_settings
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
    return {
        "states": {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()},
        "scripts.yaml": Path(hass.config.path("scripts.yaml")).read_text(),
        "entities": {e.entity_id: (e.name, sorted(e.aliases), {k: dict(v) for k, v in e.options.items()})
                     for e in er.async_get(hass).entities.values()},
        "exposed": {k: dict(v) for k, v in async_get_assistant_settings(hass, "conversation").items()},
        "devices": {d.id: (d.name_by_user, d.area_id) for d in dr.async_get(hass).devices},
        "areas": {a.id: (a.name, sorted(a.aliases)) for a in ar.async_get(hass).async_list_areas()},
        "assistants": {e.entry_id: (dict(e.options), {sid: dict(s.data) for sid, s in e.subentries.items()})
                       for e in hass.config_entries.async_entries() if e.domain != DOMAIN},
        "household": deepcopy(hass.data[household.KEY].knowledge),
    }


async def a_run(improver):
    """One run over a conversation with friction: its fix tested in the sandbox and written up."""
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    return run


@pytest.mark.asyncio
async def test_a_whole_run_changes_nothing_in_your_home_until_you_accept(improver, effect_guards):
    """README: 'Nothing in your home changes until you accept. Tests run in a sandboxed copy of your home.'"""
    from custom_components.assist_improver.snapshot import snapshot
    hass = improver.hass
    snapshot(hass)  # The recorder has read the home, as with every recorded turn: HA keeps its default exposure then.
    before = your_home(hass)
    run = await a_run(improver)
    assert your_home(hass) == before and effect_guards == []
    assert run["admitted"] == run["moved"] == ["friction01"] and run["results"] == {
        "friction01": {"stage": "Your review", "outcome": None, "held": None}}  # Up for review, nothing applied.
    assert run["progress"]["new_improvements"] == 1
    [item] = improver.runtime.improvements.pending()  # Up for review, after its test in the sandbox.
    assert item["test"]["validated"]
    status, accepted = await ask(hass, panel.ws_decide, improvement_id=item["id"], decision="accept", note="")
    after = your_home(hass)
    assert (status, accepted["status"]) == ("ok", "applied") and "press_the_relay" in after["scripts.yaml"]
    assert "script.press_the_relay" in after["states"] and after["exposed"]["script.press_the_relay"]["should_expose"]


@pytest.mark.asyncio
async def test_a_schedule_sends_every_conversation_to_the_improver_on_its_own(improver, monkeypatch):
    """README Getting started 4: 'Set a schedule in its settings, and every conversation goes to the improver.' Daily
    runs start at 03:00, weekly ones on Sundays, each taking in every conversation that finished since."""
    from homeassistant.helpers import event
    from custom_components.assist_improver.pipeline import schedule
    timers = []
    monkeypatch.setattr(event, "async_track_time_change",
                        lambda hass, action, **at: timers.append((action, at)) or (lambda: None))
    hass, entry, pipeline = improver.hass, improver.entry, improver.runtime.pipeline
    assert schedule(hass, entry, pipeline, "off") is None and timers == []
    schedule(hass, entry, pipeline, "weekly")
    [(weekly, at)] = timers
    assert at == {"hour": 3, "minute": 0, "second": 0}
    await improver.arrives(improver.friction)
    await improver.arrives(variant(make_recording(), "smooth0001", "Turn the fixture relay off.", turns=1))
    weekly(datetime(2026, 10, 24, 3, tzinfo=UTC))  # A Saturday.
    assert not pipeline.running and pipeline.current is None
    improver.client.blocks = before(improver.friction) + candidate_turn()
    weekly(datetime(2026, 10, 25, 3, tzinfo=UTC))  # Sunday.
    assert pipeline.running
    await pipeline._task
    run = pipeline.current
    assert run["trigger"] == "scheduled" and run["status"] == "done"
    assert sorted(run["admitted"]) == sorted(run["moved"]) == ["friction01", "smooth0001"]  # Every one, on its own.
    assert run["results"] == {"friction01": {"stage": "Your review", "outcome": None, "held": None},
                              "smooth0001": {"stage": "Your household", "outcome": "looked smooth", "held": None}}
    assert not any(improver.runtime.flow.waiting(rid) for rid in improver.recorder.index)  # None left behind.
    schedule(hass, entry, pipeline, "daily")
    daily = timers[-1][0]
    daily(datetime(2026, 10, 27, 3, tzinfo=UTC))  # A Tuesday.
    assert pipeline.running
    await pipeline._task


NETWORK_MODULES = {"aiohttp", "httpx", "requests", "urllib", "urllib3", "http", "socket", "ssl", "websocket",
                   "websockets", "smtplib", "ftplib", "xmlrpc", "paho", "grpc", "botocore", "boto3"}
NETWORK_HELPERS = ("homeassistant.helpers.aiohttp_client", "homeassistant.helpers.httpx_client",
                   "homeassistant.components.cloud", "homeassistant.components.webhook",
                   "homeassistant.components.analytics")
NETWORK_CALLS = {"async_get_clientsession", "async_create_clientsession", "get_async_client",
                 "create_async_httpx_client", "open_connection", "create_connection", "create_datagram_endpoint",
                 "sock_connect", "getaddrinfo", "urlopen"}
# The replay worker's guard (guard.py) opens nothing: it reads where sockets are asked to go, to refuse them, resolves
# the household's provider's own hosts to let them through, reads sqlite's file names, and imports urllib3 before it
# refuses binds (urllib3 probes for IPv6 with one).
GUARD = {"guard.py: imports socket", "guard.py: imports urllib.parse", "guard.py: imports urllib.parse.urlsplit",
         "guard.py: imports urllib.parse.unquote", "guard.py: imports urllib3", "guard.py: uses getaddrinfo"}


def test_nothing_comes_to_us_the_improver_reaches_nothing_but_the_ai_models_you_chose():
    """README: 'Sent during a run: the AI models you chose get what they need to do their work. Nothing comes to us.'
    The improver reaches models only through Home Assistant's AI Tasks and assistants: no module opens a connection or
    takes a network client, nothing is installed beside it, and the panel loads only its own files."""
    found = []
    for path in sorted(COMPONENT.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                if name.split(".")[0] in NETWORK_MODULES or name.startswith(NETWORK_HELPERS):
                    found.append(f"{path.name}: imports {name}")
            called = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""
            if called in NETWORK_CALLS:
                found.append(f"{path.name}: uses {called}")
        found += [f"{path.name}: {url}" for url in re.findall(r"\b(?:https?|wss?)://[^\s\"')]+", path.read_text())]
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    if manifest["requirements"]:
        found.append(f"manifest.json installs {manifest['requirements']}")
    for path in sorted((COMPONENT / "www").rglob("*.js")):
        text = path.read_text()
        found += [f"{path.name}: {m}" for m in re.findall(r"\b(?:fetch|XMLHttpRequest|WebSocket|sendBeacon|EventSource)\b"
                                                           r"|(?:https?|wss?):\/\/[^\s\"'`)]+", text)]
        found += [f"{path.name}: imports {m}" for m in re.findall(r"import\(\s*[`'\"]([^`'\"]+)", text)
                  if not m.startswith("./")]
    assert [f for f in found if f not in GUARD] == []


@pytest.mark.asyncio
async def test_conversations_are_kept_in_home_assistants_own_storage(improver):
    """README: 'Kept in your Home Assistant: your Assist conversations ..., stored next to the history Home Assistant
    already keeps.' A conversation is a file in Home Assistant's own .storage folder, beside its other stores."""
    await improver.arrives(improver.friction)
    path = Path(improver.hass.config.path(".storage", f"{DOMAIN}.session_{improver.friction['id']}"))
    assert improver.recorder.store(improver.friction["id"]).path == str(path)
    saved = json.loads(path.read_text())["data"]
    assert saved["id"] == improver.friction["id"] and FIRST in json.dumps(saved)


@pytest.mark.asyncio
async def test_conversations_are_kept_in_full_for_30_days_then_condensed_never_deleted(kept, effect_guards):
    """README: 'conversations are kept in full for at least 30 days, then condensed, keeping enough for the improver to
    see your household's needs as a whole.' The daily tidy-up condenses none before 30 days, and an old one is
    condensed, never deleted: years on, what was said is still there."""
    now = utcnow()
    await newest(kept, now)  # The newest, which stay in full however old.
    await arrive(kept, recording("this-month", (now - timedelta(days=29)).isoformat()))
    await arrive(kept, recording("years-ago", (now - timedelta(days=3 * 365)).isoformat()))
    said = [t["messages"][0]["content"] for t in recording_turns(recording("any", now.isoformat()), replay=False)]
    await retention.prune(kept, referenced=set(), now=now)
    assert not (await kept.export("this-month")).get("compact") and (await kept.export("years-ago"))["compact"]
    for later in (365, 10 * 365):  # Nightly tidy-ups, years on.
        await retention.prune(kept, referenced=set(), now=now + timedelta(days=later))
    for key in ("this-month", "years-ago"):
        assert key in kept.index
        condensed = await kept.export(key)
        assert condensed["compact"]
        assert [t["messages"][0]["content"] for t in recording_turns(condensed, replay=False)] == said
    assert effect_guards == []


@pytest.mark.asyncio
async def test_no_trace_outlives_a_year(hass):
    """README: 'Its own logs are kept only as long as they're useful, a year at most.' The traces (the developer
    view, with the improver's log lines) a year old go at the daily tidy-up, however little room they take."""
    for trace_id, days in (("today", 0), ("a_year_and_a_day", 366)):
        write_trace(hass, trace_id, 100, days)
    await retention.prune_traces(hass)
    assert [f["id"] for f in trace.stored(hass.config.path(".storage"))] == ["today"]


@pytest.mark.parametrize("record", [
    pytest.param({"kind": "pipeline", "status": "done", "progress": {"timings": {}}}, id="run-with-timings"),
    pytest.param({"role": "hypothesis_reviewer", "status": "completed", "reply": "Approve."}, id="ai-call-diagnostics"),
    pytest.param({"role": "editor", "status": "completed", "reply": "Press the relay in one go."}, id="write-up-call"),
    pytest.param({"kind": "journey_analyzer", "recording_id": "r1", "status": "failed", "answer_status": "error"},
                 id="failed-attempt"),
    pytest.param({"kind": "hypothesis_generator", "status": "completed", "output": {"hypotheses": []}},
                 id="latest-proposal"),
])
def test_nothing_in_the_logs_outlives_a_year(record):
    """README: 'Its own logs are kept only as long as they're useful, a year at most.' The logs are the run records,
    the AI calls' diagnostics and the traces (test_no_trace_outlives_a_year): each goes once it's a year old, at the
    latest. A conversation's findings aren't logs (the next test)."""
    old = (utcnow() - timedelta(days=366)).isoformat()
    kept_runs, _ = retention.prune_runs([{"id": "old", "started": old, "finished": old, **record}], {"r1"})
    assert [r["id"] for r in kept_runs if r["id"] == "old"] == []


@pytest.mark.asyncio
async def test_a_conversations_findings_stay_while_it_is_kept_even_condensed_and_go_when_it_is_deleted(
        improver, monkeypatch):
    """Kai, 2026-10-25: the improver's findings about a conversation (its analysis in its flow record, which every
    stage reads) are conversation data, not logs. They stay as long as the conversation is kept, condensed or not,
    years on, so it's never analysed again, while the run log's copies go as diagnostics; deleting it for good takes
    them too, and no tidy-up brings them back."""
    hass, recorder, runtime = improver.hass, improver.recorder, improver.runtime
    rid = improver.friction["id"]
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose"]}  # Analysed; nothing proposed.
    await improver.arrives(improver.friction)
    recorder.index[rid]["started"] = utcnow().isoformat()
    assert (await improver.run())["moved"] == [rid]
    flow = Path(hass.config.path(".storage", f"{DOMAIN}.flow"))
    log = Path(hass.config.path(".storage", f"{DOMAIN}.role_runs"))

    def logged():  # Its records in the run log.
        return [r for r in runtime.role_runs.runs if r.get("recording_id") == rid]

    def analysed():  # Its analysis, in its flow record.
        rounds = (runtime.flow.conversations.get(rid) or {}).get("rounds") or {}
        return ((rounds.get("1") or {}).get("records", {}).get("Analyze") or {}).get("analysis")
    assert any(r.get("analysis") for r in logged()) and analysed()
    monkeypatch.setattr(retention, "KEEP_NEWEST", 0)  # Only its age decides.
    for years in (1, 3):  # The nightly tidy-up, years on.
        later = utcnow() + timedelta(days=365 * years + 1)
        await retention.prune(recorder, referenced=set(), now=later)
        await runtime.role_runs.prune(set(recorder.index), now=later)
    assert recorder.index[rid]["stored"] == "compact"  # Condensed, and kept.
    assert analysed() and logged() == []
    assert not runtime.flow.waiting(rid) and runtime.flow.place(rid)["stage"] == "Propose"  # Not analysed again.
    assert rid in json.loads(flow.read_text())["data"]["conversations"]
    await recorder.forget(rid)  # Delete for good.
    assert logged() == [] and analysed() is None and rid not in json.loads(flow.read_text())["data"]["conversations"]
    assert not any(r.get("recording_id") == rid for r in json.loads(log.read_text())["data"]["runs"])
    await runtime.role_runs.prune(set(recorder.index), now=later)
    assert logged() == [] and analysed() is None


@pytest.mark.asyncio
async def test_what_the_improver_learns_about_your_household_reaches_every_assistant_that_controls_home_assistant(
        home):
    """README Good to know: 'Every assistant benefits. What the improver learns about your household reaches every
    assistant that controls Home Assistant.' An accepted line, tested on one assistant, reaches every assistant on
    Assist, on any provider, from its next request, with nothing to turn on; an assistant that doesn't control Home
    Assistant isn't one of them."""
    from types import SimpleNamespace
    from conftest import serve
    from test_spec_household import ALEX, Assistant, Panel, what_it_reads
    from custom_components.assist_improver.improvements import Improvements
    assert "**Every assistant benefits.** What the improver learns about your household reaches every assistant that " \
           "controls Home Assistant.\n" in README
    hass = home
    await household.async_setup(hass)
    tested = Assistant(hass, "Claude", ["assist"])
    others = [Assistant(hass, "DeepSeek", "assist"), Assistant(hass, "Haiku", ["assist"], prompt="Be kind.")]
    chat = Assistant(hass, "Chat only", None)
    improvements = Improvements(hass, None)
    await improvements.start()
    improvements.items["imp1"] = {
        "id": "imp1", "title": "Both garage doors", "cause": "Alex meant both doors twice.", "status": "awaiting_review",
        "created": "1", "decisions": [], "application": None, "history": [], "copy": {"status": "failed"},
        "test": test_record(), "fixes": [], "setup": {}, "tested": {"scripts": {}, "exposure": {}}, "user_ids": [ALEX],
        "changes": [{"kind": "preference", "for": "everyone", "text": "The garage means both garage doors."}],
        "provider": {"config_entry_id": tested.entry.entry_id, "subentry_id": "sub", "title": "Claude"}}
    serve(hass, runtime_with_flow(hass, improvements=improvements))
    before = {a.target: a.data for a in [tested, *others, chat]}
    assert (await Panel(hass)("decide", improvement_id="imp1", decision="accept"))["status"] == "applied"
    for assistant in (tested, *others):
        assert "The garage means both garage doors." in await what_it_reads(hass, assistant, ALEX)
    assert "The garage means both garage doors." not in await what_it_reads(hass, chat, ALEX)
    assert {a.target: a.data for a in [tested, *others, chat]} == before  # Nothing switched, nothing to turn on.


@pytest.mark.asyncio
async def test_uninstalling_after_real_use_removes_all_it_stored_and_leaves_what_you_accepted(improver):
    """README: 'Uninstalling removes everything the improver stored and the AI Tasks it set up. Your accepted
    improvements stay.' After a real run and an accept, every store the improver wrote goes, and the accepted script
    stays in scripts.yaml, in Home Assistant and exposed to Assist. (test_uninstall.py covers the AI Tasks.)"""
    hass = improver.hass
    run = await a_run(improver)
    assert run["results"]["friction01"]["stage"] == "Your review"
    [item] = improver.runtime.improvements.pending()
    assert (await ask(hass, panel.ws_decide, improvement_id=item["id"], decision="accept", note=""))[0] == "ok"
    storage = Path(hass.config.path(".storage"))
    wrote = {p.name for p in storage.iterdir() if p.name.startswith(f"{DOMAIN}.")}
    assert {f"{DOMAIN}.{name}" for name in ("session_friction01", "index", "role_runs", "improvements", "flow",
                                            "changeset_journal", "replays")} <= wrote
    assert any(name.startswith(trace.STORE_PREFIX) for name in wrote)
    accepted = your_home(hass)
    accepted.pop("household")  # The improver's own store, which goes (its lines: test_spec_household.py).
    await async_unload_entry(hass, improver.entry)  # Home Assistant unloads an entry, then removes it.
    await async_remove_entry(hass, improver.entry)
    await hass.async_block_till_done()
    assert sorted(p.name for p in storage.iterdir() if p.name.startswith(f"{DOMAIN}.")) == []
    left = your_home(hass)
    left.pop("household")
    assert left == accepted and "press_the_relay" in left["scripts.yaml"]


def test_the_repository_installs_through_hacs_and_add_integration_as_getting_started_says():
    """README Getting started 1-2: the button opens it in Home Assistant's Marketplace (or found there by a search),
    installed as Assist Improver, then Add integration (its button, or the menus) → Assist Improver. The README's own
    banner names the Home Assistant it runs on. Its pictures and links work where HACS shows the README: HACS resolves
    a relative <img> against Home Assistant's own address, so each is a full address into this repository, laid out
    as HACS reads it."""
    assert sorted(p.name for p in (ROOT / "custom_components").iterdir() if p.name != "__pycache__") == [DOMAIN]
    hacs = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert hacs["name"] == manifest["name"] == "Assist Improver"  # In HACS's list and in Add integration.
    assert manifest["config_flow"] is True and manifest["version"]  # Added from the UI; HACS needs a version.
    [runs_on] = re.findall(r"on Home Assistant (\d{4}\.\d+\.\d+)", README)
    assert tuple(map(int, hacs["homeassistant"].split("."))) <= tuple(map(int, runs_on.split(".")))
    assert manifest["issue_tracker"] in README  # 'open an issue' goes to this repository's issues.
    owner, repository = manifest["documentation"].removeprefix("https://github.com/").split("/")
    assert (f"redirect/hacs_repository/?owner={owner}&repository={repository}&category=integration" in README
            and f"redirect/config_flow_start/?domain={DOMAIN})" in README)  # The two buttons.
    linked = [a or b for a, b in re.findall(r"\]\(([^)#]+)\)|src=\"([^\"]+)\"", README)]
    assert [link for link in linked if not link.startswith("https://")] == []
    ours = re.compile(rf"https://(?:raw\.githubusercontent\.com/{owner}/{repository}|github\.com/{owner}/{repository}"
                      rf"/blob)/main/(.+)")
    published = [m[1] for link in linked if (m := ours.fullmatch(link))]
    assert published and [p for p in published if not (ROOT / p).is_file()] == []


def test_the_package_holds_the_integration_alone(tmp_path):
    """build_package.py's ZIP (INSTALL.md): the integration's files at their paths, and nothing else of the
    repository's (its README, docs, tools, tests)."""
    import runpy
    import zipfile
    (tmp_path / "custom_components").symlink_to(ROOT / "custom_components")
    (tmp_path / "build_package.py").write_bytes((ROOT / "build_package.py").read_bytes())
    runpy.run_path(str(tmp_path / "build_package.py"))
    version = json.loads((COMPONENT / "manifest.json").read_text())["version"]
    with zipfile.ZipFile(tmp_path / "dist" / f"assist-improver-{version}.zip") as package:
        names = package.namelist()
    shipped = sorted(str(p.relative_to(ROOT)) for p in COMPONENT.rglob("*") if p.is_file()
                     and p.suffix in {".py", ".json", ".yaml", ".png", ".js"} and "__pycache__" not in p.parts)
    assert sorted(names) == shipped and f"custom_components/{DOMAIN}/manifest.json" in names


def test_the_readmes_actions_are_the_integrations_by_the_names_home_assistant_shows():
    """README Actions: the two actions every household gets (for bug reports), each by its name in Home Assistant, and
    the ones developer mode adds."""
    from custom_components.assist_improver import DEVELOPER_SERVICES, SERVICES
    names = {action: text["name"] for action, text in json.loads((COMPONENT / "strings.json").read_text())[
        "services"].items()}
    section = README.split("\n## Actions\n", 1)[1].split("\n## ", 1)[0]
    everyone, developer = section.split("Developer mode", 1)
    assert set(names) == set(SERVICES) | set(DEVELOPER_SERVICES)
    assert re.findall(r"^- \*\*(.+?):\*\*", everyone, re.M) == [names[action] for action in SERVICES]
    assert [names[action] for action in DEVELOPER_SERVICES if names[action] not in developer] == []
