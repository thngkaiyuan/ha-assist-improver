"""Journey analyzer role: digest in, journeys in the label shape out.

This is the first reflective role (roadmap Milestone 4). Its instructions are
a draft until it is scored against the household's labels. The output schema uses a
flat criteria list because structured output needs fixed property names;
to_label turns it into the label shape the scorer compares.
"""

import probatio

ROLE = "journey_analyzer"

INSTRUCTIONS = """You analyse one recorded session between a household and its Home Assistant voice assistant ("Assist"). The input is a digest. It contains the user's turns, what the assistant said and which tools it called with what results, real device state changes with the turn they happened in, and what Assist could and could not control.

Split the session into journeys. A journey is a trip from a source state to the destination the user intended, carried out by the assistant. The destination is what the user wanted to be true: final device states, device changes in a given order (such as a relay pressed off then on), or something they needed to know. A session can hold several journeys. Several things asked for at once are one journey. A question or request the user makes in order to complete the current journey is part of that journey, not a new one. Once a journey's destination is reached, the next request starts a new journey, even if it repeats the same action. Keep separately requested journeys separate. Never merge them into a broader request the user did not make, even if their intent seems predictable.

Use the whole session, including later corrections and how it ended, to work out what the user wanted. Use only entity IDs that appear in the digest. Judge "reached" from recorded state changes and tool results, never from the assistant's own claims. A relay's on/off state does not prove a physical position.

Interventions are user turns spent correcting the assistant, clarifying, repeating, or adding a step the assistant should have handled. The turn that starts a journey is not an intervention, and neither is a turn that starts a new, separately requested journey. For each intervention, judge whether it was avoidable: could a better prompt, preference, tool, script, alias or exposure setting have removed it? A fault outside the assistant, such as speech recognition mishearing the user, is not avoidable by those means.

Turns that are not real requests, such as microphone tests, speech misfires or small talk, go in not_journeys.

If you cannot establish a journey's destination, set destination.status to "needs_feedback" and write one short question for the user. Do not guess.

Criteria:
- state: one item per entity, with entity_id and the final state. Add brightness_pct only if the user asked for a brightness.
- transition: one item per entity, with entity_id and the ordered sequence of states it must pass through.
- informational: one item per required fact, with fact."""

_CRITERION = {probatio.Optional("entity_id"): str, probatio.Optional("state"): str,
              probatio.Optional("sequence"): [str], probatio.Optional("brightness_pct"): int,
              probatio.Optional("fact"): str}

STRUCTURE = probatio.Schema({
    probatio.Required("journeys"): [{
        probatio.Required("id"): str,
        probatio.Required("first_turn"): int,
        probatio.Required("last_turn"): int,
        probatio.Required("source"): str,
        probatio.Required("destination"): {
            probatio.Required("status"): probatio.In(["established", "needs_feedback"]),
            probatio.Required("summary"): str,
            probatio.Required("kind"): probatio.In(["state", "transition", "informational"]),
            probatio.Required("criteria"): [_CRITERION],
            probatio.Optional("question"): str,
        },
        probatio.Required("reached"): probatio.In(["yes", "no", "partly", "unknown"]),
        probatio.Required("interventions"): [{
            probatio.Required("turn"): int,
            probatio.Required("type"): probatio.In(["correction", "clarification", "repetition", "extra_step"]),
            probatio.Required("avoidable"): probatio.In(["yes", "no", "unsure"]),
            probatio.Required("why"): str,
        }],
    }],
    probatio.Required("not_journeys"): [{probatio.Required("turn"): int, probatio.Required("what"): str}],
    probatio.Required("notes"): str,
})


def _criteria(kind, items):
    if kind == "informational":
        return [i["fact"] for i in items if i.get("fact")]
    out = {}
    for item in items:
        entity = item.get("entity_id")
        if not entity:
            continue
        if kind == "transition":
            out[entity] = list(item.get("sequence") or [])
        elif item.get("brightness_pct") is not None:  # Some providers send every optional field, as null.
            out[entity] = {"state": item.get("state"), "brightness_pct": item["brightness_pct"]}
        else:
            out[entity] = item.get("state")
    return out


def to_label(recording_id, output):
    """Analyzer output in the label shape (LABELS.md), ready for the scorer."""
    journeys = []
    for j in output.get("journeys", []):
        dest = j["destination"]
        converted = {"id": j["id"], "turns": [j["first_turn"], j["last_turn"]], "continues_from": None,
                     "source": j["source"],
                     "destination": {"summary": dest["summary"], "kind": dest["kind"],
                                     "criteria": _criteria(dest["kind"], dest.get("criteria", []))},
                     "reached": j["reached"], "interventions": j.get("interventions", [])}
        if dest["status"] == "needs_feedback":
            converted["destination"]["status"] = "needs_feedback"
            converted["question"] = dest.get("question") or "Help your assistant learn: what would you have liked to happen?"
        journeys.append(converted)
    return {"recording_id": recording_id, "journeys": journeys,
            "not_journeys": output.get("not_journeys", []), "notes": output.get("notes", "")}
