"""The generator and its long-running reviewer, side by side, in one run."""

from ..exploration import SessionAPI
from ..reflective import run_role
from ..trace import span
from .prompts import GENERATOR_INSTRUCTIONS, REVIEWER_INSTRUCTIONS, REVIEWER_LOOP
from .tools import AskReviewer, ChannelSubmitReview, ReviewChannel, SubmitHypotheses, WaitForDraft, reviewer_tools, tools

GENERATOR_ROLE = "hypothesis_generator"
REVIEWER_ROLE = "hypothesis_reviewer"


async def run_generator(hass, sessions, entity_id, *, tester, reviewer_entity_id=None):
    """The generator and one long-running reviewer, side by side: (generator run, its submit tool, ask tool,
    reviewer run). The reviewer runs on its own model if given, else the generator's. tester: makes Simulate's stamp,
    the test_idea tool, from the ask tool (stamps.TestIdea)."""
    from ..snapshot import live_definitions
    scripts, automations = live_definitions(hass)
    async with span("evidence.load"):
        await sessions.prepare()
        await sessions.load_evidence({"scripts": scripts, "automations": automations})
    channel = ReviewChannel()
    review_submit = ChannelSubmitReview(sessions, channel)
    reviewer = hass.async_create_task(run_role(
        hass, role=REVIEWER_ROLE, entity_id=reviewer_entity_id or entity_id,
        instructions=REVIEWER_INSTRUCTIONS + REVIEWER_LOOP,
        payload={"household": sessions.summary()}, structure=None, allow_paid_inference=True,
        llm_api=SessionAPI(hass, tools=reviewer_tools(sessions, review_submit) + [WaitForDraft(channel)])))
    channel.reviewer = reviewer
    ask = AskReviewer(sessions, channel)
    test = tester(ask)
    submit = SubmitHypotheses(sessions, ask, test)
    try:
        generator = await run_role(hass, role=GENERATOR_ROLE, entity_id=entity_id,
                                   instructions=GENERATOR_INSTRUCTIONS, payload=sessions.overview(), structure=None,
                                   allow_paid_inference=True,
                                   llm_api=SessionAPI(hass, tools=tools(sessions, submit, ask) + [test]))
    except BaseException:  # Failed or cancelled: the reviewer never waits for a draft that won't come.
        channel.close()
        reviewer.cancel()
        raise
    finally:  # However the proposer's generation ended, none of its stamps' calls runs on to change the round (P0.8).
        for tool in (ask, test):
            tool.cancel()
    channel.close()
    reviewer_run = await reviewer
    return generator, submit, ask, reviewer_run
