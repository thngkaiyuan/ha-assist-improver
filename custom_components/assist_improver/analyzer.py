"""Journey analyzer role: a recorded session in, the user's goals out.

The model does only the human modelling: what the user wanted, as goals
(device states, device sequences, facts, actions that leave no device state
or settings left open), each tagged
with the turn that first asked for it, and sub-goals, each of which exists only
because of its goal (DETAILS P2.3.2): a part of it (a device asked for with it,
or a repair of one Assist left wrong), or a reaction that names what it reacts
to: the goal still unmet (a step), what Assist said or did in one turn, in the
household's own words (conduct: about that turn, quoting them), the trouble as
a whole (a wrap-up: filing it, giving up) or the goal met (a thanks). It answers
by calling submit_goals, which checks the answer against the recording (entities
exist, states are ones the device has, a quote is the household's own words)
and rejects it with the reasons until it is valid; the last accepted answer
counts, or the best one if none was accepted. Code derives the rest from the
recording: which turns were extra (interventions), whether Assist's own changes
reached each goal, and changes Assist left behind that no goal asked for. It is
agentic: it starts from the conversation and reads turns and the household with
read-only tools over the recording (exploration.py).
"""

from datetime import datetime
import re

import probatio

from .exploration import BRIGHTNESS_TOLERANCE, RoleTool

ROLE = "journey_analyzer"
ASSISTANT = ("assistant", "follow-on of an assistant change on the same physical device")

INSTRUCTIONS = """You analyse one recorded session between a household and its Home Assistant voice assistant ("Assist"). Your job is to work out what the user wanted. You start with an overview: the whole conversation (what the user and the assistant said in each turn, and what each of the assistant's calls acted on, as Home Assistant's responses name the devices), the household's areas and the assistant's tools. Read the whole conversation before deciding anything, since later turns often explain what earlier ones meant. Entity IDs, tool results and state changes appear only in read_turn, so read every turn with it before answering (several turns at once if you like). Explore the household with the other tools whenever you need to understand what the user meant: which device, which room, what a device can do. Before deciding what a request means for a device, find out how the household operates it: scripts or automations that drive it, and other entities on the same device. The tools answer from the recording, never the live home. Request several turns or lookups in one round when you can.

List the user's goals. A goal is one thing the user wanted:
- state: a device ending in a state (entity_id and state; add brightness_pct only if the user asked for a brightness);
- sequence: a device passing through states in order (entity_id and states), such as a relay pressed off then on;
- fact: something the user needed to know. fact: what the answer needs (the facts, distinctions or limits it must give, never the answer itself); entity_ids: the devices the answer is about, when it's about the home; quote: the form they asked for the answer in ("in a single brief sentence"), in their own words, only when they asked for one;
- action: something done that leaves no device in a new state. outcome announce: something spoken or played on devices, whatever its content; entity_ids: every device it's for, in one action (each speaker the household has, when they say all of them). Asked together with a device request, it's a part at that turn;
- ask: devices whose settings the user's words leave open (entity_ids). Don't guess what the user didn't say: list the controls the user should set, as narrow as their words allow. One control when only its value is open (a seat heater with no level given); the related controls of a device when only the device or the theme is clear ("make the car toasty": its heating controls). The user will be shown those controls and set them.
Give each goal the turn where the user first asked for it. A goal is one state of the home at one time: several things asked for at once are one goal, so give the first device as the goal and each other device as a sub-goal with the same turn that serves it. What later turns reveal changes the earlier goal: a correction or clarification makes it more exact rather than adding a goal. When the assistant asks the user a follow-up question, the answer makes the earlier request more exact: give those details the turn of the original request. When the assistant answers a vague request by guessing and asking what the user wants, the user's reply says what they wanted all along: those goals belong to the vague request's turn, and the assistant's guess is not a goal. When a later turn shows how a goal must be reached (such as a relay pressed off then on), change the earlier goal itself. Once a goal has been reached, asking for the same thing again is a new goal with the new turn. Respect the timing and separation of the user's requests: what they asked for separately, at different times, stays separate goals; what they asked for together is one request. Never turn them into a broader request the user didn't make, even if their intent seems predictable.

A sub-goal exists only because of a goal: it is a part of the goal, or a reaction to it. Give it the turn where it was said and serves: the turn of the goal it belongs to. A sub-goal is never a goal of its own. Its kinds:
- a part: another device asked for in the goal's own turn (a state or sequence sub-goal at that turn, above), or a repair: a later turn asking to put right a device the goal didn't name, which Assist changed (a state or sequence sub-goal on that device);
- step: a later turn the user spent only because the goal wasn't met yet: repeating, rephrasing or correcting the request, answering the assistant's question about it, checking whether it was done (while it wasn't), asking what the assistant can do or did, telling it how to do the job, finishing what it left half done on the goal's own devices. A step has nothing of its own: serves is the turn of the goal, or of the earlier step or repair it continues;
- conduct: something the household objected to in what the assistant said or did in one of its turns (it was careless or wrong, too wordy, its tone, "next time do X"), even after it did what they asked; or a check on a result the assistant had already reached, because its reply left them unsure. about: the turn whose reply they objected to (or that left them unsure), from the goal's turn up to the turn before; quote: their own words, copied exactly from the turn where they said them (leave words out only with …; never paraphrase); improvement: leave it out, unless an applied improvement (list_improvements, when you have it) made Assist do it. One conduct goal for each thing they want done differently: two in one sentence are two conduct goals, and one said again in a later turn is a new conduct goal at that turn. One turn can hold a step and a conduct goal;
- wrap-up: filing the trouble for improvement, or giving up on it;
- thanks: thanking the assistant, or telling it unasked that all of it worked ("thanks", "it worked"), with no other fields.
A conduct goal, a wrap-up and a thanks serve the turn where their goal itself starts. A fact asked together with a device request is a sub-goal at that turn; a fact asked later is a goal of its own, or, when it only checks on a goal, a step (while the goal wasn't met) or a conduct goal (once it was).

Describe what the user wanted, not what the assistant did. Use only entity IDs the tools show you. Include a device in a goal only if the user wanted something from it.

Every turn needs at least one goal or sub-goal. Turns that are neither requests nor reactions, such as microphone tests or speech misfires, go in not_requests.

If you cannot even tell which devices the user meant, write one short question for the user in question_for_user instead of guessing. Otherwise leave it empty.

Answer only by calling submit_goals, with sub-goals in the same goals list. It checks your answer against the recording, including that every state is one the device has (device_details shows a device's modes and options). If it reports problems, fix them and submit again. When it accepts your answer, finish with one short sentence."""

# Added when the user sent the conversation in with a clear sign it needs a closer look (flow.deliberate): a note,
# filing it from Assist, or sending it in again after an earlier round looked at it.
FILED = """

The user filed this conversation for improvement themselves (filed_by_user in your input), so something here didn't go the way they wanted. Its note, when there is one, is the conversation's last turn: give it the sub-goals it holds, serving the goal it's about: a conduct goal for each objection it makes (quote the note's own words; about: the turn whose reply it objects to, or "all" when it's about the assistant in general), or a step when it only asks again for what a goal didn't get. Still describe the goals as the conversation shows them."""

# Added when the household asked for a revision of an improvement proposed for this conversation (DETAILS P12.8.1).
REVISION = """

The household asked for a revision of an improvement proposed for this conversation. Each revision note is a later turn of the conversation, after any note it was sent in with (revision_of names the improvement, what it changes and the turns of the goals it was for): give it the sub-goals it holds, serving one of those goals: a step when it asks again for what that goal didn't get, or says how to reach it while it wasn't reached; a conduct goal for each objection it makes to what the assistant said or did (quote the note's own words; about: the turn whose reply it objects to, or "all"). A note only about the improvement itself (what else it changes, or how it reaches a goal that was reached already), not about anything the assistant said or did, holds no goal: list it in about_the_fix, with its turn and in a few words what it asks of the improvement; the proposer reads it in full. Nothing else."""

# Added to the instructions when improvements exist (waiting for review, or applied): a goal one of them already fixes
# stops here instead of flowing on to the proposer.
DUPLICATES = """

Some of what went wrong here may already have a fix: an improvement waiting for the household's review, or one already applied. Your input's improvements_on_these_devices lists those that touch the devices in this session; list_improvements lists them all, and read_improvement shows one in full (its changes, and the conversations it was tested on). Once your goals are accepted, check each goal the user had to repeat, correct or clarify, or that went unmet: if an improvement already fixes what went wrong there, call mark_duplicate with the goal's turn, that improvement and why, so it isn't fixed twice. One applied before this session took place didn't prevent what happened here, so it doesn't count. Mark a goal only when you're sure; otherwise leave it.

An applied improvement can also be the cause of what the household objected to: when one applied before this session makes Assist do what a conduct goal is about (a household line telling it how to word something, a script it ran), name it in that conduct goal's improvement."""

OUTCOMES = {"announce": "a message spoken on devices"}  # What an action goal can want: the outcome, not a service.
STRUCTURE = probatio.Schema({
    probatio.Required("goals"): [{
        probatio.Required("turn"): int,
        probatio.Required("summary"): str,
        probatio.Required("kind"): probatio.In(["state", "sequence", "fact", "action", "ask", "step", "conduct",
                                                "wrap-up", "thanks"]),
        probatio.Optional("entity_id"): str,
        probatio.Optional("entity_ids"): [str],
        probatio.Optional("state"): str,
        probatio.Optional("states"): [str],
        probatio.Optional("brightness_pct"): int,
        probatio.Optional("fact"): str,
        probatio.Optional("outcome"): probatio.In(list(OUTCOMES)),  # Action: what it does.
        probatio.Optional("serves"): int,
        probatio.Optional("about"): probatio.Any(int, probatio.In(["all"])),  # Conduct: the Assist turn objected to.
        probatio.Optional("quote"): str,  # Conduct: the household's own words; fact: the form they asked for.
        probatio.Optional("improvement"): probatio.Any(str, None),  # Conduct: an applied improvement that made it.
    }],
    probatio.Required("not_requests"): [{probatio.Required("turn"): int, probatio.Required("what"): str}],
    # Revision notes only about the improvement itself: they add no goal (DETAILS P12.8.1).
    probatio.Optional("about_the_fix"): probatio.Any(None, [{probatio.Required("turn"): int,
                                                              probatio.Required("what"): str}]),
    probatio.Required("question_for_user"): str,
})
UNKNOWN_STATES = ("unknown", "unavailable")  # A start nobody can compare with (a sleeping car's entities).
REACTIONS = ("step", "conduct", "wrap-up", "thanks")  # Sub-goals that react to their goal, rather than being part of it.
CUT = re.compile(r"…|\.\.\.")  # Where a quote leaves words out.


def clean_goal(goal):
    """A goal with only the fields its kind uses (some providers send every optional field, as null)."""
    keep = {"state": ("entity_id", "state", "brightness_pct"), "sequence": ("entity_id", "states"),
            "fact": ("fact", "entity_ids", "quote"), "action": ("outcome", "entity_ids"),
            "ask": ("entity_ids",), "step": (),
            "conduct": ("about", "quote", "improvement"), "wrap-up": (), "thanks": ()}[goal["kind"]] + ("serves",)
    return {"turn": goal["turn"], "summary": goal.get("summary", ""), "kind": goal["kind"],
            **{k: goal[k] for k in keep if goal.get(k) is not None}}


def top_level(goals):
    return [g for g in goals if g.get("serves") is None]


def root_turn(goals, turn):
    """The turn of the root goal a goal at this turn hangs from (P0.3: a sub-goal may serve another sub-goal); the
    turn itself when a root starts there, or nothing there serves anything; None for a chain that loops."""
    seen = set()
    while turn not in seen:
        seen.add(turn)
        here = [g for g in goals if g.get("turn") == turn]
        if not here or any(g.get("serves") is None for g in here):
            return turn
        turn = here[0]["serves"]
    return None


def destination(goals, step):
    """What a step wants met: its parent, the goals at the turn it serves that aren't reactions (the goal and its
    parts, or a repair), found back through the steps it continues; empty when there are none."""
    seen, turn = set(), step.get("serves")
    while turn is not None and turn not in seen:
        seen.add(turn)
        there = [g for g in goals if g.get("turn") == turn]
        if parent := [g for g in there if g.get("kind") not in REACTIONS]:
            return parent
        turn = next((g.get("serves") for g in there if g.get("kind") == "step"), None)
    return []


# Services that speak or play an announcement; a legacy text-to-speech engine's own say service (tts.<engine>_say) too.
SPEAKING = ("tts.speak", "assist_satellite.announce", "assist_satellite.start_conversation", "media_player.play_media")


def state_at(view, entity, turn):
    """An entity's state as a turn began, by that turn's snapshot of the home (view.sources; else the first turn's)."""
    homes = getattr(view, "sources", None) or [view.source]
    return ((homes[min(max(turn, 1), len(homes)) - 1].get("states") or {}).get(entity) or {}).get("state")


def speaking(service):
    """Whether a service speaks or plays an announcement (SPEAKING, or a legacy engine's tts.<engine>_say)."""
    return service in SPEAKING or service.startswith("tts.") and service.endswith("_say")


def spoken(calls):
    """What a turn's calls spoke or played, and on which devices: [{message, media, targets}] (media: the audio an
    announcement played, such as a chime), from a broadcast's result (the satellites it was sent to) and the speaking
    calls a replay's sandbox logged (service, the devices that spoke, the message or media)."""
    out = []
    for call in calls:
        if speaking(call.get("service") or ""):  # Logged by a replay's sandbox, wherever it was called from.
            out.append({"message": call.get("message") or "", "media": call.get("media"),
                        "targets": set(call.get("targets") or [])})
        elif (call.get("tool") or "").endswith("HassBroadcast"):
            data = (call.get("result") or {}).get("data") if isinstance(call.get("result"), dict) else None
            inner = data.get("data") if isinstance(data, dict) else None
            out.append({"message": str((call.get("args") or {}).get("message") or ""), "media": None,
                        "targets": {t.get("id") for t in (inner or {}).get("success") or [] if isinstance(t, dict)}})
    return out


def carries(goal, announcement):
    """Whether what an announcement said or played ({message, media}) gets across what an action asked for: always,
    for now. The one place a check of its content would go (DETAILS P9.10.5)."""
    return True


def announced(view, goal, turns):
    """Whether an announce action was done over these turns' calls (view.calls): an announcement (words that aren't
    blank, or audio such as a chime) that carries it, on every device it's for, by any call (DETAILS P9.7.6); a device
    is reached when any of its entities spoke (a voice satellite and its speaker are one device), one unavailable as
    its turn began (view.sources: each turn's home) speaking nowhere, though a broadcast's result names it. None when
    that can't be told: no calls kept, or a script ran that a recording doesn't see into (view.scripts_seen: a
    replay's sandbox logs what scripts speak)."""
    if getattr(view, "calls", None) is None:
        return None
    entities = view.source.get("entities") or {}
    device = lambda e: (entities.get(e) or {}).get("device_id") or e  # noqa: E731
    calls = [c for t in turns for c in view.calls.get(t, [])]
    reached = {device(e) for t in turns for s in spoken(view.calls.get(t, []))
               if (s["message"].strip() or s["media"]) and carries(goal, s)
               for e in s["targets"] if state_at(view, e, t) != "unavailable"}
    if {device(e) for e in goal.get("entity_ids") or []} <= reached:
        return True
    if not getattr(view, "scripts_seen", False) and ran_scripts(calls):
        return None
    return False


def ran_scripts(calls):
    """The scripts these calls ran, each by its tool's name (script__name), in order, once each."""
    return list(dict.fromkeys(c["tool"] for c in calls if re.search(r"(^|__)script__", c.get("tool") or "")))


def spoken_before(view, goals, action, turn):
    """Whether an action was done by the turns of its request before this turn (announced)."""
    root = root_turn(goals, action["turn"])
    return announced(view, action, [t for t in range(action["turn"], turn) if root_turn(goals, t) == root]) is True


def question_run(goals, root):
    """The turns of a question's run: its own, then each step whose destination is that question alone (asked again,
    or asked again after that), in order; a step continuing a repair under it isn't one (DETAILS P9.10.4)."""
    return [root["turn"], *sorted(g["turn"] for g in goals if g.get("kind") == "step" and isinstance(g.get("turn"), int)
                                  and (wants := destination(goals, g)) and len(wants) == 1 and wants[0] is root)]


def extra_turns(turn_count, goals, not_requests, question=None):
    """Turns that started no goal (sub-goals don't count) and were real requests: the interventions, a step's, a
    conduct goal's or a wrap-up's among them. A turn that only thanked isn't one.

    While a question to the user is open, such turns are pending the answer instead, so this returns none.
    """
    if question:
        return []
    thanks = {g["turn"] for g in goals if g["kind"] == "thanks"} - {g["turn"] for g in goals if g["kind"] != "thanks"}
    used = {g["turn"] for g in top_level(goals)} | thanks | {n["turn"] for n in not_requests}
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
    if goal["kind"] in ("fact", "ask", "action") or not goal.get("entity_id"):
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


def home_couldnt(view, goal, goals):
    """Whether a root goal's device was unavailable in every turn its tree took (its own, as the user asked for it,
    and each of its sub-goals'), so the home couldn't have reached it whatever Assist did then: context for an unmet
    root goal, not a state of its own; for an action, every device it's for, none of whose entities with a state was
    available (a satellite and its speaker are one device). An unknown device is a sleeping one a command wakes
    (devices.ASLEEP), so it isn't one."""
    turn = goal.get("turn")
    entities = [goal["entity_id"]] if goal.get("entity_id") else goal.get("entity_ids") or []
    took = {turn} | {g["turn"] for g in goals if g.get("serves") is not None and root_turn(goals, g["serves"]) == turn}
    if not entities or not isinstance(turn, int) or not 1 <= turn <= len(view.sources):
        return False
    turns = [t for t in took if isinstance(t, int) and 1 <= t <= len(view.sources)]
    if goal.get("kind") != "action":
        return all(state_at(view, entities[0], t) == "unavailable" for t in turns)
    known = view.source.get("entities") or {}
    device = lambda e: (known.get(e) or {}).get("device_id") or e  # noqa: E731

    def down(asked, t):  # Its device's entities with a state then (a disabled one has none), else the one asked for.
        held = [e for e in known if device(e) == device(asked) and state_at(view, e, t) is not None] or [asked]
        return all(state_at(view, e, t) == "unavailable" for e in held)
    return all(down(asked, t) for asked in entities for t in turns)


def side_effects_left(view, goals):
    """Devices Assist changed itself (not a device's own follow-on) that no goal asked for (or left open for the
    user) and that did not end as they started, with the turn of Assist's last change to each. Asked for: by a root
    goal, or a sub-goal asked at once with it (one world state, P0.3), or, for changes in its request's turns from its
    own, an action's (a speaker left playing or idle by what it spoke); a later sub-goal that repairs a device doesn't
    make it wanted, nor does a question about it. A device whose start was unknown or unavailable (a sleeping car's)
    can't be told to have changed, so it isn't one."""
    wanted = {g.get("entity_id") for g in goals
              if g.get("serves") is None or root_turn(goals, g["serves"]) == g.get("turn")} | {
        e for g in goals if g.get("kind") == "ask" for e in g.get("entity_ids") or []}
    spoke = {}  # A device an action is for: the turns where what speaking leaves on it is wanted.
    for g in goals:
        if g.get("kind") == "action":
            turns = {t for t in {h.get("turn") for h in goals} if isinstance(t, int) and t >= g["turn"]
                     and root_turn(goals, t) == root_turn(goals, g["turn"])}
            for e in g.get("entity_ids") or []:
                spoke.setdefault(e, set()).update(turns)
    left = []
    for entity, changes in view.changes.items():
        own = [c for c in changes if c["by"] == "assistant"]
        if not own or entity in wanted or all(c["during_turn"] in spoke.get(entity, ()) for c in own):
            continue
        start = (view.source.get("states", {}).get(entity) or {}).get("state", changes[0]["old"])
        end = changes[-1]["new"]
        if start not in (None, *UNKNOWN_STATES) and end != start:
            left.append({"entity_id": entity, "start": start, "end": end, "turn": own[-1]["during_turn"]})
    return left


def to_analysis(recording_id, output, view, notes=()):
    """The model's goals, plus what code derives from the recording. Every goal, root or sub-goal (P0.3), is reached
    (met), not (unmet) or None (indeterminate) as measures.judge finds it in the conversation as it went; an unmet
    root goal whose device was unavailable as it was asked for is marked home_couldnt. notes: the turns after those
    said, in order (records.AnalysisResult.notes)."""
    from .measures import goals_of, judge
    goals = [clean_goal(g) for g in output.get("goals", [])]
    count = len(view.digest["turns"])
    not_requests = list(output.get("not_requests", []))
    question = (output.get("question_for_user") or "").strip()
    analysis = {"recording_id": recording_id, "goals": goals, "not_requests": not_requests,
                "extra_turns": extra_turns(count, goals, not_requests, question),
                "pending_turns": extra_turns(count, goals, not_requests) if question else [],
                "side_effects_left": side_effects_left(view, goals), "question_for_user": question or None,
                "turns": count, **({"notes": list(notes)} if notes else {}),
                **({"about_the_fix": list({n["turn"]: n for n in output["about_the_fix"]}.values())}
                   if output.get("about_the_fix") else {})}
    states = judge(goals_of(analysis), view, range(1, count + 1))["goals"]
    for i, goal in enumerate(goals):
        goal["reached"] = {"met": True, "unmet": False}.get(states[f"{goal['turn']}:{i}"])
        if goal["reached"] is False and goal.get("serves") is None and home_couldnt(view, goal, goals):
            goal["home_couldnt"] = True
    return analysis


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


def quoted(quote, said):
    """Whether a quote is the household's own words: each part of it (where … or ... leaves words out) is found in
    what they said, in order, word for word, case, punctuation and the kind of apostrophe aside."""
    def words(text):
        return re.findall(r"\w+(?:'\w+)*", text.casefold().replace("\u2019", "'").replace("\u2018", "'"))
    heard, at = words(said or ""), 0
    parts = [part for part in map(words, CUT.split(quote or "")) if part]
    for part in parts:
        at = next((i + len(part) for i in range(at, len(heard) - len(part) + 1) if heard[i:i + len(part)] == part),
                  None)
        if at is None:
            return False
    return bool(parts)


def problems(view, answer, note=None, existing=(), revisions=()):
    """What is wrong with an answer, checked against the recording; empty means valid. note: the note the user sent
    the conversation in with (a turn after those said, which needs a goal); revisions: each revision note after it,
    in order ({note, roots: the turns of the goals its improvement was for}), whose goals are a step or a conduct goal
    serving one of those; existing: the improvements list_improvements shows."""
    try:
        STRUCTURE(answer)
    except probatio.Invalid as err:
        return [f"The answer does not match the format: {err}"]
    said, known = len(view.digest["turns"]), set(view._entities())
    # The notes are the conversation's last turns (P0.3): the one it was sent in with, then each revision note.
    notes = ([note] if (note or "").strip() else []) + [r["note"] for r in revisions]
    note_turns = {said + n: words for n, words in enumerate(notes, 1)}
    revised = {said + len(notes) - len(revisions) + n: r["roots"] for n, r in enumerate(revisions, 1)}
    turn_count = said + len(notes)
    found = []
    top_turns = {g["turn"] for g in answer["goals"] if g.get("serves") is None}
    goal_turns = {g["turn"] for g in answer["goals"]}
    quiet = {n["turn"] for n in answer["not_requests"]}
    fixing = {n["turn"] for n in answer.get("about_the_fix") or []}  # Revision notes about the improvement alone.
    # Every turn maps to at least one goal; a root goal is one world state at one time; a note is a sub-goal (P0.3).
    left = [t for t in range(1, turn_count + 1)
            if t not in goal_turns and t not in fixing and (t in note_turns or t not in quiet)]
    for turn in sorted(fixing - set(revised)):
        found.append(f"Turn {turn} isn't a revision note: about_the_fix is for a revision note only about the "
                     "improvement itself.")
    for turn in sorted(fixing & set(revised) & goal_turns):
        found.append(f"Turn {turn} is in about_the_fix but has goals: a note only about the improvement holds none.")
    if left:
        found.append(f"Turns {', '.join(map(str, left))} have no goal: give every turn at least one goal or sub-goal "
                     "(a thanks, a correction or the note is a sub-goal of the goal it's about)"
                     + ("; a revision note only about the improvement itself goes in about_the_fix instead."
                        if set(left) & set(revised) else "."))
    for turn in sorted(top_turns):
        if sum(1 for g in answer["goals"] if g["turn"] == turn and g.get("serves") is None) > 1:
            found.append(f"Turn {turn} has more than one goal: what was asked for at once is one goal; give each "
                         "other device as a sub-goal with the same turn that serves it.")
    for turn in sorted(top_turns & set(note_turns)):
        found.append(f"Turn {turn} is a note: give it a sub-goal serving the goal it's about, not a goal.")
    for n, goal in enumerate(answer["goals"], 1):
        where = f"Goal {n} (turn {goal['turn']})"
        if not 1 <= goal["turn"] <= turn_count:
            found.append(f"{where}: there is no turn {goal['turn']}; turns are 1 to {turn_count}.")
        kind, entity, serves = goal["kind"], goal.get("entity_id"), goal.get("serves")
        if goal["turn"] in revised:  # Under a root its improvement was for, or any where none of those is one now.
            roots = [r for r in revised[goal["turn"]] if r in top_turns] or sorted(top_turns)
            if kind not in ("step", "conduct") or serves is None or root_turn(answer["goals"], serves) not in roots:
                found.append(f"{where}: a revision note holds a step or a conduct goal, serving a goal its "
                             f"improvement was for: turn {' or '.join(map(str, roots))}.")
        if kind in REACTIONS:
            if serves is None:
                found.append(f"{where}: a {kind} is a sub-goal: give serves, the turn of the goal it's for.")
            elif kind != "step" and serves not in top_turns:
                found.append(f"{where}: a {kind} serves the turn where its goal itself starts; goals start at turns "
                             f"{sorted(top_turns) or 'none'}.")
            if kind != "thanks" and goal["turn"] in quiet:
                found.append(f"{where}: turn {goal['turn']} has a {kind}; take it out of not_requests.")
            if kind == "conduct":
                found += _conduct_problems(view, answer, goal, where, note_turns, existing)
            if kind == "step" and serves is not None and (parent := destination(answer["goals"], goal)) and all(
                    g.get("kind") in ("state", "sequence") and g.get("entity_id") in known
                    and reached(view, g, goal["turn"]) or g.get("kind") == "action"
                    and spoken_before(view, answer["goals"], g, goal["turn"]) for g in parent):
                found.append(f"{where}: what it serves was already done by turn {goal['turn']}, so it isn't a step "
                             "towards it: a check because a reply left them unsure, or an objection, is a conduct goal "
                             "about that reply, and a new request a goal of its own.")
        elif kind == "action":
            if not goal.get("outcome"):
                found.append(f"{where}: an action goal needs outcome: {', '.join(OUTCOMES)}.")
            if serves is not None and serves != goal["turn"]:
                found.append(f"{where}: an action asked at a later turn is a goal of its own (no serves), or a step "
                             "when it asks again.")
            ids = goal.get("entity_ids") or []
            if not ids:
                found.append(f"{where}: an action goal needs entity_ids: the devices it's for.")
            if unknown := [e for e in ids if e not in known]:
                found.append(f"{where}: no device {', '.join(map(repr, unknown))} in this household; "
                             "use entity IDs the tools show.")
        elif kind == "fact":
            if not goal.get("fact"):
                found.append(f"{where}: a fact goal needs fact: what the answer needs.")
            if unknown := [e for e in goal.get("entity_ids") or [] if e not in known]:
                found.append(f"{where}: no device {', '.join(map(repr, unknown))} in this household; "
                             "use entity IDs the tools show.")
            words = next((t["user_says"] for t in view.digest["turns"] if t["turn"] == goal["turn"]), "")
            if (goal.get("quote") or "").strip() and not quoted(goal["quote"], words):
                found.append(f"{where}: quote must be the form they asked for in their own words from turn "
                             f"{goal['turn']}, copied exactly; leave it out when they asked for none.")
            if serves is not None and serves != goal["turn"]:
                found.append(f"{where}: a fact asked at a later turn is a goal of its own (no serves), or a step when "
                             "it only checks on the goal.")
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
        if kind in ("state", "sequence") and serves is not None and serves != goal["turn"]:
            wants = destination(answer["goals"], goal)  # Nor does a question about it, or speaking on it.
            if entity in {e for g in wants for e in [g.get("entity_id"), *(g.get("entity_ids") or [])]
                          if g.get("kind") not in ("fact", "action")}:
                found.append(f"{where}: its goal at turn {serves} already wants {entity}: a later turn that asks for it "
                             "again is a step (kind step, no device), not a sub-goal of its own.")
            elif entity in known and reached(view, goal, goal["turn"]):
                found.append(f"{where}: {entity} was already that way by turn {goal['turn']}, so nothing there was put "
                             "right: a check because a reply left them unsure is a conduct goal about that reply, and a "
                             "new request a goal of its own.")
        if serves is not None and (serves not in goal_turns or serves > goal["turn"]
                                   or root_turn(answer["goals"], serves) not in top_turns):
            found.append(f"{where}: serves must be the turn of an earlier goal or sub-goal that hangs from a goal; "
                         f"goals and sub-goals are at turns {sorted(goal_turns) or 'none'}.")
    for item in answer["not_requests"]:
        if not 1 <= item["turn"] <= turn_count:
            found.append(f"Not-request at turn {item['turn']}: there is no such turn.")
    return found


def _conduct_problems(view, answer, goal, where, note_turns, existing):
    """A conduct goal, checked: the Assist turn it's about, in its goal's tree and before it was said ("all" only in
    a note); its quote, the household's own words where they said them; an applied improvement it names."""
    from .exploration import applied_too_early
    found, turn, about = [], goal["turn"], goal.get("about")
    said = note_turns[turn] if turn in note_turns else next(
        (t["user_says"] for t in view.digest["turns"] if t["turn"] == turn), "")
    if not (goal.get("quote") or "").strip():
        found.append(f"{where}: a conduct goal needs quote: the household's own words from turn {turn}.")
    elif not quoted(goal["quote"], said):
        found.append(f"{where}: quote must be the household's own words from turn {turn}, copied exactly: copy their "
                     "words, and mark words you leave out with …; never paraphrase.")
    root = root_turn(answer["goals"], goal["serves"]) if goal.get("serves") is not None else None
    tree = {root} | {g["turn"] for g in answer["goals"]
                     if g.get("serves") is not None and root_turn(answer["goals"], g["serves"]) == root}
    if about is None:
        found.append(f"{where}: a conduct goal needs about: the turn whose reply the household objected to.")
    elif about == "all":
        if turn not in note_turns:
            found.append(f"{where}: about all is for a note, about the assistant in general; give the turn whose "
                         "reply they objected to.")
    elif root is not None and not (root <= about < turn and about in tree and about not in note_turns):
        found.append(f"{where}: about must be a turn of its goal's said to the assistant, from the goal's turn ({root}) "
                     f"to the turn before the objection: its turns are "
                     f"{sorted(t for t in tree if t < turn and t not in note_turns)}.")
    if name := goal.get("improvement"):
        applied = {i["improvement"]: i for i in existing or () if i.get("status") == "applied"}
        if name not in applied:
            found.append(f"{where}: no applied improvement {name!r}; list_improvements shows those applied: "
                         f"{', '.join(applied) or 'none'}.")
        elif not applied_too_early(applied[name], view.digest["started"]):
            found.append(f"{where}: {applied[name]['title']!r} was applied after this session, so it can't be what "
                         "Assist did here; leave improvement out.")
    return found


class SubmitGoals(RoleTool):
    """The analyzer's only way to answer. Keeps every submission so the best one can be used."""
    name = "submit_goals"
    description = ("Submit your answer: the user's goals (sub-goals in the same list, with serves; a conduct goal "
                   "with about and quote), the turns that were not requests, any revision note only about the "
                   "improvement itself (about_the_fix), and any question for the user. The answer is checked against "
                   "the recording; if problems are reported, fix them and submit again.")
    parameters = STRUCTURE

    def __init__(self, view, then="Finish with one short sentence.", note=None, existing=(), revisions=()):
        super().__init__(view)
        self.submissions, self.then, self.note, self.existing = [], then, note, list(existing)
        self.revisions = list(revisions)

    def run(self, **answer):
        found = problems(self.view, answer, self.note, self.existing, self.revisions)
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


def valid_part(answer, found):
    """What moves on from the best of answers none of which was accepted (DESIGN.md: an impossible answer never moves
    on): its goals the checks found nothing wrong with. found: the checks' problems ("Goal n (turn t): ..."). One not
    in the format moves on with no goals."""
    if any(p.startswith("The answer does not match the format") for p in found):
        return {"goals": [], "not_requests": [], "question_for_user": ""}
    goals = answer.get("goals") or []
    wrong = {n for n, _ in enumerate(goals, 1) if any(p.startswith(f"Goal {n} (") for p in found)}
    tops = {g.get("turn") for n, g in enumerate(goals, 1) if n not in wrong and g.get("serves") is None}
    reached, linked = set(tops), {}  # The turns a kept goal is at; {n: a sub-goal hanging from one, through kept ones}.
    while more := {n: g for n, g in enumerate(goals, 1) if n not in linked and g.get("serves") is not None
                   and g["serves"] in reached and g["serves"] <= g.get("turn", 0)}:
        linked.update(more)
        reached |= {g.get("turn") for g in more.values()}
    kept, gone = [], []
    for n, goal in enumerate(goals, 1):
        if n in linked:
            # A sub-goal of a goal kept (at any depth): the turn served that goal (an extra turn, its trouble on that
            # goal) whatever its own fields; thrown out, only what it served stays (no device the household lacks
            # moves on).
            kept.append(goal if n not in wrong else {k: goal[k] for k in ("turn", "summary", "kind", "serves")
                                                     if k in goal})
        elif n in wrong or goal.get("serves") is not None:  # Thrown out, or serving a goal thrown out: it goes too.
            gone.append(goal)
        else:
            kept.append(goal)
    # A turn whose goal was thrown out isn't an extra turn (that would invent trouble): it's set aside, though a sub-goal
    # of a kept goal there still links it to that goal. One whose sub-goal of a goal thrown out went with it, set aside
    # unless something there was kept.
    left = sorted(({g.get("turn") for g in gone if g.get("serves") is None} - tops
                   | {g.get("turn") for g in gone if g.get("serves") is not None} - {g.get("turn") for g in kept})
                  - {r.get("turn") for r in answer.get("not_requests") or []})
    return {**answer, "goals": kept, "not_requests": [*(answer.get("not_requests") or []), *(
        {"turn": turn, "what": "A goal here couldn't be checked against the household."} for turn in left
        if isinstance(turn, int))]}


class MarkDuplicate(RoleTool):
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
