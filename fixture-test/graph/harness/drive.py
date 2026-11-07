"""How the household's actions and readers reach today's product: through the panel's own commands and the records
the integration keeps, and, for what the product can't be its own witness to, the stand-ins' own log (reviews,
received, reached). Nothing here works out what the product should have recorded: a reader the product can't answer
raises Missing, naming what's absent, so a red test points at the product, not at the driver. Ideas, tests and
improvements are matched by their changes, never by title."""

from copy import deepcopy

from household import Missing

from custom_components.assist_improver.cases import digest, recording_turns

STAGE_KEYS = {"Triage": "triage", "Analyze": "analyze", "Propose": "propose", "Simulate": "test", "Write up": "write"}
# The outcome each Your household chip names, in P0.12's words (DETAILS P14.3.10-16: Looked smooth, No trouble found,
# Improved, Declined, No fix found, Marked as a test, Couldn't be read).
OUTCOME_CHIPS = {"smooth": "looked smooth", "fine": "no trouble found", "nofix": "no fix found", "improved": "improved",
                 "declined": "declined", "test": "marked as a test", "unreadable": "couldn't be read",
                 "archived": "archived", "duplicate": "duplicate"}
# The stages each chip a screen shows a conversation with names (DETAILS P14.3): Still going is at Your Assist, New
# and Sent in at Your Assist or Triage; each outcome in Your household has a chip of its own.
CHIPS = {"active": ("Your Assist",), "waiting": ("Your Assist", "Triage"), "sent": ("Your Assist", "Triage"),
         "queued": ("Analyze",), "working": ("Propose",), "writing": ("Write up",), "review": ("Your review",),
         "unclear": ("Your review",), **dict.fromkeys(OUTCOME_CHIPS, ("Your household",))}


class Record:
    """A record the product keeps, read by field; a field it doesn't keep raises Missing, named."""

    def __init__(self, name, data):
        self._name, self._data = name, data

    def __getattr__(self, field):
        if field.startswith("_"):
            raise AttributeError(field)
        if field not in self._data:
            raise Missing(f"{self._name}.{field}: not kept")
        return self._data[field]

    def __eq__(self, other):
        return isinstance(other, Record) and other._data == self._data

    def __repr__(self):
        return f"{self._name}({self._data!r})"


class StageRecord(Record):
    """One stage's record in one round of a conversation's, as it was when read: equal to another reading of the same
    one that reads the same."""

    def __init__(self, rid, round, stage, data):
        super().__init__(f"records[{stage!r}]", data)
        self._key = (rid, round, stage)

    def __getattr__(self, field):
        if field == "turns" and self._key[2] == "Analyze" and "analysis" in self._data:  # As Tree.turns.
            return Tree(self._key[0], self._data["analysis"]).turns
        return super().__getattr__(field)

    def __eq__(self, other):
        return isinstance(other, StageRecord) and (other._key, other._data) == (self._key, self._data)

    def __hash__(self):
        return hash(self._key)


class Hold(Record):
    def __init__(self, data):
        super().__init__("Hold", data)

    def __bool__(self):
        return True


class Budget:
    def __init__(self, data):
        self.id, self.cap = data["budget"], data["cap"]

    def __eq__(self, other):
        return isinstance(other, Budget) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        return f"Budget({self.id!r}, cap={self.cap})"


class Place:
    """Where a conversation is: its one record (flow.Flow.place)."""

    def __init__(self, drive, data):
        self.stage, self.round, self.outcome, self.archived = (data["stage"], data["round"], data["outcome"],
                                                               data["archived"])
        self.held = Hold(data["held"]) if data["held"] else None
        self._covered = data["covered_by"]
        self._drive = drive

    @property
    def covered_by(self):
        if self._covered is None:
            return None
        found = [i for i in self._drive.improvements() if i.id == self._covered]
        return found[0] if found else self._covered

    def _key(self):
        return (self.stage, self.round, self.outcome, self.archived, self.held and self.held._data, self._covered)

    def __eq__(self, other):
        return isinstance(other, Place) and other._key() == self._key()

    def __repr__(self):
        return f"Place({self.stage!r}, round={self.round}, held={self.held!r}, outcome={self.outcome!r})"


class TreeGoal:
    """One goal of a conversation's analysis: its state (met, unmet or indeterminate), the goal it serves, its root,
    its kind, and for a conduct goal what it's about and the household's words."""

    def __init__(self, tree, key, data):
        self._tree, self.key, self._data = tree, key, data
        reached = data.get("reached")
        self.state = "met" if reached is True else "unmet" if reached is False else "indeterminate"
        self.kind, self.about, self.quote = data.get("kind"), data.get("about"), data.get("quote")

    @property
    def parent(self):
        serves = self._data.get("serves")
        if serves is None:
            return None
        there = [g for g in self._tree.goals if g._data.get("turn") == serves and g is not self]
        return next((g for g in there if g._data.get("serves") is None), there[0] if there else None)

    @property
    def root(self):
        goal, seen = self, set()
        while goal.parent is not None and goal.key not in seen:
            seen.add(goal.key)
            goal = goal.parent
        return goal

    def __eq__(self, other):
        return isinstance(other, TreeGoal) and (other._tree.rid, other.key) == (self._tree.rid, self.key)

    def __hash__(self):
        return hash((self._tree.rid, self.key))

    def __repr__(self):
        return f"Goal({self.key}, {self._data.get('summary')!r}, {self.state})"


class Turn:
    """key: its number for what was said, "note", or "revision:<round>"."""

    def __init__(self, tree, key, goals, said=None):
        self._tree, self.key, self.goals, self.said = tree, key, goals, said
        self.number = key if isinstance(key, int) else key.split(":")[0]

    def __eq__(self, other):
        return isinstance(other, Turn) and (other._tree.rid, other.key) == (self._tree.rid, self.key)

    def __hash__(self):
        return hash((self._tree.rid, self.key))

    def __repr__(self):
        return f"Turn({self.key})"


class Tree:
    """A conversation's analysis, as the model reads it: turns, the goals each maps to, and the goals' tree. Its turns
    are what was said, then the note it was sent in with and each revision note, if any."""

    def __init__(self, rid, analysis):
        from custom_components.assist_improver.measures import note_labels
        self.rid = rid
        self.goals = [TreeGoal(self, f"{g.get('turn')}:{i}", g) for i, g in enumerate(analysis.get("goals") or [])]
        self.roots = [g for g in self.goals if g.parent is None]
        said = int(analysis.get("turns") or max([g._data.get("turn") or 0 for g in self.goals] or [0]))
        self.turns = [Turn(self, n, [g for g in self.goals if g._data.get("turn") == n]) for n in range(1, said + 1)]
        for turn, label in sorted(note_labels({**analysis, "turns": said}).items()):  # Its notes, in order: the one
            self.turns.append(Turn(self, label, [g for g in self.goals if g._data.get("turn") == turn]))  # it was sent
            # in with, then each revision note.


class Question:
    def __init__(self, conversation, question):
        self.conversation, self.question = conversation, question

    def __eq__(self, other):
        return isinstance(other, Question) and other.conversation == self.conversation

    def __hash__(self):
        return hash(("question", self.conversation.id))


class Run:
    def __init__(self, record):
        self.record = record

    @property
    def started(self):
        return self.record["started"]

    def __eq__(self, other):
        return isinstance(other, Run) and other.record["id"] == self.record["id"]

    def __hash__(self):
        return hash(self.record["id"])


class Outcome:
    def __init__(self, run=None, error=None, asks=None):
        self.run, self.error = run, error
        self.asks = Record("Asks", asks) if asks else None


class Improvement:
    """One improvement, as the improvements store keeps it. Equal to another reading of the same improvement."""

    def __init__(self, drive, item):
        self._drive, self._item = drive, item
        self.id = item["id"]

    def __eq__(self, other):
        return isinstance(other, Improvement) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        return f"Improvement({self.id!r}, {self.status!r})"

    @property
    def status(self):
        return self._item["status"]

    @property
    def unrevised(self):
        """A revision of it whose round brought no new version, as its review says it: {note, why}, or None."""
        return self._item.get("unrevised")

    @property
    def changes(self):
        """What it changes (a digest: equal for the same changes, as Idea.changes and Test.changes)."""
        return digest(self._item["changes"])

    @property
    def idea(self):
        """The idea it was built from: the latest idea of its conversation's that reached Write up with its changes."""
        rid = self._item.get("conversation")
        if rid is None:
            raise Missing("Improvement.idea: this improvement belongs to no one conversation")
        found = [i for i in self._drive.ideas(self._drive._talk(rid))
                 if i.changes == self.changes and i.reached_write_up]
        if not found:
            raise Missing("Improvement.idea: no idea of its conversation's that reached Write up has its changes")
        return found[-1]

    @property
    def decisions(self):
        """The decisions its review page offers now."""
        from custom_components.assist_improver import panel
        world = self._drive._world
        _, page = world.call(world.ws(panel.ws_get, type="assist_improver/improvement", improvement_id=self.id))
        return [choice["value"] for choice in page["choices"]]

    def _review(self):
        """Its review page, as the panel gives it."""
        from custom_components.assist_improver import panel
        world = self._drive._world
        _, page = world.call(world.ws(panel.ws_get, type="assist_improver/improvement", improvement_id=self.id))
        return page

    @property
    def notes_motion(self):
        """Whether its review says motion isn't simulated."""
        return bool(self._review()["physical"])

    @property
    def tested_on(self):
        if "conversation" not in self._item:
            raise Missing("Improvement.tested_on: this improvement belongs to no one conversation")
        return {self._drive._talk(self._item["conversation"])}

    @property
    def conversation(self):
        if "conversation" not in self._item:
            raise Missing("Improvement.conversation: this improvement belongs to no one conversation")
        return self._drive._talk(self._item["conversation"])

    @property
    def test(self):
        if "test" not in self._item:
            raise Missing("Improvement.test: this improvement keeps no test")
        talk = self.conversation
        return Test(self._item["test"], [i for i in self._drive.ideas(talk) if i._key[1] == self._item["round"]],
                    self._drive.analysis(talk))

    @property
    def checked_on(self):
        if "checked_on" not in self._item:
            raise Missing("Improvement.checked_on: the product records no conversations a change was checked against")
        return {talk for talk in self._drive._talks() if talk.id[:8] in self._item["checked_on"]}

    def changes_beyond(self, conversation):
        """What its changes reach beyond that conversation: each change's reach, as the product records it."""
        if "reach" not in self._item:
            raise Missing("Improvement.changes_beyond: the product doesn't say what a change reaches")
        return [r for r in self._item["reach"] if r.get("applies_to") in ("household", "assistant", "person")]


def _goal(tree, key):
    """The tree's goal a record names by its key; Missing when the tree has none."""
    found = [g for g in tree.goals if g.key == key]
    if not found:
        raise Missing(f"goal {key}: not in the conversation's analysis")
    return found[0]


class Test:
    """One of Simulate's tests, as the proposer's round keeps it (and an improvement copies it)."""

    def __init__(self, data, ideas, tree):
        """ideas: its round's ideas, for the one it tested; tree: its conversation's analysis, for its goals."""
        self._data, self._ideas, self._tree = data, ideas, tree
        self.validated, self.changes = data["validated"], data["changes"]

    def __eq__(self, other):
        return isinstance(other, Test) and other._data == self._data

    def __hash__(self):
        return hash((self.changes, self._data.get("at")))

    @property
    def idea(self):
        """The idea of its round it tested: the one with its changes."""
        found = [i for i in self._ideas if i.changes == self.changes]
        if not found:
            raise Missing("Test.idea: no idea in its round's record has the changes it tested")
        return found[0]

    @property
    def judged(self):
        return {_goal(self._tree, key) for key in self._data["judged"]}

    @property
    def gains(self):
        return set(self._data["gains"])

    @property
    def before(self):
        return Replay(self._data["before"], self._tree)

    @property
    def after(self):
        return Replay(self._data["after"], self._tree)


class Replay:
    """One of a test's replays, as the test keeps it, read by the conversation's goals and turns."""

    def __init__(self, data, tree):
        self._data, self._tree = data, tree

    @property
    def goals(self):
        return {_goal(self._tree, key): state for key, state in self._data["goals"].items()}

    @property
    def goals_met(self):
        return {_goal(self._tree, key) for key in self._data["goals_met"]}

    @property
    def turns_per_root_goal(self):
        return {_goal(self._tree, key): n for key, n in self._data["turns_per_root_goal"].items()}

    @property
    def side_effects_left(self):
        return list(self._data["side_effects_left"])

    @property
    def said(self):
        turns = {t.key: t for t in self._tree.turns}
        if missing := [n for n in self._data["said"] if n not in turns]:
            raise Missing(f"Replay.said: turns {missing} aren't in the conversation's analysis")
        return [turns[n] for n in self._data["said"]]

    @property
    def started_from(self):
        return self._data["started_from"]

    @property
    def config(self):
        return self._data["config"]

    def easier_than(self, other):
        from custom_components.assist_improver.measures import easier
        return easier(self._data, other._data)


class Idea:
    """One of the proposer's ideas in a round, kept by its Propose record; equal to another reading of it."""

    def __init__(self, data, rid, round, drive=None):
        """Known by its changes (a digest, as Test.changes) in its conversation's round; titles: those it went by."""
        self.titles, self.changes = data["titles"], digest((data.get("hypothesis") or {}).get("changes"))
        self._data, self._key, self._drive = data, (rid, round, self.changes), drive

    @property
    def stamps(self):
        return [stamp for stamp, given in self._data["stamps"].items() if given]

    @property
    def reached_write_up(self):
        return self._data["sent_on"]

    def __eq__(self, other):
        return isinstance(other, Idea) and other._key == self._key

    def __hash__(self):
        return hash(self._key)

    def __repr__(self):
        return f"Idea({self.titles!r}, round {self._key[1]})"

    @property
    def given(self):
        """What the proposer had when it made the idea: as seen_by reads a stage's given, plus the tests back by then."""
        if self._drive is None or "given" not in self._data:
            raise Missing("Idea.given: the product doesn't keep what the proposer had when it made the idea")
        rid, number, _ = self._key
        talk = self._drive._talk(rid)
        tests = self._drive.tests(talk, round=number)
        return self._drive._given(talk, self._data["given"]) | {tests[n] for n in self._data["given"].get("tests", [])}


class Contract:
    """A handoff's contract, as the product types it (records.HANDOFFS): its fields, the ones that steer (a fixed
    set of values), and whether it takes a record, by the product's own check."""

    def __init__(self, kind):
        import typing
        self._kind, self._hints = kind, typing.get_type_hints(kind)
        self.fields = list(self._hints)
        self.steering = [f for f, t in self._hints.items() if typing.get_origin(t) is typing.Literal]

    def accepts(self, record):
        from custom_components.assist_improver.contracts import problems
        return not problems(record.data if isinstance(record, HandedOn) else record, self._kind)


class HandedOn:
    def __init__(self, data):
        self.data, self.fields = data, list(data)


class Handoff:
    """One move: the stage it left, the stage it entered, what it handed on and the contract that types it."""

    def __init__(self, data):
        from custom_components.assist_improver.records import HANDOFFS
        self.start, self.end = data["start"], data["end"]
        self.contract, self.record = Contract(HANDOFFS[data["contract"]]), HandedOn(data["record"])

    def without(self, field):
        return {k: v for k, v in self.record.data.items() if k != field}

    def mistyped(self, field):
        """The record with a value of the wrong kind in that field: a number where a text is allowed, else a text."""
        import typing
        kind = self.contract._hints.get(field)
        args = typing.get_args(kind) or (kind,)
        texts = str in args or (typing.get_origin(kind) is typing.Literal and all(isinstance(a, str) for a in args))
        return {**self.record.data, field: 0 if texts else "not this kind"}

    def outside(self, field):
        return {**self.record.data, field: "none of its values"}

    def __repr__(self):
        return f"Handoff({self.start} → {self.end})"


class Drive:
    """The implemented half of Household (household.Interface declares all of it)."""

    def __init__(self):
        from .world import World
        self._world = World()
        self._numbers = {}  # {kind: how many conversations of it so far}

    # Helpers

    @property
    def _runtime(self):
        return self._world.runtime

    def _talks(self):
        return list(self._world.conversations.values())

    def _latest_run(self):
        runs = self.runs()
        return runs[-1] if runs else None

    def _acting(self, act):
        """Does act (a coroutine), waits for what it started, and returns the run it started, if any."""
        before = self._latest_run()
        result = self._world.call(act)
        self._world.call(self._world.settle())
        after = self._latest_run()
        return result, (after if after != before else None)

    # Acting as the household

    def converse(self, kind, filed=False, ends=True):
        from .corpus import KINDS, recording
        if kind not in KINDS:
            raise Missing(f"harness: conversation kind {kind!r}")
        number = self._numbers[kind] = self._numbers.get(kind, 0) + 1
        talk = recording(KINDS[kind], number)
        going_on = filed or not ends
        self._world.call(self._world.arrives(talk, going_on))
        if filed:  # Filed from Assist while it goes on.
            self._world.call(self._world.files(talk))
        if going_on and ends:
            self.end(talk)
        return talk

    def end(self, *conversations):
        _, run = self._acting(self._world.ends(*conversations))
        return run

    def run(self, kind="full", picked=(), stage=None):
        from custom_components.assist_improver import panel
        if kind == "scheduled":  # 03:00 comes, and the household's daily schedule starts its run.
            self._world.call(self._world.at(3, 0))
        else:
            msg = {"type": "assist_improver/run/start", "confirmed": True}
            if kind == "picked":
                msg["conversations"] = [talk.id for talk in picked]
            if kind == "stage":
                msg["only"] = STAGE_KEYS[stage]
            status, result = self._world.call(self._world.ws(panel.ws_run_start, **msg))
            if status == "error":
                raise RuntimeError(f"The run was refused: {result}")
        self._world.call(self._world.settle())
        return self._latest_run()

    def send_in(self, conversation, note=None):
        from custom_components.assist_improver import panel
        (status, result), run = self._acting(self._world.ws(
            panel.ws_conversation_send, type="assist_improver/conversation/send", recording_id=conversation.id,
            note=note or ""))
        return run

    def answer(self, conversation, text):
        from custom_components.assist_improver import panel
        (status, result), run = self._acting(self._world.ws(
            panel.ws_answer, type="assist_improver/answer", recording_id=conversation.id, answer=text))
        if status == "error":
            raise RuntimeError(f"The answer was refused: {result}")
        return run

    def block(self, stage):
        self._pause(stage, True)

    def unblock(self, stage):
        self._pause(stage, False)

    def _pause(self, stage, pause):
        from custom_components.assist_improver.settings import updated
        entry = self._world.entry
        entry.options = updated(self._world.hass, entry, stage=STAGE_KEYS[stage], pause=pause)

    def decide(self, item, decision, note=None, reason=None):
        (status, result), run = self._acting(self._deciding(item, decision, note, reason))
        if status == "error":
            return Outcome(run=run, error=result)
        return Outcome(run=run, asks=result.get("asks"))

    def _deciding(self, item, decision, note=None, reason=None):
        from custom_components.assist_improver import panel
        msg = {"type": "assist_improver/decide", "improvement_id": item.id, "decision": decision, "note": note or ""}
        if reason is not None:
            msg["reason"] = reason.replace(" ", "_")  # "not an issue" is the product's not_an_issue.
        return self._world.ws(panel.ws_decide, **msg)

    def agree(self, outcome):
        (status, result), run = self._acting(self._agreeing(outcome))
        if status == "error":
            raise RuntimeError(f"Agreeing was refused: {result}")
        return run

    def _agreeing(self, outcome):
        from custom_components.assist_improver import panel
        return self._world.ws(panel.ws_conflict_resolve, type="assist_improver/conflict/resolve",
                              improvement_id=outcome.asks.improvement_id)

    def meanwhile(self, stage, act, *args):
        """The household does act (decide or agree, with those arguments) while that stage's stand-in works, the next
        time it's called: in a run, so the act meets that run in progress."""
        acts = {self.decide: self._deciding, self.agree: self._agreeing}
        if act not in acts:
            raise Missing(f"harness: meanwhile({stage!r}, {getattr(act, '__name__', act)!r}, ...)")
        self._world.roles.meanwhile[stage] = lambda: acts[act](*args)

    def edit_home(self, improvement):
        """The household changes, by hand, what the improvement changes: each script it adds or edits."""
        import yaml
        hass = self._world.hass
        path = hass.config.path("scripts.yaml")
        scripts = yaml.safe_load(open(path)) or {}
        targets = [f"script.{c['id']}" for c in improvement._item.get("changes", [])
                   if c.get("kind") == "script" and c.get("id")]
        if not targets:
            raise Missing("harness: edit_home for an improvement with no script")
        for entity_id in targets:
            scripts[entity_id.split(".", 1)[1]] = {"alias": "Changed by hand", "sequence": [{"delay": 1}]}
        open(path, "w").write(yaml.safe_dump(scripts))
        self._world.call(hass.services.async_call("script", "reload", blocking=True))

    def archive(self, conversation):
        self._hide(conversation, True)

    def unarchive(self, conversation):
        self._hide(conversation, False)

    def _hide(self, conversation, hidden):
        from custom_components.assist_improver import panel
        self._acting(self._world.ws(panel.ws_conversation_hide, type="assist_improver/conversation/hide",
                                    recording_id=conversation.id, hidden=hidden))

    def mark_test(self, conversation):
        from custom_components.assist_improver import panel
        self._acting(self._world.ws(panel.ws_conversation_test, type="assist_improver/conversation/test",
                                    recording_id=conversation.id))

    def stand_in(self, stage, behaviour):
        self._world.roles.behave(stage, behaviour)

    # Reading what the product records

    def _talk(self, rid):
        return next((talk for talk in self._talks() if talk.id == rid), rid)

    def place(self, conversation):
        return Place(self, self._runtime.flow.place(conversation.id))

    def records(self, conversation, round):
        number = round if round is not None else self._runtime.flow.record(conversation.id)["round"]
        found = self._runtime.flow.round_of(conversation.id, number)
        if found is None:
            return {}
        return {stage: StageRecord(conversation.id, number, stage, deepcopy(data))
                for stage, data in found["records"].items()}

    def shown(self, conversation):
        """{screen: the stage it shows the conversation at}, from what each screen gets: the conversations list (where
        a conversation stands, whatever its filters) and the conversation's own page. Each shows it by its chip, and
        reads its stage for what can be done with it: both are in, the chip as the stage it names (that stage, when it
        names more than one), and in Your household as that stage only while it names the record's outcome. Each also
        gets its hold, whose why the chip's tooltip shows: in as the stage while it's the record's (cause and why; none
        while the record holds it nowhere)."""
        from custom_components.assist_improver import panel
        _, listing = self._world.call(self._world.ws(panel.ws_conversations, type="assist_improver/conversations",
                                                     ids=[conversation.id]))
        _, page = self._world.call(self._world.ws(panel.ws_conversation, type="assist_improver/conversation",
                                                  recording_id=conversation.id))
        record = self._runtime.flow.place(conversation.id)
        hold = record["held"] and {"cause": record["held"]["cause"], "why": record["held"]["why"]}
        screens = {"conversations list": next((s for s in listing["stands"] if s["id"] == conversation.id), {}),
                   "conversation page": page}
        out = {}
        for screen, shows in screens.items():
            stage, chip = shows.get("stage"), shows.get("status")
            if stage is None or chip is None:
                raise Missing(f"shown: the {screen} doesn't say where it is: stage {stage!r}, chip {chip!r}")
            if chip not in CHIPS:
                raise Missing(f"harness: the stage the chip {chip!r} names (DETAILS P14.3)")
            outcome = OUTCOME_CHIPS.get(chip)
            out[screen] = stage
            out[f"{screen}'s chip"] = (stage if stage in CHIPS[chip] and outcome in (None, record["outcome"])
                                       else " or ".join(CHIPS[chip]) + (f" ({outcome})" if outcome else ""))
            out[f"{screen}'s hold"] = stage if shows.get("held") == hold else f"held {shows.get('held')!r}"
        return out

    def counts(self):
        """{stage: how many conversations Settings shows there}, from what Settings gets."""
        from custom_components.assist_improver import panel
        _, status = self._world.call(self._world.ws(panel.ws_run_status, type="assist_improver/run/status"))
        if "stages" not in status["preview"]:
            raise Missing("counts: Settings gets no count per stage")
        return dict(status["preview"]["stages"])

    def handoffs(self, conversation):
        flow = self._runtime.flow
        return [Handoff(h) for n in sorted(flow.record(conversation.id)["rounds"], key=int)
                for h in flow.round_of(conversation.id, int(n)).get("handoffs") or []]

    def path(self, conversation, round=None):
        found = self._runtime.flow.round_of(conversation.id, round)
        return list(found["path"]) if found else []

    def analysis(self, conversation):
        """The conversation's latest analysis, as a goal tree, its notes among its turns; None while it has none."""
        flow = self._runtime.flow
        analysis = None
        for n in sorted(flow.record(conversation.id)["rounds"], key=int):
            records = flow.round_of(conversation.id, int(n))["records"]
            analysis = (records.get("Analyze") or {}).get("analysis") or analysis
        return Tree(conversation.id, analysis) if analysis else None

    def seen_by(self, stage, conversation, round=None):
        """What a stage's AI role was given in a round (the latest by default), from its record there: the
        conversation's turns and goals, and the records it read."""
        number = round if round is not None else self._runtime.flow.record(conversation.id)["round"]
        record = self.records(conversation, number).get(stage)
        if record is None or "given" not in record._data:
            raise Missing(f"seen_by: the {stage} record of round {number} keeps nothing of what it was given")
        return self._given(conversation, record._data["given"])

    def _given(self, conversation, given):
        """A stage's or an idea's given, read: the turns, goals and records it names."""
        tree = self.analysis(conversation)
        read = {self.records(conversation, r["round"]).get(r["stage"]) for r in given.get("records", [])}
        return ({t for t in tree.turns if t.key in given["turns"]} | {g for g in tree.goals if g.key in given["goals"]}
                | (read - {None}))

    def trace(self, conversation):
        tree = self.analysis(conversation)
        by_key = {g.key: g for g in tree.goals} if tree else {}
        return [Record("Moment", {"stages": [m["stage"]], "round": m["round"],
                                  "goals": [by_key.get(key, key) for key in m["goals"]]})
                for m in self._runtime.flow.record(conversation.id)["trace"]]

    def ideas(self, conversation):
        flow = self._runtime.flow
        return [Idea(i, conversation.id, int(n), self) for n in sorted(flow.record(conversation.id)["rounds"], key=int)
                for i in ((flow.round_of(conversation.id, int(n))["records"].get("Propose") or {}).get("ideas") or [])]

    def tests(self, conversation, round=None):
        number = round if round is not None else self._runtime.flow.record(conversation.id)["round"]
        ideas = [i for i in self.ideas(conversation) if i._key[1] == number]
        tree = self.analysis(conversation)
        return [Test(deepcopy(t), ideas, tree) for t in self._runtime.flow.tests(conversation.id, round)]

    def tests_left(self, conversation):
        return self._runtime.flow.tests_left(conversation.id)

    def trips_back(self, conversation):
        return [Record("Trip", {**trip, "paid_by": "person" if trip["paid_by"] == "person"
                                else Budget(trip["paid_by"])})
                for trip in self._runtime.flow.record(conversation.id)["trips"]]

    def _runs_by_id(self):
        return {run.record["id"]: run for run in self.runs()}

    @staticmethod
    def _about(stage, conversation, given):
        """Whether what a stage's stand-in was given is about this conversation."""
        if stage == "Triage":
            return (given.get("turns") or [{}])[0].get("user_says") == conversation.said
        if stage == "Analyze":
            return (given.get("conversation") or [{}])[0].get("user_says") == conversation.said
        if stage == "Propose":
            return any(s.get("session") == conversation.id[:8] for s in given.get("friction_scenarios") or [])
        if stage == "Review":  # A draft: the goals it's for.
            return any(str(g.get("session"))[:8] == conversation.id[:8] for g in given.get("goals") or [])
        if stage == "Simulate":  # A replay request: the words it starts from.
            from .roles import Assistant
            said = [Assistant._text(m) for m in given.get("messages") or [] if m.get("role") == "user"]
            return next((words for words in said if words is not None), None) == conversation.said
        raise Missing(f"harness: received({stage!r}, ...)")

    def reviews(self, conversation):
        """What the stand-in reviewer judged of the drafts for this conversation, from its own log, oldest first."""
        runs = self._runs_by_id()
        return [Record("Review", {"run": runs.get(run), "title": draft["title"], "changes": digest(draft["changes"]),
                                  "verdict": verdict})
                for run, draft, verdict in self._world.roles.reviews if self._about("Review", conversation, draft)]

    def answered(self, conversation):
        """What each test_idea answered the proposer for this conversation, in order, from the stand-ins' own log."""
        return list(self._world.roles.tested.get(conversation.id[:8], []))

    def received(self, stage, conversation):
        """What a stage's stand-in was given for this conversation, from the stand-ins' own log, oldest first: each
        role call's input, for Review each draft it was sent, and for Simulate each replay request the household's
        assistant answered; after_cut: logged after its run was cut off."""
        runs, roles = self._runs_by_id(), self._world.roles
        return [Record("Received", {"run": runs.get(run), "input": given,
                                    "after_cut": any(at <= n and cut == run for at, cut in roles.cuts)})
                for n, (kind, run, given) in enumerate(roles.received)
                if kind == stage and self._about(stage, conversation, given)]

    def reached(self, stage, conversation):
        """What the proposer or its reviewer got while it worked on this conversation, from the stand-ins' own log:
        the conversations within its reach, and this conversation's turns said to Assist (by their words) and goals
        (by their summaries, at their turns), as it got them. Within reach is in its input (the proposer's friction
        scenarios), or read with a session tool it was handed: the stand-ins look over the household, reading each
        session list_sessions shows them."""
        if stage not in ("Propose", "Review"):
            raise Missing(f"harness: reached({stage!r}, ...)")
        roles, mine = self._world.roles, conversation.id[:8]
        got = {}  # {session: that session as the role got it, a script of its turns}
        for kind, _, given in roles.received:
            if kind == stage == "Propose" and self._about(stage, conversation, given):
                got.update({scenario["session"]: scenario for scenario in given.get("friction_scenarios") or []})
        for kind, _, about, read in roles.read:
            if kind == stage and mine in about:
                got.update({session: answer for session, answer in read.items() if "turns" in answer})
        out = {talk for talk in self._talks() if talk.id[:8] in got}
        if mine not in got:
            return out
        tree, said = self.analysis(conversation), recording_turns(conversation.record)
        turns = {turn["turn"]: turn for turn in got[mine]["turns"]}
        for turn in tree.turns:
            if isinstance(turn.key, int) and (turns.get(turn.key) or {}).get("user") == (
                    said[turn.key - 1]["messages"][0]["content"].strip()):
                out.add(turn)
        for goal in tree.goals:
            there = turns.get(goal._data.get("turn")) or {}
            started = (there.get("goals_started") or []) + (there.get("settings_left_open") or [])
            listed = there.get("served") or [] if goal._data.get("serves") is not None else [
                g["summary"] for g in started]
            if goal._data.get("summary") in listed:
                out.add(goal)
        return out

    def improvements(self, conversation=None):
        out = [Improvement(self, item) for item in self._runtime.improvements.items.values()]
        if conversation is not None:  # The improvements it came from (P0.10), not those that also cover it.
            out = [i for i in out if i._item.get("conversation") == conversation.id]
        return out

    def review_queue(self):
        flow = self._runtime.flow
        questions = [Question(self._talk(rid), q) for rid in list(flow.conversations) if (q := flow.question(rid))]
        return [Improvement(self, item) for item in self._runtime.improvements.pending()] + questions

    def runs(self):
        return [Run(record) for record in self._runtime.role_runs.runs if record.get("kind") == "pipeline"]

    def moved(self, run):
        if run is None:  # The act started no run: nothing moved.
            return set()
        if "moved" not in run.record:
            raise Missing("Run.moved: that run recorded no conversations it moved")
        return {self._talk(rid) for rid in run.record["moved"]}

    def config(self):
        """The scripts (as scripts.yaml has them) and the entities exposed to Assist, read from Home Assistant."""
        import yaml
        from homeassistant.components.homeassistant.exposed_entities import async_should_expose
        hass = self._world.hass
        scripts = yaml.safe_load(open(hass.config.path("scripts.yaml"))) or {}
        exposed = sorted(s.entity_id for s in hass.states.async_all()
                         if async_should_expose(hass, "conversation", s.entity_id))
        return {"scripts": scripts, "exposed": exposed}

    def writable(self):
        """Everything in Home Assistant an improvement can write, read from Home Assistant: what config() reads, each
        device's aliases (the entity registry), the household knowledge every assistant reads, and each assistant's
        settings, its instructions among them (its config entry's subentry)."""
        from homeassistant.helpers import entity_registry as er
        from custom_components.assist_improver.household import KEY
        hass = self._world.hass
        return {**self.config(),
                "aliases": {e.entity_id: list(e.aliases) for e in er.async_get(hass).entities.values()},
                "household knowledge": deepcopy(getattr(hass.data.get(KEY), "knowledge", None)),
                "assistants": {(entry.entry_id, sid): deepcopy(dict(sub.data))
                               for entry in hass.config_entries.async_entries()
                               for sid, sub in entry.subentries.items()}}
