"""The offline suite's shared test kit: the integration installed in an offline Home Assistant (improver), its AI roles
scripted (Model, answering through Home Assistant's real AI Task path: ha_task.py) and the household's assistant
replaying from a script, so a run goes through the flow (flow.py) as installed; the conversations the scenarios use
(variant, no_turns); a conversation seated where runs would have left it (seat); and a real flow record for a stand-in
runtime (runtime_with_flow).

Each AI role plays its part through its real tools, so every answer passes the same code checks a model's would.
The graph's own harness (fixture-test/graph/harness) walks every kind of conversation through every stage."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest_asyncio
from homeassistant.setup import async_setup_component

from conftest import assist_home
from ha_task import ENTITIES, serve, use
from recordings import REPLAY_ENTITIES, TARGET, as_kept, before, bind_agent, prepared_recording
from test_ai_roles import ANALYSIS, PRESS
from test_changeset import _original_async_call, _ws
from test_milestone1 import fake_entry
from custom_components.assist_improver import async_setup_entry, async_unload_entry, panel
from custom_components.assist_improver.const import CONF_AI_TASK, CONF_TRIAGE_AI_TASK

FIRST = "Toggle the fixture relay."  # The fixture conversation's first request; each variant gets its own.
SCRIPT_TOOL = "script__press_the_relay"  # The tool Assist gets for the proposed script.


def variant(record, rid, text, *, turns=None, status="session_expired"):
    """A copy of a recorded conversation under another id and first request, cut to its first `turns` turns."""
    record = json.loads(json.dumps(record).replace(FIRST, text))
    record.update(id=rid, status=status)
    if turns is not None:
        users = [m for m in record["chat_log"]["content"] if m["role"] == "user"]
        cut = users[turns]["created"] if turns < len(users) else None
        record["turns"] = record["turns"][:turns]
        if cut:
            record["chat_log"]["content"] = [m for m in record["chat_log"]["content"] if m.get("created", "") < cut]
            record["state_changes"] = [c for c in record["state_changes"] if c["at"] < cut]
    return record


def no_turns(rid):
    """A conversation Home Assistant handled itself: nothing reached the AI, so no turns were recorded."""
    return {"format": 1, "id": rid, "core_version": "2026.10.2", "status": "session_expired", "limitations": [],
            "turns": [], "state_changes": [], "events": [{"type": "deleted"}],
            "chat_log": {"content": [{"role": "system", "content": "Frozen instructions."}]}}


def candidate_turn():
    """The household's assistant with the proposed script (Simulate's replay with the change): the request and the
    later press, each one press in one turn; the turns that finished the first by hand aren't needed (DETAILS P6.4)."""
    return [block for turn in (1, 4) for block in (
        [{"type": "tool_use", "id": f"press-{turn}", "name": SCRIPT_TOOL, "input": {}}],
        [{"type": "text", "text": "Pressed."}])]


HELD = {"blocked": "{stage} is blocked.", "scope": "The run only ran {stage}.",
        "cut off": "The run was cut off before this was done.", "failures": "{stage} failed 3 times in a row."}


def seat(improver, rid, stage, *, outcome=None, held=None, sent=None, question=None, archived=False,
         covered_by=None, status="session_expired", minutes=0):
    """A kept conversation whose flow record stands as runs would have left it, seeded straight for the screens that
    read it: at stage (new at Your Assist; else in its first round), held for a cause (HELD), sent in (sent: what its
    mark says beside via panel), with a question waiting at Your review, or ended in Your household with an outcome.
    One not kept yet is recorded too (status: its recording's; minutes: how long ago it started)."""
    from datetime import timedelta
    from homeassistant.util.dt import utcnow
    from custom_components.assist_improver.flow import STAGES
    now = utcnow()
    improver.recorder.index.setdefault(rid, {"id": rid, "status": status, "user_turns": 1, "updated": f"u-{rid}",
                                             "started": (now - timedelta(minutes=minutes)).isoformat()})
    flow = improver.runtime.flow
    flow.conversations.pop(rid, None)
    conv = flow.record(rid)
    mark = {"at": now.isoformat(), "via": "panel", "again": False, **sent} if sent is not None else None
    if stage == "Your Assist" and not held:
        conv["sent"] = mark
        return conv
    conv.update(stage=stage, round=1, outcome=outcome, archived=archived, covered_by=covered_by,
                held={"cause": held, "why": HELD[held].format(stage=stage), "at": now.isoformat()} if held else None)
    records = {"Your Assist": {"attempts": [], "sent": mark}}
    if question:
        records["Your review"] = {"attempts": [], "question": question}
    conv["rounds"]["1"] = {"path": list(STAGES[:STAGES.index(stage) + 1]), "records": records, "handoffs": []}
    return conv


def runtime_with_flow(hass, runtime=None, entry=None, **parts):
    """A stand-in runtime (a SimpleNamespace of the parts a test builds itself, or those parts) with a real flow
    record, as the panel's decisions and the pipeline's counts read it (runtime.flow)."""
    from custom_components.assist_improver.flow import Flow
    runtime = runtime if runtime is not None else SimpleNamespace(**parts)
    runtime.flow = Flow(hass, entry, runtime)
    return runtime


GOALS = {  # How the analyzer reads each conversation (its first request is the key).
    FIRST: ANALYSIS,  # Pressed in turn 1, finished by hand in turns 2 and 3 (its sub-goals): friction.
    "Please toggle the fixture relay.": ANALYSIS,
    "Toggle the fixture relay again.": ANALYSIS,
    "Could you toggle the fixture relay?": ANALYSIS,
    "Switch the fixture relay off.": {"goals": [  # Each request done as asked: no friction.
        {"turn": 1, "summary": "Relay off", "kind": "state", "entity_id": TARGET, "state": "off"},
        {"turn": 2, "summary": "Asked about the press script", "kind": "fact", "fact": "the press script"},
        {"turn": 3, "summary": "Relay back on", "kind": "state", "entity_id": TARGET, "state": "on"},
        {"turn": 4, **PRESS}],
        "not_requests": [], "question_for_user": ""},
}  # A conversation cut short (variant's turns) reads as the goals of the turns it has.
SMOOTH = {"Turn the fixture relay off.", "Turn off the fixture relay.", "Fixture relay off, please."}


class Model:
    """The AI roles, scripted: each plays its part through its real tools, so every answer passes the same code
    checks a model's would. Scenarios say how each conversation reads, which goals an existing improvement already
    fixes, and when the model fails."""

    def __init__(self):
        self.calls = []  # (role, the conversation's first request, or None)
        self.fail_once = set()  # Conversations whose analysis fails once (a model error).
        self.analyzer_marks = set()  # Conversations whose goal the analyzer marks as already fixed.
        self.proposer_addresses = set()  # Conversations whose goal the proposer puts in already_addressed.
        self.proposer_unfixable = set()  # Conversations whose goal the proposer lists in not_fixed.
        self.drafts, self.reviews, self.tests = [], [], []
        self.slips = []  # A scripted answer the code checks refused: the role would fail, so the test does.
        self.feedback_followed = True  # Whether the judge finds a short reply ("Done.") no longer does what they
        self.feedback_checks = []  # objected to. What it was asked: (the objections' quotes, the replay).
        self.fact_checks = []  # What the judge was asked about what users wanted to know: each payload.
        self.notes_dropped = set()  # Conversations whose analysis leaves out the note they were sent in with.
        self.idea_changes = None  # What the proposer's idea changes, when not the press script.

    async def answer(self, hass, role, payload, tools):
        """One AI Task call, as Home Assistant's AI Task path makes it (ha_task.ScriptedTask): the role's part."""
        try:
            return await getattr(self, role)(hass, payload, tools)
        except AssertionError as err:
            self.slips.append(f"{role}: {err}")
            raise

    use = staticmethod(use)  # A tool call through Home Assistant's chat log (ha_task).

    async def triage(self, hass, payload, tools):
        first = payload["turns"][0]["user_says"]
        self.calls.append(("triage", first))
        return {"smooth": first in SMOOTH, "sure": True, "why": "Every request was done as asked."
                if first in SMOOTH else "The user had to step in."}

    async def journey_analyzer(self, hass, payload, tools):
        lines = payload["conversation"]
        first = lines[0]["user_says"]
        self.calls.append(("analyzer", first))
        if first in self.fail_once:
            self.fail_once.discard(first)
            raise RuntimeError("The model is overloaded")
        answer = deepcopy(GOALS[first])
        last = max(line["turn"] for line in lines if not line.get("sent_with_it") and not line.get("revision_of"))
        answer["goals"] = [goal for goal in answer["goals"] if goal["turn"] <= last]  # Every turn it has, no more.
        note = (payload.get("filed_by_user") or {}).get("note")
        if note and first not in self.notes_dropped:  # The note it was sent in with is its last turn: an objection
            serves = answer["goals"][0]["turn"]  # to how Assist behaved, a sub-goal of the goal it's about (P0.3).
            turn = next((line["turn"] for line in lines if line.get("sent_with_it")), lines[-1]["turn"])
            answer["goals"].append({"turn": turn, "summary": f"The note: {note.strip()}", "kind": "conduct",
                                    "about": "all", "quote": note.strip(), "serves": serves})
        for line in lines:  # Each revision note: an objection to how Assist behaved, under a goal its improvement fixed.
            if revised := line.get("revision_of"):
                answer["goals"].append({"turn": line["turn"], "summary": f"Revision: {line['user_says']}",
                                        "kind": "conduct", "about": "all", "quote": line["user_says"],
                                        "serves": revised["for_goals_at_turns"][0]})
        accepted = await self.use(hass, tools["submit_goals"], answer)
        if first in self.notes_dropped and note:  # Rejected for it, and it gives up.
            assert not accepted["accepted"], accepted
            return "Done."
        assert accepted["accepted"], accepted
        if first in self.analyzer_marks:
            found = payload.get("improvements_on_these_devices") or []
            marked = await self.use(hass, tools["mark_duplicate"], {
                "goal_turn": 1, "improvement": found[0]["improvement"],
                "why": "Its press script does the off-then-on the user finished by hand."})
            assert marked["accepted"], marked
        return "Done."

    async def hypothesis_generator(self, hass, payload, tools):
        """One idea for the goals in trouble, with both its stamps: the reviewer's approval (answering its blocking
        comment) and then Simulate's validation (P0.4); sent on when validated, else those goals are not fixed."""
        self.calls.append(("proposer", None))
        self.proposer_input = payload
        revising = (payload.get("sent_back") or {}).get("why") == "revision"
        addressed, goals, unfixable = [], [], []
        for scenario in payload.get("friction_scenarios", []):
            first = next(t["user"] for t in scenario["turns"] if t["turn"] == 1)
            for turn in scenario["goals_with_friction"]:
                if first in self.proposer_unfixable:
                    unfixable.append({"session": scenario["session"], "goal_turn": turn,
                                      "why": "The relay doesn't report the door's position."})
                elif first in self.proposer_addresses:
                    existing = (await self.use(hass, tools["list_improvements"]))["improvements"]
                    addressed.append({"session": scenario["session"], "goal_turn": turn,
                                      "improvement": existing[0]["improvement"], "why": "Its script does it."})
                else:
                    goals.append({"session": scenario["session"], "goal_turn": turn})
        sent = []
        if goals:
            hypothesis = {
                "title": "Press the relay with one named script" if revising else "Press the relay with one script",
                "cause": "Assist has no way to press the relay", "goals": goals, "expected": "One turn",
                "support": [{"session": goals[0]["session"], "turn": 1, "note": "The user wanted one press"}],
                "conflicts": [],
                "changes": self.idea_changes or [press_script(
                    "Presses the fixture relay once (named after the room)." if revising
                    else "Presses the fixture relay.")]}
            # Reviews are the round's: on a round picked up again (a retry, or a run that releases it), one whose
            # latest review of these changes approved them isn't asked about again.
            judged = [r["verdict"] for r in tools["ask_reviewer"].reviews
                      if r["hypothesis"]["changes"] == hypothesis["changes"]]
            if judged[-1:] != ["approve"]:
                review = await self.use(hass, tools["ask_reviewer"], hypothesis)
                self.drafts.append(review["outcome"])
                if review["outcome"] == "request_changes":  # Propose ⇄ Review: answer it, ask again.
                    responses = [{"comment": c["comment"], "resolution": "resolved", "note": "Kept to one press"}
                                 for c in review["comments"] if "blocking" in c["decorations"]]
                    review = await self.use(hass, tools["ask_reviewer"], {**hypothesis, "responses": responses})
                    self.drafts.append(review["outcome"])
            if "test_idea" in tools:  # Simulate's stamp, on this conversation, from the round's tests.
                self.tests.append(await self.use(hass, tools["test_idea"], hypothesis))
            if "test_idea" not in tools or self.tests[-1].get("validated"):
                sent, goals = [hypothesis], []
        unfixable += [{**goal, "why": "No idea for it was validated."} for goal in goals]
        answer = {"hypotheses": sent, **({"already_addressed": addressed} if addressed else {}),
                  **({"not_fixed": unfixable} if unfixable else {})}
        accepted = await self.use(hass, tools["submit_hypotheses"], answer)
        assert accepted["accepted"], accepted
        return "Submitted."

    async def hypothesis_reviewer(self, hass, payload, tools):
        seen = set()
        while "draft" in (got := await self.use(hass, tools["wait_for_draft"])):
            draft = got["draft"]
            first_time = draft["title"] not in seen and not draft.get("responses")
            seen.add(draft["title"])
            comment = ({"label": "issue", "decorations": ["blocking"], "subject": "Make sure it presses only once",
                        "evidence": [{"session": draft["goals"][0]["session"], "turn": 1}]} if first_time else
                       {"label": "praise", "decorations": ["non-blocking"], "subject": "One press, as asked"})
            self.reviews.append(comment["decorations"][0])
            assert (await self.use(hass, tools["submit_review"], {"comments": [comment]}))["accepted"]
        return "Finished."

    async def feedback_check(self, hass, payload, tools):
        """The judge: an Assist turn shows what the household objected to unless its reply is just "Done."; a reply
        gives what the user asked to know unless it says it doesn't know."""
        self.calls.append(("feedback_check", None))
        if "questions" in payload:
            self.fact_checks.append(payload)
            replies = {turn[0]["turn"]: turn[3]["assistant_replied"] for turn in payload["turns"]}
            return {"answers": [{"goal": q["goal"], "turn": q["turn"], "gives": "don't know" not in replies[q["turn"]],
                                 "why": "It says."} for q in payload["questions"]]}
        self.feedback_checks.append(([o["quote"] for o in payload["objections"]], payload["replay"]))
        replies = {turn[0]["turn"]: turn[3]["assistant_replied"] for turn in payload["replay"]}
        return {"answers": [{"goal": o["goal"], "turn": n, "shows": not done, "why": "Short." if done else "Wordy."}
                            for o in payload["objections"] for n in o["judge"]
                            for done in [self.feedback_followed and replies[n] == "Done."]]}

    async def editor(self, hass, payload, tools):
        self.calls.append(("editor", payload["idea"]))
        accepted = await self.use(hass, tools["submit_copy"], {
            "headline": "Press the relay in one go",
            "benefit": "Say “toggle the relay” and it presses once, so you don't have to ask again."})
        assert accepted["accepted"], accepted
        return "Written."


def idea(title="Press it once", text="Presses the fixture relay."):
    """An idea for the friction conversation's goal: one script that presses the relay (the scripted proposer's own
    changes, with the default text). Another text is other changes."""
    return {"title": title, "cause": "Assist has no way to press the relay",
            "goals": [{"session": "friction", "goal_turn": 1}], "expected": "One turn", "conflicts": [],
            "support": [{"session": "friction", "turn": 1, "note": "The user wanted one press"}],
            "changes": [press_script(text)]}


def press_script(text="Presses the fixture relay."):
    """The relay pressed once: a new script, in Home Assistant's own format, its description this text."""
    return {"kind": "script", "id": "press_the_relay", "config": {
        "alias": "Press the relay", "description": text, "sequence": [
            {"action": "switch.turn_off", "target": {"entity_id": TARGET}},
            {"action": "switch.turn_on", "target": {"entity_id": TARGET}}, {"stop": "Pressed."}]}}


NOT_FIXED = {"hypotheses": [], "not_fixed": [{"session": "friction", "goal_turn": 1,
                                              "why": "No idea for it was validated."}]}


async def approve(model, hass, tools, hypothesis):
    """ask_reviewer until the scripted reviewer approves (it asks for changes the first time it sees a title): the
    last review."""
    review = await model.use(hass, tools["ask_reviewer"], hypothesis)
    if review.get("outcome") == "request_changes":
        responses = [{"comment": c["comment"], "resolution": "resolved", "note": "Kept to one press"}
                     for c in review["comments"] if "blocking" in c["decorations"]]
        review = await model.use(hass, tools["ask_reviewer"], {**hypothesis, "responses": responses})
    return review


def propose_record(improver):
    """The friction conversation's Propose record, in its current round."""
    conv = improver.runtime.flow.record(improver.friction["id"])
    return conv["rounds"][str(conv["round"])]["records"]["Propose"]


async def tools_of_a_round(improver):
    """Each AI role's tools ({role: {name: tool}}), as Home Assistant handed them to it in one friction round through
    every stage."""
    seen, answer = {}, improver.model.answer

    async def watched(hass, role, payload, tools):
        seen.setdefault(role, tools)
        return await answer(hass, role, payload, tools)
    improver.model.answer = watched
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    return seen


@pytest_asyncio.fixture
async def improver(hass, effect_guards, monkeypatch):
    """The integration installed in an offline Home Assistant with Assist's real tools and scripts; the AI roles and
    the household's assistant scripted."""
    from homeassistant.core import ServiceRegistry
    from homeassistant import config as conf_util
    guarded, original = ServiceRegistry.async_call, _original_async_call()

    async def allow_script_reload(registry, domain, service, *args, **kwargs):
        # Reloading scripts is configuration, not a device action: accepting a changeset does it.
        if (domain, service) == ("script", "reload"):
            return await original(registry, domain, service, *args, **kwargs)
        return await guarded(registry, domain, service, *args, **kwargs)
    monkeypatch.setattr(ServiceRegistry, "async_call", allow_script_reload)
    await assist_home(hass, REPLAY_ENTITIES)
    open(hass.config.path("configuration.yaml"), "w").write("script: !include scripts.yaml\n")
    open(hass.config.path("scripts.yaml"), "w").write("{}\n")
    assert await async_setup_component(hass, "script", await conf_util.async_hass_config_yaml(hass))

    for entity_id in ENTITIES:
        hass.states.async_set(entity_id, "unknown")
    model = Model()
    slips = serve(hass, model)  # The AI roles answer through Home Assistant's real AI Task path.
    friction = variant(prepared_recording(), "friction01", FIRST)
    assistant, client = await bind_agent(hass, friction)  # The household's own assistant, replaying from a script.
    entry = fake_entry({CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku"})
    await async_setup_entry(hass, entry)
    runtime = entry.runtime_data
    recorder = runtime.recorder

    async def arrives(record):
        """A conversation finishing, as the recorder keeps it (recordings.as_kept)."""
        await recorder.store(record["id"]).async_save(as_kept(hass, record))
        recorder.index[record["id"]] = {"status": record["status"], "user_turns": len(record["turns"])}

    async def run():
        pipeline = runtime.pipeline
        pipeline.start(trigger="manual")
        await pipeline._task
        return pipeline.current

    async def log():
        sent = []
        connection = SimpleNamespace(user=SimpleNamespace(id="alex", is_admin=True),
                                     send_result=lambda i, r: sent.append(r), send_error=lambda *a: sent.append(a))
        await _ws(panel.ws_log, hass, connection, {"id": 1})
        return sent[-1]["events"]
    try:
        yield SimpleNamespace(hass=hass, entry=entry, runtime=runtime, recorder=recorder, model=model, client=client,
                              friction=friction, arrives=arrives, run=run, log=log)
    finally:
        await async_unload_entry(hass, entry)
    assert slips == [], f"The scripted roles called tools off their schemas: {slips}"
