"""The stages' contracts (SPEC-CORE.md 3): what each stage hands the next, typed in records.py, with the fields each
must hand on (REQUIRED), checked where it's made. A stage whose output breaks its contract fails that item, with why
(tried again, then held: flow.py), instead of handing on what the next stage can't read. The interface tests
(fixture-test/test_contracts.py) connect each stage's real output to the next stage.
"""

import types
import typing

from . import records

# The fields a stage must hand on, beyond those records.py declares (all of them optional in what's stored, which
# keeps what earlier versions wrote).
REQUIRED = {records.TriageVerdict: ("smooth", "sure"),
            records.AnalysisResult: ("recording_id", "goals"), records.Goal: ("turn", "kind"),
            records.Hypothesis: ("title", "goals", "changes"),
            records.GoalRef: ("session", "goal_turn"), records.Change: ("kind",),
            records.Draft: ("title", "goals", "changes", "friction_it_targets", "sessions_where_it_would_apply"),
            records.ReviewResponse: ("comment", "resolution"), records.ReviewAnswer: ("comments",),
            records.ReviewComment: ("label", "decorations", "subject"), records.NumberedComment: ("comment", "label", "decorations", "subject"),
            records.ReviewResult: ("outcome", "comments"),
            records.Test: ("idea", "changes", "hypothesis", "judged", "before", "after", "gains", "validated", "setup",
                           "tested", "provider", "user_id", "recorded"),
            records.Replay: ("plan", "status", "said", "replies", "goals", "goals_met", "turns_per_root_goal", "counted",
                             "side_effects_left", "started_from", "config", "conduct"),
            records.Improvement: ("id", "title", "status", "changes", "test"), records.Copy: ("status",),
            records.Decision: ("decision", "at")}


class ContractError(RuntimeError):
    """A stage's output broke its contract: the code's fault, never the model's or the household's."""


def problems(value, kind, path="record", required=True):
    """Where value doesn't fit kind (a TypedDict, a union, list[...], dict[...], or a plain type): a list of lines.
    required: also the fields a stage must hand on (REQUIRED); off for what's stored, which keeps earlier shapes."""
    if kind is typing.Any:
        return []
    if typing.is_typeddict(kind):
        if not isinstance(value, dict):
            return [f"{path}: {type(value).__name__}, not {kind.__name__}"]
        fields = typing.get_type_hints(kind)
        found = [f"{path}.{key}: not declared in {kind.__name__}" for key in value if key not in fields]
        must = REQUIRED.get(kind, ()) or kind.__required_keys__  # A total TypedDict (a handoff) requires them all.
        found += [f"{path}.{key}: missing" for key in (must if required else ()) if key not in value]
        for key, item in value.items():
            if key in fields:
                found += problems(item, fields[key], f"{path}.{key}", required)
        return found
    origin = typing.get_origin(kind)
    if origin is typing.Literal:  # A steering field: only its fixed set of values.
        allowed = typing.get_args(kind)
        return [] if any(value == a and type(value) is type(a) for a in allowed) else [f"{path}: {value!r}, not one of "
                                                                                       f"{', '.join(map(repr, allowed))}"]
    if origin in (types.UnionType, typing.Union):
        tried = [problems(value, arm, path, required) for arm in typing.get_args(kind)]
        return [] if any(not t for t in tried) else min(tried, key=len)
    if origin is list:
        if not isinstance(value, list):
            return [f"{path}: {type(value).__name__}, not a list"]
        (item_kind,) = typing.get_args(kind)
        return [line for n, item in enumerate(value) for line in problems(item, item_kind, f"{path}[{n}]", required)]
    if origin is dict:
        if not isinstance(value, dict):
            return [f"{path}: {type(value).__name__}, not a dict"]
        _, item_kind = typing.get_args(kind)
        return [line for key, item in value.items() for line in problems(item, item_kind, f"{path}[{key}]", required)]
    if kind is type(None) or kind is None:
        return [] if value is None else [f"{path}: {type(value).__name__}, not None"]
    if kind is float:
        return [] if isinstance(value, (int, float)) and not isinstance(value, bool) else [f"{path}: not a number"]
    if kind is int and isinstance(value, bool):
        return [f"{path}: bool, not int"]
    return [] if isinstance(value, kind) else [f"{path}: {type(value).__name__}, not {kind.__name__}"]


def check(value, kind, what):
    """Raise ContractError when value breaks kind's contract, saying where (what: the stage's output, in words)."""
    if found := problems(value, kind, what):
        raise ContractError(f"{what} broke its contract: {'; '.join(found[:3])}"
                            + (f" (and {len(found) - 3} more)" if len(found) > 3 else ""))
    return value
