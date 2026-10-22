"""Score journey-analyzer output against the household's labels; deterministic, no model.

Both sides use the label shape in LABELS.md. Every check is reported
separately so a failure says exactly what differed. Informational
destinations need a human or separate judge; they are flagged, not passed.
"""

from .cases import CaseError

BRIGHTNESS_TOLERANCE = 5


def _same_value(expected, actual):
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.get("state") != actual.get("state"):
            return False
        if "brightness_pct" in expected:
            got = actual.get("brightness_pct")
            return isinstance(got, (int, float)) and abs(got - expected["brightness_pct"]) <= BRIGHTNESS_TOLERANCE
        return all(actual.get(k) == v for k, v in expected.items())
    if isinstance(expected, dict):  # A bare state can't satisfy an attribute requirement.
        return set(expected) == {"state"} and expected["state"] == actual
    if isinstance(actual, dict):
        return actual.get("state") == expected
    return expected == actual


def _criteria_match(expected, actual):
    if not isinstance(expected, dict) or not isinstance(actual, dict) or set(expected) != set(actual):
        return False
    return all(_same_value(expected[e], actual[e]) for e in expected)


def _is_speech_test(journey):
    text = " ".join(str(journey.get(k, "")) for k in ("id",)) + " " + str(journey["destination"].get("summary", ""))
    return "test" in text.lower()


def score_recording(predicted, label):
    if predicted.get("recording_id") != label.get("recording_id"):
        raise CaseError("Prediction and label are for different recordings")
    checks = []

    def check(name, passed, detail=None):
        checks.append({"check": name, "passed": bool(passed), **({"detail": detail} if detail else {})})

    by_turns = {tuple(j["turns"]): j for j in predicted.get("journeys", [])}
    used = set()
    for expected in label.get("journeys", []):
        key = tuple(expected["turns"])
        actual = by_turns.get(key)
        check(f"{expected['id']}: boundaries {list(key)}", actual is not None,
              None if actual else {"predicted_journeys": [j["turns"] for j in predicted.get("journeys", [])]})
        if actual is None:
            continue
        used.add(key)
        dest, got = expected["destination"], actual.get("destination", {})
        if dest["kind"] == "informational":
            check(f"{expected['id']}: destination", False, "informational destination needs review, not auto-scored")
        else:
            check(f"{expected['id']}: destination", dest["kind"] == got.get("kind") and _criteria_match(dest["criteria"], got.get("criteria")),
                  {"expected": dest, "predicted": got})
        check(f"{expected['id']}: reached", expected["reached"] == actual.get("reached"),
              {"expected": expected["reached"], "predicted": actual.get("reached")})
        want = {i["turn"]: i["avoidable"] for i in expected.get("interventions", [])}
        have = {i["turn"]: i.get("avoidable") for i in actual.get("interventions", [])}
        check(f"{expected['id']}: intervention turns", set(want) == set(have),
              {"expected": sorted(want), "predicted": sorted(have)})
        # Judged only on turns both sides call interventions; missing turns fail the check above.
        check(f"{expected['id']}: avoidable", all(have[t] == a for t, a in want.items() if t in have),
              {"expected": want, "predicted": have})

    predicted_not = {i["turn"] for i in predicted.get("not_journeys", [])}
    for item in label.get("not_journeys", []):
        turn = item["turn"]
        ok = turn in predicted_not
        if not ok and item.get("also_acceptable"):
            alt = by_turns.get((turn, turn))
            ok = alt is not None and _is_speech_test(alt) and not alt.get("interventions")
            if ok:
                used.add((turn, turn))
        check(f"turn {turn}: not a journey ({item['what']})", ok)

    extra = [list(k) for k in by_turns if k not in used]
    check("no invented journeys", not extra, {"extra": extra} if extra else None)
    return {"recording_id": label["recording_id"], "passed": all(c["passed"] for c in checks), "checks": checks}


def score_set(predictions, labels):
    """predictions and labels keyed by recording ID; a missing prediction fails."""
    reports = []
    for recording_id, label in sorted(labels.items()):
        if recording_id not in predictions:
            reports.append({"recording_id": recording_id, "passed": False,
                            "checks": [{"check": "prediction present", "passed": False}]})
            continue
        reports.append(score_recording(predictions[recording_id], label))
    checks = [c for r in reports for c in r["checks"]]
    return {"recordings": len(reports), "recordings_passed": sum(r["passed"] for r in reports),
            "checks": len(checks), "checks_passed": sum(c["passed"] for c in checks), "reports": reports}
