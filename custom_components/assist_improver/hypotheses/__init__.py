"""Hypothesis generator and reviewer roles.

The generator reads the friction across analysed sessions (extra turns with the
sub-goals they served, settings the user had to choose, side effects left
behind, the household's feedback), groups it into themes and proposes one coherent change per theme,
preferring the least specific change that works. It answers by calling
submit_hypotheses, which checks every hypothesis against the sessions and the
household and rejects it with the reasons until it is valid. The reviewer is a
separate role: it keeps, revises or drops one hypothesis, with a regression
check against a summary of the household's sessions, and answers through
submit_review. Both are agentic, with read-only tools over the recordings.
"""

from ..exploration import ListImprovements, ReadImprovement, change_devices  # noqa: F401 - used through this module.
from .checks import (household_knowledge, instructions, known_lines, open_goals,  # noqa: F401
                     problems)
from .goals import (FRICTION_KINDS, TRIAGED_NOTE, UNANALYSED_NOTE, capabilities, feedback_goal,  # noqa: F401
                    friction, friction_by_goal, goal_devices, removes, scenario, short)
from .households import CHOICES_SHOWN, SCENARIO_BUDGET, SCENARIOS_SHOWN, Sessions, decisions  # noqa: F401
from .prompts import GENERATOR_INSTRUCTIONS, HA_PRIMER, REVIEWER_INSTRUCTIONS, REVIEWER_LOOP  # noqa: F401
from .run import GENERATOR_ROLE, REVIEWER_ROLE, run_generator  # noqa: F401
from .schema import HYPOTHESES, KINDS, LABELS, REVIEW, blocking, verdict  # noqa: F401
from .tools import (REVIEWS_PER_IDEA, REVIEWS_PER_RUN, AskReviewer, ChannelSubmitReview,  # noqa: F401
                    CheckHypothesis, DeviceDetailsAt, DevicesInAreaNow, ListSessions, ReadAssistantInstructions,
                    ReadScript, ReadSession, ReadSessionTurn, ReviewChannel, SubmitHypotheses, SubmitReview,
                    WaitForDraft, review_result, reviewer_tools, tools)
