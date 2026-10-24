"""Reviewing an improvement: the same steps in Configure → Improvements and in Repairs.

Every decision goes through the one list (improvements.Improvements.decide), so
both places, and the notification, always show the same thing.
"""

import probatio
from homeassistant.helpers.selector import (SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode,
                                            TextSelector, TextSelectorConfig)

from .cases import CaseError
from .const import DOMAIN
from .changeset import plan_steps
from .improvements import DECISIONS, PENDING, choices, describe

STATUS = {"awaiting_review": "to review", "needs_attention": "needs attention", "applied": "applied",
          "undone": "undone", "declined": "declined", "revision_requested": "revision requested",
          "ignored": "ignored", "conflict": "changed since it was tested"}


def outcome_text(item):
    """What a decision did, in a sentence or two."""
    outcome = item["outcome"]
    status = outcome["status"]
    if status == "recorded":
        return "Thanks, your decision is recorded."
    if status == "done":
        return ("Applied. Assist uses it from your next request; no script was run."
                if item["status"] == "applied" else "Undone: what was there before is back.") + (
            " Left as you changed it: " + "; ".join(l["what"] for l in outcome["left"]) if outcome.get("left") else "")
    if status == "conflict":
        return "Nothing was changed: " + "; ".join(f"{c['what']}: {c['why']}" for c in outcome["conflicts"])
    if status == "rolled_back":
        return (f"Nothing was changed: {outcome['failure']['what']} failed ({outcome['failure']['error']}), so the "
                "steps already done were undone. You can try again.")
    if status == "needs_attention":
        return ("Some changes couldn't be undone after a failure: "
                + "; ".join(f"{s['what']} ({s['why']})" for s in outcome["stuck"]))
    return outcome.get("error") or status


class _ReviewSteps:
    """Shows one improvement and takes a decision: one dropdown, Accept changes by default. Any other decision
    opens a second screen for a note. The flow sets _improvement_id first."""
    _improvement_id = None
    _decision = None

    @property
    def _improvements(self):
        return self.hass.data[DOMAIN].improvements

    async def _decide(self, decision, note=""):
        """Returns (result, error)."""
        try:
            item = await self._improvements.decide(self._improvement_id, decision, note=note,
                                                   user_id=self.context.get("user_id"))
        except CaseError as err:
            return None, str(err)
        return self.async_abort(reason="decided", description_placeholders={"result": outcome_text(item)}), None

    async def async_step_review(self, user_input=None):
        try:
            item = self._improvements.get(self._improvement_id)
        except CaseError:
            return self.async_abort(reason="not_found")
        open_ = choices(item)
        planned = await plan_steps(self.hass, item) if "accept" in open_ else None  # Checked against HA now.
        placeholders = {"proposal": describe(item, planned), "status": STATUS.get(item["status"], item["status"]),
                        "title": item["title"], "error": ""}
        if not open_:
            return self.async_abort(reason="closed", description_placeholders=placeholders)
        errors = {}
        if user_input is not None:
            self._decision = user_input["decision"]
            if self._decision not in ("accept", "undo"):
                return await self.async_step_note()
            result, error = await self._decide(self._decision)
            if result:
                return result
            errors["base"], placeholders["error"] = "decision", error
        schema = probatio.Schema({probatio.Required("decision", default=open_[0]): SelectSelector(SelectSelectorConfig(
            options=[SelectOptionDict(value=d, label=DECISIONS[d][1]) for d in open_],
            mode=SelectSelectorMode.DROPDOWN))})
        return self.async_show_form(step_id="review", data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    async def async_step_note(self, user_input=None):
        """A note for any decision but accepting: required when asking for a revision."""
        errors, placeholders = {}, {"decision": DECISIONS[self._decision][1], "error": ""}
        if user_input is not None:
            result, error = await self._decide(self._decision, user_input.get("note") or "")
            if result:
                return result
            errors["base"], placeholders["error"] = "decision", error
        key = probatio.Required("note") if self._decision == "revise" else probatio.Optional("note")
        return self.async_show_form(step_id="note", data_schema=probatio.Schema(
            {key: TextSelector(TextSelectorConfig(multiline=True))}), errors=errors,
            description_placeholders=placeholders)

    async def async_step_improvements(self, user_input=None):
        """Choose an improvement: those waiting on you first, then the others (applied ones can be undone)."""
        listed = self._improvements.listed()
        if not listed:
            return self.async_abort(reason="no_improvements")
        if user_input is not None:
            self._improvement_id = user_input["improvement"]
            return await self.async_step_review()
        options = [SelectOptionDict(value=i["id"], label=f"{i['title']} ({STATUS.get(i['status'], i['status'])})")
                   for i in listed]
        pending = sum(i["status"] in PENDING for i in listed)
        return self.async_show_form(step_id="improvements", data_schema=probatio.Schema({
            probatio.Required("improvement", default=listed[0]["id"]): SelectSelector(SelectSelectorConfig(
                options=options, mode=SelectSelectorMode.LIST))}),
            description_placeholders={"pending": str(pending)})
