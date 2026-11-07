"""Hypothesis generator and reviewer roles.

The generator reads the friction across analysed sessions (extra turns with the
sub-goals they served, settings the user had to choose, side effects left
behind, what the household objected to), groups it into themes and proposes one idea
for the conversation, combining a fix per theme, each the least specific change
that works. It answers by calling submit_hypotheses, which checks the hypothesis
against the sessions and the household and rejects it with the reasons until it
is valid. The reviewer is a separate role: it reviews each draft in comments
(approve, or request changes, optionally with a revised version), checking it
against the sessions where it would apply, and answers through submit_review.
Both are agentic, with read-only tools over the recordings.
"""

from ..exploration import ListImprovements, ReadImprovement  # noqa: F401 - used through this module.
from .checks import (household_knowledge, instructions, known_lines, open_goals,  # noqa: F401
                     problems)
from .goals import (FRICTION_KINDS, TRIAGED_NOTE, UNANALYSED_NOTE, capabilities, friction,  # noqa: F401
                    friction_by_goal, goal_devices, removes, scenario, short)
from .households import CHOICES_SHOWN, SCENARIO_BUDGET, SCENARIOS_SHOWN, Sessions, decisions  # noqa: F401
from .prompts import GENERATOR_INSTRUCTIONS, HA_PRIMER, REVIEWER_INSTRUCTIONS, REVIEWER_LOOP  # noqa: F401
from .run import GENERATOR_ROLE, REVIEWER_ROLE, run_generator  # noqa: F401
from .schema import HYPOTHESES, KINDS, LABELS, REVIEW, blocking, verdict  # noqa: F401
from .tools import (REVIEWS_PER_IDEA, REVIEWS_PER_ROUND, AskReviewer, ChannelSubmitReview,  # noqa: F401
                    CheckHypothesis, DeviceDetailsAt, DevicesInAreaNow, ListSessions, ReadAssistantInstructions,
                    ReadSession, ReadSessionTurn, ReviewChannel, SubmitHypotheses, SubmitReview,
                    WaitForDraft, review_result, reviewer_tools, tools)
