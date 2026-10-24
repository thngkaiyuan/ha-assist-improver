"""A quick check before the journey analyzer: was every request in a session fulfilled smoothly?

A small model (any AI Task, local ones included) reads the conversation with what
Assist's calls acted on and what changed in the home, and answers once. Only a
sure "smooth" skips the analyzer; anything else, including a failed check, gets
the full analysis. A skipped session still reaches the generator and reviewer:
code records it as a session without friction, with no goals extracted.
"""

import probatio

ROLE = "triage"
INSTRUCTIONS = """You check one recorded conversation between a household and its Home Assistant voice assistant ("Assist"), to decide whether it needs a closer look. Answer smooth only if every request was fulfilled smoothly: in each turn, Assist did what the user asked, in the turn they asked it, on the devices they meant, with the settings they asked for. It is not smooth when the user corrects, repeats, rephrases or clarifies a request; when Assist asks a question it could have answered itself, refuses, fails, or says it can't; when Assist acts on a device the user didn't mean, or sets settings the user left open (guessing a level, mode or colour); when someone changes the same device soon after Assist did; or when you can't tell. Say sure only when the conversation leaves no doubt. A wrong "smooth" hides a problem, so when in doubt answer not smooth. Give a one-sentence reason."""
STRUCTURE = probatio.Schema({probatio.Required("smooth"): bool, probatio.Required("sure"): bool,
                             probatio.Required("why"): str})
STUB = {"goals": [], "not_requests": [], "extra_turns": [], "pending_turns": [], "side_effects_left": [],
        "question_for_user": None}


def payload(view):
    """Each turn: what the user said, what Assist said and acted on, and what changed in the home during it."""
    turns = []
    for line in view.conversation():
        changes = [{"device": c["name"], "from": c["old"], "to": c["new"], "by": c["by"],
                    **({"attributes_changed": c["attributes_changed"]} if c.get("attributes_changed") else {}),
                    **({"seconds_after_assistant_reply": c["seconds_after_assistant_reply"]}
                       if c.get("seconds_after_assistant_reply") else {})}
                   for c in view.read_turn(line["turn"])["state_changes_during_turn"]]
        turns.append({**line, "home_changes": changes})
    return {"turns": turns}


def skips_analysis(answer):
    """Only a sure "smooth" skips the analyzer."""
    return bool(answer) and answer.get("smooth") is True and answer.get("sure") is True


def unanalysed(recording_id):
    """A recording no role has analysed: the generator and reviewer still read it, with no goals extracted."""
    return {"recording_id": recording_id, **STUB, "not_analysed": True}


def smooth_analysis(recording_id, answer):
    """What the generator and reviewer get for a session the check found smooth: no friction, no goals extracted."""
    return {"recording_id": recording_id, **STUB, "triaged": {"smooth": True, "why": answer.get("why", "")}}
