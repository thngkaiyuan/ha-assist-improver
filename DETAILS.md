# Assist Improver: detail

Moved out of SPEC.md on 2026-10-26. Not the spec: where this disagrees with SPEC.md, SPEC.md wins.

Citations in parentheses name the maintainer's earlier notes, which aren't in this repository.

## P1. What it's for

- **P1.1** The improver records the household's Assist conversations and proposes changes that get the same result in
  fewer turns. It maximises goals met and minimises turns per root goal and side effects left. (Kai, 2026-10-26)
- **P1.2** Conversations go in; improvements come out. (Kai, 2026-10-26)
- **P1.3** The improver learns from trouble in the household's conversations. Trouble is one of four kinds.
  (DESIGN.md, 2026-10-25; SPEC-CORE.md)
  - **P1.3.1** A goal left unmet: Assist guessed or missed what the user wanted. (DESIGN.md, 2026-10-25)
  - **P1.3.2** Extra turns: the user had to repeat, correct or clarify themselves. (DESIGN.md; README)
  - **P1.3.3** A side effect left behind: a device Assist changed that no goal asked for, and that Assist didn't leave
    as it started. (TEXT-REVIEW.md, 2026-10-25; Kai, 2026-11-02)
  - **P1.3.4** The household's feedback on how Assist behaved (a complaint, its wording, its length, "next time do
    X"), given in a turn or in the note sent with the conversation. (Kai, 2026-10-26: "let's make sure it
    plumbs/flows its way correctly down the pipeline all the way through")
- **P1.4** Effort is the user's work to reach, or get back to, the world states they intended; SPEC P0.1.1 says how it's
  counted for now. (Kai, 2026-11-02: "the amount of energy/work done needed for the user to arrive/return to their
  projected world state")
- **P1.5** It works like self-driving: it finds the goals that needed the user to step in, works out whether each
  extra turn was avoidable, tests ideas for reaching the goal without them, and proposes the ones that work.
  (Kai, 2026-10-21: "identify outcomes (i model them as destinations, in a certain space, like full-self driving)
  that had interventions")
- **P1.6** The aim: the household no longer feels any need to watch over Assist; they just enjoy it.
  (Kai, 2026-10-22: "they won't even feel the need to supervise. they'll just enjoy.")
- **P1.7** It should eventually work for every household, and for however each person phrases a request, not only
  for Kai's style. (Kai, 2026-10-23: "i eventually want this pipeline to work for everyone too, right?")
  - **P1.7.1** Kai's household is the pilot, but no people, devices or paths are hardcoded. (PRODUCT-SPEC.md,
    2026-10-21)
- **P1.8** It isn't a general maintenance bot. Cleanup, consolidation and preference tuning are in scope only when
  they cut trouble (for example removing conflicting presets or context bloat), never as tidiness for its own sake.
  (PRODUCT-SPEC.md, 2026-10-21)
- **P1.9** On Kai's household it should come up with the kind of changes he made by hand: scripts and prompt
  personalization. (Kai, 2026-10-21: "it would probably have created a bunch of scripts for me, and added the prompt
  personalization that we did by hand")
- **P1.10** It works at Home Assistant's conversation, state and tool layer. (PRODUCT-SPEC.md, 2026-10-21)
  - **P1.10.1** "Smart exposure" (a model choosing which entities Assist sees) is a separate integration, not part of
    the improver. (Kai, 2026-10-22: "can u spawn this off as a side quest?")
- **P1.11** These limits are never relaxed silently. (PRODUCT-SPEC.md, 2026-10-21)
  - **P1.11.1** Replays work whatever AI provider the household's assistant uses. (PRODUCT-SPEC.md, 2026-10-21)
  - **P1.11.2** A conversation's goals are settled before anything is tested against them. (PRODUCT-SPEC.md,
    2026-10-21)
  - **P1.11.3** Nothing in the home moves during a simulation. (PRODUCT-SPEC.md, 2026-10-21)
  - **P1.11.4** A change counts only when more goals are met, or the same goals are met in fewer turns, or the same
    goals in the same turns with fewer side effects left (P6.1). (Kai, 2026-10-24: "maximize goals met, in addition to
    minimizing extra turns", replacing "strict turn reduction"; Kai, 2026-10-26: "agreed")
  - **P1.11.5** Counterfactuals are bounded (P6.3). (PRODUCT-SPEC.md, 2026-10-21)
  - **P1.11.6** Accepted improvements persist. (PRODUCT-SPEC.md, 2026-10-21)
- **P1.12** Nothing waits on a measure of interventions trending down until there's a good candidate measure; a bad
  metric mustn't hold the product back. (Kai, 2026-10-25: "I don't want a bad measure/metric to hold us back")
- **P1.13** Its icon is a loop with a sprout: growth and self-growth, like a flywheel. (Kai, 2026-10-22: "Go with C,
  the loop with a sprout. Polish it.")

## P2. The data model

- **P2.1 Conversation.** A conversation is a package that moves through the pipeline. It carries one canonical record
  for where it is, and one per stage for its state there; how they're stored may differ, but there's exactly one
  canonical record of each. (Kai, 2026-10-26: "there should only be one canonical record")
  - **P2.1.1** Every screen and every stage reads where a conversation is from that one record; none works it out
    differently. (SPEC-CORE.md, 2026-10-25; Kai, 2026-10-26)
- **P2.2 Turns.** A conversation has turns. Each maps to at least one goal. A turn is in the conversation (what the user
  said to Assist) or outside it: a note sent with the conversation to the improver, or a revision note on its
  improvement. (Kai, 2026-10-26)
  - **P2.2.1** A turn that corrects or clarifies a request, or answers Assist's question, maps to the goal it makes
    more exact and adds no goal of its own. (LABELS.md, 2026-10-22; DESIGN.md)
- **P2.3 Goals.** A goal is a child of a turn: what the user wants to achieve with it, a change of the world's state
  (the home, its devices, the user, Home Assistant itself, the assistant and its programming) from where it is to a
  destination. It can be a home's devices reaching a state, the user's knowledge reaching a state (knowing something),
  or the improver improving Assist's programming. (Kai, 2026-10-26)
  - **P2.3.1** A root goal is one destination: one world state, at one time. "Both garage doors open" is one goal,
    not one per door. (Kai, 2026-10-26)
  - **P2.3.2** A sub-goal is a change made only to unblock or support its parent: in a world where the parent goal
    doesn't exist, it wouldn't either. Sub-goals can nest. (Kai, 2026-10-26) A sub-goal exists only because of its
    parent, as a part of it or a reaction to it. A reaction names what it reacts to (its parent still unmet, or what
    Assist said or did in one turn, in the household's own words) and is met once that's resolved. (Kai, 2026-10-29)
  - **P2.3.3** A goal is met, unmet, or can't be told. (Kai, 2026-10-26: "have met/unmet states")
  - **P2.3.4** A note sent with a conversation is a turn whose goal is a sub-goal of that conversation's root goal (it
    wouldn't exist without it). (Kai, 2026-10-26)
  - **P2.3.5** A turn like "thanks" has a goal (getting the thanks across), a sub-goal of the root goal it thanks
    for; it's met only by saying it, so a replay keeps it. (Kai, 2026-10-26: "it's reasonable to assign them a
    goal ... even reasonable to assign this as a sub-goal"; Kai, 2026-10-27)
  - **P2.3.6** A goal is reached through Assist's actions: device commands, or replies with no tool use.
    (PRODUCT-SPEC.md, 2026-10-21)
  - **P2.3.7** A conversation can hold several root goals. (PRODUCT-SPEC.md, 2026-10-21)
  - **P2.3.8** What the user asked for separately, at different times, is separate root goals, even when their
    intent seems predictable. (Kai, 2026-10-23: "if i asked for them at a specific time, separately, then they should
    be separate. simple as that.")
  - **P2.3.9** Asking again after a goal was reached is a new root goal: doors opened, then "now close them" later,
    is two root goals, open then close. (Kai, 2026-10-23: "Two: open, then close")
  - **P2.3.10** A destination reached through device changes in order (a relay pressed off, then on) is still one
    destination: the world state they bring about ("both garage doors open"), not the sequence.
    (Kai, 2026-10-26, P2.3.1; replaces LABELS.md's "sequence" kind of goal)
  - **P2.3.11** A goal may name several devices when the user would accept any of them; it's met when one of them
    reaches the state (a smart plug or the lamp itself can each turn a lamp off). (LABELS.md; Kai, 2026-10-23: "either
    is fine")
  - **P2.3.12** A goal states only what the user wanted: a setting, such as a light's brightness, only if they asked
    for one (P9.7.1). (LABELS.md)
  - **P2.3.13** A sub-goal is any step taken only to reach its parent: asking what Assist can do (which controls it
    has, in order to set the fan to fan-only), telling it how, finishing a half-done step, undoing something it did.
    (LABELS.md, 2026-10-22; Kai, 2026-10-23)
  - **P2.3.14** Checking on a result the reply didn't make clear stays in the original goal's tree: a clear report would
    have let the user go straight on. (Kai, 2026-10-23: "they're still part of the original goal"; 2026-11-02: "asks
    about state the assistant forgot to mention or confirm")
  - **P2.3.15** Goals that Assist's mistakes create are plain nested goals (the user wanted light A; Assist turned on A
    and B; B going off is a sub-goal). There's no separate repair or need category. (Kai, 2026-10-23: "after all
    they're just nested goals, aren't they")
  - **P2.3.16** A root goal's turns are the turns said to Assist that map to it or any of its sub-goals; a met
    sub-goal never makes its root goal met. (fixture-test/SPEC.md, from README; Kai, 2026-10-27)
- **P2.4 Improvement.** A set of changes to the household's Assist (its programming), with the test that showed what it
  gets them. An improvement comes from one conversation's round and is tested on that conversation: each conversation
  either has improvements or it doesn't. Its changes can still be broad and help elsewhere: problems are found in
  specific conversations, but solutions can be broad. (Kai, 2026-10-26: "each conversation either has improvements or it
  doesn't")
  - **P2.4.1** One improvement is one set of changes, however it's worded. (MILESTONE-7.md, 2026-10-23;
    fixture-test/SPEC.md)
  - **P2.4.2** The same need, phrased differently in another conversation, is a goal in that conversation's own tree,
    worked on in that conversation's own round. (Kai, 2026-10-26)
  - **P2.4.3** An improvement keeps lasting links to its conversation, the household setup it was made against, its
    test, and the feedback on it. (PRODUCT-SPEC.md, 2026-10-21)
- **P2.5 What's kept.** The records keep apart: what was observed (the conversation, and what happened in the home),
  what was inferred (goals, and whether each was met), replays, ideas, each version of an improvement, reviews and
  notes, who accepted what and when, what applying it did, household knowledge, and what runs used (actual kept
  apart from estimated). (PRODUCT-SPEC.md, 2026-10-21)
  - **P2.5.1** Every inference stays distinguishable from an observation. (PRODUCT-SPEC.md, 2026-10-21)
  - **P2.5.2** A missing observation is never made up. (PRODUCT-SPEC.md, 2026-10-21)
  - **P2.5.3** Decisions made with earlier review menus keep their meaning. (PRODUCT-SPEC.md, 2026-10-25)
  - **P2.5.4** Stored data is migrated once, tested on old-format data first, and code kept only to read old formats
    is removed. (Kai, 2026-10-25)
- **P2.6 Rounds.** Each time a conversation enters the pipeline is a round, with fresh limits (P6.3, P4.4.3): when a run
  admits it new, when it's sent in again (P4.5), or when Your review sends it back to Propose (a revision, or an accept
  that met a conflict, P12.4). Within a round it flows until the round ends: an improvement for it waits at Your review;
  it ends in Your household (P3.3); or it's held for a problem (P4.3). A question pauses it at Your review (P9.9.7). An
  earlier round's outcome stays as it was. (Kai, 2026-10-26: "if it goes through the pipeline again
  it gets new limits. it will then either come up with nothing or come up with something that works without conflict,
  waiting for human review again"; "if the user wants to keep sending the same conversation through our pipeline for
  another round, so be it")

## P3. The pipeline

- **P3.1 Stages.** The settings view's diagram is the pipeline, in order: Your Assist (intake) → Triage → Analyze →
  Propose ⇄ Review (the proposer's reviewer) → Simulate → Write up → Your review → Your household. Your review can send
  a conversation back to Propose for another round (a revision, or an accept that met a conflict, P2.6). Review and
  Simulate are stamps the proposer collects (SPEC P0.4). (Kai, 2026-10-26)
  - **P3.1.1** A conversation still going is recording. Once it ends it's new, and waits at Your Assist until a run
    admits it. (SPEC-CORE.md §1; Kai, 2026-10-24)
  - **P3.1.2** A stage can end a conversation, sending it to Your household: Triage (Looked smooth), Analyze (No
    trouble found, Couldn't be read), Propose (No fix found), Your review (Improved, Declined).
    (SPEC-CORE.md §1, 2026-10-26)
  - **P3.1.3** Stages are responsibilities: they needn't be separate services, or each a separate paid model call.
    (PRODUCT-SPEC.md, 2026-10-21)
- **P3.2 Contracts.** What each stage hands the next is well defined, typed, and tested where it's made; the
  interfaces between one stage's output and the next's input are tested, and work. That includes Propose → Review,
  Review → Propose, and Propose → Write up. (Kai, 2026-10-26)
  - **P3.2.1** Each stage checks what it hands on where it's made, with every field the next stage reads, and checks
    what it takes in where it comes in. (SPEC-CORE.md §3; Kai, 2026-10-26: "stage contracts: yes, let's build them")
  - **P3.2.2** Every field that steers what happens next has a fixed set of allowed values, old values included.
    (Kai, 2026-10-26)
  - **P3.2.3** A record that breaks its contract is set aside on its own, with a clear reason, and hands nothing on;
    the stage goes on with the rest. (Kai, 2026-10-26: "well-defined input & output shapes of everything that goes in
    and out of each stage")
  - **P3.2.4** Every kind of conversation can flow end to end through every stage and every edge back.
    (fixture-test/SPEC.md, from README)
- **P3.3 Your household.** Everything already decided and out of the pipeline: improved, no fix found, declined, no
  trouble found, looked smooth, marked as a test, couldn't be read, and archived. Archived conversations are kept
  ("deleted but stored") so other changes are checked against them; Delete for good removes one.
  (Kai, 2026-10-26)
  - **P3.3.1** An archived conversation is out of the pipeline: no run admits it (full, picked or fast lane), no
    stage works on it or counts it (in Settings' stages or on the Run button), and its trouble is never proposed for.
    (Kai, 2026-10-26: "archived conversations are not meant to be acted upon.")
  - **P3.3.2** An archived conversation is still used to check other changes against, and the proposer may still
    read it. (Kai, 2026-10-26; fixture-test/SPEC.md, 2026-10-25)
  - **P3.3.3** Unarchiving a conversation archived by hand, or sending it in, puts it back in the pipeline, for a new
    round (P2.6). (Kai, 2026-10-26)
  - **P3.3.4** No fix found ends the conversation's round: it leaves the pipeline gracefully, and no run reconsiders it
    unless it's sent in again, for a new round (P2.6). (Kai, 2026-10-25: "if there was no fix found, we should just let
    it exit our pipeline gracefully."; Kai, 2026-10-26)
  - **P3.3.5** A conversation marked as a test stays out of the pipeline, including one marked with the earlier
    review menu ("was a test"). (fixture-test/SPEC.md; PRODUCT-SPEC.md, 2026-10-25)
- **P3.4 Who does each stage.**
  - **P3.4.1** Your Assist: the recorder, which is code. (PRODUCT-SPEC.md, 2026-10-21)
  - **P3.4.2** Triage: a small model. With none chosen, conversations pass straight through. (SPEC-CORE.md;
    DESIGN.md; fixture-test/SPEC.md, 2026-10-25)
  - **P3.4.3** Analyze: an AI role, the analyzer. (PRODUCT-SPEC.md, 2026-10-21; DESIGN.md)
  - **P3.4.4** Propose: an AI role that also writes the complete changes; there's no separate implementer.
    (ROADMAP.md; MILESTONE-6.md, 2026-10-23)
  - **P3.4.5** Review: an AI role separate from the proposer. (PRODUCT-SPEC.md, 2026-10-21)
  - **P3.4.6** Simulate: code, replaying with the household's own assistant on Home Assistant's own engine, in a
    sandbox. (PRODUCT-SPEC.md, 2026-10-21; Kai, 2026-10-23)
  - **P3.4.7** Write up: a small model writes; code checks what it wrote. (Kai, 2026-10-24: "this seems like a role
    a cheap model should be able to do"; DESIGN.md)
  - **P3.4.8** Your review: the household decides. (PRODUCT-SPEC.md, 2026-10-21; DESIGN.md)
- **P3.5 AI roles.**
  - **P3.5.1** Models make only judgement calls: what the user meant, what caused the trouble, what change would
    help. Code, or Home Assistant's own engine, works out anything settled or deterministic. (DESIGN.md;
    Kai, 2026-10-23: "so AI agents can focus on where ambiguities exist")
  - **P3.5.2** No model re-judges what an earlier stage settled: the proposer works from the analyzer's goals and
    doesn't redo them. (Kai, 2026-10-23: "not in other parts that are already solved")
  - **P3.5.3** A role is improved by changing what it's asked for before nudging its prompt's sentences.
  - **P3.5.4** AI roles are agentic: they use tools over several turns, not one-shot prompts. (Kai, 2026-10-23: "i
    think the AI roles need to be made more agentic")
  - **P3.5.5** A role answers through tools that check its answer against the household and reject it, with
    reasons, until it's valid; a malformed or impossible answer never moves on. (DESIGN.md; Kai, 2026-10-23: "if an
    action/state is invalid, we should just tell the agent so they can fix it")
  - **P3.5.6** A role that runs out of turns has its best answer taken; only what passes the checks moves on.
    (Kai, 2026-10-23: "if they "turn"-out, we could just take their best answer")
  - **P3.5.7** Whatever makes an idea or a change never also judges it. (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md)
  - **P3.5.8** Every role gets a Home Assistant primer up front: compact, foundational HA engineering knowledge,
    including the best practices HA publishes. (Kai, 2026-10-23: "im thinking these should be given upfront")
  - **P3.5.9** Every role gets the context it needs to understand the household: devices, areas, aliases, what
    Assist can and can't do with each device, and the conversations. (DESIGN.md)
  - **P3.5.10** What a role gets is justified by its job: a summary of what it needs up front, then tools for
    breadth (prioritized, paged lists and filters) and for depth (one item in full). No rule says a role sees every
    conversation. (Kai, 2026-10-25: "it should always be principled in how those data supports those agents
    discharge their roles more effectively.")
  - **P3.5.11** Nothing that grows with history is sent whole or scanned in full: it gets indices and pages.
    (Kai, 2026-10-25)
  - **P3.5.12** Each AI role is scored against conversations Kai labelled (their goals, the extra turns and whether
    each was avoidable, the expected fix); the labelled set grows with ordinary use. (PRODUCT-SPEC.md, 2026-10-21)
  - **P3.5.13** Each score has a pass rule written before the run, and is repeated for stability. A report says that
    a small labelled set catches clear failures but doesn't prove reliability. (PRODUCT-SPEC.md, 2026-10-21)
  - **P3.5.14** Recorded role outputs let the stages be tested offline, with no paid call. (PRODUCT-SPEC.md,
    2026-10-21)
  - **P3.5.15** A fresh-start check reruns the pipeline from the household's setup before Kai's hand-made fixes, to
    see that it finds them again. (PRODUCT-SPEC.md, 2026-10-21)
- **P3.6 Blocking a stage.** The household can block a stage: a run stops there, and what reaches it waits there until
  the stage is unblocked (P4.3.2); packages the run admitted that are already past it still move. (DESIGN.md; Kai,
  2026-10-24: "gates, not live flow")
  - **P3.6.1** Triage, Analyze, Propose and Write up can be blocked; no other stage can.
    (fixture-test/SPEC.md)
  - **P3.6.2** Unblocking a stage lets it work again: what waited there moves with the next manual full run or a run of
    that stage (P5.2, P5.6); other runs move only what they admit (P5.1). (fixture-test/SPEC.md; Kai, 2026-10-26)
  - **P3.6.3** Running a blocked stage on its own still runs it, and still checks its model. (fixture-test/SPEC.md)
- **P3.7 Failures.** The pipeline as a whole, and each stage, handle failures gracefully. (Kai, 2026-10-26: "The
  pipeline overall needs to handle failures gracefully, and so does individual stages.")
  - **P3.7.1** Nothing waits forever, and nothing costs a paid call every run without end (a revision that can do
    nothing, an analysis that keeps failing). (SPEC-CORE.md; Kai, 2026-10-25 and 2026-10-26)
- **P3.8 One core.** One shared core decides the product's rules: the stages, what each run admits and works on,
  conversation statuses, and what each screen counts. Every screen and stage takes them from it instead of deciding
  them again, and code the core makes redundant is removed. (Kai, 2026-10-26: "Everything else derives from the core
  instead of re-deciding these rules locally.")
- **P3.9 Around the pipeline.**
  - **P3.9.1** A developer view, for administrators in developer mode, finds any ID's lifecycle and logs, shows every
    conversation and what happened to it, shows runs as a timing chart, and shows health (disk, memory, what's
    running, hot spots, AI calls). (Kai, 2026-10-25)
  - **P3.9.2** Developer services never call a paid model without explicit permission; paid evaluation is opt-in and
    off by default. (fixture-test/SPEC.md; DESIGN.md)
  - **P3.9.3** Services the household doesn't need are gone, including any way to apply a change outside the
    undoable changeset; the household keeps status and export. (Kai, 2026-10-25: "let's clean up those you deem
    unnecessary.")
  - **P3.9.4** It follows Home Assistant's conventions: error messages are translated, and a request after unload
    gets a clean error. (Kai, 2026-10-25)

## P4. How packages move

- **P4.1 Conversations travel.** The package is the conversation, at one stage at a time, with all its goals: slicing
  it into goals loses context, and the design must stay resilient to a goal tree the analyzer got wrong.
  (Kai, 2026-10-26: "conversation travels ... we design for resiliency")
- **P4.2 Flow.** A package admitted to the pipeline flows through the stages, in and out, until its round ends (P2.6):
  it waits at Your review, reaches Your household, or is held. (Kai, 2026-10-26)
  - **P4.2.1** New conversations wait at Your Assist until a run admits them. A run admits them into Triage; each stage
    that isn't blocked works on what that run admitted or released, and passes its output on; nothing else moves (P5.1).
    (Kai, 2026-10-24: "Each 'run' basically admits requests into the triage, and the rest of the stages, if not
    paused/gated/blocked, will consume and pass on their outputs"; DESIGN.md; Kai, 2026-10-26)
- **P4.3 Held.** A package that stopped at a stage is held there, with why (it failed run after run, a run's scope left
  it, its run was cancelled or interrupted, or the stage is blocked). A held package is released only by a manual full
  run, a manual run of its stage, or a run that picks it; one held for a problem that may since be fixed (a new
  version) can go back on by itself. (Kai, 2026-10-26)
  - **P4.3.1** A held package keeps its place and the work done on it at its stage. (SPEC-CORE R1: Kai 2026-10-24
    21:42; fixture-test SPEC: "A failing stage keeps its work at that stage")
  - **P4.3.2 Blocked stages.** A blocked stage (a gate) works on nothing: what reaches it is held there (P4.3), through
    every run, a manual full run included, until it's unblocked (P3.6.2). Later stages still work on what they already
    have. (Kai, 2026-10-24: "gates is the play/pause behavior I'm looking for"; DESIGN.md; SPEC P0.8)
    - **P4.3.2.1** Only the gated stages can be blocked. Each stage in the settings view shows its backlog and its
      Block control. (Kai, 2026-10-24; fixture-test SPEC)
    - **P4.3.2.2** A package held at a blocked Propose keeps its place while Write up works on packages that
      reached Write up before. (SPEC-CORE R1: Kai 2026-10-24)
    - **P4.3.2.3** With Triage or Analyze blocked, a conversation that reaches it waits there, in that stage's count.
      (fixture-test SPEC, 2026-10-25)
- **P4.4 Bounded loops.** No loop amplifies or goes on forever. Every edge that sends a package back
  costs a capped budget or needs a person, and a test fails on one that doesn't:

  | Back edge | Bound |
  |---|---|
  | Simulate → Propose (no fix yet) | the round's counterfactuals (P6.3), then No fix found |
  | Review ⇄ Propose | 15 reviews per round and 5 per idea (P4.4.3) |
  | Your review → Propose (a revision, a conflict), or the stage that asked (an answer) | a person, run right away (P5.12, P9.9.7) |
  | Your household → Your Assist (sent in again) | a person: a new round (P2.6) |
  | A stage failing → the same stage | 3 attempts in the run, then held; one more per manual release (P4.4.2) |

  (Kai, 2026-10-26: "my main concern is if there may be unforeseen feedback loops that amplify and go on forever")
  - **P4.4.1** Nothing gets stuck: no package waits forever, and nothing costs a paid call every run without end.
    (SPEC-CORE C2: Kai 2026-10-25, 2026-10-26)
  - **P4.4.2** A stage tries an operation on a package up to three times in the run that meets the fault; then the
    package is held, with why (P13.4.1). Each release (a manual full run, a run of its stage, or a run that picks it)
    gives it one more attempt, and a failure holds it again. (PRODUCT-SPEC; SPEC-CORE F2; Kai, 2026-10-26: "yes")
  - **P4.4.3** The proposer asks its reviewer at most 15 times per round and 5 times per idea: set medium-high, and
    tuned from observed runs. (DESIGN.md; MILESTONE-5; Kai, 2026-10-23: "leave it intentionally medium-high for now,
    and tune it based on our observations")
  - **P4.4.4** Repeated runs make no duplicate proposals. (ROADMAP, Milestone 10)
- **P4.5 Sent in again.** A conversation in Your household can be sent in again, with or without a note, for a new round
  (P2.6): it goes back to Your Assist as sent in, for one fresh full analysis that reads the note if there is one.
  (PRODUCT-SPEC, 2026-10-25; SPEC-CORE C3: Kai 2026-10-25; Kai, 2026-10-26: "if the user wants to keep sending the
  same conversation through our pipeline for another round, so be it")
  - **P4.5.1** The fresh analysis replaces the old one for every later stage. A note sent before is kept when the
    conversation is sent in again without one. (SPEC-CORE C3)
  - **P4.5.2** Its new round gets fresh limits (P6.3), and analysis failures from before its last good analysis don't
    count against it. (SPEC-CORE C3, F2: Kai 2026-10-25; Kai, 2026-10-26)

## P5. Runs

- **P5.1** A run admits packages; what it admits moves, and nothing else does. (Kai, 2026-10-26)
  - **P5.1.1** One run at a time. (SPEC-CORE R8; PRODUCT-SPEC)
  - **P5.1.2** No run admits a conversation archived by hand (full, picked or on demand), and no stage works on it or
    counts it. Sending it in, or unarchiving it, puts it back. (Kai, 2026-10-26: "archived conversations are not
    meant to be acted upon"; SPEC-CORE C5)
  - **P5.1.3** A conversation deleted during a run leaves that run at once: it isn't analysed, proposed for, tested or
    written up, and it's out of every stage's count. It's deleted when the run ends. (SPEC-CORE C7: Kai 2026-10-25
    20:18; Kai, 2026-10-25: "behave optimistically")
  - **P5.1.4** A conversation archived while a run works leaves that run too: the run doesn't analyse it or propose
    for it. (FINDINGS #19; SPEC-CORE C7)
- **P5.2 Manual full run.** Admits every new conversation and releases every held package. (Kai, 2026-10-26)
  - **P5.2.1** Settings' Run and the home page's Run with nothing picked are the same manual full run: the same
    count, the same run, the same confirmation. (Kai, 2026-10-25; TEXT-REVIEW, 2026-10-25)
  - **P5.2.2** It admits every finished conversation not yet admitted, and works on what waits at every open stage,
    revisions included. (SPEC-CORE R2: Kai 2026-10-25;
    fixture-test SPEC)
  - **P5.2.3** It works through the stages in the diagram's order, in one run: its new conversations go through every
    stage, and what it releases joins them at its stage (write-ups waiting are written up with the new ones).
    (Kai, 2026-10-24: "The existing order is fine")
- **P5.3 Scheduled run.** Admits new conversations only; packages held elsewhere stay held. (Kai, 2026-10-26: "yes")
  - **P5.3.1** The schedule is off by default. (Kai, 2026-10-24: "scheduled runs (leave it disabled for now)";
    DESIGN.md)
  - **P5.3.2** The schedule is set from the Run button's menu, and named there and in its clock's tooltip: 'Only when
    you run it' · 'Every day at 3:00, on new conversations' · 'Every Sunday at 3:00, on new conversations'. Closing the
    menu changes nothing. (TEXT-REVIEW, 2026-10-25; Kai's words, 2026-10-25; fixture-test SPEC)
- **P5.4 Picked run.** Admits the picked conversations (one from Your household is sent in first); only those move.
  (Kai, 2026-10-26; Kai, 0.40.0.dev3)
  - **P5.4.1** A picked run is the home page's Run with some conversations picked. The conversations view is a main
    way to admit conversations: select all those not sent in before and run, or send in one or a few at a time.
    (Kai, 2026-10-25)
  - **P5.4.2** It admits the picked conversations when it starts, and works only on those, at every stage from intake
    through Analyze, Propose and on. It never proposes for, settles, tests or writes up anything else, ideas a full
    run left untested included; those wait for a full run. (SPEC-CORE R3; Kai, 2026-10-26: "only the selected
    one gets the fast lane in, nothing else moves")
  - **P5.4.3** Its scope is enforced, not only hidden from the proposer: the proposer's answer can't settle or name
    what the run didn't admit, and Simulate doesn't test it. The proposer still reads the other conversations to check
    its ideas against them (P7.1). (FINDINGS #38; SPEC-CORE R3: Kai 2026-10-25)
  - **P5.4.4** Before sending anything in, it checks it can start (for example, no missing model). Refused, it sends
    nothing in. (SPEC-CORE R3: Kai 2026-10-26)
  - **P5.4.5** It leaves a conversation already sent in as it is: not filed again, keeping its own note and its way
    past the quick check. (SPEC-CORE R3)
  - **P5.4.6** Everything that shows a picked run agrees on what it moves: Settings' counts, the Run button, statuses,
    progress lines and History. Its progress counts only its own conversations. (Kai, 2026-10-25; Kai, 2026-10-26
    03:40: "i only just sent in one. it should only say one")
- **P5.5 Fast lane.** A conversation sent in (from Assist or the panel) is admitted on its own as soon as it ends;
  only it moves. (Kai, 2026-10-26)
  - **P5.5.3** A conversation sent in while another run works gets its own run right after that one, also when the
    household cancels it. (SPEC-CORE R4: Kai 2026-10-25)
  - **P5.5.4** It works only on the conversations sent in; what other runs admitted waits for a full run.
    (fixture-test SPEC, 2026-10-25; Kai, 2026-10-26)
  - **P5.5.5** A conversation sent in once and analysed since is no longer the fast lane's: later picked runs and fast
    lanes don't propose for, test or count it again. (SPEC-CORE R3: Kai 2026-10-26)
  - **P5.5.6** A conversation's end (Home Assistant's chat session expiring) is a signal for runs, not what defines
    its goals. (PRODUCT-SPEC)
- **P5.6 Stage run.** A manual run of one stage releases what's held there. (Kai, 2026-10-26)
  - **P5.6.1** A stage run is Run one step, from the Run button's menu, after a confirmation that counts what waits at
    that stage. It works at that stage only; what it passes on waits at the next. (SPEC-CORE R5: Kai 2026-10-24;
    DESIGN.md; fixture-test SPEC)
  - **P5.6.2** Run one step's Triage also admits the new conversations, and counts them. (TEXT-REVIEW, 2026-10-25)
  - **P5.6.3** A blocked stage can still be run on its own; its model is checked first. (fixture-test SPEC)
- **P5.8 Starting a run.**
  - **P5.8.1** Nothing paid runs without the household's say: a manual run starts only once the household confirms
    it, and scheduled runs happen only once the household sets a schedule. (DESIGN.md: "Paid analysis runs only when
    you allow it"; fixture-test SPEC)
  - **P5.8.2** The confirmation is a short modal, so the page doesn't move. It says what the run will do and gently
    reminds that runs cost money: 'Runs use your AI models, so they cost money.' Not now, close, a click outside or
    Esc start nothing. (Kai, 2026-10-24: "It should gently remind you that the runs cost money"; fixture-test SPEC)
  - **P5.8.3** Malformed input to a run is refused before anything acts. (fixture-test SPEC)
  - **P5.8.4** Its layout: a title saying how many conversations; then which ones (up to three by their first words,
    and how many more, adding up to the title); then 'Also:' with other work, only when there is some; warnings only
    when they matter; then the cost. No sentence under the title. A one-step run, and a run with no conversations,
    keep their one sentence. (TEXT-REVIEW, 2026-10-25: Kai approved the layout and 'Also:')
  - **P5.8.5** Which conversations it names: on the home page, the picked ones; in Settings, the new ones, sent in
    first, then the newest. Never a hidden one, and in the words the conversations view keeps. (fixture-test SPEC;
    TEXT-REVIEW, 2026-10-25)
  - **P5.8.6** It leaves blocked stages out and names them. (fixture-test SPEC; DESIGN.md)
  - **P5.8.7** Under the cost line, a rough token estimate from the household's past runs, as a short list; each
    figure names the model it's spent on. Only as far as that's easy: no budgeting or accounting. (Kai, 2026-10-25:
    "make sure we're able to match those token estimates with models. otherwise, the estimate becomes unnecessary
    complexity")
  - **P5.8.8** Every run control says how many conversations, and which, it will run. Every count a run control or its
    confirmation shows is what that run will admit and move, by the rule the run uses. (Kai, 2026-10-25: "how many
    conversations will be run, and which"; SPEC-CORE R6)
- **P5.9 A run in progress.**
  - **P5.9.1** A run takes one click, keeps going when the household leaves the page, and can be come back to.
    (DESIGN.md; TEXT-REVIEW, 2026-10-25)
  - **P5.9.2** It shows where it is, with live counts, marks the stage at work, and refreshes itself. (fixture-test
    SPEC)
  - **P5.9.3** It says which run it is: on new conversations (how many), on the conversations you picked, on what you
    sent in, or one stage only. (TEXT-REVIEW, 2026-10-25; Kai's words, 2026-10-25)
  - **P5.9.4** It can be cancelled, also from Settings. Cancel run's tooltip is the run's title plus its stage, for
    example 'Running on 14 new conversations · Analyze'. (TEXT-REVIEW, 2026-10-25)
  - **P5.9.5** Cancel stops the run and loses nothing: what it finished stays done, and what it hadn't is held, for a
    manual full run or its stage's run to try again (P4.3). With no run going, Cancel does nothing. (fixture-test SPEC;
    SPEC-CORE R8; Kai, 2026-10-26)
  - **P5.9.6** History names each run's kind: 'Run finished · on demand', 'Run finished · picked', '· scheduled'. A
    manual full run adds nothing. (TEXT-REVIEW, 2026-10-25; Kai, 2026-10-25: "trust u on this")
- **P5.10 Interrupted runs.** A run Home Assistant stops is marked interrupted (the household's Cancel marks it
  cancelled). It's never restarted on its own, nothing it finished is lost, and its record keeps a run record's shape,
  so History's details never show a broken error. (SPEC-CORE R8: Kai 2026-10-25)
- **P5.11 Run records.**
  - **P5.11.1** Every AI role's call is recorded privately in Home Assistant. A call keeps running if the page
    reloads, and its result is read from that record, so it's never paid for twice. (HANDOFF; MILESTONE-4;
    MILESTONE-6)
  - **P5.11.2** A run's record keeps usage where the provider reports it, marked partial and not a bill (otherwise
    estimated or unknown), and why each idea was stopped or pruned. (ROADMAP; HAND-SEEDED-CANDIDATE)
- **P5.12 Sent back.** A revision, or a conflict the household agreed to resolve, sends its conversation straight back
  in for a new round (P2.6): it's admitted at Propose on a run of its own right away, or right after the run in progress
  (as P5.5.3 does for a conversation sent in), and only it moves. If Propose is blocked, it waits there (P4.3.2). (Kai,
  2026-10-26: "a revision sends that conversation immediately back in for revision, it does not wait"; Kai, 2026-10-24:
  "proposals sent back for refinement gets processed immediately, unless blocked")

## P6. Simulate

- **P6.1** A test replays the conversation with the change, from its baseline (P6.2), and re-judges every goal. It's
  an improvement only when strictly better: better on at least one measure (goals met, turns per root goal, side
  effects left) and worse on none. (Kai, 2026-10-26; Kai, 2026-10-27: "improvement needs to be strictly better")
  - **P6.1.1 No order between measures.** More goals met doesn't make up for a root goal taking more turns or more
    side effects left (P6.1.10), nor fewer turns for more side effects. The same goals in the same turns with no fewer
    side effects is no improvement. (Kai, 2026-10-24: "maximize goals met, in addition to minimizing extra turns";
    Kai, 2026-10-27: "I'm inclined to go with A - no order")
  - **P6.1.2** Goals met with the change are compared with the baseline replay (P6.6): a goal newly met is one the
    baseline didn't meet. (MILESTONE-6.md, 2026-10-24)
  - **P6.1.3 No new friction.** A fix is no improvement if a goal the baseline met goes unmet, or a root goal takes
    more turns. (DESIGN.md and TEXT-REVIEW.md, 2026-10-25; SPEC-CORE.md, 2026-10-24)
  - **P6.1.5 Turns per root goal.** A root goal's turns are the turns said whose goals sit in its tree (P2.3.16). The
    turns a fix saves are its root goals' own, never the user's other requests in between, and the review never lists
    those as no longer needed. (P1.1; FINDINGS.md #70; SPEC-CORE.md)
  - **P6.1.6** Fewer turns count only on the right destination: guessing the wrong device, claiming success without
    evidence, or abandoning the request is no improvement. (PRODUCT-SPEC.md, 2026-10-21)
  - **P6.1.7** A clarification the fix needs, and its answer, count as turns. Combining two requests doesn't by itself
    prove fewer turns. (PRODUCT-SPEC.md, 2026-10-21)
  - **P6.1.8** A goal that asks for a brightness is judged on the brightness, not just on the light being on. A fix
    that leaves the wrong brightness is no improvement. (SPEC-CORE.md, 2026-10-24; FINDINGS.md #67)
  - **P6.1.9 Feedback.** Feedback on how Assist behaves (in the conversation, or in a note sent with it) is a turn with
    a goal: unmet in the conversation as it went, met in a replay once no Assist turn before its own (every turn, for a
    note's) shows what the household objected to, as a small model judges one Assist turn at a time, with what it said
    and did and what the home did then (by its call log, P9.10.5: what was spoken where and each other call; each device
    changed; and each script a recorded turn with no call log ran, the rest of what it did unrecorded), and the turn
    they objected to as the example. (DESIGN.md, 2026-10-25;
    TEXT-REVIEW.md, 2026-10-25; P2.2, P2.3.4; Kai, 2026-10-26: "if the assistant was flawless in meeting the goal,
    except for its verbosity, addressing the verbosity would be the obvious solution for turn minimization"; Kai,
    2026-10-27)
  - **P6.1.10 Side effects.** A side effect left is a change Assist made that no goal asked for, and that Assist didn't
    leave as it started (P1.3.3, P9.12). Code judges whether a replay left one. (DESIGN.md, 2026-10-25; TEXT-REVIEW.md,
    2026-10-25; Kai, 2026-10-26; Kai, 2026-11-02)
  - **P6.1.11 Models judge; code derives.** Code works out whether each device goal was reached (against its states),
    how many turns each root goal took, and which side effects were left. A model judges only what code can't, such as
    whether an Assist turn shows what the household objected to, or whether a reply gives what the user asked to know
    (P9.10.4). (DESIGN.md "Models judge; code derives"; ROADMAP.md)
  - **P6.1.12** The goals and the judge are fixed before the test and kept apart from the change being tested. (PRODUCT-SPEC.md,
    2026-10-21; ROADMAP.md)
  - **P6.1.13** A test of a change learned from the conversation is framed as what it would do next time, not as what
    Assist could have known then. (PRODUCT-SPEC.md, 2026-10-21)
- **P6.2** A baseline replays the whole conversation. A test branches from it at the first turn of the earliest root
  goal with trouble (P6.8) not set aside (P6.6.2), or an earlier turn the baseline shows an objection in (P6.1.9): the
  turns before are the baseline's, its replies, history and home after them, never said again. Anything still running
  then (a timer, say), and when a script or automation last ran, doesn't carry over. (Kai, 2026-11-05: "the fix must
  branch its counterfactual from that baseline, instead of replaying the conversation from scratch", superseding Kai,
  2026-10-26)
  - **P6.2.1 Accuracy first.** A replay is the conversation as it would happen in the household's setup today
    (P6.2.4). (Kai, 2026-10-26: "Simulation accuracy comes first."; Kai, 2026-11-05: "The concept is to replay just
    the conversation, as it would happen in the user's HA setup today.")
  - **P6.2.2 The household's own assistant.** A replay runs on the assistant that served the conversation, with its
    settings as they are now, on a copy, with nothing for the household to configure. Assist's ordinary settings stay
    untouched. If the assistant is gone, the conversation can't be replayed. (Kai, 2026-10-23: "the less we ask users
    to configure, the better"; Kai, 2026-11-05; ROADMAP.md; MILESTONE-6.md)
  - **P6.2.3 Any provider.** Any model provider can replay, through the household's existing HA integration.
    Anthropic and OpenRouter are tested; another provider replays with a "not yet tested with this provider" label
    instead of being refused. (Kai, 2026-10-22; ROADMAP.md; MILESTONE-6.md; README.md)
  - **P6.2.4 Today's setup, the recorded moment.** Each replay gets a sandbox of its own (P11), set up as the
    household's Home Assistant is now, read as the recorder reads a home: its version, its registries whole (areas,
    devices, entities with their names and aliases, disabled ones too), its location, units and language, what Assist
    can see, and its scripts and automations, which Home Assistant's own engines run. Only the moment is the
    recording's: what was said (P6.5), the devices' states at the start and others' changes between turns (P6.2.8),
    and the time (P6.2.5). A device that's gone is absent, one whose entity ID changed is the same device under its
    new one, and a new one starts as it is now. (Kai, 2026-11-05: "replays should use the whole setup as it is today";
    DESIGN.md)
  - **P6.2.5 One clock, the recorded time.** A replay's sandbox runs on one clock (P11.2). It starts at the
    conversation's first turn (a test's, at its branch turn) and only moves forward: before each turn, to that turn's
    recorded time, timers due on the way firing at their times (those still due once the gap has taken half a minute
    fire at the turn's time). So Assist's instructions and date and time tool, the scripts and automations, timers and
    the sun all read the conversation's time, not the replay's. Limits: IDs made in compiled code (Home Assistant's
    ULIDs) and the local time read with no argument read the real clock; a provider that signs its requests with the
    current time could refuse a replay of an old conversation, the machinery's fault, reported. (SPEC-CORE.md,
    2026-10-23; FINDINGS.md #69; MILESTONE-6.md; Kai, 2026-11-05)
  - **P6.2.7** A device that reports its movement (a cover, lock or valve) is emulated with the in-between state Home
    Assistant recorded, so the same command reproduces. (SPEC-CORE.md, 2026-10-23; FINDINGS.md #68)
  - **P6.2.8** Between replayed turns, only other people's recorded changes carry over, each through what holds it: a
    device's stand-in, or Home Assistant itself as a scene sets a state (a helper's value, an automation on or off);
    what a scene can't set (the sun, a script running) goes on as the sandbox runs it. The original assistant's own
    changes are left out. (MILESTONE-6.md)
  - **P6.2.9 Delays settle at once.** A script's delay runs its course, as it would at home, but in no time: while a
    turn's tool calls run and its result settles, the clock moves straight on to each next timer, never past the next
    recorded turn's time (after the last turn, the conversation's end). Otherwise it runs at real speed. (Kai,
    2026-10-23: "it's reproducing the actual behavior as a simulation should"; Kai, 2026-11-05: "time travel")
  - **P6.2.10 No hindsight.** Later corrections, and outcomes reported afterwards, never enter the replayed
    assistant's starting context. (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-2.md)
  - **P6.2.11** A replay keeps its own replies and the provider's own history between the recorded user turns,
    however long the recorded gap, as the conversation went on at home; a test's turns before its branch are its
    baseline's, as Home Assistant's chat log holds them (P6.2). It never pastes in the original replies. (INSTALL.md;
    README.md; MILESTONE-6.md; Kai, 2026-11-05)
  - **P6.2.12 Hosted features.** Provider-hosted features (web search, code execution) stay as the assistant has them
    and are never turned off for a replay. They run at the provider, their results outside receipts (P11.7); the
    improver doesn't claim a remote call was prevented. (MILESTONE-2.md; INSTALL.md; README.md)
  - **P6.2.13** A command or call the sandbox doesn't simulate never falls back to a real device or service: it's
    reported "not simulated", a limit of the test, never a made-up success. (PRODUCT-SPEC.md, 2026-10-21; INSTALL.md)
  - **P6.2.14** A recording without the request's identity (its user and context) can't be replayed. (INSTALL.md;
    MILESTONE-6.md)
  - **P6.2.17** A condensed recording is never replayed or used to seed a sandbox. (fixture-test/SPEC.md, 2026-10-25)
  - **P6.2.18** A replay is never recorded as a household conversation, and never sets the improver off.
    (HANDOFF.md; FEASIBILITY.md)
- **P6.3** Each round of a conversation gets at most the household's counterfactual setting's count: configurable, 3 by
  default. (Kai, 2026-10-26: "ideally it'll be configurable, but default is 3"; "keeps the 3 counterfactuals per
  conversation limit honest")
  - **P6.3.1** The setting is a coarse knob from 1 to 20 in the settings view. (Kai, 2026-10-23: "meant to be
    user-configurable, a coarse grained knob"; fixture-test/SPEC.md)
  - **P6.3.2** The limit holds for the whole round, across runs: waiting for another run never brings a fresh allowance;
    only a new round does (P2.6). (Kai, 2026-10-25: "it's deceiving when we tell users that the upper limit on each
    conversation is 3 counter-factuals, but we let conversations free-ride"; Kai, 2026-10-26: "if it goes through the
    pipeline again it gets new limits")
  - **P6.3.3** A counterfactual is one replay of the conversation with one change. (PRODUCT-SPEC.md, 2026-10-21)
  - **P6.3.4** A change named twice for one conversation on one setup (P6.7.1) is replayed once, and counted once in
    each round that names it (P0.6). (SPEC-CORE.md, 2026-10-24 and 2026-10-25; FINDINGS.md #72)
  - **P6.3.5** A replay the machinery cut off says nothing about the goals and doesn't count: a provider or passing
    fault, a crashed test, a gap in the replay engine, a check model that couldn't answer. It's never reused as a
    result, and is tried again as P4.3 and P4.4 allow. (SPEC-CORE.md, 2026-10-23 and 2026-10-25; FINDINGS.md)
    - **P6.3.5.1** A verdict given after a replay (the baseline's, a note's or revision note's) that couldn't be
      given is asked again on its own, and counts once; one that decides whether a turn is said is part of its
      replay (P6.3.5). (FINDINGS.md C7; SPEC-CORE.md)
  - **P6.3.6** When a round's counterfactuals run out with no fix, the conversation ends No fix found, in Your household
    (P3.3.4). (SPEC-CORE.md, 2026-10-25; TEXT-REVIEW.md, 2026-10-25)
  - **P6.3.7** A conversation sent in again, or sent back by Your review, starts a new round with a fresh allowance
    (P2.6). (Kai, 2026-10-26)
    - **P6.3.7.1** Its tests are judged by its fresh analysis: its new goal tree, with the new note's goal in it, not
      the old one's goals, feedback or side effects. (SPEC-CORE.md, 2026-10-25; FINDINGS.md #63)
  - **P6.3.8** The baseline replay and code's no-model checks (P6.9) don't count against the limit. (PRODUCT-SPEC.md,
    2026-10-21: "baseline reproduction is not counted in it")
- **P6.4** A replay with the change, from its branch (P6.2), says a root goal's turn, and a turn with no goal, always,
  and any other recorded turn only while a goal on it is still open at that point (P2.3.2): a part or repair not yet
  reached, a step whose parent isn't met (a question's: no reply of its run so far gave what it needs, the judge asked
  just before the turn, P9.10.4), a conduct goal a reply so far still shows (the judge is asked just before the turn),
  a wrap-up while something of its root is open (the note's and a revision note's conduct goals judged on the replies
  so far), a thanks always (P2.3.5). A wrap-up or conduct goal the baseline doesn't show open is set aside (P6.6.2):
  it can't be told either way, and its turn is said; a step the baseline shows met didn't go as the conversation went
  (P6.6.1). The baseline sends every turn. (Kai, 2026-10-26: "agreed"; 2026-10-29; Kai, 2026-11-05)
- **P6.5 The user's own words.** No simulated user: a replay sends only the user's recorded turns, unchanged and in
  their original order, and never invents one. (PRODUCT-SPEC.md, 2026-10-21; Kai, 2026-10-23: "a dummy user that
  always just responds exactly in the turn order of the user"; ROADMAP.md)
  - **P6.5.1** A replay keeps only the recorded turns it still needs, as an ordered subset that keeps their meaning.
    (PRODUCT-SPEC.md, 2026-10-21)
  - **P6.5.2** A clarification gets its recorded answer only if the meaning matches. A path that needs something new
    from the user is cut off, never answered: a fix that needs a new answer from the user is rejected.
    (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-2.md; ROADMAP.md)
  - **P6.5.3** A fix that still needs a follow-up counts that turn. (MILESTONE-6.md; fixture-test/SPEC.md)
- **P6.6 The baseline.** Before any change is tested, the conversation is replayed without it. (DESIGN.md,
  2026-10-25; MILESTONE-6.md)
  - **P6.6.1** It reproduces when, on every turn, the devices the recorded Assist changed get the effects recorded
    (their states and settings, and a command that failed at home failing again), however the model words its tool
    calls, and every goal code decides ends as in the conversation, in the same turns, with side effects left on the
    same devices. (Kai, 2026-10-24: "The former sounds good"; MILESTONE-6.md; ROADMAP.md)
  - **P6.6.2** An objection (or a filing) the baseline doesn't show still open is set aside for that test: the
    friction didn't happen again, so it can't be told either way, and its turn is said (P6.4). So is a question the
    baseline answered otherwise at any reply of its run, with the steps and filing under it (a reply varies from run
    to run; one the conversation couldn't tell too). When what's set aside leaves nothing the household had trouble
    with happening again, the baseline doesn't reproduce (P6.6.5). (DESIGN.md, 2026-10-25)
  - **P6.6.3** The baseline needn't meet the goals; the fix must. (MILESTONE-6.md)
  - **P6.6.4** Other differences from the model's run-to-run variation are kept as notes and don't stop the test.
    (Kai, 2026-10-24; DESIGN.md)
  - **P6.6.5** A baseline try that doesn't reproduce, or stops as unsupported once it's saying its turns, is followed
    by another; the first that reproduces is the baseline. When none does, the conversation is not reproducible: its
    testing stops, every try is kept, and the developer view says why and how many tries ran. A try that can't start
    stops the conversation's testing at once. (ROADMAP.md; TEXT-REVIEW.md, 2026-10-25; Kai, 2026-11-05:
    "retry 3 times, and if the problem is reproducible at least once, we use that as the baseline for the fix",
    superseding PRODUCT-SPEC.md, 2026-10-21)
  - **P6.6.6** A conversation gets up to three baseline tries per setup (P6.7.1), a fixed number, on top of its
    counterfactuals; a try the machinery cut off isn't one (P6.3.5). (PRODUCT-SPEC.md; Kai, 2026-11-05)
- **P6.7 No resampling.** A test is never repeated in the hope of a better result; only a baseline gets its tries
  (P6.6.5). A different plan (a change in how replays are judged, or the baseline it branches from) is a different
  test. (DESIGN.md; INSTALL.md; MILESTONE-6.md; Kai, 2026-11-05)
  - **P6.7.1** Each test plan carries the replay engine's version and a digest of the setup it runs on (P6.2.4),
    leaving out the devices' states, when it was read and the names of what Assist can't see. A baseline settled under
    an older plan or on another setup runs its tries again under the new one, keeping the old ones. The same plan is
    never resampled, and a change already tried isn't run again under the same plan. (MILESTONE-6.md; Kai, 2026-11-05)
- **P6.8 What's tested.** Simulate replays a conversation only for a goal with trouble a fix can show it improves: one
  unmet, met with extra turns, with the household's feedback (P6.1.9), or with a side effect left (P6.1.10). A goal with
  none of these has nothing to fix, but is still re-judged in its conversation's replays. (fixture-test/SPEC.md;
  README.md; P6.1)
  - **P6.8.1** No replay runs until the conversation's goals are established. (ROADMAP.md)
  - **P6.8.2** Simulate tests only ideas that were reviewed and passed code's checks. (SPEC-CORE.md, 2026-10-26)
  - **P6.8.3** Simulate tests only conversations its run admitted (P5.1). It never tests an idea for a conversation
    archived or deleted, or analysed again since the idea was made. (FINDINGS.md; SPEC-CORE.md, 2026-10-26)
  - **P6.8.4** Feedback with no goal before it in its conversation has nothing to replay, and gets no test.
    (TEXT-REVIEW.md, 2026-10-25)
  - **P6.8.5** A change to one assistant's instructions is replayed only on that assistant's conversations.
    (DESIGN.md, 2026-10-25; TEXT-REVIEW.md, 2026-10-25)
  - **P6.8.7** An improvement shows only what its conversation's test showed; a conversation whose test showed no
    improvement has none (P2.4). (SPEC-CORE.md, 2026-10-25; FINDINGS.md #77; Kai, 2026-10-26)
- **P6.9 Code's checks first.** Before any model replay, code runs each new, edited or newly exposed script alone in
  a sandbox of today's setup (P6.2.4) from the state its goal started in (together only if none reaches the goal
  alone), matches new names the way Assist does, and replays every recorded conversation's tool calls with the change.
  Calls that now resolve differently are signals to weigh, not proof of a regression. (Kai, 2026-10-23: "Sounds great,
  please go ahead"; DESIGN.md; MILESTONE-6.md)
  - **P6.9.1** Archived conversations take part in these checks. (P3.3)
- **P6.10 Simulate → Write up.** Simulate hands Write up, per conversation, its test (each goal's state before and with
  the change, and each root goal's turns) and the improvements those tests build (id, title, status, changes, test).
  (SPEC-CORE.md, 2026-10-26, rewritten per conversation; P3.2)
  - **P6.10.1** A conversation whose replays the machinery keeps cutting off is held at Propose (P4.4.2).
    (SPEC-CORE.md, 2026-10-26)
- **P6.11 Says more.** A test whose replay made Assist's replies longer is flagged. Code counts it when replies are at
  least 1.5 times as long and at least 10 words longer, per reply on average. On the review: an amber "Says more" chip
  on that test's row, tooltip "Assist's replies: about {after} words each, {before} before.", and under the tests
  "With this change, Assist's replies got longer in {n} of these." A review with no such test has no line about it.
  (Kai, 2026-10-26: "let tests flag replies that got longer"; TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md)
- **P6.12 Replay a recording.** In developer mode only, the "Replay a recording" service replays a recorded
  conversation's first turns with the assistant it used, in a sandbox of today's setup, and reports whether that
  assistant changed the same devices the same way. It calls that assistant's model and stores nothing.
  (TEXT-REVIEW.md, 2026-10-25)
- **P6.13 The automation week.** An automation change also runs for a week: in a sandbox of its own, today's setup
  as the home was when the conversation ended, the change on top, the clock moving straight on through 7 days
  (P6.2.9, with no turn to stop at) within a budget of real seconds; on running out, it says how many whole days ran.
  It keeps each automation run: when, and what it changed. Devices don't act on their own: only time, the sun and the
  automations set things off. The change's automations are saved as the week starts, through Home Assistant's
  editor's own reload, so what their save sets off is in it. For an edit or turning one on or off, the week runs
  without the change too. (Kai, 2026-11-05: "time travel")

## P7. Propose and Review

- **P7.1** The proposer and its reviewer take a holistic view of the household and its assistant, as set up today
  (P6.2.4): a problem found in one scenario can have a broad fix, and for a goal met but for its verbosity, the fix is
  the verbosity. (Kai, 2026-10-26)

- **P7.2** The proposer and its reviewer are separate AI roles, and the judge of tests is separate from both: whatever
  produces an idea or a change never also judges it. (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md)
  - **P7.2.1** The proposer is also the implementer. It writes complete changes in Home Assistant's own format (P7.5),
    and code checks them when it submits. There's no separate implementer role.
    (Kai, 2026-10-23: "the generator may be able to double up as the implementer, provided you think there's enough
    room in their context window")
  - **P7.2.2** The proposer's judgement is scoped tightly to where it's needed. It builds on the analyzer's goal tree
    and doesn't redo it: code works out which trouble an idea removes from the tree's links, and the proposer doesn't
    judge that again. (Kai, 2026-10-23: "The generator's job/intelligence should be scoped as tightly as possible to
    where we need it the most, not in other parts that are already solved")
  - **P7.2.3** Whether trouble was avoidable, and how to fix it, is the proposer's call, made across conversations.
    The analyzer doesn't propose fixes. (Kai, 2026-10-23: "is requesting for the fix/improvement a little premature?";
    LABELS.md)
  - **P7.2.4** The proposer comes up with ideas, goes back and forth with the reviewer, writes the changes and submits
    them. Testing them is Simulate's (P3.1). (Kai, 2026-10-23)

- **P7.3** The proposer's task is to propose personalizations to the household's Home Assistant setup that make its
  long-horizon Assist experience as seamless as possible. (Kai, 2026-10-23: "your task is to propose
  personalizations to the user's HA setup to make their long-horizon assistant experience as seamless as possible")
  - **P7.3.1** It groups trouble across conversations into themes (one cause behind trouble in several
    conversations) before proposing anything, and looks for simpler fixes that solve several problems at once.
    (Kai, 2026-10-22: "how does it find recurring themes across different sessions to propose simpler solutions that
    potentially solve multiple problems across different sessions?"; PRODUCT-SPEC.md; MILESTONE-5.md)
  - **P7.3.2** One coherent idea is one improvement. It may combine a script, a preference and an assistant change.
    (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-5.md)
  - **P7.3.3** Ideas are ranked by the interventions they're expected to save in future and by how well they
    generalise. A rare incident can reveal a broadly useful preference; a frequent one doesn't justify the wrong fix.
    A once-only request that reveals a reusable preference is assessed for how it generalises.
    (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md)
  - **P7.3.4** Proposals are sustainable and generalise. "A script for every occasion" fixes everything once but may
    not last. (Kai, 2026-10-23: "a script for everything solves it too LOL. the question is whether or not that leads
    to a sustainable setup in the long run")
  - **P7.3.5** Prompt engineering is in scope: making Assist model the user's thought process better, reason more
    rigorously and use logical deduction. (Kai, 2026-10-23: "maybe we need to be explicit that prompt engineering is
    part of their scope?")
  - **P7.3.6** Improvements get Assist to deduce the target from the household's state: "the TV" is the only one
    that's on, and a fan-mode request goes to the fan that's already on, unless the user is getting a fan ready for
    later. (Kai, 2026-10-23)
  - **P7.3.7** Cleanup, consolidation and preference tuning are proposed only when they cut interventions (for
    example removing conflicting presets, or prompt bloat). Tidiness alone is never a goal. (PRODUCT-SPEC.md,
    2026-10-21)
  - **P7.3.8** Nothing is proposed for a conversation without trouble. No device or preference is invented.
    (ROADMAP.md; MILESTONE-5.md)
  - **P7.3.9** The proposer fixes the real cause. A speech-recognition mishearing is in scope, but only when the
    conversations show it happened. (Kai, 2026-10-23: "They can add mishearing fixes too. Why would that not be part
    of their jobs?"; DESIGN.md)
  - **P7.3.10** What was asked separately stays separate: requests made at different times (garage open, garage
    close) are never merged into one goal or one fix. (MILESTONE-4.md, MILESTONE-5.md, 2026-10-23)
  - **P7.3.11** When the household's feedback is about something an accepted improvement makes Assist do, the
    proposer can revise that improvement. (Kai, 2026-10-26: "let's make sure it plumbs/flows its way correctly down
    the pipeline all the way through"; PRODUCT-SPEC.md, 2026-10-25)

- **P7.4** The proposer prefers the least specific change that works, in this order: (1) prompt engineering that
  makes Assist reason better (inferring meaning from states, time and activity; knowing when to act and when to ask;
  checking and stating results); (2) a preference line; (3) a registry change (names, aliases, area, exposure); (4) a
  script, scene or automation, only as a last resort, for what the tools Assist had (P7.6.6) can't do. (MILESTONE-5.md,
  Kai's changes after round 9, 2026-10-23; DESIGN.md)
  - **P7.4.1** Exposing an existing script beats writing a script per incident. (MILESTONE-5.md, 2026-10-23)
  - **P7.4.2** The proposer prefers the simplest rule consistent with every conversation it would apply to. A rule is
    checked against all of them, not only the one that prompted it. A special case, or a rule built on one
    conversation, needs a reason. (Kai, 2026-10-23: "agreed with ur design improvements"; DESIGN.md)
  - **P7.4.3** Each idea lists the conversations that support it and any that contradict it, and is consistent with
    that evidence and with the cause it states. The reviewer checks the list. (Kai, 2026-10-23; PRODUCT-SPEC.md,
    2026-10-21)
  - **P7.4.4** Assist stays brief. A script gets a spoken message only where Assist would otherwise get the outcome
    wrong, in a few words, with no caveats the household didn't ask for. Fixes that add no talk come first.
    (Kai, 2026-10-26: "most people hate overly verbose/naggy assistants."; fixture-test/SPEC.md, 2026-10-25)
  - **P7.4.5** When a trouble's cause is Assist missing a fact about the request, the preferred fix is that fact's
    line from the recommended facts (P10.7), in that assistant's own instructions. (Kai, 2026-10-25)

- **P7.5** The proposer changes Home Assistant configuration and declarative pieces only, each in Home Assistant's
  own format and checked by its own validators, and memory once it has a lasting Home Assistant form: an item of its
  script, automation or scene editor, created or edited whole (an automation also turned on or off; a script's steps by
  its action validator, an automation by its schemas then its trigger, condition and action validators, a scene's
  entities against their domains' services and options); a registry entry (an entity's name, aliases, area, hidden_by,
  disabled_by cleared to enable it where its config entry reloads as it's enabled, and exposure to Assist; a device's
  area and name; an area's aliases); an
  assistant's instructions, and only ever fewer LLM APIs; household lines (preferences), for everyone or the person
  speaking. A better setting for an assistant's other options (its model, web search) is a recommendation the review
  names for the household to change, never applied or replayed. (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-6.md;
  ROADMAP.md; Kai, on automations and scenes: "could we just support these?")
  - **P7.5.1** An improvement can hide entities as well as expose them: hiding the bare garage relay switches so
    Assist can't flip one and claim success. (Kai, 2026-10-23, approving it: "wow! talk about polish!")
  - **P7.5.2** It can change configuration the household wrote, not only what the improver made.
    (PRODUCT-SPEC.md, 2026-10-21)
  - **P7.5.3** Out of scope (a stretch goal at most, never a hidden dependency): writing arbitrary Python, installing
    integrations, editing Home Assistant's core, and changing the improver's own code. (PRODUCT-SPEC.md, 2026-10-21)
  - **P7.5.4** Memory, instruction, registry and configuration changes all go through the same evidence, review and
    acceptance. Memory has no rules of its own. (PRODUCT-SPEC.md, 2026-10-21)
  - **P7.5.5** Code holds three safety rules, at submit and again at apply: (1) never a config entry's data; (2) no
    action, call target or service field, but a field whose selector takes a plain value (text, number, select,
    boolean, a time, a colour and the like), takes its value from a script field, a template or trigger data (a
    select script field's neither: Home Assistant doesn't hold a call to its options; a field chooses between fixed
    actions and targets only as branches on its value, with choose or if), and neither do variables passed into
    another script or automation (fixed ones are seen in the review) or an event's data; no step calls
    conversation.process, with any text or agent, as a model would choose the action as it runs (what it could do is
    proposed directly); a webhook trigger stays local only; all read as Home Assistant runs the item (its
    validators' output), not as written; an assistant change never adds an LLM API; (3) only items Home Assistant's
    editors keep (scripts.yaml, automations.yaml, scenes.yaml), an automation turned on or off too: any other is a
    conflict, and so is an id that isn't a plain token (letters, digits, _ and -), as it ends the address the panel
    saves the item at.

- **P7.6** What the proposer and reviewer get serves their job. They get what they need upfront, summarised, plus
  tools for breadth (prioritised, paged lists and filters) and depth (one item in full). There's no rule that they
  see every conversation, and no payload grows without bound. (Kai, 2026-10-24)
  - **P7.6.1** Upfront, the proposer gets the conversations with trouble, written as scripts with their goals and
    sub-goals. Other conversations are there on demand. (Kai, 2026-10-23; MILESTONE-5.md)
  - **P7.6.2** Conversations triage set aside, or not analysed yet, stay within reach: they show where a rule would
    misfire on requests that already work. Skipping their deep analysis is fine. (Kai, 2026-10-24: "Reviewer should
    still have access to all the requests, regardless of whether or not they were deeply analyzed.")
  - **P7.6.3** Archived conversations are read to check rules against, though their trouble is never proposed for.
    (Kai, 2026-10-26: "archived conversations are not meant to be acted upon."; fixture-test/SPEC.md, 2026-10-25)
  - **P7.6.4** Both roles get a short Home Assistant primer upfront, naming the version running: how Assist works
    (exposure, aliases, scripts; each assistant's tools come from its conversations, P7.6.6) and the best practices
    Home Assistant publishes, checked against Home Assistant's sources and quoted as Home Assistant's. (Kai,
    2026-10-23: "for the best practices i meant the ones HA themselves published and recommended on their website";
    DESIGN.md; fixture-test/SPEC.md; Kai, 2026-11-05)
  - **P7.6.5** They get what Assist can do with each device in trouble, found in the sandbox: each Assist tool is
    tried on each such device, and "Assist can" means a command reached the device. (MILESTONE-6.md, 2026-10-23)
  - **P7.6.6** They get the household knowledge, and for each conversation, which assistant it used, the exact
    instructions it ran with, and that assistant's instructions and other settings now. As the conversation recorded
    them, they read every setting of its assistant, the tools Assist had with their parameters, the provider's hosted
    tools it called and the voice assistant it ran through; the analyzer reads these too. A recording names its
    assistant unambiguously.
    (Kai, 2026-10-26: "the proposer and reviewer, especially, needs to be able to see the assistant's own
    instructions, because that could be where faults lie."; DESIGN.md; TEXT-REVIEW.md, 2026-10-25)
  - **P7.6.7** Existing improvements (up for review, or applied) that touch the same devices as new trouble are shown
    upfront, and either role can read any of them in full. (DESIGN.md)
  - **P7.6.8** A revision reaches the proposer in full: the note, and the improvement it revises (its idea, changes
    and goals). (SPEC-CORE.md S3, 2026-10-26)
  - **P7.6.9** The household's feedback on how Assist behaved reaches both roles as trouble, with where it came from
    (its turn, or the note) and, when it's about an accepted improvement, which one. (Kai, 2026-10-26: "let's make
    sure it plumbs/flows its way correctly down the pipeline all the way through")
  - **P7.6.10** The reviewer gets a summary of the household's conversations that code builds from the analyses:
    common requests and their contexts, what worked or failed, known preferences and exceptions, and failure patterns
    in the improver and the reviewer themselves. (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md)
  - **P7.6.11** That summary keeps no conversation-specific detail. It may point to the source conversations for
    debugging, but isn't a replay archive, and the regression check doesn't replay every past conversation.
    (PRODUCT-SPEC.md, 2026-10-21)
  - **P7.6.12** That summary can be inspected and corrected for debugging. It isn't a profile feature for everyday
    use, and the everyday assistant doesn't get it. (PRODUCT-SPEC.md, 2026-10-21)
  - **P7.6.13** Every role's lookups reach everything in its view: the recording, for what happened, or today's
    setup, for proposing. Searches and listings return every entity, disabled, hidden and diagnostic ones included,
    each labelled (its category, whether disabled, hidden or exposed), exposed entities and controls first, in pages
    of 25 with a total. A search reads every text field of the entity, its device (a child device takes its parent's
    maker and model) and its integration. A card carries the entity's state and registry entries whole, and the
    actions Home Assistant's own descriptions say target it. Those descriptions and an integration's name are all a
    lookup reads from Home Assistant as it is now; the rest is its view's. A conversation's readings come one line per
    entity (the count, first and last), and the rest by entity and time, a page at a time. Every role reads a script's,
    automation's or scene's definition; the proposer and reviewer find any action by its words. Nothing is dropped
    by domain or field, and a page says what it left out. (Kai, 2026-11-05: "I'd like us to fix all those problems,
    but ideally as general and as broadly and cleanly as possible"; Kai, 2026-11-05: "give them a way to sift through
    the readings")
  - **P7.6.14** The proposer and reviewer also read the live home through Home Assistant's own tools, as the
    improver's service user (P7.6.15): an entity's history, a template rendered, an integration's entries in the
    system log, and the config entries. Only tools Home Assistant ships, marks read-only and that are on this pinned
    list are kept; a test fails when Home Assistant adds or renames one. Left out: system health and integration
    search (the version is known; the improver never proposes installing an integration), the two log readers (size;
    the entries carry the errors), search and action search (the improver's lookups are wider), and the date and time
    (the recording has it). Never a tool that acts, and never the Assist API (under an AI Task it writes an exposure
    option into every entity). A template renders on the real instance, up to 3 seconds in a thread and then on Home
    Assistant's event loop, and each call counts against the role's tool-call limit (1,000 for Anthropic's AI Task).
    Their instructions say which tools read a recording's moment and which the live home. The analyzer and the judges
    answer from the recording, never the live home, but for what P7.6.13 takes from Home Assistant now. (Kai,
    2026-11-05)
  - **P7.6.15** The service user is an "Assist Improver" system user in the administrators group, local only and
    without credentials, so no one can sign in as it. Setup creates it when missing and keeps its id in the config
    entry; a leftover one of that name the entry doesn't name is removed, since Home Assistant's UI can't delete a
    system user. Uninstalling removes it first (P8.16.2). Creating a system user is undocumented for a custom
    integration: a watched touchpoint, with its Repairs notice. (Kai, 2026-11-05)

- **P7.7** The proposer submits through a check that refuses an invalid answer, with the reasons. Only what passed
  the check moves on. (MILESTONE-5.md, 2026-10-23; SPEC-CORE.md S3, 2026-10-26)
  - **P7.7.1** Code checks proposed changes, already in Home Assistant's format, with Home Assistant's own validators
    and the safety rules (P7.5.5) at submit, in check_hypothesis and in ask_reviewer. Unsafe or invalid changes are
    refused before any replay. (ROADMAP.md, Milestone 6)
  - **P7.7.2** It refuses: a device that doesn't exist; a state a device can't take; a conversation or goal that
    doesn't exist or has no such trouble (for example a turn that isn't an extra turn); exposing what's already
    exposed; a script calling a service that doesn't exist. (MILESTONE-5.md, 2026-10-23; DESIGN.md)
  - **P7.7.3** It refuses an answer while any goal with trouble is neither fixed by an idea nor listed as not fixed,
    with why (for example, outside the assistant's control). (MILESTONE-5.md, round 6, Kai, 2026-10-23)
  - **P7.7.4** An idea names the goals it fixes. Code links each to all the trouble under it in the goal tree: its
    extra turns and asks. (MILESTONE-5.md, round 2, 2026-10-23)
  - **P7.7.5** For each goal an idea names, each new, edited or newly exposed script runs alone in a sandbox from the
    state the goal started in. Scripts run together only if none reaches the goal alone, so order never decides. The
    goal's devices are compared with the goal, including the user's choices for settings left open. New aliases and
    names are matched with Home Assistant's own name matching. (MILESTONE-6.md; Kai, 2026-10-23: "Sounds great, please
    go ahead with building those improvements.")
  - **P7.7.6** Every conversation's recorded tool calls are replayed without and with the change, and calls that
    now resolve differently are shown. They're signals to weigh, not regressions: a more general script replacing an
    old flow is an expected difference. (MILESTONE-6.md; Kai, 2026-10-23)
  - **P7.7.7** A goal that an existing improvement already addresses is noted with that improvement and skipped, not
    made a new idea. Code checks the claim: it's refused when that improvement was applied before the conversation
    and the trouble still happened. A refused claim never settles a goal. (DESIGN.md; SPEC-CORE.md S3, 2026-10-26)
  - **P7.7.8** An impossible idea is never simulated. Not-fixed goals from a refused answer don't become final.
    (SPEC-CORE.md S3, 2026-10-26)
  - **P7.7.9** An idea dropped without being tested is named, with why. No idea is lost unseen. (SPEC-CORE.md S3;
    FINDINGS.md C9)

- **P7.8** Each idea is reviewed before any money is spent testing it, by the reviewer, with read-only access to the
  same conversations and devices. (DESIGN.md; PRODUCT-SPEC.md, 2026-10-21)
  - **P7.8.1** Review happens at the idea stage only. There's no second review of the implemented change unless
    testing shows that implementations, not ideas, cause the problems. (Kai, 2026-10-22: "it's more useful to have the
    review done at the ideation stage, because it'll likely be the ideas that cause problems, less the
    implementation")
  - **P7.8.2** Reviews include a sanity check for regressions against what the household usually does: for example,
    an alias that makes "the fan" ambiguous. (Kai, 2026-10-22: "its upfront reviews include a sanity check against
    regressions."; ROADMAP.md)
  - **P7.8.3** Related conversations that went well are regression evidence and must keep working: garage close
    still works after a garage-open fix. (HAND-SEEDED-CANDIDATE.md)
  - **P7.8.4** The reviewer cuts an idea that contradicts other conversations. "My lamp" meaning the desk lamp
    elsewhere rules out "my lamp means the floor lamp". (Kai, 2026-10-23: "wow, the reviewer working as it should!")
  - **P7.8.5** An idea that's plausible on the household's data is rejected through data (for example, a
    conversation where the same phrase means something else), never on opinion. (Kai, 2026-10-23: "i think the right
    way to "reject" this has to be through data")
  - **P7.8.6** The reviewer rejects merging requests made at different times into one (garage open and close), and a
    fix whose cause the conversations don't show (such as a claimed mishearing). (ROADMAP.md; MILESTONE-5.md;
    Kai, 2026-10-23)
  - **P7.8.7** The reviewer checks for added talk. A change that makes Assist say more (new speech, caveats, longer
    messages) needs household evidence that it's wanted, or it's flagged. (Kai, 2026-10-26: "add a reviewer check for
    added talk")
  - **P7.8.8** Code lists the speech a change adds or removes for the reviewer: stop messages with their words, and
    sentences telling Assist what to say, but not the household's own words. (fixture-test/SPEC.md, 2026-10-25)
  - **P7.8.9** Whether "a script for everything" stays sustainable is a point for the reviewer. (MILESTONE-5.md,
    2026-10-23)
  - **P7.8.10** Every draft the reviewer gets carries code's checks (P7.7.5, P7.7.6), and both roles can run those
    checks themselves. (MILESTONE-6.md, 2026-10-23)

- **P7.9** Reviews work like code reviews. The reviewer writes Conventional Comments (issue, suggestion, todo,
  nitpick, question, thought, praise), each marked blocking or non-blocking. (Kai, 2026-10-23: "i'd suggest using
  widely tested code review labels. there's probably a lot of fine tuning that already went into them")
  - **P7.9.1** Each comment is backed by household evidence that code checks: conversations and turns, devices, or a
    pattern across conversations (activity, usage, past goals, statistics). (Kai, 2026-10-23: "evidence required and
    checked: agreed. must point to some household evidence (eg activity, usage, historical goals, statistics).")
  - **P7.9.2** The verdict follows from the comments: any blocking comment requests changes; otherwise the idea is
    approved. (Kai, 2026-10-23: "ideally verdict derivable from findings themselves, i.e. self-evident, minimal
    chance of self-inconsistency")
  - **P7.9.3** The proposer answers every blocking comment: resolved, or won't fix with a reason. The reviewer sees
    its earlier review with those answers. (Kai, 2026-10-23: "resolution follow up: good idea"; MILESTONE-5.md)
  - **P7.9.4** When the reviewer requests changes or offers its own version, the answer reaches the proposer like a
    tool response that sets it a goal: these goals still need a fix; consider other ideas, including the reviewer's
    version; ask again. It doesn't rely on a rule in the proposer's prompt. (Kai, 2026-10-23: "the request changes
    should programmatically attach a prompt to prompt the generator to explore other ideas and solutions")
  - **P7.9.5** There's one reviewer per round. It keeps one conversation across all of a round's drafts, so it remembers
    what it approved and asked for, and it waits for the next draft rather than starting fresh. A stand-in reviewer
    behaves the same. (Kai, 2026-10-23: "it doesn't make sense for the reviewer to keep having to be reinitialized
    from new state"; DESIGN.md)
  - **P7.9.6** The back-and-forth is bounded: at most 15 reviews per round and 5 per idea. Both are set medium-high on
    purpose and tuned from observed runs. (Kai, 2026-10-23: "let's leave it intentionally medium-high for now, and
    tune it based on our observations"; DESIGN.md)
  - **P7.9.7** Only an idea the reviewer approved is tested. If the reviewer crashed, stopped partway, ran out of
    budget or gave an answer that failed the checks, the unreviewed idea is never simulated or put up for the
    household, and the proposer is never told to decide for itself. (SPEC-CORE.md S3, 2026-10-26; PRODUCT-SPEC.md,
    2026-10-21)
  - **P7.9.8** Review is a stamp the proposer collects (P3.1). A conversation whose ideas couldn't be reviewed stays at
    Propose: a failing review is tried again up to the failure limit, then the conversation is held there, with why
    (P4.3, P4.4). (Kai, 2026-10-26)

- **P7.10** What Propose and Review hand on is typed and tested (P3.2). (Kai, 2026-10-26)
  - **P7.10.1** Propose → Review: a draft of each idea with what code found about it: its title, goals and changes,
    the trouble it targets (by kind), and the conversations where it would apply. (SPEC-CORE.md §3, 2026-10-26)
  - **P7.10.2** Review → Propose: the review (its comments, and its own version if any). The proposer gets back the
    outcome the comments decide, each comment numbered (label, decorations, subject), and the next step while a
    blocking comment stands. (SPEC-CORE.md §3, 2026-10-26)
  - **P7.10.3** Propose → Simulate: the reviewed ideas (their changes, and the goals each fixes), the goals not fixed,
    the goals already addressed, and what the proposer has had (P7.11). (SPEC-CORE.md §3, 2026-10-26)
  - **P7.10.4** Propose → Write up: each improvement's idea and the proposer's title, which goes up as the title if
    the write-up fails. (SPEC-CORE.md §3, 2026-10-26)

- **P7.11** A round's proposals work from the conversation's analysis as it stands in that round; a new round's fresh
  analysis (sent in again) voids the earlier round's ideas. (SPEC-CORE.md C3, 2026-10-25; Kai, 2026-10-26)

- **P7.12** How goals leave Propose and come back to it.
  - **P7.12.1** A goal the proposer lists as not fixable, with why it's outside the assistant's control, ends No fix
    found. That's final. (SPEC-CORE.md C2, 2026-10-25; TEXT-REVIEW.md, 2026-10-25)
  - **P7.12.2** A conversation whose test found no fix, with counterfactuals left in its round (P6.3), goes back to
    Propose as No fix yet, and the next proposal in that round looks for another fix. When the round's counterfactuals
    run out with no fix, it ends No fix found. (Kai, 2026-10-26: approving "No fix yet · The next proposal looks for
    another fix"; P2.6)
  - **P7.12.3** Decline declines the improvement and archives its conversation (P12.4.12). (Kai, 2026-10-26: "keep it
    simple, stupid. decline just declines the improvement and archives the conversation.")
  - **P7.12.5** Asking for a revision sends its conversation back to Propose for a new round (P2.6), with the note as a
    turn (P2.2). The round's proposal addresses the note and, once its test shows it works, brings back a new version of
    the improvement, linked to that note. (DESIGN.md; fixture-test/SPEC.md; TEXT-REVIEW.md, 2026-10-25; Kai, 2026-10-26)

- **P7.13** Propose keeps to the run's scope (P5.1).
  - **P7.13.1** In a picked run or a fast lane, the proposer proposes only for the admitted conversations' trouble,
    but still reads the others to check its rules against them. (SPEC-CORE.md R3, 2026-10-25; Kai, 2026-10-26)
  - **P7.13.2** Its answer can't settle or name trouble the run didn't admit, or trouble in a conversation archived
    by hand, and Simulate doesn't test it. (FINDINGS.md; SPEC-CORE.md R3, C5)

## P8. Recording, intake and privacy

- **P8.1** The improver records each of the household's Assist conversations in their Home Assistant, with what
  happened in the home during it: what was said, what Assist did (its tool calls), and how the devices changed.
  (DESIGN.md, The pipeline; fixture-test/SPEC.md)
  - **P8.1.1** It records as the conversation happens, not when Home Assistant cleans the session up, including the
    final reply Home Assistant leaves out of its notifications. (PRODUCT-SPEC.md; fixture-test/SPEC.md)
  - **P8.1.2** Recording is done by code. Installing and recording make no model requests. (PRODUCT-SPEC.md,
    architecture table; INSTALL.md)
  - **P8.1.3** It listens alongside the household's assistants. It doesn't replace their AI provider or change the
    selected voice assistant. (INSTALL.md)
  - **P8.1.4** The improver's own AI calls are never recorded, and a replay never sets the improver off again.
    (HANDOFF.md; FEASIBILITY.md)
- **P8.2** Each turn in the conversation records:
  - **P8.2.1** the user's words, Assist's replies, and Assist's tool calls paired with their results. (INSTALL.md;
    MILESTONE-1.md)
  - **P8.2.2** what Assist was given: its prepared prompt as rendered, its tool definitions, and whether it read the
    household knowledge. (INSTALL.md; fixture-test/SPEC.md)
  - **P8.2.3** which assistant answered, unambiguously, with all its settings (its conversation subentry's data,
    whole), and the voice assistant it answered in. (TEXT-REVIEW.md 2026-10-25; MILESTONE-1.md; fixture-test/SPEC.md;
    Kai, 2026-11-05)
  - **P8.2.4** who asked and from where, when Home Assistant provides it: the user, the request's context and parent
    ids, and the requesting device with its name. (INSTALL.md; MILESTONE-1.md)
  - **P8.2.5** the moment, as Home Assistant has it, whole, taken before the first request and kept once per
    recording: every state, the entity, device, area, floor and label registries with every field, Home Assistant's
    configuration, exposure, the definitions of scripts, automations and scenes, to-do items and the household
    knowledge. A later turn keeps what moves: the states, and the time. A replay takes its moment from it (P6.2.4).
    (fixture-test/SPEC.md; Kai, 2026-11-05)
  - **P8.2.7** aliases, including Home Assistant's computed names, in their declared order. (INSTALL.md)
  - **P8.2.8** disabled entities, with their registry record and no state, as Home Assistant has none. (PRODUCT-SPEC.md;
    INSTALL.md; fixture-test/SPEC.md)
  - **P8.2.9** every state change while the conversation is open, timestamped, as observations: never assumed to be
    the effects of Assist's actions. Only the improver's own AI Tasks are cut (P8.1.4). The answering voice
    assistant's own entities (its agent, speech and wake word entities, and the satellite asked through) are labelled
    machinery, never what a request did. A change nobody caused that leaves the state as it was, or a counter's,
    proximity's, image's, or a sensor's with a unit, state class or numeric device class, is labelled a reading: kept,
    and listed after everything else, one line per entity. No state changes are recorded outside an open
    conversation. (INSTALL.md; fixture-test/SPEC.md; Kai, 2026-11-05: "give them a way to sift through the readings")
  - **P8.2.10** the action calls under the request: each call Home Assistant announces in the request's context chain
    (an automation it set off included), inside the call it was made in, with a tool's error on the call that failed
    (inside a script, the last call before the error). Not attributed to its call: a refusal inside a parallel or
    continue_on_error step, and a call Home Assistant refuses before announcing it (an unknown action, invalid data),
    whose error goes on the call before it. A call a device makes in the task handling a call (a light group's calls
    to its lights) is kept as the request's own, not inside that call. (Kai, 2026-11-05)
- **P8.3** Gaps are explicit.
  - **P8.3.1** A recording that started mid-conversation says so. (fixture-test/SPEC.md)
  - **P8.3.2** A reply that relied on the provider's own history, which Home Assistant doesn't record, is marked for
    replays to check. (fixture-test/SPEC.md)
  - **P8.3.3** A turn with no assistant API selected says so. (fixture-test/SPEC.md)
  - **P8.3.4** Missing observations are never invented. (PRODUCT-SPEC.md; fixture-test/SPEC.md)
  - **P8.3.5** The improver names what it can't record instead of claiming full coverage: conversations handled
    outside Home Assistant's common chat log, provider-only history, and attachments.
    (MILESTONE-1.md; ROADMAP.md; INSTALL.md)
- **P8.4** A recording's life.
  - **P8.4.1** A conversation ends when its recording closes: when Home Assistant cleans up its session, or, with no
    cleanup seen after 15 minutes without activity, as timed out. A timed-out recording is kept. (INSTALL.md)
  - **P8.4.2** Recordings survive a restart and a restore into a new Home Assistant. One still open at a restart is
    marked interrupted. (INSTALL.md; fixture-test/SPEC.md)
  - **P8.4.3** A new recording never overwrites an earlier one of the same conversation. (INSTALL.md;
    fixture-test/SPEC.md)
  - **P8.4.4** Writes are saved about a second after they happen, so a sudden power loss loses at most that.
    (INSTALL.md; fixture-test/SPEC.md)
  - **P8.4.5** A recording is never rewritten: an update keeps every recording as it was, and none is altered to make
    a check pass. Condensing (P8.13.2) is the only change made to a kept recording. (INSTALL.md; Kai, 2026-10-25 for
    condensing)

- **P8.6** Identity.
  - **P8.6.1** Who's speaking comes from Home Assistant's signed-in user. The improver builds no identity or privacy
    machinery of its own. (PRODUCT-SPEC.md 2026-10-21)
  - **P8.6.2** An unidentified speaker is never claimed to be a known person. (PRODUCT-SPEC.md 2026-10-21)

- **P8.7** Sending a conversation in.
  - **P8.7.1** Intake (Your Assist) is code. It hands each conversation a run admits to Triage, or straight to Analyze
    when it was sent in with a clear sign (P8.7.7). (PRODUCT-SPEC.md, architecture table; SPEC-CORE.md §3, 2026-10-26)
  - **P8.7.2** An opened conversation on the home page offers Send this one in. (TEXT-REVIEW.md 2026-10-25)
  - **P8.7.3** Sending a conversation in from the panel is the same as filing it from Assist, and doesn't replay it.
    (TEXT-REVIEW.md 2026-10-25)
  - **P8.7.4** Assist has a tool to file the conversation it's in, offered through Home Assistant's own tool
    gathering. (Kai, 2026-10-25; fixture-test/SPEC.md)
  - **P8.7.5** Asking Assist to file an improvement, in whatever words, files the conversation Assist is in. (Kai,
    2026-10-25: "something to the extent of 'file an improvement'"; Kai, 2026-10-25: not rigid)
  - **P8.7.7** Only a clear sign sends a conversation straight to Analyze: a note written when sending it in; filing it
    from Assist (with or without saying what to improve); or sending in again one already in Your household. A plain
    pick, or a send-in from the panel without a note, goes through Triage. (Kai, 2026-10-25; 2026-10-26
    03:05)
  - **P8.7.8** A conversation sent in records how: from the panel or from Assist, and whether it was sent in again.
    One sent in before that was recorded counts as sent with a clear sign. (fixture-test/SPEC.md, 2026-10-25)
  - **P8.7.9** Picking a conversation already sent in leaves it as sent: its note and its way past Triage stay.
    (SPEC-CORE.md R3; 2026-10-26)
  - **P8.7.10** Only a conversation the improver hasn't taken in can be sent in, so none is taken in twice. Otherwise
    sending is refused: "The improver already has that conversation." The exceptions are P8.8.1 and P8.14.6.
    (TEXT-REVIEW.md 2026-10-25; Kai, 2026-10-26)
  - **P8.7.11** Sending in a conversation that isn't kept is refused: "That conversation isn't kept." (for several:
    "{n} of those conversations aren't kept."). (TEXT-REVIEW.md 2026-10-25)
- **P8.8** Sending in again.
  - **P8.8.1** A conversation in Your household can be sent in again, with or without a note (P4.5). (Kai, 2026-10-25
    19:50: "Option A"; Kai, 2026-10-26: "yes. i'm surprised this is not already possible"; Kai, 2026-10-26: "so be
    it")
  - **P8.8.2** Sending in again is one mechanism, whatever the analysis found: the conversation is admitted as new, for
    a new round (P2.6), and gets one fresh full analysis that reads the note and replaces the old analysis for every
    later stage. (SPEC-CORE.md C3, 2026-10-25; fixture-test/SPEC.md)
  - **P8.8.3** Nothing settled comes back into the pipeline by itself. Only the household sends it in again.
    (TEXT-REVIEW.md 2026-10-25, No fix found)
- **P8.9** Notes.
  - **P8.9.1** A note sent with a conversation is a turn of it (P2.2, P2.3.4). It travels with the conversation, and
    no stage drops it. (Kai, 2026-10-26; PRODUCT-SPEC.md 2026-10-25; SPEC-CORE.md S2, 2026-10-26)
  - **P8.9.2** The note is optional. Its field reads "What should have happened? (optional)". (TEXT-REVIEW.md
    2026-10-25)
  - **P8.9.3** Opened again, the conversation shows the note after "Your note:". (TEXT-REVIEW.md 2026-10-25)
  - **P8.9.4** An earlier note is kept when the conversation is sent in again without one. (SPEC-CORE.md C3;
    FINDINGS.md #52, 2026-10-25)

- **P8.10** Where the data stays.
  - **P8.10.1** Everything the improver keeps stays in the household's Home Assistant, in its own storage, inside Home
    Assistant's configuration and backup. None of it is in the product's repository. (PRODUCT-SPEC.md 2026-10-21;
    ROADMAP.md 2026-10-24; fixture-test/SPEC.md)
  - **P8.10.2** Nothing comes to us: household data goes only to the AI models the household chose, through Home
    Assistant's AI Tasks and assistants. (Kai, 2026-10-25: "the data doesn't come to us"; DESIGN.md;
    fixture-test/SPEC.md)
  - **P8.10.3** The improver opens no connection of its own, installs no extra packages, and its panel loads only its
    own files. (fixture-test/SPEC.md)
  - **P8.10.4** Each AI role gets what it needs for its work: recording the home broadly doesn't mean sending the whole
    home to a model. (PRODUCT-SPEC.md 2026-10-21; DESIGN.md)
  - **P8.10.5** Records are private: stored with private file permissions, and only administrators can inspect them
    (P8.17). (INSTALL.md; MILESTONE-1.md)
  - **P8.10.6** Nothing specific to one household (its records, its details, how its devices are linked) ships in the
    product for others. The package and the public repository carry none. (Kai, 2026-10-24: "that discloses personal
    information about my household")
  - **P8.10.7** A Home Assistant backup carries the improver, its settings and everything it keeps, and restoring it
    brings them back. (PRODUCT-SPEC.md; INSTALL.md; ROADMAP.md 2026-10-24)
- **P8.11** Credentials.
  - **P8.11.1** Credentials are never read: no config entry's data or options but the answering provider's, handed
    unread to a replay's worker; nor Home Assistant's auth store or secrets.yaml, directly. What Home Assistant gives
    otherwise is recorded as it is, nothing redacted: it stays in the household's Home Assistant. (INSTALL.md;
    fixture-test/SPEC.md; Kai, 2026-11-05: "it still stays private within the user's HA")
  - **P8.11.4** Export recording, whose copy may be posted publicly (with a bug report), replaces with Home
    Assistant's redaction marker the value under every key that names a credential (one containing password, secret,
    api_key, apikey, access_key, authorization, cookie or private_key, a hyphen or space read as an underscore, or
    that is or ends in token) or a location (location, latitude, longitude), in every mapping, definitions and an
    assistant's settings included, and drops tokens in URLs (a camera's picture). Nothing an AI setting needs is cut
    (model, max_tokens, temperature stay); the rest goes as recorded, a secret Home Assistant resolved under another
    key included, and a location under another key or held as a state (an address). The stored recording stays
    whole. Diagnostics carry no recording. (PRODUCT-SPEC.md 2026-10-21; fixture-test/SPEC.md; Kai, 2026-11-05: cut
    "only for safety")
- **P8.12** How privacy is described.
  - **P8.12.1** Recording is never described in creepy terms (no "Watches"). The words stress that it stays inside the
    household's Home Assistant, so it's as private as their Home Assistant. (Kai, 2026-10-22: "basically as private as
    their HA setup")
  - **P8.12.2** The README has a Your data section, before License: kept in your Home Assistant; sent during a run;
    for how long; its own user; uninstalling; and "You decide what's kept: archive a conversation, or delete it for
    good."
    (TEXT-REVIEW.md 2026-10-25; Kai, 2026-10-25)
  - **P8.12.3** What's sent during a run is said broadly, never as a list: "the AI models you chose get what they need
    to do their work. Nothing comes to us." (Kai, 2026-10-25: "they don't need a laundry list")
  - **P8.12.4** The README's promises are aspirational, and a test holds each one true to the product. (Kai,
    2026-10-25: "make sure we already have tests in place to ensure these commitments don't diverge")

- **P8.13** How long things are kept.
  - **P8.13.1** No conversation is deleted for its age. (PRODUCT-SPEC.md 2026-10-21: no expiry; fixture-test/SPEC.md:
    "never deleted")
  - **P8.13.2** A conversation is kept in full for at least 30 days, then condensed, keeping enough for the improver
    to see the household's needs as a whole. (Kai, 2026-10-25: "Use: then condensed, keeping enough for the
    improver to see your household's needs as a whole")
  - **P8.13.3** A condensed conversation keeps what was said, Assist's tool calls, and the devices involved (their
    states, names, areas, exposure and device). The rest of the home goes. Condensing again gives the same result.
    (fixture-test/SPEC.md)
  - **P8.13.4** Every AI role still reads a condensed conversation, but it's never replayed (P6) or used to set up the
    sandbox. (fixture-test/SPEC.md, 2026-10-25)
  - **P8.13.5** The newest 20 conversations with turns stay in full however old, moment and all (P8.2.5), because the
    roles read the home at the newest one's moment. (fixture-test/SPEC.md)
  - **P8.13.6** A conversation an improvement's tests use stays in full however old. (fixture-test/SPEC.md)
  - **P8.13.7** A conversation waiting for analysis is never condensed. (Kai, 2026-10-25: "a conversation
    waiting for analysis is never compacted")
  - **P8.13.8** A recording with no turns (nothing reached the AI) is deleted, however recent. An open one is left
    alone. (fixture-test/SPEC.md)
  - **P8.13.9** A conversation's findings are conversation data, not logs: its analysis and, while it isn't analysed,
    its Triage verdict, its unreadable marker or its failed attempts. They're kept as long as the conversation,
    condensed or not, so it's never analysed again. (Kai, 2026-10-25: "Treat the findings as conversation data,
    kept as long as the conversation")
  - **P8.13.10** The improver's own logs are kept only as long as they're useful, a year at most. (Kai, 2026-10-25
    20:27: "R3: approved"; 20:37: "logs stay capped at a year")
  - **P8.13.11** Runs, with their stage timings, are kept a year. AI-call diagnostics are kept 30 days.
    (fixture-test/SPEC.md)
  - **P8.13.12** The Developer view's traces, with the improver's log lines, are kept a week and 20 MB at most. An
    active trace is never deleted. (Kai, 2026-10-25: "keep its retention at 1 week max"; fixture-test/SPEC.md)

- **P8.14** Archive.
  - **P8.14.1** Archiving is the default way to put a conversation away: it leaves the list but is kept, so other
    changes are still checked against it. Delete from Assist Improver is the privacy option. (Kai, 2026-10-25:
    "hide them from view while still keeping them for regression analyses"; P3.3)
  - **P8.14.2** Archive and Delete live in the opened conversation, behind a ⋮ beside Send this one in ("More actions"
    for screen readers and its tooltip). Its rows are "Archive this conversation" ("Unarchive" on an archived one) and
    "Delete from Assist Improver" (red; asks first). There's no separate window for recorded conversations.
    (TEXT-REVIEW.md 2026-10-25; Kai, 2026-10-25: "Fold this into conversation on the home page")
  - **P8.14.3** A conversation archived by hand is out of the pipeline, in Your household (P3.3). No run admits it
    (full, scheduled, picked or the fast lane), and no stage works on it or counts it. The proposer still reads it.
    (Kai, 2026-10-26: "archived conversations are not meant to be acted upon"; SPEC-CORE.md C5; fixture-test/SPEC.md)
  - **P8.14.4** Archiving a conversation during a run takes it out of that run. (FINDINGS.md #19; SPEC-CORE.md C5)
  - **P8.14.5** A revision note about an archived conversation waits until it's back: Propose doesn't count it, and no
    proposal is paid for it. (SPEC-CORE.md C5, 2026-10-26)
  - **P8.14.6** Sending an archived conversation in (from the panel, or filing it from Assist, including one archived
    while it was going on), or unarchiving it, brings it back into the pipeline. (SPEC-CORE.md C5; Kai, 2026-10-26
    03:54: "Proposal 1: yes")
  - **P8.14.7** Several conversations can be archived, unarchived or deleted at once from a selection. Each action
    shows its count, and Delete asks first. (TEXT-REVIEW.md 2026-10-25)
- **P8.15** Delete from Assist Improver.
  - **P8.15.1** Delete asks first: "Delete this conversation from Assist Improver? The improver can no longer check
    changes against it." For a selection: "Delete {n} conversation(s) from Assist Improver? The improver can no longer
    check changes against them." Buttons: Cancel · Delete from Assist Improver. An opened conversation asks in its
    Send row's place, so the question is in view and its answer the one button there. (TEXT-REVIEW.md 2026-10-25)
  - **P8.15.2** Deleting removes the conversation and everything kept about it: its recording, every stage's record of
    it, its notes, its findings (P8.13.9), and the tests run on it. (fixture-test/SPEC.md; Kai, 2026-10-25:
    findings "deleted with it")
  - **P8.15.3** Improvements stay. A kept improvement keeps what it shows as its tests: each test's goals, a copy of
    the turns it replayed, and its before and after. (fixture-test/SPEC.md: "The improvement itself stays, as Kai
    decided")
  - **P8.15.4** Deleting during a run is queued. The conversation leaves view at once and is out of that run (not
    analysed, proposed for, tested or written up, and out of every count). It's deleted when the run ends. (Kai,
    2026-10-25: "behave optimistically"; SPEC-CORE.md C7)
  - **P8.15.5** The household is told only if a delete fails: a Home Assistant notification titled "Assist Improver"
    says "A conversation couldn't be deleted. Try deleting it again." The conversation comes back (a settled one
    among the archived). (Kai, 2026-10-25: "only notify the user if there really ends up being an error"; Kai,
    2026-10-26: wording approved)

- **P8.16** Disabling and uninstalling.
  - **P8.16.1** Disabling the integration stops recording and keeps everything stored. (INSTALL.md; PRODUCT-SPEC.md)
  - **P8.16.2** Uninstalling removes everything the improver stored, the AI Tasks it set up and its service user
    (P7.6.15), the user first, so a later step's failure can't skip it. Accepted improvements but household lines
    (P16.6.5) stay. (Kai, 2026-10-25: "We'll delete AI tasks created by us and leave only
    improvements behind"; Kai, 2026-11-05)
- **P8.17** Inspecting recordings.
  - **P8.17.1** Administrators have two services for recordings: Recording status and Export recording. Developer
    mode adds List recordings, List improvements and Replay a recording. (Kai, 2026-10-25: "let's clean up those you
    deem unnecessary")
  - **P8.17.2** Recording status tells administrators only whether it's recording, how many recordings there are,
    pending writes and the last error. Once the integration stops, it says recording has stopped. (fixture-test/SPEC.md;
    INSTALL.md)
  - **P8.17.3** Export recording takes a recording's id and gives administrators only a copy of the whole recording,
    with every snapshot whole (one still recording included, a condensed one as kept) and what P8.11.4 cuts replaced.
    It changes nothing, and says when a recording is unknown or unavailable. (fixture-test/SPEC.md)
  - **P8.17.4** The integration's diagnostics download is a status summary, with no conversations. (INSTALL.md)
  - **P8.17.5** An export leaves Home Assistant only when the household shares it themselves. Nothing sends
    recordings anywhere. (INSTALL.md)
- **P8.18** Opt-in sharing, built after the integration is finished. (Kai, 2026-10-23: "we can do both after we've
  finished building the integration")
  - **P8.18.1** All sharing is off by default. A household that keeps its data private, and is working fine, stays
    that way. (Kai, 2026-10-23: "if they want to keep their data private and everything is working fine, fine";
    ROADMAP.md)
  - **P8.18.2** A problem report builds one file with what's needed to debug a conversation: its recording, its
    analysis and ideas, versions, the AI Task setup, and device and area details. It isn't redacted, names and
    address included, because personal context is often why something failed. (Kai, 2026-10-23: "home address, and
    specific names are all important"; ROADMAP.md)
  - **P8.18.3** The household sees exactly what the file contains before anything leaves their home, and sends it
    themselves (for example, attached to a GitHub issue). (ROADMAP.md 2026-10-23)
  - **P8.18.4** Ongoing sharing is a revocable switch that shares conversations continuously, with a clear retention
    policy; shared conversations become labelled tests for the product. (ROADMAP.md 2026-10-23)

## P9. Triage and Analyze

- **P9.1 The quick check.** Triage looks at each conversation it gets, on its own model (a small one, set up by
  default), and decides one thing: does it clearly need no improvement (smooth, and what was asked got done)? A
  conversation that needs nothing then doesn't pay for a full analysis. (Kai, 2026-10-24: "a small model ... or low
  cost method of filtering out requests that are already smooth and fulfilled"; DESIGN.md)
  - **P9.1.1** The stage is named Triage. On screen, what it does is called the quick check. (Kai, 2026-10-24: stage
    names; TEXT-REVIEW.md, 2026-10-25)
  - **P9.1.2** It checks conversations of any length, multi-turn ones included. Its line in Settings reads 'Triage
    clearly straightforward conversations'. (Kai, 2026-10-25: "triage for multi-turns conversations: agreed,
    approved")
  - **P9.1.3** A model judges, not code rules: whether tool calls happened doesn't show whether a request got done.
    (Kai, 2026-10-24)
  - **P9.1.4** It sees all the context Assist had, as the other AI roles do. Its tokens are cheap, and worth spending
    to save costlier ones later. (Kai, 2026-10-24: "these filter should basically have all the context that assist
    has.. just like the other AI roles")
  - **P9.1.5** It's told what a later turn usually means: the user repeating, correcting, answering a question, or
    telling Assist how it behaved. (DESIGN.md; TEXT-REVIEW.md, 2026-10-25)
  - **P9.1.6** Complaints and feedback on how Assist behaved, and a note sent with a conversation, are trouble to it:
    such a conversation is never smooth. (Kai, 2026-10-26)
  - **P9.1.7** A correction a person made themselves after Assist acted reaches both the quick check and the
    analyzer, even one that changes only an attribute (such as brightness). (FINDINGS.md #57)
  - **P9.1.8** It's conservative: when it's unsure, the conversation goes on to Analyze. (Kai, 2026-10-24: "we'll be
    conservative"; DESIGN.md)
  - **P9.1.9** It's calibrated and measured on labelled conversations, so it never skips a conversation that had
    friction. (Kai, 2026-10-24: "we'll need to calibrate"; DESIGN.md)
  - **P9.1.10** A conversation still going isn't checked or analysed; it waits until it ends. (PRODUCT-SPEC.md,
    2026-10-21: "Incomplete journeys wait for more evidence")
- **P9.2 Its verdict.** Triage hands on a verdict: smooth or not, and whether it's sure. A sure smooth ends the
  conversation there: it goes to Your household as Looked smooth. Anything else goes on to Analyze. (SPEC-CORE.md §3,
  2026-10-26)
  - **P9.2.1** With no Triage model set, every conversation passes straight through to Analyze, with no quick check.
    Settings says 'Pass through' there, not 'Off'. (Kai, 2026-10-24: "Off reads more like a block, when in reality,
    the behavior is requests will simply pass through")
- **P9.3 Who gets the quick check.** Every conversation a run admits gets it, and so does one held at Triage that a
  run releases, whichever run it is (manual full, scheduled, picked, the fast lane), unless it comes with a clear sign.
  A conversation picked for a run, or sent in from the panel without a note, gets it. (Kai, 2026-10-25: "ok let's do
  this"; TEXT-REVIEW.md, 2026-10-25)
  - **P9.3.1** A clear sign sends a conversation straight to Analyze. The clear signs: a note written when it was sent
    in; filing it from Assist (with or without saying what to improve); sending in again one already in Your household.
    (Kai, 2026-10-25 and 2026-10-26; SPEC-CORE.md C3)
  - **P9.3.2** With a clear sign, the analyzer is told the household sent it in, with the note if there is one. One
    that passed the quick check is analysed like any other, without being told it was sent in. (fixture-test/SPEC.md,
    Kai, 2026-10-25)
  - **P9.3.3** Run one step's Analyze runs no quick check. (TEXT-REVIEW.md, 2026-10-25)
- **P9.4 Looked smooth.** A conversation the quick check found smooth shows the chip 'Looked smooth', with the tooltip
  'Nothing to improve, at a quick look. Send it in for a full analysis.' (Kai, 2026-10-25: "Option A"; words approved
  the same day)
  - **P9.4.1** It can be sent in, or picked, one at a time, for a full analysis. Select all leaves it out.
    (Kai, 2026-10-25, Option A)
  - **P9.4.2** Sent in or picked, it goes past the quick check to a full analysis that reads the household's note.
    That analysis takes the verdict's place for every later stage. (Kai, 2026-10-25, Option A)
  - **P9.4.3** No run admits it again unless it's sent in or picked again. (Kai, 2026-10-25, Option A)
  - **P9.4.4** It stays within reach of the proposer and its reviewer as evidence: it shows where a rule would misfire
    on requests that already work, and helps a rule generalize. Only its full analysis is saved. (Kai, 2026-10-23,
    2026-10-24: "Reviewer should still have access to all the requests, regardless of whether or not they were deeply
    analyzed")
- **P9.5 Analyze.** An AI analyzer reads one conversation and writes down its goal tree (P2.2, P2.3): for each turn,
  the goals it serves, and their sub-goals. Analyze hands on the analysis: the conversation's goals, and the trouble on
  each. (DESIGN.md; SPEC-CORE.md §3, 2026-10-26)
  - **P9.5.1** It reads one conversation at a time, and never links a goal to another conversation. Goals that span
    conversations are left unhandled: a stretch goal for after everything works. (Kai, 2026-10-22: "maybe we should
    mark it as stretch goal that's unhandled"; ROADMAP.md)
  - **P9.5.2** It doesn't propose fixes. Fixes belong to Propose. (Kai, 2026-10-23: "is requesting for the
    fix/improvement a little premature? is it packing too much in one prompt?")
  - **P9.5.3** It judges only what needs judgement: turning the user's words into the changes they wanted. It adds no
    verdicts on top of the goals, such as met or avoidable; code derives what follows from the goals. (Kai, 2026-10-23:
    "where the model comes in handy really is human modeling")
  - **P9.5.4** It can see everything Assist saw, including every tool Assist called in a turn and what it returned.
    Upfront it gets the whole conversation (it's usually short) and, for each turn, the devices Assist's calls acted
    on. It isn't handed Assist's whole block of tools and states at once. (Kai, 2026-10-23: "show them the entire
    script at once"; Kai, 2026-10-24: "everything that the Assistant could see, the analyzer should've been able to
    see"; MILESTONE-7.md)
  - **P9.5.5** Which device a name matched comes from Home Assistant's own responses, not from custom matching.
    (Kai, 2026-10-24; MILESTONE-7.md)
  - **P9.5.6** It knows the household as the other roles do: Assist's instructions, the household knowledge, the
    personalizations already applied (preferences, aliases), and who's speaking. So it doesn't re-guess what the
    household already settled, such as which device 'my lamp' means. It reads these where they already are, with no
    second store. (Kai, 2026-10-23: "The analyzer needs to know me / the household as well as the other agents do")
  - **P9.5.7** It can explore the household to understand what was meant. Before deciding what a request means for a
    device, it finds out how the household operates it (for example, the garage doors open with a press).
    (Kai, 2026-10-23; MILESTONE-4.md)
  - **P9.5.8** Code works out the time between turns, which device states changed, and their order and timing
    (changes made together, delays between calls), and gives them to it. (DESIGN.md; Kai, 2026-10-23: "they could
    easily be derived programmatically as well")
  - **P9.5.9** It reads the whole conversation before deciding: intent from the first turn, then what each later turn
    adds. Goals and their destinations come from the whole conversation, its corrections and how it ended, before any
    test. (Kai, 2026-10-23; PRODUCT-SPEC.md)
- **P9.6 Reading goals.** A conversation is split into goals; a bare sequence of state changes isn't enough. Without
  goals it's like a round trip that never makes the stop it was for. (Kai, 2026-10-23)
  - **P9.6.1** What the user asked separately, at different times, stays separate goals; what they asked together is
    one goal. Garage open and garage close asked at different times are two goals; a requested open-and-close is one.
    This holds for the analyzer, the proposer and its reviewer. (Kai, 2026-10-23: "if i asked for them at a specific
    time, separately, then they should be separate. simple as that.")
  - **P9.6.2** Separate requests are never turned into a broader one the user didn't make, even when their intent
    seems predictable. (Kai, 2026-10-23: "restore the clause"; PRODUCT-SPEC.md)
  - **P9.6.3** A correction, a clarification, or an answer to Assist's question adds no new root goal. Its turn maps to
    the goal it corrects, and makes it more exact; its details belong to that goal. (DESIGN.md; MILESTONE-4.md;
    Kai, 2026-10-22: "turn 2 is part of the same journey")
  - **P9.6.4** When Assist answers a vague request by guessing and asking, the user's reply says what they wanted all
    along. Those goals belong to the vague request's turn; Assist's guess isn't a goal. (MILESTONE-4.md)
  - **P9.6.5** A later turn that shows how a goal is reached revises that goal: 'off and on again' turns a 'relays on'
    goal into a press. (MILESTONE-4.md)
  - **P9.6.6** A step taken only to get an earlier goal is a sub-goal of it (P2.3.2), never a goal of its own: asking
    what Assist can do, telling it how, finishing a half-done step, undoing something it did, or asking what Assist did
    or how a device is doing (a status check because Assist's reply didn't make it clear). (Kai, 2026-10-23: "add
    'sub-goals'"; 2026-11-02; LABELS.md; MILESTONE-5.md)
  - **P9.6.7** Changed intentions and missing preferences aren't set aside automatically: context may make an
    intention predictable, and a useful preference may be learnable. (PRODUCT-SPEC.md, 2026-10-21)
  - **P9.6.8** A change the user made themselves, outside Assist, isn't Assist's doing (a setting changed by hand).
    (Kai, 2026-10-22)
- **P9.7 Destinations.** A goal's destination is stated technically: the end states of the devices that changed from
  the home's start state, not loose labels. For fan-only, 'fan on' isn't enough; fan on with heat off is.
  (Kai, 2026-10-23: "the end states of the states that had a diff from the start state of the house?")
  - **P9.7.1** A device's destination is a target State: its state, plus any setting the user asked for (an attribute
    a field of one of its domain's actions sets: a light's brightness or colour, a thermostat's temperature). Both must
    be ones the device actually has, as Home Assistant has them, not the user's word: the device's own options and
    ranges where it shows them, else its domain's ('cool' is wrong for a climate device whose modes are off, fan_only
    and heat). A setting is one in any of Home Assistant's forms of it (a colour as rgb or hue and saturation, a
    brightness as a percentage or 0 to 255), and a number is reached within 5% of its range, or of the device's own
    step where that's coarser (a three-speed fan's). (MILESTONE-4.md)
  - **P9.7.2** The start state matters: a device already off changes what a request means. (PRODUCT-SPEC.md)
  - **P9.7.3** When either of two devices gets the home to the same end state, either is fine: a smart plug or the
    lamp itself both turn a lamp off. (Kai, 2026-10-23)
  - **P9.7.4** For something the user wanted to know, the destination is a short, fixed list of the facts,
    distinctions and constraints the answer needs, never the answer itself; the devices it's about, when it's about
    the home (then the truth is in the record); and the form the user asked for it in, in their own words, if any.
    (PRODUCT-SPEC.md, 2026-10-21)
  - **P9.7.5** Garage doors: the goal is the doors open (P2.3.1). A door's position can't be checked except by eye,
    so code judges the goal met by whether Assist pressed each relay (off, then on) in that order. A relay already on
    that doesn't change moves no door. Nothing claims a door's position from the relay alone. (Kai, 2026-10-23: "they'd
    check if destination was reached by checking if the assistant activated those states in those sequences";
    INSTALL.md)
  - **P9.7.6** For something done that leaves no device in a new state, the destination is the outcome, not a service:
    what it does and the devices it's for, whatever the words. An announcement is speech or audio on devices; a command
    is a device told to do something it keeps no state of (a button pressed, an integration's own action). A button, a
    scene or a script keeps no state to reach, only when it was last used: a scene asked for is its devices' target
    States (P9.7.1). Any call that does it reaches it. (Kai, 2026-10-29; Kai, 2026-10-30: "the outcome to measure here
    is whether or not the assistant was able to trigger a broadcast with words/audio of its choice ... not the content
    of those which really depends on the intelligence of the assistant model")
- **P9.8 Not requests.** A turn that isn't a request to Assist (a speech test, a misfire such as silence heard as a
  request, small talk) is never trouble: it's no extra turn, and no device or preference fix is invented for it.
  (Kai, 2026-10-22: "a speech test, not a real request"; PRODUCT-SPEC.md; LABELS.md)
  - **P9.8.1** The household sets aside a conversation that was only a test by archiving or deleting it. (Kai,
    2026-10-28: "If the user doesn't want to run it they can just archive or delete it")
- **P9.9 Asking the household.** Where the user's words leave settings open, the analyzer leaves them open rather
  than inventing values: it lists the controls to set, as narrow as the words allow. (LABELS.md; MILESTONE-4.md;
  DESIGN.md)
  - **P9.9.1** A preference still to be learned becomes such an ask: 'warm up my room', or 'warm lights only' when
    nothing shows which lights are warm. (LABELS.md; MILESTONE-4.md)
  - **P9.9.2** When what the user wanted can't reasonably be inferred from what they said, it comes back to the
    household as a question. (Kai, 2026-10-23: "they come back to me as questions, right?")
  - **P9.9.3** The question narrows the unknown to exactly what must be settled: the specific devices and their
    controls. For 'warm lights', it shows the household's lights to pick from (not a colour change); for a car, its
    controls to tap and set. (Kai, 2026-10-23: "the question should present me a set of lights and ask me to
    select")
  - **P9.9.4** Before asking, the analyzer gathers what the household already shows (for example, on the garage
    doors). (Kai, 2026-10-23: "it should first gather enough information on the garage doors based on the existing
    household")
  - **P9.9.5** While a question to the household is open, its goal can't be told yet, and the turns it covers aren't
    extra turns. (Kai, 2026-10-23: "treat a questioned turn as pending"; LABELS.md)
  - **P9.9.6** When the analyzer can't tell what the user wanted, or the proposer needs what only the household can
    tell, the conversation waits at Your review for their answer, beside the improvements: the pipeline needs a person.
    It's never shown as No trouble found. (Kai, 2026-10-26: "if the pipeline need clarifications from the user, the
    package is sitting in the human reviewer's queue")
  - **P9.9.7** The answer takes the conversation straight back to the stage that asked, in the same round, on a run of
    its own (P5.12); that stage reads it with its question, not as a turn. (Kai, 2026-11-04)
  - **P9.9.8** A goal whose destination can't be established gets no tests: nothing is spent on fixing it until it
    is. (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md)
  - **P9.9.9** A turn Home Assistant answered itself (its built-in commands, not the AI) can't be replayed, so no fix
    for its conversation can be tested. When the analysis finds trouble in such a conversation, it waits in Your
    review's queue as Can't improve, with no Propose and no test: its bubble quotes what the household said in the
    first such turn and names the voice assistant whose Prefer handling commands locally setting let it happen. The
    household archives it; sent in again, it would come back the same, since the recording keeps Home Assistant's
    answer. One the analysis finds no trouble in ends as any other. (Kai, 2026-10-29: "Approved ur suggestions to
    send those to your review, and ur new phrasing"; it still reaches the analyzer "if we can't determine
    confidently that all goals were met")
- **P9.10 Met, unmet, can't be told.** Whether a goal was met (P2.3.3) is mostly deterministic: the home's start
  state plus the actions Assist ran give its end state. Code derives it, not a model. (Kai, 2026-10-23: '"reached" is
  deterministic from house's start state + actions run on drivers = house's end state')
  - **P9.10.1** Only Assist's own changes count toward a goal met: a change the user made by hand doesn't.
    (LABELS.md; MILESTONE-4.md)
  - **P9.10.2** A goal can't be told when the evidence can't show it (the fan case). (Kai, 2026-10-23: "it plausible
    that the state is indeterminate")
  - **P9.10.3** A conversation's session expiring shows neither what the user wanted, nor whether it was met, nor how
    many extra turns it took. (INSTALL.md)
  - **P9.10.4** What the user asked to know is the exception: a model judges whether a reply gives what the answer
    needs (P9.7.4), true to the home as Assist saw it before replying and to what its tools returned in that turn, and
    in the form asked for, with what the home did in that turn beside it. In the conversation it's judged once, at
    analysis, on the reply that ends its run of steps (a reply a step follows is unmet by the step itself, as is the
    last one said when the note asks again); a wrong answer is an unmet goal, trouble. One beyond both is judged only
    on whether the reply answers it, in the form asked for when given; with no form and no tool's result in its turns,
    it can't be told.
  - **P9.10.5** An action (P9.7.6) is met when the calls of its own request's turns, from its turn on, did it on every
    device it's for (an entity counts for its whole device; one unavailable as its turn began reached nothing): an
    announcement is a call at the device carrying words or media (a chime), its content not judged for now; a command
    is a call that reached it. What happened is one record, read the same way recorded and replayed: each turn's tool
    calls with their results (a broadcast's names the satellites it was sent to), and its call log, every action call
    under the turn's request, a script's own included (P8.2.10; a replay's sandbox keeps one the same way). A call that
    failed reaches nothing. One for a device none of whose entities was available in the turns it took is one the home
    couldn't do (P9.10.6). Code decides it from the calls; where a recording ran a script and kept no call log, it can't
    be told. (Kai, 2026-10-30: "It's hard to judge the content of those audio. We can create an abstraction that
    decides based on the content of these announcements (eg audio/text that's going to be translated to speech), but
    just stub them out (passthru) for now, because it's undecided whether parsing the contents to match user intent is
    a worthy cost or not.")
  - **P9.10.6** The home couldn't do a root goal when, in every turn its tree took, its command failed at home (a call
    of the turn's call log on its device ended in an error, which a replay's sandbox gives again, P11.4) or its device
    was unavailable. That's context for an unmet goal, not a state of its own: the analysis, a test's record, the
    editor's input and the review say which.
- **P9.11 Trouble.** Trouble is any of four: extra turns, unmet goals, side effects left, and the household's feedback
  on how Assist behaved. Each kind flows through every stage, from the analysis to Your review. (Kai, 2026-10-26:
  "let's make sure it plumbs/flows its way correctly down the pipeline all the way through"; PRODUCT-SPEC.md,
  2026-10-25; DESIGN.md)
  - **P9.11.1** Extra turns are the user turns spent reaching a goal beyond the first: avoidable clarification,
    correction, repetition, and any other turn needed to get there. (PRODUCT-SPEC.md, 2026-10-21)
  - **P9.11.2** Code, not the model, derives extra turns from the goal tree. A turn that serves a sub-goal is an extra
    turn of the goal it serves; a sub-goal never stands in for a goal. (LABELS.md; ROADMAP.md; README)
  - **P9.11.3** A clarification Assist could have avoided (asking what the household's setup already shows) is an
    extra turn of the same goal. (Kai, 2026-10-22: "turn 2 is part of the same journey, an avoidable extra step")
  - **P9.11.4** An unmet goal is one Assist didn't meet: it guessed or missed what the user wanted. (DESIGN.md)
  - **P9.11.5** An analysis that finds no trouble sends the conversation to Your household as No trouble found.
    (SPEC-CORE.md §1)
- **P9.12 Side effects left.** A side effect left is a device Assist changed that no goal asked for, and that Assist
  didn't leave as it started: its settable state, its state and each setting one of its actions sets (P9.7.1), not a
  reading. A button's last press or a script running is never one: it keeps no state to reach (P9.7.6). (Kai,
  2026-10-26: "1. yes"; 2026-11-02; PRODUCT-SPEC.md, 2026-10-25; S5)
  - **P9.12.1** Code derives it, not the model. (PRODUCT-SPEC.md, 2026-10-25; LABELS.md)
  - **P9.12.2** Only Assist's own change counts. A device's own follow-on isn't one. A signed-in person's later change
    to a device Assist acted on is theirs: never counted or labelled as Assist's. (SPEC-CORE.md S2, 2026-10-26;
    FINDINGS.md #58)
  - **P9.12.3** A device whose start state was unknown or unavailable never counts, in a new analysis or one kept from
    before. (Kai, 2026-10-26; fixture-test/SPEC.md)
  - **P9.12.4** It counts on the goal whose turn made the change. (PRODUCT-SPEC.md, 2026-10-25)
  - **P9.12.5** Only how Assist left the device matters. Anyone or anything else putting it back (a person at the
    switch or in an app, an automation, the device's own timer) doesn't clear it, since putting it back was work Assist
    caused. Asking Assist to put it back counts as that turn instead, so each slip counts once: as a turn if the user
    asked Assist to undo it, otherwise as a side effect. (Kai, 2026-11-02: "Any put-back counts")
- **P9.13 Feedback.** What the household said about how Assist behaved (a complaint, carelessness, wording, length,
  tone, 'next time do X') is trouble, whether said in a turn or in the note a conversation was sent in with. It's a
  sub-goal of the goal it's about, in the household's own words (P2.3.2). (Kai, 2026-10-26 and 2026-10-29;
  PRODUCT-SPEC.md, 2026-10-25)
  - **P9.13.1** Feedback about a goal counts on that goal. (PRODUCT-SPEC.md, 2026-10-25)
  - **P9.13.2** A note sent with a conversation, or a revision note, is a turn whose goal is a sub-goal of the
    conversation's root goal (P2.3.4; a revision note's, of the root its improvement is for; one only about the
    improvement itself maps there with no goal, P12.8.1); its feedback counts there. (Kai, 2026-10-26)
  - **P9.13.3** A note is never dropped: an analysis that gives the note's turn no goal is refused (P9.15), unless
    it's a revision note only about the improvement itself, listed apart (P12.8.1).
    (PRODUCT-SPEC.md, 2026-10-25; SPEC-CORE.md S2, 2026-10-26)
  - **P9.13.4** Feedback about something an applied improvement makes Assist do lets the proposer revise that
    improvement. (Kai, 2026-10-26; PRODUCT-SPEC.md, 2026-10-25)
- **P9.14 Already fixed.** The analyzer sees the existing improvements on the devices its conversation touched (code
  finds them), and can read any of them. (DESIGN.md)
  - **P9.14.1** It can mark a goal as already fixed by an existing improvement, and code checks the mark. The goal
    stays in the conversation's tree (P4.1), but no later stage proposes for it or tests it: nothing is fixed twice.
    (Kai, 2026-10-25: "call mark_duplicate(<goal_id>, <improvement_id>) so it doesn't flow to the next stage";
    DESIGN.md)
- **P9.15 Checks on the analysis.** The analyzer's answer is checked against the recording: its form, the turns, that
  each device exists, that each state is one the device has, and that each sub-goal links to a goal it serves. An
  answer that fails is rejected with the reasons, so the analyzer can fix it. (MILESTONE-4.md; Kai, 2026-10-23: "if an
  action/state is invalid, we should just tell the agent so they can fix it")
  - **P9.15.1** The last accepted answer counts. If none was accepted, nothing moves on: Analyze tries again, then
    holds the conversation (P4.4.2). (MILESTONE-4.md; FINDINGS.md #61; DESIGN.md)
- **P9.16 A fresh analysis.** The fresh analysis of a conversation sent in again replaces the old one for every later
  stage: it's proposed for only from its fresh analysis, never from the old reading. (SPEC-CORE.md C3, 2026-10-25)

## P10. Household knowledge and assistants' instructions

- **P10.1** Every AI role (the analyzer, the proposer, the reviewer) gets the household and user context it needs
  to understand its task, as well as the other roles have it. (Kai, 2026-10-23: "they should all have the data they
  need to understand and contextualize the tasks they're doing"; "The analyzer needs to know me / the household as
  well as the other agents do.")
  - **P10.1.1** A role reads that context from the existing sources (Assist's applied personalizations, the
    household knowledge, each assistant's instructions), never from a second memory store. The context is read-only;
    models still only judge. (Kai, 2026-10-23)
  - **P10.1.2** Every AI role gets the Home Assistant primer upfront. (Kai, 2026-10-23: "im thinking these should be
    given upfront, probably. because they really can't do much of their work without them")
  - **P10.1.3** Over time the improver learns the household's facts and preferences: which lights are warm and
    which are white, and that "warm up my room" means heating on. (Kai, 2026-10-23)
  - **P10.1.4** Assist should use context from the room the user is in, or the device they speak through. The
    device fact (P10.7.2) supplies the device; Assist's own prompt states its area. (Kai, 2026-10-23: "i also want
    contextualization based on which room im in (or whatever i'm speaking through)"; TEXT-REVIEW.md, 2026-10-25)

- **P10.2** The household knowledge is one shared place. Improvements to Assist's programming carry over to every
  assistant, rather than living in one assistant's prompt. (Kai, 2026-10-24: "Yeah, let's build option 2 first")
  - **P10.2.1** It comes with Home Assistant's own Assist. Every assistant that controls the home through Assist reads
    it, on any provider: there's no second option to choose, nothing to turn on per assistant, and no tool offered
    twice. (Kai, 2026-10-25: "Not having to choose per assistant is the better design"; DESIGN.md)
  - **P10.2.2** An assistant can't end up with both Assist and a separate household option: duplicates are prevented
    by design, not repaired afterwards. (Kai, 2026-10-25: "I'd prefer if we can prevent duplicates to begin with.")
  - **P10.2.3** Every saved or accepted line reaches every assistant on Assist at its next request. An assistant
    added later reads the lines from its first request, with nothing to set up. An assistant that doesn't control
    Home Assistant isn't covered. (fixture-test/SPEC.md, 2026-10-25; TEXT-REVIEW.md, 2026-10-25)
  - **P10.2.4** It holds lines for everyone, and lines for one person that only that person's requests see: not
    someone else's, and not a voice satellite's. (DESIGN.md, 2026-10-25; fixture-test/SPEC.md)
  - **P10.2.5** Everyone's lines come first, then the speaking person's. (fixture-test/SPEC.md)
  - **P10.2.6** Household lines are plain text. A line that looks like template code (`{{`, `{%`, `{#`) is never
    run. (Kai, 2026-10-25; fixture-test/SPEC.md)
  - **P10.2.7** The household knowledge is kept across restarts. (fixture-test/SPEC.md; PRODUCT-SPEC.md)
  - **P10.2.9** The README's Good to know says every assistant benefits (P17.25.9). (TEXT-REVIEW.md, 2026-10-25)

- **P10.3** Every assistant on Assist reads it, because Home Assistant doesn't say which assistant is asking.
  (fixture-test/SPEC.md)
  - **P10.3.3** An improvement switches no assistant. (fixture-test/SPEC.md, 2026-10-25)

- **P10.4** An improvement's household line goes to the speaking person (this_user) or to everyone. A line already
  there isn't added twice. (fixture-test/SPEC.md; DESIGN.md)
  - **P10.4.1** A line it replaces is removed where it is. A replaced line found in neither place is a problem,
    unless the new line is already there. (fixture-test/SPEC.md)
  - **P10.4.2** A line for the speaking person is applied for each person it was tested for, not only one of them.
    (FINDINGS.md #14)
  - **P10.4.3** Undoing an earlier household-line improvement after a later one was accepted takes out only its own
    line. That line isn't left live, marked unapplied, or said to have been changed by the household.
    (FINDINGS.md #11)
  - **P10.4.5** When a command on one device changes another (a device link), the proposer can propose that fact as a
    household line, for the household to review, so Assist knows it on every request. (MILESTONE-7.md, decided
    2026-10-23: Home Assistant's assistant has no memory, and a to-do list would need a tool call on every request)

- **P10.5** From Your household in the settings, the household opens what the household knowledge holds.
  (Kai, 2026-10-24; TEXT-REVIEW.md, 2026-10-25)
  - **P10.5.5** The lines open in a modal, a bottom sheet on a phone: everyone's lines, then each person's by name
    ('Someone' for a person Home Assistant no longer has). Empty, it says nothing is learned yet.
    (fixture-test/SPEC.md)
  - **P10.5.6** Long lines are clamped with Read more; short ones aren't. Search appears when the list is long. The
    modal closes with its close button, a click outside, or Esc. (fixture-test/SPEC.md)
  - **P10.5.7** One line at a time can be added (for everyone, or just for me, even before I have any), edited in
    place, or deleted after asking. Text is trimmed. A person with no lines left drops out of the view.
    (fixture-test/SPEC.md)
  - **P10.5.8** A failed edit says why and keeps what was typed. (fixture-test/SPEC.md)
  - **P10.5.9** Home offers nothing about household knowledge. (fixture-test/SPEC.md)

- **P10.6** The proposer may edit an assistant's own instructions, because faults can lie there. (Kai, 2026-10-26:
  "the proposer and reviewer, especially, needs to be able to see the assistant's own instructions, because that
  could be where faults lie.")
  - **P10.6.1** A fault in one assistant's instructions (a fact they don't give, such as who's speaking, or a rule
    that's wrong) is fixed there, by adding, replacing or removing lines. A household fact goes in the household
    knowledge instead. (DESIGN.md; TEXT-REVIEW.md, 2026-10-25)
  - **P10.6.2** An instruction edit is checked by code, replayed only on that assistant's conversations, and applied
    to that assistant alone. Other assistants' instructions stay as they were. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.6.3** The review shows an instruction edit as a diff titled '{assistant}'s instructions', tagged
    'instructions'. A decided but unapplied one reads 'Add to {assistant}'s instructions: “{line}”', '…, replacing
    “{old line}”', or 'Remove from {assistant}'s instructions: “{old line}”'. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.6.4** An instruction edit can't apply, and 'Can't apply it now' says why, when: the instructions changed in
    Home Assistant after it was tested; the assistant is no longer set up; the line it replaces changed since
    testing; or Home Assistant can't read the edited instructions. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.6.5** End to end: a conversation on an assistant whose instructions don't say who's speaking gets that fact
    proposed, reviewed with what Assist would read, replayed with it, written up and accepted, and other assistants'
    instructions stay as they were. (fixture-test/SPEC.md; DESIGN.md)

- **P10.7** Code doesn't add request facts to every request, since that would repeat what households put in their
  own prompts. Instead the proposer gets a short list of recommended facts to add to an assistant's own instructions
  where they're missing. (Kai, 2026-10-26: "perhaps we'd give proposer a list of recommended facts to add into the
  instructions if it isn't already added.")
  - **P10.7.1** Who's speaking: `{% if user_name %}The person speaking is {{ user_name }}.{% endif %}` (nothing for
    a request with no signed-in person). Why: "my room" or "my lights" depend on who asks, and Home Assistant says
    who only when the instructions do. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.2** The device the request came from: `{% if llm_context.device_id and device_name(llm_context.device_id)
    %}The request came from {{ device_name(llm_context.device_id) }}.{% endif %}`. Why: Home Assistant says the asking
    device's area but never which device it is. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.3** The date and time: `It's {{ now().strftime('%A %-d %B %Y, %H:%M') }}.` Why: Home Assistant leaves the
    date and time out of the instructions when Assist has its date-and-time tool, as it does by default.
    (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.4** The household's time zone: `The household's time zone is {{ now().tzinfo }}.` (Home Assistant's own
    time zone setting). Why: Home Assistant names it only when Assist asks its date-and-time tool, and then only as a
    short code. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.5** The device fact names only the asking device, not its area: Assist's own prompt already states the
    area when the device has one (checked in Home Assistant 2026.10). (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.6** The date and time stay on the list. The proposer judges whether each fact is missing: Home Assistant
    adds the date and time unless the assistant has a date-and-time tool, and households may have written their own
    lines. (Kai, 2026-10-26: "wouldn't it be fine to let that be in the list of recommended facts, still, and let the
    respective agent decide if it was left out or not, and if it should be added, if it's not already?")
  - **P10.7.7** A fact is added only where an assistant's instructions lack it, and only when a trouble's cause is
    Assist not knowing that fact. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.8** A fact is never proposed twice. Code checks whether it's there by the template code it uses,
    including lines added by another idea or by an improvement waiting or applied. A fact written out in words (such
    as a name) is a hint the roles judge. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.9** The list is kept in one place and given to the proposer and reviewer. Each fact's why is for them
    only; Assist reads only the rendered line. (TEXT-REVIEW.md, 2026-10-25)
  - **P10.7.10** The household knowledge, which code delivers to Assist, stays a separate channel for household
    facts, and the recommended facts don't limit it. No rule says facts are never added by code. (Kai, 2026-10-26: "be
    careful of codifying texts like that. this potentially conflicts with proposer adding facts through household
    knowledge, which are added by code")

## P11. The sandbox

- **P11.1 Home Assistant's own logic.** In the sandbox, Home Assistant runs as much of its own logic as it can. Only
  the interactions with physical devices (the drivers) and calls beyond the home are stood in. (Kai, 2026-10-22:
  "ideally we want HA to run as much of its logic as possible, except the interaction between physical devices")
- **P11.2** Pieces of Home Assistant are never rebuilt for simulation: no script runner, event bus or exposure proxy
  of our own. A simulation gap is closed with Home Assistant's own machinery in the sandbox. One clock serves the
  whole sandbox's process: Python's time, set forward as Home Assistant's own tests set it, and nothing in Home
  Assistant patched (P6.2.5). (Kai, 2026-10-23: "Definitely don't do this"; Kai, 2026-11-05: one clock for the whole
  sandbox, "if we encounter issues, then we'll consider splitting them out", superseding "no clock of our own")
- **P11.3 Nothing in the home moves.** No real device acts during a replay or a test: stand-ins take the devices' place
  (P11.4). Every other call beyond the home (an integration's own action, a notification, a model other than the turn's
  own) is stood in and logged as an outside receipt (P11.7): an LLM API tool answers as the recording answered it, any
  other call empty and "not simulated". Home Assistant's built-in conversation agent answers as at home.
  (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md; FEASIBILITY.md; DESIGN.md)
- **P11.4** Every entity domain Home Assistant has is stood in: a stand-in entry per config entry, titled the same,
  holds its devices (their ids, areas and names kept) and entities as their registry entries have them. Home Assistant
  checks each command (its fields, the device's features, options and ranges), and the stand-in reports back the way
  the device would: a field to its attribute, a command to its state, a mover through its in-between state (P6.2.7). A
  command it can't report back is "not simulated"; one that failed at home fails the same way, with its recorded error.
  This includes commands through Home Assistant's own intents. Gap: a template asking by integration
  (`integration_entities('<domain>')`) finds nothing. (fixture-test/SPEC.md; ROADMAP.md)
- **P11.5** Physical positions aren't simulated: a door's motion counts as done when its command succeeds. Only where
  the improvement involves a garage, door or gate cover, a valve, a lock or an alarm panel (by its domain and a cover's
  device class, never its name), or helps a sequence goal (P9.7.5: presses standing for a motion nobody can check), the
  review says: "Motion isn't simulated: a door, valve, lock or alarm counts as done once its command succeeds."
  (TEXT-REVIEW.md, 2026-10-25)
- **P11.6** Links between devices aren't emulated (for example, one device going off switching a linked one off), so
  there's no link logic to maintain. (MILESTONE-7.md, 2026-10-23; ROADMAP.md)
  - **P11.6.1** If links are ever emulated, a household's own links never ship for everyone: they'd disclose its
    personal information. (Kai, 2026-10-24)
- **P11.7 Simulated isn't observed.** Simulated results stay apart from observed ones: a relay switching doesn't prove
  a door's position, a receipt from an outside tool is labelled as one, and goals are set at a level the simulation
  can show (a thermostat's target, not the room reaching it). Simulated success is never presented as proof of what a
  device physically did, and the write-up states these limits. (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-2.md;
  FEASIBILITY.md; MILESTONE-7.md)
- **P11.8 A replay's sandbox is isolated.** Each is a separate Home Assistant instance, started by Home Assistant's
  own startup, with no device integrations and no web server. It sets up Home Assistant's core (Assist, scripts,
  automations, scenes, the sun), the home's storage and YAML helpers, and the stand-ins (P11.4), with what they depend
  on; never discovery, the cloud, the network, the recorder or the Supervisor's. The real instance stays the one Home
  Assistant's helpers find. Sandbox log lines stay out of Home Assistant's log. Everyday Assist carries on
  unaffected while replays run. (MILESTONE-6.md, 2026-10-23; FEASIBILITY.md; DESIGN.md)
  - **P11.8.1** Each replay's sandbox runs in a fresh process of its own, gone with the replay, in a session of its
    own: a signal to its process group doesn't reach Home Assistant. (Kai, 2026-11-04: "trading a few seconds for much
    cleaner code definitely worth it")
  - **P11.8.2** If a sandbox fails to start (say Home Assistant's internals changed), the replay reports it and the
    real Home Assistant is unaffected. (MILESTONE-6.md, 2026-10-23)
  - **P11.8.3** A sandbox's process gets none of the real instance's Supervisor access, and installs nothing. (P11.3)
  - **P11.8.4** A guard in a sandbox's process catches accidents: it writes and listens only in its own folder, signals
    only its own process and those it started (not Home Assistant, nor a process group), starts no process but Home
    Assistant's own ffmpeg version check and Python's library lookups, and connects only to the internet beyond its
    host's own networks and to the endpoints its provider's entry or environment names. Anything else is refused and
    stops the replay as the machinery's fault. Limits: code in compiled libraries can get past it, and it doesn't see a
    pipe or device made outside its folder, nor a file made in memory (Python raises no audit event for os.mkfifo,
    os.mknod or os.memfd_create); it stops accidents, not malware. What holds depends on the install: on HA OS, whose
    kernels don't build Landlock, the guard alone; on a Container install whose host kernel enables Landlock, Landlock
    holds the file rule too, for compiled code and pipes or devices as well. (Kai, 2026-11-05: "sandbox containment: ok,
    yeah perhaps as seatbelts")

## P12. Write up, Your review, and applying

- **P12.1 Write up.** After Simulate, Write up writes each tested improvement up for the household; then it goes up
  for review. (DESIGN.md; Kai, 2026-10-24: "this seems like a role a cheap model should be able to do")
  - **P12.1.1** Code builds an improvement only from ideas the proposer's reviewer passed (its regression check
    included) and whose tests showed an improvement (P6.1). Only changes that proved themselves reach the household.
    (PRODUCT-SPEC.md, 2026-10-21; fixture-test/SPEC.md)
  - **P12.1.2** There's one improvement per set of changes, however it's worded (P2.4). (fixture-test/SPEC.md;
    MILESTONE-7.md, 2026-10-23)
  - **P12.1.3** A small editor role, on a cheap model, writes each improvement's headline and what you get. The
    headline says what it does and is simple to read, e.g. "Expose garage door controls via scripts".
    (Kai, 2026-10-24; DESIGN.md)
  - **P12.1.4** Code checks the editor's words: short, no entity IDs, no jargon. (DESIGN.md)
  - **P12.1.5** The editor is told what each test showed, and asked to say so, e.g. "no longer changes {device} (it
    stays {state})", "no longer does what you pointed out: “{their words}”"; and what it didn't, never to be written as
    fixed: "still does what you pointed out: “{their words}”", "the home couldn't do it: {device} was unavailable when
    asked" (or "{device} failed", P9.10.6), "still not done: {goal}". What's fixed and what's open come only from these, never from the replies.
    (TEXT-REVIEW.md, 2026-10-25; Kai, 2026-10-29: words on judgement; the household's broadcast revision, 2026-10-30)
  - **P12.1.6** The editor is told when an improvement's tests made Assist's replies longer (P6), and never calls it
    briefer. (TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md)
  - **P12.1.7** No model writes why an improvement is safe: code states it (P12.3). (DESIGN.md)
  - **P12.1.8** If the write-up fails, the improvement goes up anyway, with the proposer's title, and says why the
    write-up failed. (SPEC-CORE.md §3, 2026-10-26)
  - **P12.1.9** The household knows an improvement by its headline (the proposer's title when there's none) in every
    notice, the list and its review. (fixture-test/SPEC.md)
  - **P12.1.10** An improvement waiting for its write-up isn't up for review: its conversation shows "Writing it up",
    never "Up for review", and the notification leaves it out. (SPEC-CORE.md position table; fixture-test/SPEC.md)
  - **P12.1.11** What Write up takes in (the improvement: its idea and changes from Propose, its tests from Simulate)
    and what it hands Your review (the headline and what you get, or why the write-up failed) are well defined,
    typed and tested (P3.2). (Kai, 2026-10-26; SPEC-CORE.md §3)

- **P12.2 Your review.** Each written-up improvement is reviewed like a pull request, on its own page in the panel,
  with its own address. (DESIGN.md; fixture-test/SPEC.md)
  - **P12.2.1** The panel is the one place to review improvements: there's no review screen in Configure or
    Repairs. (Kai, 2026-10-24: "can we remove the ones in other places, and link the notification into the panel?")
  - **P12.2.2** Code builds the review from what the pipeline already has: the idea and what went wrong; the
    conversations it addresses, each goal with the user's words; the exact changes as Home Assistant will see them
    (e.g. instruction lines, names, exposure, script steps); before and after from the replays, turn by turn; what
    was tested versus only expected; and its limits. (MILESTONE-7.md; ROADMAP.md; PRODUCT-SPEC.md, 2026-10-21)
  - **P12.2.3** A review leads with its headline, its status in plain words, and what you get.
    (fixture-test/SPEC.md)
  - **P12.2.4** A Summary sits on top, always open: what you get, its before and after, and why it's safe. What went
    wrong and what changes are folded below (P14.12.2). (Kai, 2026-10-24: "Users should easily be able to get 'what's
    in it for them', and 'why it's safe'"; DESIGN.md; Kai, 2026-10-30: the order)
  - **P12.2.5** What you get is counted in goals, turns and side effects: "{n} new thing(s) Assist can do" (goals now
    met), the turns it saves and "{n} unwanted change(s) avoided" (side effects gone). (TEXT-REVIEW.md, 2026-10-25;
    DESIGN.md)
  - **P12.2.6** The page reads like a code-review diff viewer, benefits first. (Kai, 2026-10-24: "the visual should
    follow that of code review diff viewers")
  - **P12.2.7** No section lists again what the line under the headline counts (P14.12.3); top-level outcomes are
    always green. (Kai, 2026-10-24; TEXT-REVIEW.md, 2026-10-25; Kai, 2026-10-30: "the rest seems unnecessary")
  - **P12.2.8** Each test's Before shows its conversation as it went, and its After its replay with the change: a turn
    no longer needed is one After doesn't say. (Kai, 2026-10-24; Kai, 2026-10-30: "before & after (open by
    default)")
  - **P12.2.9** "What changes ({n})", folded at first, has one collapsed line per change (its name, its kind, and its +
    and − counts in green and red) that opens to its line-numbered diff. (Kai, 2026-10-24; TEXT-REVIEW.md,
    2026-10-25; DESIGN.md; Kai, 2026-10-30: "Then the proposed changes (what changes) - same")
  - **P12.2.10** Each nesting level indents further. (Kai, 2026-10-24)
  - **P12.2.11** "What went wrong" (was "How it works") gives the cause, and what the sandbox doesn't simulate (e.g.
    a relay's transitions don't prove a door's position; a thermostat's target isn't the room reaching it).
    (TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md; MILESTONE-2.md; FEASIBILITY.md)
  - **P12.2.12** The review keeps what happened in the household's conversations apart from what the replays
    predict. (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-7.md)
  - **P12.2.13** "Before & after" shows its conversation's test. (DESIGN.md; MILESTONE-7.md, 2026-10-23; Kai,
    2026-10-26; Kai, 2026-10-30)
  - **P12.2.14** Each Before & after row has a result chip: "goal now met · {a} → {b} turns · improved · side effect(s)
    gone · follows your feedback", joined with " · ", and an amber "{n} still open" chip where something the
    household pointed out isn't shown fixed (P17.21.3). On a phone the chips go on their own line under what was
    tested and wrap within the row. (TEXT-REVIEW.md, 2026-10-25)
  - **P12.2.15** A test whose replay made Assist's replies longer (P6) gets an amber "Says more" chip on its row,
    with the tooltip "Assist's replies: about {after} words each, {before} before."; under the tests: "With this
    change, Assist's replies got longer in {n} of these." A review with no such test says nothing about it.
    (Kai, 2026-10-26: "let tests flag replies that got longer"; TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md)
  - **P12.2.16** An edit to an assistant's own instructions shows as a diff titled "{assistant}'s instructions", with
    the tag "instructions". (TEXT-REVIEW.md, 2026-10-25)
  - **P12.2.17** A decided improvement that isn't applied reads "Add to {assistant}'s instructions: “{line}”",
    "…, replacing “{old line}”", or "Remove from {assistant}'s instructions: “{old line}”". (TEXT-REVIEW.md,
    2026-10-25)
  - **P12.2.18** Lines and sections with nothing in them are left out. (fixture-test/SPEC.md)
  - **P12.2.19** A review is checked against Home Assistant as it is now: before deciding, it shows what accepting
    would change right now, and any conflict (P12.6). (MILESTONE-7.md, 2026-10-23; fixture-test/SPEC.md)
  - **P12.2.20** An improvement that can't apply now lists why under "Can't apply it now:" (P12.5.18).
    (TEXT-REVIEW.md, 2026-10-25)
  - **P12.2.21** The household never has to edit an improvement's code. (PRODUCT-SPEC.md, 2026-10-21)
  - **P12.2.22** The review and applying are code: no model takes part, and the household decides.
    (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-7.md)
  - **P12.2.23** A review that can't be found says why. (fixture-test/SPEC.md)
  - **P12.2.24** An automation's review says when it runs: its triggers and conditions in words, stated by code from
    Home Assistant's own configuration; and what it did over a simulated week (P6.13), run before the write-up (a
    run's improvements' weeks side by side): how many runs and when first, each run and what it changed on demand.
    For a change to one you have, or one turned on or off, the runs the change adds and drops. A week that couldn't
    run says so. (Kai, approving: "An automation's review says when it would run and
    what it would have done over a simulated week; for a change to one you have, the runs it adds and drops.")
  - **P12.2.25** A better setting for an assistant (its model, web search) is listed with the changes, named for you
    to change yourself: "Not applied or tested: change it yourself if you want it." An assistant change that also
    edits its instructions or LLM APIs lists that edit as a change of its own.

- **P12.3 Why it's safe.** Code states why an improvement is safe, from its tests and its changes: never a model,
  and never from the editor's words. (DESIGN.md; fixture-test/SPEC.md; Kai, 2026-10-24)
  - **P12.3.1** It says plainly what Assist gains, gives counts per change kind, and says that only settings change
    and nothing runs when you accept, e.g. "Only settings change: 1 instruction change. Nothing runs when you
    accept." An automation saved or turned on makes it "Changes: …. The automation runs when it's triggered.", its
    save included (what that sets off is in its week, P6.13). Enabling an entity adds "; enabling {entity} reloads its
    integration". (fixture-test/SPEC.md; TEXT-REVIEW.md, 2026-10-25)
  - **P12.3.2** It claims nothing when nothing changes, and doesn't claim a change that's already in place.
    (fixture-test/SPEC.md)
  - **P12.3.3** Every line in it answers why it's safe: no "Assist will be able to control: {names}."
    (Kai, 2026-10-25: "I don't see how this answers 'Why it's safe'. Please drop it.")
  - **P12.3.4** No disclaimers or scaremongering in it. (Kai, 2026-10-24: "I don't want all those disclaimer
    boilerplates and scaremongering text to creep back into our product again")

- **P12.4 Decisions.** Your review offers three decisions, and only these: Accept changes; Ask for a revision, with
  a note on what should change; or Decline, with an optional note. (Kai, 2026-10-25; SPEC-CORE.md S6, 2026-10-25;
  DESIGN.md)
  - **P12.4.1** The control is one split button defaulting to "Accept changes", with a dropdown arrow to change the
    decision. Decline and Ask for a revision are never separate buttons. A decision open on its own (Undo changes,
    Send it back to resolve) is a button with no arrow. (Kai, 2026-10-25: "S8: No, why would we?")
  - **P12.4.2** The dropdown closes on a click outside it. (fixture-test/SPEC.md; Kai, 2026-10-24)
  - **P12.4.3** A note box appears only for a decision that takes a note; Accept never sends one. A note survives
    switching between decisions that take one. (Kai, 2026-10-25; fixture-test/SPEC.md)
  - **P12.4.4** Ask for a revision needs a note on what to change. Notes are trimmed. (PRODUCT-SPEC.md, 2026-10-25;
    fixture-test/SPEC.md)
  - **P12.4.5** Decline's note is optional, with the placeholder "Why not? (optional)". Above the note is a "Not an
    issue" box to tick, with the hint "It went fine, or you were only trying Assist out."; when it's ticked, the note
    asks only "Anything to add? (optional)". (TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md)
  - **P12.4.6** A reason (Not an issue) goes only with Decline; no other decision sends one. (SPEC-CORE.md S6,
    2026-10-25; fixture-test/SPEC.md)
  - **P12.4.7** The decision bar fits the footer on a small phone and reads clearly in light and dark themes.
    (fixture-test/SPEC.md, 2026-10-25)
  - **P12.4.8** The decisions open follow the improvement's status: an applied one can be undone; an undone one is up
    for review again; a closed one gets none, and has no decision bar. (fixture-test/SPEC.md)
  - **P12.4.9** Each decision is recorded with the exact version of the improvement it was made on, its time, who
    made it, and its note. (MILESTONE-7.md, 2026-10-22; SPEC-CORE.md §3, 2026-10-26; fixture-test/SPEC.md)
  - **P12.4.10** Each decision shows in the history. A decline as not an issue says "Declined: not an issue". (DESIGN.md; fixture-test/SPEC.md; TEXT-REVIEW.md, 2026-10-25)
  - **P12.4.11** Decline and Ask for a revision change nothing in Home Assistant, and close the improvement.
    (fixture-test/SPEC.md)
  - **P12.4.12** Decline declines the improvement and archives its conversation; nothing more is done to keep it from
    coming back. After Decline: "Thanks." (TEXT-REVIEW.md, 2026-10-25; Kai, 2026-10-26: "decline just declines the
    improvement and archives the conversation. don't bother with adding convoluted logic to prevent it from coming
    back."; "yes")
  - **P12.4.13** "Not an issue" is a reason kept with the decline (it went fine, or it was a try-out); the decline does
    the same either way (P12.4.12). (PRODUCT-SPEC.md, 2026-10-25; Kai, 2026-10-26)
  - **P12.4.14** Decisions made with the earlier menu still hold: a conversation marked as a test stays out of the
    pipeline, and the history still names them ("Marked as a test", "Said it needs improvement").
    (fixture-test/SPEC.md; TEXT-REVIEW.md, 2026-10-25)
  - **P12.4.15** Accept changes is the default and ready: from the notification, the household reads each headline,
    opens what they want to check, and accepts with one click, until they're caught up. (DESIGN.md)
  - **P12.4.16** After a decision, the footer says what happened and offers the way on where the decision's button
    was: the next review waiting, else back to the list ("All caught up", unless this one still waits). Never another
    decision there. Anything else it offers is a link beside what happened: Undo after an accept, Decide again once
    undone (the decision bar again), Send it back to resolve once an accept met a conflict (P12.6.2). A decision that
    couldn't be made and is still open keeps its bar, with why above it and its note as typed. While a decision is on
    its way, its button keeps its words and size. (fixture-test/SPEC.md; DESIGN.md; Kai, 2026-10-30: "Very weird after
    i've sent a diff for revision that the button that pops underneath is 'accept changes', which will probably also
    go through with one accidental click")
  - **P12.4.17** A decision that changed nothing says so, with a warning mark; one that couldn't be sent says why. After
    an accept met a conflict, why is said once, under "Can't apply it now:" above: the footer says "Nothing was
    changed." (fixture-test/SPEC.md)
  - **P12.4.18** Reviews waiting stay available; there's no "Later" decision, because review never blocks normal use.
    (PRODUCT-SPEC.md, 2026-10-21)
  - **P12.4.19** Improvements, decisions, their outcomes and the undo record survive a restart; undo is still one
    click after restarting. (fixture-test/SPEC.md)
  - **P12.4.20** Two decisions on one improvement at the same time never lose its undo record. (Kai, 2026-10-25:
    "The rest: sounds good"; fixture-test/SPEC.md)
  - **P12.4.21** Two improvements accepted at once never interleave: the second says it's busy, applies nothing,
    stays up for review, and can be accepted afterwards. (fixture-test/SPEC.md)
  - **P12.4.22** A decision moves the improvement's conversation: back to Propose for a new round after a revision, or
    a conflict sent back (P2.6); otherwise on to Your household (Improved, or Declined and archived). (P4.1;
    Kai, 2026-10-26)
  - **P12.4.23** Your review hands on a decision with its time: accept (applied, or a conflict to send back),
    revise (back to Propose), decline, or undo. The contract is typed and tested (P3.2). (SPEC-CORE.md §3,
    2026-10-26)

- **P12.5 Accept.** Accept installs the change at once, for future use. Nothing runs: no device action, no script
  or scene run, no AI call; an automation it saves or turns on runs when it's triggered, its save included.
  (PRODUCT-SPEC.md, 2026-10-25; MILESTONE-7.md; fixture-test/SPEC.md)
  - **P12.5.1** After Accept: "Applied. Assist uses it from your next request." With a script among the changes:
    "…; no script was run." With an automation saved or turned on: "Applied. The automation runs when it's
    triggered." (TEXT-REVIEW.md, 2026-10-25)
  - **P12.5.2** Accepted changes are Home Assistant's own configuration, which the assistant already reads (its
    instructions, scripts, names, exposure, scenes, automations), so they keep working after the improver is
    uninstalled. (FEASIBILITY.md; ROADMAP.md; DESIGN.md; Kai, 2026-11-04)
  - **P12.5.3** Each change kind is written through Home Assistant's own way of configuring it, never by editing
    Home Assistant's storage or a configuration file by hand: scripts, automations and scenes are saved by the panel,
    as the signed-in admin, through Home Assistant's own editors (each waited for until Home Assistant has loaded it,
    and read back); an automation turned on or off through automation.turn_on and turn_off; registry entries through
    Home Assistant's registry updates; an assistant's instructions and LLM APIs through its config subentry, whose
    integration reloads, and a change after which it doesn't load again is undone, the integration reloaded with its
    old value. (MILESTONE-7.md; FEASIBILITY.md; Kai, 2026-10-24)
  - **P12.5.4** Accepting is the only way the improver writes what it learned into Home Assistant's
    configuration: there's no apply path outside the undoable changeset. (Kai, 2026-10-25: "let's clean up
    those you deem unnecessary"; Kai, 2026-11-04)
  - **P12.5.5** A changeset applies all or nothing. (DESIGN.md; MILESTONE-7.md; Kai, 2026-10-24)
  - **P12.5.6** Before anything is written, each change gets a three-way check: the value the replays were tested
    against, the proposed value, and Home Assistant's value now. Independent edits merge. (PRODUCT-SPEC.md,
    2026-10-21; MILESTONE-7.md; Kai, 2026-10-24)
  - **P12.5.7** Home Assistant validates scripts and instructions first, with its own checks; any failure applies
    nothing. (DESIGN.md; MILESTONE-7.md)
  - **P12.5.8** A change already in place is a no-op: it shows as already in place, isn't added twice, isn't claimed
    in Why it's safe, and is never removed by undo. (PRODUCT-SPEC.md, 2026-10-21; ROADMAP.md; fixture-test/SPEC.md)
  - **P12.5.9** Changes go in a safe order: names, registry entries and fewer LLM APIs; then scripts and scenes,
    inert until run; then exposure (a new item's lands on its registry entry); then household lines and instructions,
    since they tell the assistant to use the rest; automations last, live once saved. Undo is the reverse,
    automations first: an edited item saved back, a new one deleted. (DESIGN.md; MILESTONE-7.md)
  - **P12.5.10** Before each write, a journal saves the step and the value it replaces. (DESIGN.md; MILESTONE-7.md)
  - **P12.5.11** If a step fails, the steps already done are undone in reverse order: nothing is applied, the
    improvement stays up for review, and the panel says nothing was changed and to try again. (MILESTONE-7.md;
    DESIGN.md; fixture-test/SPEC.md)
  - **P12.5.12** Every written value is read back before the improvement counts as applied. If one didn't take its
    new value, the whole change is undone, that one too. (DESIGN.md; MILESTONE-7.md; fixture-test/SPEC.md)
  - **P12.5.13** The actual status is always reported. A partial apply shows as partial, never as complete. An apply
    or undo that can't be reversed "needs attention": it lists exactly what is in place and what isn't, and waits on
    the household. (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-7.md; fixture-test/SPEC.md)
  - **P12.5.14** Every change kind applies in the safe order, is verified, and undoes back to exactly what was there.
    (fixture-test/SPEC.md)
  - **P12.5.15** A changeset is never left partly applied, and recovery from a partial apply can be tested.
    (Kai, 2026-10-24: "let's make sure it works. ideally we'd have a way to test the recovery from partial
    applications")
  - **P12.5.16** The improver can create, modify, consolidate or retire configuration, its own earlier changes
    included. It inspects the current state, manual edits included, before changing anything. (PRODUCT-SPEC.md,
    2026-10-21; ROADMAP.md)
  - **P12.5.17** Earlier versions may inform a fix going forward, but perfect restoration after arbitrary edits isn't
    promised, and no restoration framework is built. (PRODUCT-SPEC.md, 2026-10-21)
  - **P12.5.18** An improvement can't apply, and its review says why under "Can't apply it now:", when: a script,
    automation or scene is outside the file its editor keeps (a new one too, when Home Assistant now runs one with its
    id from elsewhere); a new script's or scene's entity ID is taken (a state, or another item's registry entry, a
    disabled one included); an edited automation's initial_state would turn it on or off as it's saved (its undo
    couldn't turn it back: Home Assistant restores its last state; on alone turns it on or off); a device or assistant
    it changes is gone; the assistant doesn't use Assist; the line it replaces is gone or has changed; the assistant's
    instructions were changed in Home Assistant since the test; or Home Assistant can't read the edited instructions.
    Each is a conflict with a reason, and nothing is written, not even the changes that were fine.
    (fixture-test/SPEC.md; TEXT-REVIEW.md, 2026-10-25)

- **P12.6 Conflicts.** A value changed since the improvement was tested is a conflict. It shows on the review before
  accepting; on accept it's reported instead of overwritten, and nothing is written. (DESIGN.md; MILESTONE-7.md;
  fixture-test/SPEC.md; Kai, 2026-10-24)
  - **P12.6.1** A conflict says it can't apply now, and why. (fixture-test/SPEC.md)
  - **P12.6.2** An accept that met a conflict asks the household first; agreed, it goes straight back in for a new
    round at Propose (P5.12), with the sign "you accepted, but it conflicted" and what conflicted. The proposer may
    revise, consolidate, or conclude nothing is needed. (PRODUCT-SPEC.md, 2026-10-21; Kai, 2026-10-26: "it will then
    either come up with nothing or come up with something that works without conflict, waiting for human review again")
  - **P12.6.3** It goes straight back in at Propose, on a run of its own (P5.12). (SPEC-CORE.md C4, 2026-10-26; Kai,
    2026-10-26)
  - **P12.6.5** A fix shown passing again, judged on Home Assistant as it is now, goes back up for review once, and
    can then apply. (SPEC-CORE.md C4, 2026-10-26)
  - **P12.6.7** Wherever runs say what waits, an accept that met a conflict is named with the revisions.
    (SPEC-CORE.md R6, 2026-10-26)
  - **P12.6.8** There's no elaborate conflict-recovery system: a conflict is input to the next proposal, nothing
    more. (PRODUCT-SPEC.md, 2026-10-21)

- **P12.7 Undo.** An applied improvement can be undone from its page, in one click. (DESIGN.md; MILESTONE-7.md)
  - **P12.7.1** Undo uses the same three-way check: it puts back what was there, and leaves alone, and reports, any
    value the household has changed since. (MILESTONE-7.md)
  - **P12.7.2** Undo is all or nothing: one that fails midway leaves the improvement fully applied.
    (fixture-test/SPEC.md)
  - **P12.7.3** Undo takes out only its own improvement's changes: undoing an earlier household line after a later
    one was accepted takes out only its own line, and doesn't leave it live, mark it unapplied, or say the household
    changed it. (FINDINGS.md #11)
  - **P12.7.4** An undone improvement goes back up for review (accept again, revise or decline). Its conversation
    stands at Your review again, not Declined. (Kai, 2026-10-26:
    "Just revert an undone fix back to its pending review? then the user can decide whether to decline it for good
    or to send it back.")
  - **P12.7.5** After Undo, a "Decide again" link beside what happened brings back the decision bar, Accept changes
    ready (P12.4.16). (TEXT-REVIEW.md, 2026-10-25; Kai, 2026-10-30)

- **P12.8 Revisions.** Ask for a revision sends the improvement's conversation straight back in for a new round,
  admitted at Propose right away (P5.12), and its run proposes a revised version. After it: "Thanks. Your next run
  proposes a revised version." (TEXT-REVIEW.md, 2026-10-25; DESIGN.md; Kai, 2026-10-26: "a revision sends that
  conversation immediately back in for revision, it does not wait")
  - **P12.8.1** The revision note is a turn of the improvement's conversation (P2.2). It's kept with the improvement it
    revises and passed on to the next proposal. The round's first act at Propose is to analyse the conversation
    afresh, with the note as its last turn (after the note it was sent in with, and any earlier revision note), as a
    note sent in again is (P8.8.2): the analyzer gives it its goals, a step or a conduct goal under the root the
    improvement is for, and that analysis is the round's. A note only about the improvement itself ("use a script,
    not a household line"), which reacts to the proposal rather than to anything Assist did, maps to that root as a
    correction does (P2.2.1) and adds no goal: the analyzer lists it apart (about the fix), and it's kept in full for
    the proposer and reviewer (P12.8.2, P12.8.3). (PRODUCT-SPEC.md, 2026-10-25; fixture-test/SPEC.md; Kai,
    2026-10-26)
  - **P12.8.2** The proposer gets a revision in full: the note, and the improvement it revises (its idea, steps and
    goals). (SPEC-CORE.md S3, 2026-10-26)
  - **P12.8.3** A revised proposal uses the earlier feedback: revision requests and declines feed the next proposal.
    (ROADMAP.md)
  - **P12.8.4** The revised version carries a "Revision" tag, and links to the note it answers; one without a note
    names only what it revises. (TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md; DESIGN.md)
  - **P12.8.5** The improvement it replaces shows "Replaced by a revision" and is closed; only the new version waits
    for review. (TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md)
  - **P12.8.6** While its revision's round runs, the version it revises reads "Being revised", with "Being revised
    for your note “{note}”" under its headline. Where the decision's button would be is the way on; accepting or
    declining this version, the household's only way out meanwhile (P0.11), stays one step away behind a quiet "Decide
    on this version" link beside it. The round's end brings the new version, or this one again (P12.8.5). It waits on
    the improver, not the household: on the home page it's listed after the improvements to review, with its chip,
    and not counted among them; the notification and the sidebar's count leave it out until its round ends (P12.10).
    A round held on the way (P0.8) is still that round, until a run releases it or its conversation is deleted. (Kai,
    2026-10-30: "users don't have to pause and be like, 'wait a minute'")

- **P12.10 The notification.** One summary notification lists what waits on the household: its questions, then the
  improvements (written up, or needing attention; not one being revised, P12.8.6). (PRODUCT-SPEC.md, 2026-10-21; MILESTONE-7.md;
  fixture-test/SPEC.md)
  - **P12.10.1** Its words: "{n} improvements to review", headlines linking to reviews, then "Review these
    improvements in [Assist Improver]." Questions first, linking home: "{n} questions for you, {n} improvements to
    review". Singular for one. (Kai, 2026-10-24; fixture-test/SPEC.md)
  - **P12.10.2** It updates whenever that list changes, and clears once nothing waits. (MILESTONE-7.md;
    fixture-test/SPEC.md)
  - **P12.10.4** There's no routine "nothing found" notification. (PRODUCT-SPEC.md, 2026-10-21)
  - **P12.10.5** Assist Improver's row in Home Assistant's sidebar shows a count bubble while anything waits for
    you. (Kai, 2026-10-24)

- **P12.11 Interrupted changes.** An unfinished journal at startup means an apply was cut off. Once Home Assistant
  has started (not before), the improver undoes the steps done, newest first; at a script, automation or scene, the
  rest waits for the panel, which undoes it when next opened, and each automation the apply saved is turned off
  meanwhile (an edited one on again once put back): as the improver loads, before Home Assistant arms any trigger,
  and as soon as a panel goes away mid-apply too; one Home Assistant loads later (a save that lands after its panel
  went), as soon as it does. It happens once, and a notice says so. (MILESTONE-7.md; DESIGN.md; fixture-test/SPEC.md; Kai, 2026-10-25; Kai, 2026-11-04: "so long as they're all technically feasible, and they don't break our product, then we will do it, and you have my permission to get them resolved")
  - **P12.11.1** An apply undone that way leaves the improvement up for review. (fixture-test/SPEC.md)
  - **P12.11.2** An undo cut off by a restart leaves the improvement fully in place, and the notice says it's still
    in place, never that nothing of it is. (FINDINGS.md #15; fixture-test/SPEC.md)

## P13. Failures

- **P13.1** The pipeline as a whole, and each stage, handle failures gracefully. (Kai, 2026-10-26)
- **P13.2 Contained.** One package's failure stays with that package, and a stage's with that stage: the package is
  left out of the stage's work and noted under History's Details, the stage carries on with the rest, and stages that
  don't need it go on. (SPEC-CORE F1; Kai, 2026-10-26: "generalize the resiliency improvements beyond just that
  single stage")
  - **P13.2.1** When Propose fails on a conversation, its round keeps the reviews and tests it already has; a failed
    proposal never drops them. (SPEC-CORE R1: Kai 2026-10-26)
  - **P13.2.2** A record that breaks its stage's contract (P3.2) is held on its own, with a clear reason; it hands
    nothing on and doesn't stop the stage. (Kai, 2026-10-26: "stage contracts: yes"; Kai's words,
    2026-10-26)
- **P13.3 Never silent.** Every AI step's record closes, whatever the error, and the failure is kept with why. A failed
  proposal is named as a stage that couldn't finish, with why, never a silent 'Run finished'. (SPEC-CORE F1; Kai,
  2026-10-26)
  - **P13.3.1** The developer view and the run log say why a test was skipped or couldn't judge: no friction to fix,
    no fix found, the conversation's counterfactuals are used, or replies that couldn't be checked against the
    household's feedback. (TEXT-REVIEW, 2026-10-25)
  - **P13.3.2** Anything a replay doesn't support stops it as unsupported; it never passes. (INSTALL.md)
- **P13.4 Retries.**
  - **P13.4.1** An operation is retried only when another attempt plausibly helps, usually a fault of the machinery (a
    provider or transient fault, a cut-off replay, a check model that couldn't answer, a crashed test), up to three
    attempts in the run, then the package is held, with why (P4.3, P4.4.2). Deterministic errors, and Assist behaviour
    that doesn't reproduce (but for a baseline's tries, P6.6.5), get no automatic retry. (PRODUCT-SPEC; ROADMAP;
    SPEC-CORE S5; Kai, 2026-11-05)
  - **P13.4.2** A fault of the machinery never counts against the round's counterfactuals (P6.3) and is never reused as
    a result. A baseline the provider failed is retried, not frozen. (SPEC-CORE S5: Kai 2026-10-23)
  - **P13.4.3** Analysis failures count once each, since the conversation's last good analysis. After three, the
    package is held at Analyze, with why, and no run pays for it again until it's released (P4.3). (SPEC-CORE F2,
    rewritten by P4.3: Kai 2026-10-26)
  - **P13.4.4** An analysis answer given before the model erred stands: it's kept or retried, never lost, and the run
    never says it analysed a conversation whose analysis it lost. (SPEC-CORE F2; FINDINGS #54)
  - **P13.4.5** Within an attempt, Home Assistant's own limits apply (ten tool rounds per user turn, and its SDK's two
    transport retries); the improver adds no retry loop of its own there, and no money cap. (INSTALL.md)
- **P13.5 Before a run.**
  - **P13.5.1** A stage whose model is missing, unavailable or a poor fit shows ⚠ on its model and a Repairs item.
    It's rechecked when settings or entities change, and clears when the model comes back, with no restart.
    (DESIGN.md: "A step never fails silently"; Kai, 2026-10-25: "All approved"; fixture-test SPEC)
  - **P13.5.2** A run that needs a stage whose model can't run is refused before anything is spent, naming the stage,
    instead of failing midway. Run one step, and blocked stages, say so too. (fixture-test SPEC; DESIGN.md; Kai,
    2026-10-25)
  - **P13.5.3** A missing quick-check model never blocks a run: conversations pass Triage straight through. A blocked
    stage's missing model doesn't hold up a run, and running one stage that doesn't need the missing model still
    works. (fixture-test SPEC)
- **P13.6 Interruptions.**
  - **P13.6.1** A paid attempt is saved before its call. A restart or reload that cuts off an unfinished attempt marks
    it interrupted and keeps its evidence; it's never rerun automatically, never rebuilds missing history, and never
    silently repeats inference. (HAND-SEEDED-CANDIDATE; MILESTONE-2; INSTALL.md)
  - **P13.6.2** A run cancelled or restarted while a baseline replays loses nothing; the conversation is tested in a
    later run. (SPEC-CORE R8: Kai 2026-10-25)
  - **P13.6.3** A test can't go ahead once what it was prepared from changes: changed feedback stops it, and a test
    refuses if the household's scripts changed since it was prepared. (INSTALL.md)
- **P13.7 Telling the household.**
  - **P13.7.1** The home page shows a notice about the last run only when the household must act (a stage whose model
    can't run, with 'Open settings'). A run that went fine, or whose problems need nothing from the household, shows
    nothing there. Settings › History says what happened, with technical details folded under Details. (Kai,
    2026-10-25: "drop it from home when there's nothing for me to do; keep the details in Settings → History, and keep
    a notice only when I need to act")
  - **P13.7.2** An error shown to the household says how to fix it. (Kai, 2026-10-25: "How does the user (me) fix
    it?")
  - **P13.7.3** When all of a stage's items failed, History says '{Step}: Nothing done: its 1 item failed' or
    '{Step}: Nothing done: all {n} of its items failed'. (Kai's words, 2026-10-26)
  - **P13.7.4** A delete queued during a run (P5.1.3) that then fails raises a Home Assistant notification, titled
    'Assist Improver': 'A conversation couldn't be deleted. Try deleting it again.' The conversation goes back where
    it was: in the list, or among the archived if it was settled. Only a failed delete is reported. (TEXT-REVIEW,
    2026-10-25; Kai, 2026-10-25: "only notify the user if there really ends up being an error deleting"; Kai's words,
    2026-10-26)
- **P13.8 Applying and undoing.**
  - **P13.8.1** Applying is all or nothing. If any step fails, the steps already done are undone in reverse order,
    nothing is applied, the improvement stays up for review, and the panel says nothing was changed and to try again.
    (DESIGN.md; MILESTONE-7; fixture-test SPEC)
  - **P13.8.2** If Home Assistant stops midway through applying, the improver recovers once Home Assistant has started
    (not before; a script, when the panel is next opened), and only once: it undoes the steps done and tells the
    household. Undone, the improvement stays up for review. (DESIGN.md: "the next start undoes them and tells you"; MILESTONE-7; fixture-test SPEC; Kai, 2026-11-04: "so long as they're all technically feasible, and they don't break our product, then we will do it, and you have my permission to get them resolved")
  - **P13.8.3** Undo is all or nothing too: an undo that fails midway leaves the improvement fully applied. After a
    restart cuts off an undo, the notice says the improvement is still in place. (fixture-test SPEC; FINDINGS)
  - **P13.8.4** Every changed value is read back before the improvement shows as applied. A partial apply is shown as
    partial, never as complete. An apply or undo that can't be reversed needs attention, lists exactly what is in
    place and what isn't, and waits on the household. (PRODUCT-SPEC; MILESTONE-7; fixture-test SPEC)
- **P13.9 Saved feedback.** Feedback the household gives is kept with its earlier revisions: a stale submission is
  refused, a repeated import can't overwrite it, and a failed write leaves the earlier revision intact. (MILESTONE-2)

## P14. Screens, statuses and counts

- **P14.1 One source.** A conversation's status, each stage's count in Settings, the Run bar, run confirmations,
  progress lines and History all read the conversation's one canonical record (P2.1). No screen works them out
  another way, so they always agree. (Kai: 2026-10-25, "why is this conversation 'looking for a fix', but in the
  settings panel, i don't see a (1) bubble on the propose stage?"; 2026-10-26)
  - **P14.1.1** Every count a button, confirmation or screen shows is the number of conversations that run will move,
    by the rule the run uses (P5). For example, 'Proposing fixes for {n} conversations' counts only the conversations
    the run admitted. (SPEC-CORE.md R6: Kai 2026-10-25; 2026-10-26)
  - **P14.1.2** Every stage counts conversations, the packages (P4.1). A stage's count in Settings is the number of
    conversations whose chip names that stage. Propose's count is the conversations showing Looking for a fix, also
    after a revision is asked. (SPEC-CORE.md R6, FINDINGS #4: Kai 2026-10-25)
  - **P14.1.3** A picked run's confirmation promises nothing a picked run won't do: no revisions asked for.
    (SPEC-CORE.md R6: Kai 2026-10-26)
  - **P14.1.4** A revision, or an accept that met a conflict, runs on its own right away (P5.12). One left waiting
    (Propose blocked, or its run cut off and held) is named in a manual full run's confirmation with the revisions.
    (SPEC-CORE.md R6, C4; Kai, 2026-10-26)
  - **P14.1.6** Archiving a conversation updates the Run count and its confirmation at once. (SPEC-CORE.md R6: Kai
    2026-10-26)
  - **P14.1.7** An archived conversation counts at no stage but Your household, and on no Run button. (Kai:
    2026-10-26, "archived conversations are not meant to be acted upon")
  - **P14.1.8** A conversation deleted during a run leaves every count at once, along with the view. (SPEC-CORE.md
    C7: Kai 2026-10-25)

- **P14.2 At a glance.** Every screen shows only what helps the household notice, decide or act at a glance. Detail
  goes where they ask for it: a confirmation, a tooltip, an opened row. No blocks of text. (Kai: 2026-10-25, "putting
  blocks of texts that nobody is interested in reading only serves to frustrate")
  - **P14.2.1** One clear control that says what it does ('Run on 7 conversations') beats a control plus text
    explaining it.
  - **P14.2.2** No line lists everything waiting (such as '5 ready for the next run · 1 revision you asked for ·
    2 conversations to triage · 1 write-up'). The Run bar has no line of what waits at each stage. (Kai: 2026-10-25
    20:35, "it takes up mental bandwidth that achieves nothing but frustrate the user")
  - **P14.2.3** The product needs almost no explanation. Each screen leads to one decision and has the recommended
    answer ready: simple, optimised by default, one thing at a time. (DESIGN.md; ROADMAP.md: 2026-10-23)
  - **P14.2.4** The product tells the household when it needs them: a notification, a count beside it in the sidebar,
    and Repairs for a setup gap. Every message links straight to the place to act. (DESIGN.md)
  - **P14.2.5** The home page shows a notice only when the household must act, such as a stage whose model can't run,
    with Open settings. With nothing to do (such as work a cancelled or cut-off run left, held for the next manual run,
    P4.3) there's no notice, and Settings › History keeps the details. (Kai: 2026-10-25)
  - **P14.2.6** Every panel screen works in four views: phone (402px wide) and desktop, each in dark and
    light. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.2.7** Run confirmations open as a modal, so the page doesn't move. (Kai: 2026-10-24)
  - **P14.2.8** Nothing moves under the household's finger. What a tap reveals on the page (a section opened from its
    summary, an opened conversation, a menu, a bubble, a confirmation, what a decision did) fades in where it will
    stay; nothing slides or grows, and nothing fades again as it moves. A page arriving, or the panel Home Assistant
    makes again, shows at once. With reduced motion, nothing fades. A footer (the Run bar, the decision bar) sits at
    the bottom of the screen however short the page, so it never drops away as the page grows. What changes above the
    screen on its own (a run starting or ending, what it found) leaves what's on the screen where it is; at the very
    top of the page, what's new shows as it comes. (Kai, 2026-10-30: "Things shouldn't move randomly and jitter or fly about when user
    interacts with our product. Things should be smooth.")

- **P14.3 Statuses.** Each conversation's chip names the stage it's at (P3.1), not a vague 'With the improver'. Each
  place a conversation can be has a chip of its own. (Kai: 2026-10-25, "labeling the specific stage where the
  conversation is at is even more preferable"; SPEC-CORE.md C1)
  - **P14.3.1** Still going: the conversation is still being recorded. Sent in, if it was filed. (SPEC-CORE.md §1)
  - **P14.3.2** Sent in: it was filed, from Assist or the panel, and hasn't reached Analyze yet. (SPEC-CORE.md §1;
    TEXT-REVIEW.md: 2026-10-25)
  - **P14.3.3** New: closed and readable, at Your Assist or waiting at Triage, with a blue outline as Sent in. It can
    still be sent in. (TEXT-REVIEW.md: Kai asked 2026-10-25; SPEC-CORE.md §1)
  - **P14.3.4** To analyze: at Analyze, past the quick check, or sent in after Looked smooth. (TEXT-REVIEW.md:
    2026-10-25; SPEC-CORE.md §1)
  - **P14.3.5** Looking for a fix: at Propose, before its round's first test (new to it, a revision, or an accept that
    met a conflict). (TEXT-REVIEW.md "No fix found": Kai approved 2026-10-25; SPEC-CORE.md §1; Kai, 2026-10-26)
  - **P14.3.6** No fix yet: at Propose after a test in its round found no fix, with counterfactuals left (P6.3).
    Tooltip: 'The next proposal looks for another fix.' (Kai: 2026-10-26, approving "No fix yet · The next
    proposal looks for another fix")
  - **P14.3.8** At Write up, with its fix waiting for its write-up, a conversation has a chip of its own. It's never
    'Up for review', and it stays in the list. (SPEC-CORE.md §1; FINDINGS #3, #29)
  - **P14.3.9** Up for review: while its written-up improvement waits at Your review. The chip opens that review.
    (TEXT-REVIEW.md: 2026-10-25; SPEC-CORE.md §1; FINDINGS #3)
  - **P14.3.10** Looked smooth: Triage's sure smooth verdict. Tooltip: 'Nothing to improve, at a quick look. Send it
    in for a full analysis.' (Kai: 2026-10-25, "Option A"; words approved 20:56)
  - **P14.3.11** No trouble found: analysed, nothing to fix. Tooltip: 'Nothing went wrong, as far as it could tell.
    Send it in again with a note if something did.' (Kai: approved 2026-10-26)
  - **P14.3.12** Improved: its improvement was accepted. (TEXT-REVIEW.md: 2026-10-25; Kai, 2026-10-26)
  - **P14.3.13** Declined: its improvement was declined, and it's archived (P12.4.12). (TEXT-REVIEW.md: 2026-10-25; Kai,
    2026-10-26)
  - **P14.3.14** No fix found: none of its goals with trouble has a fix, because the conversation's counterfactuals
    are used up or the proposer said why it's outside the assistant's control. Tooltip: 'No fix worked. Send it in
    again with a note for one more look.' (TEXT-REVIEW.md "No fix found": Kai approved 2026-10-25)
  - **P14.3.15** Marked as a test: the household said it was a test. Decisions made with the earlier menu still hold.
    (TEXT-REVIEW.md: 2026-10-25; fixture-test/SPEC.md)
  - **P14.3.16** Couldn't be read: no stage can read it. One that couldn't be read after being sent in again shows
    'Couldn't be read', never 'Sent in'. (TEXT-REVIEW.md: 2026-10-25; FINDINGS #30, #62)
  - **P14.3.17** A conversation waiting for the household's answer (P9.9.6) is at Your review, its question at the
    top of the home page; it's never shown as No trouble found. (SPEC-CORE.md C6: Kai 2026-10-26; Kai,
    2026-10-26)
  - **P14.3.18** A conversation stands at one stage (P4.1). (Kai, 2026-10-26: "conversation travels")
  - **P14.3.19** A conversation archived by hand, where a stage would otherwise work on it, is out of the pipeline. It
    never shows that stage's chip, so it's never 'Looking for a fix' while Propose leaves it out. (SPEC-CORE.md C5:
    Kai 2026-10-26; FINDINGS #79)
  - **P14.3.20** No trouble found, No fix found and Declined are quiet grey (Kai: "less interesting"). New, Sent in,
    Looking for a fix and No fix yet have a blue outline. 'Says more' is amber. (TEXT-REVIEW.md: Kai 2026-10-25
    21:31)
  - **P14.3.21** Every chip fits on one line of a 402px phone row beside long first words. The widest may push the
    row's '· {n} failed steps' onto a second line. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.3.22** An improvement shows as applied only once every changed value has been read back. If an undo fails,
    it shows 'needs attention', with exactly what is in place and what isn't. The screen always shows the real
    status. (MILESTONE-7.md; ROADMAP.md)

- **P14.4 The conversations list.** The home page's 'Your conversations' surfaces only conversations that need the
  household: Still going, New, Sent in, and those being worked on. (Kai: 2026-10-26, "only conversations that
  need me are surfaced"; SPEC-CORE.md C6)
  - **P14.4.1** A conversation that reaches Your household (P3.3), or whose improvement or question waits at Your
    review, leaves the list on its own: it's hidden from the list only, and stays where it stands in the pipeline,
    counted there, and kept for checking other changes against. Its improvement or question waits at the top of the
    home page. (SPEC-CORE.md C6: Kai 2026-10-26; TEXT-REVIEW.md: 2026-10-25; Kai,
    2026-10-26)
  - **P14.4.2** While any conversations are archived, the bottom of the list has a 'Show archived ({n})' checkbox. It
    shows settled conversations together with those archived by hand below it, with their own Select all, dimmed,
    with no word of their own; ticking it moves nothing on the screen. (TEXT-REVIEW.md: Kai 2026-10-25; Kai's words:
    2026-10-26; Kai 2026-10-29)
  - **P14.4.3** Unarchiving keeps a conversation listed for as long as it stands where it did. One up for review
    comes back to the list. (SPEC-CORE.md C6: Kai 2026-10-26; FINDINGS #28)
  - **P14.4.4** With nothing waiting above, every conversation archived, no search or filter on, the list says
    'Nothing needs you right now.' (TEXT-REVIEW.md: Kai approved 2026-10-26)
  - **P14.4.5** Other empty states: with nothing recorded yet, 'Use Assist as usual: your conversations show up here,
    ready to send in.' A search or filter with no match says 'No conversations match.' A conversation with nothing
    said shows '(nothing said)'. The list grows with 'Show more'. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.4.6** A search box finds conversations by what was said, in any case, or by when: 'Search what was said, or
    when', with a 'Clear search' button. When no conversation has every word, the closest show under 'No conversation
    has all those words. The closest:'. (TEXT-REVIEW.md: 2026-10-25; fixture-test/SPEC.md)
  - **P14.4.7** A day filter (All · Today · 7 days · 30 days) and a sort (Newest · Most back-and-forth · Oldest),
    the sort one compact menu, as Status is; each menu's button is as wide as its longest choice, so choosing one moves
    nothing beside it (on a phone the sort takes its room from the search beside it). (TEXT-REVIEW.md: 2026-10-25; Kai,
    2026-10-30)
  - **P14.4.8** Search, the day filter, the sort and Select all stay pinned on top while the list scrolls, as wide as
    the list: on a phone in three rows (the search and the sort, the day filter and Status, Select all), on a desktop
    in two. (Kai: 2026-10-25; Kai, 2026-10-30: "Of course it has to be for both")
  - **P14.4.9** The household can choose conversations by where they stand (every Improved one, say), then Select
    all and Archive. (TEXT-REVIEW.md "The Status filter": Kai 2026-10-25)
  - **P14.4.10** Wherever statuses are listed to choose from, No fix yet and No fix found come after Looking for a fix
    and Declined. (TEXT-REVIEW.md "No fix found": Kai approved 2026-10-25)
  - **P14.4.11** Each row shows its sign of trouble as '{n} failed step(s)'. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.4.12** An opened conversation reads turn by turn as chat bubbles, labelled You (on the right) and Assist (on
    the left). A failed step shows where it happened, between what you said and Assist's reply, as 'Failed step:
    {what} · {on what}', then Home Assistant's own error exactly as it gave it. (TEXT-REVIEW.md: 2026-10-25;
    fixture-test/SPEC.md)
  - **P14.4.13** In an opened conversation, 'Send this one in' looks like the Run buttons, with a send arrow on its
    left; while it's shown, it's the one primary action, and the Run bar's button steps back (quiet, the same size in
    the same place). 'Archive this conversation' (or 'Unarchive') and 'Delete from Assist Improver' sit behind a ⋮ ('More
    actions'), each with an icon. Delete is in the panel's tasteful red and asks first. (Kai: 2026-10-25;
    TEXT-REVIEW.md "The opened conversation's ⋮": Kai 2026-10-25)
  - **P14.4.14** The last run line reads 'Last run {when} · {n} new improvement(s)'. The count is left out once
    nothing is left to review. (TEXT-REVIEW.md: 2026-10-25)

- **P14.5 Selection.** Every row can be selected, toward a zero inbox. (Kai: 2026-10-25; fixture-test/SPEC.md)
  - **P14.5.1** 'Select all {n}' selects every conversation the list shows, with its search and filters applied and
    past Show more. The selection lasts through searches and filter changes. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.5.2** Unticking Select all clears every pick in its list; there's no 'Clear picks'. Select all shows a mixed
    state when only some are picked. (Kai: 2026-10-25)
  - **P14.5.3** Select all leaves out Looked smooth conversations. One joins a run only when ticked by hand or sent in
    on its own. (Kai: 2026-10-25, "Option A"; TEXT-REVIEW.md)
  - **P14.5.4** With a selection, the bottom bar shows only the actions that apply, each with its count: Run (only
    those that can still be sent in), Archive ({n}) for those not archived, Unarchive ({n}) for archived ones, and
    Delete ({n}), which asks first, naming how many; the same whether archived ones are shown or not. (TEXT-REVIEW.md
    "The home page, at a glance": 2026-10-25)
  - **P14.5.5** A delete during a run is queued: the conversation is gone from view at once and deleted when the run
    ends. The household hears of it only if the delete fails. (Kai: 2026-10-25, "behave optimistically";
    TEXT-REVIEW.md)

- **P14.6 Run controls.** Every run control says how many conversations it will run, and which, on every screen that
  offers a run. (Kai: 2026-10-25, "We need to be more specific about what the user can expect to happen,
  for example, how many conversations will be run, and which")
  - **P14.6.1** The home page's Run bar is just its button, with a play icon: 'Run on {n} new conversation(s)', or
    'Run what's waiting' when no conversation would be admitted but other work waits (a revision, say). What a run
    does stage by stage stays in its confirmation. (Kai: 2026-10-25; TEXT-REVIEW.md; DESIGN.md story 3)
  - **P14.6.2** With nothing picked, the home page's Run is Settings' manual full run (P5.2): the same count, the same
    run, the same confirmation. (Kai: 2026-10-25; TEXT-REVIEW.md)
  - **P14.6.3** With some picked, the home page's Run is a picked run (P5.4). Its {n} counts the picked conversations
    and nothing else. Its confirmation names them, and its progress and History say 'picked'. (Kai: 2026-10-25;
    2026-10-26, "only the selected one gets the fast lane in, nothing else moves"; fixture-test/SPEC.md)
  - **P14.6.4** Settings' Run reads 'Run on {n} new conversation(s)', or 'Run what's waiting' when that's what it will
    do. Its confirmation's title: P17.17.4. (Kai: 2026-10-25, '"new" says which'; 2026-10-26)
  - **P14.6.5** A run's {n} leaves out conversations that would stop at a stage that's still blocked. (TEXT-REVIEW.md:
    2026-10-25; fixture-test/SPEC.md)
  - **P14.6.6** In the Run menu, each scheduled-run option has a clock icon and each 'Run one step' row a play icon.
    'Only when you run it' has no clock but stays aligned with the rest. (Kai: 2026-10-24)

- **P14.7 Run confirmations.** A run asks first, in a short confirmation that gently reminds the household runs cost
  money: 'Runs use your AI models, so they cost money.' Not now, close, a click outside or Esc start nothing. (Kai:
  2026-10-24, "the confirmation text is currently too lengthy"; fixture-test/SPEC.md)
  - **P14.7.1** A run with conversations has the title 'Run on {n} new conversation(s)?' and no sentence under it.
    Under the title come the conversations by first words, then an 'Also: …' line only when other work waits, then
    the blocked and model warnings when they matter, then the cost line with its estimate. (TEXT-REVIEW.md: Kai
    approved the layout and "Also:" 2026-10-25; Kai 2026-10-25 for the title)
  - **P14.7.2** The conversations are named '"…", "…", "…" and {n} more', or all of them when there are three or
    fewer. On the home page: the picked ones. In Settings: the new ones, those sent in first, then the newest. Hidden
    ones are never named. Each is up to 40 characters, cut at a word, then '…'. The named ones and the 'and {n} more'
    add up to the title's count. The list is left out when none can be named. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.7.3** The Also line's items, in their own words, joined by ' · ': '{n} conversation(s) to triage · {n}
    conversation(s) to analyze · {n} conversation(s) to propose fixes for · {n} write-up(s)'. (Kai approved, M6)
  - **P14.7.4** Run one step, and a run with no conversations to take, keep one short sentence ('Analyzes 3
    conversations.', '1 revision you asked for is waiting.', 'Runs on 1 revision you asked for.') and no Also line. With
    no new conversation, its title is "Run Assist Improver?" (P17.17.4). (TEXT-REVIEW.md: 2026-10-25; Kai,
    2026-10-26)
  - **P14.7.5** The confirmation leaves blocked stages out and names them. A stage whose model can't run is named
    before anything is spent, and it disables Run; a blocked stage's missing model doesn't hold up the run.
    (fixture-test/SPEC.md; Kai 2026-10-25, "All approved")
  - **P14.7.6** Only if it's easy, the cost line carries a rough token estimate from the household's past runs: a
    short list, each figure naming the model it's spent on, readable at a glance. One line for analysing the run's
    conversations, one for the quick check, merged into one when both run on one model. Without past runs, only the
    cost line shows. No budgeting or accounting. (Kai: 2026-10-25, "make sure we're able to match those
    token estimates with models. otherwise, the estimate becomes unnecessary complexity")

- **P14.8 A run in progress.** It shows where it is with live counts ('Analyzing conversations: {done} of
  {total}.'), marks the stage at work, and can be left and come back to ('You can leave this page; it keeps going.').
  (fixture-test/SPEC.md; TEXT-REVIEW.md: 2026-10-25; DESIGN.md)
  - **P14.8.1** Its title reads 'Improving your assistant' until it has admitted its conversations, then says which
    run it is: 'Running on {n} new conversation(s)' · 'Running on the conversations you picked' · 'Running on what you
    sent in (on demand)' · 'Running {step} only'. (Kai: approved 2026-10-25; TEXT-REVIEW.md)
  - **P14.8.2** Settings' Cancel run tooltip is that title plus its stage, such as 'Running on 14 new conversations ·
    Analyze'. (TEXT-REVIEW.md: 2026-10-25)
  - **P14.8.3** Propose's progress line reads 'Proposing fixes for {n} conversation(s), with a reviewer checking
    each…'. Its {n} counts only the conversations whose goals it's fixing in this run, not every one it reads. (Kai:
    approved 2026-10-25; 2026-10-26)

- **P14.9 Settings' track and counts.** Settings draws the pipeline (P3.1) as a track with every stage: one wide flow
  down the screen on a phone, across on a desktop. Cards are sized to their content, never overlap or get cut off, and
  fit any window width. (fixture-test/SPEC.md; Kai 2026-10-24)
  - **P14.9.1** The track is design C: a small loop between Propose and Review, blended in (not hard-connected, not a
    sankey) and wide enough for its arrows, in a muted light-grey flow in both themes. (Kai: 2026-10-24)
  - **P14.9.2** Stage cards have consistent side and top/bottom margins and even spacing. Block icons are centred and
    don't touch their neighbours. Each stage has a glyph, Your household, the user and Assist especially (P15.3.2).
    (Kai: 2026-10-24)
  - **P14.9.3** Each stage card shows how many conversations are at it (P14.1.2). Conversations held there show as
    held, and why can be seen on demand (P4.3). (DESIGN.md; fixture-test/SPEC.md; Kai 2026-10-24, "gates is the
    play/pause behavior I'm looking for")
  - **P14.9.4** Tooltips show in a bubble on hover, not on click. Menus close on a click outside or Esc, changing
    nothing. Model menus show no 'Assist Improver' tooltips. 'Your review' isn't a link. '0 lines learned' has no
    dropdown arrow. (Kai: 2026-10-24)
  - **P14.9.5** Picking a model saves it at once, and a setting that can't be saved says why. On a phone a menu rises
    from the bottom. (fixture-test/SPEC.md)
  - **P14.9.6** A model menu doesn't offer 'New: …' (adding the recommended AI Task) once the household has added it.
    (Kai: 2026-10-22)
  - **P14.9.7** History sits below the pipeline. (Kai: 2026-10-24)

- **P14.10 History.** History lists finished runs, improvements going up for review, and decisions with who made
  them, in plain words, newest first. A run in progress isn't history yet. Eight rows show, then Show more; a row
  about an improvement opens its review. (DESIGN.md; fixture-test/SPEC.md)
  - **P14.10.2** It names how each run started: 'Run finished · on demand', 'Run finished · picked', 'Run finished · scheduled'. A
    manual full run adds nothing. (Kai: 2026-10-25; TEXT-REVIEW.md)
  - **P14.10.3** It says what happened in each run, with the technical details folded under Details. When all of a
    stage's items failed: '{Step}: Nothing done: its 1 item failed' or '{Step}: Nothing done: all {n} of its items
    failed'. (TEXT-REVIEW.md: Kai 2026-10-25; approved 2026-10-26)
  - **P14.10.4** It counts as analysed only the conversations analysed through, not every attempt. It never says one
    conversation was both analysed and couldn't be analysed. (FINDINGS #7, #81; SPEC-CORE.md R6)

- **P14.11 Home, improvements and notices.** Home shows the one next thing: what waits on the household first
  (improvements to review, and questions, P9.9.6: a count, in the singular or plural, each with what it gets you or what
  it asks); otherwise a run in progress, above the reviews, with no second run button; otherwise improvements waiting
  for their write-up, with Run; otherwise new conversations, with Run; otherwise 'Use Assist as usual'; otherwise All
  caught up. (fixture-test/SPEC.md; DESIGN.md "one thing at a time"; Kai, 2026-10-26: "the package is sitting in the
  human reviewer's queue")
  - **P14.11.1** Home offers nothing about household knowledge, since every assistant on Assist reads it already.
    (fixture-test/SPEC.md; DESIGN.md story 7)
  - **P14.11.2** The panel is the one place to review improvements. Configure and Repairs have no review screens,
    and the notification links into the panel. (Kai: 2026-10-24, "can we remove the ones in other places, and
    link the notification into the panel?")
  - **P14.11.3** Each review has its own address, which the notification links to. Earlier improvements are folded
    away until opened. The gear opens Settings, and back (the browser's too) returns. (fixture-test/SPEC.md;
    DESIGN.md)
  - **P14.11.4** One summary notification lists what waits on the household (P12.10); never one per proposal. (Kai:
    2026-10-24; fixture-test/SPEC.md; MILESTONE-7.md)
  - **P14.11.5** The Assist Improver row in Home Assistant's sidebar shows a count bubble while anything waits for
    you, as Settings and Notifications do. (Kai: 2026-10-24; fixture-test/SPEC.md)
  - **P14.11.6** An update loads the new version's panel; unloading removes the panel. (fixture-test/SPEC.md)
  - **P14.11.7** The panel shows what the README's pictures promise: improvements to review, each with what it gets
    you; a review with what it gets you, how it was tested, and the exact change as a diff; a new script
    as a diff, made visible to Assist; and Settings where conversations flow through every stage, each AI stage
    naming its model. (fixture-test/SPEC.md; public/README.md)

- **P14.12 The review page.** A review reads like a code-review diff viewer. (Kai: 2026-10-24, "Basically the
  visual should follow that of code review diff viewers")
  - **P14.12.1** It leads with its headline, status and what it gets you: '{n} new thing(s) Assist can do · {n} fewer
    turn(s) · {n} unwanted change(s) avoided · follows your feedback', the same line as on the home list. Lines and
    sections with nothing in them are left out. (TEXT-REVIEW.md: Kai approved 2026-10-26; fixture-test/SPEC.md)
  - **P14.12.2** The summary sits open on top, in this order: what you get; its before and after (each test open to
    its conversation as it went, then its replay with the change); why it's safe (stated by code). Below, folded: what went wrong (the cause, and what the sandbox doesn't simulate),
    then what changes. Each status reads in plain words. (fixture-test/SPEC.md; DESIGN.md; Kai, 2026-10-30: "What
    you get (summary of value proposition) still on top, but followed immediately by before & after (open by
    default), then 'why it's safe'"; "Below, lead with our problem diagnosis, collapsed. Then the proposed changes
    (what changes) - same. the rest seems unnecessary")
  - **P14.12.3** What an improvement gets you is counted in the line under its headline (P14.12.1) and shown by its
    before and after; no section repeats it. (Kai, 2026-10-30: "the rest seems unnecessary")
  - **P14.12.4** What the household pointed out that a test doesn't show fixed closes that test's replay with the
    change, under 'Still open', each line their words in quotation marks. (TEXT-REVIEW.md: Kai approved 2026-10-26
    03:05; P17.21.2)
  - **P14.12.5** The changeset has one folded line per change (its name, its type, + and − counts in green and red)
    that opens to its diff. Each change is a file with a line-numbered diff. On a phone the line numbers give way and
    long lines wrap. A change says when it's already in place or only proposed; one without a diff says why. (Kai:
    2026-10-24; fixture-test/SPEC.md; DESIGN.md)
  - **P14.12.6** Each change type label (script, exposure, instructions…) has its own distinct, tasteful colour. (Kai:
    2026-10-24)
  - **P14.12.7** Each conversation under Before & after has a result chip: 'goal now met · {a} → {b} turns · improved ·
    side effect(s) gone · follows your feedback', joined with ' · ', and an amber '{n} still open' chip where
    something the household pointed out isn't shown fixed. On a phone the chips go on their own line under what was
    tested and wrap within the row. (TEXT-REVIEW.md: 2026-10-25; Kai approved 2026-10-26; P17.21.3)
  - **P14.12.8** After a decision, the page stays put: the feedback and the next button appear below, and expanded
    sections stay expanded. (Kai: 2026-10-24)

- **P14.13 Developer view.** For administrators, in developer mode only: find any ID's lifecycle and logs; every
  conversation and what happened to it (where it is, and its record at each stage, P2.1); runs as a timing chart;
  health (disk, memory, what's running, hot spots, AI calls). (Kai: 2026-10-25)
  - **P14.13.1** Its traces are kept for a week at most. (Kai: 2026-10-25, "we could keep its retention at 1
    week max")
  - **P14.13.2** It says why a test was skipped or can't judge, which the household's screens don't show: no friction
    to fix (no extra turns, the goals met, no side effect left, no feedback); no fix found; the conversation's
    counterfactuals are used (P6.3); replies that couldn't be checked against the household's feedback; a baseline
    that didn't reproduce the friction. (TEXT-REVIEW.md "No fix found", "What counts as trouble": Kai 2026-10-25)

- **P14.14 No interventions graph yet.** No headline interventions-over-time metric or graph, and no claim of a
  measured downward trend, until there's a measure that helps. If one comes back, it's normalised by how many
  conversations there were, keeps actual use apart from predicted gains, and is an objective, never a reason to change
  how tests are judged. (ROADMAP.md: 2026-10-24; PRODUCT-SPEC.md)

## P15. Settings and models

- **P15.1 One place.** All the improver's settings live in one settings view, the panel's Settings. No setting sits on
  the device page, in a service's fields or in a per-call flag. A new setting goes there, with a sensible default.
  (Kai, 2026-10-23: "I'd like them centralized for ease of usability")
  - **P15.1.1** The integration's Configure only opens the panel's Settings. (Kai, 2026-10-25: "All approved";
    DESIGN.md)
  - **P15.1.2** Settings draws the whole pipeline (P3.1), and every setting is made there.
    (Kai, 2026-10-24: "In this configuration panel, the whole pipeline/workflow should be visualized")
  - **P15.1.3** On a phone the pipeline runs top to bottom; on a desktop, across. (Kai, 2026-10-24;
    fixture-test/SPEC.md)
  - **P15.1.5** Picking a model saves it at once. A setting that can't be saved says why and changes nothing. Esc or a
    click outside closes a menu without changing anything. On a phone a menu rises from the bottom.
    (fixture-test/SPEC.md)

- **P15.2 Safe defaults.** The safe choices are the defaults, and good results need no settings. (DESIGN.md)
  - **P15.2.1** Counterfactuals per round: 3 by default (P6.3, P15.9.2). (PRODUCT-SPEC.md; fixture-test/SPEC.md; Kai,
    2026-10-26)
  - **P15.2.2** No schedule: runs happen only when the household runs them. (Kai, 2026-10-24: scheduled runs "stay
    disabled by default"; DESIGN.md)
  - **P15.2.3** No stage blocked. (fixture-test/SPEC.md)
  - **P15.2.8** The recommended setup's models (P15.5). (DESIGN.md)

- **P15.3 Settings' stages.** (refines P3.1)
  - **P15.3.1** The track: conversations merge onto the improvement loop; Propose and Review go round their own small
    loop; revisions go round again; what the household accepts peels off to Your household. (DESIGN.md;
    Kai, 2026-10-24)
  - **P15.3.2** Each stage has a glyph and a small tooltip saying what it does. (DESIGN.md; Kai, 2026-10-24)
  - **P15.3.3** Each AI stage names its model; one click opens a menu to pick another AI Task or add one (P15.7).
    (Kai, 2026-10-24: "clicking it opens a dropdown")
  - **P15.3.4** Each stage shows its backlog (what waits there), and can be blocked (P4.3) or run on its own (P5.6).
    (DESIGN.md; Kai, 2026-10-24: "gates is the play/pause behavior I'm looking for")
  - **P15.3.5** Your Assist names the assistant Assist uses by default, with nothing to turn on for it. Switching it
    changes Home Assistant's own default. With no assistant, it says to add one; choosing one that's gone gives not
    found. (Kai, 2026-10-24; TEXT-REVIEW.md, 2026-10-25; fixture-test/SPEC.md)
  - **P15.3.6** Your household opens what the household knowledge holds.
    (TEXT-REVIEW.md, 2026-10-25; DESIGN.md)

- **P15.4 Models through Home Assistant.** The AI stages reach models only through Home Assistant's AI Task entities,
  through HA's public AI Task interface, with structured answers. The improver has no inference client, credential
  store or provider code of its own, and paid calls go at the provider's normal charges.
  (Kai, 2026-10-22: "couldn't we use the anthropic integration installed in my HA?"; INSTALL.md; ROADMAP.md)
  - **P15.4.1** Any provider's AI Task works. Covered for now: Anthropic, OpenAI, Google and OpenRouter.
    (Kai, 2026-10-22: "just cover anthropic, openai, google, and openrouter for now")
  - **P15.4.2** The README's Good to know says: "Tested with Anthropic and OpenRouter. Other AI providers should work
    too." and adds no more caveats. (Kai, 2026-10-25: "avoid adding too much 'batteries not included' boilerplate")
  - **P15.4.3** Stages can run on different models. Each is named as the setup dialog names it.
    (TEXT-REVIEW.md, 2026-10-25)
  - **P15.4.4** A stage's model and effort come from the AI Task it points at. A different model or effort is another
    AI Task entry, never a per-run setting. (Kai, 2026-10-22: "option 1 sounds good"; PRODUCT-SPEC.md)
  - **P15.4.5** A stage with no AI Task of its own runs on the main one. (fixture-test/SPEC.md; README)
  - **P15.4.6** Simulate has no model of its own: replays run on the household's own assistant, as it's set up now,
    with nothing to configure (P6). (Kai, 2026-10-23: "the less we ask users to configure, the better")
  - **P15.4.7** The reply check (P6.1.9) runs on its own small model, set in Settings; switched off, Write up's
    model does it. (Kai, 2026-10-23: "a very small/light model doing this check would also suffice"; Kai, 2026-10-27)
  - **P15.4.8** Triage's model needs no setup outside Home Assistant, such as a local model server: HA-native options
    and cheap remote models (e.g. DeepSeek V4.1 Flash 1017, Haiku 5.5, GPT-6 Luna). (Kai, 2026-10-24: "let's start
    with HA native, and also go with the cheap remote models")
  - **P15.4.9** The improver's AI Tasks keep Home Assistant's "automatic" prompt caching. It's settled.
    (Kai, 2026-10-24: "Yes, I'll keep the automatic caching.")

- **P15.5 Recommended setup.**
  - **P15.5.1** It comes preselected: Claude Opus 5.5 for the judgement stages (Analyze, Propose, Review), and a small
    model (Claude Haiku, or the provider's own small model) for Triage and Write up, so a conversation that needs
    nothing costs little. (Kai, 2026-10-25: "All approved"; DESIGN.md; fixture-test/SPEC.md)
  - **P15.5.2** Effort depends on the stage. Power users can override the model and effort per stage.
    (PRODUCT-SPEC.md; ROADMAP.md)
  - **P15.5.3** Every model menu offers Use recommended setup, in one click, only when the current setup differs from
    it. (Kai, 2026-10-25: "All approved"; DESIGN.md)
  - **P15.5.4** Applying it reuses AI Tasks that fit: those in use first, Claude Haiku before the provider's other
    small model, never an unavailable one. (fixture-test/SPEC.md)
  - **P15.5.5** Applying it makes per-stage overrides on the judgement stages give way, and keeps every other
    setting. (fixture-test/SPEC.md)
  - **P15.5.6** It comes back with a note on anything it couldn't add, and still does the rest. (DESIGN.md)
  - **P15.5.7** It's recommended only when Claude Opus 5.5 is available. On other models everything still works,
    just not as well. (Kai, 2026-10-22: "we can stick to only recommending when claude opus 5.5 is available, in
    other cases it'll still work but not optimal")

- **P15.6 Model menus and fit.**
  - **P15.6.1** Model menus rank models by fit and mark each Recommended, Not recommended (a small model on a
    judgement stage) or Fit unknown, with why. (fixture-test/SPEC.md; Kai, 2026-10-25)
  - **P15.6.2** Fit unknown is a neutral tag, for a model whose provider's settings the improver can't read,
    including one whose provider was removed. (Kai, 2026-10-25: "All approved")
  - **P15.6.3** The quick stages' menus (Triage, Write up) mark small models a good fit and offer pass-through.
    (fixture-test/SPEC.md)
  - **P15.6.4** Settings shows each stage's model, its size, whether the stage is optional, and why it can't run
    (missing, unavailable, none). (fixture-test/SPEC.md)
  - **P15.6.5** When all is well, a stage just names its model: no tooltip about AI Tasks or "Assist Improver".
    (Kai, 2026-10-24; fixture-test/SPEC.md)
  - **P15.6.6** A model that can't use tools is named as the reason its stage failed. (Kai, 2026-10-25: "All
    approved")
  - **P15.6.7** A stage whose model is missing or unavailable, the main model included, shows ⚠ on its model and an
    item in Repairs at once. Both clear when the model comes back, with no restart. (Kai, 2026-10-25: "All
    approved"; DESIGN.md; fixture-test/SPEC.md)

- **P15.7 Adding an AI Task.**
  - **P15.7.1** A new AI Task can be added from a stage's menu, through the provider's own add-AI-Task flow, which is
    closed afterwards. The stage's suggested model is offered, a typed model ID can be added in one click, and the
    new AI Task is used for that stage at once. (DESIGN.md; fixture-test/SPEC.md)
  - **P15.7.2** Adding one needs an AI provider first. A failure says why and changes nothing.
    (fixture-test/SPEC.md)
  - **P15.7.3** A household without Claude Opus 5.5 (e.g. on OpenRouter) can still create an AI Task from the menu, at
    its provider's defaults. (Kai, 2026-10-22)

- **P15.8 First setup.**
  - **P15.8.1** Setup lists the household's AI Task entities and suggests one, with no manual AI Task setup needed.
    (Kai, 2026-10-22: "pick an AI Task entity, warn if it's weak, offer to create a recommended one"; ROADMAP.md)
  - **P15.8.2** It warns when the chosen AI Task is weak for these stages (e.g. Haiku 4.5 at low effort with a
    3,000-token cap). (Kai, 2026-10-22; ROADMAP.md)
  - **P15.8.3** It offers to create a recommended AI Task (a stronger model, with higher effort and output limit).
    (Kai, 2026-10-22; ROADMAP.md)
  - **P15.8.4** It gets the household to a recommended AI Task in a minute or less. (ROADMAP.md)
  - **P15.8.5** Accepting the recommended AI Task selects it, never leaving the old one shown as selected, and says
    they can use their own instead (not recommended). (Kai, 2026-10-22: "we select the recommended task setting for
    them, but we also communicate that if they want (not recommended), they can use their own task")
  - **P15.8.6** A weak choice is confirmed with why, in a line or two, offering "Use it anyway" or "Choose another".
    An accepted weak choice is remembered. (Kai, 2026-10-25: "Plainer 'too weak' and Repairs dialogs: approved";
    fixture-test/SPEC.md)
  - **P15.8.7** First setup gives Triage and Write up a small model: one already available, or a new one only beside
    a newly created AI Task. Setup never stops for lack of one. (fixture-test/SPEC.md; README)
  - **P15.8.8** Until a suitable AI Task is chosen, a Repairs notice shows. Its fix runs in place with setup's steps:
    the recommended model preselected, and "Choose another" going back. (ROADMAP.md; fixture-test/SPEC.md)
  - **P15.8.9** A strong choice in that fix saves at once, keeps the other settings, drops a stale accepted warning,
    and the notice clears live. (fixture-test/SPEC.md)

- **P15.9 Spending.**
  - **P15.9.1** Cost controls are coarse on purpose: no per-call cost accounting, no accounting through Home
    Assistant, and no dollar cap. (Kai, 2026-10-22: "accounting using existing HA infrastructure adds too much
    complexity"; Kai, 2026-10-21; PRODUCT-SPEC.md)
  - **P15.9.2** The one spending knob is the count of counterfactuals per round (P6.3): a household setting, 3 by
    default, never hard-coded. (Kai, 2026-10-23: "This is meant to be user-configurable, a coarse grained knob that the
    user can adjust, ideally in one 'settings' view."; Kai, 2026-10-26)
  - **P15.9.3** It's a bounded stepper, from 1 to 20. (fixture-test/SPEC.md)
  - **P15.9.4** It's set on Settings' Simulate stage. (Kai, 2026-10-24)
  - **P15.9.5** A baseline replay isn't counted in it. (PRODUCT-SPEC.md)
  - **P15.9.6** It's described as a rough multiplier on the household's normal assistant spend: each counterfactual
    replays at most the whole conversation on their own assistant (P6.1), so 3 costs up to about 3 times those
    conversations, plus up to three baseline tries each (P6.6.6). (Kai, 2026-10-24; PRODUCT-SPEC.md; MILESTONE-6.md)
  - **P15.9.7** There's no coverage knob and no percentile-coverage promise. (PRODUCT-SPEC.md)

## P16. Install, updates and distribution

- **P16.1 Identity.**
  - **P16.1.1** The product is named Assist Improver; its integration domain is assist_improver.
    (Kai, 2026-10-22: "Assist Improver is accurate and to the point"; HANDOFF.md)
  - **P16.1.2** It's licensed Apache 2.0, with a later move into Home Assistant Core in mind. (Kai, 2026-10-22:
    "apache 2.0 then")

- **P16.2 Distribution.**
  - **P16.2.1** It ships as a public, experimental Home Assistant custom integration from the public GitHub
    repository thngkaiyuan/ha-assist-improver, installed the ordinary custom-integration way, through HACS.
    (PRODUCT-SPEC.md; HANDOFF.md, 2026-10-22)
  - **P16.2.2** Kai's household is the pilot; other households are who it's for. (README.md)
  - **P16.2.3** The code makes no household-specific assumptions. (ROADMAP.md)
  - **P16.2.4** The built package holds only the component: no household records or credentials. (README.md)
  - **P16.2.5** Each commit pushed to the public repository is scanned for household details, before the push and
    again in CI (tools/leak_guard.py), and a find stops it.
  - **P16.2.6** The published tree holds one integration under custom_components, plus every picture and link the
    README uses. (fixture-test/SPEC.md)
  - **P16.2.7** Issues go through the manifest's issue tracker. (fixture-test/SPEC.md)

- **P16.3 What it needs.**
  - **P16.3.1** It works on Home Assistant 2026.11.0 or later (the HACS minimum), with no pin to one version.
    (Kai, 2026-10-23: "so our integration does not depend on a specific HA version or internal implementation";
    MILESTONE-6.md)
  - **P16.3.2** It doesn't depend on Home Assistant's internal implementation. (Kai, 2026-10-23, same words)
  - **P16.3.3** If HA changes stop a sandbox from starting, the replay reports it and the real Home Assistant is
    unaffected. (MILESTONE-6.md, 2026-10-23)
  - **P16.3.4** It has no extra Python dependencies; its manifest installs nothing. (INSTALL.md; README)
  - **P16.3.5** It uses Home Assistant's own integration points and local storage: no companion process (on a Mac or
    elsewhere) and no special host setup. (PRODUCT-SPEC.md)
  - **P16.3.6** What it uses beyond HA's public API (touchpoints.py) loads where used; a break stops only its feature,
    is logged and shows one Repairs notice per feature, saying it needs an update, until the feature works again.

- **P16.4 Install and setup.**
  - **P16.4.1** Install through HACS (the Marketplace from Home Assistant 2026.11), from the README's button or a
    search; then Settings → Devices & services → Add integration → Assist Improver.
    (README; fixture-test/SPEC.md)
  - **P16.4.2** Setup happens once: install, add the integration, keep the recommended AI model, done. Its
    confirmation says what happens next. (DESIGN.md)
  - **P16.4.3** It's one setup entry, which can be disabled and unloaded. (MILESTONE-1.md)
  - **P16.4.4** Installing makes no model request. (INSTALL.md)

- **P16.5 Updates.**
  - **P16.5.1** An update keeps everything the improver stored. (INSTALL.md)
  - **P16.5.2** An update migrates stored data once, after the migration is tested on old-format data; the old
    version's compatibility code then goes. (Kai, 2026-10-25)
  - **P16.5.3** After an update, the panel loads the new version. (fixture-test/SPEC.md)

- **P16.6 Disable and uninstall.** What was improved stays.
  - **P16.6.1** Disabling or uninstalling stops recording and further improvement. Every accepted improvement but
    household lines (P16.6.5) stays and keeps working; it just gets no further updates. (Kai, 2026-10-21)
  - **P16.6.2** Accepted improvements stay because they're Home Assistant's own configuration: scripts, aliases,
    exposure and the assistants' instructions. (DESIGN.md; FEASIBILITY.md)
  - **P16.6.3** Disabling keeps what the improver stored. (INSTALL.md)
  - **P16.6.4** Uninstalling removes everything the improver stored and the AI Tasks it created; only the accepted
    improvements but household lines (P16.6.5) stay. (Kai, 2026-10-25: "We'll delete AI tasks created by us and
    leave only improvements behind.")
  - **P16.6.5** Uninstalling writes nothing into any assistant's instructions. The settings' Uninstall shows the
    household's lines as one text to copy, and goes once the household ticks that it saved them or will do
    without; removed from Home Assistant's own page, the improver leaves them in a notification. (Kai,
    2026-11-04: "the user is asking us to leave"; "just a small scrollable textbox")
  - **P16.6.8** Uninstalling works after a failed start too. (fixture-test/SPEC.md)
  - **P16.6.9** Reinstalling doesn't duplicate the household lines. (ROADMAP.md, 2026-10-24)

- **P16.7 Releases.**
  - **P16.7.1** v1 is worded as an early release. (Kai, 2026-10-24)
  - **P16.7.2** v1.0.0 comes after a code review, end-to-end pipeline tests and testable specs, in that order. (Kai,
    2026-10-24; Kai, 2026-10-29)
  - **P16.7.3** Before a new version is installed on the maintainer's Home Assistant (tools/preflight.py), a preflight
    replays its stored conversations with no model, and a regression stops the install. (SPEC-CORE.md §4 F3, Kai,
    2026-10-25)
  - **P16.7.4** GitHub release notes (HACS shows them) are user-facing words. Past releases stay empty; v1 gets one
    note, written once the on-demand-first experience settles. (TEXT-REVIEW.md, 2026-10-25)
  - **P16.7.5** README v1 opens with the banner "Early release. Used every day in my household, on Home Assistant
    2026.11.0. Tell me how it works in yours: open an issue." (Kai, 2026-10-25)
  - **P16.7.6** README v1's "Where we are" is one paragraph: every stage works end to end and runs every day in the
    household; this is the first release for other households, and their feedback decides what comes next.
    (Kai, 2026-10-25)
  - **P16.7.7** README v1's Getting started is on-demand first. Steps 3–5 start "Use Assist" (not "Use Assist as
    usual"): ask Assist to file an improvement (the fast lane, no run to start); send in past conversations from
    the panel, or set a schedule; review. Step 4's Run matches the button's words. (Kai, 2026-10-25)
  - **P16.7.8** The README's install step says the recommended settings give better outcomes. (Kai, 2026-10-22: "add
    that we recommend using the recommended settings for better outcomes")
  - **P16.7.9** Getting Started screenshots are generated from a demo household with made-up devices and
    conversations, never captured by hand, so nothing personal shows. (Kai, 2026-10-23; ROADMAP.md)

## P17. Words on screen

The principles for every word the household reads (the panel, its bubbles, notifications, setup and Repairs, the
README, release notes), then the approved words that are requirements in themselves. Where a clause below gives exact
words, those words are the requirement; elsewhere the behaviour lives in its own section.

- **P17.1** Everything on screen, words included, lets the household take it in at a glance and turn it into awareness,
  a decision or an action. No blocks of text nobody reads. (Kai, 2026-10-25: "putting blocks of texts that nobody is
  interested in reading only serves to frustrate. let's always keep this in mind")
- **P17.2** A change never makes a text longer or adds a block of text. Of two wordings that both work, the shorter
  one. (Kai, 2026-10-26: "don't make texts lengthier, don't add new blocks of texts"; Kai, 2026-10-25: chose the
  shorter home intro and the shorter on-demand bubble)
- **P17.3** One clear control that says what it does (e.g. "Run on 7 conversations") beats a control plus text that
  explains it. (Kai, 2026-10-25)
- **P17.4** Detail goes where the household asks for it: a confirmation, a bubble, an opened row. (Kai, 2026-10-25;
  TEXT-REVIEW.md "The home page, at a glance")
- **P17.5** Explanations are bubbles, not text on the page. (DESIGN.md "Text is kept to a minimum"; Kai, 2026-10-24)
  - **P17.5.1** A bubble shows on hover and on focus, never only on click, and stays while hovered or focused; a click
    puts it aside. (Kai, 2026-10-24: "show information in a bubble when hovered over - not click"; fixture-test/SPEC.md)
  - **P17.5.2** A bubble beside a menu never covers the menu's options. (fixture-test/SPEC.md)
  - **P17.5.3** A bubble's text is plain text, never markup. (fixture-test/SPEC.md)
  - **P17.5.4** Each stage on the settings diagram explains itself in a small bubble beside it (P17.16.1). (Kai,
    2026-10-24: "Add a small tooltip next to each role")
  - **P17.5.5** No bubble where it adds nothing: none on a model selector naming "Assist Improver" or AI Tasks while
    all is well, none on "Revisions". (Kai, 2026-10-24; fixture-test/SPEC.md;
    TEXT-REVIEW.md ✓, Kai 2026-10-25)
- **P17.6** No disclaimer boilerplate, scaremongering or "batteries not included" warnings in any words the household
  reads: panel copy, bubbles, notifications, setup and Repairs, Why it's safe, the README, release notes. (Kai,
  2026-10-24: "I don't want all those disclaimer boilerplates and scaremongering text to creep back into our product
  again")
  - **P17.6.1** A review finding about wording gets the smallest plain change, never more warnings. (Kai, 2026-10-24)
- **P17.7** Simulated success is never presented as proof of how physical devices behave. (PRODUCT-SPEC.md)
- **P17.8** The proposer's reviewer's confidence is never described as proof of no regressions. (PRODUCT-SPEC.md)
- **P17.9** Which AI providers are tested is said honestly, in one line, without "batteries not included"
  boilerplate (P17.25.10). (Kai, 2026-10-25: "Be honest about providers: sure. but avoid adding too much 'batteries
  not included' boilerplate")
- **P17.10** Words about where data goes stay broad, so they don't need editing whenever the pipeline needs new data:
  the AI models the household chose get what they need for their work, and nothing comes to us. No list of the data
  held or sent. (Kai, 2026-10-25: "they don't need a laundry list of all the data the system has on them"; "no list
  of what's sent")
- **P17.11** Words about recording never sound creepy (no "Watches"), and stress that conversations stay in the
  household's own Home Assistant, as private as it is. (Kai, 2026-10-22: "as private as their HA setup")
- **P17.12** Plain words: plain verbs, no entity IDs, no jargon. An improvement's headline is short and plain, and every
  status reads in plain words. (DESIGN.md "Written for the household"; fixture-test/SPEC.md)
- **P17.13** What the words say is what the product does. A README commitment, a chip, a count or a confirmation that
  stops being true is a bug, and a test holds each one true. (Kai, 2026-10-25: "make sure we already have tests in
  place to ensure these commitments don't diverge from our code behavior"; Kai, 2026-10-25 and 2026-10-26: a chip and a
  stage's count must agree)
- **P17.14** New or changed words need the maintainer's OK. (Kai, 2026-10-24: "let me know if you're planning to
  change this text")

- **P17.15** Names the household sees:
  - **P17.15.1** The product is "Assist Improver". (Kai, 2026-10-22)
  - **P17.15.2** The stages are named as in P3.1: Your Assist (not "Requests"), Triage (not "Quick check"), Analyze,
    Propose, Review, Simulate (not "Test"), Write up, Your review (not "You review"), Your household (not
    "Household"). (Kai, 2026-10-24)
  - **P17.15.4** The stage control is "Block" (not "Pause"), in each stage card's bottom-right corner. (Kai, 2026-10-24)
  - **P17.15.5** "Archive" (not "Hide") and "Delete from Assist Improver" (not "Delete for good"; P3.3's "Delete for
    good" is this action). (Kai, 2026-10-25)
  - **P17.15.7** An improvement is known by its write-up headline everywhere: every notice, the list, the review. If
    the write-up failed, by the proposer's title. (fixture-test/SPEC.md; DESIGN.md; SPEC-CORE.md)

- **P17.16** The settings diagram's words:
  - **P17.16.1** Each stage's bubble reads, exactly:
    Triage "Recommended to use a small model to reduce downstream costs by filtering out conversations that clearly
    need no improvement." · Analyze "Reads each conversation to identify your intents, and where Assist could've done
    better." · Propose "Propose changes to make your Assist experience smoother." · Review "Align proposals with the
    broader view of how Assist is used in your household." · Simulate "Replay your conversations with proposed fixes
    in a simulated copy of your home. Nothing in your home moves." · Write up "Packages technical improvements so
    they're easy for you to review." · Your review "Accept, decline, or ask for a revision." · Your household "Your
    house, made easier." (Kai, 2026-10-24, his words; Analyze: Kai, 2026-10-25)
  - **P17.16.2** Block's bubble: "Stop this stage."; while blocked: "Resume this stage." (Kai, 2026-10-24)
  - **P17.16.3** The revision loop's label is "Revisions": plain text in the stream, readable, with no icon or fill, so
    it doesn't look like a button. (Kai, 2026-10-24)
  - **P17.16.4** Triage with no model reads "Pass through", not "Off". (Kai, 2026-10-24: "Off reads more like a block")
  - **P17.16.5** Triage's setting reads "Triage clearly straightforward conversations" (not "Triage the one-turn ones").
    (Kai, 2026-10-24; 2026-10-25)
  - **P17.16.6** Simulate's chip bubble: "At most {n} tests of a conversation each round, on your assistant."; under
    its stepper, "tests per conversation, each round"; then "Check replies with", bubble "Replies are checked with
    the write-up model when off". (Kai, 2026-10-25; Kai, 2026-10-27: "keep them for now"; P6.3)
  - **P17.16.7** Simulate's picker tells the household the limit multiplies their normal assistant spend: "Each test
    replays one of your conversations with a fix, on your own assistant: {n} each round costs at most about {n} times
    that conversation, plus up to three replays of it without the fix". (Kai, 2026-10-24, his words, the three
    replays per Kai, 2026-11-05; Kai, 2026-10-27: "keep them for now")
  - **P17.16.11** The household view names a person Home Assistant no longer has as "Someone". (fixture-test/SPEC.md)
  - **P17.16.16** Every model menu offers "Use recommended setup". (Kai, 2026-10-25: "All approved.")
  - **P17.16.17** A model from a provider the improver can't judge carries a neutral "Fit unknown" tag. (Kai,
    2026-10-25: "All approved."; DESIGN.md)
  - **P17.16.18** A weak model on a judgement step is marked "not recommended". (fixture-test/SPEC.md)
  - **P17.16.19** The weak-model and Repairs dialogs are plain: why in a line or two, then "Use it anyway" or "Choose
    another". No walls of text. (Kai, 2026-10-25: "Plainer 'too weak' and Repairs dialogs: approved";
    fixture-test/SPEC.md)

- **P17.17** Run words:
  - **P17.17.1** Every run control says how many conversations it runs on, and its confirmation names which. (Kai,
    2026-10-25: "how many conversations will be run, and which")
  - **P17.17.2** Settings' Run button: "Run on {n} new conversations" · "Run on 1 new conversation". With no new
    conversation but other work waiting, "Run what's waiting"; with nothing at all, "Run now". (Kai, 2026-10-25: "Use
    'Run on {n} new conversations'"; Kai, 2026-10-26: "Run what's waiting" when that's what it does)
  - **P17.17.3** The home page's Run bar is just its button: "Run on {n} new conversation(s)" (with picks, the picked
    ones), or "Run what's waiting" when no conversation waits but other work does (a revision, a write-up…). (Kai,
    2026-10-25: "Approve the stripped-down Run bar."; "Update the text 'Run on 16 new conversations'")
  - **P17.17.4** A run confirmation's title, home and Settings: "Run on {n} new conversation(s)?"; "Run Assist
    Improver?" when it takes no conversation. (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.17.5** A run with conversations has no sentence under the title. Under it, which conversations by their
    first words: "“…”, “…”, “…” and {n} more", or all of them when three or fewer. (Kai, 2026-10-25: F4 approved)
    - **P17.17.5.1** Each is up to 40 characters, cut at a word, then "…". (TEXT-REVIEW.md, Kai 2026-10-25)
    - **P17.17.5.2** The ones named plus "and {n} more" add up to the title's count. (TEXT-REVIEW.md, Kai 2026-10-25)
    - **P17.17.5.3** Order: on the home page the picked ones; in Settings the new ones, those sent in first, then the
      newest. (TEXT-REVIEW.md, Kai 2026-10-25)
    - **P17.17.5.4** An archived conversation is never named. With none to name, the list is left out.
      (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.17.6** Then "Also: {items}", only when other work waits, in P14.7.3's words. (Kai, 2026-10-25: "Kai approved
    the layout and 'Also:'")
    - **P17.17.6.1** An accept that met a conflict is counted in "{n} revision(s) you asked for", wherever runs say
      what waits. (SPEC-CORE.md, Kai 2026-10-26)
  - **P17.17.7** Then the blocked-stage and model warnings, only when they matter, then the cost line. (Kai, 2026-10-25)
  - **P17.17.8** The cost line gently reminds that runs cost money: "Runs use your AI models, so they cost money."
    (Kai, 2026-10-24: "It should gently remind you that the runs cost money ... but the confirmation text is currently
    too lengthy"; fixture-test/SPEC.md)
  - **P17.17.9** Under it, a rough estimate from past runs, as a short list of tokens per model; every figure names the
    model it's spent on, readable for people; one line when both steps run on one model. (Kai, 2026-10-25: "make sure
    we're able to match those token estimates with models")
  - **P17.17.10** Run one stage, and a run with no conversations to take, keep one short sentence and no Also line:
    "Analyzes {n} conversation(s).", "1 revision you asked for is waiting.", "Runs on 1 revision you asked for."
    (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.17.11** A running run's title is "Improving your assistant" until it has taken its conversations in, then:
    "Running on {n} new conversations" · "Running on 1 new conversation" (a full run) · "Running on the conversations
    you picked" (a picked run) · "Running on what you sent in" (the fast lane) · "Running {stage} only" (a stage run).
    (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.17.12** Settings' Cancel run bubble is that title plus its stage, e.g. "Running on 14 new conversations ·
    Analyze". (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.17.13** Progress: "Analyzing conversations: {done} of {total}." and "You can leave this page; it keeps
    going." (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.17.14** Propose's progress: "Proposing fixes for {n} conversation(s), with a reviewer checking each…", {n}
    counting only the conversations whose trouble it's fixing. (TEXT-REVIEW.md, Kai approved 2026-10-25; Kai,
    2026-10-26)
  - **P17.17.15** The schedule options, in the Run button's menu and its clock's bubble: "Only when you run it" ·
    "Every day at 3:00, on new conversations" · "Every Sunday at 3:00, on new conversations". (Kai, 2026-10-25)
  - **P17.17.16** History names how a run started: "Run finished · on demand" (the fast lane), "Run finished · picked",
    "Run finished · scheduled"; a manual full run adds nothing. (Kai, 2026-10-25: "trust u on this")
  - **P17.17.17** History, a stage whose items all failed: "{Stage}: Nothing done: its 1 item failed" · "{Stage}:
    Nothing done: all {n} of its items failed". (Kai, 2026-10-26: "Approved")
  - **P17.17.19** The home page shows a notice about the last run only when the household must act (a stage whose AI
    model can't run), with "Open settings". History keeps what happened, technical details folded under "Details", in
    "Nothing was lost: what it didn't finish is held for your next run." and "{steps} couldn't finish". (Kai,
    2026-10-25: "keep a notice only when I need to act")

- **P17.18** The home page's words:
  - **P17.18.1** Section title: "Your conversations". (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.18.3** Intro, in the ⓘ bubble beside the section title: "Pick the ones that didn't go well, and run. Any you
    send in, or file with Assist, go straight to the improver." (Kai, 2026-10-25: "Use the shorter intro.")
  - **P17.18.4** Search: "Search what was said, or when"; its clear button "Clear search" (screen readers, bubble);
    with no conversation having every word, "No conversation has all those words. The closest:". (TEXT-REVIEW.md, Kai
    2026-10-25)
  - **P17.18.5** Day filter "All · Today · 7 days · 30 days"; sort "Newest · Most back-and-forth · Oldest", its menu's
    button naming the one chosen, or "Sort" while a time searched for puts the closest first. (TEXT-REVIEW.md, Kai
    2026-10-25; Kai, 2026-10-30)
  - **P17.18.6** Selection: "Select all {n}"; a row's checkbox, for screen readers and its bubble, "Select “{said}”" ·
    "Select"; the bottom bar "Archive ({n})" · "Unarchive ({n})" (archived ones) · "Delete ({n})". (TEXT-REVIEW.md,
    Kai 2026-10-25)
  - **P17.18.7** A row's sign of trouble: "{n} failed step(s)". (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.18.8** Nothing recorded yet: "Use Assist as usual: your conversations show up here, ready to send in."
    (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.18.9** A search or filter with no match: "No conversations match."; a conversation with nothing said:
    "(nothing said)"; the list grows with "Show more". (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.18.10** Nothing waiting above, all archived, unfiltered: "Nothing needs you right now." (Kai, 2026-10-26:
    "Approved"; TEXT-REVIEW.md)
  - **P17.18.11** Last run line: "Last run {when} · {n} new improvement(s)"; the count is left out once nothing is left
    to review. (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.18.12** While any are archived, the list ends with a "Show archived ({n})" checkbox. Archived ones it shows
    are dimmed and carry no word of their own. (TEXT-REVIEW.md ✓, Kai 2026-10-25)
- **P17.19** An opened conversation's words:
  - **P17.19.1** Chat bubbles labelled "You" (on the right) and "Assist" (on the left). (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.19.2** A failed step shows where it happened, between what was said and Assist's reply: "Failed step: {what}
    · {on what}", then Home Assistant's own error exactly as it gave it. (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.19.3** {what} is a plain verb: Turn on · Turn off · Toggle · Set light · Set position · Stop · Set temperature
    · Set fan speed · Pause · Resume · Next track · Previous track · Set volume · Start vacuum · Send vacuum home ·
    Vacuum an area · Broadcast · Check · Look up devices · Add to list · Start timer · Run script {name}. Any other step
    is named from its own name (e.g. "Media player mute"). (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.19.4** Sending in: "What should have happened? (optional)" · "Send this one in" (on its way, the same words,
    its arrow a spinner). Then "Sent in. The improver is looking at it now." Opened again, it shows "Your note:". (TEXT-REVIEW.md, Kai 2026-10-25; Kai, 2026-10-30)
  - **P17.19.5** A ⋮ beside Send this one in ("More actions" for screen readers and its bubble) holds "Archive this
    conversation" (or "Unarchive" for an archived one) and "Delete from Assist Improver" (in red; asks first).
    (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.19.6** Delete asks first and says what deleting means: "Delete this conversation from Assist Improver? The
    improver can no longer check changes against it."; for a selection, "Delete {n} conversation(s) from Assist
    Improver? The improver can no longer check changes against them."; buttons "Cancel" · "Delete from Assist
    Improver". (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.19.7** Sending in is refused with "That conversation isn't kept." · "{n} of those conversations aren't kept." ·
    "The improver already has that conversation." (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.19.8** A queued delete that fails: a Home Assistant notification titled "Assist Improver": "A conversation
    couldn't be deleted. Try deleting it again." (Kai, 2026-10-26: "Approved"; TEXT-REVIEW.md)

- **P17.20** A conversation's chip names the stage it's at, never a vague "With the improver". (Kai, 2026-10-25:
  "labeling the specific stage where the conversation is at is even more preferable")
  - **P17.20.1** Each chip fits one line of a 402px phone row beside long first words. (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.20.2** The chips, by where the conversation is (SPEC-CORE.md; TEXT-REVIEW.md, Kai 2026-10-25):
    "Still going" (still recording) · "Sent in" (filed or sent in, until a stage past Triage takes it) · "New" (not
    looked at yet: new, or waiting at Triage) · "To analyze" (at Analyze) · "Looking for a fix" (at Propose) · "No fix
    yet" (at Propose after a fix was tried, tests left) · "Up for review" (at Your review; opens that review) ·
    "Looked smooth" · "No trouble found" · "Declined" · "Improved" · "No fix found" · "Marked as a test" · "Couldn't be
    read" (in Your household).
  - **P17.20.3** "Looked smooth"'s bubble: "Nothing to improve, at a quick look. Send it in for a full analysis."
    (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.20.4** "No trouble found"'s bubble: "Nothing went wrong, as far as it could tell. Send it in again with a note
    if something did." (TEXT-REVIEW.md, Kai approved 2026-10-25; Kai, 2026-10-26: "Approved")
  - **P17.20.5** "No fix yet"'s bubble: "The next proposal looks for another fix." (Kai, 2026-10-26: approving "No fix
    yet · The next proposal looks for another fix")
  - **P17.20.6** "No fix found"'s bubble: "No fix worked. Send it in again with a note for one more look."
    (TEXT-REVIEW.md, Kai approved 2026-10-25)

- **P17.21** The review's words:
  - **P17.21.1** What an improvement gets, on the home list and the review's header: "{n} new thing(s) Assist can do"
    (was "Enables {n} task(s)") · "{n} fewer turn(s)" · "{n} unwanted change(s) avoided" · "follows your feedback".
    (TEXT-REVIEW.md, Kai 2026-10-25; Kai, 2026-10-26: "Approved")
  - **P17.21.2** Its parts, in order: "What you get" · "Before & after", each test's "Before" and "After" · "Why it's
    safe" · then folded, "What went wrong" (was "How it works") · "What changes ({n})". Under a test's After, "Still
    open", each line their words (once), its bubble "Reply {n}: {the judge's reason}" ({n}: that reply's place in the
    replay with the change), or for one set aside (P6.6.2) "It didn't happen without the change either, so this test
    couldn't show it." (TEXT-REVIEW.md, Kai 2026-10-25; Kai, 2026-10-26: "Approved"; Kai, 2026-10-29: words on
    judgement; Kai, 2026-10-30: the order, and the sections that repeated what you get gone)
  - **P17.21.3** A Before & after row's result chip: "goal now met" · "{a} → {b} turns" · "improved" · "side effect
    gone" / "side effects gone" · "follows your feedback"; several joined by " · ", e.g. "3 → 1 turns · follows your
    feedback". After it, an amber "{n} still open" chip (how many Still open lines close its After), bubble "Something you
    pointed out still happened in a reply with this change.", or the set-aside bubble when none did. A goal the home
    couldn't do either way (P9.10.6) reads "{goal}: the home couldn't do it ({device} was unavailable)", or "({device}
    failed)" where its command failed at home. (TEXT-REVIEW.md, Kai approved 2026-10-25; Kai, 2026-10-26: "Approved"; Kai,
    2026-10-29: words on judgement)
  - **P17.21.4** A test that made Assist wordier gets an amber "Says more" chip after its result, bubble "Assist's
    replies: about {after} words each, {before} before."; under the tests, "With this change, Assist's replies got
    longer in {n} of these." (TEXT-REVIEW.md, Kai approved 2026-10-25; Kai, 2026-10-26)
  - **P17.21.5** The words in P11.5. (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.21.6** The decision menu: "Accept changes" (the default) · "Ask for a revision" · "Decline". (Kai, 2026-10-24;
    TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.21.7** Under Decline, a box to tick, "Not an issue", with the hint "It went fine, or you were only trying
    Assist out." The note's placeholder is "Anything to add? (optional)" when it's ticked, "Why not? (optional)" when
    not. (TEXT-REVIEW.md, Kai approved the change 2026-10-25; DESIGN.md "Your choices")
  - **P17.21.8** After Accept: "Applied. Assist uses it from your next request."; with a script among the changes, and
    only then, "…; no script was run." (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.21.9** After Ask for a revision: "Thanks. Your next run proposes a revised version." (TEXT-REVIEW.md, Kai
    2026-10-25)
  - **P17.21.10** After Decline: "Thanks." (replaces "Thanks. The improver won't propose this again.", TEXT-REVIEW.md,
    Kai 2026-10-25; Kai, 2026-10-26: "yes")
  - **P17.21.11** After Undo, beside what happened, "Decide again" (was an "Accept changes" link, which sat where Undo
    was: one tap from accepting again). After a revision: "Being revised", "Being revised for your note “{note}”" and
    "Decide on this version". The way on: "Next: {headline} →", "All caught up · back to the list", or "Back to the
    list" while this one still waits.
    (TEXT-REVIEW.md, Kai 2026-10-25; Kai, 2026-10-30)
  - **P17.21.12** A revision carries a small "Revision" tag after its title; the improvement it replaced shows "Replaced
    by a revision". (TEXT-REVIEW.md, Kai 2026-10-25)
  - **P17.21.13** An instruction edit shows as a diff titled "{assistant}'s instructions", tagged "instructions". A
    decided improvement that wasn't applied reads "Add to {assistant}'s instructions: “{line}”" · "Add to
    {assistant}'s instructions: “{line}”, replacing “{old line}”" · "Remove from {assistant}'s instructions: “{old
    line}”". (TEXT-REVIEW.md, Kai approved 2026-10-25)
  - **P17.21.14** "Can't apply it now:" says why, one line each: "{assistant}'s instructions: It was changed in Home
    Assistant after the improvement was tested." · "{assistant}'s instructions: The assistant it was tested on is no
    longer set up." · "{assistant}'s instructions: The instruction line it replaces has changed since it was tested:
    '{old line}'" · "{assistant}'s instructions: Home Assistant can't read the edited instructions: {error}" ·
    "Household knowledge: The assistant doesn't use Assist, so it can't read the household knowledge." (TEXT-REVIEW.md,
    Kai approved 2026-10-25)
  - **P17.21.15** Why it's safe answers why it's safe, e.g. "Only settings change: 1 instruction change. Nothing runs
    when you accept." It never says "Assist will be able to control: {names}." (Kai, 2026-10-25: "I don't see how this
    answers 'Why it's safe'. Please drop it."; TEXT-REVIEW.md)

- **P17.22** When only improvements wait, one notification: "{n} improvements to review", a bulleted link per
  improvement, then "Review these improvements in Assist Improver.", with "Assist Improver" linking to the panel.
  (Kai, 2026-10-24, his layout: "link the notification into the panel")

- **P17.23** When the analysis can't tell what the household wanted, the improver asks the household instead of
  guessing, and the conversation isn't tested until the household answers; it waits in Your review's queue meanwhile
  (P9.9.6). (PRODUCT-SPEC.md; ROADMAP.md Milestone 4; SPEC-CORE.md; Kai, 2026-10-26)

- **P17.24** The recommended facts, as lines in an assistant's own instructions, word for word (Kai, 2026-10-26:
  "Approved"; TEXT-REVIEW.md, Kai approved 2026-10-25):
  - **P17.24.1** Who's speaking: `{% if user_name %}The person speaking is {{ user_name }}.{% endif %}`
  - **P17.24.2** The device the request came from: `{% if llm_context.device_id and device_name(llm_context.device_id)
    %}The request came from {{ device_name(llm_context.device_id) }}.{% endif %}`
  - **P17.24.3** The date and time: `It's {{ now().strftime('%A %-d %B %Y, %H:%M') }}.`
  - **P17.24.4** The household's time zone: `The household's time zone is {{ now().tzinfo }}.`

- **P17.25** The public README:
  - **P17.25.1** It's friendly and high level: what the integration aims for and how it gets there, then where things
    stand. No implementation detail. One "How it works" section, with no long "why", told with the self-driving
    analogy. (Kai, 2026-10-22: "high level of what the integration aims to achieve, and specifically how we aim to do
    it"; HANDOFF.md)
  - **P17.25.2** Tagline: "Your Home Assistant's AI, easier every week." (Kai, 2026-10-22)
  - **P17.25.3** v1 is worded as an early release. Its banner: "**Early release.** Used every day in my household, on
    Home Assistant 2026.11.0. Tell me how it works in yours: open an issue." (Kai, 2026-10-24; TEXT-REVIEW.md ✓, Kai
    2026-10-25)
  - **P17.25.4** "Where we are" is one paragraph: "Every step above works end to end, and runs every day in my
    household. This is the first release for other households: your conversations will show it things mine never did,
    and your feedback decides what comes next." (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.25.5** Pictures: a GIF of a relaxed driver who suddenly grabs the wheel, right below the self-driving
    sentences; Assist screenshots that get smoother each time (first fewer extra turns, then less guided wording),
    in a simple UI style; the icon about 128 px. (Kai, 2026-10-22)
  - **P17.25.6** The screenshots' caption "1 turn, naturally", and in bold, "you just enjoy the ride." (Kai,
    2026-10-22: "i prefer '1 turn, naturally'")
  - **P17.25.7** Getting started is on-demand first, starting "Use Assist" (not "Use Assist as usual"): ask Assist to
    file an improvement (the fast lane, no run to start); send in past conversations from the panel, or set a
    schedule; review. Step 4's Run uses the button's words. (Kai, 2026-10-25)
  - **P17.25.8** The install steps recommend the recommended settings, for better outcomes. (Kai, 2026-10-22: "add that
    we recommend using the recommended settings for better outcomes")
  - **P17.25.9** Good to know: "**Every assistant benefits.** What the improver learns about your household reaches
    every assistant that controls Home Assistant." (TEXT-REVIEW.md, Kai approved 2026-10-25)
  - **P17.25.10** Good to know: "**Tested with Anthropic and OpenRouter.** Other AI providers should work too."
    (TEXT-REVIEW.md ✓, Kai 2026-10-25)
  - **P17.25.11** A "Your data" section, before License, with six lines (TEXT-REVIEW.md ✓, Kai 2026-10-25):
    - **P17.25.11.1** Kept in your Home Assistant: the household's Assist conversations, next to the history Home
      Assistant already keeps.
    - **P17.25.11.2** "**Sent during a run:** the AI models you chose get what they need to do their work. Nothing comes
      to us." (Kai, 2026-10-25: "no list of what's sent")
    - **P17.25.11.3** "**For how long:** conversations are kept in full for at least 30 days, then condensed, keeping
      enough for the improver to see your household's needs as a whole. Its own logs are kept only as long as they're
      useful, a year at most." (Kai, 2026-10-25: "Use: then condensed, keeping enough…")
    - **P17.25.11.4** Uninstalling removes everything the improver stored and the AI Tasks it set up; accepted
      improvements stay. (Kai, 2026-10-25)
    - **P17.25.11.5** "**You decide what's kept:** archive a conversation, or delete it for good." (TEXT-REVIEW.md ✓,
      Kai 2026-10-25)
    - **P17.25.11.6** Its own user, after For how long: the system user it reads the home through (P7.6.15), with no
      login; uninstalling removes it.
- **P17.26** GitHub release notes are words the household reads (HACS shows them). Past releases stay empty; v1 gets
  one note, written once the on-demand-first experience settles. (Kai, 2026-10-25)
