"""Evidence grouping before solution generation; no model or HA effects.

A theme can cross journeys. A successful related journey is useful context,
not an additional observed intervention or an invented improvement target.
"""

from copy import deepcopy

from .cases import CaseError, digest


def case_reference(case):
    """Bind grounding, but allow a baseline to be completed after aggregation."""
    fields = ("id", "revision", "recording_digest", "turns", "intervention_turns", "destination")
    return {"case_id": case["id"], "grounding_digest": digest({k: case[k] for k in fields})}


def create_theme(cases, *, title, friction, observations, provenance):
    if not all(isinstance(s, str) and s.strip() for s in (title, friction, provenance)):
        raise CaseError("Theme needs a title, shared friction and attributed provenance")
    if not observations:
        raise CaseError("Theme needs supporting evidence before proposing solutions")
    linked, seen = [], set()
    for observation in observations:
        case_id = observation.get("case_id")
        if case_id not in cases or case_id in seen:
            raise CaseError("Theme references a missing or duplicate journey")
        case = cases[case_id]
        role = observation.get("role")
        if role not in {"friction", "related_success", "counterexample"}:
            raise CaseError("Unknown theme evidence role")
        if role == "friction" and not case["intervention_turns"]:
            raise CaseError("A successful journey cannot be counted as observed friction")
        if role == "related_success" and case["intervention_turns"]:
            raise CaseError("An intervention journey cannot be counted as a related success")
        rationale = observation.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise CaseError("Explain why this journey supports or challenges the theme")
        linked.append({**case_reference(case), "role": role, "rationale": rationale})
        seen.add(case_id)
    friction_cases = [x for x in linked if x["role"] == "friction"]
    if not friction_cases:
        raise CaseError("No observed intervention supports this improvement theme")
    result = {"format": 1, "title": title, "friction": friction,
              "provenance": provenance, "journeys": linked,
              "observed_intervention_journeys": len(friction_cases),
              "observed_intervention_turns": sum(len(cases[x["case_id"]]["intervention_turns"])
                                                 for x in friction_cases),
              "future_recurrence": "not_established_by_these_counts"}
    result["id"] = digest(result)
    return result


def verify_theme(theme, cases):
    content = {k: v for k, v in theme.items() if k != "id"}
    if theme.get("id") != digest(content):
        raise CaseError("Theme content changed; create a new theme revision")
    for link in theme["journeys"]:
        if link["case_id"] not in cases or case_reference(cases[link["case_id"]]) != {
                k: link[k] for k in ("case_id", "grounding_digest")}:
            raise CaseError("Journey grounding changed; revisit the shared theme")
    return deepcopy(theme)
