# Assist Improver roadmap

**Rewritten 2026-10-21 with Kai.** It replaces the roadmap agreed earlier the same day. The product specification is still the requirements contract. This rewrite changes the order of work: the AI stages that find, explain and fix interventions are now milestones of their own, each scored against journeys Kai has labelled.

## The idea, in Kai's words

> "identify outcomes (i model them as destinations, in a certain space, like full-self driving) that had interventions (just like driver interventions in FSD), figure out if the interventions were avoidable or not (candidate hypotheses for getting from source to destination without interventions), test them, and propose changes." (Kai, 2026-10-21)

> "if it were run for my case, it would probably have created a bunch of scripts for me, and added the prompt personalization that we did by hand" (Kai, 2026-10-21)

> Uninstalling is like laying off a driver-assist team: what it built stays in people's cars, but gets no further updates. (Kai, 2026-10-21, paraphrased)

Every user turn spent correcting the assistant is an intervention. The product watches drives (journeys), works out where each was meant to go (the destination), finds the avoidable interventions, and ships changes that remove them. The headline measure is interventions over time, trending down.

## The pipeline and its roles

| # | Stage | Role | Who does it | Output |
|---|---|---|---|---|
| 1 | Trace intake | Recorder | Code (built) | Conversations, tool calls, device state, settings |
| 2 | Trace digest | Distiller | Code | A compact view of one session a model can read (raw recordings are 3–5 MB) |
| 3 | Journey segmentation | Journey analyzer | **AI**, checked by code | The user's goals (device states, sequences or facts), each with the turn that first asked for it, sub-goals serving them, and a question for Kai when unclear. Code derives the extra turns (interventions), whether Assist reached each goal, and side effects left behind. It answers through a tool that rejects invalid answers (unknown devices, states a device doesn't have) with the reasons (Kai, 2026-10-22) |
| 4 | Themes and hypotheses | Hypothesis generator | **AI** | Friction grouped into themes across journeys; ranked hypotheses, each one coherent change |
| 5 | Candidate review | Reviewer | **AI**, separate from the generator, using a summarized model of the household's journeys | Keep, revise or drop each hypothesis, including a sanity check for regressions against what the household usually does, before any money is spent testing it |
| 6 | Implementation | Implementer | **AI**, checked by code | Concrete HA configuration: exposure, aliases, scripts, prompt or preference lines |
| 7 | Validation | Assistant under test, simulator, judge | Code plus **the user's own assistant** (paid replay; harness built) | Baseline and counterfactual replays; strictly better (goals met, turns per root goal, side effects left), or not |
| 8 | Changeset proposal and apply | Change manager, review interface | Code; the user decides | Before/after evidence; accept, request revision or decline; three-way merge on apply; nothing executes |

The starting preset for the AI roles is Opus 5.5 with effort chosen per role, overridable by power users. Generator, reviewer and judge stay separate, so the system never grades its own work. Destinations are fixed before any trial. The review happens once, at the idea stage: "it's more useful to have the review done at the ideation stage, because it'll likely be the ideas that cause problems, less the implementation, imo. but we may realize later that it could be worth checking the implementation too." (Kai, 2026-10-21)

## How every AI stage is validated

- **Labelled journeys** are the ground truth: Kai's own sessions, segmented and labelled with Kai ([format](LABELS.md); labels kept privately). The set grows as Kai uses the assistant.
- **Offline first.** Each AI stage gets recorded model outputs as fixtures, so its plumbing, schemas and failure paths are tested without spending.
- **Then scored.** Each AI stage is run on the labelled journeys, repeated a few times to see how stable it is. It is scored against the labels, with the pass rule written down before the run.
- **The set is small.** It can catch clear failures; it can't prove a stage is reliable. Results say so.

## We are here

**Updated 2026-11-06: 0.40.35.** Every stage runs end to end from the panel, every day in the maintainer's household. Since 0.33 each turn keeps its whole record (every action and what it changed); replays run the household's own setup in a sandboxed Home Assistant, with one stand-in integration for every device and every call beyond the home; proposals cover scripts, automations, scenes, names, aliases, areas, exposure and assistant settings, applied through Home Assistant's own editors and undone the same way; each goal is judged by everything it asked for and by actions; and every suite runs against Home Assistant's official image and HA OS. Next: a made-up household with its own Home Assistant for tests anyone can run.

## Milestones

Each milestone ends with something Kai can check. "Validated when" is the completion test.

### 0. Feasibility — complete
- **Delivered:** offline proof that HA's own provider code can be driven with recorded context while every tool call is intercepted.
- **Validated:** baseline and candidate requests differed only in the intended change, with no paid calls and no real actions.

### 1. Recorder — complete
- **Delivered:** an installable custom integration that records conversations, tool activity, device state and settings, kept across restarts.
- **Validated:** real conversations captured on Kai's HA and preserved through a restart; unsupported cases identified explicitly.

### 2. Replay harness and reproduced baseline — complete
- **Delivered:**
  - case records with destinations and interventions;
  - replay through the existing Anthropic integration with frozen context and simulated devices;
  - a code judge;
  - stop rules: no destination means no trial, non-reproduction stops, no resampling.
- **Validated:** a recorded conversation's baseline reproduced on its first paid attempt, with original settings unchanged.
- **Gap:** journeys and destinations were grounded by hand. That is the next milestone.

### 3. Labelled journeys, trace digest and AI-role runtime — complete
- **Delivered:**
  - **Label format**, with Kai's labels for the existing recordings: journey boundaries, source state, destination, interventions, whether each was avoidable, and expected themes.
  - **Distiller:** turns a recording into a compact digest, with a recorded size budget. It covers the conversation, tool calls and results, state changes for the devices involved, and an index of what Assist can and cannot do (exposed entities, their operations, unexposed scripts and scenes).
  - **Runtime for AI roles:** each role calls a chosen AI Task entity through HA's public interface (decision below), with structured output, recorded-output fixtures for offline tests, and usage recorded where the provider reports it.
  - **Guided AI Task setup** in the improver's setup screen. It lists the household's AI Task entities and suggests one. It warns when the chosen one is weak for these roles, for example Anthropic's automatic default of Claude Haiku 4.5 at low effort with a 3,000-token output cap. It offers to create a recommended entry (Opus 5.5, higher effort and output limit) through that integration's own add-AI-Task flow. If that can't be done cleanly, it links to HA's "Add AI Task" screen instead.
  - **Scorer:** compares a stage's output to the labels.
- **Validated when:**
  - Kai has confirmed the labels.
  - The scorer gives the hand labels a perfect score and catches deliberately wrong labels.
  - Every digest stays within budget and still contains everything the labels depend on.
  - On a fresh install, the setup screen finds the existing AI Task, warns about a weak one, and gets the user to a recommended entry in a minute or less. It is checked offline and then on Kai's HA.

### 4. Journey analyzer (AI) — complete
- **Delivered:** an AI role that turns a digest into journeys, source and destination states, and interventions marked avoidable or not. When it cannot establish a destination, it produces "Help your assistant learn: what would you have liked to happen?" instead of guessing.
- **Validated when, on the labelled set:**
  - Requests made at different times split into separate journeys, not one invented "testing" journey.
  - The interventions are the turns the labels mark.
  - One-turn successes have no interventions.
  - A destination is stated in the device's own modes, and an unreached one is found.
  - An ambiguous session yields a question, not a destination.
  - Results hold across repeated runs.
  - **Held-out check against overfitting** (Kai, 2026-10-22): Kai labels sessions the prompt was never tuned on, and the analyzer is scored on them once, after it passes the tuning set. They are never used for tuning.

### 5. Themes, hypotheses and candidate review (AI) — complete
- **Delivered:**
  - The **hypothesis generator** groups friction across journeys into themes before proposing anything. It ranks hypotheses by expected future intervention reduction and generalisation, choosing the smallest useful change.
  - A separate **reviewer** keeps, revises or drops each hypothesis. Its review includes a regression sanity check against a summarized, statistical model of the household's journeys, which is inspectable only for debugging.
- **Validated when, on the labelled set:**
  - The generator proposes the fixes the labels expect, such as exposing an existing script or naming a missing operation.
  - It proposes nothing for the successful journeys and invents no devices or preferences.
  - The reviewer rejects seeded bad candidates, such as combining two requests Kai made at different times into one, or a fix whose cause the sessions don't show (a claimed mishearing).
  - The reviewer flags a seeded regression, such as an alias that makes "the fan" ambiguous, and passes the sound hypotheses.
  - Results hold across repeated runs.

### 6. Implementation and validation of candidates — complete
- **Before starting:** build the two planned tools below, any-provider replay and practice chat.
- **Delivered:**
  - The concrete HA change. The hypothesis generator doubles as the implementer, since it already writes complete changes (script steps, aliases, exposure, preference text) that code validates on submit; no separate AI role (Kai, 2026-10-23; its tool budget has ample room, about 20 of 1000 tool rounds used). Code turns them into HA's configuration format, checks them with HA's own schema and the script simulator, and rejects unsafe or unmodelled changes before replay.
  - Candidates are validated through the existing harness: the baseline first, then up to three counterfactual replays per goal per cycle (the tests-per-goal setting: a multiplier on the user's normal assistant spend, since each replay costs about what the conversation did; Kai, 2026-10-24) using the user's own assistant and settings, then the code judge.
- **Validated when:**
  - An AI-generated candidate reaches the destination in fewer original turns in a paid replay.
  - A weak candidate is correctly judged "not improved".
  - A non-reproducing baseline stops the trial.
  - No real device acts at any point.

### 7. Changeset proposal and first applied improvement — complete
- **Delivered:**
  - **Changeset proposal:** hypothesis, affected journeys, concrete changes, before/after examples, tested gains versus expected wider benefit, and limits.
  - **Review in HA:** accept; request revision; decline; the presets "things were fine as-is" and "changes need improvement"; and the learning question, with a one-tap "ignore, that was a test" so questions about tests and misfires are easy to dismiss (Kai, 2026-10-22).
  - **Apply:** three-way merge, nothing executes, actual status reported.
- **Validated when:**
  - Kai accepts it in HA, and the scripts are shared without running.
  - Kai's next ordinary request of that kind succeeds in one turn.
  - This is the first improvement produced by the AI pipeline end to end.
- **Not included for now:** a second review of the implemented change. Add one if testing shows implementations, not ideas, cause problems.

### 8. Fresh-start check of the whole pipeline — not run as written; the end-to-end tests cover its plumbing
- **Delivered:** a repeatable end-to-end run on the labelled sessions, starting from the configuration as it was before any hand-made fixes.
- **Validated when:**
  - The pipeline rediscovers the known-good changes made by hand.
  - It proposes nothing harmful, and Kai can compare its proposals with what was done by hand.
  - Where traces exist for the prompt personalization, it rediscovers that too.

### 9. Revise and maintain — complete
- **Delivered:**
  - **Feedback loop:** revision requests and declines feed the next cycle.
  - **Corrected diagnoses:** "fine as-is" corrects the understanding and closes the case.
  - **Merge conflicts:** return as next-cycle input.
  - **Own artifacts:** the improver modifies, consolidates or retires its own changes, inspecting manual edits first.
  - **More kinds of change:** prompt, preferences and memory with a durable HA representation; scenes, automations and aliases.
- **Validated when:**
  - A manual edit is merged, not overwritten.
  - A real conflict comes back as feedback.
  - A duplicate becomes a no-op.
  - A revised proposal uses the earlier feedback.
  - A partial apply is shown as partial.

### 10. Scheduled cycles and controls — complete, except the interventions graph (on hold)
- **Delivered:**
  - **Scheduling:** off by default, daily or weekly, and Run now.
  - **Controls:** the counterfactual count as the coarse spending knob (three by default), and transient retries only, three attempts at most.
  - **Reporting:** one summary notification when proposals are ready, usage where available, and the interventions-over-time graph, normalised by journey volume.
- **Validated when:**
  - A week of real cycles produces no duplicate proposals.
  - The allowance is never exceeded.
  - No trial runs without an established destination.
  - The graph separates actual use from predicted gains.

### 11. Household pilot and public experimental release — under way
- **Delivered:**
  - A measured pilot on Kai's household with defaults tuned from real data.
  - Backup and restore verified.
  - Uninstall stops the improver while every accepted improvement keeps working.
  - No household assumptions in code.
  - Repository name, license and docs.
- **Validated when:**
  - ~~Interventions trend down over the pilot, measured, not predicted.~~ On hold (Kai, 2026-10-24): no headline metric until there's one that helps.
  - A clean backup restores, and uninstall leaves improvements working. Done on the demo HA, 2026-10-24.
  - Kai approves publishing.

## Planned tools

Both are **built before Milestone 6 starts** (Kai, 2026-10-22): Milestone 6 is the first to replay fixes, and Kai then wants many practice chats to test how well the improver holds up. **The practice chat moved up** (Kai, 2026-10-22): it's built now, mid-Milestone 4, so tuning can continue on practice sessions with less concern over cost.

### Any-provider replay
- **Why:** users should be free to choose their assistant's model provider, such as a cheaper model through OpenRouter (Kai, 2026-10-22).
- **Status (2026-10-23, 0.15.5):** built, tested offline, and checked on HA: a DeepSeek baseline reproduced and a candidate met the destination in 1 turn instead of 3. Since 0.15.5 replays run on practice mode's engine (Kai, 2026-10-23): HA's own tools and intent logic act on a simulated copy of the recorded states, and only device commands are emulated (`simulation.py`); a practice recording's snapshots get its earlier simulated changes laid on top. A baseline turn reproduces when its effects on the home match the recording, however the model words its tool calls; the old receipt-bound simulator only handled calls naming one device. Replays use the user's own assistant, as recorded, with its recorded settings copied onto it per run (nothing for the user to configure). OpenRouter is tested offline through HA's real OpenRouter code (baseline and candidate, no device action); other providers replay with a "provider_not_yet_tested_for_replay" note. 20 of the 25 replayable recordings ran on DeepSeek through OpenRouter. Kai expects reproducibility may vary across DeepSeek's OpenRouter upstream providers (2026-10-23); the baseline check stops on a non-reproducing baseline.
- **Before:** the baseline and candidate replays refused anything but HA's Anthropic assistant. That was caution, not necessity: it was the only provider tested end to end. The core (HA's shared chat log plus simulated devices) is provider-neutral, and an early offline test already ran the simulation through HA's OpenRouter assistant. The Anthropic-specific parts are copying the assistant's settings per run (including its model metadata) and Anthropic's hosted tools, such as web search.
- **Delivered:** any provider can replay. Anthropic and OpenRouter are tested; others run with a clear "not yet tested with this provider" label instead of being refused.
- **Validated when:** a recorded OpenRouter conversation reproduces as a baseline, a candidate replays against it, and no real device acts.

### Practice chat
Practice mode was later removed; replays run in a sandboxed Home Assistant (`sandbox.py`).

- **Why:** Kai wants to generate realistic sessions, for example after switching an assistant to a cheaper model, without speaking to the app and without touching real devices (2026-10-22).
- **For everyone:** "it's an easy way for the lazy, like me, to do the training upfront so we don't have to experience the friction when we need the assistant to actually perform" (Kai, 2026-10-22). So it's a product feature, not just a development tool.
- **Delivered** (design agreed with Kai, 2026-10-22; revised the same day from a separate "Practice" assistant to a mode):
  - **Practice mode, a switch.** Turning it on sandboxes only your own conversations, with whatever assistant you normally use, so practice always mirrors your production assistant and the learning transfers. Nothing extra appears in Assist's assistant list. Others in the house, and requests without a signed-in person such as voice satellites, are unaffected.
  - It stays on until you turn it off, even across restarts, so it never switches off by surprise (Kai, 2026-10-22). A notification shows while it's on. The switch is on Assist Improver's device page.
  - **HA runs its own logic; only the drivers are emulated** (Kai, 2026-10-22, from 0.7.0). HA's real tools and intents decide which devices a request targets, how it's scoped and what it replies, against a copy of the home that starts from the real states. Only the commands to devices (for example `light.turn_on` with a brightness) are caught and emulated on the copy (`devices.py`). Changes reset with each session. A command without an emulation, such as running a script, is reported to the assistant as done and flagged. Tools from outside HA never run in practice. **Known gap:** links between devices aren't emulated; for example, switching one device off can switch off a linked one in real HA, but not in practice. The first version re-implemented HA's intent logic and got scoping wrong twice (sensors "turned on", a speaker given a brightness); this design removes that layer.
  - **No goals asked for.** The learning loop works out what you wanted, as it does for real use. (A "Goal:" message was tried in 0.6.0–0.6.4 and removed: specifying goals in the chat wasn't seamless, Kai, 2026-10-22.) When sessions need labels for scoring, Kai and the agent label them together.
  - Sessions are recorded and marked as practice, so the improver learns from them without mixing them up with real use.
  - Each practice message costs the same as a normal one.
- **Status (2026-10-22):** practice mode built in 0.6.2. Offline tests run HA's real Anthropic and OpenRouter assistants with HA's real tools and fail on any device action. 0.6.0's "Practice (simulated home)" assistant is removed on upgrade.
- **How Kai will use it:** re-enact his labelled scenarios in practice; accept the improvements practice finds; then undo them and check whether learning from real use (post-training) rediscovers them.
- **First try (2026-10-22):** a labelled scenario re-enacted with DeepSeek V4 Flash through OpenRouter. Recorded completely as practice; the simulated devices changed and the real ones never did.
- **Limits:** simulated devices never really change, so long back-and-forths drift from reality. Single requests and short corrections are the intended use.
- **Validated when:**
  - A practice session with the Anthropic assistant and one with an OpenRouter model (such as DeepSeek V4.1 Flash) are both recorded completely.
  - No real device changes state during either.
  - The journey analyzer reads practice sessions like real ones.

## Opt-in data sharing: after the integration is built

Kai, 2026-10-23: one household only has so much to learn from, and other households will hit problems we can't see or debug. Both steps come after the integration itself is built.

1. **Share a problem report.** A per-session "report a problem" (and the integration's *Download diagnostics*) builds one file with what's needed to debug: the session's recording or digest, the analysis and hypotheses, versions, the AI Task setup, and device and area details. It is **not redacted**: home address, people's names, device names and aliases stay in, because personalisation and context are often exactly why something didn't work. The user sees exactly what the file contains before anything leaves their home, and sends it themselves (for example attached to a GitHub issue from a template that asks what went wrong).
2. **Ongoing opt-in sharing.** A revocable switch to share sessions continuously, to improve the product for everyone, with a clear retention policy and a hosted endpoint. Shared sessions become labelled test cases and held-out sets from other households, which is what makes the analyzer and generator generalise, and the best check that synthetic users are representative.

Off by default; private and working fine stays private and working fine.

## Stretch goals: after everything works

Not part of the prototype or MVP; picked up only once milestones 0–11 work.

- **Journeys that span sessions.** The spec allows them, but Kai has none and expects few (2026-10-22). Until then the journey analyzer reads one session at a time and never links a journey to an earlier one.
- **Experience, overnight 2026-10-23/24** (Kai: simple, optimized by default, one thing at a time): the user stories and principles are in `docs/DESIGN.md` ("Experience"). Built: Run now and a schedule (off by default); runs leave out goals that already have an improvement and link a revision to the one it revises; the panel shows the one next thing (to review, else a run to start, else all caught up); after a decision the next review is one click away; Repairs and the panel offer household knowledge to assistants added later; Getting Started trimmed to five steps with screenshots from a demo home (`tools/demo`).
- **Quick check before analysis** (Kai, 2026-10-23): built in 0.19.0 (`triage.py`, Settings → Triage). Next: choose a local decision model and calibrate it with `tools/triage_check.py` on the labelled sessions (no friction session skipped). When the history grows to hundreds of sessions, `list_sessions` should take a search and page its results.
- **Screenshots for Getting Started** (Kai, 2026-10-23): generated, not captured by hand, by `tools/demo/screenshots.py` from a demo household (made-up devices and conversations, so nothing personal). Shares the synthetic household with the dev-tools item below.
- **Dev tools for other developers** (Kai, 2026-10-23). One public repo for the integration and the dev tools: HACS installs only `custom_components/assist_improver/`, so `evaluation/` (scorers, stand-in harnesses), `fixture-test/`, `tools/` and `docs/` (DESIGN, the label format, CONTRIBUTING) can sit beside it. Built: maintainer tools read the household's data, HA host and SSH key from a private folder outside the checkout (`tools/private.py`), and tests that need real data skip without it; the privacy scan (`tools/leak_guard.py`) checks every commit pushed. Still needed: a small synthetic example household (recordings and labels) so anyone can run the scorers, stand-ins and tests. The dev history stays private (it holds household details), so the public repo starts from a clean commit.

## Decided for Milestone 3

- **The AI roles use the household's existing Anthropic integration** (Kai, 2026-10-21). Its "Claude AI Task" entity is part of that integration and uses the same key. HA 2026.10.2 supports structured output through it. Roles call HA's public AI Task interface (Kai chose this option, 2026-10-21). They start with the one existing "Claude AI Task" entry for every role. A second entry, such as "Improver – high effort", is added only if testing shows a role needs a different effort. This works with any provider's AI Task, OpenRouter included.
- **Kai provides the labels** (2026-10-21: "im down to provide labels"), starting with the 2 unlabelled recordings.

## Kept as is

Rules that stay:
- no real device actions in replays;
- destinations established before trials;
- no simulated user;
- strictly better: better on goals met, turns per root goal or side effects left, and worse on none;
- no resampling to get a convenient baseline;
- household data stays out of the repository.
