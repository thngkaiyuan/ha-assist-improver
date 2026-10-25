"""The roles' tools over the sessions: submitting an answer, reading sessions and turns, asking the reviewer, and
checking a draft with Home Assistant itself."""

import asyncio

import probatio
from homeassistant.helpers import llm

from ..candidates import live_script_definitions
from ..cases import CaseError
from ..exploration import (TOOLS, DeviceDetails, DevicesInArea, ListImprovements, ReadImprovement, ReadTurn,
                           _SessionTool, change_devices)
from ..sessions import STATES
from ..trace import span
from .checks import household_knowledge, instructions, problems
from .goals import scenario
from .schema import _HYPOTHESIS, HYPOTHESES, REVIEW, verdict

# How often the generator may ask the reviewer, per run and per idea.
REVIEWS_PER_RUN, REVIEWS_PER_IDEA = 15, 5


class _Submit(_SessionTool):
    """A role's only way to answer. Keeps every submission so the best one can be used."""

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions, self.submissions = sessions, []

    def check(self, answer):
        raise NotImplementedError

    def run(self, **answer):
        found = self.check(answer)
        self.submissions.append({"answer": answer, "problems": found})
        if found:
            return {"accepted": False, "problems": found}
        return {"accepted": True, "message": "Accepted. Finish with one short sentence."}

    def best(self):
        """(status, answer, problems): the last accepted submission, else the one with fewest problems."""
        if not self.submissions:
            return "no_answer_submitted", None, []
        accepted = [s for s in self.submissions if not s["problems"]]
        if accepted:
            return "accepted", accepted[-1]["answer"], []
        best = min(reversed(self.submissions), key=lambda s: len(s["problems"]))
        return "best_rejected", best["answer"], best["problems"]


class SubmitHypotheses(_Submit):
    name = "submit_hypotheses"
    description = ("Submit your answer: one hypothesis per theme, each with the goals it fixes (session and goal "
                   "turn, from goals_with_friction), its cause, its changes and the expected effect. A change's text "
                   "is the line for Assist's instructions (preference, prompt; say who it's for: everyone, or "
                   "this_user) or the script's description (script, which Assist reads to decide when to run it). A "
                   "script change makes a new script (script_name, steps; exposed to Assist unless expose is false), "
                   "or edits an existing one when it gives that script's entity_id (a new description in text, "
                   "and/or new steps; editing doesn't expose it, so add an exposure change if Assist should run an "
                   "unexposed one); its steps may end with a stop step carrying the message Assist tells the "
                   "user. An exposure change exposes a device unless expose is false, which hides it. Goals an existing "
                   "improvement already addresses go in already_addressed (from list_improvements), not in a "
                   "hypothesis. It is checked against "
                   "the sessions and the household; if problems are reported, fix them and submit again.")
    parameters = HYPOTHESES

    def check(self, answer):
        return problems(self.sessions, answer)


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


class ReadSessionTurn(_SessionTool):
    name = "read_session_turn"
    description = ("Read turns of one session: what the user and the assistant said, every tool the assistant "
                   "called with the result, and the state changes during the turn. Give the session and one turn "
                   "or several.")
    parameters = probatio.Schema({probatio.Required("session"): str, probatio.Optional("turn"): int,
                                  probatio.Optional("turns"): [int]})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        await self.sessions.load(args["session"])
        return llm.ToolResult(data=self.run(**args))

    def run(self, session, turn=None, turns=None):
        view = self.sessions.views.get(session)
        if view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return view.read_turns(turns) if turns else view.read_turn(turn or 1)


class DeviceDetailsAt(_SessionTool):
    """device_details, optionally as of one session: the household snapshot is the newest session's."""
    name = "device_details"
    description = ("Everything about one device: its states and attributes, aliases, whether Assist can see it, its "
                   "changes, and other entities on the same physical device. By default as of the newest session; "
                   "give session to see it as it was in that session.")
    parameters = probatio.Schema({probatio.Required("entity_id"): str, probatio.Optional("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        if args.get("session"):
            await self.sessions.load(args["session"])
        return llm.ToolResult(data=self.run(**args))

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


class ListSessions(_SessionTool):
    name = "list_sessions"
    description = ("List recorded sessions, newest first, a page at a time: when, how many turns, the first request, "
                   "and the state (open_friction: friction no improvement handles yet; friction_handled; "
                   "without_friction; not_analysed). Filter by state, by a device (its entity_id or a name), by an "
                   "area, or by words in the user's requests, to find the sessions where a rule would apply.")
    parameters = probatio.Schema({probatio.Optional("page", default=1): int,
                                  probatio.Optional("state"): probatio.In(["all", *STATES]),
                                  probatio.Optional("device"): str, probatio.Optional("area"): str,
                                  probatio.Optional("words"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self, page=1, state=None, device=None, area=None, words=None):
        return self.sessions.listing(page=page, state=state, device=device, area=area, words=words)


class ReadSession(_SessionTool):
    name = "read_session"
    description = ("Read one session as a script: each turn's words, the goals it started, and any friction. Use "
                   "read_session_turn for a turn's tool calls and state changes.")
    parameters = probatio.Schema({probatio.Required("session"): str})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        args = self.parameters(tool_input.tool_args)
        await self.sessions.load(args["session"])
        return llm.ToolResult(data=self.run(**args))

    def run(self, session):
        view = self.sessions.views.get(session)
        if session not in self.sessions.analyses or view is None:
            return {"error": f"There is no session {session!r}; list_sessions shows them."}
        return scenario(self.sessions.analyses[session], view)


class ReviewChannel:
    """Drafts from the generator to one long-running reviewer, and reviews back. The reviewer keeps its own
    conversation across reviews, waiting on a tool until the generator sends the next draft."""
    CLOSED = object()

    def __init__(self):
        self.queue, self.current, self.reviewer = asyncio.Queue(), None, None

    def __deepcopy__(self, memo):  # HA's conversation trace deep-copies tools; the channel is shared, not copied.
        return self

    async def ask(self, draft):
        """Send a draft and wait for its review; None if the reviewer stopped."""
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((draft, future))
        waits = [future] + ([self.reviewer] if self.reviewer else [])
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        return future.result() if future.done() else None

    def close(self):
        self.queue.put_nowait((self.CLOSED, None))


class WaitForDraft(llm.Tool):
    """The reviewer's side of the channel: blocks until the generator sends a draft."""
    name = "wait_for_draft"
    description = "Wait for the generator's next draft hypothesis and return it, or say that you're finished."
    parameters = probatio.Schema({})
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, channel):
        self.channel = channel

    async def async_call(self, hass, tool_input, llm_context):
        draft, future = await self.channel.queue.get()
        if draft is ReviewChannel.CLOSED:
            return llm.ToolResult(data={"finished": "No more drafts. You're done; finish with one short sentence."})
        self.channel.current = (draft, future)
        return llm.ToolResult(data={"draft": draft})


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
            _, future = self.channel.current
            self.channel.current = None
            if not future.done():
                future.set_result(answer)
            result["message"] = "Sent to the generator. Call wait_for_draft for the next draft."
        return result


class AskReviewer(llm.Tool):
    """The generator's way to discuss a draft with the reviewer: a separate model, never its own judgement."""
    name = "ask_reviewer"
    description = ("Ask the reviewer, a separate model, about one draft hypothesis (same fields as in "
                   "submit_hypotheses). It answers with numbered review comments backed by household evidence, and an "
                   "outcome that follows from them (any blocking comment requests changes). When asking again about "
                   "the same idea (same title), answer each previous blocking comment in responses: resolved, or "
                   "wont_fix with a note.")
    parameters = probatio.Schema({**_HYPOTHESIS, probatio.Optional("responses"): [{
        probatio.Required("comment"): int, probatio.Required("resolution"): probatio.In(["resolved", "wont_fix"]),
        probatio.Required("note"): str}]})
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, sessions, channel):
        self.sessions, self.channel, self.reviews = sessions, channel, []

    async def async_call(self, hass, tool_input, llm_context):
        args = dict(tool_input.tool_args)
        hypothesis = {k: v for k, v in args.items() if k != "responses"}
        earlier = [r for r in self.reviews if r["hypothesis"].get("title") == hypothesis.get("title")]
        if len(self.reviews) >= REVIEWS_PER_RUN or len(earlier) >= REVIEWS_PER_IDEA:
            return llm.ToolResult(data={"error": "The review budget for this run or idea is used up; submit your best version."})
        checks = None
        if self.sessions.evidence:
            async with span("evidence.check"):
                checks = await self.sessions.evidence.check(hypothesis)
        related = self.sessions.related([(g["session"], g["goal_turn"]) for g in hypothesis.get("goals", [])],
                                        change_devices(hypothesis.get("changes", [])))
        applies = self.sessions.where_it_applies_to(hypothesis)
        answer = await self.channel.ask({**args, **({"checks": checks} if checks else {}),
                                         **({"related_improvements": related} if related else {}),
                                         "sessions_where_it_would_apply": applies})
        self.reviews.append({"hypothesis": hypothesis, "responses": args.get("responses"), "review": answer,
                             "verdict": verdict(answer) if answer else None, "checks": checks})
        if answer is None:
            return llm.ToolResult(data={"error": "The reviewer stopped; decide yourself."})
        return llm.ToolResult(data={**review_result(hypothesis, answer), **({"checks": checks} if checks else {})})


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
    return result


class CheckHypothesis(llm.Tool):
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
                   "effect shows only when the conversation is replayed with the model, later.")
    parameters = probatio.Schema(_HYPOTHESIS)
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, sessions):
        self.sessions = sessions

    async def async_call(self, hass, tool_input, llm_context):
        hypothesis = dict(tool_input.tool_args)
        return llm.ToolResult(data={**await self.sessions.evidence.check(hypothesis),
                                    "sessions_where_it_would_apply": self.sessions.where_it_applies_to(hypothesis)})


class ReadAssistantInstructions(_SessionTool):
    name = "read_assistant_instructions"
    description = ("Read Assist's current instructions (its prompt template, as configured), the household knowledge "
                   "(the lines every assistant reads: for everyone, and for each person), and which assistant and "
                   "model it uses. Preference and prompt changes add lines to the household knowledge.")
    parameters = probatio.Schema({})

    def __init__(self, sessions):
        super().__init__(sessions.household)
        self.sessions = sessions

    def run(self):
        template, assistant = instructions(self.sessions)
        if template is None:
            return {"error": "No recorded session shows the assistant's instructions."}
        knowledge = household_knowledge(self.sessions)
        return {"assistant": assistant.get("title"), "model": assistant.get("options", {}).get("model"),
                "instructions_template": template,
                "household_knowledge": {"everyone": knowledge["everyone"],
                                        "the_person_in_the_sessions": [l for mine in knowledge["people"].values()
                                                                       for l in mine]}}


class ReadScript(llm.Tool):
    """A script's current definition, read live (read-only): recordings only hold script names.

    Offline (in tests), definitions come from a copy of the household's scripts instead.
    """
    name = "read_script"
    description = "Read a script's current definition: its name and steps. Give the script's entity_id."
    parameters = probatio.Schema({probatio.Required("entity_id"): str})
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self, definitions=None):
        self.definitions = definitions  # {entity_id: definition}, offline only.

    def run(self, entity_id):
        definition = (self.definitions or {}).get(entity_id)
        if definition is None:
            return {"error": f"Script {entity_id} has no readable configuration"}
        return {"entity_id": entity_id, "definition": definition}

    async def async_call(self, hass, tool_input, llm_context):
        entity_id = tool_input.tool_args["entity_id"]
        if self.definitions is not None:
            return llm.ToolResult(data=self.run(entity_id))
        try:
            definitions = live_script_definitions(hass, [entity_id])
        except CaseError as err:
            return llm.ToolResult(data={"error": str(err)})
        return llm.ToolResult(data={"entity_id": entity_id, "definition": definitions[entity_id]})


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
