"""Applying a change (changeset.py) and driving replays (replay.py, worker.py) use only Home Assistant's public API: no
underscore name of HA's, no other integration's non-root module, and none of the accessors HA keeps for its own
internals (the HACS review)."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path("custom_components/assist_improver")
# HA's exposure API, as its own Assist integrations use it.
PUBLIC_MODULES = {"homeassistant.components.homeassistant.exposed_entities"}
INTERNAL = {"async_services_internal", "DATA_REGISTRY"}  # Named publicly, but for HA's own internals.


def private_uses(source):
    """What a module uses of Home Assistant that isn't its public API."""
    tree, bound, found = ast.parse(source), set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("homeassistant"):
            parts = node.module.split(".")
            if parts[:2] == ["homeassistant", "components"] and len(parts) > 3 and node.module not in PUBLIC_MODULES:
                found.append(node.module)
            found += [f"{node.module}.{a.name}" for a in node.names if a.name.startswith("_")]
            bound |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            bound |= {a.asname or a.name.split(".")[0] for a in node.names if a.name.startswith("homeassistant")}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in INTERNAL:
            found.append(node.attr)
        elif (isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__")
              and isinstance(node.value, ast.Name) and node.value.id in bound):
            found.append(f"{node.value.id}.{node.attr}")
    return found


def test_the_check_finds_each_kind():
    assert private_uses("from homeassistant.helpers import entity_registry as er, llm\n"
                        "from homeassistant.components.script.config import async_validate_config_item\n"
                        "er._serialize_aliases([])\nllm._async_get_apis(hass)\n"
                        "hass.services.async_services_internal()\n") == [
        "homeassistant.components.script.config", "er._serialize_aliases", "llm._async_get_apis",
        "async_services_internal"]


@pytest.mark.parametrize("module", ["changeset.py", "replay.py", "worker.py"])
def test_applying_and_replaying_use_only_home_assistants_public_api(module):
    assert private_uses((PACKAGE / module).read_text()) == []
