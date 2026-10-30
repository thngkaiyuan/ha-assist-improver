"""The shapes of what the improver stores: one TypedDict per kind of record, with what each field holds.

Every field is optional, since a record has the fields its writer had at the time. A field only earlier versions
wrote is deleted here with its last reader, and the check of the household's own stores leaves it out. Parts whose
shape belongs to someone else (a provider's settings, a model's answer, Home Assistant's own states and registries)
are plain dicts. fixture-test/test_records.py checks the fixture's stores, and the household's own where present,
against these.
"""

from typing import Any, Literal, TypedDict


# The recordings: the index, and one recording (stored packed or compacted, retention.py; these are the unpacked form).

class RecordingSummary(TypedDict, total=False):
    """An entry in the recordings index (Recorder.index): what's known without reading the recording."""
    id: str
    started: str
    updated: str
    status: str  # active, session_expired, interrupted or capture_timed_out.
    user_turns: int
    stored: str  # packed or compact (retention.py); absent while kept in full.
    deleting: bool  # Deleted during a run: out of view, and deleted once the run ends (Recorder.forget_later).


class Turn(TypedDict, total=False):
    """One user turn: what was said, the home as Assist saw it then, and what Assist was given."""
    user: dict  # The user's message: content, created, role.
    source: dict  # states, entities, areas, devices, exposure, home_name, time_zone, at, attribute_policy.
    prepared: dict  # api_id, api_prompt, tools, provider_config, provider_model_capabilities, context.
    preparations: list[dict]  # The chat log as each answer was prepared.
    context_limitations: list[str]  # What the turn's record lacks when the assistant had no LLM API.


class Recording(TypedDict, total=False):
    """One Assist conversation, as the recorder keeps it (Recorder.sessions, and its store)."""
    format: int
    id: str
    conversation_id: str
    started: str
    last_activity: str
    core_version: str
    integration_version: str
    status: str  # As in its summary.
    events: list[dict]  # The chat log's events: at, type, data.
    turns: list[Turn]
    state_changes: list[dict]  # at, entity_id, old, new.
    flaps_not_kept: int  # Availability flaps left out of state_changes (digest.flap), counted for the digest.
    limitations: list[str]  # What the recording may be missing, and why.
    chat_log: dict  # The chat log at the end.
    practice: dict  # Sandbox mode: simulated_changes, unsimulated_actions.
    compact: bool  # Kept as a digest (retention.compact): read by every role, never replayed.


# What one stage hands the next (SPEC-CORE.md 3): typed here, checked where it's made (contracts.py, with the fields
# each stage must hand on), and read by the next stage through these shapes alone.

class Goal(TypedDict, total=False):
    """A goal the user had in a turn (a device state, a sequence, a fact, controls they chose: ask), or a sub-goal: a
    part of its goal, or a reaction to it (a step, conduct, a wrap-up or a thanks)."""
    turn: int
    summary: str
    kind: str  # state, sequence, fact, action, ask, step, conduct, wrap-up or thanks.
    entity_id: str
    entity_ids: list[str]  # Ask: the controls left open; fact: the devices it's about; action: the devices it's for.
    state: str
    states: list[str]
    brightness_pct: int
    fact: str  # Fact: what the answer needs, never the answer.
    outcome: str  # Action: what it does (analyzer.OUTCOMES), whatever service does it.
    message: str  # Action: the words to speak, the household's own.
    serves: int | None  # A sub-goal: the turn of the goal it serves.
    about: int | str  # Conduct: the turn of the Assist reply the household objected to ("all": Assist in general).
    quote: str  # Conduct: the household's own words; fact: the form they asked for the answer in, in their words.
    improvement: str | None  # Conduct: an applied improvement that makes Assist do what they objected to.
    reached: bool | None  # Code's verdict from the recording (a fact's: the judge's, answered).
    answered: dict  # Fact: the judge's verdict on the reply that ends its run of steps: turn, gives, why.
    home_couldnt: bool  # An unmet root goal whose device was unavailable as it was asked for.
    user_chose: list[dict]


class SideEffect(TypedDict, total=False):
    """A device Assist changed that no goal asked for, that didn't end as it started."""
    entity_id: str
    start: str | None
    end: str | None
    turn: int | None  # The turn of Assist's change.


class AnalysisResult(TypedDict, total=False):
    """Analyze → Propose and Simulate: one conversation's goals and the trouble on each (analyzer.to_analysis)."""
    recording_id: str
    goals: list[Goal]
    not_requests: list[dict]
    extra_turns: list[int]
    pending_turns: list[int]
    side_effects_left: list[SideEffect]
    question_for_user: str | None  # It couldn't tell what the user wanted.
    turns: int
    problems: list[str]  # What the checks found in an answer kept though none was accepted.
    duplicates: list[dict]  # Goals an existing improvement already fixes (marked by the analyzer).
    triaged: dict  # The quick check found it smooth (no goals extracted).


GoalRef = TypedDict("GoalRef", {"session": str, "goal_turn": int}, total=False)
Change = TypedDict("Change", {  # One change a hypothesis makes (hypotheses.schema._CHANGE).
    "kind": str, "text": str, "for": str, "replaces": list[str], "assistant": str, "target": str, "entity_id": str,
    "alias": str, "expose": bool, "script_name": str, "steps": list[dict], "fields": list[dict]}, total=False)


class TriageVerdict(TypedDict, total=False):
    """Triage → Analyze: the quick check's verdict on one conversation (triage.py). Only a sure smooth ends it there
    (Looked smooth); anything else goes on to the full analysis."""
    smooth: bool
    sure: bool
    why: str


class TriageCheck(TypedDict, total=False):
    """The quick check as kept with its conversation's record (analysis.analyze): its verdict once it completed."""
    status: str
    output: TriageVerdict | None
    error: dict | None
    usage: dict
    entity_id: str | None
    kept: bool  # A verdict kept from an earlier check of the same conversation: nothing paid again.


class Hypothesis(TypedDict, total=False):
    """Propose → Simulate: one idea, the goals it fixes, and its changes."""
    title: str
    goals: list[GoalRef]
    cause: str
    changes: list[Change]
    support: list[dict]
    conflicts: list[dict]
    expected: str


class ReviewResponse(TypedDict, total=False):
    """The generator's answer to one blocking comment, asking again about the same idea."""
    comment: int
    resolution: str  # resolved or wont_fix.
    note: str


class GoalFriction(TypedDict, total=False):
    """One goal a draft names, with its trouble (hypotheses.goal_friction)."""
    session: str
    goal_turn: int
    summary: str
    friction: list[dict]


class DraftFriction(TypedDict, total=False):
    """The trouble of each goal a draft names, and what each kind of trouble means."""
    goals: list[GoalFriction]
    kinds: dict[str, str]


class WhereItApplies(TypedDict, total=False):
    """The other conversations a draft's changes would reach (hypotheses.where_it_applies_to)."""
    sessions: list[dict]
    count: int
    more: str


class Draft(Hypothesis, total=False):
    """Propose → Review: an idea the generator asks the reviewer about (hypotheses.AskReviewer), with what code found
    about it."""
    responses: list[ReviewResponse]
    checks: dict  # What its changes do, checked against the household (evidence.check).
    related_improvements: list[dict]  # Existing improvements on the same devices.
    friction_it_targets: DraftFriction
    sessions_where_it_would_apply: WhereItApplies


class Evidence(TypedDict, total=False):
    """What a review comment rests on: a session and turn, a device, or a pattern (note)."""
    session: str
    turn: int
    entity_id: str
    note: str


class ReviewComment(TypedDict, total=False):
    label: str  # hypotheses.LABELS.
    decorations: list[str]  # blocking or non-blocking (one).
    subject: str
    discussion: str
    evidence: list[Evidence]


class ReviewAnswer(TypedDict, total=False):
    """Review → its loop: the reviewer's accepted review of one draft (hypotheses.ChannelSubmitReview): its comments
    and, when it knows a better version, that one."""
    comments: list[ReviewComment]
    revised: Hypothesis


class NumberedComment(ReviewComment, total=False):
    comment: int  # Its number, for the generator's responses.


class ReviewResult(TypedDict, total=False):
    """Review → Propose: what the generator gets back (hypotheses.review_result): the outcome the comments decide,
    each comment numbered, the reviewer's version if any, the next step while a blocking comment stands, and what
    code found about the draft."""
    outcome: str  # approve or request_changes.
    comments: list[NumberedComment]
    revised: Hypothesis
    next_step: str
    checks: dict


class ProposalAnswer(TypedDict, total=False):
    """The proposal's answer: its ideas, goals outside the assistant's control, goals already addressed."""
    hypotheses: list[Hypothesis]
    not_fixed: list[dict]
    already_addressed: list[dict]


class FailedStep(TypedDict, total=False):
    step: str  # What Assist tried, in plain words, and on what.
    error: str  # Home Assistant's error.


class Said(TypedDict, total=False):
    you: str  # What the user said.
    assist: str  # Assist's last reply to it.
    failed: list[FailedStep]  # The steps that failed in between, where any did.


class Replay(TypedDict, total=False):
    """One replay of a conversation, as its test keeps it (simulate.test): how it went, and how it fulfilled the goals
    by P0.1.1's measures (measures.judge)."""
    plan: str  # The id of the replays store's entry it was judged from (ReplayEntry).
    status: str  # reproduced (without the change) or done (with it).
    said: list[int]  # The conversation's turns it said, in order.
    replies: list[dict]  # Each turn said: turn, you, assistant, and failed (the steps that failed) where any did.
    goals: dict[str, str]  # {goal key: met, unmet or indeterminate}.
    goals_met: list[str]
    turns_per_root_goal: dict[str, int]
    home_couldnt: list[str]  # Root goals the home couldn't reach: their device was unavailable as it was asked for.
    counted: dict[str, list[int]]  # {root goal key: the turns it took}.
    side_effects_left: list[dict]  # entity_id, name (as the household names it), start, end, turn.
    started_from: dict  # The home and time it started from (cases.started_from).
    config: dict  # The scripts and the exposure it ran on (snapshot.assist_config).
    conduct: dict[str, dict]  # {conduct goal key: state, answers [{turn, shows, why}], set_aside}: the judge's.
    facts: dict[str, dict]  # {question's key: state, answers [{turn, gives, why}], set_aside}: the judge's.


class Test(TypedDict, total=False):
    """Simulate → Propose and the improvements: one test of an idea on its conversation (simulate.test), kept with
    its round's Propose record."""
    idea: str | None
    changes: str  # The digest of the idea's changes.
    hypothesis: dict
    at: str
    judged: dict  # {goal key: summary, turn, root, devices, kind, home_couldnt, quote, about}: every goal it judged.
    before: Replay
    after: Replay
    gains: list[str]  # The measures the change made strictly better (measures.gains).
    validated: bool  # Strictly better (measures.easier).
    setup: dict  # The change as the sandbox ran it (changes.setup_from_changes).
    tested: dict  # What it was tested against: scripts, exposure, and prompts (the instructions it edits, now).
    provider: dict  # The assistant it ran on.
    reused: bool  # Its replay with the change was an earlier round's, kept: not paid for again (never resampled).
    user_id: str | None  # The person speaking.
    recorded: list[Said]  # The conversation as it went.
    notes: list[str]


class ReplayEntry(TypedDict, total=False):
    """One replay as the replays store keeps it, by its plan's id (simulate.Replays)."""
    plan: dict  # What it replays and how (simulate.before_plan, after_plan); its id is their digest.
    recording_id: str
    at: str
    status: str  # running, reproduced, not_reproducible, done, unsupported, infrastructure_failure, interrupted or
    # cancelled.
    error: dict | None
    notes: list[str]
    usage: dict | None
    said: list[int]
    steps: list[dict]  # Each turn said: turn, input, reply, output, transitions, brightness, external_calls, ...
    verdicts: dict  # {digest of the goals judged: model, answers {goal's content digest: [{turn, shows, why}]},
    #   usage}: the judge's, on whether its Assist turns show what the household objected to.


# The run log (RoleRuns): one record per AI role call, and per pipeline run.

class RoleCall(TypedDict, total=False):
    """An AI role's call (reflective.run_role, then the stage that asked): what went in, what came back, what it
    cost."""
    id: str
    kind: str  # journey_analyzer or hypothesis_generator.
    role: str
    entity_id: str  # The AI Task it ran on.
    status: str  # running, completed, failed, held or interrupted.
    started: str
    finished: str
    seconds: float
    input_chars: int
    input_sha256: str
    instructions_sha256: str
    conversation_id: str  # The AI Task's own conversation.
    output: dict | None
    reply: str | None
    error: dict | None  # type and message.
    problems: list  # What the answer's checks found.
    submissions: int  # Times the role called its submit tool.
    answer_status: str  # How the answer came: accepted, triaged_smooth, awaiting_analysis, unreadable, error, ...
    usage: dict  # Provider-reported token counts where available.
    run_id: str  # The pipeline run it was made in, with its trace.
    trace_id: str


class Analysis(RoleCall, total=False):
    """journey_analyzer: one conversation's quick check and analysis (analysis.analyze)."""
    recording_id: str
    digest_sha256: str  # Of what the analyzer read: a held verdict is reused only for the same.
    agentic: bool
    triage: TriageCheck
    analysis: AnalysisResult  # The goals, with their friction, as the pipeline reads them.
    fact_check: dict  # The reply judge's call on what users asked to know: status, answers, error, usage.


class Proposal(RoleCall, total=False):
    """hypothesis_generator (proposal.propose): the proposed changes, and the reviewer's verdict on each."""
    sessions: list  # The conversations it was given.
    reviews: list[dict]  # On one conversation, its round's (Flow): an earlier attempt's too.
    reviewer_run: dict  # The reviewer's own call: status, error, usage, output.


class PipelineRun(TypedDict, total=False):
    """One run of the pipeline (pipeline.py), kept as it goes, with the progress the flow publishes (flow.Flow.run)."""
    id: str
    kind: str  # pipeline.
    trigger: str  # manual, scheduled, filed (the fast lane), revision or conflict.
    status: str  # running, done, failed, cancelled or interrupted.
    started: str
    finished: str
    stage: str | None  # The stage at work on the conversation it's on (flow.KEYS).
    # total and done (conversations), the one it's on (conversation), taken_in, analyzed, new_improvements, duplicates,
    # unreadable, held (with the first one's why), and problems (failures.py).
    progress: dict
    error: str | None
    only: str | None  # A run of one step.
    picked: list[str]  # The conversations a picked run (or the fast lane) admits only.
    conversation: str  # The one a revision's or a conflict's run takes in at Propose.
    admitted: list[str]  # The new ones it took in,
    released: list[str]  # the held ones it released,
    moved: list[str]  # and both, in the order it moved them.
    results: dict  # {recording id: {stage, outcome, held}}, for each whose round stopped in it.
    note: str | None
    trace_id: str


# The improvements (improvements.py).

class Copy(TypedDict, total=False):
    """Write-up → Review: the editor's headline and what-you-get for one improvement (editor.write), or why it failed
    (it goes up with the proposer's title)."""
    status: str  # written or failed.
    entity_id: str | None
    headline: str
    benefit: str
    error: dict | None


class Decision(TypedDict, total=False):
    """Review (the household's) → the improvement: one decision and what came of it (Improvements.decide)."""
    decision: str  # accept, decline, revise or undo.
    note: str
    reason: str  # Given with it: declined as not_an_issue.
    by: str | None
    at: str
    outcome: str  # done, recorded, conflict, ...
    auto: bool  # Accepted automatically (auto-accept).
    unannounced: bool  # Accepted automatically before it was ever announced as waiting: never up for review.


class Improvement(TypedDict, total=False):
    """What goes to the household for review: a set of changes, the test that showed what it gets them, and what
    they decided."""
    id: str
    title: str
    cause: str
    status: str  # awaiting_review, needs_attention, applied, declined, conflict or revised.
    created: str
    hypothesis: dict
    changes: list[dict]
    setup: dict
    tested: dict  # What its test ran against (Test.tested): changed since, accepting it meets a conflict.
    provider: dict  # The assistant it was tested on.
    user_ids: list  # The people it was tested for: a line for the person speaking goes to each (None: everyone).
    test: Test  # The test that validated it, as its round kept it.
    round: int  # The round of its conversation it was built in.
    fixes: list[str]  # The root goals its test helped ("session:turn").
    devices: list[str]  # The devices those goals want something from.
    copy: Copy | None
    decisions: list[Decision]
    application: dict | None  # How it was applied, to undo it.
    last_outcome: dict
    history: list[dict]
    revises: dict  # The improvement it revises: {id, title, note}, and conflict: true when sent back after one.
    revised_by: str
    conflicts: list[dict]  # What stood in the way when an accept met a conflict (sent back, the proposer hears it).
    conversation: str  # The one conversation it came from and was tested on (SPEC.md P0.10).
    reach: list[dict]  # What each change applies to: the household, one assistant, or one person (SPEC.md P0.10).
    checked_on: list[str]  # The conversations the proposer had in view when it made it (short ids).


class Transcript(TypedDict, total=False):
    """What a kept conversation said (conversations.Transcripts), as of its recording's last change."""
    updated: str | None
    turns: list[Said]
    failed: int  # Assist's tool calls that failed in it.


RUN_LOG: dict[str, Any] = {  # The run log's record type for each kind.
    "journey_analyzer": Analysis, "hypothesis_generator": Proposal, "pipeline": PipelineRun}


# The handoffs between stages (SPEC.md P0.4): what each move hands the stage it enters. Unlike the stored records
# above, every field is required, and a field that steers what happens next takes only its fixed set of values.

OUTCOMES = Literal["looked smooth", "no trouble found", "duplicate", "couldn't be read", "no fix found", "improved",
                   "declined", "archived", "marked as a test"]


class Admitted(TypedDict):  # Your Assist → Triage or Analyze: a run took it in.
    conversation: str
    round: int


class Checked(TypedDict):  # Triage → Analyze: the quick check found it needs a full analysis.
    conversation: str
    round: int
    smooth: bool


class Analysed(TypedDict):  # Analyze → Propose: the goals it carries, and its question for the household, if any.
    conversation: str
    round: int
    goals: list[str]
    question: str | None


class Proposed(TypedDict):  # Propose → Write up: the improvements it sent on (or the one still waiting), or the question.
    conversation: str
    round: int
    improvements: list[str]
    question: str | None


class Written(TypedDict):  # Write up → Your review: what waits there for the household.
    conversation: str
    round: int
    improvements: list[str]
    question: str | None


class SentBack(TypedDict):  # Your review → Propose: a new round, for a revision or a conflict the household agreed to.
    conversation: str
    round: int
    improvement: str
    why: Literal["revision", "conflict"]


class Undone(TypedDict):  # Your household → Your review: its applied improvement undone, up for review again.
    conversation: str
    round: int
    improvement: str


class Ended(TypedDict):  # Any stage → Your household: how its round ended.
    conversation: str
    round: int
    outcome: OUTCOMES


HANDOFFS = {kind.__name__: kind for kind in (Admitted, Checked, Analysed, Proposed, Written, SentBack, Undone, Ended)}
