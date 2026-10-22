"""Bounded declarative script simulation in a private RecordedWorld.

This is not HA's script executor. Unsupported syntax stops evaluation. Scripts
must be idle, argument-free static sequences of switch on/off actions or direct
calls to other captured scripts. Timing and physical position are not modeled.
"""

from copy import deepcopy
import re

from .simulation import UnsupportedSimulation


def compile_script(entity_id, definitions, source):
    steps = []

    def visit(script_id, stack):
        if script_id in stack or len(stack) >= 8:
            raise UnsupportedSimulation("Recursive or excessively nested script")
        definition = definitions.get(script_id)
        if not isinstance(definition, dict) or not re.fullmatch(r"script\.[a-z0-9_]+", script_id):
            raise UnsupportedSimulation("Script definition is missing or invalid")
        if source["states"].get(script_id, {}).get("state") != "off":
            raise UnsupportedSimulation("Script is not known to be idle in the frozen source")
        if (set(definition) - {"alias", "description", "icon", "sequence", "mode", "fields"}
                or definition.get("mode", "single") != "single" or definition.get("fields")):
            raise UnsupportedSimulation("Only argument-free single-mode scripts are modeled")
        sequence = definition.get("sequence")
        if not isinstance(sequence, list) or not sequence:
            raise UnsupportedSimulation("Script must have a nonempty static sequence")
        for step in sequence:
            if (not isinstance(step, dict) or set(step) - {"action", "target", "data", "metadata"}
                    or step.get("data") or step.get("metadata")):
                raise UnsupportedSimulation("Unmodeled script action, data or control flow")
            action = step.get("action", "")
            if not isinstance(action, str):
                raise UnsupportedSimulation("Action must be a literal service name")
            if action.startswith("script."):
                if "target" in step:
                    raise UnsupportedSimulation("Only direct blocking script calls are modeled")
                visit(action, [*stack, script_id])
                continue
            if action not in {"switch.turn_on", "switch.turn_off"}:
                raise UnsupportedSimulation("Only static switch on/off effects are modeled")
            target = step.get("target")
            if not isinstance(target, dict) or set(target) != {"entity_id"}:
                raise UnsupportedSimulation("Explicit entity targets are required")
            entities = target["entity_id"]
            if isinstance(entities, str):
                entities = [entities]
            if (not isinstance(entities, list) or not entities or len(entities) > 64
                    or any(not isinstance(e, str) or not re.fullmatch(r"switch\.[a-z0-9_]+", e) for e in entities)
                    or len(set(entities)) != len(entities)):
                raise UnsupportedSimulation("Switch targets must be distinct literal entity IDs")
            for entity in entities:
                if source["states"].get(entity, {}).get("state") not in {"on", "off"}:
                    raise UnsupportedSimulation("Switch effect is unknown from the recorded source")
                steps.append({"script": script_id, "action": action, "entity_id": entity,
                              "state": "on" if action == "switch.turn_on" else "off"})
                if len(steps) > 64:
                    raise UnsupportedSimulation("Script exceeds bounded simulation coverage")

    visit(entity_id, [])
    return steps


def execute_compiled(world, entity_id, steps):
    """Simulate one full call, checking every precondition before changing state."""
    if any(world.states.get(step["entity_id"]) not in {"on", "off"} for step in steps):
        raise UnsupportedSimulation("Script target state became unknown")
    for step in steps:
        target, after = step["entity_id"], step["state"]
        before = world.states[target]
        world.calls.append({"tool": entity_id.replace(".", "__", 1), "args": {},
                            "entity_id": target, "state": after, "action": step["action"],
                            "script": step["script"], "provenance": "declarative_simulation"})
        if before != after:
            world.transitions.append({"entity_id": target, "old": before, "new": after})
        world.states[target] = after
    # Matches HA ActionTool's ordinary no-response script return shape.
    return {"data": {"result": None}, "error": False}


def script_tool_definition(entity_id, definition, source):
    """The pinned HA ScriptTool schema for fieldless scripts, checked in fixtures."""
    description = definition.get("description") or None
    aliases = source.get("entities", {}).get(entity_id, {}).get("aliases", [])
    if aliases:
        text = "Aliases: " + str(sorted(aliases))
        description = f"{description}. {text}" if description else text
    return {"name": entity_id.replace(".", "__", 1), "description": description,
            "integration": "script", "parameters": {
                "type": "object", "properties": {}, "additionalProperties": False}}
