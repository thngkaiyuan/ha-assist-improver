"""Offline effect guards shared by all replay fixtures."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import types

# A replay sandbox's clock (clock.py) serves inline sandboxes from this process: its reads go in before Home Assistant
# is imported, as a worker installs them, each sandbox's threads on a clock of their own, every other thread's on the
# real one. Loaded alone, as a worker loads it (the improver's package imports Home Assistant).
_spec = importlib.util.spec_from_file_location(
    "custom_components.assist_improver.clock",
    Path(__file__).resolve().parent.parent / "custom_components" / "assist_improver" / "clock.py")
sys.modules[_spec.name] = _clock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_clock)
_clock.patch()

# HA's AI Task imports camera code, which imports turbojpeg at module load.
# No test handles images; a stub avoids building that package from source.
if "turbojpeg" not in sys.modules:
    sys.modules["turbojpeg"] = types.SimpleNamespace(TurboJPEG=object)
import platform

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant, ServiceRegistry


# HA reads the processor name once at start (a subprocess on macOS); read it before the guards are up.
platform.uname().processor

# HA writes its stores atomically (atomicwrites: a temp file, synced, renamed into place). On macOS the sync is a full
# flush of the disk's own cache (F_FULLFSYNC), which takes the disk for every process at once, so tests run side by
# side wait on each other's flushes; on Linux, as on Home Assistant OS, it's a plain fsync. Tests sync as Linux does.
import atomicwrites
assert atomicwrites._proper_fsync  # Still the name atomicwrites syncs through.
atomicwrites._proper_fsync = os.fsync


def pytest_addoption(parser):
    parser.addoption("--graph", action="store_true",
                     help="Run the spec's test graph (fixture-test/graph): each test holds a SPEC.md clause, and "
                          "passes once the code does what it says (all do today).")
    parser.addoption("--private", action="store_true",
                     help="Also replay the maintainer's stored conversations against their baseline "
                          "(test_private_replay_baseline.py, tools/private.py), as he does at a merge.")


_spec = importlib.util.spec_from_file_location("private", Path(__file__).resolve().parent.parent / "tools/private.py")
_private = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_private)
private_folder = _private.folder  # Whether or not it's there.


def private(*parts, on_demand=None):
    """A path in the maintainer's private folder (tools/private.py). The test skips, saying why, where there's none,
    as for every contributor, and an on-demand one (on_demand: pytest's config) unless --private asks for it; a path
    missing from a folder that's there fails it, as a moved one should."""
    if on_demand is not None and not on_demand.getoption("--private"):
        pytest.skip("On the maintainer's household data, on demand: --private")
    folder = private_folder()
    if not folder.is_dir():
        pytest.skip(f"Household data stays out of the checkout: no private folder at {folder} (tools/private.py)")
    if not (path := folder.joinpath(*parts)).exists():
        pytest.fail(f"No {path.relative_to(folder)} in the private folder {folder}")
    return path


def pytest_configure(config):
    config.addinivalue_line("markers", "spec(clause): the SPEC.md clause a test holds (fixture-test/graph)")
    config.addinivalue_line("markers", "refines(parent): the parent test a graph test refines (fixture-test/graph)")


def pytest_ignore_collect(collection_path, config):
    """The spec's test graph runs on its own (--graph)."""
    if "graph" in collection_path.parts and not config.getoption("--graph"):
        return True
    return None


# Tests set the integration up with a stand-in config entry (async_setup_entry called directly), which Home
# Assistant's registry doesn't know: it's listed as loaded until it's unloaded, as HA lists a real one
# (runtime.loaded), the latest first.
import custom_components.assist_improver as integration
from homeassistant.config_entries import ConfigEntries

SERVED = []  # (hass, entry), as set up.
_setup_entry, _unload_entry, _loaded_entries = (integration.async_setup_entry, integration.async_unload_entry,
                                                ConfigEntries.async_loaded_entries)


async def setup_entry(hass, entry):
    integration.panel.register_commands(hass)  # As the integration's async_setup does first, once per run.
    ok = await _setup_entry(hass, entry)
    SERVED.append((hass, entry))
    return ok


async def unload_entry(hass, entry):
    SERVED[:] = [(h, e) for h, e in SERVED if e is not entry]
    return await _unload_entry(hass, entry)


async def translated(hass):
    """The integration's texts loaded as Home Assistant loads them when it sets the integration up (it then counts as
    loaded): its errors read as the user sees them."""
    from homeassistant import loader
    from homeassistant.helpers import translation
    if loader.DATA_INTEGRATIONS not in hass.data:
        loader.async_setup(hass)
    await translation.async_load_integrations(hass, {integration.DOMAIN})
    hass.config.components.add(integration.DOMAIN)


def loaded_entries(self, domain):
    entries = _loaded_entries(self, domain)
    if domain == integration.DOMAIN:
        entries = [e for h, e in reversed(SERVED) if h is self.hass and e not in entries] + entries
    return entries


def loaded_for(hass):
    """async_loaded_entries for a test's stand-in config_entries."""
    return lambda domain: [e for h, e in reversed(SERVED) if h is hass] if domain == integration.DOMAIN else []


def improver_entry(hass, **options):
    """The improver's own config entry in Home Assistant's list, as installed with these settings (the Household
    knowledge switch reads it), and the integration loaded, so Home Assistant's Assist asks its llm platform."""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    entry = ConfigEntry(domain=integration.DOMAIN, title="Assist Improver", data={}, source="user", version=1,
                        minor_version=1, options=options, unique_id=None, discovery_keys=MappingProxyType({}),
                        state=ConfigEntryState.NOT_LOADED, subentries_data=[])
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config.components.add(integration.DOMAIN)
    return entry


def serve(hass, runtime):
    """A stand-in runtime as the loaded improver's, for a test that builds the parts itself."""
    SERVED.append((hass, types.SimpleNamespace(runtime_data=runtime)))


integration.async_setup_entry, integration.async_unload_entry = setup_entry, unload_entry
ConfigEntries.async_loaded_entries = loaded_entries


@pytest.fixture(autouse=True)
def served():
    yield SERVED
    SERVED.clear()


@pytest.fixture(autouse=True)
def service_user_on_real_accounts(monkeypatch):
    """The improver's user (service_user.py) lives in Home Assistant's own accounts and config entry: with a test's
    stand-ins for them (accounts that only look a person up, an entry with options only), there's none, as on a Home
    Assistant whose accounts can't make one. test_service_user.py runs it on the real ones."""
    from homeassistant.auth import AuthManager
    from custom_components.assist_improver import service_user
    ensure, remove = service_user.async_ensure, service_user.async_remove

    def real(hass, entry):
        return isinstance(getattr(hass, "auth", None), AuthManager) and hasattr(entry, "data")

    async def ensuring(hass, entry):
        return await ensure(hass, entry) if real(hass, entry) else None

    async def removing(hass, entry):
        if real(hass, entry):
            await remove(hass, entry)
    monkeypatch.setattr(service_user, "async_ensure", ensuring)
    monkeypatch.setattr(service_user, "async_remove", removing)


@pytest.fixture(autouse=True)
def effect_guards(monkeypatch):
    """Tripwires for accidental network, subprocess, or HA service execution."""
    attempts = []

    def deny(kind):
        def blocked(*args, **kwargs):
            attempts.append(kind)
            raise AssertionError(f"Forbidden effect: {kind}")
        return blocked

    original_call = ServiceRegistry.async_call

    async def service_blocked(registry, *args, **kwargs):
        if registry._hass.data.get("assist_improver_sandbox"):  # A sandbox's services act only on its own copy.
            return await original_call(registry, *args, **kwargs)
        if tuple(args[:2]) == ("todo", "get_items"):  # Reads a list's items (snapshot.refresh); acts on nothing.
            return await original_call(registry, *args, **kwargs)
        deny("HA service")()

    monkeypatch.setattr(ServiceRegistry, "async_call", service_blocked)
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, deny(f"socket.{method}"))
    # asyncio uses an existing AF_UNIX socketpair to wake its loop after disk
    # I/O. Keep that internal wakeup; block every IP data-send path.
    for method in ("sendto", "send", "sendall"):
        original = getattr(socket.socket, method)
        def guarded(sock, *args, _method=method, _original=original, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                deny(f"socket.{_method}")()
            return _original(sock, *args, **kwargs)
        monkeypatch.setattr(socket.socket, method, guarded)
    monkeypatch.setattr(socket, "getaddrinfo", deny("DNS"))
    monkeypatch.setattr(subprocess, "Popen", deny("subprocess"))
    # A sandbox's voice assistant sets up Home Assistant's ffmpeg, which asks the ffmpeg binary its version: the one
    # process a worker's guard lets it start (guard.py). Here no binary is asked.
    from homeassistant.components.ffmpeg import FFmpegManager

    async def no_binary(manager):
        return None, None
    monkeypatch.setattr(FFmpegManager, "async_get_version", no_binary)
    yield attempts


@pytest.fixture(autouse=True)
def household_worlds(effect_guards):
    """Graph tests' households (fixture-test/graph/harness/world.py) are closed after each test, before the guards
    are lifted."""
    yield
    if (world := sys.modules.get("harness.world")) is not None:
        world.close_all()


@pytest.fixture(scope="session", autouse=True)
def sandbox_integrations(tmp_path_factory):
    """A sandbox's own integrations (standin.write) where Home Assistant's loader finds them, as a worker's folder
    holds them: custom_components is a namespace package here, so a folder of them on the path joins it."""
    from custom_components.assist_improver.standin import write
    root = tmp_path_factory.mktemp("sandbox-integrations")
    (root / "custom_components").mkdir()
    write(root / "custom_components")
    sys.path.append(str(root))
    yield
    sys.path.remove(str(root))


@pytest.fixture(autouse=True)
def inline_worker(monkeypatch):
    """Replays' sandboxes serve from the test's own process, through the same JSON as a worker process's
    (test_worker.py starts real ones). A sandbox takes its worker's process's time zone as its own: this one gets it
    back after each test."""
    from homeassistant.util import dt as dt_util
    from custom_components.assist_improver import worker
    monkeypatch.setattr(worker, "INLINE", True)
    zone = dt_util.get_default_time_zone()
    yield
    dt_util.set_default_time_zone(zone)


class ScriptedProvider:
    """The household's provider as a sandbox's HTTP client reaches it, answered here (no network): its model list,
    and the Messages API answering each request with the next of blocks (a model message, streamed as the API streams
    it; for OpenRouter, its chat completions, each an OpenAI message), or, when reply is set (an async function of a
    request's body to its blocks; chat, OpenRouter's), with what it gives, run on the test's own loop. What each
    request asked ("METHOD /path key", with the key it carried) and each message request's body."""

    CAPABILITIES = {"thinking": {"supported": True, "types": {"adaptive": {"supported": True},
                                                              "enabled": {"supported": True},
                                                              "disabled": {"supported": True},
                                                              "between_tools": {"supported": False}}},
                    "effort": {"supported": True, **{level: {"supported": True}
                                                     for level in ("low", "medium", "high", "max")}},
                    "code_execution": {"supported": True}, "structured_outputs": {"supported": True},
                    "server_tools": {"supported": True, "code_execution": {"supported": True},
                                     "web_search": {"supported": True}}}

    def __init__(self):
        self.asked, self.requests, self.blocks, self.models = [], [], [], ["fixture-no-model"]
        self.reply, self.loop = None, None  # The loop: the test's own (recordings.bind_agent).
        self.chat = None  # OpenRouter's reply hook, as reply is the Messages API's: a body to its OpenAI message.

    async def answer(self, request):
        import asyncio
        import httpx
        from anthropic_fixture import events_of
        key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        self.asked.append(f"{request.method} {request.url.path} {key}")
        if request.url.path == "/api/v1/models":  # OpenRouter's, at its own address: OpenAI's shape.
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "fixture"} for m in self.models]})
        if request.url.path == "/v1/models":
            models = [{"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z",
                       "lifecycle": "active", "capabilities": self.CAPABILITIES} for m in self.models]
            return httpx.Response(200, json={"data": models, "has_more": False, "first_id": self.models[0],
                                             "last_id": self.models[-1]})
        if request.url.path == "/v1/messages" and (self.blocks or self.reply):
            body = json.loads(request.content)
            self.requests.append(body)
            blocks = self.blocks.pop(0) if self.reply is None else await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self.reply(body), self.loop))
            stream = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events_of(blocks))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())
        if request.url.path == "/api/v1/chat/completions":  # OpenRouter's: each of blocks an OpenAI message.
            body = json.loads(request.content)
            self.requests.append(body)
            if self.chat is not None:
                message = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.chat(body), self.loop))
            elif not self.blocks:
                return httpx.Response(400, json={"error": {"message": "Unexpected inference call"}})
            else:
                message = self.blocks.pop(0)
            choice = {"index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}
            if body.get("stream"):  # Home Assistant 2026.11 on: the message streamed, in one chunk.
                calls = [{"index": i, **call} for i, call in enumerate(message.get("tool_calls") or [])]
                chunk = {"id": "fixture-completion", "object": "chat.completion.chunk", "created": 0,
                         "model": body.get("model"), "choices": [{**choice, "delta": {**message, "tool_calls": calls}}]}
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
            return httpx.Response(200, json={
                "id": "fixture-completion", "object": "chat.completion", "created": 0, "model": body.get("model"),
                "choices": [{**choice, "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
        if request.url.path == "/v1/messages":
            self.requests.append(json.loads(request.content))
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                        "message": "Unexpected inference call"}})
        return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "Not here"}})


PROVIDER = None  # This test's ScriptedProvider (the provider fixture).


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    """Every sandbox that loads the household's assistant reaches this test's scripted provider (ScriptedProvider)
    through its own HTTP client, as the provider's integration takes it from Home Assistant."""
    import httpx
    from homeassistant.helpers.httpx_client import DATA_ASYNC_CLIENT
    from homeassistant.util.ssl import SSL_ALPN_HTTP11, SSL_ALPN_HTTP11_HTTP2
    from custom_components.assist_improver import sandbox
    import openai._base_client
    global PROVIDER
    # OpenAI's SDK (OpenRouter's) asks the system what it runs on, by subprocess here: the guards forbid that.
    monkeypatch.setattr(openai._base_client, "get_platform", lambda: "MacOS")
    scripted, load = ScriptedProvider(), sandbox.load_assistant
    PROVIDER = scripted

    async def loaded(hass, entry, agent):  # Before the provider's integration takes the sandbox's client.
        client = httpx.AsyncClient(transport=httpx.MockTransport(scripted.answer))
        hass.data[DATA_ASYNC_CLIENT] = {(verify, alpn): client for verify in (True, False)
                                        for alpn in (SSL_ALPN_HTTP11, SSL_ALPN_HTTP11_HTTP2)}
        return await load(hass, entry, agent)
    monkeypatch.setattr(sandbox, "load_assistant", loaded)
    yield scripted
    PROVIDER = None


def inside(remote):
    """The sandbox behind a replay's RemoteSandbox, served inline: for a test to look inside it."""
    return remote.worker._server.sandbox


@pytest_asyncio.fixture
async def hass(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from homeassistant import loader
    from custom_components.assist_improver.worker import shutdown
    instance = HomeAssistant(str(tmp_path))
    loader.async_setup(instance)  # Its integrations' loader, as Home Assistant's own start sets it up first.
    # Its people, as HA's accounts give them: whoever is asked about is Alex, an administrator (household_user).
    instance.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=household_user("Alex")))
    yield instance
    await instance.async_block_till_done()
    await shutdown(instance)  # Replays' workers a test left running, so none outlives it.
    await instance.async_stop()


async def load_registries(hass):
    """The offline HA's config entries and registries, loaded empty as at its start (its entity registry waits for
    the config entries)."""
    from homeassistant import config_entries
    from homeassistant.helpers import (area_registry as ar, device_registry as dr, entity_registry as er,
                                       floor_registry as fr, issue_registry as ir, label_registry as lr)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await ir.async_load(hass)
    dr.async_setup(hass)
    for registry in (fr, lr, ar, dr, er):
        await registry.async_load(hass, load_empty=True)


def household_user(name, group="system-admin"):
    """A person's user record as Home Assistant's accounts keep it, in one of HA's own groups (its administrators',
    by default)."""
    from homeassistant.auth.models import Group, User
    from homeassistant.auth.permissions import system_policies
    policy = {"system-admin": system_policies.ADMIN_POLICY, "system-users": system_policies.USER_POLICY,
              "system-read-only": system_policies.READ_ONLY_POLICY}[group]
    return User(name=name, perm_lookup=None, is_active=True,
                groups=[Group(name=group, policy=policy, id=group, system_generated=True)])


async def assist_home(hass, states, *, registries=True):
    """HA's real Assist tools and intents in the offline HA, with these entities set and exposed.

    states: {entity_id: (state, friendly name)}. Device services exist (HA's intents check for them) but fail
    the test if they ever run; replays must emulate them instead.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from homeassistant import loader
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.components.intent import OnOffIntentHandler
    from homeassistant.components.light import intent as light_intent
    from homeassistant.const import EVENT_COMPONENT_LOADED
    from homeassistant.helpers import floor_registry as fr, intent, label_registry as lr
    from homeassistant.setup import async_setup_component
    loader.async_setup(hass)
    if registries:
        await load_registries(hass)
    for registry in (fr, lr):
        if registry.DATA_REGISTRY not in hass.data:  # load_registries loads them too.
            await registry.async_load(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    for intent_type, service in ((intent.INTENT_TURN_ON, "turn_on"), (intent.INTENT_TURN_OFF, "turn_off")):
        intent.async_register(hass, OnOffIntentHandler(intent_type, "homeassistant", service))

    async def never_called(call):
        raise AssertionError("A device service ran")
    for domain, services in (("light", ("turn_on", "turn_off", "toggle")), ("media_player", ("turn_on", "turn_off")),
                             ("switch", ("turn_on", "turn_off")),
                             ("cover", ("open_cover", "close_cover")), ("script", ("turn_on", "turn_off")),
                             ("tts", ("speak",))):  # As a speech engine (tts.speech) brings it, described by HA.
        for service in services:
            hass.services.async_register(domain, service, never_called)
    await light_intent.async_setup_intents(hass)  # HassLightSet, which HA scopes to lights.
    # HA's intent integration needs a web server; marking it loaded lets the llm integration offer its tools.
    for component in ("intent", "light"):
        hass.config.components.add(component)
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": component})
    await hass.async_block_till_done()
    from homeassistant.helpers import condition, trigger
    for helper in (condition, trigger):  # As Home Assistant's start sets them up: its validators read them.
        await helper.async_setup(hass)
    for entity_id, (state, name) in states.items():
        hass.states.async_set(entity_id, state, {"friendly_name": name})
        async_expose_entity(hass, "conversation", entity_id, True)
    # HA's chat log looks up the signed-in person's name; the offline HA has no user accounts.
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=household_user("Alex")))
    await hass.async_block_till_done()
    return hass
