"""The shapes of a hypothesis and a review, checked by the submit tools, and a review's outcome."""

import probatio

KINDS = ("preference", "prompt", "alias", "exposure", "script")

_CHANGE = {
    probatio.Required("kind"): probatio.In(list(KINDS)),
    probatio.Optional("text"): str,
    probatio.Optional("for"): probatio.In(["everyone", "this_user"]),
    probatio.Optional("replaces"): [str],
    # A prompt change edits this assistant's own instructions (its name, as read_assistant_instructions shows it);
    # code adds where Home Assistant keeps it (target: "config entry/subentry") once it's accepted.
    probatio.Optional("assistant"): str,
    probatio.Optional("target"): str,
    probatio.Optional("entity_id"): str,
    probatio.Optional("alias"): str,
    probatio.Optional("expose"): bool,
    probatio.Optional("script_name"): str,
    probatio.Optional("steps"): [probatio.Any(
        {probatio.Required("service"): str, probatio.Required("entity_id"): str,
         probatio.Optional("data"): {str: probatio.Any(str, int, float, bool)}},
        {probatio.Required("stop"): str})],  # A closing stop step: the message Assist relays (HA's advice).
}
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
