"""A replay's home: a sandbox set up as the household's Home Assistant is today, at the moment of the conversation,
where the household's assistant runs each turn (DETAILS P6.2.4).

Today's setup is read at replay time, the way the recorder reads a home (snapshot.py, setup_now), with the
conversation's assistant as it's set up now (today); only the moment comes from the recording: what was said, the
devices' states as it started (snapshot.at_moment) and others' changes between turns, and its time. The assistant loads
in the sandbox from its own config entry, by its provider's integration (sandbox.load_assistant); HA builds its prompt
and tools there, its model is asked from there, and each tool call runs there: HA's own intents, scripts and
automations act on the sandbox's copy of the home, and only the devices and calls beyond the home are stood in. The
sandbox runs in a replay worker of its own (worker.py); the real instance only drives which recorded turn is said
(simulate.Trial).
"""

from copy import deepcopy

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, llm

from .cases import CaseError
from .changes import changed
from .const import DOMAIN
from .digest import machinery
from .household import render
from .recorder import _pipeline, _pipeline_record
from .reflective import role_marker
from .sandbox import seed_options, setup_of
from .serialization import plain
from .settings import own_models
from .simulation import UnsupportedSimulation, recorded, reports_movement
from .snapshot import at_moment, home, taken
from .targets import describe
from .touchpoints import Broken, get, guarded, relied_on, report
from .trace import span
from .worker import Refused, RemoteSandbox, WorkerError, context_data

GONE = "The assistant this conversation used is no longer set up, so it can't be replayed."


class SandboxHome:
    """One replay's sandbox, started on first use: today's setup (today: replay.today's) at the recording's first turn
    (snapshot.at_moment), with a change on top (change: changes.setup_from_changes's, the home it makes seeded as
    changes.changed makes it, its household knowledge, instructions and LLM APIs); for a test that branches from its
    baseline, set to the baseline's home after the turn before the branch (start: how it differed from that moment
    then, sandbox.moved), at the branch turn's time (at: the first turn it says). watch: the entities whose attributes
    a turn is judged on (the devices the recorded Assist changed, for their settings: DETAILS P6.6.1)."""

    def __init__(self, hass, recording, turn, run_id, today, *, change=None, watch=(), start=None, at=None):
        self.hass, self.id, self.recording, self.first_turn, self.today = hass, run_id, recording, turn, today
        self.change = change or {}
        self.source = changed(as_moved(at_moment(today["home"], turn["source"]), start), self.change)  # Seeded as.
        if at is not None:  # Its clock starts there (sandbox.Sandbox.start).
            self.source["at"] = at
        self.sandbox = None
        self.watch = sorted(watch)

    def knowledge(self):
        """The household knowledge Assist reads in the sandbox: a change's, else today's while it's on; None for
        none."""
        if self.change.get("household") is not None:
            return self.change["household"]
        return self.source.get("household") if self.source.get("household_on") else None

    async def start(self):
        if self.sandbox is not None:
            return
        user_id = _user_id(self.first_turn)
        source = self.source
        person = await _person(self.hass, user_id, source.get("states") or {})
        self.sandbox = await RemoteSandbox.start(self.hass, source.get("time_zone") or self.hass.config.time_zone,
                                                 source, setup_of(self.today), custom_providers(self.hass))
        try:
            async with span("sandbox.seed"):
                await self.sandbox.call(
                    "seed", person=person, options=seed_options(self.today, self.first_turn),
                    scripts=source.get("scripts") or {}, automations=source.get("automations") or [],
                    exposure_changes=self.change.get("exposure"), moving=reports_movement(self.recording),
                    machinery=sorted(machinery(self.recording, _pipeline_now(self.hass, self.recording))),
                    cut=sorted(own_models(self.hass)), recorded=recorded(self.recording, self.today["tools"]))
        except Exception as err:
            await self.stop()
            # Home Assistant changed, or the guard refused something: the machinery's fault, never the recording's.
            if isinstance(err, (Broken, Refused)):
                raise
            raise UnsupportedSimulation(f"The sandbox could not be set up as the household's home: {err}") from err
        if (knowledge := self.knowledge()) is not None:
            await self.sandbox.call("household", knowledge=knowledge)
            lines = render(knowledge, user_id)
            # Never a test without the lines it's meant to read: the sandbox's fault.
            if lines and lines not in ((await self.assist(self.first_turn))["api_prompt"] or ""):
                raise WorkerError("Assist didn't read the household knowledge in the sandbox")

    async def assist(self, turn):
        """Assist's system prompt, its own part (api_prompt) and its tools, as Home Assistant builds them in the sandbox
        for this recorded request, with the assistant's instructions now (or a change's)."""
        await self.start()
        ctx = turn["prepared"]["context"]
        context = llm.LLMContext(**{**ctx, "context": Context(**(ctx.get("context") or {}))})
        template = self.change.get("prompt")
        if template is None:
            template = self.today["assistant"]["entry"]["subentries"][0]["data"].get("prompt")
        return await self.sandbox.call("prompt", context=context_data(context), api_id=llm.LLM_API_ASSIST,
                                       template=template)

    async def observe(self, source, moves):
        await self.sandbox.call("observe", source=source, moves=moves)

    async def snapshot(self):
        """The sandbox's home now (sandbox.snapshot): its states, the replay's changes, commands and what it spoke."""
        return await self.sandbox.call("snapshot", **({"attributes": self.watch} if self.watch else {}))

    async def load_assistant(self):
        """The household's assistant in the sandbox, from its own config entry as it's set up now, a change's
        instructions in place of its own and only the LLM APIs it keeps: its entity id there."""
        from homeassistant.loader import IntegrationNotFound, async_get_integration
        await self.start()
        found = deepcopy(self.today["assistant"])
        subentry = found["entry"]["subentries"][0]
        if self.change.get("prompt") is not None:
            subentry["data"] = {**subentry["data"], "prompt": self.change["prompt"]}
        if self.change.get("llm_hass_api") is not None:
            subentry["data"] = {**subentry["data"], "llm_hass_api": self.change["llm_hass_api"]}
        domain = found["entry"]["domain"]
        try:
            built_in = (await async_get_integration(self.hass, domain)).is_built_in
        except IntegrationNotFound:
            built_in = False
        if not built_in and domain not in custom_providers(self.hass):  # Not loaded at home: none to load here.
            raise CaseError(f"This assistant's provider ({domain}) isn't loaded, so it can't be replayed.")
        return (await self.sandbox.call("assistant", **found))["agent"]

    async def turn(self, turn, agent_id, conversation_id):
        """One recorded turn said to the household's assistant in the sandbox (sandbox.run_turn), in the context it
        was recorded in: its words, identity, language and device; its clock never past the next recorded moment
        (horizon)."""
        ctx = turn["prepared"]["context"]
        identity = ctx.get("context") or {}
        return await self.sandbox.call(
            "turn", text=turn["messages"][0]["content"], conversation_id=conversation_id,
            context={k: identity.get(k) for k in ("id", "user_id", "parent_id")}, language=ctx.get("language") or "en",
            agent_id=agent_id, device_id=ctx.get("device_id"), watch=self.watch,
            until=horizon(self.recording, turn["number"]))

    async def carry(self, steps):
        """A test's turns before its branch, its baseline's steps, in the replay's chat log as its own history
        (sandbox.carry)."""
        ctx = self.first_turn["prepared"]["context"]
        context = llm.LLMContext(**{**ctx, "context": Context(**(ctx.get("context") or {}))})
        await self.sandbox.call("carry", conversation_id=self.id, context=context_data(context),
                                steps=[{"input": s["input"], "output": s["output"]} for s in steps])

    async def stop(self):
        if self.sandbox is not None:
            sandbox, self.sandbox = self.sandbox, None
            await sandbox.stop()


def horizon(recording, number):
    """The recorded moment after a turn (ISO), which a replay's clock never passes in it (sandbox.run_turn): the next
    turn's start, else the conversation's end (its last recorded activity: Home Assistant ending it); None: unknown."""
    later = [at for t in (recording.get("turns") or [])[number:] if (at := (t.get("source") or {}).get("at"))]
    return later[0] if later else recording.get("last_activity")


def as_moved(source, moved):
    """A snapshot (source) with its states as they differed from it later (moved: sandbox.moved's), so a sandbox is
    seeded as that home: before the household's automations load, which nothing seeded sets off."""
    if not moved:
        return source
    states = deepcopy(source.get("states") or {})
    for entity_id, move in moved.items():
        if move is None:
            states.pop(entity_id, None)
            continue
        was = states.get(entity_id) or {"entity_id": entity_id, "attributes": {}}
        kept = {k: v for k, v in (was.get("attributes") or {}).items() if k not in move["removed"]}
        states[entity_id] = {**was, "state": move["state"], "attributes": {**kept, **move["attributes"]}}
    return {**source, "states": states}


def _user_id(turn):
    return ((turn["prepared"].get("context") or {}).get("context") or {}).get("user_id")


async def speaker(hass, recording):
    """The person who spoke in a recording, as their user record has them today (_person); None for none."""
    turns = recording.get("turns") or []
    if not turns or "prepared" not in turns[0]:
        return None
    return await _person(hass, _user_id(turns[0]), (turns[0].get("source") or {}).get("states") or {})


async def _person(hass, user_id, entities):
    """The person speaking, as their user record has them today: their name, whether they're an administrator, their
    groups, and which of these entities they may not control (sandbox._user). None: no one is signed in, or no such
    person is left."""
    if not user_id or not getattr(hass, "auth", None) or (user := await hass.auth.async_get_user(user_id)) is None:
        return None
    try:
        with relied_on("auth_user"):
            from homeassistant.auth.permissions.const import POLICY_CONTROL
            return {"name": user.name, "is_admin": user.is_admin, "group_ids": [group.id for group in user.groups],
                    "refused": [] if user.is_admin else sorted(
                        e for e in entities if not user.permissions.check_entity(e, POLICY_CONTROL))}
    except Broken as err:  # On the real instance: reported here.
        report(hass, err.name, err.reason)
        raise


def _pipeline_now(hass, recording):
    """The voice assistant (Assist pipeline) a conversation ran through, as it's set up today (a replay's setup is
    today's): the one it was recorded running through, else its device's, else Home Assistant's preferred one; None
    when there's none."""
    from homeassistant.components.assist_pipeline import PipelineNotFound, async_get_pipeline
    if "assist_pipeline" not in hass.config.components:
        return None
    turns = recording.get("turns") or []
    device = ((turns[0].get("prepared") or {}).get("context") or {}).get("device_id") if turns else None
    found = recording.get("pipeline_id") or _pipeline(hass, device)
    try:
        return _pipeline_record(hass, found or async_get_pipeline(hass).id)
    except (KeyError, PipelineNotFound):
        return None


async def setup_now(hass):
    """Today's setup, as a replay and code's checks seed a sandbox with it (DETAILS P6.2.4): the household's Home
    Assistant as the recorder reads a home (snapshot.py); the tools Home Assistant's Assist has there, as the household's
    own conversations get them (an improver role's never file); the devices that run timers; and what a sandbox sets up
    beyond the home (sandbox.setup_of): each config entry (its integration, title and whether it's disabled), each
    storage or YAML helper the home has (an integration of Home Assistant's helper type with no config flow: a toggle,
    a timer), with its storage and its configuration.yaml part, and every action, by whether it answers."""
    from homeassistant.components.intent import async_device_supports_timers
    from homeassistant.helpers import device_registry as dr
    read = taken(hass)
    context = llm.LLMContext(platform=DOMAIN, context=Context(), language=hass.config.language,
                             assistant="conversation", device_id=None)
    try:
        with role_marker(None):
            tools = sorted(t.name for t in (await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)).tools)
    except HomeAssistantError:  # No Assist at home: no tools.
        tools = []
    timers = sorted(d.id for d in dr.async_get(hass).devices if async_device_supports_timers(hass, d.id))
    entries = {e.entry_id: {"domain": e.domain, "title": e.title, "disabled_by": e.disabled_by}
               for e in hass.config_entries.async_entries()} if getattr(hass, "config_entries", None) else {}
    services = {domain: {service: hass.services.supports_response(domain, service).value for service in found}
                for domain, found in hass.services.async_services().items()}
    return {"home": await hass.async_add_executor_job(_home, read), "tools": tools, "timers": timers,
            "entries": plain(entries), "helpers": await _helpers(hass, read), "services": services}


def _home(read):
    """Today's home as a snapshot keeps it, with how Home Assistant describes its devices' services read
    (targets.describe): off the event loop."""
    found = home(read)
    describe(found)
    return found


async def _helpers(hass, read):
    """The home's storage and YAML helpers (setup_now): {domain: {storage: its store as Home Assistant keeps it, yaml:
    its part of configuration.yaml}}."""
    import json
    from pathlib import Path
    from homeassistant import config as conf_util
    from homeassistant.loader import async_get_loaded_integration
    used = {s.entity_id.split(".", 1)[0] for s in read["states"]} | {e.domain for e in read.get("entries") or ()}
    found, folder = {}, None
    with guarded(hass, "storage_folder"):  # Without it, helpers are set up with no store: none of their items.
        folder = get(hass, "storage_folder")
    for domain in sorted(used & hass.config.components):
        integration = async_get_loaded_integration(hass, domain)
        if integration.integration_type == "helper" and not integration.config_flow:
            path = Path(hass.config.path(folder, domain)) if folder else None
            found[domain] = {"storage": await hass.async_add_executor_job(
                lambda p=path: json.loads(p.read_text()) if p and p.is_file() else None)}
    if found:
        try:
            yaml = await conf_util.async_hass_config_yaml(hass)
        except (HomeAssistantError, OSError):  # No configuration.yaml to read: none.
            yaml = {}
        for domain, helper in found.items():
            helper["yaml"] = plain({domain: yaml[domain]}) if yaml.get(domain) is not None else None
    return found


async def today(hass, agent_id):
    """Today's setup for replaying a conversation of this assistant (setup_now), with the assistant as it's set up now
    (assistant: assistant_of's) and as a plan keeps it (provider). CaseError when it's no longer set up."""
    assistant = assistant_of(hass, agent_id)
    subentry = assistant["entry"]["subentries"][0]
    provider = {"config_entry_id": assistant["entry"]["entry_id"], "subentry_id": subentry["subentry_id"],
                "title": subentry.get("title"),
                "options": plain(subentry["data"])}  # Every setting (DETAILS P8.2.3); the credentials are entry.data's.
    return {**await setup_now(hass), "assistant": assistant, "provider": provider}


def custom_providers(hass):
    """The household's conversation agents' providers that are custom integrations, as loaded at home: {domain: its
    folder}. A sandbox loads such a provider as the household's assistant's own integration (DETAILS P6.2.3), and no
    other custom integration (worker._folder)."""
    from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration
    found = {}
    if getattr(hass, "config_entries", None) is None:
        return found  # Nothing set up yet: no assistant.
    for entity in er.async_get(hass).entities.values():
        if entity.domain != "conversation" or (entry := hass.config_entries.async_get_entry(
                entity.config_entry_id or "")) is None or entry.domain not in hass.config.components:
            continue  # Not an assistant, or its provider isn't set up at home.
        try:
            integration = async_get_loaded_integration(hass, entry.domain)
        except IntegrationNotLoaded:
            continue
        if not integration.is_built_in:
            found[entry.domain] = str(integration.file_path)
    return found


def assistant_of(hass, agent_id):
    """A conversation agent's assistant as it's set up now, for a sandbox (sandbox.load_assistant): its provider's
    config entry in HA's storage form, with only the agent's subentry, and the agent's registry identity. CaseError
    when it's no longer set up."""
    if (getattr(hass, "config_entries", None) is None or (agent := er.async_get(hass).async_get(agent_id or "")) is None
            or (entry := hass.config_entries.async_get_entry(agent.config_entry_id or "")) is None):
        raise CaseError(GONE)
    if agent.config_subentry_id is None:
        raise CaseError("This assistant keeps no settings of its own to replay it with.")
    # HA's storage form as it is, the provider's key in it, unread here: it goes only to the sandbox's worker
    # (sandbox.load_assistant). How HA found the entry is no part of the assistant.
    stored = {**entry.as_dict(), "discovery_keys": {}}
    stored["subentries"] = [s for s in stored["subentries"] if s["subentry_id"] == agent.config_subentry_id]
    return {"entry": stored, "agent": {"entity_id": agent.entity_id, "unique_id": agent.unique_id,
                                       "platform": agent.platform, "config_subentry_id": agent.config_subentry_id}}
