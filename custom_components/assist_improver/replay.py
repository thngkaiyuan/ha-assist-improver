"""A replay's recorded home: a sandbox seeded as the recorded home, where the household's assistant runs each turn.

The assistant loads there from its own config entry, by its provider's integration (assistant_of,
sandbox.load_assistant), with the recorded settings; HA builds its prompt and tools there, its model is asked from
there, and each tool call runs there: HA's own intents, scripts and automations act on the sandbox's copy of the
home, and only device commands are emulated. The sandbox runs in the replay worker's process (worker.py); the real
instance only drives which recorded turn is said (simulate.Trial).
"""

from uuid import uuid4

from homeassistant.core import Context
from homeassistant.helpers import llm

from .cases import CaseError, recording_turns
from .sandbox import seed_options
from .simulation import UnsupportedSimulation, reports_movement
from .trace import span
from .worker import RemoteSandbox, WorkerError, context_data


class SandboxHome:
    """One replay's sandbox, started on first use from the recording's first turn.

    scripts ({object id: config}) and automations are those the sandbox runs: the recorded ones, with a change's on
    top. watch: the entities whose attributes a turn is judged on (a light's brightness).
    """

    def __init__(self, hass, recording, turn, run_id, *, scripts, automations, exposure_changes=None,
                 alias_changes=None, alias_removals=None, prompt_template=None, household=None, watch=()):
        self.hass, self.id, self.recording, self.first_turn = hass, run_id, recording, turn
        self.scripts, self.automations, self.exposure_changes = scripts, automations, exposure_changes or {}
        self.alias_changes, self.prompt_template = alias_changes or {}, prompt_template
        self.alias_removals = alias_removals or {}
        self.household = household  # A change's household knowledge, which Assist reads.
        self.sandbox = None
        self.tools = None  # Assist's tools as HA built them in the sandbox: name, description, parameters.
        self.watch = sorted(watch)
        self.prompt = None  # The system prompt HA builds in the sandbox, for comparison with the recorded one.

    async def start(self):
        if self.sandbox is not None:
            return
        source, prepared = self.first_turn["source"], self.first_turn["prepared"]
        user_name = None
        if (user_id := prepared["context"].get("context", {}).get("user_id")) and getattr(self.hass, "auth", None):
            user = await self.hass.auth.async_get_user(user_id)
            user_name = getattr(user, "name", None)
        self.sandbox = await RemoteSandbox.start(source.get("time_zone") or self.hass.config.time_zone,
                                                 custom_providers(self.hass))
        try:
            async with span("sandbox.seed"):
                await self.sandbox.call(
                    "seed", source=source, user_name=user_name, options=seed_options(self.first_turn),
                    scripts=self.scripts, automations=self.automations, exposure_changes=self.exposure_changes,
                    alias_changes=self.alias_changes, alias_removals=self.alias_removals,
                    moving=reports_movement(self.recording))
        except Exception as err:
            await self.stop()
            raise UnsupportedSimulation(f"The sandbox could not be seeded as the recorded home: {err}") from err

    async def prepare(self, turn, llm_context):
        """Assist as HA builds it in the sandbox for this turn: its system prompt (prompt) and tools (tools), the
        household knowledge it reads served first (a change's, or as recorded)."""
        await self.start()
        options = turn["prepared"]["provider_config"][0]["options"]
        template = self.prompt_template if self.prompt_template is not None else options.get("prompt")
        from .household import API_ID, api_ids, render
        apis = api_ids(turn["prepared"].get("api_id") or "assist")
        # Recorded through the household API (up to 0.37): replayed through it, so the prompt is the one recorded.
        legacy = API_ID in apis
        if (legacy or "assist" in apis) and (self.household is not None or reads_household(turn)):
            # The household knowledge Assist read then, or as a change would make it.
            knowledge = self.household if self.household is not None else turn["source"].get("household")
            await self.sandbox.call("household", knowledge=knowledge, legacy=legacy)
            lines = render(knowledge, getattr(llm_context.context, "user_id", None))
        else:
            lines = ""
        built = await self.sandbox.call("prompt", context=context_data(llm_context),
                                        api_id=apis if len(apis) > 1 else apis[0], template=template)
        self.prompt = built["prompt"]
        if built["api_prompt"] is None:
            raise UnsupportedSimulation("Home Assistant's Assist tools are unavailable in the sandbox")
        if lines not in built["api_prompt"]:  # Never a test without the lines it's meant to read: the sandbox's fault.
            raise WorkerError("Assist didn't read the household knowledge in the sandbox")
        self.tools, self.api_prompt = built["tools"], built["api_prompt"]

    async def observe(self, source, moves):
        await self.sandbox.call("observe", source=source, moves=moves)

    async def snapshot(self):
        """The sandbox's home now (sandbox.snapshot): its states, the replay's changes, commands and what it spoke."""
        return await self.sandbox.call("snapshot", **({"attributes": self.watch} if self.watch else {}))

    async def load_assistant(self, agent_id, provider):
        """The household's assistant in the sandbox, from its own config entry, with the recorded provider settings
        (provider: a turn's provider configuration; assistant_of): its entity id there."""
        from homeassistant.loader import IntegrationNotFound, async_get_integration
        await self.start()
        if (found := assistant_of(self.hass, agent_id, provider)) is None:
            raise CaseError("The assistant this conversation used is no longer set up, so it can't be replayed.")
        domain = found["entry"]["domain"]
        try:
            built_in = (await async_get_integration(self.hass, domain)).is_built_in
        except IntegrationNotFound:
            built_in = False
        if not built_in and domain not in custom_providers(self.hass):  # Not loaded at home: none to load here.
            raise CaseError(f"This assistant's provider ({domain}) isn't loaded, so it can't be replayed.")
        return (await self.sandbox.call("assistant", **found))["agent"]

    async def faithful(self, turn, llm_context):
        """Whether Home Assistant, in the sandbox, builds this turn's recorded system prompt and tools exactly, with
        the recorded instructions: the model sees what the sandbox builds, so a replay runs only where that's the
        recording (DETAILS P6.2.6). Before the turn's model call."""
        options = turn["prepared"]["provider_config"][0]["options"]
        saved = self.prompt_template
        self.prompt_template = options.get("prompt")
        try:
            await self.prepare(turn, llm_context)
        finally:
            self.prompt_template = saved
        return self.prompt == turn["history"][0]["content"] and [t["name"] for t in self.tools] == [
            t["name"] for t in turn["prepared"]["tools"]]

    async def turn(self, turn, agent_id, conversation_id):
        """One recorded turn said to the household's assistant in the sandbox (sandbox.run_turn), in the context it
        was recorded in: its words, identity, language and device."""
        ctx = turn["prepared"]["context"]
        identity = ctx.get("context") or {}
        return await self.sandbox.call(
            "turn", text=turn["messages"][0]["content"], conversation_id=conversation_id,
            context={k: identity.get(k) for k in ("id", "user_id", "parent_id")}, language=ctx.get("language") or "en",
            agent_id=agent_id, device_id=ctx.get("device_id"), watch=self.watch)

    async def stop(self):
        if self.sandbox is not None:
            sandbox, self.sandbox, self.tools = self.sandbox, None, None
            await sandbox.stop()


def reads_household(turn):
    """Whether Assist read the household knowledge in this recorded turn: the switch was on (recorded since the lines
    came with Assist), or the assistant used the household API (up to 0.37)."""
    from .household import API_ID, api_ids
    return bool(turn["source"].get("household_on")) or API_ID in api_ids(turn["prepared"].get("api_id"))


async def register(hass, api):
    """In the sandbox: serve this API there (the household API up to 0.37, with a replay's knowledge)."""
    from homeassistant.helpers import llm
    apis = llm._async_get_apis(hass)
    apis.pop(api.id, None)  # A sandbox serves one replay's knowledge at a time.
    llm.async_register_api(hass, api)


def custom_providers(hass):
    """The household's conversation agents' providers that are custom integrations, as loaded at home: {domain: its
    folder}. A sandbox loads such a provider as the household's assistant's own integration (DETAILS P6.2.3), and no
    other custom integration (worker.link_providers)."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration
    found, registry = {}, hass.data.get(er.DATA_REGISTRY)
    if getattr(registry, "entities", None) is None or getattr(hass, "config_entries", None) is None:
        return found  # Nothing set up yet: no assistant.
    for entity in registry.entities.values():
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


def assistant_of(hass, agent_id, provider=None):
    """A conversation agent's assistant for a sandbox (sandbox.load_assistant): its provider's config entry in HA's
    storage form, with only the agent's subentry, and the agent's registry identity. provider: a turn's recorded
    provider configuration, which must be the agent's subentry: its settings are the subentry's, today's kept only for
    what the recorder doesn't keep (PROVIDER_OPTIONS). None when it isn't set up."""
    from homeassistant.helpers import entity_registry as er
    from .const import PROVIDER_OPTIONS
    if (agent := er.async_get(hass).async_get(agent_id or "")) is None or (
            entry := hass.config_entries.async_get_entry(agent.config_entry_id or "")) is None:
        return None
    if agent.config_subentry_id is None:
        raise CaseError("This assistant keeps no settings of its own to replay it with.")
    if provider is not None and (provider.get("config_entry_id"), provider.get("subentry_id")) != (
            agent.config_entry_id, agent.config_subentry_id):
        raise CaseError("Selected HA agent does not match the recorded provider entry")
    recorded = (provider or {}).get("options")
    # HA's storage form as it is, the provider's key in it (never plain(), which redacts it): it goes to the sandbox's
    # memory only (sandbox.load_assistant). How HA found the entry is no part of the assistant.
    stored = {**entry.as_dict(), "discovery_keys": {}}
    stored["subentries"] = [{**s, "data": s["data"] if recorded is None else {
        **{k: v for k, v in s["data"].items() if k not in PROVIDER_OPTIONS}, **recorded}}
        for s in stored["subentries"] if s["subentry_id"] == agent.config_subentry_id]
    return {"entry": stored, "agent": {"entity_id": agent.entity_id, "unique_id": agent.unique_id,
                                              "platform": agent.platform,
                                              "config_subentry_id": agent.config_subentry_id}}


async def check_fidelity(hass, recording, turn_number=1, assistant=False):
    """Seed a sandbox from a recording's first turn and compare what HA builds there with what was recorded:
    the system prompt and the tools. No model call. Differences point at gaps in recording or seeding. assistant: also
    load the conversation's assistant there from its config entry (its provider's model list is fetched), and report
    its entity id there (agent), or why it couldn't (agent_error)."""
    import difflib
    from time import monotonic
    turn = recording_turns(recording)[turn_number - 1]
    ctx = turn["prepared"]["context"]
    identity = ctx.get("context", {})
    if not identity.get("id") or not {"id", "user_id", "parent_id"} <= identity.keys():
        raise CaseError("Cannot backfill missing historical identity")
    started = monotonic()
    source = turn["source"]
    home = SandboxHome(hass, recording, turn, f"improver-fidelity-{uuid4()}", scripts=source.get("scripts") or {},
                       automations=source.get("automations") or [])
    try:
        await home.prepare(turn, llm.LLMContext(**{**ctx, "context": Context(**identity)}))
        seeded = round(monotonic() - started, 2)
        recorded_prompt = turn["history"][0]["content"]
        recorded_tools = [t["name"] for t in turn["prepared"]["tools"]]
        tools = [t["name"] for t in home.tools]
        report = {"recording_id": recording["id"], "prompt_matches": home.prompt == recorded_prompt,
                "tools_match": tools == recorded_tools,
                "missing_tools": sorted(set(recorded_tools) - set(tools)),
                "extra_tools": sorted(set(tools) - set(recorded_tools)),
                "prompt_diff": list(difflib.unified_diff(recorded_prompt.splitlines(), home.prompt.splitlines(),
                                                         "recorded", "sandbox", lineterm="", n=0))[:60],
                "seconds_to_seed": seeded, "paid_inference_performed": False,
                # What HA logged while setting up the recorded home (an automation it couldn't set up, say).
                "sandbox_log": list(home.sandbox.log)[:40]}
        if assistant:
            agents = {m["agent_id"] for t in recording_turns(recording) for m in t["messages"]
                      if m["role"] == "assistant"}
            try:
                report["agent"] = await home.load_assistant(
                    next(iter(agents)) if len(agents) == 1 else None, turn["prepared"]["provider_config"][0])
            except Exception as err:  # noqa: BLE001 - reported, the probe's other findings stand.
                report["agent"], report["agent_error"] = None, f"{type(err).__name__}: {err}"  # HA's why in it.
    finally:
        stopping = monotonic()
        await home.stop()
    report["seconds_to_stop"] = round(monotonic() - stopping, 2)
    return report
