# Assist Improver: the spec

The one source of what the product does: Kai's decisions. Each clause has an id that tests name (`@spec("P0.2")` in
fixture-test/graph). Anything not here isn't a requirement. The detail that used to be here is in DETAILS.md, for code
and its tests to settle.

## P0. The model

Between two of Kai's own words, the later wins.

- **P0.1 What it's for.** Conversations in, improvements out: the improver makes fulfilling users' goals (their
  intent) effortless. (Kai, 2026-10-26)
  - **P0.1.1** For now, fulfilment is goals met; effort is turns per root goal and side effects left.
    (Kai, 2026-10-26)
- **P0.2 The package.** A conversation is a package with one canonical record of where it is, and, for each round, one
  per stage of its state there. Every screen and stage reads those records; none works them out differently. (Kai,
  2026-10-26)
- **P0.3 Turns and goals.** Every turn (said to Assist, a note sent with the conversation, or a revision note) maps to
  at least one goal. Goals are a tree under turns: a root goal is one world state at one time; a sub-goal exists only
  because of its parent; any goal is met, unmet, or indeterminate. A note, or a "thanks", is a sub-goal of the root
  goal it's about. (Kai, 2026-10-26)
- **P0.4 Stages.** Your Assist → Triage → Analyze → Propose → Write up → Your review → Your household, each with typed,
  tested contracts between them. Review and Simulate are stamps the proposer collects: an idea goes on to Write up only
  with both. (Kai, 2026-10-26)
- **P0.5 Conversations travel whole.** A conversation is at one stage at a time, with all its goals. (Kai, 2026-10-26)
- **P0.6 Rounds.** Each time a conversation enters the pipeline is a round with fresh limits. Within a round it flows
  until an improvement waits at Your review, it ends in Your household from any stage, or it's held for a problem. A
  question for the household pauses the round at Your review; the answer takes it back to the stage that asked, and
  the round goes on. Sending it in again, a revision, or a conflict the household sends back starts a new round; an
  earlier round's outcome stands. (Kai, 2026-10-26; questions 2026-11-04)
- **P0.7 Runs.** A run admits conversations, and only what it admits moves. A manual full run admits every new one and
  releases every held one; a scheduled run admits new ones only; a picked run, the picked ones; the fast lane, one sent
  in, as soon as it ends; a stage run releases what's held at that stage; an answer to a question goes straight back
  to the stage that asked; a revision goes straight back in at Propose; an accept that meets a conflict says so and
  asks the household before it goes back in at Propose to resolve it. (Kai, 2026-10-26; answers 2026-11-04)
- **P0.8 Held.** A held conversation stopped at a stage for a problem (failures, a run's scope, a cut-off run, or the
  stage being blocked), with why; only a manual full run, its stage's run, or a run that picks it releases it. A stage
  retries a failing conversation a few times in a run before holding it, and each release gives one more try. (Kai,
  2026-10-26)
- **P0.9 Simulate.** Simulate gives the proposer a way to validate its hypothesised improvements (P0.1) on empirical
  evidence. The proposer decides how to use the round's tests: it sends on what's validated, or tries to make it better
  first. (Kai, 2026-10-26)
- **P0.10 Improvements.** An improvement belongs to the one conversation it came from and was tested on; its changes can
  be broad. The proposer and its reviewer look at the household as a whole. (Kai, 2026-10-26)
- **P0.11 Your review.** The household accepts, asks for a revision, or declines. Decline declines the improvement and
  archives the conversation, nothing more. A question for the household waits here too. (Kai, 2026-10-26)
- **P0.12 Your household.** Everything decided and out of the pipeline: improved, no fix found, declined, no trouble
  found, looked smooth, marked as a test, couldn't be read, duplicate (another conversation's improvement covers it),
  archived (kept for checking other changes against). Any of them can be sent in again, for a new round.
  (Kai, 2026-10-26)
- **P0.13 No endless loops.** Every edge that sends a conversation back costs a capped budget or needs a person.
  (Kai, 2026-10-26)
