"""The conversations view: find a kept conversation, read it, and send it in.

Sending a conversation in files it, as Assist's file_improvement tool does (llm.py): one the pipeline hasn't taken
in yet goes straight to analysis at its next run, past the quick check, with the user's note; with on-demand
improvements on, a run starts for it as soon as it has ended. Nothing is copied or run again: it's the same
conversation, let in earlier.

What a conversation said (the user's words, and Assist's last reply to each) is read from its recording once and
kept in a small store of its own, so listing, searching and reading conversations never reopens the recordings.
"""

import asyncio
from datetime import timedelta
import re

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import utcnow

from .cases import failed_calls, transcript
from .const import DOMAIN, STORAGE_VERSION
from .sessions import short

WORDS = 1000  # Characters kept of each message.
SAID = 160  # Characters of the first thing said, for the list.
PAGE = 30  # Conversations a page lists.
SHAPE = 2  # What a kept transcript holds: 2 has each turn's failed steps.


class Transcripts:
    """{recording id: {"updated", "turns": [{"you", "assist"}]}}: each kept conversation's words, as of when its
    recording last changed."""

    def __init__(self, hass, recorder):
        self.recorder = recorder
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.transcripts", private=True, atomic_writes=True)
        self.kept = None
        self.lock = asyncio.Lock()

    async def all(self):
        """Every kept conversation's words; read from the recording only where it changed since (or is new)."""
        async with self.lock:
            if self.kept is None:
                stored = (await self.store.async_load()) or {}
                # Kept in an older shape (before the failed steps): read again from the recordings.
                self.kept = stored.get("transcripts", {}) if stored.get("shape") == SHAPE else {}
            index, changed = self.recorder.index, False
            for rid in [rid for rid in self.kept if rid not in index]:  # The tidy-up deleted it.
                del self.kept[rid]
                changed = True
            for rid, summary in index.items():
                if not summary.get("user_turns") or (rid in self.kept
                                                     and self.kept[rid].get("updated") == summary.get("updated")):
                    continue
                try:
                    recording = await self.recorder.export(rid)
                    turns, failed = transcript(recording), failed_calls(recording)
                except (ValueError, KeyError, TypeError):  # Unreadable or gone: listed without its words.
                    continue
                self.kept[rid] = {"updated": summary.get("updated"), "failed": failed,
                                  "turns": [{**{k: (t[k] or "")[:WORDS] for k in ("you", "assist")},
                                             **({"failed": t["failed"]} if t.get("failed") else {})} for t in turns]}
                changed = True
            if changed:
                self.store.async_delay_save(lambda: {"transcripts": self.kept, "shape": SHAPE}, 5)
            return self.kept


def improved(runtime):
    """The conversations (their short IDs) an applied improvement covers."""
    return {goal.split(":")[0] for item in runtime.improvements.items.values() if item["status"] == "applied"
            for goal in runtime.improvements._covers(item)}


TAKEN = ("in", "improved")  # Standings the improver has or is done with: never sent in again, never picked.


def standing(runtime, rid, at=None, tests=None, fixed=None):
    """Where a conversation stands: active (still going), sent (in, at the next run), in (the pipeline has it, or
    is done with it), improved (an improvement for it was accepted), or waiting (it can be sent in: one a run took in
    that still waits for the quick check can too, and then skips it)."""
    pipeline, summary = runtime.pipeline, runtime.recorder.index.get(rid) or {}
    runs = pipeline._runs()
    at = pipeline._at() if at is None else at
    tests = pipeline._tests() if tests is None else tests
    if short(rid) in (improved(runtime) if fixed is None else fixed):
        return "improved"
    if (rid in at and at[rid] != "triage") or rid in runs.analyzed or rid in runs.unreadable or rid in tests:
        return "in"
    if summary.get("filed"):
        return "sent"
    return "active" if summary.get("status") == "active" else "waiting"


ORDERS = {  # How a list can be sorted: newest, oldest, or the most effort first (more turns, then failed calls).
    "newest": lambda r: r["started"] or "", "oldest": lambda r: r["started"] or "",
    "effort": lambda r: (r["turns"] or 0, r["failed"], r["started"] or "")}


DAYS = {name: n for n, names in enumerate([("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"),
                                           ("thu", "thur", "thurs", "thursday"), ("fri", "friday"),
                                           ("sat", "saturday"), ("sunday",)]) for name in names}  # "sun" is the sun.
# Hours of each part of the day, overlapping as people remember them (5pm is afternoon and evening); 29 is 5am.
PARTS = {"morning": (5, 12), "afternoon": (12, 18), "evening": (17, 23), "night": (20, 29), "tonight": (17, 29)}
TIME = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?(am|pm)?(ish)?$")
FILLER = {"this", "last", "on", "at", "around", "about", "in", "the", "early", "late", "ish"}  # Beside a when.


def _about_when(word):
    """A word that says when, or goes with one: a day, a part of the day, a time, or a small word beside them."""
    time = TIME.match(word)
    return (word in DAYS or word in PARTS or word in ("today", "yesterday") or word in FILLER
            or bool(time and (time.group(2) or time.group(3) or time.group(4))))


def maybe_names(query):
    """Two words of the query that may name something (night light, good morning, morning routine) rather than say
    when: a part of the day beside a word that doesn't say when."""
    words, parts = query.casefold().split(), PARTS.keys() - {"tonight"}  # "tonight" never names a thing.
    return {f"{a} {b}" for a, b in zip(words, words[1:])
            if (a in parts or b in parts) and not _about_when(b if a in parts else a)}


def together(words, names):
    """The words, with two that make one of the names kept as one ("night light")."""
    out = []
    for word in words:
        if out and f"{out[-1]} {word}" in names:
            out[-1] = f"{out[-1]} {word}"
        else:
            out.append(word)
    return out


def when(query, now=None, names=()):
    """What the query says about when, as a test of a conversation's local start time (None if nothing), and the
    query's other words. When: a day (today, yesterday, a weekday), a part of the day (morning, afternoon, evening,
    night, tonight; this morning is today's, last night yesterday's), a time (10pm, 10:40 pm, 22:40, 10ish: an hour or
    45 minutes around it, morning or evening where it doesn't say). Small words beside them (last, around, on...) go
    with them; the night runs past midnight. names: two words kept together as words to find, not a when ("night
    light")."""
    now = now or dt_util.now()
    query = re.sub(r"(\d)\s+([ap]m|ish)\b", r"\1\2", query.casefold())
    query = re.sub(r"(\d{1,2})\s*o'?clock\b", r"\1:00", query)
    query = re.sub(r"\blast night\b", "yesterday night", query)
    query = re.sub(r"\bthis (morning|afternoon|evening)\b", r"today \1", query)
    days, parts, times, rest = [], [], [], []
    for word in together(query.split(), names):
        time = TIME.match(word)
        if word in DAYS:
            days.append(lambda day, n=DAYS[word]: day.weekday() == n)
        elif word in ("today", "tonight", "yesterday"):
            # Tonight is the night we're in: before 5am, it's still the evening before's.
            date = (now - (timedelta(hours=5) if word == "tonight" else timedelta(days=word == "yesterday"))).date()
            days.append(lambda day, date=date: day == date)
            if word == "tonight":
                parts.append(PARTS["tonight"])
        elif word in PARTS:
            parts.append(PARTS[word])
        elif time and (time.group(2) or time.group(3) or time.group(4)) and int(time.group(1)) <= 23:
            hour, minute = int(time.group(1)), int(time.group(2) or 0)
            if time.group(3):
                hours = [hour % 12 + (12 if time.group(3) == "pm" else 0)]
            else:
                hours = [hour % 12, hour % 12 + 12] if 1 <= hour <= 12 and not time.group(1).startswith("0") else [hour]
            spread = 45 if time.group(2) else 60
            times += [(h * 60 + minute, spread) for h in hours]
        else:
            rest.append(word)
    if not (days or parts or times):
        return None, rest

    def test(at):
        late = at.hour + (24 if at.hour < 5 else 0)  # 1am is still the night before.
        night = parts and all(low >= 17 for low, _ in parts) and at.hour < 5
        day = (at - timedelta(days=1)).date() if night else at.date()
        clock = at.hour * 60 + at.minute
        return (all(check(day) for check in days)
                and (not parts or any(low <= late < high for low, high in parts))
                and (not times or any(off(clock, m) <= spread for m, spread in times)))
    # How far a start is from the nearest time said, in minutes (the list puts the closest first).
    test.off = (lambda at: min(off(at.hour * 60 + at.minute, m) for m, _ in times)) if times else None
    return test, [word for word in rest if word not in FILLER]


def off(clock, minutes):
    """Minutes between two times of day, either way round midnight."""
    return min(abs(clock - minutes), 1440 - abs(clock - minutes))


def snippet(turns, words):
    """Where a word the list doesn't show yet is (it shows the first thing said), with a little around it; None
    when the first thing said shows every word."""
    shown = turns[0]["you"][:SAID].casefold() if turns else ""
    unseen = [w for w in words if w not in shown]
    for n, text in enumerate(part for t in turns for part in (t["you"], t["assist"])):
        found = [(i, w) for w in unseen if (i := text.casefold().find(w)) >= 0]
        if not found:
            continue
        at = min(found)[0]
        start = max(0, at - 50)
        return ("…" if start else "") + text[start:start + SAID].strip() + ("…" if start + SAID < len(text) else "")
    return None


def listed(runtime, kept, query=None, days=None, order=None, offset=0, limit=PAGE, plain=False):
    """A page of conversations: those with something said, started within the days, and matching the words: when
    (a day, a part of the day, a time), and every other word, in any order, in what the user said or Assist replied
    (any case); if none has them all, those with the most of them (close: true). With a time said, the closest to it
    come first unless an order is chosen (closest: true), and each row says it was found by when (timed); otherwise
    newest first unless chosen. Two words a conversation said that
    name something with a part of the day in it ("night light", "good morning") are words to find together, not a
    when; if none fits the when, the words are all looked for as said ("weather morning"). Sorted (ORDERS), with how
    many match, the IDs of those among them that can be sent in (to pick them all), and how many of all the
    conversations stand where."""
    pipeline, index = runtime.pipeline, runtime.recorder.index
    at, tests, fixed = pipeline._at(), pipeline._tests(), improved(runtime)
    since = (utcnow() - timedelta(days=days)).isoformat() if days else None
    texts = {rid: "\n".join(t["you"] + "\n" + t["assist"] for t in (k or {}).get("turns", [])).casefold()
             for rid, k in kept.items()}
    said = {name for name in maybe_names(query or "") if any(name in text for text in texts.values())}
    at_time, words = (None, together((query or "").casefold().split(), said)) if plain \
        else when(query or "", names=said)
    found, counts, offs = [], dict.fromkeys(("waiting", "sent", "active", "in", "improved"), 0), {}
    for rid, summary in index.items():
        if not summary.get("user_turns"):
            continue
        status = standing(runtime, rid, at, tests, fixed)
        counts[status] += 1
        if since and (summary.get("started") or "") < since:
            continue
        local = dt_util.as_local(dt_util.parse_datetime(summary["started"])) if at_time and summary.get("started") else None
        if at_time and not (local and at_time(local)):
            continue
        if at_time and at_time.off:
            offs[rid] = at_time.off(local)
        turns, text = (kept.get(rid) or {}).get("turns", []), texts.get(rid, "")
        matched = sum(1 for word in words if word in text)
        if words and not matched:
            continue
        found.append({"id": rid, "started": summary.get("started"), "turns": summary.get("user_turns"),
                      "said": (turns[0]["you"] if turns else "")[:SAID], "reply": (turns[0]["assist"] if turns else "")[:SAID],
                      "match": snippet(turns, words) if words else None,
                      "status": status, "failed": (kept.get(rid) or {}).get("failed", 0), "timed": bool(at_time),
                      "note": (summary.get("filed") or {}).get("note"), "matched": matched})
    if at_time and not found:
        return listed(runtime, kept, query, days, order, offset, limit, plain=True)
    close = bool(words) and not any(r["matched"] == len(words) for r in found)
    if words and not close:
        found = [r for r in found if r["matched"] == len(words)]
    found.sort(key=ORDERS[order or "newest"], reverse=order != "oldest")
    closest = bool(offs) and order is None
    if closest:  # A time said and no order chosen: the closest to it first.
        found.sort(key=lambda r: offs[r["id"]])
    if close:  # The most words matched first, each group in the chosen order.
        found.sort(key=lambda r: r["matched"], reverse=True)
    # Those a run took in that wait for the quick check: sent in or picked, they skip it, so the confirmation
    # counts them once.
    queued = [rid for rid, stage in at.items() if stage == "triage"]
    return {"conversations": found[offset:offset + limit], "total": len(found), "counts": counts, "close": close,
            "closest": closest, "queued": {"ids": [rid for rid in queued if not pipeline.filed(rid)],
                                           "sent": sum(1 for rid in queued if pipeline.filed(rid))},
            "waiting": [r["id"] for r in found if r["status"] == "waiting"], "on_demand": pipeline.on_demand}


def shown(runtime, kept, rid):
    """One conversation to read: what was said, turn by turn, and where it stands."""
    summary = runtime.recorder.index[rid]
    return {"id": rid, "started": summary.get("started"), "status": standing(runtime, rid),
            "turns": (kept.get(rid) or {}).get("turns", []), "note": (summary.get("filed") or {}).get("note"),
            "on_demand": runtime.pipeline.on_demand}

