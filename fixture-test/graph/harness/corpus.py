"""The kinds of conversation a household can have, each a synthetic recording plus how the stand-ins read it.

The relay kinds (friction, no fix, same cause, smooth, fine, unclear, broken, unreadable) are the fixture relay's
recording (recordings.prepared_recording), under their own first request. The other kinds are recordings of their
own, built here in the same format (talk), with the household's other devices. Every recording snapshots the whole
household (HOUSE) at each turn, so the household looks the same from every conversation. Each conversation has its
own id and its own first request, so the stand-ins tell conversations apart by what was said first.

A kind says what was said and what Assist did (talk), how many turns it keeps, whether Triage finds it smooth, what
the analyzer answers (analysis, in submit_goals' schema), the proposer's fix for its trouble (fix: the script it
proposes), and what Assist does when that fix is offered in a replay (fixed: per turn, call the fix's script, or ask
something first; fix_works false: as recorded)."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import json

from recordings import PROFILE, REPLAY_ENTITIES, SCRIPT, TARGET, prepared_recording

FIRST = "Toggle the fixture relay."  # The fixture recording's first request; each conversation gets its own.

LAMP, FAN, HEATER = "light.living_room_lamp", "fan.living_room_fan", "switch.heater"
GARAGE, LEFT, RIGHT = "cover.garage_door", "cover.left_garage_door", "cover.right_garage_door"
PORCH, FRIDGE, SPEAKER, SPEECH = ("light.porch_light", "switch.fridge_plug", "media_player.kitchen",
                                  "tts.speech")
LOUNGE, CHIME = "media_player.living_room_speaker", "button.chime"
AREAS = {"fixture_area": "Fixture room", "living_room": "Living room", "garage": "Garage", "porch": "Porch",
         "kitchen": "Kitchen"}
# The household's devices beside the fixture relay: {entity_id: (state, friendly name, area)}. A kind may start one
# in another state (Kind.start).
HOUSE = {
    LAMP: ("off", "Lamp", "living_room"),
    FAN: ("off", "Fan", "living_room"),
    HEATER: ("on", "Heater", "living_room"),
    GARAGE: ("closed", "Garage door", "garage"),
    LEFT: ("closed", "Left garage door", "garage"),
    RIGHT: ("closed", "Right garage door", "garage"),
    PORCH: ("off", "Porch light", "porch"),
    FRIDGE: ("on", "Fridge plug", "kitchen"),
    SPEAKER: ("idle", "Kitchen speaker", "kitchen"),
    LOUNGE: ("idle", "Living room speaker", "living_room"),
    SPEECH: ("unknown", "Speech", "kitchen"),  # A text-to-speech engine, which speaks on a media player.
    CHIME: ("2026-01-01T07:00:00+00:00", "Chime", "kitchen"),  # A button, as it was last pressed.
}
# What each kind of device can do, as its state's attributes say (a fan turns on and off, a cover opens and closes, a
# speaker plays and announces), so a replay's stand-ins take the same commands.
ABLE = {"light": {"supported_color_modes": ["onoff"]}, "fan": {"supported_features": 48},
        "cover": {"supported_features": 3}, "media_player": {"supported_features": 512 | 1048576}}
# The household's devices: {entity_id: (state, friendly name)}, exposed to Assist.
ENTITIES = {**REPLAY_ENTITIES, **{e: (state, name) for e, (state, name, _) in HOUSE.items()}}

FINE_GOALS = {"goals": [
    {"turn": 1, "summary": "Relay off", "kind": "state", "entity_id": TARGET, "state": "off"},
    {"turn": 2, "summary": "Asked about the press script", "kind": "fact", "fact": "the press script"}],
    "not_requests": [], "question_for_user": ""}

PRESS = {"kind": "sequence", "entity_id": TARGET, "states": ["off", "on"]}
# The friction conversation (the fixture's first three turns): one root goal (a relay press), and each later turn a
# step towards it, while it wasn't met: telling Assist how to do the job, then finishing what it left half done.
FRICTION_GOALS = {"goals": [
    {"turn": 1, "summary": "One relay press", **PRESS, "_went": "met"},
    {"turn": 2, "summary": "Told it to use the press script", "kind": "step", "serves": 1},
    {"turn": 3, "summary": "Had it finish the press: relay back on", "kind": "step", "serves": 1}],
    "not_requests": [], "question_for_user": ""}


def goal(turn, summary, entity=None, state=None, serves=None, fact=None, went=None, thanks=False, step=False,
         wrap_up=False, quote=None, about=None, settings=None):
    """One goal in submit_goals' schema: a device state (with the settings asked for, by their actions' field names),
    a fact, a thanks, a step (a later turn asking again for what the goal it serves wanted), a wrap-up (filing it), or,
    with quote, what the household objected to in Assist's turn about; serves makes it a sub-goal. went: whether
    Assist reached it, by how the kind was made (only the stand-in's "says each goal went the other way" reads it)."""
    out = {"turn": turn, "summary": summary}
    out.update({"kind": "thanks"} if thanks else {"kind": "step"} if step else {"kind": "wrap-up"} if wrap_up else
               {"kind": "conduct", "about": about, "quote": quote} if quote else {"kind": "fact", "fact": fact} if fact
               else {"kind": "state", "entity_id": entity, "state": state})
    if serves is not None:
        out["serves"] = serves
    if settings:
        out["attributes"] = settings
    if went:
        out["_went"] = went
    return out


def goals(*listed):
    return {"goals": list(listed), "not_requests": [], "question_for_user": ""}


@dataclass(frozen=True)
class Say:
    """One turn: what the user said, what Assist changed ((entity, new state), each its own call of HassTurnOn or
    HassTurnOff by the device's name), and its reply. wait: how long after the turn before it (a later time).
    broadcast: a message Assist broadcast (HassBroadcast), which reached no device: the household has no satellite.
    local: Home Assistant answered it itself, not the AI (nothing prepared for a model): with its built-in commands
    (True), or as the agent named (an automation's sentence trigger). failed: the error each of its calls failed with
    at home, changing nothing (the turn keeps its call log)."""
    text: str
    calls: tuple = ()
    reply: str = "Done."
    wait: timedelta = timedelta(seconds=20)
    broadcast: str | None = None
    local: bool | str = False
    failed: str | None = None


@dataclass(frozen=True)
class Fix:
    """The proposer's fix for a kind's trouble: a new script Assist can run. steps: (service, entity_id[, data])."""
    title: str
    cause: str
    script: str
    steps: tuple
    text: str
    expected: str
    breaks: tuple = ()  # Steps of a fix that also undoes the goal that went fine (PARTLY's trade).
    fields: tuple = ()  # Its text inputs, ({key, name, description}, ...), which its steps' data use as {{ key }}.
    asks: dict = field(default_factory=dict)  # What Assist gives those inputs when it runs it.


@dataclass(frozen=True)
class Kind:
    name: str
    said: str  # Its first request, before the conversation's own number is added.
    turns: int | None = None  # How many of the recording's turns it keeps (all when None).
    smooth: bool = False  # Triage is sure nothing needs improving.
    analysis: dict | None = None  # What the analyzer answers; None: every analysis of it fails.
    fix: Fix | None = None  # The proposer's fix for its trouble, if it has one.
    fix_works: bool = False  # Replayed with that fix offered, Assist acts as fixed says (else as recorded).
    fixed: dict = field(default_factory=dict)  # {turn: "fix" (runs the fix's script) or "ask" (asks first)}
    malformed: bool = False  # Its recording is broken: no stage can read it.
    talk: tuple | None = None  # Its own recording's turns; None: the fixture relay's.
    start: dict = field(default_factory=dict)  # Devices that start in another state than HOUSE's.
    able: dict = field(default_factory=dict)  # {entity: what it can do beyond its kind's ABLE}: a lamp that dims.
    note: str | None = None  # The note it was sent in with.
    note_about: int = 1  # The turn of the root goal a note sent with it is about.
    thanks: int | None = None  # The turn of its "thanks".
    correction: int | None = None  # The turn of its correction.
    covered_by: str | None = None  # The kind whose first conversation's fix also removes its trouble.
    side_effects: tuple = ()  # The devices it leaves changed that no goal asked for.
    put_back: tuple = ()  # What someone else puts back a while after its talk: (entity, state, context of the change).
    unmarked: tuple = ()  # Devices reporting Assist's changes without its request on them: only its results name them.
    shows: dict = field(default_factory=dict)  # {quote: the replies that show what it objects to}; else any reply but
    #   "Done." shows it.

    def fix_for(self, number):
        """The fix for this kind's conversation `number`: each friction conversation has a cause of its own, so
        each gets a fix of its own; a covered kind's is the covering kind's first conversation's."""
        if self.covered_by:
            return KINDS[self.covered_by].fix_for(1)
        if self.fix is None or number == 1:
            return self.fix
        return replace(self.fix, title=f"{self.fix.title}, conversation {number}",
                       cause=f"{self.fix.cause} (conversation {number})", script=f"{self.fix.script} {number}")


def on(entity, verb="turn_on"):
    return (f"{entity.split('.', 1)[0]}.{verb}", entity)


PRESS_FIX = Fix("Press the relay with one script", "Assist has no way to press the relay", "Press the relay",
                (on(TARGET, "turn_off"), on(TARGET)), "Presses the fixture relay.", "One turn")
LAMP_STEPS = (on(LAMP),)
LIVING_ROOM_STEPS = (on(LAMP), on(FAN))  # The lamp and, beside it, the fan: a side effect.
LAMP_AND_FAN = Say("Turn on the lamp.", ((LAMP, "on"), (FAN, "on")), "The lamp and the fan are on.")
# Who else changes a device, by the context Home Assistant puts on the change: someone signed in to it (in its app),
# or no one signed in, which is how it records a person at the switch or in the device's own app, the device's own
# timer and an automation set off by anything but Assist alike. One Assist's change set off has Assist's request as its
# run's parent.
SIGNED_IN = {"id": "someone-signed-in", "parent_id": None, "user_id": "fixture-user"}
NOT_SIGNED_IN = {"id": "no-one-signed-in", "parent_id": None, "user_id": None}
SET_OFF_BY_ASSIST = {"id": "automation-run", "parent_id": "household-context-1", "user_id": None}
ITS_OWN = {"id": "the-device-itself", "parent_id": None, "user_id": None}  # A device's report of its own change.

KINDS = {kind.name: kind for kind in (
    Kind("friction", FIRST, turns=3, analysis=FRICTION_GOALS, fix=PRESS_FIX, fix_works=True, fixed={1: "fix"}),
    Kind("same cause", "Press the fixture relay.", turns=3, analysis=FRICTION_GOALS, fix=PRESS_FIX, fix_works=True,
         fixed={1: "fix"}, covered_by="friction"),
    Kind("no fix", "Could you toggle the fixture relay?", turns=3, analysis=FRICTION_GOALS, fix=PRESS_FIX),
    Kind("smooth", "Turn the fixture relay off.", turns=1, smooth=True),
    Kind("fine", "Switch the fixture relay off.", turns=2, analysis=FINE_GOALS),
    Kind("unclear", "Do the relay thing.", turns=2, analysis={**FINE_GOALS, "question_for_user": "Which relay?"}),
    Kind("broken", "Please toggle the fixture relay.", analysis=None),
    Kind("unreadable", "Toggle the relay.", malformed=True),

    # A goal Assist missed, in one turn: the fix meets it.
    Kind("unmet", "Turn on the porch light.",
         talk=(Say("Turn on the porch light.", reply="Sorry, I can't do that right now."),),
         analysis=goals(goal(1, "Porch light on", PORCH, "on", went="unmet")),
         fix=Fix("A script for the porch light", "Assist doesn't reach the porch light", "Porch light on",
                 (on(PORCH),), "Turns on the porch light.", "The porch light comes on when asked"),
         fix_works=True, fixed={1: "fix"}),
    # The lamp asked for; Assist turns the fan on too and leaves it on.
    Kind("side effect", "Turn on the lamp.",
         talk=(Say("Turn on the lamp.", ((LAMP, "on"), (FAN, "on")), "The lamp and the fan are on."),),
         analysis=goals(goal(1, "Lamp on", LAMP, "on")),
         fix=Fix("Just the lamp", "Assist turns on the fan beside the lamp", "Lamp only", LAMP_STEPS,
                 "Turns on the living room lamp, and nothing else.", "The lamp alone comes on"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,)),
    # SIDE_EFFECT, and a while after the conversation the fan is turned back off: by no one signed in to Home
    # Assistant, or by someone signed in.
    Kind("side effect put back", "Can you turn on the lamp?", talk=(LAMP_AND_FAN,),
         analysis=goals(goal(1, "Lamp on", LAMP, "on")),
         fix=Fix("Only the lamp", "Assist turns on the fan beside the lamp", "Lamp by itself", LAMP_STEPS,
                 "Turns on the living room lamp, and nothing else.", "The lamp alone comes on"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,), put_back=((FAN, "off", NOT_SIGNED_IN),)),
    Kind("side effect put back by someone signed in", "Could you turn the lamp on?", talk=(LAMP_AND_FAN,),
         analysis=goals(goal(1, "Lamp on", LAMP, "on")),
         fix=Fix("The lamp alone", "Assist turns on the fan beside the lamp", "Lamp and nothing else", LAMP_STEPS,
                 "Turns on the living room lamp, and nothing else.", "The lamp alone comes on"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,), put_back=((FAN, "off", SIGNED_IN),)),
    Kind("side effect put back, reported without Assist's request", "Lamp on, would you?", talk=(LAMP_AND_FAN,),
         analysis=goals(goal(1, "Lamp on", LAMP, "on")),
         fix=Fix("Lamp, no fan", "Assist turns on the fan beside the lamp", "Lamp with no fan", LAMP_STEPS,
                 "Turns on the living room lamp, and nothing else.", "The lamp alone comes on"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,), put_back=((FAN, "off", NOT_SIGNED_IN),),
         unmarked=(FAN,)),
    Kind("side effect put back by an automation it set off", "Would you turn on the lamp?", talk=(LAMP_AND_FAN,),
         analysis=goals(goal(1, "Lamp on", LAMP, "on")),
         fix=Fix("The lamp only", "Assist turns on the fan beside the lamp", "Lamp but not the fan", LAMP_STEPS,
                 "Turns on the living room lamp, and nothing else.", "The lamp alone comes on"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,), put_back=((FAN, "off", SET_OFF_BY_ASSIST),)),
    # SIDE_EFFECT, and the user has Assist turn the fan back off. A fix that leaves the fan alone saves that turn.
    Kind("side effect undone on request", "Please turn on the lamp.",
         talk=(LAMP_AND_FAN, Say("Turn the fan back off.", ((FAN, "off"),), "The fan is off.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on"), goal(2, "The fan back off", FAN, "off", serves=1)),
         fix=Fix("Nothing but the lamp", "Assist turns on the fan beside the lamp", "Lamp without the fan", LAMP_STEPS,
                 "Turns on the living room lamp, and nothing else.", "The lamp alone comes on, and the fan stays off"),
         fix_works=True, fixed={1: "fix"}),
    # The fan turned on instead of the lamp, so the user asks again; the fan is left on.
    Kind("side effect and repeat", "Put the lamp on.",
         talk=(Say("Put the lamp on.", ((FAN, "on"),), "The fan is on."),
               Say("No, the lamp.", ((LAMP, "on"),), "The lamp is on.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on"), goal(2, "The lamp, not the fan", serves=1, step=True)),
         fix=Fix("The lamp, not the fan", "Assist mistakes the fan for the lamp", "Lamp not the fan", LAMP_STEPS,
                 "Turns on the living room lamp.", "The lamp comes on at the first request, and the fan stays off"),
         fix_works=True, fixed={1: "fix"}, correction=2, side_effects=(FAN,)),
    # The same, but Assist turns the fan back off in that turn: nothing is left changed.
    Kind("undone side effect and repeat", "Switch on the lamp.",
         talk=(Say("Switch on the lamp.", ((FAN, "on"), (FAN, "off")), "Sorry, that was the fan. It's off again."),
               Say("The lamp, please.", ((LAMP, "on"),), "The lamp is on.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on"), goal(2, "Asked again for the lamp", serves=1, step=True)),
         fix=Fix("The lamp at once", "Assist mistakes the fan for the lamp", "Lamp straight away", LAMP_STEPS,
                 "Turns on the living room lamp.", "The lamp comes on at the first request"),
         fix_works=True, fixed={1: "fix"}, correction=2),
    # Two root goals: the porch light went fine; the lamp took a question and its answer.
    Kind("partly", "Turn on the porch light, please.",
         talk=(Say("Turn on the porch light, please.", ((PORCH, "on"),), "The porch light is on."),
               Say("Turn on the lamp too.", reply="Which lamp do you mean?", wait=timedelta(minutes=2)),
               Say("The one in the living room.", ((LAMP, "on"),), "The living room lamp is on.")),
         analysis=goals(goal(1, "Porch light on", PORCH, "on"), goal(2, "Lamp on", LAMP, "on"),
                        goal(3, "Said which lamp", serves=2, step=True)),
         fix=Fix("A script for the living room lamp", "Assist doesn't know which lamp the lamp is",
                 "Living room lamp", LAMP_STEPS, "Turns on the living room lamp.", "The lamp comes on when asked",
                 breaks=(on(LAMP), on(PORCH, "turn_off"))),
         fix_works=True, fixed={2: "fix"}),
    # A goal Assist missed, then a question it can't answer from the home: unmet beside indeterminate.
    Kind("unmet beside indeterminate", "Switch the porch light on.",
         talk=(Say("Switch the porch light on.", reply="Sorry, I can't do that right now."),
               Say("Is the fridge plugged in?", reply="I'm not sure.")),
         analysis=goals(goal(1, "Porch light on", PORCH, "on"),
                        goal(2, "Whether the fridge is plugged in", fact="whether the fridge plug is on")),
         fix=Fix("A script for the porch light", "Assist doesn't reach the porch light", "Porch light please",
                 (on(PORCH),), "Turns on the porch light.", "The porch light comes on when asked"),
         fix_works=True, fixed={1: "fix"}),
    # The garage opened in the morning; closed in the evening, after Assist asked to be sure.
    Kind("two times", "Open the garage door.",
         talk=(Say("Open the garage door.", ((GARAGE, "open"),), "The garage door is open."),
               Say("Close the garage door.", reply="Do you want me to close the garage door now?",
                   wait=timedelta(hours=10)),
               Say("Yes.", ((GARAGE, "closed"),), "The garage door is closed.")),
         analysis=goals(goal(1, "Garage door open", GARAGE, "open"), goal(2, "Garage door closed", GARAGE, "closed"),
                        goal(3, "Said yes to closing it", serves=2, step=True)),
         fix=Fix("Close the garage without asking", "Assist asks before closing the garage door",
                 "Close the garage door", (("cover.close_cover", GARAGE),), "Closes the garage door.",
                 "The garage door closes when asked"),
         fix_works=True, fixed={2: "fix"}, note_about=2),
    # Garage opened, porch light on (which took a question), garage closed, at different times.
    Kind("trouble in the middle", "Open the garage door, please.",
         talk=(Say("Open the garage door, please.", ((GARAGE, "open"),), "The garage door is open."),
               Say("Turn on the porch light.", reply="Which light do you mean?", wait=timedelta(hours=9)),
               Say("The one by the front door.", ((PORCH, "on"),), "The porch light is on."),
               Say("Close the garage door.", ((GARAGE, "closed"),), "The garage door is closed.",
                   wait=timedelta(hours=3))),
         analysis=goals(goal(1, "Garage door open", GARAGE, "open"), goal(2, "Porch light on", PORCH, "on"),
                        goal(3, "Said which light", serves=2, step=True),
                        goal(4, "Garage door closed", GARAGE, "closed")),
         fix=Fix("A script for the porch light", "Assist doesn't know which light the porch light is",
                 "Front porch light", (on(PORCH),), "Turns on the porch light by the front door.",
                 "The porch light comes on when asked"),
         fix_works=True, fixed={2: "fix"}),
    # The garage asked for twice, with the porch light in between.
    Kind("in between", "Can you open the garage door?",
         talk=(Say("Can you open the garage door?", reply="Sorry, I didn't catch which device."),
               Say("Turn on the porch light.", ((PORCH, "on"),), "The porch light is on."),
               Say("Open the garage door.", ((GARAGE, "open"),), "The garage door is open.")),
         analysis=goals(goal(1, "Garage door open", GARAGE, "open"), goal(2, "Porch light on", PORCH, "on"),
                        goal(3, "Asked again for the garage", serves=1, step=True)),
         fix=Fix("A script for the garage door", "Assist doesn't catch the garage door", "Open the garage door",
                 (("cover.open_cover", GARAGE),), "Opens the garage door.", "The garage opens at the first request"),
         fix_works=True, fixed={1: "fix"}),
    # One request, one time: both garage doors open (one world state, so one root goal).
    Kind("both doors at once", "Open both garage doors.",
         talk=(Say("Open both garage doors.", ((LEFT, "open"), (RIGHT, "open")), "Both garage doors are open."),),
         analysis=goals(goal(1, "Both garage doors open", LEFT, "open"),
                        goal(1, "The right door open, with the left", RIGHT, "open", serves=1))),
    # Lamp on; Assist turns the fan on too; turning it off turns the heater off too; the heater goes back on.
    Kind("nested", "Turn on the living room lamp.",
         talk=(Say("Turn on the living room lamp.", ((LAMP, "on"), (FAN, "on")), "The lamp and the fan are on."),
               Say("Turn the fan off.", ((FAN, "off"), (HEATER, "off")), "The fan and the heater are off."),
               Say("Turn the heater back on.", ((HEATER, "on"),), "The heater is on.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on", went="met"),
                        goal(2, "The fan off again", FAN, "off", serves=1, went="met"),
                        goal(3, "The heater back on", HEATER, "on", serves=2, went="met"))),
    # NESTED, but the heater isn't turned back on.
    Kind("heater left off", "Turn on the lamp in the living room.",
         talk=(Say("Turn on the lamp in the living room.", ((LAMP, "on"), (FAN, "on")),
                   "The lamp and the fan are on."),
               Say("Turn the fan off.", ((FAN, "off"), (HEATER, "off")), "The fan and the heater are off."),
               Say("Turn the heater back on.", reply="Sorry, I can't do that right now.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on", went="met"),
                        goal(2, "The fan off again", FAN, "off", serves=1, went="met"),
                        goal(3, "The heater back on", HEATER, "on", serves=2, went="unmet")),
         side_effects=(HEATER,)),
    # A request, a correction, a "thanks", and a note sent with it.
    Kind("thanks and note", "Open the garage.",
         talk=(Say("Open the garage.", reply="Which garage door?"),
               Say("The left one.", ((LEFT, "open"),), "The left garage door is open."),
               Say("Thanks!", reply="You're welcome!")),
         analysis=goals(goal(1, "Left garage door open", LEFT, "open"),
                        goal(2, "Said it's the left door", serves=1, step=True),
                        goal(3, "Thanked Assist", serves=1, thanks=True)),
         note="Open the left door when I say the garage.", thanks=3, correction=2,
         fix=Fix("The garage is the left door", "Assist doesn't know which door the garage is",
                 "Open the left garage door", (("cover.open_cover", LEFT),), "Opens the left garage door.",
                 "The left door opens at the first request"),
         fix_works=True, fixed={1: "fix"}),
    # The only fix saves a turn by leaving a side effect (the fan, beside the lamp).
    Kind("turn for side effect", "Turn the lamp on.",
         talk=(Say("Turn the lamp on.", reply="Which lamp do you mean?"),
               Say("The living room lamp.", ((LAMP, "on"),), "The lamp is on.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on"), goal(2, "Said which lamp", serves=1, step=True)),
         fix=Fix("Turn on the living room", "Assist doesn't know which lamp the lamp is", "Living room on",
                 LIVING_ROOM_STEPS, "Turns on the living room lamp and fan.", "The lamp comes on at once"),
         fix_works=True, fixed={1: "fix"}),
    # The only fix saves two turns by leaving one side effect.
    Kind("two turns for side effect", "Lamp on, please.",
         talk=(Say("Lamp on, please.", reply="Which lamp do you mean?"),
               Say("The living room one.", reply="Which one in the living room?"),
               Say("The lamp by the sofa.", ((LAMP, "on"),), "The lamp is on.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on"), goal(2, "Said which room", serves=1, step=True),
                        goal(3, "Said which lamp", serves=1, step=True)),
         fix=Fix("Turn on the living room at once", "Assist doesn't know which lamp the lamp is",
                 "Living room lamp and fan", LIVING_ROOM_STEPS, "Turns on the living room lamp and fan.",
                 "The lamp comes on at once"),
         fix_works=True, fixed={1: "fix"}),
    # The only fix changes how Assist turns the lamp on, not how many turns it takes or what it leaves.
    Kind("no gain", "I'd like the lamp on.",
         talk=(Say("I'd like the lamp on.", reply="Which lamp do you mean?"),
               Say("The living room lamp.", ((LAMP, "on"),), "The lamp is on.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on"), goal(2, "Said which lamp", serves=1, step=True)),
         fix=Fix("A script for the lamp", "Assist turns the lamp on through its own tools", "Lamp on", LAMP_STEPS,
                 "Turns on the living room lamp.", "The lamp comes on through the script"),
         fix_works=True, fixed={2: "fix"}),
    # Assist meets the request but for its long reply; the note sent with it asks for just "Done." (DETAILS P6.1.9).
    Kind("wordy", "Lamp on in the living room.",
         talk=(Say("Lamp on in the living room.", ((LAMP, "on"),),
                   "I've turned on the living room lamp for you. Let me know if there's anything else I can do!"),),
         analysis=goals(goal(1, "Lamp on", LAMP, "on")), note="Just say Done.",
         fix=Fix("Just say Done", "Assist's replies are longer than the household wants", "Lamp on, just Done",
                 LAMP_STEPS, "Turns on the living room lamp and says Done.", "Assist says just Done"),
         fix_works=True, fixed={1: "fix"}),
    # The porch light is unavailable, so its goal stays unmet whatever Assist does; Assist turns the fan on instead,
    # and the user has it turned back off. A fix that leaves the fan alone saves that turn.
    Kind("unreachable and repair", "Porch light on, please.", start={PORCH: "unavailable"},
         talk=(Say("Porch light on, please.", ((FAN, "on"),), "The porch light is on."),
               Say("Turn the fan back off.", ((FAN, "off"),), "The fan is off.")),
         analysis=goals(goal(1, "Porch light on", PORCH, "on", went="unmet"),
                        goal(2, "The fan back off", FAN, "off", serves=1, went="met")),
         fix=Fix("The porch light, not the fan", "Assist mistakes the porch light for the fan", "Porch light only",
                 (on(PORCH),), "Turns on the porch light, and nothing else.", "The fan stays off"),
         fix_works=True, fixed={1: "fix"}),
    # Assist turns the fan on instead of the lamp, and the user files it: a fix that turns the lamp on closes both.
    Kind("filed", "Lamp on in here.",
         talk=(Say("Lamp on in here.", ((FAN, "on"),), "The fan is on."), Say("File an improvement.", reply="Filed.")),
         analysis=goals(goal(1, "Lamp on", LAMP, "on", went="unmet"), goal(2, "Filed it", serves=1, wrap_up=True)),
         fix=Fix("The lamp, not the fan", "Assist mistakes the fan for the lamp", "Lamp in here", LAMP_STEPS,
                 "Turns on the living room lamp.", "The lamp comes on, and the fan stays off"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,)),
    # Assist turns the fan on beside the porch light; the user objects, then files it. A fix that leaves the fan alone
    # removes the side effect, but the objection stands, so the filing turn is still said.
    Kind("objected and filed", "Porch light on now.",
         talk=(Say("Porch light on now.", ((PORCH, "on"), (FAN, "on")), "The porch light and the fan are on."),
               Say("I only asked for the porch light.", reply="Sorry about that."),
               Say("File an improvement.", reply="Filed.")),
         analysis=goals(goal(1, "Porch light on", PORCH, "on"),
                        goal(2, "Only the porch light", serves=1, about=1, quote="I only asked for the porch light"),
                        goal(3, "Filed it", serves=1, wrap_up=True)),
         fix=Fix("Just the porch light", "Assist turns on the fan beside the porch light", "Porch light alone",
                 (on(PORCH),), "Turns on the porch light, and nothing else.", "The fan stays off"),
         fix_works=True, fixed={1: "fix"}, side_effects=(FAN,),
         shows={"I only asked for the porch light": ("Generated response 1.", "Done.")}),  # The fix's "Done." too.
    # The heater is unavailable, yet Assist says it's on and will warm the room; the user says it isn't, then objects to
    # the promise. A fix that has Assist say only what it did closes the objection; the heater stays unreachable.
    Kind("heater unreachable", "Heater on, please.", start={HEATER: "unavailable"},
         talk=(Say("Heater on, please.", reply="Done, the heater is on and will warm the room."),
               Say("It doesn't seem to be on.", reply="The heater is unavailable right now; check its power."),
               Say("Just say what you did, without promising it will warm the room.", reply="Okay.")),
         analysis=goals(goal(1, "Heater on", HEATER, "on", went="unmet"),
                        goal(2, "Said it isn't on", serves=1, step=True),
                        goal(3, "Only what it did", serves=1, about=1, quote="without promising it will warm the room")),
         fix=Fix("Say only what was done", "Assist promises what the heater will do", "Heater on, plainly",
                 (on(HEATER),), "Turns on the heater.", "Assist says only what it did"),
         fix_works=True, fixed={1: "fix"},
         shows={"without promising it will warm the room": ("Generated response 1.",)}),
    # The right garage door opens with a disclaimer; the user thanks Assist and objects to it in one turn. A fix with a
    # plain reply closes the objection; the turn is still said, for the thanks.
    Kind("thanks and objection", "Open the right garage door.",
         talk=(Say("Open the right garage door.", ((RIGHT, "open"),),
                   "The right garage door should be open, though I can't confirm its position."),
               Say("Thanks, but less disclaimer please.", reply="You're welcome.")),
         analysis=goals(goal(1, "Right garage door open", RIGHT, "open"),
                        goal(2, "Thanked Assist", serves=1, thanks=True),
                        goal(2, "Less disclaimer", serves=1, about=1, quote="less disclaimer please")),
         fix=Fix("Plain confirmations", "Assist adds a disclaimer it wasn't asked for", "Right garage door",
                 (("cover.open_cover", RIGHT),), "Opens the right garage door.", "Assist confirms plainly"),
         fix_works=True, fixed={1: "fix"}),
    # A message for the kitchen speaker: Assist's broadcast says "Done." but reaches no device (a broadcast speaks only
    # on voice satellites), so it isn't spoken. A script that speaks it on the speaker meets it.
    Kind("unspoken", "Say dinner is ready on the kitchen speaker.",
         talk=(Say("Say dinner is ready on the kitchen speaker.", broadcast="Dinner is ready"),),
         analysis=goals({"turn": 1, "summary": "Dinner is ready, on the kitchen speaker", "kind": "action",
                         "outcome": "announce", "entity_ids": [SPEAKER],
                         "_went": "unmet"}),
         fix=Fix("Speak on the kitchen speaker", "A broadcast speaks only on voice satellites", "Dinner call",
                 (("tts.speak", SPEECH, {"media_player_entity_id": SPEAKER, "message": "Dinner is ready"}),),
                 "Says dinner is ready on the kitchen speaker.", "The kitchen speaker says dinner is ready"),
         fix_works=True, fixed={1: "fix"}),
    # A goal missed in a turn Home Assistant answered itself, not the AI: no replay can run it, so nothing is proposed.
    Kind("answered locally", "Turn the porch light on.",
         talk=(Say("Turn the porch light on.", reply="Sorry, I couldn't understand that", local=True),),
         analysis=goals(goal(1, "Porch light on", PORCH, "on", went="unmet"))),
    # ANSWERED_LOCALLY's trouble, which the unmet kind's improvement covers too: none reaches it while Home Assistant
    # answers first.
    Kind("answered locally, covered", "Porch light on, Assist.",
         talk=(Say("Porch light on, Assist.", reply="Sorry, I couldn't understand that", local=True),),
         analysis=goals(goal(1, "Porch light on", PORCH, "on", went="unmet")), covered_by="unmet"),
    # A goal missed in a turn an automation's sentence trigger answered: no setting gives it to the AI.
    Kind("answered by an automation", "Porch light on, please, now.",
         talk=(Say("Porch light on, please, now.", reply="Good night", local="sentence_trigger"),),
         analysis=goals(goal(1, "Porch light on", PORCH, "on", went="unmet"))),
    # The same, but Home Assistant's own commands did what was asked: nothing to improve.
    Kind("answered locally, fine", "Switch on the porch light.",
         talk=(Say("Switch on the porch light.", ((PORCH, "on"),), "Turned on the light", local=True),),
         analysis=goals(goal(1, "Porch light on", PORCH, "on"))),
    # UNSPOKEN's trouble on every speaker. The fix speaks any message: a script taking it from Assist.
    Kind("unspoken everywhere", "Tell everyone on the speakers that dinner is ready.",
         talk=(Say("Tell everyone on the speakers that dinner is ready.", broadcast="Dinner is ready"),),
         analysis=goals({"turn": 1, "summary": "Dinner is ready, on every speaker", "kind": "action",
                         "outcome": "announce", "entity_ids": [SPEAKER, LOUNGE],
                         "_went": "unmet"}),
         fix=Fix("Speak on every speaker", "A broadcast speaks only on voice satellites", "Speak everywhere",
                 (("tts.speak", SPEECH, {"media_player_entity_id": f"{SPEAKER}, {LOUNGE}", "message": "{{ message }}"}),),
                 "Says a message on every speaker.", "Every speaker says the message",
                 fields=({"key": "message", "name": "Message", "description": "The words to say"},),
                 asks={"message": "Dinner is ready"}),
         fix_works=True, fixed={1: "fix"}),
    # A broadcast revision: asked in reported speech, so no words to speak are said;
    # Assist's broadcast says "Done." and reaches no device, and the user says it didn't work. A script speaking words
    # of Assist's choice on every speaker meets it at turn 1, so turn 2 isn't said with it.
    Kind("unspoken, asked again", "Broadcast on the speakers asking everyone to come in.",
         talk=(Say("Broadcast on the speakers asking everyone to come in.", broadcast="Everyone, please come in."),
               Say("That didn't work.", reply="Sorry about that.")),
         analysis=goals({"turn": 1, "summary": "Everyone asked in, on every speaker", "kind": "action",
                         "outcome": "announce", "entity_ids": [SPEAKER, LOUNGE], "_went": "unmet"},
                        goal(2, "Said it didn't work", serves=1, step=True)),
         fix=Fix("Call everyone on the speakers", "A broadcast speaks only on voice satellites", "Call everyone",
                 (("tts.speak", SPEECH, {"media_player_entity_id": f"{SPEAKER}, {LOUNGE}",
                                         "message": "{{ message }}"}),),
                 "Says a message on every speaker.", "Every speaker says the message",
                 fields=({"key": "message", "name": "Message", "description": "The words to say"},),
                 asks={"message": "Come inside, it's time"}),
         fix_works=True, fixed={1: "fix"}),
    # The lamp asked for at half brightness; Assist turns it on at the brightness it had. A script that turns it on at
    # half, given as 0 to 255 where the user's was a percentage, meets it.
    Kind("dimmed", "Turn the lamp on at half brightness.", able={LAMP: {"supported_color_modes": ["brightness"]}},
         talk=(Say("Turn the lamp on at half brightness.", ((LAMP, "on"),), "The lamp is on."),),
         analysis=goals(goal(1, "Lamp on at half brightness", LAMP, "on", settings={"brightness_pct": 50},
                             went="unmet")),
         fix=Fix("Dim the lamp to half", "Assist turns the lamp on without dimming it", "Lamp at half",
                 (("light.turn_on", LAMP, {"brightness": 128}),), "Turns on the living room lamp at half brightness.",
                 "The lamp comes on at half brightness"),
         fix_works=True, fixed={1: "fix"}),
    # The chime asked for; Assist can't press it. A script that presses it meets it: a button keeps no state to reach,
    # so the call reaching it does it.
    Kind("unpressed", "Ring the chime.",
         talk=(Say("Ring the chime.", reply="Sorry, I can't do that right now."),),
         analysis=goals({"turn": 1, "summary": "The chime rung", "kind": "action", "outcome": "command",
                         "entity_ids": [CHIME], "_went": "unmet"}),
         fix=Fix("A script for the chime", "Assist can't press the chime", "Ring the chime",
                 (("button.press", CHIME),), "Presses the chime.", "The chime rings when asked"),
         fix_works=True, fixed={1: "fix"}),
    # The porch light asked for; Assist misses it. The fix's script turns it on and presses the chime, unasked.
    Kind("chimed unasked", "Light the porch.",
         talk=(Say("Light the porch.", reply="Sorry, I can't do that right now."),),
         analysis=goals(goal(1, "Porch light on", PORCH, "on", went="unmet")),
         fix=Fix("Porch light and chime", "Assist doesn't reach the porch light", "Welcome home",
                 (on(PORCH), ("button.press", CHIME)), "Turns on the porch light and rings the chime.",
                 "The porch light comes on when asked"),
         fix_works=True, fixed={1: "fix"}),
    # The heater asked for, then again; its command failed at home both times, so the home couldn't do it whatever
    # Assist did.
    Kind("heater failed", "Turn the heater on.", start={HEATER: "off"},
         talk=(Say("Turn the heater on.", ((HEATER, "on"),), failed="The heater didn't respond"),
               Say("It's still off, try again.", ((HEATER, "on"),), "The heater didn't respond.",
                   failed="The heater didn't respond")),
         analysis=goals(goal(1, "Heater on", HEATER, "on", went="unmet"), goal(2, "Asked again", serves=1, step=True)),
         fix=Fix("Say when the heater doesn't respond", "Assist says Done when the heater fails", "Heater on, or say so",
                 (on(HEATER),), "Turns on the heater.", "Assist says when the heater doesn't respond"),
         fix_works=True, fixed={1: "fix"}),
)}
ORDER = list(KINDS)  # Each kind's conversations start on a day of their own.


@dataclass(eq=False)
class Conversation:
    """One conversation the household had: what the tests hold on to. Equal only to itself."""
    id: str
    kind: Kind
    said: str
    record: dict = field(repr=False)
    number: int = 1

    def __hash__(self):
        return hash(self.id)

    @property
    def fix(self):
        return self.kind.fix_for(self.number)


def recording(kind, number):
    """The kind's recording for the household's conversation number `number` (counting from 1)."""
    said = kind.said if number == 1 else f"{kind.said[:-1]}, conversation {number}{kind.said[-1]}"
    began = datetime(2026, 1, 2, 8, tzinfo=UTC) + timedelta(days=ORDER.index(kind.name), hours=number)
    if kind.talk is None:
        record = json.loads(json.dumps(prepared_recording()).replace(FIRST, said))
        _shift(record, began - datetime(2026, 1, 1, tzinfo=UTC))
        _whole_household(record)
    else:
        record = _built(kind, said, began)
    # The product knows a conversation by its id's first 8 characters: each conversation's are its own.
    record.update(id=sha256(f"{kind.name}:{number}".encode()).hexdigest()[:8] + kind.name.replace(" ", "")[:12]
                  + f"{number:02d}", status="session_expired")
    if kind.turns is not None:
        users = [m for m in record["chat_log"]["content"] if m["role"] == "user"]
        cut = users[kind.turns]["created"] if kind.turns < len(users) else None
        record["turns"] = record["turns"][:kind.turns]
        if cut:
            record["chat_log"]["content"] = [m for m in record["chat_log"]["content"] if m.get("created", "") < cut]
            record["state_changes"] = [c for c in record["state_changes"] if c["at"] < cut]
    if kind.malformed:  # The chat log keeps four requests; only three turns were kept.
        record["turns"] = record["turns"][:3]
    return Conversation(record["id"], kind, said, record, number)


def _shift(record, delta):
    """Moves every time in the fixture recording by delta: each conversation happened at a time of its own."""
    def moved(value):
        return (datetime.fromisoformat(value) + delta).isoformat()
    for message in record["chat_log"]["content"]:
        if "created" in message:
            message["created"] = moved(message["created"])
    for turn in record["turns"]:
        turn["user"]["created"] = moved(turn["user"]["created"])
        turn["source"]["at"] = moved(turn["source"]["at"])
        for preparation in turn["preparations"]:
            for message in preparation["content"]:
                if "created" in message:
                    message["created"] = moved(message["created"])
    for change in record["state_changes"]:
        change["at"] = moved(change["at"])


def _state(entity, value, able=None):
    """A device's state, as it shows what it can do: its kind's ABLE, and anything more able gives it (Kind.able)."""
    can = {**ABLE.get(entity.split(".")[0], {}), **(able or {}).get(entity, {})}
    return {"entity_id": entity, "state": value, "attributes": {"friendly_name": _name(entity), **can}}


def _name(entity):
    return HOUSE[entity][1] if entity in HOUSE else REPLAY_ENTITIES.get(entity, (None, "Fixture press"))[1]


def _entry(entity):
    """The device's registry entry, as snapshot.py records it."""
    name = _name(entity)
    return {"name": name, "aliases": [None], "area_id": HOUSE[entity][2] if entity in HOUSE else "fixture_area"}


def _whole_household(record):
    """The fixture recording's snapshots, with the rest of the household as it stood (as HOUSE has it)."""
    for turn in record["turns"]:
        source = turn["source"]
        for entity, (state, _, _) in HOUSE.items():
            source["states"].setdefault(entity, _state(entity, state))
            source["entities"].setdefault(entity, _entry(entity))
            source["exposure"].setdefault(entity, True)
        for area, name in AREAS.items():
            source["areas"].setdefault(area, {"name": name})


def _built(kind, said, began):
    """A recording of the kind's own talk, in make_recording's format, prepared for replays as prepared_recording
    prepares the fixture's: each turn's snapshot of the whole household, its prepared request, the chat log, and the
    state changes HA attributes to each turn's request."""
    clock = began

    def tick():
        nonlocal clock
        clock += timedelta(seconds=1)
        return clock.isoformat()
    states = {TARGET: "on", SCRIPT: "off", **{e: s for e, (s, _, _) in HOUSE.items()}, **kind.start}
    schema = {"type": "object", "properties": {"name": {"type": "string"},
                                               "domain": {"type": "array", "items": {"type": "string"}}},
              "additionalProperties": False}
    tools = [{"name": f"intent__HassTurn{direction}", "description": f"Turn {direction}", "integration": "intent",
              "parameters": schema} for direction in ["On", "Off"]]
    tools.append({"name": "homeassistant__GetLiveContext", "description": "Get recorded state",
                  "integration": "homeassistant",
                  "parameters": {"type": "object", "properties": {"domain": {"type": "string"}},
                                 "additionalProperties": False}})
    tools += [{"name": f"FixtureExtra{i}", "description": "Unused fixture tool", "integration": "fixture",
               "parameters": {"type": "object", "properties": {}}} for i in range(8)]
    if any(say.broadcast for say in kind.talk):
        tools.append({"name": "assist_satellite__HassBroadcast", "description": "Broadcast a message",
                      "integration": "assist_satellite", "parameters": {
                          "type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}})
    messages = [{"role": "system", "content": "Frozen instructions and the household's inventory."}]
    record = {"format": 1, "id": "synthetic-recording", "core_version": "2026.10.2", "status": "session_expired",
              "limitations": ["provider_native_history_omitted; replay adapter must check compatibility"],
              "turns": [], "state_changes": [], "chat_log": {"content": messages}}
    for number, say in enumerate(kind.talk, 1):
        if number > 1:
            clock += say.wait
        text = said if number == 1 else say.text
        user = {"role": "user", "content": text, "created": tick()}
        messages.append(user)
        source = {"at": tick(), "states": {e: _state(e, s, kind.able) for e, s in states.items()},
                  "entities": {e: _entry(e) for e in states},
                  "exposure": {e: e != SCRIPT for e in states}, "devices": {},
                  "areas": {area: {"name": name} for area, name in AREAS.items()}}
        context = {"id": f"household-context-{number}", "parent_id": None, "user_id": "fixture-user"}
        prepared = {"context": {"platform": "anthropic", "context": context, "language": "en",
                                "assistant": "conversation", "device_id": "fixture-phone"},
                    "tools": deepcopy(tools),
                    "provider_config": [{"config_entry_id": "fixture-entry", "subentry_id": "fixture-anthropic",
                                         "options": deepcopy(PROFILE)}],
                    "provider_model_capabilities": {}}
        record["turns"].append({"user": deepcopy(user), "source": source} if say.local else {
            "user": deepcopy(user), "source": source, "prepared": prepared,
            "preparations": [{"content": deepcopy(messages)}]})
        for index, (entity, new) in enumerate(say.calls):
            direction = "On" if new in ("on", "open") else "Off"
            call = {"tool_name": f"intent__HassTurn{direction}",
                    "tool_args": {"name": _name(entity), "domain": [entity.split(".", 1)[0]]},
                    "id": f"call-{number}-{index}", "external": False}
            messages.append({"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call],
                             "created": tick()})
            if say.failed:  # Its call log has the call, with Home Assistant's error; the tool's result, the intent's.
                service = f"{entity.split('.', 1)[0]}.turn_{direction.lower()}"
                record["turns"][-1].setdefault("actions", []).append(
                    {"action": service, "data": {"entity_id": entity}, "error": say.failed})
                messages.append({"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"],
                                 "tool_name": call["tool_name"], "created": tick(), "result": {
                                     "error": "IntentHandleError",
                                     "error_text": f"Failed to call turn_{direction.lower()} for {_name(entity)}"}})
                continue
            if states[entity] != new:
                record["state_changes"].append({
                    "entity_id": entity, "at": tick(), "old": _state(entity, states[entity], kind.able),
                    "new": {**_state(entity, new, kind.able),
                            "context": deepcopy(ITS_OWN if entity in kind.unmarked else context)}})
                states[entity] = new
            result = {"data": {"speech": {}, "response_type": "action_done", "data": {
                "success": [{"name": _name(entity), "type": "entity", "id": entity}], "failed": []}}, "error": False}
            messages.append({"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"],
                             "tool_name": call["tool_name"], "result": result, "created": tick()})
        if say.broadcast:
            call = {"tool_name": "assist_satellite__HassBroadcast", "tool_args": {"message": say.broadcast},
                    "id": f"call-{number}-broadcast", "external": False}
            messages.append({"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call],
                             "created": tick()})
            result = {"data": {"speech": {}, "response_type": "action_done", "data": {
                "targets": [], "success": [], "failed": []}}, "error": False}  # As HA answers: done, on none.
            messages.append({"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"],
                             "tool_name": call["tool_name"], "result": result, "created": tick()})
        messages.append({"role": "assistant", "content": say.reply, "created": tick(), "agent_id": (
            say.local if isinstance(say.local, str) else "conversation.home_assistant" if say.local
            else "conversation.fixture")})
    for entity, new, context in kind.put_back:  # Five minutes after its last reply, before its chat session ends.
        clock += timedelta(minutes=5)
        record["state_changes"].append({"entity_id": entity, "at": tick(),
                                        "old": _state(entity, states[entity], kind.able),
                                        "new": {**_state(entity, new, kind.able), "context": dict(context)}})
        states[entity] = new
    return record
