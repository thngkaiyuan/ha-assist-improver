"""Journey analyzer role: a recorded session in, the user's goals out.

The model does only the human modelling: what the user wanted, as goals
(device states, device sequences or facts), each tagged with the turn that
first asked for it, and sub-goals: steps the user only took to get an earlier
goal. It answers by calling submit_goals, which checks the answer against the
recording (entities exist, states are ones the device has) and rejects it with
the reasons until it is valid; the last accepted answer counts, or the best one
if none was accepted. Code derives the rest from the recording: which turns were
extra (interventions), whether Assist's own changes reached each goal, and
changes Assist left behind that no goal asked for. It is agentic: it starts from
the conversation and reads turns and the household with read-only tools over
the recording (exploration.py).
"""

from datetime import datetime

import probatio

from .exploration import _SessionTool

ROLE = "journey_analyzer"
BRIGHTNESS_TOLERANCE = 5
ASSISTANT = ("assistant", "follow-on of an assistant change on the same physical device")

INSTRUCTIONS = """You analyse one recorded session between a household and its Home Assistant voice assistant ("Assist"). Your job is to work out what the user wanted. You start with an overview: the whole conversation (what the user and the assistant said in each turn, and what each of the assistant's calls acted on, as Home Assistant's responses name the devices), the household's areas and the assistant's tools. Read the whole conversation before deciding anything, since later turns often explain what earlier ones meant. Entity IDs, tool results and state changes appear only in read_turn, so read every turn with it before answering (several turns at once if you like). Explore the household with the other tools whenever you need to understand what the user meant: which device, which room, what a device can do. Before deciding what a request means for a device, find out how the household operates it: scripts or automations that drive it, and other entities on the same device. The tools answer from the recording, never the live home. Request several turns or lookups in one round when you can.

List the user's goals. A goal is one thing the user wanted:
- state: a device ending in a state (entity_id and state; add brightness_pct only if the user asked for a brightness);
- sequence: a device passing through states in order (entity_id and states), such as a relay pressed off then on;
- fact: something the user needed to know (fact);
- ask: devices whose settings the user's words leave open (entity_ids). Don't guess what the user didn't say: list the controls the user should set, as narrow as their words allow. One control when only its value is open (a seat heater with no level given); the related controls of a device when only the device or the theme is clear ("make the car toasty": its heating controls). The user will be shown those controls and set them.
Give each goal the turn where the user first asked for it. Several things asked for at once are several goals with the same turn, one per device. What later turns reveal changes the earlier goal: a correction or clarification makes it more exact rather than adding a goal. When the assistant asks the user a follow-up question, the answer makes the earlier request more exact: give those details the turn of the original request. When the assistant answers a vague request by guessing and asking what the user wants, the user's reply says what they wanted all along: those goals belong to the vague request's turn, and the assistant's guess is not a goal. When a later turn shows how a goal must be reached (such as a relay pressed off then on), change the earlier goal itself. Once a goal has been reached, asking for the same thing again is a new goal with the new turn. Respect the timing and separation of the user's requests: what they asked for separately, at different times, stays separate goals; what they asked for together is one request. Never turn them into a broader request the user didn't make, even if their intent seems predictable.

A sub-goal is a step the user took only in order to get an earlier goal: asking what the assistant can do after it failed, asking what it did or how a device is doing, telling it how to do the job, finishing a step it left half done, or undoing something it did that the user did not want. Give a sub-goal the turn where it was asked, the same fields as a goal, and serves: the turn of the goal it was for. A sub-goal is never a goal of its own.

Describe what the user wanted, not what the assistant did. Use only entity IDs the tools show you. Include a device in a goal only if the user wanted something from it.

Turns that are not real requests, such as microphone tests, speech misfires or small talk, go in not_requests.

If you cannot even tell which devices the user meant, write one short question for the user in question_for_user instead of guessing. Otherwise leave it empty.

Answer only by calling submit_goals, with sub-goals in the same goals list. It checks your answer against the recording, including that every state is one the device has (device_details shows a device's modes and options). If it reports problems, fix them and submit again. When it accepts your answer, finish with one short sentence."""

# Added when the user filed the conversation for improvement themselves (on-demand improvements).
FILED = """

The user filed this conversation for improvement themselves (filed_by_user in your input), so something here didn't go the way they wanted; its note, when there is one, says what in their words. Take it into account, but still describe the goals as the conversation shows them."""

# Added to the instructions when improvements exist (waiting for review, or applied): a goal one of them already fixes
# stops here instead of flowing on to the proposer.
DUPLICATES = """

Some of what went wrong here may already have a fix: an improvement waiting for the household's review, or one already applied. Your input's improvements_on_these_devices lists those that touch the devices in this session; list_improvements lists them all, and read_improvement shows one in full (its changes, and the conversations it was tested on). Once your goals are accepted, check each goal the user had to repeat, correct or clarify, or that went unmet: if an improvement already fixes what went wrong there, call mark_duplicate with the goal's turn, that improvement and why, so it isn't fixed twice. One applied before this session took place didn't prevent what happened here, so it doesn't count. Mark a goal only when you're sure; otherwise leave it."""

STRUCTURE = probatio.Schema({
    probatio.Required("goals"): [{
        probatio.Required("turn"): int,
        probatio.Required("summary"): str,
        probatio.Required("kind"): probatio.In(["state", "sequence", "fact", "ask"]),
        probatio.Optional("entity_id"): str,
        probatio.Optional("entity_ids"): [str],
        probatio.Optional("state"): str,
        probatio.Optional("states"): [str],
        probatio.Optional("brightness_pct"): int,
        probatio.Optional("fact"): str,
        probatio.Optional("serves"): int,
    }],
    probatio.Required("not_requests"): [{probatio.Required("turn"): int, probatio.Required("what"): str}],
    probatio.Required("question_for_user"): str,
})


def clean_goal(goal):
    """A goal with only the fields its kind uses (some providers send every optional field, as null)."""
    keep = {"state": ("entity_id", "state", "brightness_pct"), "sequence": ("entity_id", "states"),
            "fact": ("fact",), "ask": ("entity_ids",)}[goal["kind"]] + ("serves",)
    return {"turn": goal["turn"], "summary": goal.get("summary", ""), "kind": goal["kind"],
            **{k: goal[k] for k in keep if goal.get(k) is not None}}


def top_level(goals):
    return [g for g in goals if g.get("serves") is None]


def extra_turns(turn_count, goals, not_requests, question=None):
    """Turns that started no goal (sub-goals don't count) and were real requests: the interventions.

    While a question to the user is open, such turns are pending the answer instead, so this returns none.
    """
    if question:
        return []
    used = {g["turn"] for g in top_level(goals)} | {n["turn"] for n in not_requests}
    return [t for t in range(1, turn_count + 1) if t not in used]


def _brightness_ok(goal, value):
    return "brightness_pct" not in goal or (
        isinstance(value, (int, float)) and abs(value - goal["brightness_pct"]) <= BRIGHTNESS_TOLERANCE)


def reached(view, goal, until_turn=None):
    """Whether Assist's own changes reached a state or sequence goal; None for facts.

    Only changes from the goal's turn up to (not including) until_turn count. A state goal is reached when
    the device ends there because of Assist, or was already there and nobody moved it; a sequence goal when
    Assist's changes pass through its states in order.
    """
    if goal["kind"] in ("fact", "ask") or not goal.get("entity_id"):
        return None
    entity = goal["entity_id"]
    changes = sorted(view.changes.get(entity, []), key=lambda c: datetime.fromisoformat(c["at"]))
    start = view.source.get("states", {}).get(entity) or {}
    state, brightness = start.get("state"), (start.get("attributes") or {}).get("brightness")
    brightness = None if brightness is None else round(brightness * 100 / 255)
    window = []
    for change in changes:
        if change["during_turn"] < goal["turn"]:
            state = change["new"]
            brightness = (change.get("attributes_changed", {}).get("brightness_pct") or [None, brightness])[1]
        elif until_turn is None or change["during_turn"] < until_turn:
            window.append(change)
    if goal["kind"] == "sequence":
        states = list(goal.get("states") or [])
        if len(states) > 1 and states[0] == state:  # Written from the state before the first change.
            states = states[1:]
        visited = iter(c["new"] for c in window if c["by"] in ASSISTANT)
        return bool(states) and all(any(s == v for v in visited) for s in states)
    by_assistant = None  # None: unchanged in the window.
    for change in window:
        new_brightness = change.get("attributes_changed", {}).get("brightness_pct")
        moved = change["new"] != state or ("brightness_pct" in goal and new_brightness is not None)
        if new_brightness is not None:
            brightness = new_brightness[1]
        if moved:  # Attribute-only updates (readings, colour) neither reach nor undo a state.
            state, by_assistant = change["new"], change["by"] in ASSISTANT
    return state == goal.get("state") and _brightness_ok(goal, brightness) and by_assistant is not False


def _assistant_changes(view):
    return {e: [c for c in cs if c["by"] in ASSISTANT] for e, cs in view.changes.items()}


def side_effects_left(view, goals):
    """Devices Assist changed that no goal asked for (or left open for the user) and that did not end as they started."""
    wanted = {g.get("entity_id") for g in top_level(goals)} | {e for g in goals for e in g.get("entity_ids") or []}
    left = []
    for entity, changes in _assistant_changes(view).items():
        if not changes or entity in wanted:
            continue
        start = (view.source.get("states", {}).get(entity) or {}).get("state", changes[0]["old"])
        end = view.changes[entity][-1]["new"]
        if end != start:
            left.append({"entity_id": entity, "start": start, "end": end})
    return left


def to_analysis(recording_id, output, view):
    """The model's goals, plus what code derives from the recording."""
    goals = [clean_goal(g) for g in output.get("goals", [])]
    tops = top_level(goals)
    for goal in tops:
        later = [g["turn"] for g in tops if g.get("entity_id") == goal.get("entity_id") and g["turn"] > goal["turn"]]
        goal["reached"] = reached(view, goal, min(later) if later else None)
    not_requests = list(output.get("not_requests", []))
    question = (output.get("question_for_user") or "").strip()
    count = len(view.digest["turns"])
    return {"recording_id": recording_id, "goals": goals, "not_requests": not_requests,
            "extra_turns": extra_turns(count, goals, not_requests, question),
            "pending_turns": extra_turns(count, goals, not_requests) if question else [],
            "side_effects_left": side_effects_left(view, goals), "question_for_user": question or None}


# States a device can be in, by domain, where its attributes don't list them (climate, select and friends do).
STATES = {
    **dict.fromkeys(("light", "switch", "fan", "input_boolean", "humidifier", "siren", "remote", "automation",
                     "script"), {"on", "off"}),
    "cover": {"open", "closed", "opening", "closing", "stopped"},
    "valve": {"open", "closed", "opening", "closing"},
    "lock": {"locked", "unlocked", "open", "opening", "locking", "unlocking", "jammed"},
    "media_player": {"on", "off", "idle", "playing", "paused", "standby", "buffering"},
    "vacuum": {"cleaning", "docked", "idle", "paused", "returning", "error"},
    "alarm_control_panel": {"disarmed", "armed_home", "armed_away", "armed_night", "armed_vacation",
                            "armed_custom_bypass", "triggered"},
}
OPTION_ATTRIBUTES = {"climate": "hvac_modes", "select": "options", "input_select": "options",
                     "water_heater": "operation_list"}


def allowed_states(view, entity_id):
    """The states a device can be in, or None when they can't be told from the recording."""
    domain = entity_id.split(".", 1)[0]
    attributes = (view.source.get("states", {}).get(entity_id) or {}).get("attributes") or {}
    listed = attributes.get(OPTION_ATTRIBUTES.get(domain, ""))
    if listed:
        return set(listed)
    return STATES.get(domain)


def problems(view, answer):
    """What is wrong with an answer, checked against the recording; empty means valid."""
    try:
        STRUCTURE(answer)
    except probatio.Invalid as err:
        return [f"The answer does not match the format: {err}"]
    turn_count, known = len(view.digest["turns"]), set(view._entities())
    found = []
    top_turns = {g["turn"] for g in answer["goals"] if g.get("serves") is None}
    for n, goal in enumerate(answer["goals"], 1):
        where = f"Goal {n} (turn {goal['turn']})"
        if not 1 <= goal["turn"] <= turn_count:
            found.append(f"{where}: there is no turn {goal['turn']}; turns are 1 to {turn_count}.")
        kind, entity = goal["kind"], goal.get("entity_id")
        if kind == "fact":
            if not goal.get("fact"):
                found.append(f"{where}: a fact goal needs fact.")
        elif kind == "ask":
            ids = goal.get("entity_ids") or []
            if not ids:
                found.append(f"{where}: an ask goal needs entity_ids: the controls the user should set.")
            unknown = [e for e in ids if e not in known]
            if unknown:
                found.append(f"{where}: no device {', '.join(map(repr, unknown))} in this household; "
                             "use entity IDs the tools show.")
        elif not entity:
            found.append(f"{where}: a {kind} goal needs entity_id.")
        elif entity not in known:
            found.append(f"{where}: there is no device {entity!r} in this household; use an entity ID the tools show.")
        else:
            states = [goal.get("state")] if kind == "state" else list(goal.get("states") or [])
            if not states or None in states:
                found.append(f"{where}: a {kind} goal needs {'state' if kind == 'state' else 'states'}.")
            allowed = allowed_states(view, entity)
            wrong = [s for s in states if s is not None and allowed is not None and s not in allowed]
            if wrong:
                found.append(f"{where}: {entity} has no state {', '.join(map(repr, wrong))}; "
                             f"its states are {', '.join(sorted(allowed))}.")
            if goal.get("brightness_pct") is not None and (
                    not entity.startswith("light.") or not 0 <= goal["brightness_pct"] <= 100):
                found.append(f"{where}: brightness_pct is for lights, from 0 to 100.")
        serves = goal.get("serves")
        if serves is not None and (serves not in top_turns or serves > goal["turn"]):
            found.append(f"{where}: serves must be the turn of an earlier goal (not a sub-goal); "
                         f"goals start at turns {sorted(top_turns) or 'none'}.")
    for item in answer["not_requests"]:
        if not 1 <= item["turn"] <= turn_count:
            found.append(f"Not-request at turn {item['turn']}: there is no such turn.")
    return found


class SubmitGoals(_SessionTool):
    """The analyzer's only way to answer. Keeps every submission so the best one can be used."""
    name = "submit_goals"
    description = ("Submit your answer: the user's goals (sub-goals in the same list, with serves), the turns that "
                   "were not requests, and any question for the user. The answer is checked against the recording; "
                   "if problems are reported, fix them and submit again.")
    parameters = STRUCTURE

    def __init__(self, view, then="Finish with one short sentence."):
        super().__init__(view)
        self.submissions, self.then = [], then

    def run(self, **answer):
        found = problems(self.view, answer)
        self.submissions.append({"answer": answer, "problems": found})
        if found:
            return {"accepted": False, "problems": found}
        return {"accepted": True, "message": f"Accepted. {self.then}"}

    def best(self):
        """(status, answer, problems): the last accepted submission, else the one with fewest problems."""
        if not self.submissions:
            return "no_answer_submitted", None, []
        accepted = [s for s in self.submissions if not s["problems"]]
        if accepted:
            return "accepted", accepted[-1]["answer"], []
        best = min(reversed(self.submissions), key=lambda s: len(s["problems"]))
        return "best_rejected", best["answer"], best["problems"]


class MarkDuplicate(_SessionTool):
    """A goal an existing improvement already fixes: it stops here, kept with that improvement, instead of going on
    to the proposer. Checked by code; the marks on goals in the final answer count."""
    name = "mark_duplicate"
    description = ("Mark a goal whose friction an existing improvement (from list_improvements) already fixes, so it "
                   "isn't fixed twice: the goal's turn, the improvement, and why it fixes what went wrong there.")
    parameters = probatio.Schema({probatio.Required("goal_turn"): int, probatio.Required("improvement"): str,
                                  probatio.Required("why"): str})
    THEN = "Check existing improvements for any goal that had friction (mark_duplicate), then finish with one short " \
           "sentence."

    def __init__(self, view, existing):
        super().__init__(view)
        self.existing, self.marks = {i["improvement"]: i for i in existing}, {}

    def run(self, goal_turn, improvement, why):
        from .exploration import applied_too_early
        item = self.existing.get(improvement)
        if item is None:
            return {"accepted": False, "problem": f"There's no improvement {improvement!r}; list_improvements shows "
                                                  f"{', '.join(self.existing) or 'none'}."}
        if not 1 <= goal_turn <= len(self.view.digest["turns"]):
            return {"accepted": False, "problem": f"This session has no turn {goal_turn}."}
        if not why.strip():
            return {"accepted": False, "problem": f"Say why {item['title']!r} fixes what went wrong there."}
        if too_early := applied_too_early(item, self.view.digest["started"]):
            return {"accepted": False, "problem": f"{too_early} Leave this goal unmarked."}
        self.marks[goal_turn] = {"goal_turn": goal_turn, "improvement": improvement, "why": why.strip()}
        return {"accepted": True, "message": f"Marked: the goal at turn {goal_turn} is already fixed by "
                                             f"{item['title']!r}. It must be one of your accepted goals."}

    def kept(self, answer):
        """The marks on goals in the final answer (a mark on a turn that isn't a goal there is dropped)."""
        turns = {g["turn"] for g in top_level((answer or {}).get("goals", []))}
        return [mark for turn, mark in sorted(self.marks.items()) if turn in turns]
