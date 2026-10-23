# Design principles

How Assist Improver is built, and why. The code follows these; when a principle changes, this page changes with it.

## The pipeline

1. **Record.** Each Assist conversation is kept in Home Assistant with what happened in the home during it: the words, the assistant's tool calls and the device changes.
2. **Analyze.** An AI *journey analyzer* reads one session and lists what the user wanted (goals) and the steps they took only to get there (sub-goals).
3. **Propose.** An AI *hypothesis generator* looks at the friction across sessions, groups it into themes and proposes one change per theme. A separate AI *reviewer* checks each idea before any money is spent testing it.
4. **Test** (in development). Each change is replayed against the original conversations, with devices simulated, and kept only if it saves effort.
5. **Ask.** Changes that proved themselves are shown to you to accept, revise or decline.

## Models judge; code derives

AI models are used only for judgement calls: what the user meant, what caused the friction, what change would help. Anything settled or deterministic is computed by code, for example:

- how many extra turns a goal took, whether it was reached, and the side effects left behind;
- the time between turns, and which device states changed;
- whether a proposal names real devices, states they can take, and the sessions it claims to fix.

Models answer through tools that validate their answer against the household and reject it with reasons until it is valid, so a malformed or impossible answer never moves on.

## Reading what the user wanted

- **Respect the timing and separation of requests.** What the user asked for separately, at different times, stays separate; what they asked for together is one request. A request is never turned into a broader one the user didn't make, even if their intent seems predictable.
- **Corrections refine, they don't add.** A correction, a clarification or an answer to the assistant's question makes the original request more exact.
- **Don't guess what the user left open.** When the user's words leave settings open, the analyzer lists those controls for the user to choose instead of inventing values.

## Proposing changes

- **Least specific change that works.** In order of preference: instructions and preferences for the assistant, aliases, exposure (what the assistant can see and control), then scripts for what the assistant's own tools can't do.
- **The simplest rule consistent with every session.** A rule is tested against every session where it would apply, not only the one that prompted it. Special cases and rules built on a single session need a reason.
- **Evidence, both ways.** Each proposal lists the sessions that support it and any that contradict it.
- **Fix the real cause.** Speech recognition mishearing the user is in scope, but only when the sessions show it happened.

## Reviewing changes

- **A separate reviewer** reads each idea with read-only access to the same sessions and devices. It writes [Conventional Comments](https://conventionalcomments.org/) (each labelled, marked blocking or non-blocking, and backed by session evidence).
- **The verdict follows from the comments:** any blocking comment means changes are requested; otherwise the idea is approved.
- **Every blocking comment gets an answer:** the generator marks it resolved, or won't fix with a reason.
- **One reviewer per run.** The reviewer keeps a single conversation across all the drafts of a run, so it remembers what it already approved and asked for.
- **Bounded back-and-forth:** at most 15 reviews per run and 5 per idea, tuned from observed runs.

## Shared knowledge

- **Home Assistant's own rules, upfront.** The generator and reviewer get a short primer on how Assist works (exposure, the tools it has per device type, aliases, scripts) and Home Assistant's published best practices, checked against the Home Assistant sources.
- **Each role gets the context it needs** to understand the household: devices, areas, aliases, what the assistant can and can't do with each device, and the sessions themselves.

## Safety

- **Nothing in your home moves during testing.** Replays and practice mode run against a simulated copy of the home; only commands to devices are intercepted.
- **Practice mode is per person.** It affects only the signed-in person who turned it on; others and voice satellites are never affected.
- **Changes are ordinary Home Assistant configuration.** They are shown to you before they apply, nothing runs when they do, and they stay if you uninstall Assist Improver.

## Cost and data

- **Your conversations stay in your Home Assistant.** Analysis sends what it needs to the AI model you chose, through Home Assistant's own AI Task.
- **Paid analysis runs only when you allow it.** Cost controls are coarse on purpose: the number of counterfactual tests per cycle, not per-call accounting.

## How the AI roles are tested

- **Hand-labelled sessions.** Each role's output is scored by code against labels for real sessions. Hard checks pass or fail; judgement calls (such as how narrowly to ask) are soft findings for a person to read.
- **Three rounds in a row.** A prompt or design change counts only when the role passes three rounds in a row with the same instructions and model, plus a check on sessions it was never tuned on.
- **Seeded cases for the reviewer:** deliberately good and flawed proposals check that it approves the good one and blocks the flawed ones.
