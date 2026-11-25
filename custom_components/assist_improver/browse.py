"""A tool result too large to send whole, browsed instead of cut (DETAILS P7.6.13.1). Over THRESHOLD characters of
the compact JSON a provider sends, any AI role's tool, the improver's or Home Assistant's, answers with a view of its
result and a handle: the same JSON, its first parts shown, and each part left out marked with where it is (a JSON
Pointer), its size and how many items or lines it holds. read_result (exploration.ReadResult) reads any part of it a
page at a time, or finds words in it. Each view and page is within PAGE, however long its names. Nothing is lost, but a
member whose name alone outgrows a page: read_result says so, and where the rest starts. Plain JSON only, no Home
Assistant state."""

from dataclasses import replace
from functools import cache
from itertools import accumulate, islice
import math

from homeassistant.helpers import llm
from homeassistant.helpers.json import json_dumps
from homeassistant.util.json import json_loads

THRESHOLD = 64_000  # Characters of a result's compact JSON a role gets whole.
PAGE = 32_000  # Characters of a view, and of each page read_result gives at most.
PARTS = 16  # A view shares its room among at most this many larger parts: fewer parts, with more of each.
MARK = "…"  # A marker's key: what's left out, and where.
SNIPPET = 200  # Characters of a found line shown around its first match.
NAMES = 1000  # Characters of an object's member names an error lists.
HOW = ("view shows it in part: each part left out is marked … with where it is (at, from offset), its characters "
       "and its items or lines. read_result reads any part a page at a time, or finds words in all of it.")


def size(value):
    return len(json_dumps(value))


def sized(tool, results):
    """An AI role's tool whose result, too large to send whole, comes as a view with a handle into results (its role
    call's, which its read_result reads); any other comes as it is. In place, as trace.traced_tool is, and outside it:
    the trace sees what the tool answered."""
    tool._results = results  # A tool given to a later role call answers into that one's.
    if not getattr(tool, "_sized", False):
        call = tool.async_call

        async def async_call(hass, tool_input, llm_context):
            result = await call(hass, tool_input, llm_context)
            data = getattr(result, "data", result)
            fitted = tool._results.fit(data)
            if fitted is data:
                return result
            return replace(result, data=fitted) if isinstance(result, llm.ToolResult) else fitted
        tool.async_call, tool._sized = async_call, True
    return tool


class Results:
    """The results too large to send whole in one role call, by handle."""

    def __init__(self):
        self.kept = {}

    def fit(self, data):
        """data itself, if it fits THRESHOLD; else a view of it with a handle."""
        text = json_dumps(data)
        if len(text) <= THRESHOLD:
            return data
        handle = f"r{len(self.kept) + 1}"
        self.kept[handle] = whole = json_loads(text)
        return {"too_large": {"handle": handle, "chars": len(text), "how": HOW}, "view": view(whole)}

    def read(self, handle, at="", offset=0, find=None):
        """A page of the part at a pointer from offset, or of the matches of find's words in it; else why not, naming
        what there is."""
        if handle not in self.kept:
            return {"error": f"There's no result {handle!r}: " + (
                f"the handles are {', '.join(self.kept)}." if self.kept else "no result was too large to send whole.")}
        try:
            part = resolve(self.kept[handle], at)
            return found(part, at, find, offset) if find is not None else page(part, at, offset)
        except LookupError as err:
            return {"error": str(err)}


def pointer(at, key):
    return f"{at}/{str(key).replace('~', '~0').replace('/', '~1')}"


def resolve(data, at):
    """The part of data at a JSON Pointer."""
    if at and not at.startswith("/"):
        raise LookupError(f"{at!r} isn't a pointer: one starts with / as the view gives it, or is empty for the whole "
                          "result.")
    part, path = data, ""
    for token in at.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(part, dict) and key in part:
            part = part[key]
        elif isinstance(part, list) and key.isdigit() and int(key) < len(part):
            part = part[int(key)]
        else:
            raise LookupError(f"{at!r} isn't in this result: {_holds(part, path)}")
        path = pointer(path, key)
    return part


def _holds(part, path):
    """What the part at path holds, for an error."""
    where = repr(path) if path else "the result"
    if isinstance(part, dict):
        names = list(islice(map(repr, part), 20))
        while names and len(", ".join(names)) > NAMES:  # A long name isn't listed.
            names.pop()
        more = len(part) - len(names)
        return f"{where} has " + (f"the keys {', '.join(names)}" + (f" and {more} more" if more else "")
                                  if names else f"{more} keys") + "."
    if isinstance(part, list):
        return f"{where} is a list of {len(part)} items" + (f" (0 to {len(part) - 1})." if part else ".")
    if isinstance(part, str):
        return f"{where} is a text of {len(part)} characters."
    return f"{where} is a value, with no parts."


def view(value, at="", budget=PAGE):
    """value as it fits budget characters, which hold at least its least: whole, or its first parts, each part left
    out marked."""
    whole = size(value)
    if whole <= budget:
        return value
    if isinstance(value, str):
        return {MARK: _cut(value, at, budget)}
    mark = MARK
    while isinstance(value, dict) and mark in value:  # A member named … keeps its name: the rest's marker takes …….
        mark += MARK
    most = {mark: {"at": at, "offset": len(value), "items": len(value), "chars": whole}}  # The rest's marker, at most.
    part, upto, used = entries(value, at, 0, budget - size(most) - 1)
    if not upto:
        return {MARK: {"at": at, "items": len(value), "chars": whole}}
    if upto < len(value):
        rest = {"at": at, "offset": upto, "items": len(value) - upto, "chars": whole - used}
        if isinstance(part, list):
            part.append({MARK: rest})
        else:
            part[mark] = rest
    return part


def least(value, at, whole):
    """The fewest characters value (whole of them) takes in a view: whole, or marked as left out."""
    if isinstance(value, str):
        return min(whole, size({MARK: {"at": at, "offset": 0, "chars": len(value), "lines": len(value) + 1}}))
    if isinstance(value, (list, dict)):
        return min(whole, size({MARK: {"at": at, "items": len(value), "chars": whole}}))
    return whole


def entries(value, at, offset, budget):
    """A list's items or an object's members from offset, as many as budget characters show: each whole, or the
    larger ones each viewed in an equal share of what's left, none in less than its least. (part, where it stopped,
    its characters)."""
    keyed = isinstance(value, dict)
    pairs = list(value.items() if keyed else enumerate(value))[offset:offset + max(budget, 0) // 2]  # Two or more each.
    names = [size(k) + 2 if keyed else 1 for k, _ in pairs]  # A member's name, colon and comma; an item's comma.
    wholes = [size(v) for _, v in pairs]
    costs = [name + whole for name, whole in zip(names, wholes)]
    floor = cache(lambda i: names[i] + least(pairs[i][1], pointer(at, pairs[i][0]), wholes[i]))  # Of the larger.
    n, share = _fit(costs, floor, budget - 2)  # The brackets.
    shown = [(k, v if cost <= share else view(v, pointer(at, k), int(share) - name))
             for (k, v), name, cost in zip(pairs[:n], names, costs)]
    return (dict(shown) if keyed else [v for _, v in shown]), offset + n, sum(costs[:n])


def _fit(costs, floor, budget):
    """How many of these entries, in order, a budget shows, and the share each larger one gets: as many as leave each
    whole, or with at least a PARTS-th of the budget and never less than its floor (floor(i): its least)."""
    def level(n):  # The share that spends the budget on the first n, each costing at most it; inf: all whole.
        room, ordered = budget, sorted(costs[:n])
        for i, cost in enumerate(ordered):
            if cost * (n - i) > room:
                return room / (n - i)
            room -= cost
        return math.inf

    def shows(n):
        share = level(n)
        return share >= budget / PARTS and all(floor(i) <= share for i in range(n) if costs[i] > share)
    low, high = 0, len(costs)
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if shows(middle) else (low, middle - 1)
    return low, level(low)


def _cut(text, at, budget):
    """A text too long for budget: its first lines, then where the rest is, its characters and lines."""
    most = {MARK: {"text": "", "at": at, "offset": len(text), "chars": len(text), "lines": len(text) + 1}}
    shown = _window(text, 0, budget - size(most) + 2)  # Its quotes are counted in most.
    rest = text[len(shown):]
    return {**({"text": shown} if shown else {}), "at": at, "offset": len(shown), "chars": len(rest),
            "lines": rest.count("\n") + (not rest.endswith("\n"))}


def _window(text, offset, room):
    """The text from offset that takes at most room characters of JSON, ending at a line's end where one falls in it."""
    end = min(len(text), offset + max(room, 0))
    while end > offset and (taken := size(text[offset:end])) > room:  # Escapes take more than one character.
        end = offset + (end - offset) * room // taken
    if end < len(text) and (cut := text.rfind("\n", offset, end)) > offset:
        end = cut + 1
    return text[offset:end]


def page(part, at, offset):
    """read_result's page of the part at a pointer, from offset: of a text by characters, a line's end closing it where
    one falls in it; of a list or object by items, each whole or viewed; with next while more remains. Within PAGE."""
    if not isinstance(part, (str, list, dict)):
        return {"at": at, "value": part}
    if offset < 0 or offset and offset >= len(part):
        raise LookupError(f"Offset {offset} is past the end: {_holds(part, at)}")
    end = len(part)
    if isinstance(part, str):
        text = _window(part, offset, PAGE - size({"at": at, "offset": end, "chars": end, "text": "", "next": end}) + 2)
        if text or not end:
            return {"at": at, "offset": offset, "chars": end, "text": text,
                    **({"next": offset + len(text)} if offset + len(text) < end else {})}
    else:
        room = PAGE - size({"at": at, "offset": end, "items": end, "part": [], "next": end}) + 2
        shown, upto, _ = entries(part, at, offset, room)
        if upto > offset or not end:
            return {"at": at, "offset": offset, "items": end, "part": shown, **({"next": upto} if upto < end else {})}
    raise LookupError(f"Offset {offset} here can't be shown within a page, even marked: its pointer is too long"
                      + (f"; the rest starts at offset {offset + 1}." if offset + 1 < end and not isinstance(part, str)
                         else "."))


def found(part, at, words, offset):
    """The places under the part at a pointer whose words hold every one of words (plurals too; failing that any,
    saying so), in order, a page at a time from offset, with the total: each one's pointer, where its text starts in
    a text (offset), and its text around the first match. Within PAGE."""
    from .exploration import _match
    units = list(_units(part, at))
    hits, matched = _match({i: label + line for i, (_, _, label, line) in enumerate(units)}, words)
    if offset < 0 or offset and offset >= len(hits):
        raise LookupError(f"Offset {offset} is past the end: {len(hits)} places hold {words!r}.")
    wanted = [w.rstrip("s") or w for w in words.casefold().split()]
    room = PAGE - size({"find": words, "matched": matched, "total": len(hits), "matches": [], "next": len(hits)})
    matches = []
    for i in hits[offset:]:
        where, start, label, line = units[i]
        text = label + line
        around = _around(text, wanted)
        match = {"at": where, **({"offset": start + max(0, around - len(label))} if start is not None else {}),
                 "text": ("…" if around else "") + text[around:around + SNIPPET]
                 + ("…" if around + SNIPPET < len(text) else "")}
        room -= size(match) + 1
        if room < 0:
            break
        matches.append(match)
    if hits[offset:] and not matches:
        raise LookupError(f"Match {offset} can't be shown within a page: its pointer is too long"
                          + (f"; the rest starts at offset {offset + 1}." if offset + 1 < len(hits) else "."))
    upto = offset + len(matches)
    return {"find": words, "matched": matched, "total": len(hits), "matches": matches,
            **({"next": upto} if upto < len(hits) else {})}


def _units(value, at, label=""):
    """What find reads, in order: (pointer, where the line starts in a text or None, its member's name, its words):
    a member's name, with its value unless that's a list or object; a list's item; each line of a text."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield pointer(at, key), None, "", key
            yield from _units(item, pointer(at, key), f"{key}: ")
    elif isinstance(value, list):
        for n, item in enumerate(value):
            yield from _units(item, pointer(at, n))
    elif isinstance(value, str):
        start = 0
        for n, line in enumerate(value.split("\n")):
            yield at, start, label if n == 0 else "", line
            start += len(line) + 1
    else:
        yield at, None, label, json_dumps(value)


def _around(text, words):
    """Where a found text's snippet starts: just before its first match, in a long text."""
    if len(text) <= SNIPPET:
        return 0
    folded = text.casefold()
    first = min((i for w in words if (i := folded.find(w)) >= 0), default=0)
    if len(folded) != len(text):  # Casefolding made a letter longer (ß is ss): where the match is in text.
        first = next(n for n, end in enumerate(accumulate(len(c.casefold()) for c in text)) if end > first)
    return max(0, min(first - SNIPPET // 4, len(text) - SNIPPET))
