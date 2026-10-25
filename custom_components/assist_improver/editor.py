"""The editor: writes what the household reads first about an improvement, in their words.

A small model gets the improvement (its idea, cause and exact changes) and what the
tests showed, and writes two things: a headline (what the change does) and what you
get (what becomes easier). Code checks both before they're kept, as the other roles'
answers are checked. Why it's safe is never written by a model: code states it from
the tests and the changeset (panel.py).
"""

import re

import probatio
from homeassistant.helpers import llm

ROLE = "editor"
INSTRUCTIONS = """You write the two short texts a household reads first when deciding whether to accept a change to their Home Assistant voice assistant ("Assist"). You get the change (its idea, what was going wrong, and the exact changes) and what testing showed: what the household said, what happened before, and what happened with the change.

Write:
- headline: what the change does, in plain words anyone in the household understands. Start with a verb. At most 60 characters, no full stop. Name things the way the household does (the garage doors, the bedroom lights), never by entity ID. Examples: "Expose garage door controls via scripts", "Turn off only lights when you ask for lights".
- benefit: what gets easier for them, in one or two short sentences addressed to "you": what they can now say, and what happens, compared with before. Only what the tests showed; no promises beyond them, no entity IDs, no technical terms they wouldn't use.

Answer only by calling submit_copy. If it reports problems, fix them and submit again. When it accepts your answer, finish with one short sentence."""
STRUCTURE = probatio.Schema({probatio.Required("headline"): str, probatio.Required("benefit"): str})
ENTITY_ID = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")
JARGON = ("the user", "entity", "entity_id", "intent", "hass", "llm")


def problems(answer):
    """What's wrong with an editor's answer; empty means it reads right."""
    found = []
    headline, benefit = (answer.get("headline") or "").strip(), (answer.get("benefit") or "").strip()
    if not 3 <= len(headline) <= 60:
        found.append(f"The headline has {len(headline)} characters; keep it between 3 and 60.")
    if headline and not headline[0].isupper():
        found.append("Start the headline with a capitalised verb.")
    if headline.endswith("."):
        found.append("Drop the full stop at the end of the headline.")
    if not benefit:
        found.append("Write the benefit: what gets easier, addressed to you.")
    elif len(benefit) > 240:
        found.append(f"The benefit has {len(benefit)} characters; keep it within 240 (one or two short sentences).")
    elif len(re.findall(r"[.!?](\s|$)", benefit)) > 2:
        found.append("Keep the benefit to one or two sentences.")
    for name, text in (("headline", headline), ("benefit", benefit)):
        if ENTITY_ID.search(text):
            found.append(f"The {name} names an entity ID ({ENTITY_ID.search(text).group()}); use the household's own name.")
        lowered = f" {text.lower()} "
        found += [f"The {name} uses {word!r}; say it the way the household would." for word in JARGON
                  if re.search(rf"\b{re.escape(word)}\b", lowered)]
    return found


def named(text, names):
    """The text with each entity ID replaced by the household's name for it, where known."""
    return ENTITY_ID.sub(lambda m: f"\u201c{names[m.group()]}\u201d" if m.group() in names else m.group(), text)


def entity_names(hass, item):
    """{entity ID: name} for the entities an improvement's changes mention: Home Assistant's names, else the
    names its scripts give themselves."""
    from .panel import _change_text
    names = {f"script.{oid}": c.get("alias") for oid, c in (item.get("setup") or {}).get("scripts", {}).items()
             if c.get("alias")}
    for entity_id in {m for c in item.get("changes", []) for m in ENTITY_ID.findall(_change_text(c, ""))}:
        state = hass.states.get(entity_id) if hass else None
        if state is not None and state.attributes.get("friendly_name"):
            names[entity_id] = state.attributes["friendly_name"]
    return names


def payload(item, names=None, assistant=None):
    """The improvement as the editor reads it: its idea, cause and changes in plain words (named as the household
    names things), and each test."""
    from .panel import _change_text
    names = names or {}
    assistant = assistant or (item.get("provider") or {}).get("title") or "Assist"
    tests = []
    for test in item.get("tests", []):
        entry = {"goal": test.get("summary"), "improved": test.get("status") == "awaiting_review",
                 "before": test.get("before", []), "with_the_change": test.get("after", [])}
        if entry["improved"]:
            entry["result"] = ("goal now met (it went unmet before)" if test.get("goal_newly_met")
                               else f"{test.get('turns_after')} turn(s) instead of {test.get('turns_before')}")
        tests.append(entry)
    return {"idea": item.get("title"), "what_was_going_wrong": item.get("cause"),
            "changes": [named(_change_text(c, assistant), names) for c in item.get("changes", [])], "tested": tests,
            "expected_to_help_more_conversations": len(item.get("untested_goals", []))}


class SubmitCopy(llm.Tool):
    """The editor's only way to answer; keeps every submission so the best one can be used."""
    name = "submit_copy"
    description = ("Submit the headline and the benefit. They're checked; if problems are reported, fix them and submit "
                   "again.")
    parameters = STRUCTURE
    integration = "assist_improver"
    annotations = llm.ToolAnnotations(read_only=True, open_world=False)

    def __init__(self):
        self.submissions = []

    def run(self, **answer):
        found = problems(answer)
        self.submissions.append({"answer": answer, "problems": found})
        if found:
            return {"accepted": False, "problems": found}
        return {"accepted": True, "message": "Accepted. Finish with one short sentence."}

    async def async_call(self, hass, tool_input, llm_context):
        return self.run(**self.parameters(tool_input.tool_args))

    def best(self):
        """The last accepted answer, or None."""
        accepted = [s["answer"] for s in self.submissions if not s["problems"]]
        return accepted[-1] if accepted else None


async def write(hass, item, entity_id):
    """{headline, benefit, status, entity_id} for an improvement, or a failed status (the improvement keeps the
    generator's title, and its page reads the same as before)."""
    from .exploration import SessionAPI
    from .reflective import run_role
    submit = SubmitCopy()
    record = await run_role(hass, role=ROLE, entity_id=entity_id, instructions=INSTRUCTIONS,
                            payload=payload(item, entity_names(hass, item)),
                            structure=None, llm_api=SessionAPI(hass, tools=[submit]), allow_paid_inference=True)
    answer = submit.best()
    if answer is None:
        return {"status": "failed", "entity_id": entity_id, "error": record.get("error")}
    return {"status": "written", "entity_id": entity_id, "headline": answer["headline"].strip(),
            "benefit": answer["benefit"].strip()}
