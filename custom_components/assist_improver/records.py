"""The shapes of what the improver stores: one TypedDict per kind of record, with what each field holds.

Every field is optional, since a record has the fields its writer had at the time. A few come only from earlier
versions and are marked so; nothing reads them. Parts whose shape belongs to someone else (a provider's settings, a
model's answer, Home Assistant's own states and registries) are plain dicts. fixture-test/test_records.py checks the
fixture's stores, and the household's own where present, against these.
"""

from typing import Any, TypedDict


# The recordings: the index, and one recording (stored packed or compacted, retention.py; these are the unpacked form).

class Filed(TypedDict, total=False):
    """On-demand improvements: the user asked Assist to file the conversation."""
    at: str
    note: str  # What the user wants improved, in their words, if they said.
    by: str  # Who filed it (a user ID).


class RecordingSummary(TypedDict, total=False):
    """An entry in the recordings index (Recorder.index): what's known without reading the recording."""
    id: str
    started: str
    updated: str
    status: str  # active, session_expired, interrupted or capture_timed_out.
    user_turns: int
    stored: str  # packed or compact (retention.py); absent while kept in full.
    filed: Filed


class Turn(TypedDict, total=False):
    """One user turn: what was said, the home as Assist saw it then, and what Assist was given."""
    user: dict  # The user's message: content, created, role.
    source: dict  # states, entities, areas, devices, exposure, home_name, time_zone, at, attribute_policy.
    prepared: dict  # api_id, api_prompt, tools, provider_config, provider_model_capabilities, context.
    preparations: list[dict]  # The chat log as each answer was prepared.


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
    limitations: list[str]  # What the recording may be missing, and why.
    chat_log: dict  # The chat log at the end.
    practice: dict  # Sandbox mode: simulated_changes, unsimulated_actions.
    filed: Filed


# The run log (RoleRuns): one record per AI role call, per pipeline run, and per test of hypotheses.

class RoleCall(TypedDict, total=False):
    """An AI role's call (reflective.run_role, then the stage that asked): what went in, what came back, what it
    cost."""
    id: str
    kind: str  # journey_analyzer, hypothesis_generator, hypothesis_reviewer or quick_check.
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
    triage: dict  # The quick check's status, output, error, usage and entity_id.
    analysis: dict  # The goals, with their friction, as the pipeline reads them.


class Proposal(RoleCall, total=False):
    """hypothesis_generator (proposal.propose): the proposed changes, and the reviewer's verdict on each."""
    sessions: list  # The conversations it was given.
    reviews: list[dict]
    seeded_reviews: dict
    reviewer_run: dict  # The reviewer's own call: status, error, usage, output.
    tested: bool  # Whether the test stage is done with its hypotheses.
    test_attempts: int


class Review(RoleCall, total=False):
    """hypothesis_reviewer, run alone on one hypothesis (a developer service)."""
    sessions: list


class PipelineRun(TypedDict, total=False):
    """One run of the pipeline (pipeline.py), kept as it goes."""
    id: str
    kind: str  # pipeline.
    trigger: str  # manual, scheduled or filed.
    status: str  # running, done, failed, cancelled or interrupted.
    started: str
    finished: str
    stage: str | None
    progress: dict  # Per stage: counts, timings and problems.
    error: str | None
    force: bool
    only: str | None  # A run of one step.
    note: str | None
    trace_id: str


class TestReport(TypedDict, total=False):
    """hypothesis_tests: what testing hypotheses found (validation.test_hypotheses)."""
    id: str
    kind: str
    status: str
    started: str
    finished: str
    output: dict
    run_id: str
    trace_id: str


# Cases and their baselines (casebook.py, baseline.py).

class Transition(TypedDict, total=False):
    entity_id: str
    old: str
    new: str


class BaselineStep(TypedDict, total=False):
    """One replayed turn of a baseline."""
    turn: int
    input: str
    output: list  # The chat log content the replay added.
    original_reply: str
    ha_effects_match: bool  # The goal's devices got the recorded effects.
    same_tool_calls: bool
    differences_elsewhere: dict
    transitions: list[Transition]
    recorded_transitions: list[Transition]
    prompt_matches_recording: bool
    external_calls: list  # When the turn failed: what it tried to reach.
    incomplete: bool
    ha_action_trace_matches: bool  # Earlier versions.
    semantic_review: str  # Earlier versions: the manual pilot's review.


class Baseline(TypedDict, total=False):
    """A case's conversation replayed without a fix, to check it reproduces (BaselineTrial.export)."""
    status: str  # not_run, running, reproduced, not_reproducible, unsupported, infrastructure_failure, cancelled,
    # or interrupted.
    plan: dict  # What it replays and how; its id is their digest.
    steps: list[BaselineStep]
    error: dict | None
    destination_met: bool | None  # Whether the replay met the goal without a fix.
    usage: dict
    attempts: int  # Earlier versions (and a case not run yet).
    run_id: str  # Earlier versions.
    next_turn: int  # Earlier versions.
    reviews: list[dict]  # Earlier versions: the manual pilot's review between turns.
    effect_boundary: str  # Earlier versions.


class Case(TypedDict, total=False):
    """A goal in a recorded conversation that tests replay (cases.create_case)."""
    format: int
    id: str
    recording_id: str
    recording_digest: str
    title: str
    question: str | None
    turns: list  # The user turns the goal spans.
    original_user_turns: int
    intervention_turns: list
    goal_unmet: bool
    acceptable_devices: list
    destination: dict  # status, summary, criteria, evidence: when the goal counts as met.
    feedback: list[dict]
    revision: int
    audit: dict | None
    baseline: Baseline
    baseline_history: list[Baseline]  # Baselines under earlier plans.


# Changes tested on a case (candidates.py) and the improvements built from them (improvements.py).

class CandidatePlan(TypedDict, total=False):
    """What a change's test replays: the hypothesis, its edits, and the baseline it's compared with."""
    format: int
    id: str  # The digest of the rest.
    kind: str  # hypothesis_changes.
    hypothesis: dict | str
    evaluation_case_id: str
    baseline_plan: dict
    baseline_digest: str
    judge: dict  # What the result is judged by: destination, acceptable devices, original turns, baseline met.
    coverage: list[dict]
    user_inputs: list
    setup: dict
    definitions: dict
    definition_evidence: dict
    compiled_scripts: dict
    edits: list
    added_tools: list[dict]
    limits: list
    provider_option_changes: dict
    system_prompt_change: bool
    counterfactual_allowance_used_by_this_plan: int
    retained_turns: list
    theme: dict  # Earlier versions, as are the rest.
    proposal_origin: str
    semantic_review: str
    regression_review: str
    application: str


class CandidateResult(TypedDict, total=False):
    """What a change's test found (CandidateTrial.run)."""
    status: str
    plan_id: str
    evaluation_case_id: str
    evidence_kind: str
    input: str
    output: list
    turns: list[dict]  # Each replayed turn: turn, input, output, reply.
    followups: list[dict]
    followup_check: dict | None
    transitions: list[Transition]
    simulated_calls: list[dict]
    external_calls: list
    real_device_actions: int
    assessment: dict  # How it compares with the baseline: turns, goal met, unintended targets.
    coverage: list[dict]
    usage: dict
    error: dict | None
    run_id: str
    theme_id: str  # Earlier versions, as are the rest.
    validated_improvement: bool
    application: str


class Candidate(TypedDict, total=False):
    """A change tested on a case: its plan, and once it ran, its result."""
    plan: CandidatePlan
    status: str  # running, awaiting_review (it improved the goal), not_improved, ...
    result: CandidateResult | None
    evidence: list  # Earlier versions, as are the rest.
    reviews: list
    application: None


class Improvement(TypedDict, total=False):
    """What goes to the household for review: a set of changes, the tests that showed what it gets them, and what
    they decided."""
    id: str
    title: str
    cause: str
    status: str  # awaiting_review, applied, declined, revision_requested, ignored, needs_attention, ...
    created: str
    hypothesis: dict
    changes: list[dict]
    setup: dict
    tested: dict  # The scripts and exposure the tests ran with.
    provider: dict  # The assistant it was tested on.
    user_id: str | None  # The person it's for, when it's personal.
    tests: list[dict]
    untested_goals: list
    also_addressed: list[dict]  # Goals found already fixed by it.
    copy: dict | None  # The editor's headline and what-you-get.
    decisions: list[dict]
    application: dict | None  # How it was applied, to undo it.
    last_outcome: dict
    history: list[dict]
    revises: dict  # The improvement it revises.
    revised_by: str


class FailedStep(TypedDict, total=False):
    step: str  # What Assist tried, in plain words, and on what.
    error: str  # Home Assistant's error.


class Said(TypedDict, total=False):
    you: str  # What the user said.
    assist: str  # Assist's last reply to it.
    failed: list[FailedStep]  # The steps that failed in between, where any did.


class Transcript(TypedDict, total=False):
    """What a kept conversation said (conversations.Transcripts), as of its recording's last change."""
    updated: str | None
    turns: list[Said]
    failed: int  # Assist's tool calls that failed in it.


class Waiting(TypedDict, total=False):
    """Where the pipeline's taken-in conversations wait (Pipeline.store)."""
    at: dict[str, str]  # Recording ID: triage or analyze.


RUN_LOG: dict[str, Any] = {  # The run log's record type for each kind.
    "journey_analyzer": Analysis, "hypothesis_generator": Proposal, "hypothesis_reviewer": Review,
    "quick_check": RoleCall, "pipeline": PipelineRun, "hypothesis_tests": TestReport}
