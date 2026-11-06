"""A replay's home: a sandbox set up as the household's Home Assistant is today, at the moment of the conversation,
where the household's assistant runs each turn (DETAILS P6.2.4).

Today's setup is read at replay time, the way the recorder reads a home (snapshot.py, setup_now), with the
conversation's assistant as it's set up now (today); only the moment comes from the recording: what was said, the
devices' states as it started (snapshot.at_moment) and others' changes between turns, and its time. The assistant loads
in the sandbox from its own config entry, by its provider's integration (sandbox.load_assistant); HA builds its prompt
and tools there, its model is asked from there, and each tool call runs there: HA's own intents, scripts and
automations act on the sandbox's copy of the home, and only device commands are emulated. The sandbox runs in a replay
worker of its own (worker.py); the real instance only drives which recorded turn is said (simulate.Trial).
"""

from copy import deepcopy

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, llm

from .cases import CaseError
from .const import DOMAIN, PROVIDER_OPTIONS
from .sandbox import seed_options
from .serialization import plain
from .simulation import UnsupportedSimulation, reports_movement
from .snapshot import at_moment
from .touchpoints import Broken
from .trace import span
from .worker import RemoteSandbox, WorkerError, context_data

GONE = "The assistant this conversation used is no longer set up, so it can't be replayed."


class SandboxHome:
    """One replay's sandbox, started on first use: today's setup (today: replay.today's) at the recording's first turn
    (snapshot.at_moment), with a change on top (change: changes.setup_from_changes's scripts, exposure, aliases,
    household knowledge and instructions); for a test that branches from its baseline, set to the baseline's home after
    the turn before the branch (start: how it differed from that moment then, sandbox.moved). watch: the entities whose
    attributes a turn is judged on (a light's brightness)."""

    def __init__(self, hass, recording, turn, run_id, today, *, change=None, watch=(), start=None):
        self.hass, self.id, self.recording, self.first_turn, self.today = hass, run_id, recording, turn, today
        self.change = change or {}
        self.source = as_moved(at_moment(today["home"], turn["source"]), start)  # What the sandbox is seeded as.
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
        user_name = None
        if (user_id := _user_id(self.first_turn)) and getattr(self.hass, "auth", None):
            user = await self.hass.auth.async_get_user(user_id)
            user_name = getattr(user, "name", None)
        source = self.source
        self.sandbox = await RemoteSandbox.start(self.hass, source.get("time_zone") or self.hass.config.time_zone,
                                                 custom_providers(self.hass))
        try:
            async with span("sandbox.seed"):
                await self.sandbox.call(
                    "seed", source=source, user_name=user_name, options=seed_options(self.today, self.first_turn),
                    scripts={**(source.get("scripts") or {}), **(self.change.get("scripts") or {})},
                    automations=source.get("automations") or [], exposure_changes=self.change.get("exposure"),
                    alias_changes=self.change.get("aliases"), alias_removals=self.change.get("alias_removals"),
                    moving=reports_movement(self.recording))
        except Exception as err:
            await self.stop()
            if isinstance(err, Broken):  # Home Assistant changed: the machinery's fault, never the recording's.
                raise
            raise UnsupportedSimulation(f"The sandbox could not be set up as the household's home: {err}") from err
        if (knowledge := self.knowledge()) is not None:
            from .household import render
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
        instructions in place of its own: its entity id there."""
        from homeassistant.loader import IntegrationNotFound, async_get_integration
        await self.start()
        found = deepcopy(self.today["assistant"])
        if self.change.get("prompt") is not None:
            subentry = found["entry"]["subentries"][0]
            subentry["data"] = {**subentry["data"], "prompt": self.change["prompt"]}
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
        was recorded in: its words, identity, language and device."""
        ctx = turn["prepared"]["context"]
        identity = ctx.get("context") or {}
        return await self.sandbox.call(
            "turn", text=turn["messages"][0]["content"], conversation_id=conversation_id,
            context={k: identity.get(k) for k in ("id", "user_id", "parent_id")}, language=ctx.get("language") or "en",
            agent_id=agent_id, device_id=ctx.get("device_id"), watch=self.watch)

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


async def setup_now(hass):
    """Today's setup, as a replay and code's checks seed a sandbox with it (DETAILS P6.2.4): the household's Home
    Assistant as the recorder reads a home (snapshot.py); the tools Home Assistant's Assist has there, as the household's
    own conversations get them (an improver role's never file); and the devices that run timers."""
    from homeassistant.components.intent import async_device_supports_timers
    from homeassistant.helpers import device_registry as dr
    from .reflective import role_marker
    from .snapshot import home, taken
    read = taken(hass)
    context = llm.LLMContext(platform=DOMAIN, context=Context(), language=hass.config.language,
                             assistant="conversation", device_id=None)
    try:
        with role_marker(None):
            tools = sorted(t.name for t in (await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)).tools)
    except HomeAssistantError:  # No Assist at home: no tools.
        tools = []
    timers = sorted(d.id for d in dr.async_get(hass).devices if async_device_supports_timers(hass, d.id))
    return {"home": await hass.async_add_executor_job(home, read), "tools": tools, "timers": timers}


async def today(hass, agent_id):
    """Today's setup for replaying a conversation of this assistant (setup_now), with the assistant as it's set up now
    (assistant: assistant_of's) and as a plan keeps it (provider). CaseError when it's no longer set up."""
    assistant = assistant_of(hass, agent_id)
    subentry = assistant["entry"]["subentries"][0]
    provider = {"config_entry_id": assistant["entry"]["entry_id"], "subentry_id": subentry["subentry_id"],
                "title": subentry.get("title"),
                "options": plain({k: v for k, v in subentry["data"].items() if k in PROVIDER_OPTIONS})}
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
    # HA's storage form as it is, the provider's key in it (never plain(), which redacts it): it goes only to the
    # sandbox's worker (sandbox.load_assistant). How HA found the entry is no part of the assistant.
    stored = {**entry.as_dict(), "discovery_keys": {}}
    stored["subentries"] = [s for s in stored["subentries"] if s["subentry_id"] == agent.config_subentry_id]
    return {"entry": stored, "agent": {"entity_id": agent.entity_id, "unique_id": agent.unique_id,
                                       "platform": agent.platform, "config_subentry_id": agent.config_subentry_id}}
