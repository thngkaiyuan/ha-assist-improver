"""The sandbox: a separate HA instance in its own thread, running HA's own engine on a simulated home."""

import asyncio
from datetime import datetime
import threading
import time

import pytest
from homeassistant.core import Context, async_get_hass
from homeassistant.helpers import llm

from recordings import kept_home
from custom_components.assist_improver import sandbox as sandbox_module
from custom_components.assist_improver.sandbox import SANDBOX_KEY, Sandbox, observe, seed

PERSON = {"name": "Alex", "is_admin": True, "group_ids": ["system-admin"], "refused": []}  # As replay._person reads one.
PLAYS, ANNOUNCES = 512, 1048576  # A media player's features: it plays media, and announcements.


def home(**states):
    """A home of devices as the recorder keeps it (kept_home): {entity_id: (state, attributes)}, each exposed."""
    return kept_home({"entities": {e: {} for e in states}, "exposure": dict.fromkeys(states, True),
                      "states": {e: {"state": s, "attributes": a} for e, (s, a) in states.items()}})


RELAY_AND_PORCH = home(**{"switch.relay": ("on", {"friendly_name": "Garage Relay"}),
                          "light.porch": ("off", {"friendly_name": "Porch Light", "supported_color_modes": ["onoff"]})})


async def started(seeded=RELAY_AND_PORCH, time_zone="America/Los_Angeles", setup=None, **options):
    """A sandbox started as a home (Sandbox.start) and seeded (seed, with its options)."""
    sandbox = await Sandbox.start(time_zone, seeded, setup)
    try:
        await sandbox.run(lambda h: seed(h, **options))
    except BaseException:
        await sandbox.stop()
        raise
    return sandbox


async def assist_tool(hass, name, args):
    context = llm.LLMContext(platform="test", context=Context(), language="en", assistant="conversation", device_id=None)
    api = await llm.async_get_api(hass, "assist", context)
    result = await api.async_call_tool(llm.ToolInput(tool_name=name, tool_args=args))
    return result, api.api_prompt


@pytest.mark.asyncio
async def test_sandbox_runs_its_own_engine_isolated_from_the_real_instance(hass, effect_guards):
    hass.states.async_set("switch.relay", "LIVE")
    sandbox = await started()
    try:
        assert async_get_hass() is hass  # The real instance is still the one HA's helpers find.
        result, prompt = await sandbox.run(assist_tool, "intent__HassTurnOff", {"name": "Garage Relay"})
        assert "Garage Relay" in prompt
        assert sandbox.hass.states.get("switch.relay").state == "off"
        assert sandbox.commands == [{"entity_id": "switch.relay", "service": "switch.turn_off", "data": {}}]
        assert hass.states.get("switch.relay").state == "LIVE" and effect_guards == []
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_the_sandbox_keeps_what_the_recorder_keeps_the_voice_assistants_machinery_left_out(hass, effect_guards):
    """The keep rule applies in the sandbox as at home (digest.kept): the answering assistant's own entities, as the
    recording has them, and the improver's own AI Tasks are never a replay's change nor its home; every other entity
    is, the household's AI Task among them."""
    from custom_components.assist_improver.sandbox import home_now
    sandbox = await Sandbox.start("America/Los_Angeles")
    try:
        async def turn(hass):
            sandbox.machinery, sandbox.cut = {"conversation.fixture"}, {"ai_task.improver"}
            hass.bus.async_listen("state_changed", sandbox.state_changed)
            for entity_id in ("conversation.fixture", "ai_task.improver", "ai_task.household", "light.porch"):
                hass.states.async_set(entity_id, "before")
                hass.states.async_set(entity_id, "after")
            await hass.async_block_till_done()
            return home_now(hass)
        home = await sandbox.run(turn)
        assert [t["entity_id"] for t in sandbox.transitions] == ["ai_task.household", "light.porch"]
        assert set(home) == {"ai_task.household", "light.porch"} and effect_guards == []
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_scripts_and_automations_run_in_the_sandbox(hass, effect_guards):
    sandbox = await started(scripts={"press": {"sequence": [
        {"action": "switch.turn_off", "target": {"entity_id": "switch.relay"}},
        {"delay": {"milliseconds": 200}},
        {"action": "switch.turn_on", "target": {"entity_id": "switch.relay"}}]}}, automations=[{
            "id": "porch", "triggers": [{"trigger": "state", "entity_id": "switch.relay", "to": "off"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]}])
    try:
        async def press(hass):
            await hass.services.async_call("script", "press", blocking=True)
            await hass.async_block_till_done()
        await sandbox.run(press)
        assert [c["service"] for c in sandbox.commands] == ["switch.turn_off", "light.turn_on", "switch.turn_on"]
        assert sandbox.hass.states.get("light.porch").state == "on" and effect_guards == []
    finally:
        await sandbox.stop()


async def logged(hass, action, data=None):
    """A call made under a request of its own, with the call log a sandbox keeps of a turn (sandbox._call_log)."""
    context = Context()
    with sandbox_module._call_log(hass, {}, context.id) as actions:
        await hass.services.async_call(*action.split("."), data or {}, blocking=True, context=context)
        await hass.async_block_till_done()
    return actions


def said(actions, home):
    """What a call log said or played where, as a test reads it (happened.announcements)."""
    from custom_components.assist_improver import happened
    return [(a["message"], a["media"], sorted(a["targets"])) for a in happened.announcements([], actions, home)]


@pytest.mark.asyncio
async def test_what_a_script_speaks_is_in_its_call_log_with_its_devices_and_media_that_isnt_a_message_plays(
        hass, effect_guards):
    """DETAILS P9.10.5: what a script says or plays is in the turn's call log, each call with its words or media and
    the devices it's for: tts.speak through its engine, which plays it on its media players as Home Assistant's own
    speech engine does, an announcement on a satellite, a media player playing a spoken message. Each is taken as done
    (simulated, leaving no state); a media player playing anything else plays it (its stand-in, standin.py)."""
    kitchen, hall = "media_player.kitchen", "assist_satellite.hall"
    play = lambda media: {"action": "media_player.play_media", "target": {"entity_id": kitchen},  # noqa: E731
                          "data": {"media_content_id": media, "media_content_type": "music"}}
    speakers = home(**{kitchen: ("idle", {"supported_features": PLAYS}), "tts.speech": ("unknown", {}),
                       hall: ("idle", {"supported_features": 1})})  # The satellite announces.
    sandbox = await started(speakers, scripts={"say": {"sequence": [
        {"action": "tts.speak", "target": {"entity_id": "tts.speech"},
         "data": {"media_player_entity_id": kitchen, "message": "Dinner is ready"}},
        {"action": "assist_satellite.announce", "target": {"entity_id": hall}, "data": {"message": "Dinner"}},
        play("media-source://tts/speech?message=Come%20down"), play("jazz")]}})
    try:
        actions = await sandbox.run(logged, "script.say")
        assert said(actions, speakers) == [
            ("Dinner is ready", None, [kitchen, "tts.speech"]), ("Dinner is ready", None, [kitchen]),  # Its engine's.
            ("Dinner", None, [hall]), ("Come down", None, [kitchen]), ("", "jazz", [kitchen])]
        playing = sandbox.hass.states.get(kitchen)
        assert playing.state == "playing" and playing.attributes["media_content_id"] == "jazz"
        assert sandbox.unsimulated == [] and effect_guards == []
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_a_chime_a_legacy_engines_say_and_a_conversation_started_are_each_in_the_call_log_as_announcements(
        hass, effect_guards):
    """Kai, 2026-10-30: any speech or audio sent to a device announces (DETAILS P9.10.5). A satellite's chime (its
    media, no words), a media player's announcement, a legacy engine's say (one the sandbox doesn't load, as a
    household configures it in YAML: a call beyond the home) and a conversation a satellite starts are each in the
    call log with where they played. A satellite named that can't start a conversation refuses it, as Home
    Assistant's service does by its features."""
    from homeassistant.exceptions import ServiceNotSupported
    kitchen, hall, porch = "media_player.kitchen", "assist_satellite.hall", "assist_satellite.porch"
    chime = "media-source://media_source/local/chime.mp3"
    speakers = home(**{kitchen: ("idle", {"supported_features": PLAYS | ANNOUNCES}),
                       hall: ("idle", {"supported_features": 3}),  # Announces, and starts conversations.
                       porch: ("idle", {"supported_features": 1})})  # Only announces.
    sandbox = await started(speakers, setup={"services": {"tts": {"cloud_say": "none"}}}, scripts={"call": {
        "sequence": [
            {"action": "assist_satellite.announce", "target": {"entity_id": hall}, "data": {"media_id": chime}},
            {"action": "media_player.play_media", "target": {"entity_id": kitchen},
             "data": {"media_content_id": chime, "media_content_type": "music", "announce": True}},
            {"action": "tts.cloud_say", "data": {"entity_id": kitchen, "message": "Dinner"}},
            {"action": "assist_satellite.start_conversation", "target": {"entity_id": hall},
             "data": {"start_message": "Coming down?"}}]}})
    try:
        async def call(hass):
            actions = await logged(hass, "script.call")
            with pytest.raises(ServiceNotSupported):
                await hass.services.async_call("assist_satellite", "start_conversation", {
                    "entity_id": porch, "start_message": "Coming down?"}, blocking=True)
            await hass.async_block_till_done()
            return actions
        actions = await sandbox.run(call)
        assert said(actions, speakers) == [
            ("", chime, [hall]), ("", chime, [kitchen]), ("Dinner", None, [kitchen]), ("Coming down?", None, [hall])]
        assert sandbox.unsimulated == ["tts.cloud_say"] and effect_guards == []  # An outside receipt (P11.3).
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_a_toggle_reaches_every_device_home_assistant_toggles(hass, effect_guards):
    """Home Assistant toggles climate devices, humidifiers, sirens and remotes too: in a replay a toggle reaches their
    stand-ins, by name or through homeassistant.toggle, as it would at home."""
    devices = {"climate.heater": ("heat", {"hvac_modes": ["off", "heat"], "supported_features": 384}),  # On, off.
               "humidifier.dry": ("on", {}), "siren.bell": ("on", {"supported_features": 3}), "remote.tv": ("on", {})}
    sandbox = await started(home(**devices))
    try:
        async def toggle(hass):
            await hass.services.async_call("climate", "toggle", {"entity_id": "climate.heater"}, blocking=True)
            await hass.services.async_call("homeassistant", "toggle", {"entity_id": sorted(devices)[1:]}, blocking=True)
        await sandbox.run(toggle)
        assert {e: sandbox.hass.states.get(e).state for e in devices} == dict.fromkeys(devices, "off")
        assert sandbox.unsimulated == [] and effect_guards == []
    finally:
        await sandbox.stop()


SOURCE = {"time_zone": "America/Los_Angeles", "home_name": "Fixture Home",
          "areas": {"garage_old_id": {"name": "Garage", "aliases": ["Car Port"]}},
          "devices": {"phone-recorded-id": {"name": "Alex's Phone", "name_by_user": None, "area_id": None},
                      "relay-device": {"name": "Relay Box", "name_by_user": None, "area_id": "garage_old_id"}},
          "entities": {"switch.relay": {"name": "Garage Relay", "original_name": None, "aliases": [None, "Door"],
                                        "area_id": None, "device_id": "relay-device", "platform": "fixture"},
                       "switch.hidden_relay": {"name": "Hidden Relay", "original_name": None, "aliases": [None],
                                               "area_id": "garage_old_id", "platform": "fixture"}},
          "states": {"switch.relay": {"state": "on", "attributes": {"friendly_name": "Garage Relay"}},
                     "switch.hidden_relay": {"state": "off", "attributes": {"friendly_name": "Hidden Relay"}}},
          "exposure": {"switch.relay": True, "switch.hidden_relay": False}}


@pytest.mark.asyncio
async def test_sandbox_is_seeded_as_the_recorded_home(hass, effect_guards):
    from custom_components.assist_improver.sandbox import system_prompt
    sandbox = await started(kept_home(SOURCE), person=PERSON, integrations=["light"],
                            timer_device="phone-recorded-id")
    try:
        context = llm.LLMContext(platform="test", context=Context(user_id="alex"), language="en",
                                 assistant="conversation", device_id="phone-recorded-id")
        template = "Speaking from {{ device_attr(llm_context.device_id, 'name') }}."
        prompt, api = await sandbox.run(lambda h: system_prompt(h, context, "assist", template))
        assert prompt.startswith("Speaking from Alex's Phone.")  # The recorded device, by its sandbox ID.
        assert "Garage Relay" in prompt and "Door" in prompt and "Garage" in prompt  # Names, aliases, area via device.
        assert "Hidden Relay" not in prompt  # Not exposed when recorded.
        names = {t.name for t in api.tools}
        assert {"intent__HassTurnOn", "intent__HassStartTimer"} <= names  # Timers: the recorded device ran them.
        # The area's recorded alias reaches HA's own intent matching.
        result = await sandbox.run(assist_tool, "intent__HassTurnOff", {"area": "Car Port", "domain": ["switch"]})
        assert sandbox.hass.states.get("switch.relay").state == "off" and effect_guards == []
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_a_replay_shows_assist_the_live_context_the_recorded_home_did(hass, effect_guards):
    """Home Assistant's live context (GetLiveContext) in a replay of a recorded home is what it showed Assist when the
    home was recorded: every attribute it shows, and a sensor's state rounded to its display precision, recorded,
    stored and seeded, with names alike."""
    import json
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.helpers import entity_registry as er
    from conftest import assist_home
    from recordings import make_recording, recorded_today
    from custom_components.assist_improver.cases import recording_turns
    from custom_components.assist_improver.replay import SandboxHome, setup_now
    from custom_components.assist_improver.snapshot import snapshot
    await assist_home(hass, {})
    home = {  # Each attribute the live context shows, on a device that has it.
        "light.den_lamp": ("on", {"friendly_name": "Den lamp", "brightness": 128, "color_mode": "brightness",
                                  "supported_color_modes": ["brightness"]}),
        "climate.den": ("heat", {"friendly_name": "Den heater", "temperature": 22, "current_temperature": 19.5,
                                 "humidity": 40, "hvac_modes": ["off", "heat"]}),
        "fan.den": ("on", {"friendly_name": "Den fan", "percentage": 50, "current_temperature": 20,
                           "temperature_unit": "°C"}),
        "cover.garage": ("open", {"friendly_name": "Garage door", "current_position": 40, "device_class": "garage"}),
        "media_player.living_room_tv": ("playing", {"friendly_name": "Living room TV", "volume_level": 0.25,
                                                    "media_title": "Song", "media_artist": "Band",
                                                    "media_album_name": "Album"}),
        "sensor.garden": ("12.5", {"friendly_name": "Garden temperature", "device_class": "temperature",
                                   "unit_of_measurement": "°C"}),
    }
    home["sensor.rain_today"] = ("3.2049056", {"friendly_name": "Rain today", "device_class": "precipitation",
                                               "unit_of_measurement": "mm"})
    registry = er.async_get(hass)  # Shown to a tenth, as its integration suggests.
    registry.async_get_or_create("sensor", "fixture", "rain-today", suggested_object_id="rain_today")
    registry.async_update_entity_options("sensor.rain_today", "sensor", {"suggested_display_precision": 1,
                                                                         "unit_of_measurement": "mm"})
    for entity_id, (state, attributes) in home.items():
        hass.states.async_set(entity_id, state, attributes)
        async_expose_entity(hass, "conversation", entity_id, True)
    recording = make_recording()
    turn = recording_turns(recording)[0]
    turn["source"] = json.loads(json.dumps(snapshot(hass)))  # As the recording stores it.
    assert turn["source"]["entities"]["sensor.rain_today"]["options"]["sensor"] == {
        "suggested_display_precision": 1, "unit_of_measurement": "mm"}
    asked = turn["prepared"]["context"]
    context = llm.LLMContext(**{**asked, "context": Context(**asked["context"])})
    live = llm.ToolInput(tool_name="homeassistant__GetLiveContext", tool_args={})
    seen = await (await llm.async_get_api(hass, "assist", context)).async_call_tool(live)
    today = {**await setup_now(hass), "assistant": recorded_today(recording)["assistant"]}  # This home, as it is.
    replay = SandboxHome(hass, recording, turn, "live-context", today)
    try:
        await replay.assist(turn)
        replayed = await replay.sandbox.call("tool", tool_name=live.tool_name, tool_args=live.tool_args)
    finally:
        await replay.stop()
    assert "media_title: Song" in seen.data["result"] and "temperature_unit: °C" in seen.data["result"]
    assert "state: '3.2'" in seen.data["result"]
    assert replayed["data"] == seen.data and effect_guards == []


@pytest.mark.asyncio
async def test_the_households_scripts_and_automations_run_in_the_replay(hass, effect_guards):
    from recordings import make_recording, recorded_today, TARGET
    record = make_recording()
    for turn in record["turns"]:  # The household's own definitions.
        # HA 2026.10 names an exposed script's tool by its domain, as it does integrations' tools.
        turn["prepared"]["tools"].append({"name": "script__fixture_press", "description": "Fixture press",
                                          "parameters": {}})
        turn["source"]["scripts"] = {"fixture_press": {"alias": "Fixture press", "sequence": [
            {"action": "switch.turn_off", "target": {"entity_id": TARGET}},
            {"action": "switch.turn_on", "target": {"entity_id": TARGET}}]}}
        turn["source"]["automations"] = [{"id": "porch", "alias": "Porch on press", "triggers": [
            {"trigger": "state", "entity_id": TARGET, "to": "off"}],
            "actions": [{"action": "switch.turn_off", "target": {"entity_id": "switch.never_recorded"}}]}]
    from custom_components.assist_improver.cases import recording_turns
    from custom_components.assist_improver.replay import SandboxHome
    home = SandboxHome(hass, record, recording_turns(record)[0], "improver-replay-scripts", recorded_today(record),
                       change={"exposure": {"script.fixture_press": True}})
    try:
        await home.start()
        from conftest import inside
        await inside(home.sandbox).run(lambda h: h.services.async_call("script", "fixture_press", blocking=True))
        world = await home.snapshot()
        assert [c["service"] for c in world["commands"]] == ["switch.turn_off", "switch.turn_on"]
        # The automation reacted, on a device the home doesn't have: Home Assistant skips it, as at home.
        assert any("switch.never_recorded" in line for line in inside(home.sandbox).log)
        assert inside(home.sandbox).unsimulated == []
    finally:
        await home.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_home_assistant_sets_the_improver_up_in_a_sandbox_and_its_setup_does_nothing_there(hass, effect_guards):
    """A sandbox's Assist reads the household knowledge through the improver's llm platform: Home Assistant sets the
    improver up there as it sets up any integration, and the improver's own setup (its stores, the panel's commands)
    doesn't run there."""
    from homeassistant.const import EVENT_COMPONENT_LOADED
    from homeassistant.core import callback
    from custom_components.assist_improver import household
    from custom_components.assist_improver.const import DOMAIN
    sandbox = await Sandbox.start("UTC")
    try:
        loaded = []

        async def listen(h):  # Heard as the event fires (a callback): a plain function would run later, in a thread.
            h.bus.async_listen(EVENT_COMPONENT_LOADED, callback(lambda event: loaded.append(event)))
        await sandbox.run(listen)
        await sandbox.run(lambda h: household.serve(h, {"everyone": ["The porch light is on a timer."]}))
        assert [event.data["component"] for event in loaded if event.data["component"] == DOMAIN] == [DOMAIN]
        assert isinstance(sandbox.hass.data[household.KEY], household.Fixed)
        assert not any(str(command).startswith(DOMAIN) for command in sandbox.hass.data.get("websocket_api") or {})
    finally:
        await sandbox.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_an_ha_error_from_the_sandbox_reads_on_any_thread(hass, effect_guards):
    """HA translates its errors' messages from its own loop: the worker reads them on its own thread."""
    from homeassistant.exceptions import ServiceNotFound
    sandbox = await Sandbox.start("UTC")
    try:
        with pytest.raises(ServiceNotFound) as raised:
            await sandbox.run(lambda h: h.services.async_call("script", "never_loaded", blocking=True))
        assert await asyncio.to_thread(str, raised.value)  # A thread with no HA instance.
    finally:
        await sandbox.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_sandbox_installs_nothing_and_never_sets_up_the_supervisors_integration(hass, effect_guards,
                                                                                        monkeypatch):
    """The worker's Python is the real instance's: nothing is installed from a sandbox, from its start, so not while
    it's seeded either (an integration Assist's tools come from could ask for its requirements). The Supervisor's
    integration, whose services act on the host, is never set up there, even when Assist's tools today name it
    (DETAILS P11.8.3)."""
    from homeassistant import setup
    from recordings import make_recording, recorded_today
    from custom_components.assist_improver.sandbox import seed_options
    record = make_recording()
    today = recorded_today(record)
    today["tools"] += ["hassio__fixture_restore", "light__fixture_set"]
    options = seed_options(today, record["turns"][0])
    set_up, original = [], setup.async_setup_component

    async def recorded(hass, domain, config):
        set_up.append((domain, hass.config.skip_pip))
        return await original(hass, domain, config)
    sandbox = await Sandbox.start("UTC", kept_home(SOURCE))
    try:
        monkeypatch.setattr(setup, "async_setup_component", recorded)
        await sandbox.run(lambda h: seed(h, **options))
        assert ("light", True) in set_up and all(skipped for _, skipped in set_up)
        assert "hassio" not in {domain for domain, _ in set_up} | sandbox.hass.config.components
    finally:
        await sandbox.stop()
    assert effect_guards == []


PORCH_HOME = kept_home({
    "time_zone": "UTC", "home_name": "Fixture Home", "areas": {}, "devices": {},
    "entities": {"switch.relay": {"name": None, "aliases": ["Relay"]},
                 "light.porch": {"name": None, "aliases": ["Porch"]}},
    "states": {"switch.relay": {"state": "off", "attributes": {"friendly_name": "Relay"}},
               "light.porch": {"state": "on", "attributes": {"friendly_name": "Porch", "brightness": 200,
                                                            "color_mode": "brightness"}},
               "automation.porch_follows": {"state": "on", "attributes": {"friendly_name": "Porch follows"}}},
    "exposure": {"switch.relay": True, "light.porch": True}})
PORCH_FOLLOWS = [{"id": "follow", "alias": "Porch follows", "triggers": [
    {"trigger": "state", "entity_id": "switch.relay", "to": "on"}],
    "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.porch"}}]}]


@pytest.mark.asyncio
async def test_a_sandbox_seeded_as_a_baselines_home_after_a_turn_is_that_home_its_automations_on_or_off_too(
        hass, effect_guards):
    """A test that branches from its baseline starts from the baseline's home after the turn before (DETAILS P6.2):
    its states and attributes, and each automation on or off as the baseline left it, which Home Assistant keeps apart
    from the automation's configuration, so an automation the baseline switched off doesn't fire in the test."""
    from custom_components.assist_improver.replay import as_moved
    from custom_components.assist_improver.sandbox import home_now, moved

    def home(h):  # The sun is worked out anew from the clock as each sandbox starts: it moves on in between.
        return {e: s for e, s in home_now(h).items() if e != "sun.sun"}

    async def turn(h):  # The baseline's turn: the porch dimmed (an attribute gone too), its automation switched off.
        h.data[SANDBOX_KEY].standins["light.porch"].hold("on", {"brightness": 50}, removed=["color_mode"])
        await h.services.async_call("automation", "turn_off", {"entity_id": "automation.porch_follows"},
                                    blocking=True)
        await h.async_block_till_done()
        return moved(h.data[SANDBOX_KEY].moment, home_now(h)), home(h)

    async def relay_on(h):  # The branch turn: someone switches the relay on.
        h.data[SANDBOX_KEY].standins["switch.relay"].hold("on")
        await h.async_block_till_done()
        return h.states.get("light.porch").state

    async def now(h):
        return home(h)
    sandbox = await started(PORCH_HOME, "UTC", integrations=["light"], automations=PORCH_FOLLOWS)
    try:
        after, baseline = await sandbox.run(turn)
    finally:
        await sandbox.stop()
    sandbox = await started(as_moved(PORCH_HOME, after), "UTC", integrations=["light"], automations=PORCH_FOLLOWS)
    try:
        restored = await sandbox.run(now)
        porch = await sandbox.run(relay_on)
    finally:
        await sandbox.stop()
    assert after["automation.porch_follows"]["state"] == "off" and after["light.porch"]["removed"] == ["color_mode"]
    assert restored == baseline and porch == "on" and effect_guards == []


# What a stand-in of each entity domain starts as where a service needs something to act on: its state (else unknown)
# and attributes, as its integration would show them.
EVERY_STATE = {"light": "off", "switch": "off", "fan": "off", "siren": "off", "remote": "off", "humidifier": "off",
               "climate": "off", "cover": "closed", "valve": "closed", "lock": "unlocked", "media_player": "off",
               "vacuum": "docked", "lawn_mower": "docked", "alarm_control_panel": "disarmed", "select": "a",
               "number": "1", "text": "x", "date": "2026-01-01", "time": "07:00:00",
               "datetime": "2026-01-01T07:00:00+00:00", "water_heater": "eco", "todo": "0", "update": "off",
               "camera": "idle", "weather": "sunny", "calendar": "off", "assist_satellite": "idle"}
EVERY_ATTRIBUTES = {
    "climate": {"hvac_modes": ["off", "heat"], "fan_modes": ["low"], "preset_modes": ["eco"], "swing_modes": ["on"],
                "swing_horizontal_modes": ["on"], "min_temp": 7, "max_temp": 35, "min_humidity": 30,
                "max_humidity": 99},
    "cover": {"current_position": 0, "current_tilt_position": 0}, "valve": {"current_position": 0},
    "media_player": {"volume_level": 0.5, "source_list": ["TV"], "sound_mode_list": ["Movie"]},
    "vacuum": {"fan_speed_list": ["low"]}, "select": {"options": ["a", "b"]},
    "number": {"min": 0, "max": 10, "step": 1},
    "text": {"min": 0, "max": 100, "mode": "text"}, "fan": {"preset_modes": ["eco"]},
    "water_heater": {"operation_list": ["eco", "off"], "min_temp": 30, "max_temp": 60},
    "alarm_control_panel": {"code_arm_required": False},
    "humidifier": {"available_modes": ["eco"], "min_humidity": 30, "max_humidity": 80},
    "update": {"installed_version": "1", "latest_version": "2"},
    "light": {"supported_color_modes": ["hs", "color_temp"], "min_color_temp_kelvin": 2000,
              "max_color_temp_kelvin": 6500, "effect_list": ["a"]}}


def every_domain():
    """A home with a device of each entity domain Home Assistant has, with all its domain's features."""
    from homeassistant.const import Platform
    from custom_components.assist_improver.standin import _features
    return home(**{f"{p}.any": (EVERY_STATE.get(p, "unknown"), {"supported_features": sum(_features(p) or []),
                                                                 **EVERY_ATTRIBUTES.get(p, {})}) for p in Platform})


def example(field, entity_id):
    """A value for a service's field, by its selector, as one could pick it in Home Assistant's own form."""
    kind, conf = next(iter((field.get("selector") or {"text": {}}).items()))
    conf = conf or {}
    if kind == "entity":  # One of the domain it asks for, else the device the call is on.
        domain = (conf.get("filter") or [conf])[0].get("domain")
        return f"{domain[0] if isinstance(domain, list) else domain}.any" if domain else entity_id
    picks = {"number": conf.get("min", 1), "boolean": True, "color_rgb": [255, 0, 0], "time": "07:00:00",
             "color_temp": conf.get("min", 3000), "date": "2026-11-06", "datetime": "2026-11-06 07:00:00",
             "duration": {"seconds": 1}, "object": {}, "constant": conf.get("value"),
             "media": {"media_content_id": "x", "media_content_type": "music"}}
    if kind == "select":
        option = (conf.get("options") or ["x"])[0]
        return option["value"] if isinstance(option, dict) else option
    return picks.get(kind, "x")


def required(fields, entity_id):
    """A service's required fields (in its sections too), each with an example."""
    found = {}
    for name, field in (fields or {}).items():
        if "fields" in field:
            found |= required(field["fields"], entity_id)
        elif field.get("required"):
            found[name] = example(field, entity_id)
    return found


@pytest.mark.asyncio
async def test_every_entity_domain_has_a_stand_in_and_each_service_on_it_changes_it_or_is_reported_not_simulated(
        hass, effect_guards):
    """DETAILS P11.4: a device of each entity domain Home Assistant has (homeassistant.const.Platform) is a
    stand-in of its domain's own class, but one whose integration sets up no config entries (an image processor's,
    standin.standable). Each of the domain's services on it, as Home Assistant validates it, changes it (writes its
    state), answers, is done by the calls Home Assistant's own part of it makes (a speech engine speaking on a media
    player) or is reported not simulated: none is taken silently, none breaks. One Home Assistant
    refuses (its data, the device's features) never reaches it, as at home."""
    import voluptuous as vol
    from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED, Platform
    from homeassistant.core import SupportsResponse, callback
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.service import async_get_all_descriptions
    from custom_components.assist_improver.standin import standable
    sandbox = await started(every_domain(), "UTC")

    async def each_service(h):
        descriptions, written, outcomes = await async_get_all_descriptions(h), [], {}
        for event in (EVENT_STATE_CHANGED, EVENT_STATE_REPORTED):
            h.bus.async_listen(event, callback(lambda e: written.append(e.data["entity_id"])),
                               event_filter=callback(lambda data: True))
        for platform in Platform:
            entity_id = f"{platform}.any"
            for service, description in (descriptions.get(platform) or {}).items():
                if "target" not in description:  # Not a service on a device.
                    continue
                marks = (len(sandbox.commands), len(sandbox.unsimulated), len(written), len(sandbox.calls))
                try:
                    answer = await h.services.async_call(
                        platform, service, {"entity_id": entity_id, **required(description.get("fields"), entity_id)},
                        blocking=True, return_response=h.services.supports_response(platform, service)
                        is not SupportsResponse.NONE)
                    await h.async_block_till_done()
                except (vol.Invalid, HomeAssistantError):
                    outcomes[f"{platform}.{service}"] = "refused"
                    continue
                except Exception as err:  # noqa: BLE001 - what the test looks for.
                    outcomes[f"{platform}.{service}"] = f"broke: {err!r}"
                    continue
                outcomes[f"{platform}.{service}"] = (
                    "silent" if len(sandbox.commands) == marks[0] else "taken" if entity_id in written[marks[2]:]
                    else "not simulated" if len(sandbox.unsimulated) > marks[1]
                    else "answered" if (answer or {}).get(entity_id) is not None
                    else "passed on" if len(sandbox.calls) > marks[3] + 1 else "silent")
        return outcomes
    try:
        outcomes = await sandbox.run(each_service)
    finally:
        await sandbox.stop()
    assert set(sandbox.standins) == {f"{p}.any" for p in Platform if standable(p)}
    assert {c: o for c, o in outcomes.items() if o == "silent" or o.startswith("broke")} == {}
    assert sum(o != "refused" for o in outcomes.values()) >= 100 and effect_guards == []  # Most reach their device.


@pytest.mark.asyncio
async def test_a_command_reports_back_as_its_device_would_a_mode_as_its_state_a_setting_as_its_attribute(
        hass, effect_guards):
    """DETAILS P11.4: a thermostat set to heat at 22 degrees shows heat as its state and 22 as its temperature, as
    Home Assistant's own climate entity does: no attribute the state already shows, nor the command's target. A
    speaker turned off shows none of what it played."""
    seeded = home(**{"climate.den": ("off", {"hvac_modes": ["off", "heat"], "temperature": 20, "min_temp": 7,
                                             "max_temp": 35, "supported_features": 1}),
                     "media_player.den": ("playing", {"media_title": "Song", "volume_level": 0.4,
                                                      "supported_features": 256})})  # It turns off.
    sandbox = await started(seeded, "UTC")
    try:
        async def heat(h):
            call = h.services.async_call
            await call("climate", "set_hvac_mode", {"entity_id": "climate.den", "hvac_mode": "heat"}, blocking=True)
            await call("climate", "set_temperature", {"entity_id": "climate.den", "temperature": 22}, blocking=True)
            await call("media_player", "turn_off", {"entity_id": "media_player.den"}, blocking=True)
            return h.states.get("climate.den"), h.states.get("media_player.den")
        thermostat, speaker = await sandbox.run(heat)
    finally:
        await sandbox.stop()
    assert thermostat.state == "heat" and dict(thermostat.attributes) == {
        "hvac_modes": ["off", "heat"], "min_temp": 7, "max_temp": 35, "temperature": 22, "supported_features": 1}
    assert speaker.state == "off" and dict(speaker.attributes) == {"supported_features": 256}
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_setting_reports_back_as_the_attribute_its_device_shows_whatever_its_field_or_property_is_called(
        hass, effect_guards):
    """DETAILS P11.4: a fan turned to reverse shows direction reverse (Home Assistant's fan reads it from
    current_direction), and a remote started on an activity shows it as its current_activity, as their own entities
    do, though neither showed one before."""
    seeded = home(**{"fan.ceiling": ("on", {"supported_features": 4}),  # FanEntityFeature.DIRECTION
                     "remote.living_room": ("on", {"activity_list": ["Music", "Watch TV"],
                                                   "supported_features": 4})})  # RemoteEntityFeature.ACTIVITY
    sandbox = await started(seeded, "UTC")
    try:
        async def set_both(h):
            call = h.services.async_call
            await call("fan", "set_direction", {"entity_id": "fan.ceiling", "direction": "reverse"}, blocking=True)
            await call("remote", "turn_on", {"entity_id": "remote.living_room", "activity": "Watch TV"}, blocking=True)
            return h.states.get("fan.ceiling"), h.states.get("remote.living_room")
        fan, remote = await sandbox.run(set_both)
    finally:
        await sandbox.stop()
    assert fan.attributes["direction"] == "reverse" and remote.attributes["current_activity"] == "Watch TV"
    assert sandbox.unsimulated == [] and effect_guards == []


@pytest.mark.asyncio
async def test_devices_keep_their_ids_areas_and_names_and_two_integrations_sharing_a_unique_id_both_stand_in(
        hass, effect_guards):
    """DETAILS P11.4: a recorded config entry's devices move to a stand-in entry titled the same, keeping their ids,
    areas and names; two integrations' lights sharing a unique ID both appear, each under the stand-in by its
    integration's and its own. No device integration is set up."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from custom_components.assist_improver.standin import STANDIN
    seeded = kept_home({
        "areas": {"porch": {"name": "Porch"}},
        "devices": {"bridge-lamp": {"name": "Lamp", "name_by_user": "Porch lamp", "area_id": "porch",
                                    "config_entry_id": "hue-entry", "identifiers": [["hue", "lamp-1"]]}},
        "entities": {"light.lamp": {"platform": "hue", "unique_id": "1", "config_entry_id": "hue-entry",
                                    "device_id": "bridge-lamp"},
                     "light.strip": {"platform": "zha", "unique_id": "1", "config_entry_id": "zha-entry"}},
        "states": {"light.lamp": {"state": "off", "attributes": {"friendly_name": "Porch lamp"}},
                   "light.strip": {"state": "on", "attributes": {"friendly_name": "Strip", "color_mode": "onoff",
                                                                "supported_color_modes": ["onoff"]}}}})
    setup = {"entries": {"hue-entry": {"domain": "hue", "title": "Hue Bridge"},
                         "zha-entry": {"domain": "zha", "title": "Zigbee"}}}
    sandbox = await started(seeded, "UTC", setup)
    try:
        async def look(h):
            entities, devices = er.async_get(h), dr.async_get(h)
            lamp, strip = entities.async_get("light.lamp"), entities.async_get("light.strip")
            device = devices.async_get("bridge-lamp")
            titles = {e.entry_id: e.title for e in h.config_entries.async_entries(STANDIN)}
            return ({e.entity_id: (e.platform, e.unique_id, titles[e.config_entry_id]) for e in (lamp, strip)},
                    (device.name, device.name_by_user, device.area_id, titles[device.config_entry_id]),
                    {e: h.states.get(e).state for e in ("light.lamp", "light.strip")}, set(h.config.components))
        stood_in, device, states, components = await sandbox.run(look)
    finally:
        await sandbox.stop()
    assert stood_in == {"light.lamp": (STANDIN, "hue:1", "Hue Bridge"), "light.strip": (STANDIN, "zha:1", "Zigbee")}
    assert device == ("Lamp", "Porch lamp", "porch", "Hue Bridge")
    assert states == {"light.lamp": "off", "light.strip": "on"}
    assert not {"hue", "zha"} & components and effect_guards == []


@pytest.mark.asyncio
async def test_a_stand_in_sets_up_after_another_sandbox_in_the_process_stopped(hass, effect_guards):
    """Sandboxes in one process (tests serve them inline) share the instance Home Assistant reports odd usage to: the
    last one started. A stand-in entry's setup gives it nothing to report, so it sets up once that sandbox is gone."""
    from homeassistant.config_entries import ConfigEntryState
    from custom_components.assist_improver.standin import STANDIN
    seeded = kept_home({"entities": {"switch.relay": {"platform": "zha", "unique_id": "1", "config_entry_id": "zha"}},
                        "states": {"switch.relay": {"state": "on", "attributes": {"friendly_name": "Relay"}}}})
    first = await Sandbox.start("UTC", seeded, {"entries": {"zha": {"domain": "zha", "title": "Zigbee"}}})
    try:
        last = await Sandbox.start("UTC")
        await last.stop()
        await first.run(lambda h: seed(h))

        async def entries(h):
            return [e.state for e in h.config_entries.async_entries(STANDIN)], h.states.get("switch.relay").state
        assert await first.run(entries) == ([ConfigEntryState.LOADED], "on")
    finally:
        await first.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_to_do_list_keeps_its_items_and_takes_each_change_and_a_media_player_answers_from_the_recording(
        hass, effect_guards):
    """DETAILS P11.4: a to-do list's stand-in holds its recorded items and adds, renames, moves and removes them as
    asked, its state the count left to do; a media player steps its volume as asked, and its search and browse
    answer with what the recording's search found on it, else aren't simulated."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature as Feature
    shelf = {"title": "Album", "media_class": "album", "media_content_id": "album-1", "media_content_type": "album",
             "can_play": True, "can_expand": False}
    seeded = home(**{"todo.shopping": ("1", {"supported_features": 15}),  # Create, delete, update, move.
                     "media_player.kitchen": ("idle", {"volume_level": 0.5, "supported_features": int(
                         Feature.VOLUME_STEP | Feature.VOLUME_SET | Feature.SEARCH_MEDIA | Feature.BROWSE_MEDIA)}),
                     "media_player.office": ("idle", {"supported_features": int(Feature.SEARCH_MEDIA)})})
    seeded["todo"] = {"todo.shopping": {"items": [{"uid": "milk", "summary": "Milk", "status": "needs_action"}]}}
    sandbox = await started(seeded, "UTC", recorded={"media": [{"entity_ids": ["media_player.kitchen"],
                                                                "media": shelf}]})
    try:
        async def shop(h):
            call = h.services.async_call
            await call("todo", "add_item", {"entity_id": "todo.shopping", "item": "Eggs"}, blocking=True)
            await call("todo", "update_item", {"entity_id": "todo.shopping", "item": "Milk", "rename": "Oat milk"},
                       blocking=True)
            list_ = h.data[SANDBOX_KEY].standins["todo.shopping"]
            await list_.async_move_todo_item(list_.todo_items[1].uid)  # As Home Assistant's to-do card moves it.
            moved = await call("todo", "get_items", {"entity_id": "todo.shopping"}, blocking=True,
                               return_response=True)
            await call("todo", "remove_item", {"entity_id": "todo.shopping", "item": "Eggs"}, blocking=True)
            left = h.states.get("todo.shopping").state
            for step in ("volume_up", "volume_up", "volume_down"):
                await call("media_player", step, {"entity_id": "media_player.kitchen"}, blocking=True)
            found = await call("media_player", "search_media", {"entity_id": "media_player.kitchen",
                                                                "search_query": "album"},
                               blocking=True, return_response=True)
            browsed = await call("media_player", "browse_media", {"entity_id": "media_player.kitchen",
                                                                  "media_content_id": "album-1"},
                                 blocking=True, return_response=True)
            await call("media_player", "search_media", {"entity_id": "media_player.office", "search_query": "album"},
                       blocking=True, return_response=True)
            return (moved, left, h.states.get("media_player.kitchen").attributes["volume_level"], found, browsed)
        moved, left, volume, found, browsed = await sandbox.run(shop)
    finally:
        await sandbox.stop()
    assert [(i["summary"], i["status"]) for i in moved["todo.shopping"]["items"]] == [
        ("Eggs", "needs_action"), ("Oat milk", "needs_action")]
    assert left == "1" and volume == 0.6
    assert [r.media_content_id for r in found["media_player.kitchen"].result] == ["album-1"]
    assert browsed["media_player.kitchen"].media_content_id == "album-1"
    assert sandbox.unsimulated == ["media_player.search_media media_player.office"] and effect_guards == []


@pytest.mark.asyncio
async def test_a_gap_whose_waits_cant_pass_at_once_takes_its_real_seconds_then_the_clock_is_at_the_turns_time(
        hass, effect_guards, monkeypatch):
    """A job of Home Assistant's keeps running, so the sandbox's clock can't move on by itself: the gap to a turn an
    hour later takes no more than its real seconds (GAP_SECONDS), then the clock is at that turn's time (DETAILS
    P6.2.5)."""
    monkeypatch.setattr(sandbox_module, "GAP_SECONDS", 0.5)
    sandbox = await started({**RELAY_AND_PORCH, "at": "2026-03-01T06:00:00+00:00"}, time_zone="UTC")
    busy, turn = threading.Event(), "2026-03-01T07:00:00+00:00"

    async def keep_busy(h):
        h.async_add_executor_job(busy.wait, 30)

    async def now(h):
        return h.loop.clock.now()
    try:
        await sandbox.run(keep_busy)
        began = time.monotonic()
        await sandbox.run(lambda h: observe(h, {"at": turn}))
        assert 0.5 <= time.monotonic() - began < 5
        assert await sandbox.run(now) >= datetime.fromisoformat(turn).timestamp()
    finally:
        busy.set()
        await sandbox.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_others_move_on_a_helper_between_turns_goes_through_home_assistant_so_a_toggle_after_acts_on_it(
        hass, effect_guards):
    """Someone switched the household's toggle helper on between turns; then it's toggled: the helper itself took the
    move (Home Assistant's own, as a scene sets it, DETAILS P6.2.8), so the toggle switches it off, as at home."""
    seeded = home(**{"input_boolean.guest": ("off", {"friendly_name": "Guest", "editable": True})})
    setup = {"helpers": {"input_boolean": {"storage": {"version": 1, "minor_version": 1, "key": "input_boolean",
                                                       "data": {"items": [{"id": "guest", "name": "Guest"}]}},
                                           "yaml": None}}}
    sandbox = await started({**seeded, "at": "2026-03-01T06:00:00+00:00"}, time_zone="UTC", setup=setup)

    async def toggled(h):
        moved = h.states.get("input_boolean.guest").state
        await h.services.async_call("input_boolean", "toggle", {"entity_id": "input_boolean.guest"}, blocking=True)
        return moved, h.states.get("input_boolean.guest").state
    moves = [{"input_boolean.guest": {"state": "on", "attributes": {}, "removed": []}}]
    try:
        await sandbox.run(lambda h: observe(h, {"at": "2026-03-01T06:01:00+00:00"}, moves))
        assert await sandbox.run(toggled) == ("on", "off")
    finally:
        await sandbox.stop()
    assert effect_guards == []
