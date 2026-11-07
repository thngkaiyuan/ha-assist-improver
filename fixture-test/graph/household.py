"""The household, as the test graph sees it: the only code graph tests use to touch the product.

Tests act the way the household does (converse, run, send in, decide) and read only what the product records. The AI
roles are stand-ins that answer each kind of conversation the same way every time. Anything the product doesn't offer
yet raises Missing, so a red test names the interface it's waiting for.
"""

STAGES = ("Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review", "Your household")
STAMPS = ("Review", "Simulate")
DECISIONS = ("accept", "revise", "decline")
GOAL_STATES = ("met", "unmet", "indeterminate")

# Kinds of conversation, all made up for the tests.
FRICTION = "friction"        # Assist needs an extra turn to reach a goal; a fix removes it. Each FRICTION
                             # conversation has a cause of its own: one's fix never covers another's
SAME_CAUSE = "same cause"    # another conversation whose trouble FRICTION's fix also removes
UNMET = "unmet"              # Assist misses a goal; a fix meets it
SIDE_EFFECT = "side effect"  # Assist leaves a device changed that no goal asked for; a fix avoids it
NO_GAIN = "no gain"          # the only fix changes nothing in goals met, turns or side effects
SMOOTH = "smooth"            # Triage is sure nothing needs improving
FINE = "fine"                # analysed: no trouble
UNCLEAR = "unclear"          # the analyzer can't tell what the user wanted
BROKEN = "broken"            # every analysis of it fails
NO_FIX = "no fix"            # no fix it gets is validated
UNREADABLE = "unreadable"    # no stage can read it
THANKS_AND_NOTE = "thanks and note"  # a request, a correction, a "thanks", and a note sent with it
TWO_TIMES = "two times"      # garage opened, then closed later: two requests at different times
PARTLY = "partly"            # two root goals: one has trouble a fix removes, the other went fine
UNMET_BESIDE_INDETERMINATE = "unmet beside indeterminate"  # one goal missed (a fix meets it); another indeterminate
SIDE_EFFECT_AND_REPEAT = "side effect and repeat"  # a side effect left, and a repeated request; a fix avoids both
UNDONE_SIDE_EFFECT_AND_REPEAT = "undone side effect and repeat"  # the same, but Assist undoes the change in that turn
SIDE_EFFECT_PUT_BACK = "side effect put back"  # SIDE_EFFECT, and a while after the conversation the fan is turned back
                             # off by no one signed in to Home Assistant: a person at its switch or in its own app,
                             # its own timer, or an automation set off by anything but Assist (recorded alike)
SIDE_EFFECT_PUT_BACK_SIGNED_IN = "side effect put back by someone signed in"  # the same, by someone signed in
SIDE_EFFECT_PUT_BACK_UNMARKED = "side effect put back, reported without Assist's request"  # SIDE_EFFECT_PUT_BACK, but
                             # the fan reports Assist's change without Assist's request on it: only Assist's results
                             # name it as Assist's
SIDE_EFFECT_PUT_BACK_BY_AUTOMATION = "side effect put back by an automation it set off"  # the same, by an automation
                             # Assist's turning the fan on set off, whose run names Assist's request as its parent
SIDE_EFFECT_UNDONE_ON_REQUEST = "side effect undone on request"  # SIDE_EFFECT, and the user has Assist turn the fan
                             # back off; a fix that leaves the fan alone saves that turn
IN_BETWEEN = "in between"    # the garage asked for twice, with the porch light in between; a fix saves the repeat
TURN_FOR_SIDE_EFFECT = "turn for side effect"  # the only fix saves a turn by leaving a side effect
TWO_TURNS_FOR_SIDE_EFFECT = "two turns for side effect"  # the only fix saves two turns by leaving one side effect
TROUBLE_IN_THE_MIDDLE = "trouble in the middle"  # garage opened, porch light on, garage closed, at different times;
                                                 # only the porch light takes an extra turn, and a fix removes it
BOTH_DOORS = "both doors at once"  # one request to open both garage doors at one time
NESTED = "nested"            # lamp on; Assist also turns the fan on, and turning it off also turns the heater off; the
                             # heater goes back on: sub-goals nested under the lamp
HEATER_LEFT_OFF = "heater left off"  # NESTED, but the heater isn't turned back on: unmet two deep, beside the fan met
WORDY = "wordy"              # Assist meets the request but for its long reply, and the note sent with it asks for a
                             # shorter one; a fix gives it
FILED = "filed"              # Assist turns the fan on instead of the lamp, and the user files it; a fix meets the goal
OBJECTED_AND_FILED = "objected and filed"  # a side effect left, objected to, then filed; a fix avoids the side
                             # effect, while the objection stands
HEATER_UNREACHABLE = "heater unreachable"  # the heater is unavailable, yet Assist says it's on and will warm the
                             # room; the user says it isn't, then objects to the promise; a fix closes the objection
THANKS_AND_OBJECTION = "thanks and objection"  # a garage door opened with a disclaimer; the user thanks Assist and
                             # objects to it in one turn; a fix closes the objection
UNSPOKEN = "unspoken"        # a message asked for on the kitchen speaker; Assist's broadcast says "Done." but reaches
                             # no device; a fix speaks it on the speaker
UNSPOKEN_EVERYWHERE = "unspoken everywhere"  # UNSPOKEN on every speaker; a fix is a script that takes the message
UNSPOKEN_ASKED_AGAIN = "unspoken, asked again"  # UNSPOKEN_EVERYWHERE asked in reported speech, then "it didn't
                             # work"; a fix speaking other words on every speaker meets it at once
ANSWERED_LOCALLY = "answered locally"  # a goal missed in a turn Home Assistant answered itself, not the AI
ANSWERED_LOCALLY_FINE = "answered locally, fine"  # the same, but the goal met: nothing to improve
ANSWERED_LOCALLY_COVERED = "answered locally, covered"  # ANSWERED_LOCALLY, its trouble covered by UNMET's improvement
ANSWERED_BY_AN_AUTOMATION = "answered by an automation"  # a goal missed in a turn a sentence trigger answered
UNREACHABLE_AND_REPAIR = "unreachable and repair"  # the porch light is unavailable, so its goal is unmet whatever
                             # Assist does; Assist turns the fan on instead and is asked to turn it back off; a fix
                             # that leaves the fan alone saves that turn
DIMMED = "dimmed"            # the lamp asked for at half brightness and turned on undimmed; a fix dims it to half
UNPRESSED = "unpressed"      # a button (the chime) asked to be pressed, and Assist can't; a fix presses it
CHIMED_UNASKED = "chimed unasked"  # UNMET, where the fix's script also presses the chime, unasked
HEATER_FAILED = "heater failed"  # the heater asked for twice, its command failing at home both times
HEATER_FAILED_OBJECTED = "heater failed, objected"  # the heater's command failing at home, yet Assist says it's on
                             # and will warm the room; asked again, it fails again, and the user objects to the
                             # promise; a fix closes the objection
LOOKED_UP = "looked up"      # a question about the pasta timer, answered from what Assist's lookup returned


class Missing(Exception):
    """The product doesn't offer this yet."""


def _missing(name):
    def method(self, *args, **kwargs):
        raise Missing(f"Household.{name}")
    method.__name__ = name
    return method


class Interface:
    """Everything graph tests may use, each raising Missing until harness/drive.py implements it on the product."""

    # Acting as the household
    converse = _missing("converse")              # (kind, filed=False, ends=True) -> conversation: recorded, then
                                                 #   ended; filed: filed from Assist while it was going on; ends=False:
                                                 #   still going on, until end
    end = _missing("end")                        # (*conversations) -> run: conversations going on end together (their
                                                 #   chat sessions expire); the last run that started
    run = _missing("run")                        # (kind="full"|"scheduled"|"picked"|"stage", picked=(), stage=None) -> run
    send_in = _missing("send_in")                # (conversation, note=None) -> run: the fast lane, or sending in again
    answer = _missing("answer")                  # (conversation, text) -> run: the household answers the question it
                                                 #   waits at Your review with
    block = _missing("block")                    # (stage)
    unblock = _missing("unblock")                # (stage): lifts a block (releases nothing by itself, P0.8)
    decide = _missing("decide")                  # (item, decision, note=None, reason=None) -> Outcome; decision in
                                                 #   DECISIONS; reason ("not an issue") only with "decline"
    agree = _missing("agree")                    # (outcome) -> run: yes to what an outcome asks, e.g. resolving a conflict
    archive = _missing("archive")                # (conversation)
    unarchive = _missing("unarchive")            # (conversation)
    mark_test = _missing("mark_test")            # (conversation)
    edit_home = _missing("edit_home")            # (improvement): change, by hand, what the improvement changes
    meanwhile = _missing("meanwhile")            # (stage, act, *args): the household does act (decide or agree) with
                                                 #   those arguments while that stage's stand-in works, next time it's
                                                 #   called (in a run)

    # Stand-ins. A behaviour is plain words, named the same everywhere it's used (grep the graph for stand_in).
    stand_in = _missing("stand_in")              # (stage, behaviour): how that stage's stand-in AI role behaves
    # What the stand-ins logged themselves, which the product can't change (oldest first)
    reviews = _missing("reviews")                # (conversation) -> [Review]: what the stand-in reviewer judged of
                                                 #   the drafts for it
    received = _missing("received")              # (stage, conversation) -> [Received]: what that stage's stand-in was
                                                 #   given for it: each call's input, for Review each draft, and for
                                                 #   Simulate each replay request the household's assistant answered
    reached = _missing("reached")                # (stage, conversation) -> what the proposer or its reviewer ("Propose"
                                                 #   or "Review") got while working on it: the conversations within
                                                 #   its reach (in its input, or read with a session tool it was
                                                 #   handed), and its turns and goals as it got them

    # Reading what the product records (the fields tests use are listed below)
    place = _missing("place")                    # (conversation) -> Place
    records = _missing("records")                # (conversation, round) -> {stage: that stage's record of it}
    shown = _missing("shown")                    # (conversation) -> {screen: the stage it shows it at}; a chip or
                                                 #   hold that contradicts the record shows as what it says instead
    trace = _missing("trace")                    # (conversation) -> [Moment]
    path = _missing("path")                      # (conversation, round=None) -> the stages it went through, in order
    handoffs = _missing("handoffs")              # (conversation) -> [Handoff]
    analysis = _missing("analysis")              # (conversation) -> Tree, or None while it has none kept
    ideas = _missing("ideas")                    # (conversation) -> [Idea]
    tests = _missing("tests")                    # (conversation, round=None) -> [Test]
    tests_left = _missing("tests_left")          # (conversation) -> how many tests its round has left
    improvements = _missing("improvements")      # (conversation=None) -> [Improvement], oldest first (a revision
                                                 #   adds a newer version beside the earlier one)
    review_queue = _missing("review_queue")      # () -> [Item]: what waits at Your review, by its records (an
                                                 #   earlier round's improvement can wait while its conversation is
                                                 #   back at Propose)
    counts = _missing("counts")                  # () -> {stage: how many conversations the screens show there}
    runs = _missing("runs")                      # () -> every run, oldest first
    moved = _missing("moved")                    # (run) -> {conversation}: what moved in it
    trips_back = _missing("trips_back")          # (conversation) -> [Trip]: each time it was sent back
    seen_by = _missing("seen_by")                # (stage, conversation, round=None) -> what that stage's AI role was
                                                 #   given for it (in that round)
    answered = _missing("answered")              # (conversation) -> what each test_idea answered the proposer for
                                                 #   it, in order (validated, gains, open: each sub-goal still open,
                                                 #   with why; set_aside)
    config = _missing("config")                  # () -> Home Assistant's configuration as the assistant reads it
    writable = _missing("writable")              # () -> everything in Home Assistant an improvement can write: config()'s
                                                 #   scripts and exposure, aliases, household knowledge, and the
                                                 #   assistants' instructions

# What the readers return, by the fields tests use:
# Place: stage, round, held (None, or a Hold), outcome (in Your household), archived, covered_by (a duplicate's
#   improvement)
# Hold: cause ("failures", "scope", "cut off" or "blocked"), why (what the household is shown on demand)
# Stage record (records(...)[stage]): attempts (one per try in that round); Analyze's also turns (as Tree.turns); Your
#   Assist's also ended (when the conversation ended) and started_from (the home and time it started from)
# Moment: stages (where it is at that moment), goals (what it carries)
# Handoff: start, end, record (record.fields), without(field) (the record minus a field), mistyped(field) (the record
#   with a value that field's type refuses), outside(field) (a steering field holding a value of the right kind
#   outside its fixed set), contract (accepts(record), fields, steering: the fields that decide what happens next,
#   each with a fixed set of values)
# Tree: turns (each with goals), goals, roots; a goal's state (in GOAL_STATES), parent, root, kind, and for a conduct
#   goal about (the turn whose reply it objects to, or "all") and quote (the household's words)
# Idea: changes (equal for the same changes, as Test's and Improvement's), stamps, reached_write_up, given (what the
#   proposer had when it made the idea, as seen_by returns it)
# Test: idea (its round's idea with its changes), changes, validated, judged (the goals it re-judged), before and
#   after (each a Replay), gains (the measures of P0.1.1 the change moved)
# Replay: goals ({goal: state}), goals_met (the goals met), turns_per_root_goal ({root: turns}), side_effects_left (one
#   per change left, so len() counts them),
#   said (the conversation's turns it said, in order), started_from (the home and time it started from), config (the
#   configuration it ran on, comparable with config()), easier_than(other)
# Improvement: conversation, tested_on, checked_on, changes_beyond(conversation), test, idea (the idea that reached
#   Write up with its changes), changes, status, decisions, notes_motion (whether its review says motion isn't
#   simulated), tested (what each of its review's Before & after rows says it tested)
# Item: conversation, question (None for an improvement)
# Outcome: run (the run it started, if any), asks (what it asks the household first, if anything; asks.about)
# Run: started (comparable with a conversation's end)
# Trip: start (the stage it went back from), paid_by ("person", or the budget it spent, which has a cap)
# Review: run, title and changes (the draft's), verdict ("approve" or "request_changes")
# Received: run, input (as the product handed it to the stand-in), after_cut (logged after its run was cut off)


from harness.drive import Drive  # noqa: E402 (after Missing, which the harness imports)


class Household(Drive, Interface):
    """A household with the improver installed: Home Assistant, its assistant, and stand-in AI roles."""
