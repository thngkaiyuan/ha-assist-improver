"""Stand-ins for the household's AI roles and its assistant.

Each AI role answers through its real tools, on Home Assistant's real AI Task path (ha_task.py), so every answer
passes the same code checks a model's would (as kit.Model does), and answers each conversation as its kind says
(corpus.py). The proposer and its reviewer also look over the household as a whole (P0.10), through the session tools
they're handed. Behaviours a test names with Household.stand_in change that, stage by stage. Simulate isn't an AI role:
its behaviours are how the household's assistant answers its replays (Assistant), and, for a run cut off around a
test, when the run is cut off. A run is cut off as the household's Cancel or Home Assistant stopping cuts it off: its
task is cancelled. What the household does while a run works (Household.meanwhile) it does as a stage's stand-in is
called, and a proposer's behaviour can also time a decision the household makes in the panel, landing as the proposer
finishes. The stand-ins keep their own log, which the product can't change: what each was given (for Simulate,
each replay the household's assistant answered), what the proposer and the reviewer read when they looked over the
household, what the reviewer judged, and where a run was cut off (Household.received, reached and reviews).

The household's assistant answers each replay request from what the request holds: which conversation (its first
request), which turn, whether it's answering a tool's result, and whether a change under test is offered (a script's
tool: no script is exposed to Assist otherwise). Without one it does what the recording shows; with one, what the
conversation's kind says the change makes it do."""

import asyncio
import contextvars
from copy import deepcopy
import json

from homeassistant.util import slugify

from household import Missing

from ha_task import use, use_many
from custom_components.assist_improver.cases import recording_turns

FAILING = ("fails every try", "fails every try in the next run, then answers as usual", "fails the first try, then works")
FIRST_RUN_FAILS = "fails every try in its first run, then works"
WITHOUT_A_FIELD = "answers without a field the next stage reads"
# The analyzer's first answer (or every answer, for "always") breaks one of P0.3's rules; the next is right.
WRONG_FIRST = ("first leaves the thanks without a goal", "first leaves the correction without a goal",
               "first leaves the note without a goal", "first paraphrases the note instead of quoting it",
               "first points the note's objection at a turn outside its goal's tree",
               "first makes the thanks a root goal of its own", "first makes the note a root goal of its own",
               "first hangs a sub-goal from a goal that isn't in the analysis", "first makes one goal per door",
               "first makes the opening and the later closing one goal")
# Wrong only in a revision's round, the first time its analysis reads the revision note.
REVISION_WRONG_FIRST = ("first leaves the revision note without a goal",
                        "first hangs the revision note under a goal its improvement wasn't for")
ABOUT_THE_FIX = "reads the revision note as about the fix itself"  # No goal: listed apart (DETAILS P12.8.1).
ALWAYS_NO_THANKS = "always leaves the thanks without a goal"
OTHER_WAY = "says each goal went the other way"
TRIES_BETTER = "after a validated idea, tries a better one while tests are left"
BOTH_STAMPS = "asks for both stamps on every idea"
CLAIMS = "claims each idea it tests makes the goals easier"
CUTS_OFF = "cuts off the next run it works in"
BREAKS = "for the goal in trouble, proposes only fixes that break the goal that went fine"
KEEPS_ASKING = "keeps asking the reviewer while it can"
KEEPS_TESTING = "keeps testing while it can"  # Heeds no tests_left: only Simulate's refusal stops it.
WHOLE_HOUSEHOLD = "proposes a change for the whole household"
TWO_AT_ONCE = "proposes two ideas at once and sends both on"  # Told to send on one, it sends the first.
TESTS_TWO_SENDS_UNTESTED = "tests two ideas, then sends on a third it never tested"
CUT_AFTER_REVIEW = "asks for changes every time; the first run is cut off once its first review is done"
CUT_AFTER_TEST = "validates nothing; the first run is cut off once its first test is done"
CUT_AFTER_ONE_TEST = "cuts the first run off after one test"
CUT_IN_SECOND_TEST = "is cut off during its second test"
CUT_ON_FIRST_IDEA = "is cut off while it works on the first idea"
CUT_WHILE_TWO_RUN = "is cut off while two tests run"
CHANGES_AFTER_APPROVAL = ("after its idea is approved, tests and sends on a changed version under the same title without "
                          "asking again")
UNDER_ANOTHER_TITLE = "sends on its validated idea under another idea's title"
SLIPS = "slips once in each stamp's tool call"
BETTER_SAME_TITLE = "after a validated idea, tries a better version under the same title"
RETYPES = "re-types its changes when it sends them on"
TESTS_MANY_AT_ONCE = "tests more ideas in one message than the round has tests left"
TESTS_TWO_AT_ONCE = "tests two ideas in one message"
# The household accepts or declines the earlier version in the panel just as the revision's proposer finishes, its
# decision still being written as the proposer returns.
DECIDED_AS_IT_FINISHES = {"finishes its revision just as the household accepts the earlier version": "accept",
                          "finishes its revision just as the household declines the earlier version": "decline"}
FAILS_AFTER_REVIEW = "fails after its first review, then works"
# With its idea validated, it asks the household which door is the main one instead of sending it on; answered, it
# sends it on.
ASKS = "asks the household one question before it sends its idea on"
ASKED = "Which garage door is the main one?"
ASKS_MANY_AT_ONCE = "asks the reviewer about more ideas in one message than the budget has left"
TWO_WAITS = "waits for two drafts at once"
WAITS_AFTER_FINISHED = "waits again after it's told it's finished"
FIRST_VERSION_ONLY = "approves only the first version of each idea"
GOES_UNLIKE = "replays it without the change leaving its goal unmet, though the conversation met it"
EXTRA_TURN_GONE = ("replays it without the change meeting its goal in the first turn, though the conversation took an "
                   "extra turn")
SIDE_EFFECT_GONE = "replays it without the change leaving no side effect, though the conversation left one"
ASKS_NEW = ("replays it with the change asking the user, before it acts, something the conversation never "
            "answered")
NOT_VALIDATING = ("refuses every idea", "validates nothing", CUT_AFTER_TEST)
# The behaviours a test can name, by stage. Any other is the harness's gap.
BEHAVIOURS = {
    "Triage": {*FAILING, "has no model chosen"},
    "Analyze": {*FAILING, FIRST_RUN_FAILS, WITHOUT_A_FIELD, *WRONG_FIRST, *REVISION_WRONG_FIRST, ABOUT_THE_FIX,
                ALWAYS_NO_THANKS, OTHER_WAY},
    "Propose": {*FAILING, WITHOUT_A_FIELD, TRIES_BETTER, BOTH_STAMPS, CLAIMS, CUTS_OFF, BREAKS, KEEPS_ASKING,
                KEEPS_TESTING, WHOLE_HOUSEHOLD, TWO_AT_ONCE, TESTS_TWO_SENDS_UNTESTED, "proposes one idea at a time",
                "sends on its first validated idea", CHANGES_AFTER_APPROVAL, UNDER_ANOTHER_TITLE, SLIPS,
                BETTER_SAME_TITLE, RETYPES, TESTS_MANY_AT_ONCE, TESTS_TWO_AT_ONCE, FAILS_AFTER_REVIEW,
                ASKS_MANY_AT_ONCE, ASKS, *DECIDED_AS_IT_FINISHES},
    "Write up": set(FAILING),
    "Review": {"refuses every idea", "asks for changes to every idea", "crashes every time",
               "asks for changes to the first idea only", "approves only the first idea", CUT_ON_FIRST_IDEA,
               CUT_AFTER_REVIEW, TWO_WAITS, WAITS_AFTER_FINISHED, FIRST_VERSION_ONLY},
    "Simulate": {"refuses every idea", "validates nothing", "crashes every time", CUT_AFTER_TEST, CUT_AFTER_ONE_TEST,
                 CUT_IN_SECOND_TEST, CUT_ON_FIRST_IDEA, "doesn't validate the first idea it tests",
                 "validates only the first idea", GOES_UNLIKE, EXTRA_TURN_GONE, SIDE_EFFECT_GONE, ASKS_NEW,
                 CUT_WHILE_TWO_RUN},
}
ROLE_STAGES = {"triage": "Triage", "journey_analyzer": "Analyze", "hypothesis_generator": "Propose",
               "hypothesis_reviewer": "Review", "editor": "Write up"}
IDEAS = 8  # The most ideas the proposer puts forward in one proposal.
ASKS_PER_IDEA = 5  # As often as the reviewer may be asked about one idea (hypotheses.REVIEWS_PER_IDEA).
REVIEWS = 15  # The reviews a round has (hypotheses.REVIEWS_PER_ROUND).


async def cut_off():
    """The run in progress is cut off now, as the household's Cancel (or Home Assistant stopping) cuts it off: its
    task is cancelled. Called from inside the run, the cut lands here."""
    for task in asyncio.all_tasks():
        if task.get_name() == "assist_improver_pipeline" and not task.done():
            task.cancel()
    await asyncio.sleep(0)


def _plain(goals):
    """Goals as submit_goals takes them: without the harness's own marks (keys starting with _)."""
    return [{k: v for k, v in g.items() if not k.startswith("_")} for g in goals]


def _described(change, more):
    """A script change with more words at the end of its description: other changes."""
    return {**change, "config": {**change["config"], "description": change["config"]["description"] + more}}


def versions_of(script):
    """Whether a script name is one of the versions the proposer makes of a fix's script (its takes, its revised
    versions), and not another conversation's fix."""
    base = slugify(script)
    return lambda name: (slugify(name or "") == base or slugify(name or "").startswith(f"{base}_take_")
                         or slugify(name or "").startswith(f"{base}_v"))


class Roles:
    """The AI roles, scripted, for one household. conversations: {first request: Conversation}, kept by the world.
    current_run: the id of the run in progress (for behaviours that last one run)."""

    def __init__(self, conversations, current_run=lambda: None):
        self.conversations, self.current_run = conversations, current_run
        self.behaviours = {}  # {stage: behaviour}
        self.bound = {}  # {stage, or (stage, what): the run a behaviour holds for}
        self.done = set()  # (stage, what): one-time behaviours already done.
        self.failed = set()  # Stages whose "first try" already failed.
        self.answered = {}  # {conversation id: how often the analyzer answered for it}
        self.revising = {}  # {conversation id: how often it answered for it with a revision note to read}
        self.received = []  # (stage, run, what it was given): each role call's input, each draft the reviewer got, and
        # each replay request the household's assistant answered (Simulate).
        self.read = []  # (stage, run, the conversations it worked on, {session: what read_session gave it}): each time
        # the proposer or the reviewer looked over the household.
        self.reviews = []  # (run, the draft, the verdict): each review the reviewer sent back, as it judged it.
        self.cuts = []  # (how much of received was logged then, run): each time a run was cut off.
        self.testing = 0  # How many test_idea calls the proposer's latest message made, while they run.
        self.tested = {}  # {session: what each test_idea answered the proposer for it, in order}
        self.meanwhile = {}  # {stage: what the household does while that stage's stand-in works, next time it's called}

    def behave(self, stage, behaviour):
        if behaviour not in BEHAVIOURS.get(stage, ()):
            raise Missing(f"harness: stand_in({stage!r}, {behaviour!r})")
        self.behaviours[stage] = behaviour
        self.bound = {k: v for k, v in self.bound.items() if k != stage and not (isinstance(k, tuple) and k[0] == stage)}
        self.done = {k for k in self.done if k[0] != stage}
        self.failed.discard(stage)
        if stage == "Analyze":
            self.answered.clear()
            self.revising.clear()
        if (stage, behaviour) == ("Triage", "has no model chosen"):  # The household's setting, as the panel sets it.
            from custom_components.assist_improver.settings import updated
            world = self._world()
            world.entry.options = updated(world.hass, world.entry, role="triage", entity_id=None)

    def _world(self):
        """The world the household lives in (for what the household does: a setting it changes, a decision it
        makes)."""
        for cell in getattr(self.current_run, "__closure__", None) or ():
            if hasattr(cell.cell_contents, "entry") and hasattr(cell.cell_contents, "hass"):
                return cell.cell_contents
        raise Missing("harness: a stand-in can't reach the household")

    def _fails(self, stage):
        """Whether the stand-in for this stage fails now, as its behaviour says."""
        behaviour = self.behaviours.get(stage)
        if behaviour == "fails every try" or (stage == "Review" and behaviour == "crashes every time"):
            return True
        if behaviour == "fails the first try, then works":
            if stage in self.failed:
                return False
            self.failed.add(stage)
            return True
        if behaviour in ("fails every try in the next run, then answers as usual", FIRST_RUN_FAILS):
            return self.first_run(stage)
        return False

    def first_run(self, key):
        """Whether the run in progress is the first one this behaviour acted in."""
        run = self.current_run()
        return self.bound.setdefault(key, run) == run

    def once(self, key):
        """True the first time only."""
        if key in self.done:
            return False
        self.done.add(key)
        return True

    def kind(self, said):
        return self.conversations[said].kind

    async def cut(self):
        """The run in progress is cut off now (cut_off), and the log says where."""
        self.cuts.append((len(self.received), self.current_run()))
        await cut_off()

    def cut_in(self, run):
        """Whether that run was cut off."""
        return any(cut_run == run for _, cut_run in self.cuts)

    async def answer(self, hass, role, payload, tools):
        """One AI Task call, as Home Assistant's AI Task path makes it (ha_task.ScriptedTask): the role's part."""
        stage = ROLE_STAGES.get(role, role)
        self.received.append((stage, self.current_run(), deepcopy(payload)))
        if (act := self.meanwhile.pop(stage, None)) is not None:  # As the panel's command runs: a task of its own.
            await asyncio.get_running_loop().create_task(act(), context=contextvars.Context())
        if self._fails(stage):
            raise RuntimeError("The model is overloaded")
        return await getattr(self, role)(hass, payload, tools)

    use = staticmethod(use)  # A tool call through Home Assistant's chat log (ha_task).
    use_many = staticmethod(use_many)  # Several tool calls in one message.

    # Triage

    async def triage(self, hass, payload, tools):
        if self.behaviours.get("Triage") == "has no model chosen":
            raise RuntimeError("No AI model is chosen for Triage")
        said = payload["turns"][0]["user_says"]
        smooth = self.kind(said).smooth
        return {"smooth": smooth, "sure": True,
                "why": "Every request was done as asked." if smooth else "The user had to step in."}

    # Analyze

    async def journey_analyzer(self, hass, payload, tools):
        """The kind's goals, as submit_goals takes them; a behaviour may make an answer wrong, which the stand-in
        doesn't mend in the same call however the checks answer it: the next call answers right."""
        lines = payload["conversation"]
        said = lines[0]["user_says"]
        talk = self.conversations[said]
        if talk.kind.analysis is None:
            raise RuntimeError("The model is overloaded")
        count = self.answered[talk.id] = self.answered.get(talk.id, 0) + 1
        behaviour = self.behaviours.get("Analyze")
        note = ((payload.get("filed_by_user") or {}).get("note") or "").strip()
        answer = self._analysis(talk.kind, lines, note, behaviour)
        if payload.get("answered_by_user"):  # The household answered its question: it can tell.
            answer["question_for_user"] = ""
        if behaviour == ABOUT_THE_FIX:
            answer = {**answer, "goals": [g for g in answer["goals"] if not g.get("_revision")], "about_the_fix": [
                {"turn": line["turn"], "what": line["user_says"]} for line in lines if line.get("revision_of")]}
        if any(line.get("revision_of") for line in lines):
            revised = self.revising[talk.id] = self.revising.get(talk.id, 0) + 1
            wrong = self._wrong_revision(answer, behaviour, first=revised == 1)
        else:
            wrong = None
        wrong = wrong or self._wrong(talk.kind, answer, behaviour, first=count == 1)
        right = {**answer, "goals": _plain(answer["goals"])}
        accepted = await self.use(hass, tools["submit_goals"], wrong if wrong is not None else right,
                                  slip=wrong is not None)
        if wrong is not None:
            return "Done."
        assert accepted.get("accepted"), accepted
        await self._mark_duplicate(hass, talk, tools)
        return "Done."

    @staticmethod
    def _analysis(kind, lines, note, behaviour):
        """The kind's goals for the turns the analyzer was given, and, where the conversation shows the note it was sent
        in with as a turn of its own, the note's goal: an objection to how Assist behaved, in its own words, under the
        root goal it's about."""
        answer = deepcopy(kind.analysis)
        claims = {"met": "Assist missed it", "unmet": "Assist did it"}
        for goal in answer["goals"]:
            went = goal.pop("_went", None)
            if behaviour == OTHER_WAY:  # Its words are all it has to say so: the schema takes no verdict.
                goal["summary"] = f"{goal['summary']} ({claims.get(went, 'it went the other way')})"
        roots = [g["turn"] for g in answer["goals"] if g.get("serves") is None]
        about = kind.note_about if kind.note_about in roots else (roots or [None])[0]
        if note:
            turn = next((line["turn"] for line in lines if line["user_says"].strip() == note), None)
            if turn is not None:
                answer["goals"].append({"turn": turn, "summary": f"The note: {note}", "kind": "conduct", "about": "all",
                                        "quote": note, "serves": about, "_note": True})
        for line in lines:  # Each revision note: an objection to how Assist behaved, under a goal its improvement fixed.
            if revised := line.get("revision_of"):
                answer["goals"].append({"turn": line["turn"], "summary": f"Revision: {line['user_says']}",
                                        "kind": "conduct", "about": "all", "quote": line["user_says"],
                                        "serves": revised["for_goals_at_turns"][0], "_revision": True})
        return answer

    @staticmethod
    def _wrong_revision(answer, behaviour, first):
        """The answer as a revision behaviour makes it wrong, the first time it reads the revision note, or None."""
        if not first or behaviour not in REVISION_WRONG_FIRST:
            return None
        goals = answer["goals"]
        if behaviour == "first leaves the revision note without a goal":
            return {**answer, "goals": _plain(g for g in goals if not g.get("_revision"))}
        fixed = {g["serves"] for g in goals if g.get("_revision")}
        other = next(g["turn"] for g in goals if g.get("serves") is None and g["turn"] not in fixed)
        return {**answer, "goals": _plain({**g, "serves": other} if g.get("_revision") else g for g in goals)}

    @staticmethod
    def _wrong(kind, answer, behaviour, first):
        """The answer as the behaviour makes it wrong, or None when it answers right this time."""
        strip, goals = _plain, answer["goals"]
        right = {**answer, "goals": strip(goals)}
        if behaviour == WITHOUT_A_FIELD:
            return {k: v for k, v in right.items() if k != "goals"}
        if behaviour == ALWAYS_NO_THANKS or (first and behaviour == "first leaves the thanks without a goal"):
            return {**right, "goals": strip(g for g in goals if g["turn"] != kind.thanks)}
        if not first or behaviour not in WRONG_FIRST:
            return None
        if behaviour == "first leaves the correction without a goal":
            return {**right, "goals": strip(g for g in goals if g["turn"] != kind.correction)}
        if behaviour == "first leaves the note without a goal":
            return {**right, "goals": strip(g for g in goals if not g.get("_note"))}
        if behaviour == "first paraphrases the note instead of quoting it":
            return {**right, "goals": strip({**g, "quote": "Do better next time."} if g.get("_note") else g
                                            for g in goals)}
        if behaviour == "first points the note's objection at a turn outside its goal's tree":
            [note] = [g for g in goals if g.get("_note")]
            tree = {note["serves"]} | {g["turn"] for g in goals if g.get("serves") == note["serves"]}
            outside = [t for t in range(1, note["turn"]) if t not in tree] or [note["turn"]]
            return {**right, "goals": strip({**g, "about": outside[0]} if g is note else g for g in goals)}
        if behaviour == "first makes the thanks a root goal of its own":
            return {**right, "goals": strip({k: v for k, v in g.items() if k != "serves" or g["turn"] != kind.thanks}
                                            for g in goals)}
        if behaviour == "first makes the note a root goal of its own":
            return {**right, "goals": strip({k: v for k, v in g.items() if k != "serves" or not g.get("_note")}
                                            for g in goals)}
        if behaviour == "first hangs a sub-goal from a goal that isn't in the analysis":
            wrong, hung = strip(goals), False
            for g in wrong:
                if g.get("serves") is not None and not hung:
                    g["serves"], hung = 0, True  # No goal of the analysis starts at turn 0.
            return {**right, "goals": wrong}
        if behaviour == "first makes one goal per door":  # The doors asked for at once, each a root goal.
            first_turn = goals[0]["turn"]
            return {**right, "goals": strip({k: v for k, v in g.items() if k != "serves" or g["turn"] != first_turn}
                                            for g in goals)}
        # "first makes the opening and the later closing one goal": one goal through both, the later request under it.
        tops = [g for g in goals if g.get("serves") is None]
        opening, closing = tops[0], tops[1]
        merged = []
        for g in strip(goals):
            if g["turn"] == opening["turn"] and g.get("serves") is None:
                g = {"turn": g["turn"], "summary": f"{opening['summary']}, then {closing['summary'].lower()}",
                     "kind": "sequence", "entity_id": opening["entity_id"],
                     "states": [opening["state"], closing["state"]]}
            elif g["turn"] == closing["turn"] and g.get("serves") is None or g.get("serves") == closing["turn"]:
                g = {**g, "serves": opening["turn"]}
            merged.append(g)
        return {**right, "goals": merged}

    async def _mark_duplicate(self, hass, talk, tools):
        """A conversation whose trouble another conversation's fix also removes: once that fix is an improvement,
        its goal is marked as already fixed by it."""
        if not talk.kind.covered_by or "list_improvements" not in tools or "mark_duplicate" not in tools:
            return
        covering = await self._covering(hass, talk, tools)
        if covering:
            await self.use(hass, tools["mark_duplicate"], {
                "goal_turn": 1, "improvement": covering,
                "why": "Its script presses the relay in one go, which is what went wrong here."})

    async def _covering(self, hass, talk, tools):
        """The existing improvement whose fix also removes this conversation's trouble, if any, of those it's shown:
        the product leaves the conversation's own out (P0.12)."""
        if not talk.kind.covered_by or "list_improvements" not in tools:
            return None
        fixes = versions_of(talk.fix.script)
        for item in (await self.use(hass, tools["list_improvements"])).get("improvements") or []:
            if any(c.get("kind") == "script" and fixes(c.get("id")) for c in item["changes"]):
                return item["improvement"]
        return None

    # Propose, with its stamps

    async def hypothesis_generator(self, hass, payload, tools):
        """Ideas for the conversation's trouble, each with the reviewer's approval (answering blocking comments) and
        Simulate's test while the round has tests; sends on its first validated idea, or as its behaviour says. A goal
        another conversation's improvement already fixes is already addressed."""
        behaviour = self.behaviours.get("Propose")
        if behaviour == CUTS_OFF and self.first_run(("Propose", "cut")) and self.once(("Propose", "cut")):
            await self.cut()
        upfront = [scenario["session"] for scenario in payload.get("friction_scenarios", [])]
        await self._look_over(hass, tools, "Propose", upfront, held=upfront)
        target, addressed, trouble = None, [], []
        for scenario in payload.get("friction_scenarios", []):
            said = next(t["user"] for t in scenario["turns"] if t["turn"] == 1)
            talk = self.conversations.get(said)
            turns = list(scenario["goals_with_friction"])
            trouble += [(scenario["session"], t) for t in turns]
            if talk is None or talk.fix is None or not turns:
                continue
            if covering := await self._covering(hass, talk, tools):
                addressed += [{"session": scenario["session"], "goal_turn": t, "improvement": covering,
                               "why": "Its script presses the relay in one go, which is what went wrong here."}
                              for t in turns]
            elif target is None:
                target = (talk, [{"session": scenario["session"], "goal_turn": t} for t in turns])
        sent = await self._ideas(hass, tools, payload, *target, behaviour) if target else []
        if behaviour == WITHOUT_A_FIELD:  # Write up reads each idea's expected effect.
            sent = [{k: v for k, v in idea.items() if k != "expected"} for idea in sent]
        placed = {(g["session"], g["goal_turn"]) for idea in sent for g in idea["goals"]} | {
            (a["session"], a["goal_turn"]) for a in addressed}
        unfixed = [{"session": session, "goal_turn": turn, "why": "No idea for it was validated."}
                   for session, turn in trouble if (session, turn) not in placed]
        answer = {"hypotheses": sent, **({"already_addressed": addressed} if addressed else {}),
                  **({"not_fixed": unfixed} if unfixed else {})}
        if behaviour == ASKS and sent and not payload.get("answered"):
            answer = {"hypotheses": [], "question_for_user": ASKED}
        if behaviour == RETYPES and sent:
            # The first time it sends them on, each script's description gains a trailing space; told what that
            # version lacks, it sends the tested one.
            retyped = [{**idea, "changes": [_described(c, " ") for c in idea["changes"]]} for idea in sent]
            if (await self.use(hass, tools["submit_hypotheses"], {**answer, "hypotheses": retyped})).get("accepted"):
                return "Submitted."
        accepted = await self.use(hass, tools["submit_hypotheses"], answer, slip=behaviour == WITHOUT_A_FIELD)
        if behaviour == TWO_AT_ONCE and len(sent) > 1 and not accepted.get("accepted"):  # Told to send on one idea:
            accepted = await self.use(hass, tools["submit_hypotheses"], {**answer, "hypotheses": sent[:1]})  # the first.
        if behaviour not in (WITHOUT_A_FIELD, TESTS_TWO_SENDS_UNTESTED):  # Told its idea lacks a test, it stops there.
            assert accepted.get("accepted"), accepted
        if (decision := DECIDED_AS_IT_FINISHES.get(behaviour)) and payload.get("sent_back"):
            self._decides(hass, payload["sent_back"]["improvement"]["improvement"], decision)
        return "Submitted."

    def _decides(self, hass, improvement_id, decision):
        """The household decides on an improvement in the panel, its click landing now: Home Assistant handles it beside
        the run, as the stand-in finishes. Its write to the improvements store takes a moment, as on a slow disk, so the
        decision is still under way when the proposer returns."""
        from custom_components.assist_improver import panel
        world = self._world()
        store = world.runtime.improvements.store

        async def slowly(data):  # The next write only (a decline's own): the store writes as usual after it.
            del store.async_save
            await asyncio.sleep(0.3)
            return await store.async_save(data)

        async def decides():
            store.async_save = slowly
            return await world.ws(panel.ws_decide, type="assist_improver/decide", improvement_id=improvement_id,
                                  decision=decision, note="")
        hass.async_create_task(decides())

    async def _ideas(self, hass, tools, payload, talk, goals, behaviour):
        """The ideas it sends on for one conversation's goals in trouble."""
        fix, version = talk.fix, self._version(payload, talk.fix)

        def idea(take):
            return self._idea(talk, fix, goals, take, version, behaviour)
        testing = "test_idea" in tools
        if behaviour in (CHANGES_AFTER_APPROVAL, UNDER_ANOTHER_TITLE, BETTER_SAME_TITLE):
            return await self._reworded(hass, tools, idea(0), idea(1), behaviour)
        if behaviour == TESTS_MANY_AT_ONCE:  # Four ideas, two of them with the same changes: more than its tests.
            ideas = [idea(0), idea(1), idea(2), {**idea(0), "title": f"{idea(0)['title']}, said again"}]
            approved = [i for i in ideas if await self._approved(hass, tools, i, behaviour)]
            tests = await self._tests_at_once(hass, tools, approved)
            return [i for i, t in zip(approved, tests) if t.get("validated")][:1]
        if behaviour == ASKS_MANY_AT_ONCE:  # More ideas than the round has reviews, all asked about in one message.
            ideas = [idea(take) for take in range(REVIEWS + 1)]
            reviews = await self.use_many(hass, [(tools["ask_reviewer"], i) for i in ideas])
            for current in [i for i, r in zip(ideas, reviews) if r.get("outcome") == "approve"]:
                if (await self._test(hass, tools, current)).get("validated"):
                    return [current]
            return []
        if behaviour in (TWO_AT_ONCE, TESTS_TWO_SENDS_UNTESTED, TESTS_TWO_AT_ONCE):
            ideas = [idea(0), idea(1)]
            approved = [i for i in ideas if await self._approved(hass, tools, i, behaviour)]
            if behaviour == TESTS_TWO_AT_ONCE and testing:
                tests = await self._tests_at_once(hass, tools, approved)
            else:
                tests = [await self._test(hass, tools, i) for i in approved] if testing else []
            if behaviour == TESTS_TWO_SENDS_UNTESTED:
                third = idea(2)
                await self._ask(hass, tools, third)  # Reviewed, so it can be sent on; never tested.
                return [third]
            validated = [i for i, t in zip(approved, tests) if t.get("validated")] if testing else approved
            return validated if behaviour == TWO_AT_ONCE else validated[:1]
        best = None
        for take in range(IDEAS):
            current = idea(take)
            if behaviour == SLIPS and self.once(("Propose", "slip")):
                await self._slip(hass, tools, current)
            approved = await self._approved(hass, tools, current, behaviour)
            if approved is None:  # The reviewer can't be asked any more.
                break
            if not approved and behaviour != BOTH_STAMPS:
                continue
            if not testing:
                return [current]
            test = await self._test(hass, tools, current)
            if "error" in test:
                if "used up" in test["error"]:
                    break
                continue  # Not approved, so not tested.
            if test.get("validated"):
                best = current
                if behaviour != TRIES_BETTER:
                    break
            if not test.get("tests_left") and behaviour != KEEPS_TESTING:
                break
        return [best] if best else []

    @staticmethod
    def _version(payload, fix):
        """Which version of its fix this proposal is: one more than the household's revisions of it (a revision, or a
        conflict sent back), so each answers with changed changes."""
        def ours(title):
            return title == fix.title or any(title.startswith(f"{fix.title}, {word}")
                                             for word in ("take ", "version ", "better take "))
        return 1 + sum(1 for d in payload.get("household_decisions") or []
                       if ours(d.get("title") or "") and (d.get("decision") == "revise" or d.get("status") == "conflict"))

    @staticmethod
    def _idea(talk, fix, goals, take, version, behaviour):
        better = behaviour == TRIES_BETTER and take
        title = fix.title + (f", {'better take' if better else 'take'} {take + 1}" if take else "") + (
            f", version {version}" if version > 1 else "")
        script = fix.script + (f" take {take + 1}" if take else "") + (f" v{version}" if version > 1 else "")
        steps = fix.breaks if behaviour == BREAKS and fix.breaks else fix.steps
        text = f"{fix.text} For everyone in the household." if behaviour == WHOLE_HOUSEHOLD else fix.text
        change = {"kind": "script", "id": slugify(script), "config": {
            "alias": script, "description": text,
            "sequence": [{"action": service, "target": {"entity_id": entity}, **({"data": data[0]} if data else {})}
                         for service, entity, *data in steps] + [{"stop": "Done."}],
            **({"fields": {f["key"]: {"name": f["name"], "description": f["description"], "required": True,
                                      "selector": {"text": {}}} for f in fix.fields}} if fix.fields else {})}}
        return {"title": title, "cause": fix.cause, "goals": goals,
                "expected": "It makes every goal of the conversation easier" if behaviour == CLAIMS else fix.expected,
                "support": [{"session": goals[0]["session"], "turn": goals[0]["goal_turn"],
                             "note": "The user had to step in here"}],
                "conflicts": [], "changes": [change]}

    async def _reworded(self, hass, tools, first, other, behaviour):
        """An approved idea it then changes: tested and sent on with changed changes without asking again; sent on
        under another approved idea's title once validated; or, once validated, bettered under its own title, the
        better version going on only with both stamps of its own."""
        if not await self._approved(hass, tools, first, behaviour):
            return []
        changed = {**first, "changes": [_described(c, " It says when it's done.") for c in first["changes"]]}
        if behaviour == CHANGES_AFTER_APPROVAL:
            return [changed] if (await self._test(hass, tools, changed)).get("validated") else []
        if behaviour == UNDER_ANOTHER_TITLE:
            approved = await self._approved(hass, tools, other, behaviour)  # Approved, and never tested.
            validated = (await self._test(hass, tools, first)).get("validated")
            return [{**other, "changes": first["changes"]}] if approved and validated else []
        if not (await self._test(hass, tools, first)).get("validated"):
            return []
        if await self._approved(hass, tools, changed, behaviour):
            if (await self._test(hass, tools, changed)).get("validated"):
                return [changed]
        return [first]

    async def _slip(self, hass, tools, idea):
        """One call off its tool's schema for each stamp: a goal's turn as text, then the changes as one JSON string."""
        await self.use(hass, tools["ask_reviewer"], {
            **idea, "goals": [{**g, "goal_turn": str(g["goal_turn"])} for g in idea["goals"]]}, slip=True)
        await self.use(hass, tools["test_idea"], {**idea, "changes": json.dumps(idea["changes"])}, slip=True)

    async def _ask(self, hass, tools, idea):
        """ask_reviewer; a run the Review behaviour cuts off once its first review is back is cut off here, and a
        proposer that fails after its first review fails here."""
        review = await self.use(hass, tools["ask_reviewer"], idea)
        if (self.behaviours.get("Review") == CUT_AFTER_REVIEW and "error" not in review
                and self.first_run(("Review", "cut")) and self.once(("Review", "cut"))):
            await self.cut()
        if (self.behaviours.get("Propose") == FAILS_AFTER_REVIEW and "error" not in review
                and self.once(("Propose", "fail"))):
            raise RuntimeError("The model is overloaded")
        return review

    async def _approved(self, hass, tools, idea, behaviour):
        """Propose ⇄ Review: asks, answers each blocking comment, asks again: whether the reviewer approved, or None
        when it can't be asked any more (its budget is spent, or it stopped). Reviews are the round's: on a round
        picked up again (a retry, or a run that releases it), changes its latest review of them approved aren't asked
        about again."""
        judged = [r["verdict"] for r in tools["ask_reviewer"].reviews if r["hypothesis"]["changes"] == idea["changes"]]
        if judged[-1:] == ["approve"]:
            return True
        review = await self._ask(hass, tools, idea)
        for _ in range(ASKS_PER_IDEA - 1):
            if "error" in review or review.get("outcome") != "request_changes" or behaviour == BOTH_STAMPS:
                break
            responses = [{"comment": c["comment"], "resolution": "resolved", "note": "Kept to what was asked"}
                         for c in review["comments"] if "blocking" in c["decorations"]]
            review = await self._ask(hass, tools, {**idea, "responses": responses})
        if "error" in review:
            return None
        return review.get("outcome") == "approve"

    async def _test(self, hass, tools, idea):
        """test_idea; a run the Simulate behaviour cuts off once its first test is done is cut off here."""
        test = await self.use(hass, tools["test_idea"], idea)
        self.tested.setdefault(idea["goals"][0]["session"], []).append(test)
        if (self.behaviours.get("Simulate") in (CUT_AFTER_TEST, CUT_AFTER_ONE_TEST) and "error" not in test
                and self.first_run(("Simulate", "cut")) and self.once(("Simulate", "cut"))):
            await self.cut()
        return test

    async def _tests_at_once(self, hass, tools, ideas):
        """test_idea for each idea, all in one message: Home Assistant runs them at once."""
        self.testing = len(ideas)
        try:
            return await self.use_many(hass, [(tools["test_idea"], i) for i in ideas])
        finally:
            self.testing = 0

    async def hypothesis_reviewer(self, hass, payload, tools):
        """Reviews each draft as its behaviour says; at the first, it looks over the household."""
        behaviour = self.behaviours.get("Review")
        seen, titles, firsts, looked = set(), [], {}, False
        while (draft := await self._next_draft(hass, tools, behaviour)) is not None:
            self.received.append(("Review", self.current_run(), deepcopy(draft)))
            if behaviour == CUT_ON_FIRST_IDEA and self.once(("Review", "cut")):
                await self.cut()  # The run's own cancellation then ends this reviewer too.
                await asyncio.sleep(30)
            if not looked:
                looked = await self._look_over(hass, tools, "Review", [g["session"] for g in draft["goals"]])
            if draft["title"] not in titles:
                titles.append(draft["title"])
            first_idea = draft["title"] == titles[0]
            first_time = draft["title"] not in seen and not draft.get("responses")
            seen.add(draft["title"])
            if behaviour in ("refuses every idea", "asks for changes to every idea", CUT_AFTER_REVIEW):
                blocking = True
            elif behaviour == "asks for changes to the first idea only":
                blocking = first_idea and first_time
            elif behaviour == "approves only the first idea":
                blocking = not first_idea
            elif behaviour == FIRST_VERSION_ONLY:  # Of each title, only the changes it was first asked about.
                changes = json.dumps(draft["changes"], sort_keys=True)
                blocking = firsts.setdefault(draft["title"], changes) != changes
            else:  # As it is: every idea does only what was asked, so it approves at once.
                blocking = False
            comment = ({"label": "issue", "decorations": ["blocking"], "subject": "Make sure it does only what was asked",
                        "evidence": [{"session": draft["goals"][0]["session"], "turn": 1}]} if blocking else
                       {"label": "praise", "decorations": ["non-blocking"], "subject": "Just what was asked"})
            assert (await self.use(hass, tools["submit_review"], {"comments": [comment]})).get("accepted")
            self.reviews.append((self.current_run(), deepcopy(draft), "request_changes" if blocking else "approve"))
        return "Finished."

    async def _look_over(self, hass, tools, stage, about, held=()):
        """The household as a whole, through the session tools the role was handed: each session list_sessions shows
        it, every page, read with read_session unless its input holds it already (held); logged for the conversations
        it works on (about). True once done."""
        read, page, pages = {}, 1, 1
        while "list_sessions" in tools and page <= pages:
            listed = await self.use(hass, tools["list_sessions"], {"page": page})
            page, pages = page + 1, listed["pages"]
            for row in listed["sessions"]:
                if "read_session" in tools and row["session"] not in held:
                    read[row["session"]] = await self.use(hass, tools["read_session"], {"session": row["session"]})
        self.read.append((stage, self.current_run(), set(about), read))
        return True

    async def _next_draft(self, hass, tools, behaviour):
        """The next draft, or None once it's told it's finished. A reviewer that waits for two drafts at once calls
        wait_for_draft twice in one message (the draft is in whichever answer has it); one that waits again calls it
        once more after it's told it's finished."""
        wait = tools["wait_for_draft"]
        while True:
            got = await self.use_many(hass, [(wait, {})] * (2 if behaviour == TWO_WAITS else 1))
            if drafts := [answer["draft"] for answer in got if "draft" in answer]:
                return drafts[0]
            if any("finished" in answer for answer in got):
                if behaviour == WAITS_AFTER_FINISHED:
                    await self.use(hass, wait)
                return None

    async def feedback_check(self, hass, payload, tools):
        """The judge: an Assist turn shows what the household objected to when its reply is one the kind says shows
        that objection (Kind.shows), else unless it's just "Done."."""
        kind = self.conversations[payload["replay"][0][1]["user_said"]].kind
        replies = {turn[0]["turn"]: turn[3]["assistant_replied"] for turn in payload["replay"]}
        answers = []
        for objection in payload["objections"]:
            showing = kind.shows.get(objection["quote"])
            for n in objection["judge"]:
                shows = replies[n] in showing if showing is not None else replies[n] != "Done."
                answers.append({"goal": objection["goal"], "turn": n, "shows": shows,
                                "why": "It still does that." if shows else "It doesn't do that."})
        return {"answers": answers}

    # Write up

    async def editor(self, hass, payload, tools):
        accepted = await self.use(hass, tools["submit_copy"], {
            "headline": "Get it done in one go",
            "benefit": "Ask once and it's done as you asked, so you don't have to ask again."})
        assert accepted.get("accepted"), accepted
        return "Written."


class Assistant:
    """The household's assistant in replays: answers each request as its conversation's kind says, and as Simulate's
    behaviour makes its replays go. A replay runs on the household's setup as it is now (DETAILS P6.2.4), so a script
    it's offered is a change under test only when the household has no such script (hass: its Home Assistant)."""

    def __init__(self, client, conversations, roles, hass):
        self.client, self.conversations, self.roles, self.hass = client, conversations, roles, hass
        self.fixes = {}  # {conversation id: the changes' scripts offered to it, in the order first offered}

    async def answer(self, request):
        """A request as the provider gets it (its body): the blocks of its reply."""
        blocks = await self.reply(request)
        self.roles.received.append(("Simulate", self.roles.current_run(), deepcopy(request)))  # Answered.
        return blocks

    async def reply(self, request):
        behaviour = self.roles.behaviours.get("Simulate")
        if behaviour == "crashes every time":
            raise RuntimeError("The model is overloaded")
        if behaviour == CUT_ON_FIRST_IDEA and self.roles.once(("Simulate", "cut")):
            await self.roles.cut()
        if behaviour == CUT_WHILE_TWO_RUN:
            # Cut off at the first replay asked for while two tests run; in that run, nothing more is answered until
            # the run has ended.
            if self.roles.testing >= 2 and self.roles.once(("Simulate", "cut")):
                await self.roles.cut()
            if self.roles.cut_in(self.roles.current_run()):
                while any(task.get_name() == "assist_improver_pipeline" and not task.done()
                          for task in asyncio.all_tasks()):
                    await asyncio.sleep(0.01)
        messages = request.get("messages") or []
        said = [self._text(m) for m in messages if m.get("role") == "user" and self._text(m) is not None]
        talk = self.conversations[said[0]]
        turn, answering = self._turn(talk, said), self._answers_tool(messages[-1]) if messages else False
        from custom_components.assist_improver.snapshot import _definitions
        recorded = {f"script__{oid}" for oid in {**(recording_turns(talk.record)[0]["source"].get("scripts") or {}),
                                                 **_definitions(self.hass)[0]}}
        offered = sorted(name for t in request.get("tools") or [] if (name := t.get("name", "")).startswith("script__")
                         and name not in recorded)
        if offered:  # A change under test: a script the household's Assist doesn't have.
            fix = offered[0]
            seen = self.fixes.setdefault(talk.id, [])
            if fix not in seen:
                seen.append(fix)
                if (behaviour == CUT_IN_SECOND_TEST and len(seen) == 2 and self.roles.first_run(("Simulate", "cut"))
                        and self.roles.once(("Simulate", "cut"))):
                    await self.roles.cut()
            if behaviour == ASKS_NEW:  # Asks before it acts, every time; nobody answers for the user.
                return [{"type": "text", "text": "Before I do that: should it stay that way afterwards?"}]
            if self._uses(talk, fix, seen, behaviour):
                [tool] = [t for t in request["tools"] if t.get("name") == fix]
                return self._fixed(talk, turn, fix, answering, (tool.get("input_schema") or {}).get("required") or [])
        elif (unlike := self._unlike(talk, turn, answering, behaviour)) is not None:
            return unlike
        return self._recorded(talk, turn, answering)

    @staticmethod
    def _turn(talk, said):
        """The recorded turn the latest words said are: a replay with the change may leave turns out, so the words
        said, in order, are walked through the recording's."""
        texts = [turn["messages"][0]["content"] for turn in recording_turns(talk.record)]
        number = 0
        for words in said:
            number = texts.index(words, number) + 1
        return number

    @staticmethod
    def _uses(talk, fix, seen, behaviour):
        """Whether, replayed with this change, Assist acts as the kind says the change makes it act."""
        if not talk.kind.fix_works or behaviour in NOT_VALIDATING:
            return False
        if behaviour == "doesn't validate the first idea it tests":
            return fix != seen[0]
        if behaviour == "validates only the first idea":
            return fix == seen[0]
        return True

    def _fixed(self, talk, turn, fix, answering, inputs=()):
        """inputs: those the fix's tool asks for, which Assist gives as the kind says."""
        action = talk.kind.fixed.get(turn)
        if action == "fix":
            return ([{"type": "text", "text": "Done."}] if answering else
                    [{"type": "tool_use", "id": f"fix-{turn}", "name": fix,
                      "input": {k: v for k, v in talk.fix.asks.items() if k in inputs}}])
        if action == "ask":
            return [{"type": "text", "text": "Just that one, and nothing else?"}]
        return self._recorded(talk, turn, answering)

    def _unlike(self, talk, turn, answering, behaviour):
        """A replay without the change that doesn't go as the conversation went, as the behaviour says; None when it
        goes as it went."""
        if behaviour == GOES_UNLIKE:
            return [{"type": "text", "text": "Sorry, I can't do that right now."}]
        turns = recording_turns(talk.record)
        if behaviour == EXTRA_TURN_GONE and turn == 1:  # Everything its goal took, in its first turn.
            calls = [c for t in turns for c in t["calls"]]
        elif behaviour == SIDE_EFFECT_GONE:  # Leaves the devices it changed unasked alone.
            calls = [c for c in turns[turn - 1]["calls"]
                     if not any(e in talk.kind.side_effects for e in self._targets(c))]
            if calls == turns[turn - 1]["calls"]:
                return None
        else:
            return None
        if not calls:
            return [{"type": "text", "text": "Which one do you mean?"}]
        return self._calls(calls, turn, answering)

    @staticmethod
    def _targets(call):
        """The devices a recorded call names (by their names, as the household's devices are named)."""
        from .corpus import HOUSE
        name = (call.get("tool_args") or {}).get("name")
        return [e for e, (_, named, _) in HOUSE.items() if named == name]

    def _recorded(self, talk, turn, answering):
        """As it went: the turn's tool calls, then its reply."""
        recorded = recording_turns(talk.record)[turn - 1]
        return self._calls(recorded["calls"], recorded["number"], answering)

    @staticmethod
    def _calls(calls, number, answering):
        if calls and not answering:
            return [{"type": "thinking", "thinking": "Synthetic reasoning.",
                     "signature": f"synthetic-signature-{number}"}] + [
                {"type": "tool_use", "id": c["id"], "name": c["tool_name"], "input": c["tool_args"]} for c in calls]
        return [{"type": "text", "text": f"Generated response {number}."}]

    @staticmethod
    def _text(message):
        content = message.get("content")
        if isinstance(content, str):
            return content
        texts = [b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        return texts[0] if texts and not any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content) else None

    @staticmethod
    def _answers_tool(message):
        content = message.get("content")
        return message.get("role") == "user" and isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
