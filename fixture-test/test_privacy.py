"""Privacy controls, in an opened conversation: Archive (out of the list and the pipeline, kept for the improver) and
Delete for good (with what was derived from it)."""

from types import SimpleNamespace

import pytest

from test_changeset import _ws
from test_conversations import three
from kit import improver, seat  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import panel


async def ask(hass, handler, user="alex", **msg):
    sent = []
    connection = SimpleNamespace(user=SimpleNamespace(id=user, is_admin=True),
                                 send_result=lambda i, r: sent.append(("ok", r)),
                                 send_error=lambda i, code, text: sent.append((code, text)))
    await _ws(handler, hass, connection, {"id": 1, **msg})
    return sent[-1]


@pytest.mark.asyncio
async def test_a_forgotten_conversation_goes_with_what_was_derived_from_it(improver):
    from custom_components.assist_improver.sessions import BriefCache
    recorder, runtime = improver.recorder, improver.runtime
    await improver.arrives(improver.friction)
    await runtime.role_runs.record({"id": "a1", "kind": "journey_analyzer", "recording_id": "friction01",
                                    "status": "completed", "analysis": {"goals": []}})
    assert "friction01" in await runtime.transcripts.all()
    recorder.brief_cache = BriefCache(improver.hass)
    await recorder.brief_cache.get(recorder, "friction01")
    await runtime.replays.save({"plan": {"id": "plan-f"}, "recording_id": "friction01", "status": "reproduced"})
    await recorder.forget("friction01")
    assert "friction01" not in recorder.index and await recorder.store("friction01").async_load() is None
    assert not [r for r in runtime.role_runs.runs if r.get("recording_id") == "friction01"]
    assert "friction01" not in runtime.transcripts.kept and "friction01" not in recorder.brief_cache.briefs
    assert runtime.replays.items == {}  # Its replays, with what the user said in them, went too.


# Archive (the main one) and Delete for good, in an opened conversation on the home page.

@pytest.mark.asyncio
async def test_an_archived_conversation_leaves_the_list_and_the_pipeline_but_is_kept(improver):
    """Archived by hand: out of the list and Select all, unless shown, and out of the pipeline (P0.12: in Your
    household; no run takes it in: Kai, 2026-10-25); kept, so later changes are still checked against it. Only its
    flow record says so: the recordings' index is left as it was."""
    hass, recorder, runtime = improver.hass, improver.recorder, improver.runtime
    await three(improver)
    before = dict(recorder.index["porch0001"])
    assert await ask(hass, panel.ws_conversation_hide, recording_id="porch0001") == ("ok", {"hidden": True})
    assert recorder.index["porch0001"] == before  # Nothing else: its words aren't read again.
    place = runtime.flow.place("porch0001")
    assert (place["stage"], place["outcome"], place["archived"]) == ("Your household", "archived", True)
    _, page = await ask(hass, panel.ws_conversations)
    assert [c["id"] for c in page["conversations"]] == ["friction01", "weather01"] and page["total"] == 2
    assert [r["id"] for r in page["every"]] == ["friction01", "weather01"] and page["hidden"] == 1
    _, found = await ask(hass, panel.ws_conversations, query="porch")
    assert found["conversations"] == [] and found["hidden"] == 1
    _, shown = await ask(hass, panel.ws_conversations, archived=True)  # Show archived: below it, on their own.
    assert {c["id"]: (c["status"], c["hidden"]) for c in shown["conversations"]} == {"porch0001": ("archived", True)}
    assert [r["id"] for r in shown["every"]] == ["porch0001"] and shown["hidden"] == 1
    _, opened = await ask(hass, panel.ws_conversation, recording_id="porch0001")
    assert opened["hidden"] is True and opened["turns"]
    assert (await recorder.export("porch0001"))["turns"] and "porch0001" in await runtime.transcripts.all()
    assert not runtime.flow.waiting("porch0001")
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "test", "write"]}
    await improver.run()
    assert ("triage", "Turn on the porch light.") not in improver.model.calls  # No run takes it in.


@pytest.mark.asyncio
async def test_an_archived_conversation_comes_back_when_unarchived(improver):
    hass, runtime = improver.hass, improver.runtime
    await three(improver)
    await ask(hass, panel.ws_conversation_hide, recording_id="porch0001")
    assert await ask(hass, panel.ws_conversation_hide, recording_id="porch0001", hidden=False) == (
        "ok", {"hidden": False})
    place = runtime.flow.place("porch0001")
    assert (place["stage"], place["archived"]) == ("Your Assist", False) and runtime.flow.waiting("porch0001")
    _, page = await ask(hass, panel.ws_conversations)
    row = next(c for c in page["conversations"] if c["id"] == "porch0001")
    assert (row["status"], row["runnable"]) == ("waiting", True) and page["hidden"] == 0
    assert await ask(hass, panel.ws_conversation_hide, recording_id="nope") == ("not_found", "That conversation isn't kept.")


@pytest.mark.asyncio
async def test_one_archived_while_still_going_stays_archived_as_it_goes_on(improver):
    """Archived while Assist still records it: its record says so as it goes on, and the recordings' index carries
    nothing of it; unarchived, it's still going."""
    recorder, flow = improver.recorder, improver.runtime.flow
    recorder.on_chat("conv-going", "created", {})
    key = recorder.keys["conv-going"]
    await flow.archive(key)
    recorder.on_chat("conv-going", "content_added", {})
    assert flow.place(key)["archived"] and set(recorder.index[key]) == {"id", "started", "updated", "status",
                                                                         "user_turns"}
    await flow.archive(key, archived=False)
    recorder.on_chat("conv-going", "content_added", {})
    assert not flow.place(key)["archived"] and flow.place(key)["stage"] == "Your Assist"
    assert set(recorder.index[key]) == {"id", "started", "updated", "status", "user_turns"}


# Settled ones, and those up for review or asking the household (their improvement or question waits at the top of the
# home page), leave the list on their own, from where their flow record says they stand (conversations.row), so one
# that stands otherwise again is back by itself. Archive and Unarchive are the household's choice, and win.

KEPT = ("waiting", "sent", "active", "queued", "working", "writing")  # Need the household, or worked on.
LEFT = ("smooth", "fine", "declined", "nofix", "duplicate", "improved", "test", "unreadable", "review", "unclear",
        "archived")  # Off on their own.
SEATS = {"waiting": dict(stage="Your Assist"), "sent": dict(stage="Your Assist", sent={}),
         "active": dict(stage="Your Assist", status="active"), "queued": dict(stage="Analyze", held="failures"),
         "unclear": dict(stage="Your review", question="Which light?"), "working": dict(stage="Propose", held="scope"),
         "writing": dict(stage="Write up", held="blocked"),
         "smooth": dict(stage="Your household", outcome="looked smooth"),
         "fine": dict(stage="Your household", outcome="no trouble found"),
         "declined": dict(stage="Your household", outcome="declined", archived=True),
         "nofix": dict(stage="Your household", outcome="no fix found"),
         "duplicate": dict(stage="Your household", outcome="duplicate", covered_by="imp-other"),
         "improved": dict(stage="Your household", outcome="improved"),
         "test": dict(stage="Your household", outcome="marked as a test"),
         "unreadable": dict(stage="Your household", outcome="couldn't be read"), "review": dict(stage="Your review"),
         "archived": dict(stage="Your household", outcome="archived", archived=True)}
ALL = (*KEPT, *LEFT)


def one_each(improver):
    """A kept conversation standing each way ("fine-1" stands fine), the newest first in ALL's order."""
    for n, status in enumerate(ALL):
        seat(improver, f"{status}-1", minutes=n, **SEATS[status])


def ids(page):
    return [c["id"] for c in page["conversations"]]


@pytest.mark.asyncio
async def test_settled_ones_leave_the_list_on_their_own_and_show_with_the_archived(improver):
    """Only those that may still need the household are listed; the settled ones are counted and shown with the
    archived, choosable by status only then. Nothing is written for it."""
    from copy import deepcopy
    hass, recorder, flow = improver.hass, improver.recorder, improver.runtime.flow
    one_each(improver)
    before, records = deepcopy(recorder.index), deepcopy(flow.conversations)
    _, page = await ask(hass, panel.ws_conversations)
    assert ids(page) == [f"{s}-1" for s in KEPT] and page["total"] == 6 and page["hidden"] == 11
    assert not any(c["hidden"] for c in page["conversations"])
    assert [r["id"] for r in page["every"]] == ids(page)  # Select all: those listed.
    assert page["by_status"] == dict.fromkeys(KEPT, 1)
    _, shown = await ask(hass, panel.ws_conversations, archived=True)  # Show archived: below it, on their own.
    assert ids(shown) == [f"{s}-1" for s in LEFT] and shown["total"] == 11 and shown["hidden"] == 11
    assert all(c["hidden"] for c in shown["conversations"])
    assert {c["status"] for c in shown["conversations"]} == set(LEFT)
    assert [r["id"] for r in shown["every"]] == ids(shown)  # Their own Select all: those shown.
    assert shown["by_status"] == dict.fromkeys(LEFT, 1)
    _, chosen = await ask(hass, panel.ws_conversations, statuses=["fine", "working"])
    assert ids(chosen) == ["working-1"] and chosen["by_status"] == page["by_status"]
    _, chosen = await ask(hass, panel.ws_conversations, statuses=["fine", "working"], archived=True)
    assert ids(chosen) == ["fine-1"] and len(chosen["every"]) == 1
    _, searched = await ask(hass, panel.ws_conversations, days=7)  # Minutes old: today's, or past midnight yesterday's.
    assert searched["total"] == 6 and searched["hidden"] == 11
    for status in ALL:
        _, opened = await ask(hass, panel.ws_conversation, recording_id=f"{status}-1")
        assert opened["hidden"] is (status in LEFT)  # Its ⋮ offers Unarchive.
    assert recorder.index == before and flow.conversations == records


@pytest.mark.asyncio
async def test_one_that_stands_otherwise_again_is_back_in_the_list_by_itself(improver):
    """Sent in again, a revision asked: back in the list with nothing written; and out again once settled."""
    hass, flow = improver.hass, improver.runtime.flow
    one_each(improver)
    await ask(hass, panel.ws_conversation_send, recording_id="nofix-1")  # Sent in again: at Your Assist.
    seat(improver, "declined-1", "Propose", held="scope")  # Its revision waits at Propose, held.
    _, page = await ask(hass, panel.ws_conversations)
    assert {"nofix-1", "declined-1"} <= set(ids(page)) and page["hidden"] == 9
    seat(improver, "nofix-1", "Your household", outcome="no fix found")
    seat(improver, "declined-1", "Your household", outcome="declined", archived=True)
    _, page = await ask(hass, panel.ws_conversations)
    assert not {"nofix-1", "declined-1"} & set(ids(page)) and page["hidden"] == 11
    seat(improver, "review-1", "Propose", held="blocked")  # A revision asked: its improvement no longer waits.
    _, page = await ask(hass, panel.ws_conversations)
    assert "review-1" in ids(page) and page["hidden"] == 10


@pytest.mark.asyncio
async def test_unarchive_keeps_a_settled_one_in_the_list_while_it_stands_so(improver):
    hass, flow = improver.hass, improver.runtime.flow
    one_each(improver)
    assert await ask(hass, panel.ws_conversation_hide, recording_id="improved-1", hidden=False) == (
        "ok", {"hidden": False})
    assert flow.unarchived("improved-1")
    _, page = await ask(hass, panel.ws_conversations)
    assert "improved-1" in ids(page) and page["hidden"] == 10 and page["by_status"]["improved"] == 1
    assert not next(c for c in page["conversations"] if c["id"] == "improved-1")["hidden"]
    assert (await ask(hass, panel.ws_conversation, recording_id="improved-1"))[1]["hidden"] is False
    assert await ask(hass, panel.ws_conversation_hide, recording_id="improved-1") == ("ok", {"hidden": True})
    assert flow.place("improved-1")["archived"] and not flow.unarchived("improved-1")
    assert "improved-1" not in ids((await ask(hass, panel.ws_conversations))[1])
    await ask(hass, panel.ws_conversation_hide, recording_id="improved-1", hidden=False)
    assert "improved-1" in ids((await ask(hass, panel.ws_conversations))[1])
    record = flow.record("improved-1")
    record["outcome"] = "declined"  # It stands otherwise: the choice no longer applies.
    _, page = await ask(hass, panel.ws_conversations)
    assert "improved-1" not in ids(page) and page["hidden"] == 11
    record["outcome"] = "improved"  # As it stood when unarchived: in the list again.
    assert "improved-1" in ids((await ask(hass, panel.ws_conversations))[1])


@pytest.mark.asyncio
async def test_archive_takes_any_conversation_out_whatever_it_comes_to_stand(improver):
    """Archived from anywhere, its round ends there, archived (P0.6, P0.12): out of the list and the pipeline whatever
    it stood as; unarchived mid-round, it's back in the pipeline, new, for a new round (DETAILS P3.3.3), until it
    settles."""
    hass, flow = improver.hass, improver.runtime.flow
    one_each(improver)
    await ask(hass, panel.ws_conversation_hide, recording_id="working-1")
    _, page = await ask(hass, panel.ws_conversations, archived=True, ids=["working-1"])
    assert page["stands"][0]["status"] == "archived" and page["stands"][0]["hidden"]
    assert "working-1" not in sum(flow.admitted({"trigger": "manual"}), [])  # A full run doesn't take it in.
    await ask(hass, panel.ws_conversation_hide, recording_id="working-1", hidden=False)
    assert flow.place("working-1")["stage"] == "Your Assist" and "working-1" in ids((await ask(
        hass, panel.ws_conversations))[1])
    seat(improver, "working-1", "Your household", outcome="no trouble found")  # Settled later: out on its own.
    _, page = await ask(hass, panel.ws_conversations)
    assert "working-1" not in ids(page) and page["hidden"] == 12


@pytest.mark.asyncio
async def test_the_selection_unarchives_and_archives_settled_ones_and_others_at_once(improver):
    hass, flow = improver.hass, improver.runtime.flow
    one_each(improver)
    await ask(hass, panel.ws_conversation_hide, recording_id="queued-1")
    assert await ask(hass, panel.ws_conversations_hide, recording_ids=["fine-1", "test-1", "queued-1"],
                     hidden=False) == ("ok", {"changed": 3})
    assert all(flow.unarchived(rid) for rid in ("fine-1", "test-1", "queued-1"))
    _, page = await ask(hass, panel.ws_conversations)
    assert {"fine-1", "test-1", "queued-1"} <= set(ids(page)) and page["hidden"] == 9 and len(page["every"]) == 8
    assert await ask(hass, panel.ws_conversations_hide, recording_ids=["fine-1", "test-1", "queued-1"]) == (
        "ok", {"changed": 3})
    assert all(flow.place(rid)["archived"] for rid in ("fine-1", "test-1", "queued-1"))
    _, page = await ask(hass, panel.ws_conversations)
    assert not {"fine-1", "test-1", "queued-1"} & set(ids(page)) and page["hidden"] == 12


@pytest.mark.asyncio
async def test_the_listing_says_where_the_selected_ones_stand_whatever_the_filters(improver):
    """The home page asks where its selected ones and the opened one stand at each reload (ids), so one a run has
    settled since leaves the selection though it's out of view: each is answered past the search, the day filter, the
    statuses chosen and the archived; one gone from view (deleted during a run) or unknown isn't."""
    hass, recorder = improver.hass, improver.recorder
    one_each(improver)
    recorder.index["queued-1"]["deleting"] = True
    asked = ["working-1", "fine-1", "queued-1", "nobody"]
    stands = lambda page: {r["id"]: (r["status"], r["hidden"]) for r in page["stands"]}  # noqa: E731
    _, page = await ask(hass, panel.ws_conversations, query="nothing said like this", ids=asked)
    assert page["conversations"] == [] and stands(page) == {"working-1": ("working", False), "fine-1": ("fine", True)}
    seat(improver, "working-1", "Your household", outcome="no fix found")  # A run found no fix: off the list.
    _, page = await ask(hass, panel.ws_conversations, days=1, statuses=["waiting"], ids=asked)
    assert stands(page) == {"working-1": ("nofix", True), "fine-1": ("fine", True)}
    await ask(hass, panel.ws_conversation_hide, recording_id="fine-1", hidden=False)
    _, page = await ask(hass, panel.ws_conversations, archived=True, ids=asked)
    assert stands(page) == {"working-1": ("nofix", True), "fine-1": ("fine", False)}
    assert (await ask(hass, panel.ws_conversations))[1]["stands"] == []  # None asked for.


@pytest.mark.asyncio
async def test_delete_for_good_removes_it_and_what_was_derived_from_it(improver):
    hass, recorder, runtime = improver.hass, improver.recorder, improver.runtime
    await three(improver)
    await runtime.role_runs.record({"id": "a1", "kind": "journey_analyzer", "recording_id": "porch0001",
                                    "status": "completed", "analysis": {"goals": []}})
    assert "porch0001" in await runtime.transcripts.all() and runtime.flow.waiting("porch0001")
    assert await ask(hass, panel.ws_conversation_delete, recording_id="nope") == (
        "not_found", "That conversation isn't kept.")
    assert await ask(hass, panel.ws_conversation_delete, recording_id="porch0001") == ("ok", {"deleted": "porch0001"})
    assert "porch0001" not in recorder.index and await recorder.store("porch0001").async_load() is None
    assert not [r for r in runtime.role_runs.runs if r.get("recording_id") == "porch0001"]
    assert "porch0001" not in runtime.transcripts.kept
    _, page = await ask(hass, panel.ws_conversations)
    assert [c["id"] for c in page["conversations"]] == ["friction01", "weather01"]
    assert (await ask(hass, panel.ws_conversations, archived=True))[1]["conversations"] == []  # Nor with the archived.
    assert "porch0001" not in runtime.flow.admitted({"trigger": "manual"})[0]  # No run takes it in.
    assert "porch0001" not in runtime.flow.conversations  # Its flow record went with it.


@pytest.mark.asyncio
async def test_delete_for_good_drops_the_brief_even_before_any_proposal_has_read_the_briefs(improver):
    """After a restart the briefs aren't loaded until a run reaches Propose; a brief holds what the user said."""
    from custom_components.assist_improver.sessions import BriefCache
    hass, recorder = improver.hass, improver.recorder
    await three(improver)
    earlier = BriefCache(hass)  # Made before the restart.
    for rid in ("porch0001", "weather01"):
        await earlier.get(recorder, rid)
    await earlier.save(recorder)
    recorder.brief_cache = None
    await ask(hass, panel.ws_conversation_delete, recording_id="porch0001")
    stored = (await BriefCache(hass).store.async_load())["briefs"]
    assert "porch0001" not in stored and "weather01" in stored


@pytest.mark.asyncio
async def test_delete_for_good_drops_its_replays_and_another_conversations_stay(improver):
    hass, runtime = improver.hass, improver.runtime
    await three(improver)
    for plan_id, rid in (("plan-a", "porch0001"), ("plan-b", "porch0001"), ("plan-c", "weather01")):
        await runtime.replays.save({"plan": {"id": plan_id}, "recording_id": rid, "status": "done",
                                    "steps": [{"turn": 1, "input": "Turn on the porch light."}]})
    await ask(hass, panel.ws_conversation_delete, recording_id="porch0001")
    assert set(runtime.replays.items) == {"plan-c"}
    assert set((await runtime.replays.store.async_load())["replays"]) == {"plan-c"}


@pytest.mark.asyncio
async def test_a_conversation_deleted_while_still_going_isnt_recorded_again(improver):
    recorder = improver.recorder
    recorder.on_chat("conv-going", "created", {})
    key, before = recorder.keys["conv-going"], set(recorder.index)
    await recorder.forget(key)
    recorder.on_chat("conv-going", "content_added", {"content": {"role": "user", "content": "And the porch?"}})
    assert set(recorder.index) == before - {key} and key not in recorder.sessions and "conv-going" in recorder.ignored


@pytest.mark.asyncio
async def test_deleting_during_a_run_is_queued_out_of_view_at_once_and_done_when_the_run_ends(improver):
    hass, recorder, runtime = improver.hass, improver.recorder, improver.runtime
    await three(improver)
    run = runtime.pipeline._task = hass.loop.create_future()  # A run going.
    assert await ask(hass, panel.ws_conversation_delete, recording_id="porch0001") == (
        "ok", {"deleted": "porch0001", "queued": True})
    assert recorder.index["porch0001"]["deleting"] and await recorder.store("porch0001").async_load()  # Not yet.
    _, page = await ask(hass, panel.ws_conversations)
    assert [c["id"] for c in page["conversations"]] == ["friction01", "weather01"] and page["hidden"] == 0
    assert [r["id"] for r in page["every"]] == ["friction01", "weather01"]
    assert (await ask(hass, panel.ws_conversations, archived=True))[1]["conversations"] == []  # Nor with the archived.
    assert await ask(hass, panel.ws_conversation, recording_id="porch0001") == (
        "not_found", "That conversation isn't kept.")
    assert "porch0001" not in runtime.flow.admitted({"trigger": "manual"})[0]  # Not taken in meanwhile.
    run.set_result(None)  # The run ends.
    runtime.pipeline._ran(run)
    await hass.async_block_till_done()
    assert "porch0001" not in recorder.index and await recorder.store("porch0001").async_load() is None


@pytest.mark.asyncio
async def test_one_deleted_during_a_run_a_restart_cut_off_goes_at_the_next_start(improver):
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass, entry = improver.hass, improver.entry
    await three(improver)
    improver.runtime.pipeline._task = hass.loop.create_future()
    await ask(hass, panel.ws_conversation_delete, recording_id="porch0001")
    improver.runtime.pipeline._task = None
    await async_unload_entry(hass, entry)  # Home Assistant restarts before the run ends.
    await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    recorder = entry.runtime_data.recorder
    assert "porch0001" not in recorder.index and {"friction01", "weather01"} <= set(recorder.index)
    assert await recorder.store("porch0001").async_load() is None


@pytest.mark.asyncio
async def test_a_queued_deletion_that_fails_says_so_and_puts_the_conversation_back(improver, monkeypatch):
    hass, recorder = improver.hass, improver.recorder
    await three(improver)
    await recorder.forget_later("porch0001")
    await recorder.forget_later("weather01")
    forget, notes = recorder.forget, []

    async def broken(*keys):
        if "porch0001" in keys:
            raise OSError("The disk is full")
        await forget(*keys)
    monkeypatch.setattr(recorder, "forget", broken)
    monkeypatch.setattr("homeassistant.components.persistent_notification.async_create",
                        lambda hass, message, title=None, notification_id=None: notes.append((title, message)))
    assert await recorder.forget_queued() == ["weather01"]  # The other still goes.
    assert notes == [("Assist Improver",
                      "A conversation couldn't be deleted. Try deleting it again.")]
    assert "deleting" not in recorder.index["porch0001"] and "weather01" not in recorder.index
    _, page = await ask(hass, panel.ws_conversations)
    assert "porch0001" in [c["id"] for c in page["conversations"]]


@pytest.mark.asyncio
async def test_hundreds_are_archived_unarchived_and_deleted_at_once_each_store_written_once(improver, monkeypatch):
    """The home page's selection: a big backlog cleared in one go, quickly, with the list right after."""
    import time
    from homeassistant.util.dt import utcnow
    hass, recorder, runtime = improver.hass, improver.recorder, improver.runtime
    await three(improver)
    many = [f"bulk{n:04d}" for n in range(300)]
    for n, rid in enumerate(many):
        recorder.index[rid] = {"id": rid, "status": "session_expired", "user_turns": 1,
                               "started": utcnow().isoformat(), "updated": f"u{n}"}
    await runtime.role_runs.record({"id": "a-bulk", "kind": "journey_analyzer", "recording_id": "bulk0001",
                                    "status": "completed", "analysis": {"goals": []}})
    writes = {"index": 0, "runs": 0, "flow": 0}
    save_index, save_runs, save_flow = recorder.index_store.async_save, runtime.role_runs._save, runtime.flow.save

    async def index(data):
        writes["index"] += 1
        await save_index(data)

    async def runs():
        writes["runs"] += 1
        await save_runs()

    async def flow():
        writes["flow"] += 1
        await save_flow()
    monkeypatch.setattr(recorder.index_store, "async_save", index)
    monkeypatch.setattr(runtime.role_runs, "_save", runs)
    monkeypatch.setattr(runtime.flow, "save", flow)
    began = time.monotonic()
    assert await ask(hass, panel.ws_conversations_hide, recording_ids=[*many, "nope"]) == ("ok", {"changed": 300})
    _, page = await ask(hass, panel.ws_conversations)
    assert page["total"] == 3 and page["hidden"] == 300 and len(page["every"]) == 3
    _, shown = await ask(hass, panel.ws_conversations, archived=True)
    assert shown["total"] == 300 and {r["id"] for r in shown["every"] if r["hidden"]} == set(many)
    assert await ask(hass, panel.ws_conversations_hide, recording_ids=many[:100], hidden=False) == (
        "ok", {"changed": 100})
    assert (await ask(hass, panel.ws_conversations))[1]["total"] == 103
    _, deleted = await ask(hass, panel.ws_conversations_delete, recording_ids=many)
    assert sorted(deleted["deleted"]) == many and "queued" not in deleted
    assert not set(many) & set(recorder.index) and not [r for r in runtime.role_runs.runs if r["id"] == "a-bulk"]
    _, page = await ask(hass, panel.ws_conversations)
    assert page["total"] == 3 and page["hidden"] == 0
    assert (await ask(hass, panel.ws_conversations, archived=True))[1]["total"] == 0  # Nor with the archived.
    # Archive and Unarchive are each one write of the flow records; Delete, one of each store (the records go too).
    assert writes == {"index": 1, "runs": 1, "flow": 3}
    assert time.monotonic() - began < 15


@pytest.mark.asyncio
async def test_many_deleted_during_a_run_are_queued_together(improver):
    hass, recorder, runtime = improver.hass, improver.recorder, improver.runtime
    await three(improver)
    run = runtime.pipeline._task = hass.loop.create_future()
    assert await ask(hass, panel.ws_conversations_delete, recording_ids=["porch0001", "weather01"]) == (
        "ok", {"deleted": ["porch0001", "weather01"], "queued": True})
    assert [c["id"] for c in (await ask(hass, panel.ws_conversations))[1]["conversations"]] == ["friction01"]
    run.set_result(None)
    runtime.pipeline._ran(run)
    await hass.async_block_till_done()
    assert set(recorder.index) == {"friction01"}
