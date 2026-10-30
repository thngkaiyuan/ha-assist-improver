"""The proposer's stamps (SPEC.md P0.4, P0.9): an idea goes on to Write up only with the reviewer's approval and
Simulate's validation, each on exactly its changes, and the proposer collects both while it works. The reviewer's is
ask_reviewer (hypotheses.tools); Simulate's is test_idea, here: one test of approved changes on the proposer's own
conversation, from the round's tests, which the proposer spends as it sees fit. Approval comes first, so changes that
validate have both stamps: in a real run a proposer collected each stamp on a different wording of one idea."""

import probatio

from .cases import digest
from .hypotheses.schema import _HYPOTHESIS, verdict
from .hypotheses.tools import StampTool


def of(reviews, tests, hypothesis):
    """The stamps an idea holds, one rule wherever they're read: Review, when the reviewer's latest review of exactly
    its changes approved them; Simulate, when a test of exactly its changes validated. Its title only threads its
    discussion with the reviewer."""
    changes = digest(hypothesis.get("changes"))
    verdicts = [r.get("verdict") for r in reviews if digest(r["hypothesis"].get("changes")) == changes]
    return {"Review": bool(verdicts) and verdicts[-1] == "approve",
            "Simulate": any(t["validated"] for t in tests if t["changes"] == changes)}


def result(test):
    """What the proposer reads of a test: whether it validated, the measures it made better, the goals met without
    the change that weren't with it, each root goal both ways (a question still unanswered with the change, with
    why), the side effects left both ways, what's still open with the change (each sub-goal unmet, with why) and what
    couldn't be told (an objection or a question set aside: the replay without the change didn't show the trouble
    again)."""
    before, after, judged = test["before"], test["after"], test["judged"]
    conduct, facts = after.get("conduct") or {}, after.get("facts") or {}
    return {"validated": test["validated"], "gains": list(test["gains"]),
            "open": [{"goal": judged[k]["summary"], "kind": judged[k].get("kind"),
                      "why": _open(judged[k], conduct.get(k))} for k, state in after["goals"].items()
                     if state == "unmet" and k in judged and judged[k]["root"] != k],
            "set_aside": [judged[k].get("quote") or judged[k]["summary"] for k, c in [*conduct.items(), *facts.items()]
                          if c.get("set_aside") and k in judged],
            "lost": [judged[k]["summary"] for k, state in before["goals"].items()
                     if state == "met" and after["goals"].get(k) == "unmet"],
            "roots": [{"summary": judged[root]["summary"], "met_before": before["goals"].get(root) == "met",
                       "met_after": after["goals"].get(root) == "met", "turns_before": turns,
                       "turns_after": after["turns_per_root_goal"].get(root),
                       **({"why": answers[-1]["why"]} if after["goals"].get(root) == "unmet" and (
                           answers := sorted((facts.get(root) or {}).get("answers") or [], key=lambda a: a["turn"]))
                          else {})}
                      for root, turns in before["turns_per_root_goal"].items()],
            "side_effects_left": {"before": [s["name"] for s in before["side_effects_left"]],
                                  "after": [s["name"] for s in after["side_effects_left"]]}}


def _open(goal, found):
    """Why a sub-goal is still open with the change, for the proposer."""
    if goal.get("kind") == "conduct":
        shown = next((a for a in (found or {}).get("answers") or [] if a["shows"]), None)
        return (f"Assist's reply in turn {shown['turn']} still does what the household objected to "
                f"(\u201c{goal.get('quote')}\u201d): {shown['why']}") if shown else "still does what they objected to"
    return {"step": "what it asked for again wasn't done before it was said",
            "wrap-up": "something of its goal was still open when it was said"}.get(goal.get("kind"),
                                                                                "Assist didn't reach it")


def so_far(reviews, tests, tests_left, reviews_left):
    """What a proposer picking up its round reads of it (P0.9): each test as test_idea answered it, with the idea and
    changes it tested; each review's verdict on its changes; and the tests and reviews left. Nothing while the round
    has neither."""
    if not (reviews or tests):
        return {}
    return {"tests": [{"idea": t.get("idea"), "changes": t["hypothesis"]["changes"], **result(t)} for t in tests],
            "tests_left": tests_left,
            "reviews": [{"title": r["hypothesis"].get("title"), "changes": r["hypothesis"].get("changes"),
                         "verdict": r["verdict"]} for r in reviews],
            "reviews_left": reviews_left}


class TestIdea(StampTool):
    """Simulate's stamp, for the proposer: test one idea on this conversation, in whichever order it collects its
    stamps (SPEC.md P0.4, P0.9)."""
    name = "test_idea"
    description = ("Test one idea the reviewer approved, with exactly the changes it approved, on the "
                   "conversation you're proposing for. The whole conversation is replayed from its first turn in "
                   "the user's recorded words, without the idea's changes and with them, and every goal is judged "
                   "both ways: goals met, turns per root goal, side effects left. It validates only if it's strictly "
                   "better: better on one of those and worse on none (no goal met without it goes unmet, no root goal "
                   "takes more turns, no more side effects are left). Each test spends one of this round's tests; "
                   "the same changes are tested once. When they run out, send on your best validated idea, or none. "
                   "Approved changes that validate have both stamps: send them on as they are.")
    parameters = probatio.Schema(_HYPOTHESIS)

    def __init__(self, flow, rid, ask, cap):
        super().__init__()  # One test at a time, from one allowance: the same changes are tested once (P6.3.4).
        self.flow, self.rid, self.ask, self.cap = flow, rid, ask, cap
        self.crashed = None  # Why Simulate couldn't test, if it couldn't: the round's Propose fails (P0.8).
        self.untestable = set()  # The changes (digests) Simulate said it can't test: no stamp they can get.

    def left(self):
        return self.flow.tests_left(self.rid)

    def tests(self):
        return self.flow.tests(self.rid)

    async def take(self, hass, hypothesis):
        """The test of arguments its schema took: an argument error goes back to the model before this (RoleTool),
        so whatever stops Simulate here is Simulate's own."""
        from .simulate import Untestable
        # Tested as it would be sent on: an edit of an assistant's instructions names the assistant it edits.
        hypothesis = {**hypothesis, "changes": self.ask.sessions.with_targets(hypothesis["changes"])}
        if not of(self.ask.reviews, (), hypothesis)["Review"]:  # Approval first: what validates has both (P0.4).
            return {"error": "Ask the reviewer about these changes first (ask_reviewer): only changes its latest "
                             "review of them approved are tested."}
        if self.left() <= 0:
            return {"error": "This round's tests are used up: send on your best validated idea, or none."}
        try:
            test = await self.flow.test(self.rid, hypothesis, self.cap)
        except Untestable as err:  # Why it can't be tested: the proposer's to know, never a failure of the stage.
            self.untestable.add(digest(hypothesis["changes"]))
            return {"error": str(err)}
        except Exception as err:  # noqa: BLE001 - whatever stopped it, the stage holds it, with why.
            self.crashed = f"{type(err).__name__}: {err}"
            return {"error": "Simulate couldn't test this idea."}
        return {**result(test), "tests_left": self.left()}


__all__ = ["TestIdea", "of", "so_far", "verdict"]
