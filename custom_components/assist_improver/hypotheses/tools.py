"""The roles' tools over the sessions: submitting an answer, reading sessions and turns, asking the reviewer, and
checking a draft with Home Assistant itself."""

import asyncio

import probatio

from .. import contracts, records
from ..cases import CaseError, digest
from ..exploration import (TOOLS, DeviceDetails, DevicesInArea, ListImprovements, ReadImprovement, ReadTurn,
                           RoleTool, change_devices)
from ..sessions import STATES
from ..snapshot import live_script_definitions
from ..trace import span
from .checks import household_knowledge, instructions, problems
from .goals import scenario
from .schema import _HYPOTHESIS, HYPOTHESES, REVIEW, verdict

# How often the generator may ask the reviewer, per round and per idea.
REVIEWS_PER_ROUND, REVIEWS_PER_IDEA = 15, 5
STOPPED = "The reviewer has stopped, so it can approve no more ideas this round."


class _Submit(RoleTool):
    """A role's only way to answer. Keeps every submission so the best one can be used."""

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions, self.submissions = sessions, []

    def check(self, answer):
        """What's wrong with the answer itself (checked against the sessions and the household)."""
        raise NotImplementedError

    def pending(self, answer):
        """What the role must still do before the answer is accepted, though nothing is wrong with it."""
        return []

    def run(self, **answer):
        found, pending = self.check(answer), self.pending(answer)
        self.submissions.append({"answer": answer, "problems": found})
        if found or pending:
            return {"accepted": False, "problems": found + pending}
        return {"accepted": True, "message": "Accepted. Finish with one short sentence."}

    def best(self):
        """(status, answer, problems): the last submission with nothing wrong with it, accepted though something was
        still pending (were it the answer left when the model stopped, it's the answer), else the one with fewest
        problems."""
        if not self.submissions:
            return "no_answer_submitted", None, []
        accepted = [s for s in self.submissions if not s["problems"]]
        if accepted:
            return "accepted", accepted[-1]["answer"], []
        best = min(reversed(self.submissions), key=lambda s: len(s["problems"]))
        return "best_rejected", best["answer"], best["problems"]


class SubmitHypotheses(_Submit):
    name = "submit_hypotheses"
    description = ("Submit your answer: one hypothesis for this conversation, its changes combining the fixes of "
                   "every theme (they can be broad), with the goals it fixes (session and goal turn, from "
                   "goals_with_friction), its cause, its changes and the expected effect. A preference's "
                   "text is a line for the household knowledge, which every assistant reads (say who it's for: "
                   "everyone, or this_user). A prompt change edits one assistant's own instructions: assistant (its "
                   "name, as read_assistant_instructions shows it), text to add, and replaces: lines of those "
                   "instructions the text replaces, or removes when there's no text; it may use Home Assistant's "
                   "template values ({{ user_name }}, {{ ha_name }}, llm_context) and functions, and Home Assistant "
                   "must render it. A script's text is its description (which Assist reads to decide when to run "
                   "it). A script change makes a new script (script_name, steps; exposed to Assist unless expose is false), "
                   "or edits an existing one when it gives that script's entity_id (a new description in text, "
                   "and/or new steps; editing doesn't expose it, so add an exposure change if Assist should run an "
                   "unexposed one); its steps may end with a stop step carrying the message Assist tells the "
                   "user. An exposure change exposes a device unless expose is false, which hides it. Goals an existing "
                   "improvement already addresses go in already_addressed (from list_improvements), not in the "
                   "hypothesis. It is checked against "
                   "the sessions and the household; if problems are reported, fix them and submit again.")
    parameters = HYPOTHESES

    def __init__(self, sessions, ask=None, tester=None):
        super().__init__(sessions)
        self.ask, self.tester = ask, tester  # Its ask_reviewer and test_idea (stamps.TestIdea): the stamps' tools.

    async def answer(self, hass, args):
        await self.sessions.render_edits(hass, args.get("hypotheses"))  # The checks read whether each renders.
        return self.run(**args)

    def run(self, **answer):
        """Kept with each edit of an assistant's instructions saying where Home Assistant keeps them (target)."""
        answer = {**answer, "hypotheses": [{**h, "changes": self.sessions.with_targets(h.get("changes"))}
                                           for h in answer.get("hypotheses") or []]} if "hypotheses" in answer else answer
        return super().run(**answer)

    def check(self, answer):
        """problems, and more than one idea: a round brings its conversation one improvement (SPEC P0.6, P0.10)."""
        found, ideas = problems(self.sessions, answer), answer.get("hypotheses")
        if isinstance(ideas, list) and len(ideas) > 1:
            found.append(f"Send on one idea for this conversation, not {len(ideas)}: one hypothesis that combines its "
                         "fixes (its changes can be broad), with both stamps on exactly its changes: the reviewer's "
                         "approval (ask_reviewer) and a validated test (test_idea).")
        return found

    def pending(self, answer):
        """Each idea whose exact changes lack a stamp, named with that stamp's tool, so it goes back to the model
        rather than staying behind unsaid (P0.4), while it can still get the stamp (the reviewer's approval while it
        runs and can be asked about the idea; Simulate's validation while tests are left and Simulate hasn't tested
        those changes or said it can't), or while the answer sends none of the changes that hold both, which it can
        send instead. Without a tester (an answer checked offline), nothing is pending."""
        from ..stamps import of  # stamps reads this package's schema.
        if self.tester is None:
            return []
        reviews, tests, ideas = self.ask.reviews, self.tester.tests(), answer.get("hypotheses") or []
        reviewing = not self.ask.stopped
        tried = {t["changes"] for t in tests} | self.tester.untestable
        stamped = [of(reviews, tests, h) for h in ideas]
        instead = not any(all(s.values()) for s in stamped) and any(  # Changes that hold both, left out.
            all(of(reviews, tests, r["hypothesis"]).values()) for r in reviews)
        found = []
        for n, (h, s) in enumerate(zip(ideas, stamped), 1):
            title = h.get("title")
            if not s["Review"]:
                if instead or reviewing and not self.ask.refused(title):
                    found.append(f"The reviewer hasn't approved idea {n} ({title!r}) with exactly these changes, so "
                                 "it won't go on: send the exact changes it approved, or ask it about these "
                                 "(ask_reviewer).")
            elif not s["Simulate"] and (instead or self.tester.left() > 0 and digest(h.get("changes")) not in tried):
                found.append(f"No test validated idea {n} ({title!r}) with exactly these changes, so it won't go "
                             "on: send the exact changes that validated, or test these (test_idea).")
        return found


class SubmitReview(_Submit):
    name = "submit_review"
    description = ("Submit your review: your comments (label, blocking or non-blocking, subject, discussion, household "
                   "evidence) and, when you know a better version, the revised hypothesis. The outcome follows from "
                   "the comments: any blocking comment requests changes.")
    parameters = REVIEW

    def check(self, answer):
        found = problems(self.sessions, answer, REVIEW)
        known = set(self.sessions.household._entities())
        for n, comment in enumerate(answer.get("comments", []), 1):
            if len(comment.get("decorations", [])) != 1:
                found.append(f"Comment {n}: decorate it blocking or non-blocking (one of them).")
            if comment.get("label") != "praise" and not comment.get("evidence"):
                found.append(f"Comment {n}: give household evidence (a session and turn, a device, or a pattern).")
            for item in comment.get("evidence") or []:
                session, turn, entity = item.get("session"), item.get("turn"), item.get("entity_id")
                known_session = self.sessions.briefs.get(session) if session else None
                if session and known_session is None:
                    found.append(f"Comment {n}: there is no session {session!r}.")
                elif known_session is not None and turn is not None and not 1 <= turn <= known_session["turns"]:
                    found.append(f"Comment {n}: session {session} has no turn {turn}.")
                if entity and entity not in known:
                    found.append(f"Comment {n}: there is no device {entity!r} in this household.")
                if not (session or entity or (item.get("note") or "").strip()):
                    found.append(f"Comment {n}: each piece of evidence names a session, a device or a pattern (note).")
        return found


class ReadSessionTurn(RoleTool):
    name = "read_session_turn"
    description = ("Read turns of one session: what the user and the assistant said, every tool the assistant "
                   "called with the result, and the state changes during the turn. Give the session and one turn "
                   "or several.")
    parameters = probatio.Schema({probatio.Required("session"): str, probatio.Optional("turn"): int,
                                  probatio.Optional("turns"): [int]})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def answer(self, hass, args):
        await self.sessions.load(args["session"])
        return self.run(**args)

    def run(self, session, turn=None, turns=None):
        view = self.sessions.views.get(session)
        if view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return view.read_turns(turns) if turns else view.read_turn(turn or 1)


class DeviceDetailsAt(RoleTool):
    """device_details, optionally as of one session: the household snapshot is the newest session's."""
    name = "device_details"
    description = ("Everything about one device: its states and attributes, aliases, whether Assist can see it, its "
                   "changes, and other entities on the same physical device. By default as of the newest session; "
                   "give session to see it as it was in that session.")
    parameters = probatio.Schema({probatio.Required("entity_id"): str, probatio.Optional("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def answer(self, hass, args):
        if args.get("session"):
            await self.sessions.load(args["session"])
        return self.run(**args)

    def run(self, entity_id, session=None):
        view = self.sessions.views.get(session) if session else self.sessions.household
        if view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return {"as_of_session": session or "the newest session", **view.device_details(entity_id)}


class DevicesInAreaNow(DevicesInArea):
    """devices_in_area over the household snapshot, which is the newest session's (the analyzer's tool reads one)."""
    description = ("List the devices in an area with their state as of the newest session, what they can do, and "
                   "whether Assist may use them (exposed). Use device_details with a session for an earlier state. "
                   "Sensors only on request.")


class ListSessions(RoleTool):
    name = "list_sessions"
    description = ("List recorded sessions, newest first, a page at a time: when, how many turns, the first request, "
                   "and the state (open_friction, without_friction or not_analysed). Filter by state, by a device "
                   "(its entity_id or a name), by an area, or by words in the user's requests, to find the sessions "
                   "where a rule would apply.")
    parameters = probatio.Schema({probatio.Optional("page", default=1): int,
                                  probatio.Optional("state"): probatio.In(["all", *STATES]),
                                  probatio.Optional("device"): str, probatio.Optional("area"): str,
                                  probatio.Optional("words"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self, page=1, state=None, device=None, area=None, words=None):
        return self.sessions.listing(page=page, state=state, device=device, area=area, words=words)


class ReadSession(RoleTool):
    name = "read_session"
    description = ("Read one session as a script: each turn's words, the goals it started, and any friction. Use "
                   "read_session_turn for a turn's tool calls and state changes.")
    parameters = probatio.Schema({probatio.Required("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def answer(self, hass, args):
        await self.sessions.load(args["session"])
        return self.run(**args)

    def run(self, session):
        view = self.sessions.views.get(session)
        if session not in self.sessions.analyses or view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return scenario(self.sessions.analyses[session], view)


class ReviewChannel:
    """Drafts from the generator to one long-running reviewer, and reviews back. The reviewer keeps its own
    conversation across reviews, waiting on a tool until the generator sends the next draft."""

    def __init__(self):
        self.queue, self.current, self.reviewer, self.closed = asyncio.Queue(), None, None, False

    async def ask(self, draft):
        """Send a draft and wait for its review; None if the reviewer stopped."""
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((draft, future))
        waits = [future] + ([self.reviewer] if self.reviewer else [])
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        return future.result() if future.done() else None

    def close(self):
        """No more drafts: every wait for one, under way or later, is finished."""
        self.closed = True
        self.queue.put_nowait(None)  # Ends the wait under way, if any.


class WaitForDraft(RoleTool):
    """The reviewer's side of the channel: blocks until the generator sends a draft. Home Assistant runs one model
    message's calls at once: they take their turn, and a call while the reviewer holds a draft it hasn't reviewed is
    refused, so no call waits for a draft another one took. Once the channel is closed, every call is finished."""
    name = "wait_for_draft"
    description = "Wait for the generator's next draft hypothesis and return it, or say that you're finished."
    parameters = probatio.Schema({})

    def __init__(self, channel):
        self.channel, self.lock = channel, asyncio.Lock()

    async def answer(self, hass, args):
        async with self.lock:
            if not self.channel.closed and self.channel.current:
                return {"error": "You have a draft to review: submit_review first, then wait_for_draft."}
            waited = None if self.channel.closed else await self.channel.queue.get()
            if waited is None:  # Closed, before this call or while it waited.
                return {"finished": "No more drafts. You're done; finish with one short sentence."}
            self.channel.current = waited
            return {"draft": waited[0]}


class ChannelSubmitReview(_Submit):
    """submit_review in the loop: an accepted review answers the draft the generator is waiting on."""
    name = "submit_review"
    description = ("Submit your review of the current draft: your comments (label, blocking or non-blocking, subject, "
                   "discussion, household evidence) and, when you know a better version, the revised hypothesis. The "
                   "outcome follows from the comments: any blocking comment requests changes. Then call "
                   "wait_for_draft for the next draft.")
    parameters = REVIEW

    def __init__(self, sessions, channel):
        super().__init__(sessions)
        self.channel = channel

    def check(self, answer):
        if self.channel.current is None:
            return ["There's no draft to review; call wait_for_draft first."]
        return SubmitReview(self.sessions).check(answer)

    def run(self, **answer):
        result = super().run(**answer)
        if result["accepted"]:
            contracts.check(answer, records.ReviewAnswer, "The review")  # What its loop hands the generator's ask.
            _, future = self.channel.current
            self.channel.current = None
            if not future.done():
                future.set_result(answer)
            result["message"] = "Sent to the generator. Call wait_for_draft for the next draft."
        return result


class StampTool(RoleTool):
    """A stamp's tool: ask_reviewer, and test_idea (stamps.TestIdea). Home Assistant runs one model message's tool calls
    at once: these take their turn, so they spend one budget and the reviewer's current draft is the one asked about
    (P0.9, P0.13). Once the proposer's generation ends, however it ends, cancel() stops the calls still under way: none
    starts or changes anything after it (P0.8)."""

    def __init__(self):
        self.lock, self.running = asyncio.Lock(), set()  # running: the calls under way, each its task.

    def cancel(self):
        for task in self.running:
            task.cancel()

    async def answer(self, hass, args):
        self.running.add(task := asyncio.current_task())
        try:
            async with self.lock:
                return await self.take(hass, args)
        except asyncio.CancelledError:  # Home Assistant cancels a call only as the generation ends (a cut-off run):
            self.cancel()  # the calls waiting their turn stop with it, before the turn passes to them.
            raise
        finally:
            self.running.discard(task)

    async def take(self, hass, args):
        """What the model gets back, its turn come."""
        raise NotImplementedError


class AskReviewer(StampTool):
    """The generator's way to discuss a draft with the reviewer: a separate model, never its own judgement. On one
    conversation its reviews are the round's, from every attempt (the flow's list), and keep keeps each one in the
    round's record as it returns: each counts once, however its attempt ends (P0.2, P0.13)."""
    name = "ask_reviewer"
    description = ("Ask the reviewer, a separate model, about one draft hypothesis (same fields as in "
                   "submit_hypotheses). It answers with numbered review comments backed by household evidence, and an "
                   "outcome that follows from them (any blocking comment requests changes). When asking again about "
                   "the same idea (same title), answer each previous blocking comment in responses: resolved, or "
                   "wont_fix with a note.")
    parameters = probatio.Schema({**_HYPOTHESIS, probatio.Optional("responses"): [{
        probatio.Required("comment"): int, probatio.Required("resolution"): probatio.In(["resolved", "wont_fix"]),
        probatio.Required("note"): str}]})

    def __init__(self, sessions, channel):
        super().__init__()
        self.sessions, self.channel, self.reviews = sessions, channel, []
        self.cap, self.keep = REVIEWS_PER_ROUND, None  # Every review in reviews counts against the cap.
        self.stopped = False  # The reviewer stopped: it approves nothing more.

    def refused(self, title):
        """Whether the reviewer can't be asked about an idea with this title: the round's or the idea's review budget
        is used up."""
        earlier = [r for r in self.reviews if r["hypothesis"].get("title") == title]
        return len(self.reviews) >= self.cap or len(earlier) >= REVIEWS_PER_IDEA

    async def take(self, hass, args):
        hypothesis = {k: v for k, v in args.items() if k != "responses"}
        if self.stopped:
            return {"error": STOPPED}
        if self.refused(hypothesis.get("title")):
            return {"error": "The review budget for this round or idea is used up; submit your best version."}
        checks = None
        if self.sessions.evidence:
            async with span("evidence.check"):
                checks = await self.sessions.evidence.check(hypothesis, hass)
        related = self.sessions.related([(g["session"], g["goal_turn"]) for g in hypothesis.get("goals", [])],
                                        change_devices(hypothesis.get("changes", [])))
        applies = self.sessions.where_it_applies_to(hypothesis)
        draft = contracts.check({**args, **({"checks": checks} if checks else {}),  # What the reviewer reads.
                                 **({"related_improvements": related} if related else {}),
                                 "friction_it_targets": self.sessions.goal_friction(hypothesis),
                                 "sessions_where_it_would_apply": applies}, records.Draft, "A draft for the reviewer")
        answer = await self.channel.ask(draft)
        if answer is None:
            self.stopped = True
            return {"error": STOPPED}
        # Kept as it would be tested and sent on: an edit of an assistant's instructions names the assistant it edits.
        kept = {**hypothesis, "changes": self.sessions.with_targets(hypothesis.get("changes"))}
        review = {"hypothesis": kept, "responses": args.get("responses"), "review": answer, "verdict": verdict(answer),
                  "checks": checks}
        self.reviews.append(review)
        if self.keep is not None:
            await self.keep(review)
        return contracts.check(  # What the generator reads.
            {**review_result(hypothesis, answer), **({"checks": checks} if checks else {})}, records.ReviewResult,
            "The review for the generator")


def review_result(hypothesis, answer):
    """What the generator gets back from a review: numbered comments, the outcome, and the next step if blocked."""
    result = {"outcome": verdict(answer),
              "comments": [{"comment": n, **c} for n, c in enumerate(answer["comments"], 1)],
              **({"revised": answer["revised"]} if answer.get("revised") else {})}
    open_ = [n for n, c in enumerate(answer["comments"], 1) if "blocking" in c.get("decorations", [])]
    if open_:  # Like a goal in an agent harness: the friction stays open until fixed.
        goals = ", ".join(f"{g['session']}:{g['goal_turn']}" for g in hypothesis.get("goals", []))
        result["next_step"] = (
            f"Blocking comments {', '.join(map(str, open_))} mean goals {goals} still need a fix. Consider other "
            "ideas for them" + (", including the reviewer's revised version" if answer.get("revised") else "")
            + ". To ask again about this idea, keep its title and answer each blocking comment in responses "
            "(resolved, or wont_fix with a note). Only if the friction is outside the assistant's control, "
            "list those goals in not_fixed with why.")
    elif result["outcome"] == "approve":  # Its other stamp: Simulate's test, on this same idea (P0.4).
        result["next_step"] = ("Approved. Test these same changes (test_idea) to collect Simulate's stamp; send them "
                               "on if they validate.")
    return result


class CheckHypothesis(RoleTool):
    """Facts from Home Assistant itself about a draft hypothesis, run in sandboxes: no model, no device."""
    name = "check_hypothesis"
    description = ("Check a draft hypothesis (same fields as submit_hypotheses) with Home Assistant itself, in a "
                   "sandbox of the recorded home. implementation: for each named goal, each new, edited or newly "
                   "exposed script runs alone from the state the goal started in (and all of them together if none "
                   "reaches it alone), showing whether the goal's devices end up as the goal wants, including what "
                   "the user chose for settings left open; new aliases are matched by Home Assistant's own name "
                   "matching. resolution_signals: the recorded tool calls of every session the change could affect (its calls "
                   "touch the change's devices, their names or areas) are replayed with it, listing those that now "
                   "resolve differently: signals to weigh, not proof of a regression. "
                   "Instruction lines (preference, prompt) don't change how Home Assistant resolves devices; their "
                   "effect shows only when the conversation is replayed with the model, later. instructions: each "
                   "assistant's instructions as a prompt change edits them, rendered by Home Assistant for its "
                   "newest conversation (what Assist would read), or why they don't render.")
    parameters = probatio.Schema(_HYPOTHESIS)

    def __init__(self, sessions):
        self.sessions = sessions

    async def answer(self, hass, hypothesis):
        return {**await self.sessions.evidence.check(hypothesis, hass),
                "sessions_where_it_would_apply": self.sessions.where_it_applies_to(hypothesis)}


class ReadAssistantInstructions(RoleTool):
    name = "read_assistant_instructions"
    description = ("Read the assistants' own instructions (each assistant's template, which Home Assistant renders "
                   "for every request) and the household knowledge (the lines every assistant reads: for everyone, "
                   "and for each person). Give a session to see which assistant it used, the exact instructions it "
                   "ran with and that assistant's current ones; give an assistant to read its current ones; give "
                   "neither for every assistant. Each lists the recommended facts its instructions lack. Preference "
                   "changes add lines to the household knowledge; a prompt change edits one assistant's "
                   "instructions.")
    parameters = probatio.Schema({probatio.Optional("session"): str, probatio.Optional("assistant"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def answer(self, hass, args):
        if args.get("session"):
            await self.sessions.load(args["session"])
        return self.run(**args)

    def run(self, session=None, assistant=None):
        from ..instructions import ran_with
        knowledge = household_knowledge(self.sessions)
        household = {"everyone": knowledge["everyone"],
                     "the_person_in_the_sessions": [l for mine in knowledge["people"].values() for l in mine]}
        if session:
            view = self.sessions.views.get(session[:8])
            record = self.sessions.assistant_of(session)
            if view is None:
                return {"error": f"There is no session {session!r}; list_sessions shows them."}
            if record is None:
                return {"error": f"Session {session} doesn't show which assistant it used."}
            ran = (ran_with(view.recording) or {}).get("template")
            return {**self.sessions.instructions_of(record, (session[:8], ran)), "household_knowledge": household}
        if assistant:
            record = self.sessions.assistant_named(assistant)
            if record is None:
                return {"error": f"There's no assistant {assistant!r}; the assistants are "
                                 f"{self.sessions.assistant_names()}."}
            return {**self.sessions.instructions_of(record), "household_knowledge": household}
        records = sorted(self.sessions.assistants().values(), key=lambda r: -r["sessions"])
        if not records:  # Recordings from before assistants were told apart: the newest one's instructions.
            template, recorded = instructions(self.sessions)
            if template is None:
                return {"error": "No recorded session shows the assistant's instructions."}
            return {"assistant": recorded.get("title"), "model": recorded.get("options", {}).get("model"),
                    "instructions_template": template, "household_knowledge": household}
        return {"assistants": [self.sessions.instructions_of(r) for r in records], "household_knowledge": household}


class ReadScript(RoleTool):
    """A script's current definition, read live (read-only): recordings only hold script names.

    Offline (in tests), definitions come from a copy of the household's scripts instead.
    """
    name = "read_script"
    description = "Read a script's current definition: its name and steps. Give the script's entity_id."
    parameters = probatio.Schema({probatio.Required("entity_id"): str})

    def __init__(self, definitions=None):
        self.definitions = definitions  # {entity_id: definition}, offline only.

    def run(self, entity_id):
        definition = (self.definitions or {}).get(entity_id)
        if definition is None:
            return {"error": f"Script {entity_id} has no readable configuration"}
        return {"entity_id": entity_id, "definition": definition}

    async def answer(self, hass, args):
        entity_id = args["entity_id"]
        if self.definitions is not None:
            return self.run(entity_id)
        try:
            definitions = live_script_definitions(hass, [entity_id])
        except CaseError as err:
            return {"error": str(err)}
        return {"entity_id": entity_id, "definition": definitions[entity_id]}


def reviewer_tools(sessions, submit, scripts=None):
    """The household tools over the newest snapshot, reading sessions, instructions and scripts, and the submit tool.
    scripts: offline script definitions for read_script; live from Home Assistant when None."""
    return ([cls(sessions.household) for cls in TOOLS if cls not in (ReadTurn, DeviceDetails, DevicesInArea)]
            + [DevicesInAreaNow(sessions.household), DeviceDetailsAt(sessions), ListSessions(sessions), ReadSession(sessions), ReadSessionTurn(sessions),
               ReadAssistantInstructions(sessions), ReadScript(scripts),
               ListImprovements(sessions.household, sessions.existing),
               ReadImprovement(sessions.household, sessions.existing)]
            + ([CheckHypothesis(sessions)] if sessions.evidence else []) + [submit])


def tools(sessions, submit, reviewer=None, scripts=None):
    """The generator's tools: the reviewer's, plus ask_reviewer when given."""
    return reviewer_tools(sessions, submit, scripts) + ([reviewer] if reviewer else [])
