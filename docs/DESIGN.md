# Design principles

How Assist Improver is built, and why. The code follows these; when a principle changes, this page changes with it.

## The pipeline

1. **Record.** Each Assist conversation is kept in Home Assistant with what happened in the home during it: the words, the assistant's tool calls and the device changes.
2. **Analyze.** An AI *journey analyzer* reads one session and lists what the user wanted (goals) and the steps they took only to get there (sub-goals).
3. **Propose.** An AI *hypothesis generator* looks at the friction across sessions, groups it into themes and proposes one change per theme. A separate AI *reviewer* checks each idea before any money is spent testing it.
4. **Test.** Each change is replayed against the original conversations, on the household's own assistant, in a sandboxed Home Assistant set up as the recorded home, and kept only if it saves effort.
5. **Ask.** Changes that proved themselves are shown to you to accept, revise or decline.

New conversations wait at the start until a run takes them in; then each step works on what reached it and passes its output on: triage (or pass through, with no triage model), analysis, proposals, simulations, write-ups. It starts when you choose **Run now** (after it tells you what it will do and that it costs money), or on a schedule you set, off by default. A step you block is a gate: a run stops there and what reaches it waits, while later steps still work on theirs; the Run now menu can also run a single step. Improvements go up for review once written up. Your decisions feed the next run: it never proposes again what you declined or said was fine as it was, and a revision addresses your note. Nothing is fixed twice: code finds the existing improvements (waiting for your review, or applied) that touch the same devices as new friction and shows them to the proposer and reviewer upfront, who can read any of them in full. A goal one of them already addresses is noted with it and skipped, instead of becoming a new idea to simulate and review; code checks the claim (an improvement applied before the conversation didn't stop its friction).

## Experience: simple, optimized by default, one thing at a time

Assist Improver should need almost no explanation: not because it does little, but because each screen asks for one thing, the best choice is already made, and it tells you when it needs you.

- **One thing at a time.** Each screen leads to one decision, with the recommended answer ready: the panel shows the next thing to do, a review opens with its summary on top and the details folded, **Accept changes** is the default, and the next review is one click away.
- **Written for the household.** Each improvement leads with a plain headline (what it does) and what you get, written by a small AI editor and checked by code (short, no entity IDs, no jargon). Why it's safe is never written by a model: code states it from the tests and the changeset.
- **Optimized by default.** The recommended setup is preselected: Claude Opus 5.5 for the steps that need judgement, and a small model (Claude Haiku, or the provider's own small one) for Triage and Write up, so a conversation that needs nothing costs little. The safe choices are the defaults, and no setting is needed to get good results. If you change things, one click in any model menu (Use recommended setup) brings it back.
- **A step never fails silently.** A step whose model is gone or unavailable shows ⚠ on its model and in Repairs; a run that would need it waits and says which step, instead of failing midway. A model the improver can't judge (a provider whose settings it can't read) is marked "Fit unknown"; one that can't use tools is named as the reason a step failed.
- **Nothing to remember.** It tells you when it needs you (a notification, the count beside it in the sidebar, Repairs for a setup gap) and every message links straight to the place to act.
- **Safe by default.** Nothing in your home changes without your review; a run says what it will spend before it starts; changes apply all or nothing and can be undone.
- **Never repeats itself.** A run leaves out goals that already have an improvement, and the same change is never shown twice. Asking for a revision brings back a new version, linked to your note.
- **Takes care of itself.** Household knowledge reaches every assistant without setup; an assistant added later is offered in the panel and in Repairs.
- **Settings you can see.** The panel's settings draw the pipeline as a track: requests merge onto the improvement loop, the proposer and reviewer go round their own small loop, revisions go round again, and what you accept peels off to the household. Each step has a glyph; each AI step names its model (one click to change it or add a new AI Task) and shows its backlog; a step can be blocked, or run on its own from the Run now menu. Your Assist names the assistant Assist uses by default; Your household opens what the household knowledge holds, beside its one switch (on by default). Auto-accept (off by default) applies each improvement as soon as it's written up. A history below lists runs, improvements going up for review, and decisions. Text is kept to a minimum: explanations are tooltip bubbles, there when you hover.

### The stories

1. **Set up once.** Install, add the integration, keep the recommended AI model, done. The confirmation says what happens next. Every setting after that lives in the panel (Configure links there).
2. **Live as usual.** Use Assist as you always do; the conversations where you had to repeat, correct or clarify yourself, where it changed something you didn't ask for, or where you told it how to behave, are what it learns from.
3. **Run.** The panel shows how many new conversations are waiting and offers **Run now**, saying what the run will do and that it costs money. One click, then progress you can leave and come back to.
4. **Review.** A notification lists what passed its tests, each linking to its review. Read the headline, open what you want to check, accept with one click, and go straight to the next one, until you're all caught up.
5. **Change your mind.** An applied improvement can be undone from its page.
6. **Steer.** Ask for a revision with a note, or decline, saying if you like that it wasn't an issue; the next run takes it into account and never proposes a declined idea again.
7. **Add an assistant.** It reads the household knowledge from its first request: nothing to set up.

## Models judge; code derives

AI models are used only for judgement calls: what the user meant, what caused the friction, what change would help. Anything settled or deterministic is computed by code, for example:

- how many extra turns a goal took, whether it was reached, the side effects left behind, and whether a fix left them alone;
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
- **Each assistant's own instructions.** The proposer and reviewer see which assistant each conversation used, the instructions it ran with and the ones it has now. A fault in one assistant's instructions (a fact they don't give, such as who's speaking, or a rule that's wrong) is fixed there, and only that assistant's conversations are replayed with the edit; a household fact goes in the household knowledge. A short list of facts every assistant's instructions should give (who's speaking, the device the request came from, the date and time, the household's time zone) is offered where they're missing, never twice: code checks each isn't there already.

## Testing changes

- **Home Assistant's own engine, in a sandbox.** Each replay gets a separate Home Assistant instance, seeded from the recording: areas, devices, names and aliases, states, what the assistant could see, scripts and automations. Home Assistant's own intents, tools, scripts and automations run there; only commands to devices are emulated. Before trusting a replay, the sandbox's version of the assistant's instructions and tools is compared with the recorded one.
- **Friction is measured four ways:** goals that went unmet (the assistant guessed or missed what the user wanted), extra turns, side effects left (a device the assistant changed that no goal asked for, and that didn't end as it started), and your feedback on how the assistant behaved (in the conversation, or in the note you send it in with). A fix improves a goal when it removes some of that friction and adds none: it meets the goal in no more turns, without any of its side effects (judged by code), and it meets a goal the original conversation didn't, takes fewer turns, removes a side effect or follows your feedback. It follows your feedback once no reply still does what you objected to (a small model reads each reply with its tool calls and results, against your own words and the reply you objected to).
- **The baseline comes first.** The original conversation is replayed without the change; it reproduces when, every turn, the devices the assistant changed behave as recorded and every goal ends as it did. A complaint the baseline doesn't repeat is set aside for that test, counting neither way. A model's other variations are kept as notes.
- **The user's own words, in order.** The whole conversation is replayed from its first message, with the user's recorded turns, unchanged and in order; with the change, a turn is left out only when nothing on it is still open: what it asked for is done, or what it complained about no longer happens; a thanks is always said. So a fix that still needs a follow-up counts its turns.
- **Facts for the AI roles.** Before any model replay, code runs each proposed script from the state its goal started in, matches new names the way Assist does, and replays every recorded tool call with the change; calls that now resolve differently are signals to weigh, not proof of a regression.
- **No resampling.** A test is never repeated hoping for a better result; a different plan (a change in how replays are judged, say) is a different test.

## Reviewing changes

- **A separate reviewer** reads each idea with read-only access to the same sessions and devices. It writes [Conventional Comments](https://conventionalcomments.org/) (each labelled, marked blocking or non-blocking, and backed by session evidence).
- **The verdict follows from the comments:** any blocking comment means changes are requested; otherwise the idea is approved.
- **Every blocking comment gets an answer:** the generator marks it resolved, or won't fix with a reason.
- **One reviewer per round.** The reviewer keeps a single conversation across all the drafts of a round, so it remembers what it already approved and asked for.
- **Bounded back-and-forth:** at most 15 reviews per round and 5 per idea, tuned from observed runs.

## Asking you, and applying what you accept

- **Reviewed like a pull request.** Each tested improvement has its own page in the Assist Improver panel: what it gets you, the goals it enables and the turns it saves, the changeset as files with line-numbered diffs, and the conversations it was tested on, with what's only expected. The notification links straight to each one.
- **Your choices:** accept, ask for a revision (with a note on what should change), or decline, with an optional note; tick "Not an issue" when it went fine or you were only trying Assist out. An applied improvement can be undone.
- **All of it or none of it.** Every change is checked before anything is written: a value you changed since the improvement was tested is a conflict, reported instead of overwritten, and Home Assistant validates scripts and instructions first. Changes then go in a safe order (scripts, saved but not exposed; aliases; exposure; instruction lines last), with a journal saved before each write. A failure undoes the steps already done; if Home Assistant stops midway, the next start undoes them and tells you. Every value is read back before the improvement counts as applied.
- **Household knowledge, shared by every assistant.** Household lines go into one place, the household knowledge: lines for everyone, and lines for one person that only their requests see. Every assistant that controls the home reads it: the lines come with Home Assistant's own Assist, so there's no second option to choose, nothing to turn on for each assistant, and no tool offered twice. One switch in the settings turns them off for every assistant (on by default). A line already there isn't added twice.

## Shared knowledge

- **Home Assistant's own rules, upfront.** The generator and reviewer get a short primer on how Assist works (exposure, the tools it has per device type, aliases, scripts) and Home Assistant's published best practices, checked against the Home Assistant sources.
- **Each role gets the context it needs** to understand the household: devices, areas, aliases, what the assistant can and can't do with each device, and the sessions themselves.

## Safety

- **Nothing in your home moves during testing.** Replays run in a sandboxed Home Assistant with no device integrations, and sandbox mode runs against a simulated copy of the home; only commands to devices are intercepted.
- **Sandbox mode is per person.** It affects only the signed-in person who turned it on; others and voice satellites are never affected.
- **Changes are ordinary Home Assistant configuration.** They are shown to you before they apply, nothing runs when they do, and they stay if you uninstall Assist Improver: scripts, aliases and exposure are Home Assistant's own, and uninstalling moves the household knowledge into each assistant's own instructions and switches it back to Assist.

## Cost and data

- **Triage before the deep analysis.** With a triage model chosen in Settings (a small model, set up by default), each conversation is checked first, however many turns it has; one it's sure went smoothly skips the journey analyzer. It's told what a later turn usually means (the user repeating, correcting, answering a question, or telling the assistant how it behaved). Unsure means a full analysis. The check is calibrated on labelled conversations: it must never skip one that had friction.
- **A goal that's already fixed stops early.** The analyzer sees the existing improvements on the devices its conversation touched (found by code) and can read any of them; a goal one of them already fixes is marked as a duplicate, checked by code, and goes no further.
- **Every conversation stays visible.** The generator and reviewer see all recordings, analysed, skipped as smooth, or not analysed yet: the ones with friction upfront, the rest through their session tools, and all of them in the regression replays. Filtering only saves the deep analysis.


- **Your conversations stay in your Home Assistant.** Analysis sends what it needs to the AI model you chose, through Home Assistant's own AI Task.
- **Paid analysis runs only when you allow it.** Cost controls are coarse on purpose, not per-call accounting: the **tests per conversation** setting caps how many fixes each conversation is tested with, each round. Each test replays a conversation on the household's own assistant, so it works as a multiplier on what the assistant normally costs.

## How the AI roles are tested

- **Hand-labelled sessions.** Each role's output is scored by code against labels for real sessions. Hard checks pass or fail; judgement calls (such as how narrowly to ask) are soft findings for a person to read.
- **Three rounds in a row.** A prompt or design change counts only when the role passes three rounds in a row with the same instructions and model, plus a check on sessions it was never tuned on.
- **Seeded cases for the reviewer:** deliberately good and flawed proposals check that it approves the good one and blocks the flawed ones.
