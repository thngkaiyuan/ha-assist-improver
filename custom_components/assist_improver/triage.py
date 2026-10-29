"""A quick check before the journey analyzer: was every request in a session fulfilled smoothly?

A small model (any AI Task, local ones included) reads a conversation, however many turns it
has, with all the context Assist had (its instructions, which name the speaker and list the household's
devices; every tool call and Home Assistant's result), what changed in the home, and
the involved devices' names, areas and aliases, and answers once. Its tokens are
cheap next to the analysis they can save, so it gets everything it needs to decide. Only a
sure "smooth" skips the analyzer; anything else, including a failed check, gets
the full analysis. A skipped session still reaches the generator and reviewer:
code records it as a session without friction, with no goals extracted.
"""

import probatio

ROLE = "triage"
INSTRUCTIONS = """You check one recorded conversation between a household and its Home Assistant voice assistant ("Assist"), to decide whether it needs a closer look. Answer smooth only if every request was fulfilled smoothly: in each turn, Assist did what the user asked, in the turn they asked it, on the devices they meant, with the settings they asked for. A later turn is often the sign: the user repeating, correcting or answering a question. A later turn is smooth only when it's a new request, made after the earlier ones were done as asked. It is not smooth when the user corrects, repeats, rephrases or clarifies a request; when Assist asks the user anything they then have to answer (even a fair question: answering it is an extra turn), refuses, fails, or says it can't; when Assist acts on a device the user didn't mean, or sets settings the user left open (guessing a level, mode or colour); when someone changes the same device soon after Assist did; when the user complains or comments on how Assist behaved (it was careless, too wordy, its tone, "next time do X"), even after it did what they asked; or when you can't tell. You get what Assist had: its instructions (which say who is speaking and list the household's devices, with their names and areas; given in full once, then only the lines that changed in a later turn), each of its tool calls with Home Assistant's result, then what changed in the home and the names, areas and aliases of the devices involved. Judge which device the user meant from the household's own names, areas and aliases, as the speaker would (their own room or device is the one named for them). Say sure only when the conversation leaves no doubt. A wrong "smooth" hides a problem, so when in doubt answer not smooth. Give a one-sentence reason."""
STRUCTURE = probatio.Schema({probatio.Required("smooth"): bool, probatio.Required("sure"): bool,
                             probatio.Required("why"): str})
STUB = {"goals": [], "not_requests": [], "feedback": [], "extra_turns": [], "pending_turns": [],
        "side_effects_left": [], "question_for_user": None}


def payload(view):
    """All the context Assist had, turn by turn (its instructions as recorded; what the user said; each tool call
    with Home Assistant's full result; its reply), what changed in the home during each turn, and the names, areas
    and aliases of the devices involved. Assist's instructions name the speaker and list the household's devices,
    the same in every turn but for a line or two (the time), so they come in full with the first turn and after that
    only as the lines that changed: the same facts, without a copy per turn (most of a long conversation's input)."""
    from .cases import recording_turns
    from .serialization import plain
    turns, involved, previous = [], set(), None
    for turn, line in zip(recording_turns(view.recording, replay=False), view.conversation()):
        steps = []
        for message in turn["messages"][1:]:
            if message["role"] == "assistant":
                steps += [{"assist_calls": c["tool_name"], "with": c["tool_args"]} for c in message.get("tool_calls") or []]
                if message.get("content"):
                    steps.append({"assist_says": message["content"]})
            elif message["role"] == "tool_result":
                steps.append({"home_assistant_returns": plain(message.get("result"))})
        changes = []
        for c in view.read_turn(line["turn"])["state_changes_during_turn"]:
            involved.add(c["entity_id"])
            changes.append({"device": c["name"], "from": c["old"], "to": c["new"], "by": c["by"],
                            **({"attributes_changed": c["attributes_changed"]} if c.get("attributes_changed") else {}),
                            **({"seconds_after_assistant_reply": c["seconds_after_assistant_reply"]}
                               if c.get("seconds_after_assistant_reply") else {})})
        involved |= {a.rsplit("(", 1)[1].rstrip(")") for call in line.get("assist_acted_on", [])
                     for a in call.get("acted_on", []) + call.get("failed_on", []) if "(" in a}
        system = next((m["content"] for m in turn.get("history") or [] if m["role"] == "system"), None)
        turns.append({"turn": line["turn"], "at": line["at"], **_instructions(previous, system),
                      "user_says": line["user_says"], "steps": steps, "home_changes": changes})
        previous = system or previous
    devices = [{k: v for k, v in view._card(e).items() if k in ("entity_id", "name", "area", "aliases")}
               for e in sorted(involved) if e in view.source.get("states", {}) or e in view.source.get("entities", {})]
    return {"turns": turns, "devices_involved": devices}


def _instructions(previous, system):
    """A turn's instructions: in full the first time, then only the lines that changed since the turn before."""
    if not system or system == previous:
        return {}
    if previous is None:
        return {"assist_instructions": system}
    old, new = previous.splitlines(), system.splitlines()
    return {"assist_instructions_changed": {"no_longer": [l for l in old if l not in set(new)],
                                            "now": [l for l in new if l not in set(old)]}}


def instructions_seen(view):
    """The instructions Assist ran with in this conversation, as the quick check reads them (who's speaking, the
    household knowledge, the devices): in full as of the first turn, then each later turn's changed lines. For the
    analyzer, which needs to know the household as the other roles do."""
    from .cases import recording_turns
    out, previous, changed = {}, None, []
    for turn in recording_turns(view.recording, replay=False):
        system = next((m["content"] for m in turn.get("history") or [] if m["role"] == "system"), None)
        seen = _instructions(previous, system)
        if "assist_instructions" in seen:
            out.update(seen)
        elif seen:
            changed.append({"turn": turn["number"], **seen["assist_instructions_changed"]})
        previous = system or previous
    return {**out, **({"assist_instructions_changed": changed} if changed else {})}


def skips_analysis(answer):
    """Only a sure "smooth" skips the analyzer."""
    return bool(answer) and answer.get("smooth") is True and answer.get("sure") is True


def unanalysed(recording_id):
    """A recording no role has analysed: the generator and reviewer still read it, with no goals extracted."""
    return {"recording_id": recording_id, **STUB, "not_analysed": True}


def smooth_analysis(recording_id, answer):
    """What the generator and reviewer get for a session the check found smooth: no friction, no goals extracted."""
    return {"recording_id": recording_id, **STUB, "triaged": {"smooth": True, "why": answer.get("why", "")}}
