# End-to-end journeys

Assist Improver as a household uses it: a real Home Assistant 2026.11.0 server running the checkout's
`custom_components/assist_improver`, driven through Home Assistant's real frontend in headless Chromium (Playwright).
Each journey types to Assist in its dialog, opens the sidebar's Assist Improver panel, and checks what the screen shows
and what Home Assistant holds (aliases, states), at phone size (402x874) and desktop size
(1280x800). Not shipped; separate from `fixture-test/` (whose conftest blocks network and subprocesses).

## Run it

Set up once as `CONTRIBUTING.md` says, then:

```sh
tools/e2e/.venv/bin/python -m pytest tools/e2e -q -p no:cacheprovider -n 8
```

Home Assistant runs on the project's `.venv` (`fixture-test/requirements-lock.txt` installs it with its frontend); the browser is
`tools/ui/browsers`. In a worktree, both are found in the checkout it hangs from. The whole suite takes about five
minutes on eight workers (`-n 8`, pytest-xdist), several times that one test at a time (without `-n`), and longer on a
busy machine, where a journey can time out waiting for a run; `tools/check.sh` runs it beside the other three suites.

Options: `E2E_KEEP=1` keeps each test's Home Assistant config folder, `E2E_SHOTS=1` saves the screenshots journeys
take (`page.shot(name)`), `E2E_RUNS_DIR` says where the folders go. A failing test keeps its folder either way, with
Home Assistant's log, a screenshot and the stand-in model's requests (`e2e-model-requests.jsonl`), and prints where.

## What's real and what's a stand-in

- Real: Home Assistant (its frontend, Assist pipeline, conversation agent, anthropic integration, AI Tasks, intents,
  entity registry), Assist Improver as shipped, its replay workers and sandboxes, and the browser.
- The one stand-in is the model: `fake_anthropic.py` answers Anthropic's Messages API on 127.0.0.1 (the anthropic SDK
  reads `ANTHROPIC_BASE_URL`; Home Assistant passes no base URL of its own). It is deterministic and acts only on what
  each request carries. The household's assistant calls an intent tool when a name it's given matches one in Assist's
  prompt, or a household knowledge line there says which device it means, says it can't find it otherwise, and files the
  conversation when asked to file an improvement. Asked whether a device is on, it guesses the other way, unless a
  household line asks it to check, when it reads Home Assistant's live context and answers from it. The improver's AI
  roles (triage, analyzer, proposer with its reviewer and Simulate stamps, editor, the judge of replies) are told apart
  by the tools or output each request asks for, and each answers through its real tools, so the product's own checks
  apply. The proposer proposes a household line for a note that asks Assist to remember something, for an objection to
  Assist's promises, or for a question it answered wrongly (check before answering), a script for a note that asks for
  one (Assist runs an exposed script named as the device was said), and an alias otherwise; the judge
  of replies checks an answer against the home it's given. The household's wording is a knob: an assistant whose
  instructions ask it to say what to expect next promises it, and a household line asking for only what it did makes it
  say "Done.".
- Devices are simulated: template lights and a switch backed by helpers (`config/configuration.yaml`), in rooms,
  exposed to Assist. Nothing is called the "reading lamp", so asking for it is a real friction an alias fixes.
- Test-only (`config/custom_components/e2e_support`): Home Assistant ends an idle chat session after 6 seconds instead
  of 5 minutes, so a conversation ends, and the recorder closes it, soon after its last turn.

## How it's set up

Each session (each pytest-xdist worker is one) sets up one household's Home Assistant (`golden` in `conftest.py`), in
a folder of its own, as a household would: onboarding with a test user (generated, never printed), the devices in
rooms and exposed, the anthropic integration through its config flow (the stand-in answers its key check) with the
assistant on an explicitly chosen model, Assist on that assistant, and Assist Improver through its own config flow
(its roles on the anthropic AI Tasks it adds). Each test then gets its own Home Assistant
started from a copy of that folder on a free port (a few seconds), so no journey sees another's conversations or
improvements, and it's stopped, with its replay workers, when the test ends.

The harness keeps to the machine: Home Assistant and the stand-in listen on 127.0.0.1 only, on free ports (Home
Assistant 2026.10 keeps its HTTP settings in `.storage/http`, so the harness writes them there before every start); the
browser refuses any request beyond 127.0.0.1; no model call is paid; no fixed sleeps.

## Journeys

- `test_file_from_assist.py`: a name Assist doesn't know, filed from the Assist dialog, reaches Your review on its own
  (also on an assistant left on Home Assistant's recommended model settings).
- `test_decisions.py`: accept and undo (an alias; a script, which the panel saves and deletes through Home
  Assistant's script editor; and a scene and an automation, whose review shows the automation's simulated week), ask
  for a revision with a note, decline (Not an issue, or with a note), and an
  accept that meets a conflict (the device's entity ID changed in Home Assistant): it says why and asks first, and
  Send it back to resolve takes the conversation in at Propose for a new round, on a run of its own.
- `test_runs.py`: Run now on what nobody sent in, a blocked stage holding a conversation until released, the daily
  schedule, Settings showing each stage in full, and a home page left open following a run that starts after it
  loaded (filed from Assist on another screen).
- `test_household.py`: archive and unarchive, send in again, settings that stay as set.
- `test_notifications.py`: a fix filed from Assist waits in Your review, and one summary notification lists what
  waits ("1 improvement to review", then "2 ..."), opens each review, follows each decision and clears.
- `test_conversations.py`: the conversations list. Delete from Assist Improver from the ⋮: it asks first, Cancel keeps
  it, and once confirmed it's gone from the list, the run and Home Assistant's stores. Conversations are found by what
  was said or replied, by when, by day (Today is today's) and in each order; the older ones are recorded now, then
  moved back on disk while Home Assistant is stopped. A run on two picked conversations asks first and moves only
  those, then the Status filter finds them by where they stand.
- `test_knowledge.py`: household knowledge (Your household's lines): added, edited and deleted from Settings, also
  when changed on another screen; what the household's assistant reads of them (everyone's, then the speaker's, never
  another person's); and an accepted improvement's line, which shows in the modal and goes again on Undo.
- `test_reactions.py`: what Alex's later turns reacted to. Assist promises what happens next (its instructions ask it
  to); Alex thanks it, objects, then files it: the fix (a household line) has Assist say only what it did, so the
  objection is met, its turn and the filing aren't needed (the thanks is said all the same), and the review lists his
  words as fixed. A request said again after Assist failed is a step: the fix (the name he said) closes it, and a
  filing made once the request was met is set aside, said with the change too.
- `test_questions.py`: what Alex asked to know about the home. Assist answers without looking and he asks again; the
  fix (check before answering) has it answer right the first time, and the review shows the question met.

A test that fails because the product falls short keeps asserting the right behaviour (no skip, no xfail).

## Writing a journey

Take `page` (the screen, at both sizes) and `ha` (Home Assistant's state): see the docstrings of `Screen` and
`HomeAssistant` in `conftest.py`. `test_file_from_assist.py` is the pattern: act on the screen, wait on what it shows
(`expect`) or on Home Assistant (`ha.wait`, `ha.wait_for_run`), and assert both.
