# Journey labels

Kai's labels are the ground truth that each AI stage is scored against (roadmap Milestone 3). Labels are household data: they live in the private folder (`tools/private.py`) as `labels/<recording-id>.json`, never in git. Labels for practice sessions live in its `labels-practice/` (`"practice": true`). They were the held-out set until their one scoring; they are now part of the tuning set.

## One file per recording

```json
{
  "recording_id": "c0ffee12…",
  "labelled_by": "Kai",
  "confirmed": "2026-10-21",
  "goals": [
    {"turn": 1, "summary": "Open the gate (press: off, then on)", "kind": "sequence",
     "entity_id": "switch.gate_relay", "states": ["off", "on"], "reached": true},
    {"turn": 2, "summary": "Porch light on", "kind": "state",
     "entity_id": "light.porch", "state": "on", "reached": true}
  ],
  "optional_goals": [],
  "sub_goals": [{"turn": 4, "summary": "Less wordy", "kind": "conduct", "serves": 2, "about": 2,
                 "quote": "shorter, please"}],
  "not_requests": [{"turn": 3, "what": "test utterance"}],
  "notes": ""
}
```

## Fields

The labels describe only what the user wanted (Kai, 2026-10-22). Everything else is derived by code from the recording: the extra turns (interventions) are the real requests that started no goal, `reached` is whether Assist's own changes reached each goal, and side effects are devices Assist changed that no goal asked for and that Assist didn't leave as they started. Fixes belong to the hypothesis stage, which looks across sessions.

- **goals:** one per thing the user wanted, per device (an action holds all the devices it's for):
  - `turn`: the turn where they first asked for it. Several things asked for at once share a turn. A later turn that corrects, clarifies or adds a step adds no goal. Asking again after a goal was reached is a new goal.
  - `kind`: `state` (`entity_id`, `state`, and `brightness_pct` only if they asked for a brightness), `sequence` (`entity_id` and `states` in order, such as a relay pressed off then on) or `action` (something done that leaves no device in a new state: `outcome` `announce`, a message spoken on devices; `message`: the words to speak, as they said them; `entity_ids`: every device it's for, in one goal; scored by its outcome, devices and message words, whichever service does it) or `fact` (`fact`: what the answer needs, never the answer; `entity_ids`: the devices it's about, when it's about the home; `quote`: the form they asked for it in, in their words, if any; only its turn is scored, with the devices a soft finding).
  - `or_state` (optional, a state goal): a list of other end states the label accepts where a household fact the label rests on isn't known, such as whether turning a device off puts it in standby or cuts its power (Kai, 2026-10-29).
  - `reached`: the labeller's answer, used to test the code that derives it, never compared with a model.
- **ask goals:** where the user's words left settings open (a heater with no temperature, "warm the room up"), the analyzer should leave them open for the user instead of guessing: an ask goal lists the controls they should set (`entity_ids`), as narrow as their words allow. In a label, an ask goal has `must_include` (the controls they actually wanted), `allowed` (the controls an ask may cover, such as the room's heating controls) and `intended` (what they meant). Asks are judgement calls, so they give soft findings for a person to read (too narrow, too broad, guessed, a free-text question instead), never a pass or fail (Kai, 2026-10-22). A guess on an allowed control is a finding, not an invented goal.
- **any_of:** a state or sequence goal may name several devices instead of `entity_id`, when the user would accept any of them; the answer must have the goal on at least one (Kai, 2026-10-23).
- **optional_goals:** goals an answer may list without being required, such as a fan that was already on.
- **sub_goals:** what exists only because of an earlier goal (DETAILS P2.3.2), with `serves`: the turn of the goal it belongs to, and `kind`: a part or repair (`state`, `sequence` or `action`, as a goal: another device asked for at once, or putting right one Assist changed), `step` (a turn the user spent only because the goal wasn't met yet: repeating, correcting, answering Assist's question, asking what it can do, telling it how), `conduct` (what they objected to in what Assist said or did in one turn: `about`, that turn, or `"all"` in the note, or a list of the turns their words leave open; `quote`, their own words, any span of them), `wrap-up` (filing it, giving up) or `thanks`. Parts and repairs are scored as goals are, by turn, device and value, whichever device the answer makes the turn's root. Each reaction is scored by its kind at its turn under the same root goal, a conduct goal about the same turn (one each); quoting another span of their words is a soft finding. A model's sub-goal can never stand in for a labelled goal: a turn where one starts needs a goal of its own.
- **note:** the note the conversation was sent in with, if any, as the analyzer gets it (its sub-goals sit at the turn after the last one said).
- **not_requests:** turns that aren't requests, such as a test utterance, a speech misfire or small talk. `also_acceptable` lets the scorer also accept the turn read as a fact goal (for example a speech test), and `question_acceptable: true` a question back to the user where the session alone can't show what the turn was (Kai, 2026-10-22); either only if no device goal was made of it. `any_without_interventions: true` accepts any reading that leaves the turn not extra, since it can't lead to a fix (Kai, 2026-10-22). While a question is open, the turns without a goal are pending the answer, not extra.
