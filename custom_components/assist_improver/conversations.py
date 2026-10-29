"""The conversations view: find a kept conversation, read it, and send it in, archive it, or delete it for good.

Each conversation's chip, whether the list shows it, and what can be done with it come from its flow record (flow.py,
P0.2): where it is, its outcome, its sent-in mark and the question it waits with (standing, row). Sending one in marks
it on that record (flow.Flow.sent_in), as Assist's file_improvement tool does (llm.py): it goes in at the next run that
admits it, with the user's note; with on-demand improvements on, a run starts for it as soon as it has ended.

The list shows only conversations that may need the household (DETAILS P14.4): one archived, in Your household, or up
for review (its improvement waits at the top of the home page) is left out, unless the household unarchived it where
it stands now (flow.Flow.unarchived). Archiving one takes it out of the pipeline, to Your household (P0.12); it's kept,
so later changes are still checked against it. Deleting one (Recorder.forget) removes it, with what the improver
derived from it.

What a conversation said (the user's words, and Assist's last reply to each) is read from its recording once and
kept in a small store of its own, so listing, searching and reading conversations never reopens the recordings.
"""

import asyncio
from collections import Counter
from datetime import timedelta
import re

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .cases import failed_calls, transcript
from .const import DOMAIN, STORAGE_VERSION

WORDS = 1000  # Characters kept of each message.
SAID = 160  # Characters of the first thing said, for the list.
PAGE = 30  # Conversations a page lists.


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
                self.kept = ((await self.store.async_load()) or {}).get("transcripts", {})
            index, changed = self.recorder.index, False
            for rid in [rid for rid in self.kept if rid not in index]:  # The tidy-up deleted it.
                del self.kept[rid]
                changed = True
            for rid, summary in list(index.items()):  # The index can change while a recording is read.
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
                self.store.async_delay_save(lambda: {"transcripts": self.kept}, 5)
            return self.kept


# The chip of each outcome in Your household (P0.12), and of the stages between (P14.3).
OUTCOMES = {"looked smooth": "smooth", "no trouble found": "fine", "no fix found": "nofix", "improved": "improved",
            "declined": "declined", "marked as a test": "test", "couldn't be read": "unreadable",
            "archived": "archived", "duplicate": "duplicate"}
AT = {"Analyze": "queued", "Propose": "working", "Write up": "writing"}


def standing(runtime, rid, place=None):
    """Where a conversation stands, as its chip names it, from its flow record (place: flow.Flow.place, if read): at
    Your Assist or Triage, sent if it was sent in (filed while still going too), else still going while recorded, else
    waiting (New); at Your review, unclear while a question waits for the household, else review; in Your household,
    by its outcome."""
    flow = runtime.flow
    place = place or flow.place(rid)
    stage = place["stage"]
    if stage == "Your household":
        return OUTCOMES[place["outcome"]]
    if stage == "Your review":
        return "unclear" if flow.question(rid) else "review"
    if stage in AT:
        return AT[stage]
    if flow.sent(rid):
        return "sent"
    if stage == "Your Assist" and (runtime.recorder.index.get(rid) or {}).get("status") == "active":
        return "active"
    return "waiting"


def row(runtime, rid):
    """Where a conversation stands, as the list and its page read it: its chip (status); hidden: left out of the list
    (archived, in Your household, or up for review), unless unarchived where it stands now; sendable: it can be sent
    in (from Your household, P0.12; at Your Assist, unheld; or as the answer to its question, P0.11); runnable: a
    picked run can take it (new and waiting, or held anywhere, P0.7; or in Your household, sent in first when picked
    by hand: the panel never picks those with Select all); archivable: it can be archived (not while an improvement of
    it waits for the household, flow.Flow.waits); and its stage, hold, and the improvement a duplicate points to."""
    flow = runtime.flow
    place = flow.place(rid)
    status, stage = standing(runtime, rid, place), place["stage"]
    off = place["archived"] or stage == "Your household" or status == "review"
    held = place["held"]
    return {"status": status, "hidden": bool(off) and not flow.unarchived(rid),
            "sendable": stage == "Your household" or (stage == "Your Assist" and not held) or status == "unclear",
            "runnable": flow.waiting(rid) or bool(held) or stage == "Your household",
            "archivable": not flow.waits(rid),
            "stage": stage, "held": {"cause": held["cause"], "why": held["why"]} if held else None,
            "covered_by": place["covered_by"]}


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


def listed(runtime, kept, query=None, days=None, order=None, offset=0, limit=PAGE, plain=False, archived=False,
           statuses=None, ids=()):
    """A page of conversations: those with something said, started within the days (today, and the days - 1 before
    it, from midnight in Home Assistant's time zone, as the search's 'today' is), and matching the words: when
    (a day, a part of the day, a time), and every other word, in any order, in what the user said or Assist replied
    (any case); if none has them all, those with the most of them (close: true). With a time said, the closest to it
    come first unless an order is chosen (closest: true), and each row says it was found by when (timed); otherwise
    newest first unless chosen. Two words a conversation said that name something with a part of the day in it
    ("night light", "good morning") are words to find together, not a when; if none fits the when, the words are all
    looked for as said ("weather morning"). Sorted (ORDERS), with how many match. Each row says where it stands and
    what can be done with it (row); one up for review names its improvement (review). Those the list leaves out
    (row's hidden) are listed on their own, below Show archived, when asked for (archived: those instead of the
    rest); how many there are is said either way (hidden). statuses: only those
    standing so (all of them when None); by_status: how many of those matching the rest stand each way, for choosing.
    every: each one that matches, past this page too, with where it stands (its chip and stage), whether it's left
    out, whether a run can take it and whether it can be archived (the home page's selection: Select all, what its
    actions apply to, and what a picked run's confirmation counts). stands: the same of each of
    ids still in view, whatever the filters (the selection's and the opened one's: one a run has moved out of the
    list since leaves the selection)."""
    index = runtime.recorder.index
    since = dt_util.start_of_local_day(dt_util.now().date() - timedelta(days=days - 1)) if days else None
    texts = {rid: "\n".join(t["you"] + "\n" + t["assist"] for t in (k or {}).get("turns", [])).casefold()
             for rid, k in kept.items()}
    said = {name for name in maybe_names(query or "") if any(name in text for text in texts.values())}
    at_time, words = (None, together((query or "").casefold().split(), said)) if plain \
        else when(query or "", names=said)
    found, offs, out_of_view, asked, stands = [], {}, 0, set(ids), []
    for rid, summary in index.items():
        if not summary.get("user_turns") or summary.get("deleting"):  # Deleted during a run: gone from view.
            continue
        where = row(runtime, rid)
        if rid in asked:
            stands.append({"id": rid, **where})
        out_of_view += where["hidden"]
        if where["hidden"] != archived:
            continue
        started = dt_util.parse_datetime(summary["started"]) if summary.get("started") else None
        if since and not (started and started >= since):
            continue
        local = dt_util.as_local(started) if at_time and started else None
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
                      "failed": (kept.get(rid) or {}).get("failed", 0), "timed": bool(at_time),
                      "note": (runtime.flow.sent(rid) or {}).get("note"), "matched": matched, **where,
                      **({"review": runtime.flow.review(rid)} if where["status"] == "review" else {})})
    if at_time and not found:
        return listed(runtime, kept, query, days, order, offset, limit, plain=True, archived=archived, statuses=statuses,
                      ids=ids)
    close = bool(words) and not any(r["matched"] == len(words) for r in found)
    if words and not close:
        found = [r for r in found if r["matched"] == len(words)]
    by_status = dict(Counter(r["status"] for r in found))
    if statuses is not None:
        found = [r for r in found if r["status"] in statuses]
    found.sort(key=ORDERS[order or "newest"], reverse=order != "oldest")
    closest = bool(offs) and order is None
    if closest:  # A time said and no order chosen: the closest to it first.
        found.sort(key=lambda r: offs[r["id"]])
    if close:  # The most words matched first, each group in the chosen order.
        found.sort(key=lambda r: r["matched"], reverse=True)
    return {"conversations": found[offset:offset + limit], "total": len(found), "close": close, "closest": closest,
            "on_demand": runtime.pipeline.on_demand,
            "every": [{k: r[k] for k in ("id", "status", "stage", "hidden", "runnable", "archivable")} for r in found],
            "hidden": out_of_view, "by_status": by_status, "stands": stands}


def kept(runtime, rid):
    """Whether the conversations view still has a conversation: kept, and not deleted during a run (deleting)."""
    summary = runtime.recorder.index.get(rid)
    return summary is not None and not summary.get("deleting")


def shown(runtime, kept, rid):
    """One conversation to read: what was said, turn by turn, and where it stands (row)."""
    return {"id": rid, "started": runtime.recorder.index[rid].get("started"), **row(runtime, rid),
            "turns": (kept.get(rid) or {}).get("turns", []), "note": (runtime.flow.sent(rid) or {}).get("note"),
            "on_demand": runtime.pipeline.on_demand}
