"""Calibrating the quick check (triage) on the labelled sessions, offline.

A model's answer to each brief is pasted in; the score says whether any session the labels show had friction was
skipped. That must never happen; skipping too few only costs more.

  .venv/bin/python tools/triage_check.py sessions                        the labelled sessions' short ids
  .venv/bin/python tools/triage_check.py <name> brief <session>          writes the brief to a file, prints the path
  .venv/bin/python tools/triage_check.py <name> answer <session> '<json>'   saves {smooth, sure, why}
  .venv/bin/python tools/triage_check.py <name> score [one-turn]      one-turn: only the one-turn sessions
Results: triage/<name>/ in the private folder (tools/private.py), where the labels and recordings are too.
"""

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.modules.setdefault("turbojpeg", types.SimpleNamespace(TurboJPEG=object))  # As in fixture-test/conftest.py.
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import private  # noqa: E402
from custom_components.assist_improver import triage  # noqa: E402
from custom_components.assist_improver.analyzer import to_analysis  # noqa: E402
from custom_components.assist_improver.exploration import SessionView  # noqa: E402
from custom_components.assist_improver.reflective import task_text  # noqa: E402

LABELS = ("labels", "labels-practice", "labels-heldout-2")
PRIVATE = private.path()
OUT = PRIVATE / "triage"


def labelled(one_turn_only=False):
    """{short id: label} for every labelled session (the quick check sees them all), or only the one-turn ones."""
    labels = {p.stem[:8]: json.loads(p.read_text()) for folder in LABELS
              for p in sorted((PRIVATE / folder).glob("*.json"))}
    return {s: l for s, l in labels.items() if not one_turn_only or len(view(l).digest["turns"]) == 1}


def view(label):
    path = PRIVATE / "recordings" / f"{label['recording_id']}.json"
    return SessionView(json.loads(path.read_text())["data"])


def friction(label):
    """What the labels show: extra turns, or a goal unmet (never reached, or settings left open)."""
    analysis = to_analysis(label["recording_id"], label, view(label))
    # Whether a goal was reached is the label's own call (the recording can miss it), not what code derives from the
    # recording.
    unmet = any(g["kind"] == "ask" or g.get("reached") is False for g in label["goals"])
    return {"extra_turns": analysis["extra_turns"], "unmet": unmet,
            "any": bool(analysis["extra_turns"]) or unmet}


def brief(name, session):
    (OUT / name / "briefs").mkdir(parents=True, exist_ok=True)
    path = OUT / name / "briefs" / f"{session}.txt"
    path.write_text(task_text(triage.INSTRUCTIONS, triage.payload(view(labelled()[session]))) + "\n\n"
                    "Answer with JSON only: {\"smooth\": true|false, \"sure\": true|false, \"why\": \"...\"}")
    print(json.dumps({"brief_file": str(path)}))


def answer(name, session, raw):
    parsed = triage.STRUCTURE(json.loads(raw))
    (OUT / name).mkdir(parents=True, exist_ok=True)
    (OUT / name / f"{session}.json").write_text(json.dumps(parsed, indent=1, ensure_ascii=False))
    print(json.dumps({"saved": session}))


def score(name, which="all"):
    """which: all (every labelled session, as the quick check sees them) or one-turn."""
    rows, unsafe, skipped_smooth, smooth = [], [], 0, 0
    for session, label in labelled(one_turn_only=which == "one-turn").items():
        path = OUT / name / f"{session}.json"
        if not path.exists():
            continue
        got, truth = json.loads(path.read_text()), friction(label)
        skips = triage.skips_analysis(got)
        if skips and truth["any"]:
            unsafe.append(session)
        if not truth["any"]:
            smooth += 1
            skipped_smooth += skips
        rows.append(f"  {'❌' if skips and truth['any'] else '✅'} {session} labels: "
                    f"{'friction' if truth['any'] else 'smooth'}; check: {'skip' if skips else 'analyse'} "
                    f"({'smooth' if got['smooth'] else 'not smooth'}, {'sure' if got['sure'] else 'unsure'}): {got['why']}")
    result = {"answered": len(rows), "unsafe_skips": unsafe, "smooth_sessions": smooth, "smooth_skipped": skipped_smooth}
    (OUT / name / "score.json").write_text(json.dumps(result, indent=1))
    print(f"Triage {name}: {len(rows)} answered; {len(unsafe)} friction session(s) skipped (must be 0); "
          f"{skipped_smooth}/{smooth} smooth sessions skipped. {'PASS' if not unsafe else 'FAIL'}")
    print("\n".join(rows))


def main(*args):
    if args[0] == "sessions":
        return print(json.dumps(sorted(labelled())))
    name, command, *rest = args
    {"brief": brief, "answer": answer, "score": score}[command](name, *rest)


if __name__ == "__main__":
    main(*sys.argv[1:])
