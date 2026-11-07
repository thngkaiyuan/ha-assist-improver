"""What recording a home costs, on a made-up large home (by default 2,000 states, 2,500 entity registry
entries, 230 devices; every attribute as Home Assistant's integrations write them): the time a user turn
holds Home Assistant's event loop (the first turn's read of the moment, and a later turn's), the time serializing it
takes off the loop, the stored size of a one-turn recording's moment (and its parts), and what a replay adds that
grows with the home: reading today's setup (replay.setup_now), shipping the moment to a fresh worker (its JSON both
ways) and seeding the worker's sandbox with it. Each time is the median of several runs, and the first run's too
(cold: Home Assistant caches a state's dict once it's made, which a state that just changed doesn't have yet).

Usage, from the checkout: .venv/bin/python tools/measure_recording.py [runs, default 5]. Not shipped; no household
data (the home is generated).
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SIZES = {"states": 2000, "entries": 2500, "devices": 230}
# Rough share of a large home's states per domain (sensors and binary sensors most of them).
DOMAINS = {"sensor": 0.4, "binary_sensor": 0.25, "switch": 0.07, "event": 0.07, "image": 0.04,
           "device_tracker": 0.03, "button": 0.02, "light": 0.02, "update": 0.01, "number": 0.01,
           "select": 0.01, "media_player": 0.01, "camera": 0.01, "script": 0.01, "cover": 0.005,
           "climate": 0.005, "scene": 0.005, "automation": 0.005, "todo": 0.002, "lock": 0.002, "vacuum": 0.002}


def main(runs):
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder, "found.json")
        subprocess.run([sys.executable, "-m", "pytest", "-p", "conftest", "-q", "-p", "no:cacheprovider", "-s",
                        f"{__file__}::test_measure"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "fixture-test"), "MEASURE_OUT": str(out),
                            "MEASURE_RUNS": str(runs)})
        print(json.dumps(json.loads(out.read_text()), indent=1))


def _attributes(domain, n):
    """What an integration of this domain typically writes as a state's attributes."""
    name = {"friendly_name": f"{domain.replace('_', ' ').title()} {n}"}
    token = f"{n:064x}"
    kinds = ("temperature", "humidity", "power", "energy", "battery", "illuminance", "voltage", "signal_strength")
    if domain == "sensor":
        kind = kinds[n % len(kinds)]
        if n % 9 == 0:
            return {**name, "device_class": "timestamp"}
        if n % 7 == 0:
            return {**name, "icon": "mdi:information-outline"}
        return {**name, "state_class": "measurement" if kind != "energy" else "total_increasing",
                "unit_of_measurement": {"temperature": "°C", "humidity": "%", "power": "W", "energy": "kWh",
                                        "battery": "%", "illuminance": "lx", "voltage": "V",
                                        "signal_strength": "dBm"}[kind], "device_class": kind}
    if domain == "binary_sensor":
        return {**name, "device_class": ("motion", "door", "window", "connectivity", "problem")[n % 5]}
    if domain == "event":
        return {**name, "event_types": ["initial_press", "short_release", "long_press", "long_release"],
                "event_type": "short_release", "device_class": "button"}
    if domain in ("image", "camera"):
        extra = {"frontend_stream_type": "hls", "brand": "Maker", "model_name": "Cam 2",
                 "supported_features": 2} if domain == "camera" else {}
        return {**name, "access_token": token, **extra,
                "entity_picture": f"/api/{domain}_proxy/{domain}.thing_{n}?token={token}"}
    if domain == "device_tracker":
        return {**name, "source_type": "router", "ip": f"10.0.1.{n % 250}", "mac": f"AA:BB:CC:DD:{n % 99:02}:EE",
                "host_name": f"host-{n}"}
    if domain == "light":
        return {**name, "supported_color_modes": ["color_temp", "hs"], "color_mode": "color_temp",
                "brightness": 180, "color_temp_kelvin": 2700, "color_temp": 370, "hs_color": [27.0, 56.0],
                "rgb_color": [255, 169, 87], "xy_color": [0.52, 0.38], "min_color_temp_kelvin": 2000,
                "max_color_temp_kelvin": 6535, "min_mireds": 153, "max_mireds": 500, "effect_list": ["None", "Candle"],
                "effect": "None", "supported_features": 44}
    if domain == "update":
        return {**name, "auto_update": False, "display_precision": 0, "installed_version": "1.2.3",
                "in_progress": False, "latest_version": "1.2.4", "release_summary": "Fixes and improvements.",
                "release_url": f"https://example.com/releases/{n}", "skipped_version": None, "title": "Firmware",
                "update_percentage": None, "entity_picture": "https://brands.home-assistant.io/_/maker/icon.png",
                "supported_features": 23}
    if domain == "number":
        return {**name, "min": 0, "max": 100, "step": 1, "mode": "slider", "unit_of_measurement": "%"}
    if domain == "select":
        return {**name, "options": ["Off", "Low", "Medium", "High", "Auto"]}
    if domain == "media_player":
        return {**name, "volume_level": 0.3, "is_volume_muted": False, "media_content_id": f"track:{n}",
                "media_content_type": "music", "media_duration": 215, "media_position": 30,
                "media_title": "A song", "media_artist": "A band", "media_album_name": "An album",
                "source_list": [f"Source {i}" for i in range(10)], "source": "Source 1", "device_class": "speaker",
                "entity_picture": f"/api/media_player_proxy/media_player.thing_{n}?token={token}&cache=abc",
                "supported_features": 4127295}
    if domain == "climate":
        return {**name, "hvac_modes": ["off", "heat", "cool", "auto"], "min_temp": 7, "max_temp": 35,
                "target_temp_step": 0.5, "fan_modes": ["auto", "low", "high"], "preset_modes": ["home", "away"],
                "current_temperature": 21.5, "temperature": 21, "hvac_action": "idle", "fan_mode": "auto",
                "preset_mode": "home", "supported_features": 409}
    if domain == "cover":
        return {**name, "current_position": 100, "device_class": "shade", "supported_features": 15}
    if domain == "script":
        return {**name, "last_triggered": "2026-11-01T08:00:00+00:00", "mode": "single", "current": 0}
    if domain == "automation":
        return {**name, "id": str(n), "last_triggered": "2026-11-01T08:00:00+00:00", "mode": "single", "current": 0}
    if domain == "scene":
        return {**name, "id": str(n), "entity_id": ["light.thing_1", "light.thing_2"]}
    if domain == "todo":
        return {**name, "supported_features": 15}
    return {**name, "supported_features": 1}


async def build_home(hass, sizes=SIZES):
    """A home of these sizes in this (offline) Home Assistant: devices in areas, entity registry entries with what
    integrations register, and states with their attributes; a few hundred entries are disabled (no state)."""
    from types import MappingProxyType
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import EntityCategory
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
    from conftest import assist_home
    await assist_home(hass, {})
    entry = ConfigEntry(domain="maker", title="Maker", data={"host": "192.0.2.1"}, source="user", version=1,
                        minor_version=1, options={}, unique_id=None, subentries_data=None,
                        discovery_keys=MappingProxyType({}))
    await hass.config_entries.async_add(entry)
    areas = [ar.async_get(hass).async_create(f"Room {n}").id for n in range(20)]
    devices = []
    for n in range(sizes["devices"]):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("maker", f"device-{n:04}")},
            connections={("mac", f"aa:bb:cc:dd:{n // 256:02x}:{n % 256:02x}")}, manufacturer="Maker",
            model=f"Model {n % 12}", model_id=f"M{n % 12}", sw_version="1.2.3", hw_version="rev2",
            name=f"Device {n}", serial_number=f"SN{n:08}")
        dr.async_get(hass).async_update_device(device.id, area_id=areas[n % len(areas)])
        devices.append(device.id)
    registry, domains = er.async_get(hass), list(DOMAINS)
    counts = {d: max(1, round(share * sizes["states"])) for d, share in DOMAINS.items()}
    counts["sensor"] += sizes["states"] - sum(counts.values())
    made = 0
    for domain, count in counts.items():
        for n in range(count):
            attributes = _attributes(domain, made)
            entity = registry.async_get_or_create(
                domain, "maker", f"{domain}-{n:05}-{made:032x}", suggested_object_id=f"thing_{made}",
                config_entry=entry, device_id=devices[made % len(devices)],
                capabilities={k: attributes[k] for k in ("state_class", "options", "hvac_modes", "min", "max",
                                                         "supported_color_modes") if k in attributes} or None,
                original_name=attributes["friendly_name"].split(" ", 1)[1], has_entity_name=True,
                translation_key=domain, original_device_class=attributes.get("device_class"),
                supported_features=attributes.get("supported_features") or 0,
                unit_of_measurement=attributes.get("unit_of_measurement"),
                entity_category=EntityCategory.DIAGNOSTIC if made % 11 == 0 else None)
            if domain == "sensor" and "unit_of_measurement" in attributes:
                registry.async_update_entity_options(entity.entity_id, "sensor", {"suggested_display_precision": 1})
            hass.states.async_set(entity.entity_id, "on" if domain != "sensor" else str(made % 40), attributes)
            if made % 4 == 0:
                async_expose_entity(hass, "conversation", entity.entity_id, True)
            made += 1
    for n in range(sizes["entries"] - made):  # Disabled entries: no state.
        registry.async_get_or_create("sensor", "maker", f"disabled-{n:05}", suggested_object_id=f"off_{n}",
                                     config_entry=entry, device_id=devices[n % len(devices)],
                                     original_name=f"Disabled {n}", has_entity_name=True,
                                     disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    await hass.async_block_till_done()
    return entry


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
else:  # Run by pytest, in a process of its own.
    import statistics
    import time

    import pytest

    @pytest.mark.asyncio
    async def test_measure(hass):
        from custom_components.assist_improver import snapshot
        from custom_components.assist_improver.replay import setup_now
        from custom_components.assist_improver.sandbox import Sandbox, seed
        runs = int(os.environ.get("MEASURE_RUNS", "5"))
        await build_home(hass)
        found = {k: [] for k in ("loop_first_turn", "loop_later_turn", "serialize_first_turn", "setup_now",
                                 "ship_json", "seed")}
        for _ in range(runs):
            began = time.perf_counter()
            read = snapshot.taken(hass)
            found["loop_first_turn"].append(time.perf_counter() - began)
            began = time.perf_counter()
            snapshot.taken(hass, moment=False)  # The moment is stored once: a later turn's read.
            found["loop_later_turn"].append(time.perf_counter() - began)
            began = time.perf_counter()
            home = await hass.async_add_executor_job(snapshot.home, read)
            found["serialize_first_turn"].append(time.perf_counter() - began)
            began = time.perf_counter()
            today = await setup_now(hass)
            found["setup_now"].append(time.perf_counter() - began)
            source = snapshot.at_moment(today["home"], home)
            began = time.perf_counter()
            line = (json.dumps({"op": "seed", "args": {"source": source}}, separators=(",", ":")) + "\n").encode()
            source = json.loads(line)["args"]["source"]
            found["ship_json"].append(time.perf_counter() - began)
            began = time.perf_counter()  # Starting writes the home's stores; seeding loads them.
            sandbox = await Sandbox.start("UTC", source, today)
            try:
                await sandbox.run(seed)
                found["seed"].append(time.perf_counter() - began)
            finally:
                await sandbox.stop()
        sizes = {"moment": len(json.dumps(home, separators=(",", ":"))), "seed_line": len(line),
                 **{k: len(json.dumps(home.get(k), separators=(",", ":"))) for k in ("states", "entities", "devices")}}
        Path(os.environ["MEASURE_OUT"]).write_text(json.dumps({
            "seconds": {k: round(statistics.median(v), 3) for k, v in found.items() if v},
            "seconds_cold": {k: round(v[0], 3) for k, v in found.items() if v},
            "bytes": sizes, "home": {"states": len(hass.states.async_all()), "devices": len(home.get("devices") or {}),
                                     "entries": len(home.get("entities") or {})}}))
