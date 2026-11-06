"""The shapes of a hypothesis and a review, checked by the submit tools, and a review's outcome."""

import probatio

KINDS = ("preference", "assistant", "registry", "script", "automation", "scene")  # changes.py says what each is.

_CHANGE = {
    probatio.Required("kind"): probatio.In(list(KINDS)),
    # preference: a household line (text) for everyone or this_user. assistant: lines of one assistant's own
    # instructions (text in place of those in replaces), fewer LLM APIs (llm_hass_api), other options (recommend).
    probatio.Optional("text"): str,
    probatio.Optional("for"): probatio.In(["everyone", "this_user"]),
    probatio.Optional("replaces"): [str],
    # The assistant by its name, as read_assistant_instructions shows it; code adds where Home Assistant keeps it
    # (target: "config entry/subentry").
    probatio.Optional("assistant"): str,
    probatio.Optional("target"): str,
    probatio.Optional("llm_hass_api"): [str],
    probatio.Optional("recommend"): {str: probatio.Any(str, int, float, bool, None, list, dict)},
    # registry: as Home Assistant's registry updates take them: an entity (entity_id), a device (device_id), or an
    # area (area_id alone).
    probatio.Optional("entity_id"): str,
    probatio.Optional("device_id"): str,
    probatio.Optional("area_id"): probatio.Any(str, None),
    probatio.Optional("name"): probatio.Any(str, None),
    probatio.Optional("name_by_user"): probatio.Any(str, None),
    probatio.Optional("aliases"): [str],
    probatio.Optional("hidden_by"): probatio.Any("user", None),
    probatio.Optional("disabled_by"): None,
    probatio.Optional("expose"): bool,
    # script, automation, scene: an item of Home Assistant's editors by its id there, its whole configuration as the
    # editor takes it; an automation turned on or off.
    probatio.Optional("id"): str,
    probatio.Optional("config"): dict,
    probatio.Optional("on"): bool,
}
# What each kind of change is, for the tools that take one.
CHANGE_KINDS = (
    "A change is one of these kinds. preference: a line for the household knowledge (text), which every assistant "
    "reads, for everyone or this_user. assistant: one assistant (its name, as read_assistant_instructions shows it): "
    "text added to its own instructions in place of the lines in replaces (or replaces alone removes them; Home "
    "Assistant must render the result, which may use its template values and functions), and/or llm_hass_api, the "
    "LLM APIs it keeps (only ever fewer); a better setting for any other option goes in recommend, named for the user "
    "to change, never applied or tested. registry: as Home Assistant's registries take it: an entity (entity_id with "
    "name, aliases, area_id, hidden_by, disabled_by: null to enable it, or expose to show it to Assist or hide it), a "
    "device (device_id with name_by_user, area_id) or an area (area_id with aliases). script, automation, scene: an "
    "item kept by Home Assistant's editor, by its id there (a script's object id, an automation's or scene's id), "
    "with its whole configuration in config as that editor saves it, Home Assistant's own format and checks; a new "
    "script or scene is exposed to Assist unless expose is false; on (true or false) alone turns an automation on or "
    "off. A script's or automation's description says what it does (Assist reads a script's to decide when to run "
    "it); a script may end with a stop step whose message Assist tells the user. No action, target or field but "
    "one taking a plain value (text, number, select, boolean, a time, a colour) may take its value from a template, "
    "a script field or trigger data (a field chooses between fixed actions and devices only as branches on its "
    "value: choose or if); nor may variables passed to another script or automation, or an event's data; no step "
    "calls conversation.process; a webhook trigger stays local_only.")
_EVIDENCE_REF = {probatio.Required("session"): str, probatio.Optional("turn"): int, probatio.Required("note"): str}
_HYPOTHESIS = {
    probatio.Required("title"): str,
    probatio.Required("goals"): [{probatio.Required("session"): str, probatio.Required("goal_turn"): int}],
    probatio.Required("cause"): str,
    probatio.Required("changes"): [_CHANGE],
    # The sessions the rule agrees with, and any it contradicts (each with what happened there).
    probatio.Required("support"): [_EVIDENCE_REF],
    probatio.Required("conflicts"): [_EVIDENCE_REF],
    probatio.Required("expected"): str,
}
HYPOTHESES = probatio.Schema({
    probatio.Required("hypotheses"): [_HYPOTHESIS],
    # Goals with friction left unfixed, each with why the friction is outside the assistant's control.
    probatio.Optional("not_fixed"): [{probatio.Required("session"): str, probatio.Required("goal_turn"): int,
                                      probatio.Required("why"): str}],
    # Goals with friction an existing improvement (waiting for review, or applied) already addresses.
    probatio.Optional("already_addressed"): [{probatio.Required("session"): str, probatio.Required("goal_turn"): int,
                                              probatio.Required("improvement"): str, probatio.Required("why"): str}],
    # Instead, one question only the household can answer, whose answer would change what it proposes.
    probatio.Optional("question_for_user"): str,
})
# Review comments in the Conventional Comments style (conventionalcomments.org), outcomes as in GitHub reviews.
LABELS = ("issue", "suggestion", "todo", "nitpick", "question", "thought", "praise")
_EVIDENCE = {probatio.Optional("session"): str, probatio.Optional("turn"): int, probatio.Optional("entity_id"): str,
             probatio.Optional("note"): str}
REVIEW = probatio.Schema({
    probatio.Required("comments"): [{
        probatio.Required("label"): probatio.In(list(LABELS)),
        probatio.Required("decorations"): [probatio.In(["blocking", "non-blocking"])],
        probatio.Required("subject"): str,
        probatio.Optional("discussion"): str,
        probatio.Optional("evidence"): [_EVIDENCE],
    }],
    probatio.Optional("revised"): _HYPOTHESIS,
})


def blocking(review):
    return [c for c in (review or {}).get("comments", []) if "blocking" in c.get("decorations", [])]


def verdict(review):
    """A review's outcome, derived from its comments so the two can't disagree: request_changes or approve."""
    return "request_changes" if blocking(review) else "approve"
