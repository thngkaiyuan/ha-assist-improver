"""A tool result too large to send whole is browsed, never cut (DETAILS P7.6.13.1): over 64,000 characters of the
compact JSON a provider sends, any AI role's tool, the improver's or Home Assistant's, answers with a 32,000-character
view of its result and a handle, each part left out marked with where it is, its size and how many items or lines it
holds; read_result reads any part of the rest a page at a time, or finds words in it. (Kai, 2026-11-25: "make sure we
give them a tool to manage the size, not remove that ability.")"""

from datetime import UTC, datetime
import json
import random
from types import SimpleNamespace

import probatio
import pytest
from homeassistant.core import Context
from homeassistant.helpers import llm
from homeassistant.helpers.json import json_dumps

from recordings import make_recording
from test_ai_roles import TASK, task_entity
from test_milestone1 import recording_hass  # noqa: F401  (fixture)
from custom_components.assist_improver import exploration
from custom_components.assist_improver.browse import MARK, PAGE, THRESHOLD, pointer, size
from custom_components.assist_improver.exploration import RoleTool, SessionAPI, SessionView
from custom_components.assist_improver.ha_tools import AsUser
from custom_components.assist_improver.reflective import run_role


class Holding(RoleTool):
    """One of the improver's tools, answering with what it holds."""
    name, description, parameters = "holding", "Gives what it holds.", probatio.Schema({})

    def __init__(self, data, name=None):
        super().__init__(None)
        self.data, self.name = data, name or self.name

    def run(self):
        return self.data


def context():
    return llm.LLMContext(platform="ai_task", context=Context(), language="en", assistant=None, device_id=None)


async def instance_of(hass, *tools, monkeypatch=None, theirs=()):
    """A role call's tools as Home Assistant's AI Task gets them: these, and Home Assistant's (theirs, through
    AsUser, as ha_tools gives them)."""
    if monkeypatch is not None:
        async def given(hass, user_id):
            return [AsUser(tool, context().context) for tool in theirs]
        monkeypatch.setattr(exploration, "ha_tools", given)
    return await SessionAPI(hass, tools=list(tools)).async_get_api_instance(context())


async def call(instance, name, **args):
    """A tool's answer as the model gets it: Home Assistant calls the tool, and the provider sends its JSON."""
    result = await instance.async_call_tool(llm.ToolInput(tool_name=name, tool_args=args))
    return json.loads(json_dumps(result.data))


async def whole(instance, handle, at, offset=0):
    """Everything at a pointer from offset, read page after page to the end, each page's markers followed."""
    out = None
    while True:
        page = await call(instance, "read_result", handle=handle, at=at, offset=offset)
        assert "error" not in page, page
        assert len(json_dumps(page)) <= PAGE  # read_result's own pages are never sized: each holds a page.
        if "value" in page:
            return page["value"]
        if "text" in page:
            out = (out or "") + page["text"]
        elif isinstance(page["part"], list):
            out = (out or []) + [await rebuilt(instance, handle, item, pointer(at, offset + n))
                                 for n, item in enumerate(page["part"])]
        else:
            out = {**(out or {}), **{key: await rebuilt(instance, handle, item, pointer(at, key))
                                     for key, item in page["part"].items()}}
        if "next" not in page:
            return out
        offset = page["next"]


def marker(shown, at):
    """The key of the marker in a viewed object at a pointer: its last member's, … (or …… beside a member named …)
    marking where it is itself; else None."""
    last = next(reversed(shown), None) if isinstance(shown, dict) else None
    if last and set(last) == {MARK} and isinstance(shown[last], dict) and shown[last].get("at") == at:
        return last
    return None


async def rebuilt(instance, handle, shown, at=""):
    """What a view (or a part of a page) stands for: what it shows, and each part left out read with read_result."""
    if (mark := marker(shown, at)) is not None:
        left = shown[mark]
        if "lines" in left:  # A text: its first lines, then the rest.
            return left.get("text", "") + await whole(instance, handle, left["at"], left["offset"])
        here = {key: await rebuilt(instance, handle, item, pointer(at, key)) for key, item in shown.items()
                if key != mark}
        rest = await whole(instance, handle, left["at"], left.get("offset", 0))
        return {**here, **rest} if isinstance(rest, dict) else rest
    if isinstance(shown, dict):
        return {key: await rebuilt(instance, handle, item, pointer(at, key)) for key, item in shown.items()}
    if isinstance(shown, list):
        items, rest = list(shown), []
        if items and marker(items[-1], at) == MARK:  # The list's rest.
            rest = await whole(instance, handle, at, items.pop()[MARK]["offset"])
        return [await rebuilt(instance, handle, item, pointer(at, n)) for n, item in enumerate(items)] + rest
    return shown


def markers(shown, at=""):
    """Every marker of a part left out in a view."""
    if isinstance(shown, dict):
        mark = marker(shown, at)
        return ([shown[mark]] if mark is not None else []) + [m for key, item in shown.items() if key != mark
                                                              for m in markers(item, pointer(at, key))]
    if isinstance(shown, list):
        return [m for n, item in enumerate(shown) for m in markers(item, at if marker(item, at) else pointer(at, n))]
    return []


def history(monkeypatch, days):
    """Home Assistant's own history tool, its recorder stood in (rows as the recorder returns them): daily means."""
    from homeassistant.components.history import llm as history_llm
    from homeassistant.components.recorder.models import StatisticMeanType
    meta = {"mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "unit_class": "temperature",
            "unit_of_measurement": "°C"}
    first = datetime(2025, 11, 25, tzinfo=UTC).timestamp()
    rows = [{"start": first + day * 86400, "mean": 20 + day % 7 / 10, "min": 17 + day % 5 / 10,
             "max": 23 + day % 3 / 10} for day in range(days)]
    monkeypatch.setattr(history_llm.statistics, "get_metadata",
                        lambda hass, statistic_ids: {s: (1, meta) for s in statistic_ids})
    monkeypatch.setattr(history_llm.statistics, "get_display_unit", lambda *args: "°C")
    monkeypatch.setattr(history_llm.statistics, "statistics_during_period",
                        lambda hass, start, end, ids, period, units, types: {s: rows for s in ids})
    monkeypatch.setattr(history_llm, "get_instance", lambda hass: SimpleNamespace(
        keep_days=10, async_add_executor_job=hass.async_add_executor_job))
    tool = history_llm.GetHistoryTool()
    ids = next(str(key) for key in tool.parameters.schema if isinstance(key, probatio.Required))  # Its name varies.
    return tool, lambda sensors: {ids: [f"sensor.room_{n}_temperature" for n in range(sensors)], "period": "day",
                                  "start_time": "2025-11-25T00:00:00+00:00", "end_time": "2026-11-25T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_a_result_within_the_threshold_reaches_the_model_as_it_is(hass, monkeypatch):
    """Ours and Home Assistant's alike: the very result, byte for byte, up to 64,000 characters; a view past it."""
    at_most = {"text": "x" * (THRESHOLD - size({"text": ""}))}
    one_more = {"text": at_most["text"] + "x"}
    theirs, week = history(monkeypatch, 7)
    instance = await instance_of(hass, Holding(at_most), Holding(one_more, "one_more"), monkeypatch=monkeypatch,
                                 theirs=[theirs])
    ours = await instance.async_call_tool(llm.ToolInput(tool_name="holding", tool_args={}))
    assert size(at_most) == THRESHOLD and ours.data is at_most
    alone = await theirs.async_call(hass, llm.ToolInput(tool_name=theirs.name, tool_args=week(2)), context())
    through = await instance.async_call_tool(llm.ToolInput(tool_name=theirs.name, tool_args=week(2)))
    assert json_dumps(through.data) == json_dumps(alone.data) and through.error == alone.error
    assert "too_large" in await call(instance, "one_more")


@pytest.mark.asyncio
async def test_a_view_and_its_pages_rebuild_the_whole_result_whatever_its_shape(hass):
    """Nothing is lost: for results of many shapes (objects, lists, CSV, one giant line, unicode and escapes, numbers,
    keys a pointer escapes, long keys at every level, a member named …), the view and each page are at most a page,
    each part left out is marked with where it is, its size and its count, and its markers followed with read_result
    give back the result exactly."""
    rng = random.Random(20261125)
    word = lambda: rng.choice(["a", "state", "csv", "sensor.t7", "a/b", "x~y", "温度", "", "~1", MARK,  # noqa: E731
                               "k" * 250, "é/" * 600])

    def shape(depth=0):
        kind = rng.choice(["number", "word", "text"] + ["object", "list", "csv", "line"] * (depth < 4))
        if kind == "object":
            return {word() + str(rng.randint(0, 99)): shape(depth + 1) for _ in range(rng.randint(0, 14 >> depth))}
        if kind == "list":
            return [shape(depth + 1) for _ in range(rng.randint(0, rng.choice([5, 60, 400]) >> 2 * depth))]
        if kind == "csv":
            return "\n".join(",".join(str(rng.randint(0, 999)) for _ in range(4))
                             for _ in range(rng.randint(1, 4000 >> depth))) + rng.choice(["", "\n"])
        if kind == "line":
            return rng.choice(["ab cé", "温度🙂", '"\\', "\t\x01"]) * rng.randint(1, 30_000 >> depth)
        if kind == "text":
            return rng.choice(["héllo", "温度 22 °C", "🙂", 'a "quote"', "back\\slash", "a/b~c", "", "line\nline"])
        if kind == "word":
            return rng.choice([True, False, None, "on", "off"])
        return rng.choice([rng.randint(-10**15, 10**15), rng.random() * 1000, 0, -0.5])
    long = "x" * 250  # The review's shapes: nested long keys, many very long keys, a member named … beside the rest.
    given = [{f"a{i}{long}": {f"b{j}{long}": {"v": "y" * 5000} for j in range(30)} for i in range(30)},
             {f"k{i}" + "x" * 2000: {"v": "y" * 50_000} for i in range(40)},
             {MARK: "real", **{f"k{i}": "z" * 3000 for i in range(200)}}]
    shapes = 0
    while shapes < 43:
        data = given.pop() if given else {"result": shape(), "more": [shape() for _ in range(rng.randint(0, 3))]}
        if size(data) <= THRESHOLD:
            continue
        shapes += 1
        instance = await instance_of(hass, Holding(data))
        got = await call(instance, "holding")
        assert got["too_large"]["chars"] == size(data) and len(json_dumps(got)) <= THRESHOLD
        assert size(got["view"]) <= PAGE
        assert all({"at", "chars"} <= set(m) and ("items" in m) != ("lines" in m) for m in markers(got["view"]))
        assert await rebuilt(instance, got["too_large"]["handle"], got["view"]) == json.loads(json_dumps(data))


@pytest.mark.asyncio
async def test_read_result_pages_lists_by_item_texts_by_line_and_one_line_by_characters_and_says_what_went_wrong(hass):
    log = {"entries": [{"n": n, "message": "boom " * 60} for n in range(500)],
           "csv": "\n".join(f"2026-01-{n % 28 + 1:02d},{n}" for n in range(9000)), "line": "word " * 30_000}
    instance = await instance_of(hass, Holding(log))
    handle = (await call(instance, "holding"))["too_large"]["handle"]
    read = lambda **args: call(instance, "read_result", handle=handle, **args)  # noqa: E731
    first = await read(at="/entries")
    assert first["items"] == 500 and first["part"][0] == log["entries"][0] and first["next"] == len(first["part"])
    following = await read(at="/entries", offset=first["next"])
    assert following["part"][0] == log["entries"][first["next"]]
    text = await read(at="/csv")
    assert text["text"].endswith("\n") and log["csv"].startswith(text["text"]) and text["next"] == len(text["text"])
    line = await read(at="/line", offset=1)
    assert "\n" not in log["line"] and log["line"][1:].startswith(line["text"]) and line["next"] == 1 + len(line["text"])
    pages, offset = 0, 0
    while offset is not None:  # next, until the end.
        page = await read(at="/csv", offset=offset)
        pages, offset = pages + 1, page.get("next")
    assert pages > 1 and page["text"] == log["csv"][-len(page["text"]):]
    errors = {"past the end": await read(at="/entries", offset=500), "no handle": await call(
        instance, "read_result", handle="r9"), "no such part": await read(at="/entries/600/n"),
        "no pointer": await read(at="entries"), "past a text": await read(at="/csv", offset=10**6)}
    assert errors["past the end"] == {"error": "Offset 500 is past the end: '/entries' is a list of 500 items "
                                               "(0 to 499)."}
    assert errors["no handle"] == {"error": f"There's no result 'r9': the handles are {handle}."}
    assert errors["no such part"]["error"].endswith("'/entries' is a list of 500 items (0 to 499).")
    assert errors["no pointer"] == {"error": "'entries' isn't a pointer: one starts with / as the view gives it, or "
                                             "is empty for the whole result."}
    assert "a text of" in errors["past a text"]["error"]
    named = await instance_of(hass, Holding({"k" * 100_000: "on", "after": "the rest", "none": [], "blank": ""}))
    assert len(json_dumps(await call(named, "holding"))) <= PAGE
    assert await call(named, "read_result", handle="r1") == {
        "error": "Offset 0 here can't be shown within a page, even marked: its pointer is too long; the rest starts "
                 "at offset 1."}
    assert (await call(named, "read_result", handle="r1", offset=1))["part"] == {
        "after": "the rest", "none": [], "blank": ""}  # The rest: a name over a page is all it can't show.
    assert await call(named, "read_result", handle="r1", at="/none") == {"at": "/none", "offset": 0, "items": 0,
                                                                          "part": []}
    assert (await call(named, "read_result", handle="r1", at="/blank"))["text"] == ""
    with pytest.raises(probatio.Invalid):  # An argument its schema refuses: Home Assistant tells the model.
        await call(instance, "read_result", handle=handle, offset="two")


@pytest.mark.asyncio
async def test_find_gives_each_place_a_key_a_value_or_a_line_of_a_text_holds_the_words_paged_with_the_total(hass):
    statistics = {f"sensor.t{n}": {"unit": "°C", "csv": "\n".join(f"2026-{m:02d}-01,{20 + n}.{m}" for m in
                                                                  range(1, 13)) + "\n"} for n in range(10)}
    data = {"statistics": statistics, "pad": ["x" * 300] * 300, "note": "the porch light stays on",
            "street": "Straße " * 100 + "the garden gate" + " Straße" * 100}  # Casefolded, ß is ss: longer.
    instance = await instance_of(hass, Holding(data))
    handle = (await call(instance, "holding"))["too_large"]["handle"]
    find = lambda words, **args: call(instance, "read_result", handle=handle, find=words, **args)  # noqa: E731
    key = await find("sensor.t7")
    assert key["total"] == 1 and key["matches"] == [{"at": "/statistics/sensor.t7", "text": "sensor.t7"}]
    value = await find("porch lights")  # Plurals too.
    assert value["matches"] == [{"at": "/note", "offset": 0, "text": "note: the porch light stays on"}]
    [gate] = (await find("garden gate"))["matches"]
    assert gate["at"] == "/street" and "the garden gate" in gate["text"]
    read = await call(instance, "read_result", handle=handle, at="/street", offset=gate["offset"])
    assert gate["text"].startswith("…") and read["text"].startswith(gate["text"][1:-1])  # Read from the snippet.
    line = await find("2026-05-01 27.5")
    [match] = line["matches"]
    assert match["at"] == "/statistics/sensor.t7/csv" and match["text"] == "2026-05-01,27.5"
    page = await call(instance, "read_result", handle=handle, at=match["at"], offset=match["offset"])
    assert page["text"].startswith("2026-05-01,27.5\n")  # A pointer and offset read_result takes.
    every = await find("2026-05-01")
    assert every["total"] == 10 and every["matched"] == "all words" and "next" not in every
    some = await find("2026-05-01 nothing")
    assert some["matched"] == "any word (nothing had all of them)" and some["total"] == 10
    pages, offset, places = 0, 0, []
    while offset is not None:  # Every padded line: a page at a time, each from where the last stopped.
        many = await find("x", offset=offset)
        assert many["total"] == 300 and len(json_dumps(many)) <= THRESHOLD
        pages, offset, places = pages + 1, many.get("next"), places + [m["at"] for m in many["matches"]]
    assert pages > 1 and places == [f"/pad/{n}" for n in range(300)]
    assert (await find("x", offset=300))["error"].startswith("Offset 300 is past the end")


@pytest.mark.asyncio
async def test_every_tool_a_role_call_offers_is_covered_a_new_home_assistant_tool_too_and_read_result_never_is(
        hass, monkeypatch):
    """With no code naming any tool: ours, Home Assistant's through AsUser, and a tool Home Assistant adds tomorrow;
    read_result's own pages stay within the threshold, however large the part it reads. A role without tools gets
    none."""
    class Tomorrows(llm.Tool):
        name, description, parameters = "newthing__list_everything", "Lists everything.", probatio.Schema({})
        annotations, integration = llm.ToolAnnotations(read_only=True), "newthing"

        async def async_call(self, hass, tool_input, llm_context):
            return llm.ToolResult(data={"everything": [{"n": n, "text": "word " * 50} for n in range(2000)]})
    assert (await SessionAPI(hass, tools=[]).async_get_api_instance(context())).tools == []
    instance = await instance_of(hass, Holding({"big": "y" * 100_000}), monkeypatch=monkeypatch, theirs=[Tomorrows()])
    assert [t.name for t in instance.tools] == ["holding", "newthing__list_everything", "read_result"]
    assert all(getattr(t, "_sized", False) for t in instance.tools[:-1]) and not getattr(
        instance.tools[-1], "_sized", False)
    ours, new = await call(instance, "holding"), await call(instance, "newthing__list_everything")
    assert (ours["too_large"]["handle"], new["too_large"]["handle"]) == ("r1", "r2")
    page = await call(instance, "read_result", handle="r2", at="/everything", offset=3)
    assert page["part"][0]["n"] == 3 and len(json_dumps(page)) <= THRESHOLD
    giant = await call(instance, "read_result", handle="r1")
    assert len(json_dumps(giant)) <= THRESHOLD and "big" in giant["part"]


@pytest.mark.asyncio
async def test_real_shapes_a_year_of_ten_statistics_and_a_pasted_request_come_as_views_and_read_to_the_end(
        hass, monkeypatch):
    """Home Assistant's own history tool, a year of ten temperatures (185K), and a turn whose request was a 160K paste:
    both come as views; a full CSV row and the request's end are readable."""
    tool, year = history(monkeypatch, 365)
    alone = (await tool.async_call(hass, llm.ToolInput(tool_name=tool.name, tool_args=year(10)), context())).data
    instance = await instance_of(hass, monkeypatch=monkeypatch, theirs=[tool])
    got = await call(instance, tool.name, **year(10))
    assert got["too_large"]["chars"] == size(alone) > THRESHOLD and len(json_dumps(got)) <= PAGE + 1000
    [(statistics, rows)] = [(f"/result/{k}/sensor.room_7_temperature/csv", v["sensor.room_7_temperature"]["csv"])
                            for k, v in alone["result"].items() if isinstance(v, dict)]
    row = rows.split("\n")[200]
    [match] = [m for m in (await call(instance, "read_result", handle="r1", find=row))["matches"]
               if m["at"] == statistics]
    assert (await call(instance, "read_result", handle="r1", at=statistics, offset=match["offset"]))[
        "text"].startswith(row + "\n")
    paste = "Here is what I want: " + "the hallway lamp at dusk, the porch at night; " * 3500 + "and keep it lit."
    record = json.loads(json.dumps(make_recording()).replace("Toggle the fixture relay.", paste))  # Turn 1's request.
    instance = await SessionAPI(hass, SessionView(record)).async_get_api_instance(context())
    turn = await call(instance, "read_turn", turn=1)
    left = turn["view"]["user_says"][MARK]
    assert len(paste) > 160_000 and paste.startswith(left["text"]) and left["at"] == "/user_says"
    rest = await whole(instance, turn["too_large"]["handle"], left["at"], left["offset"])
    assert left["text"] + rest == paste and rest.endswith("and keep it lit.")


@pytest.mark.asyncio
async def test_through_home_assistants_ai_task_no_tool_result_sent_is_over_the_threshold_and_the_tools_never_change(
        recording_hass, effect_guards):
    """The Anthropic integration's own requests: the model calls a large tool, then pages and searches it with
    read_result; no tool result it sends is over 64,000 characters, and every request offers the same tools (the
    prompt cache holds)."""
    _, client = task_entity(recording_hass, "Done.")
    data = {"entries": [{"n": n, "message": "boom " * 100} for n in range(400)]}
    use = lambda n, name, args: [{"type": "tool_use", "id": n, "name": name, "input": args}]  # noqa: E731
    client.blocks = [use("t1", "holding", {}), use("t2", "read_result", {"handle": "r1", "at": "/entries",
                                                                           "offset": 100}),
                     use("t3", "read_result", {"handle": "r1", "find": "399"}), [{"type": "text", "text": "Done."}]]
    run = await run_role(recording_hass, role="r", entity_id=TASK, instructions="i", payload={}, structure=None,
                         llm_api=SessionAPI(recording_hass, tools=[Holding(data)]), allow_paid_inference=True)
    assert run["status"] == "completed", run["error"]
    assert len(client.requests) == 4 and len({json.dumps(r["tools"], sort_keys=True) for r in client.requests}) == 1
    assert [t["name"] for t in client.requests[0]["tools"]] == ["holding", "read_result"]
    sent = [block["content"] for message in client.requests[-1]["messages"] if isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"]
    assert len(sent) == 3 and all(len(content) <= THRESHOLD for content in sent) and size(data) > THRESHOLD
    view, page, found = map(json.loads, sent)
    assert view["too_large"]["handle"] == "r1" and page["part"][0] == data["entries"][100]
    assert found["matches"] == [{"at": "/entries/399/n", "text": "n: 399"}] and effect_guards == []


@pytest.mark.asyncio
async def test_handles_belong_to_one_role_call(hass):
    """A second role call can't read the first's results; a tool given to a later call answers into that one's."""
    shared = Holding({"big": "z" * 70_000})
    first = await SessionAPI(hass, tools=[shared]).async_get_api_instance(context())
    assert (await call(first, "holding"))["too_large"]["handle"] == "r1"
    second = await SessionAPI(hass, tools=[Holding({"small": 1})]).async_get_api_instance(context())
    assert await call(second, "read_result", handle="r1") == {
        "error": "There's no result 'r1': no result was too large to send whole."}
    later = await SessionAPI(hass, tools=[shared]).async_get_api_instance(context())
    assert (await call(later, "holding"))["too_large"]["handle"] == "r1"
    assert "part" in await call(later, "read_result", handle="r1")
    assert (await call(first, "read_result", handle="r2"))["error"].endswith("the handles are r1.")
