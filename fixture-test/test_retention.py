"""Retention: a closed recording is packed losslessly, older ones nothing needs are compacted, empty ones deleted."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util.dt import parse_datetime

from recordings import TARGET, make_recording
from kit import improver  # noqa: F401 - a fixture.
from custom_components.assist_improver import retention, triage
from custom_components.assist_improver.cases import CaseError, recording_turns
from custom_components.assist_improver.exploration import SessionView


def with_home(record, extra=40):
    """The fixture recording in a bigger home: devices the conversation never touched, in every turn's snapshot."""
    record = deepcopy(record)
    for turn in record["turns"]:
        for n in range(extra):
            entity = f"light.other_{n}"
            turn["source"]["states"][entity] = {"entity_id": entity, "state": "off", "attributes": {}}
            turn["source"]["entities"][entity] = {"name": f"Other {n}", "aliases": [], "area_id": "fixture_area"}
    record["events"] = [{"at": "x", "type": "created", "data": {"chat_log": {"content": []}}},
                        {"at": "y", "type": "log_observed", "data": {"big": "x" * 5000}}]
    return record


def test_packing_is_lossless_and_much_smaller():
    record = with_home(make_recording())
    packed = retention.pack(record)
    assert packed["packed"] and all("source" not in t for t in packed["turns"][1:])
    assert len(json.dumps(packed)) < len(json.dumps(record)) / 2
    restored = retention.unpack(deepcopy(packed))
    expected = deepcopy(record)
    expected["events"][1] = {"at": "y", "type": "log_observed"}  # A chat-log copy nothing reads.
    assert restored == expected
    assert retention.pack({**record, "status": "active"})["status"] == "active"  # Still growing: kept as is.


def test_a_compacted_recording_is_still_read_but_never_replayed():
    record = with_home(make_recording())
    compacted = retention.unpack(retention.compact(record))
    # It keeps the conversation and the devices it involved; the rest of the home goes.
    assert set(compacted["turns"][0]["source"]["states"]) == {TARGET}
    named = retention.unpack(retention.compact(record, also={"light.other_3"}))  # A device its analysis named.
    assert set(named["turns"][0]["source"]["states"]) == {TARGET, "light.other_3"}
    assert len(json.dumps(retention.compact(record))) < len(json.dumps(record)) / 2
    with pytest.raises(CaseError, match="compacted"):
        recording_turns(compacted)
    view = SessionView(compacted)  # Every role still reads it: the conversation, the turns, the device.
    assert view.conversation()[0]["user_says"] == "Toggle the fixture relay."
    assert view.read_turn(1)["state_changes_during_turn"] and triage.payload(view)["turns"]


def test_turns_hand_out_the_recordings_own_snapshots():
    """Reading a recording's turns doesn't copy each turn's home snapshot (megabytes, on the event loop): readers only
    read it."""
    record = make_recording()
    assert all(t["source"] is s["source"] for t, s in zip(recording_turns(record), record["turns"], strict=True))


def on_the_event_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.asyncio
async def test_the_daily_tidy_keeps_what_is_needed_and_compacts_the_rest(monkeypatch):
    """It packs and compacts off the event loop: each copies the whole recording."""
    on_loop = []
    for name in ("pack", "compact"):
        def watched(*args, real=getattr(retention, name)):
            on_loop.append(on_the_event_loop())
            return real(*args)
        monkeypatch.setattr(retention, name, watched)
    now = parse_datetime("2026-06-01T00:00:00+00:00")
    old, recent = (now - timedelta(days=60)).isoformat(), (now - timedelta(days=2)).isoformat()
    base = with_home(make_recording())
    stores = {}

    class FakeStore:
        def __init__(self, key):
            self.key = key

        async def async_load(self):
            return deepcopy(stores.get(self.key))

        async def async_save(self, data):
            stores[self.key] = deepcopy(data)

    index = {}
    for key, started, turns in (("empty", old, 0), ("old-free", old, 4), ("old-used", old, 4), ("new", recent, 4)):
        index[key] = {"status": "session_expired", "started": started, "user_turns": turns}
        stores[key] = {**deepcopy(base), "id": key, "turns": base["turns"] if turns else []}
    deleted = []

    async def delete(key):
        deleted.append(key); index.pop(key); stores.pop(key)

    async def save_index():
        pass
    hass = SimpleNamespace(async_add_executor_job=lambda job, *args: asyncio.to_thread(job, *args))
    recorder = SimpleNamespace(hass=hass, index=index, store=FakeStore, delete=delete, save_index=save_index,
                               lock=asyncio.Lock(), sessions={})
    retention.KEEP_NEWEST, keep = 1, retention.KEEP_NEWEST
    try:
        done = await retention.prune(recorder, referenced={"old-used"}, now=now)
    finally:
        retention.KEEP_NEWEST = keep
    assert done == {"deleted": 1, "packed": 2, "compacted": 1} and deleted == ["empty"]
    assert stores["old-free"]["compact"] and not stores["old-used"].get("compact")  # An improvement uses it.
    assert stores["new"]["packed"] and index["old-free"]["stored"] == "compact"
    assert on_loop and not any(on_loop)
    again = await retention.prune(recorder, referenced={"old-used"}, now=now)
    assert again == {"deleted": 0, "packed": 0, "compacted": 0}  # The index says how each is stored.


@pytest.mark.asyncio
async def test_conversations_deleted_during_the_tidy_up_leave_no_file(monkeypatch):
    """The tidy-up holds the recorder's lock from reading a recording to writing it back: conversations deleted while
    one is compacted (forget takes the lock too) go for good, with no file left, and the tidy-up goes on."""
    import threading
    now = parse_datetime("2026-06-01T00:00:00+00:00")
    base, stores, index = with_home(make_recording()), {}, {}
    for key, days in (("old-a", 60), ("old-b", 90)):
        index[key] = {"status": "session_expired", "started": (now - timedelta(days=days)).isoformat(), "user_turns": 4}
        stores[key] = {**deepcopy(base), "id": key}

    class FakeStore:
        def __init__(self, key):
            self.key = key

        async def async_load(self):
            return deepcopy(stores.get(self.key))

        async def async_save(self, data):
            stores[self.key] = deepcopy(data)

    async def save_index():
        pass
    started, release, real = threading.Event(), threading.Event(), retention.compact

    def slow(*args):
        started.set()
        release.wait(5)
        return real(*args)
    monkeypatch.setattr(retention, "compact", slow)
    monkeypatch.setattr(retention, "KEEP_NEWEST", 0)
    hass = SimpleNamespace(async_add_executor_job=lambda job, *args: asyncio.to_thread(job, *args))
    recorder = SimpleNamespace(hass=hass, index=index, store=FakeStore, save_index=save_index, lock=asyncio.Lock(),
                               sessions={})

    async def forget(*keys):  # As Recorder.forget does it.
        async with recorder.lock:
            for key in keys:
                stores.pop(key, None)
                index.pop(key, None)
    tidying = asyncio.create_task(retention.prune(recorder, referenced=set(), now=now))
    await asyncio.to_thread(started.wait, 5)  # Compacting old-a.
    forgetting = asyncio.create_task(forget("old-a", "old-b"))
    await asyncio.sleep(0)
    release.set()
    assert await tidying == {"deleted": 0, "packed": 0, "compacted": 1}
    await forgetting
    assert stores == {} and index == {}


def run_log(old, recent):
    """A run log as months of use leave it: what the pipeline reads, mixed with diagnostics."""
    return [
        {"id": "a1", "kind": "journey_analyzer", "recording_id": "r-old", "status": "completed", "started": old,
         "finished": old, "analysis": {"goals": [1]}, "output": {"big": "x" * 3000}, "usage": {"events": [1, 2]}},
        {"id": "h0", "kind": "journey_analyzer", "recording_id": "r-old", "status": "held", "started": old,
         "digest_sha256": "d", "triage": {"status": "completed", "output": {"smooth": False}}},  # Analysed after it.
        {"id": "a2", "kind": "journey_analyzer", "recording_id": "r-old", "status": "completed", "finished": old,
         "analysis": {"goals": [1, 2]}},
        {"id": "e1", "kind": "journey_analyzer", "recording_id": "r-flaky", "status": "failed", "finished": old,
         "answer_status": "error", "error": {"message": "boom"}},
        {"id": "unreadable-r-bad", "kind": "journey_analyzer", "recording_id": "r-bad", "status": "failed",
         "answer_status": "unreadable", "error": {"message": "malformed"}},  # From before records had a time.
        {"id": "h1", "kind": "journey_analyzer", "recording_id": "r-held", "status": "held", "started": old,
         "digest_sha256": "d", "triage": {"status": "completed", "output": {"smooth": False}}},
        {"id": "g0", "kind": "hypothesis_generator", "status": "completed", "started": old,
         "output": {"hypotheses": []}, "friction": ["r-old:1"], "sessions": ["r-old"]},
        {"id": "rev", "role": "hypothesis_reviewer", "kind": "hypothesis_reviewer", "status": "completed",
         "started": old, "output": {"verdict": "x" * 2000}},
        {"id": "p-old", "kind": "pipeline", "status": "done", "started": old, "progress": {"done": 1}},
        {"id": "h-gone", "kind": "journey_analyzer", "recording_id": "r-deleted", "status": "held", "started": old,
         "triage": {"status": "completed", "output": {"smooth": False}}},
        {"id": "t-new", "kind": "quick_check", "status": "completed", "started": recent, "output": {}},
        {"id": "a3", "kind": "journey_analyzer", "recording_id": "r-new", "status": "running", "started": recent},
    ]


def test_the_run_log_keeps_what_the_pipeline_reads_and_lets_diagnostics_age_out():
    """The run log keeps what the pipeline reads from it: a record still running, and a quick-check verdict waiting
    for its analysis, which the analysis reuses (analysis.kept_verdict). What the stages found about a conversation is
    in its flow record, so the rest of the log (analyses, failed attempts, unreadable markers, proposals) is
    diagnostics: gone after 30 days. Runs stay a year."""
    from custom_components.assist_improver.analysis import kept_verdict
    now = parse_datetime("2026-06-01T00:00:00+00:00")
    old, recent = (now - timedelta(days=60)).isoformat(), (now - timedelta(days=2)).isoformat()
    runs = run_log(old, recent)
    recordings = {"r-old", "r-flaky", "r-bad", "r-held", "r-new"}
    kept, done = retention.prune_runs(deepcopy(runs), recordings, now=now)
    assert [r["id"] for r in kept] == ["h1", "p-old", "t-new", "a3"]  # In order.
    assert kept[0] == runs[5]  # The verdict stays whole: the analysis reuses it.
    assert kept_verdict(SimpleNamespace(role_runs=SimpleNamespace(runs=kept)), "r-held", "d") == runs[5]["triage"]
    assert done == {"deleted": 8}
    year = (now + timedelta(days=330)).isoformat()
    assert "p-old" not in [r["id"] for r in retention.prune_runs(kept, recordings, parse_datetime(year))[0]]
    assert retention.prune_runs(kept, recordings, now=now)[1] == {"deleted": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage, held, outcome, kept", [
    ("Your Assist", None, None, True), ("Your Assist", "cut off", None, True), ("Triage", "failures", None, True),
    ("Analyze", "blocked", None, True), ("Propose", "scope", None, True), ("Write up", "cut off", None, True),
    ("Your household", None, "no trouble found", False)],
    ids=["waiting", "held-at-your-assist", "held-at-triage", "held-at-analyze", "held-at-propose", "held-at-write-up",
         "ended"])
async def test_a_conversation_waiting_for_a_run_or_held_in_its_round_is_never_compacted_however_old(
        improver, stage, held, outcome, kept):
    """One waiting for a run to take it in, or held where its round still has work to do (it still needs its full
    recording for its replays), stays in full, as its flow record says; one whose round ended is condensed like any
    other once it's old."""
    from custom_components.assist_improver import tidy
    from kit import seat, variant
    old = "2026-01-01T00:00:00+00:00"  # Months before the fixture's run: past the 30 days.
    for n in range(retention.KEEP_NEWEST + 2):  # Enough newer ones that age alone would compact it.
        await improver.arrives({**variant(make_recording(), f"newer{n:04d}", f"Relay off {n}."), "started": old})
    await improver.arrives(variant(make_recording(), "waiting01", "Relay off, please."))
    for rid, summary in improver.recorder.index.items():  # It's the oldest: not among the newest 20.
        summary["started"] = old if rid == "waiting01" else "2026-01-02T00:00:00+00:00"
    for rid in improver.recorder.index:
        if rid != "waiting01":  # The newer ones have ended their rounds: nothing holds them in full.
            seat(improver, rid, "Your household", outcome="no trouble found")
    seat(improver, "waiting01", stage, held=held, outcome=outcome)
    await tidy(improver.hass, improver.runtime)
    assert (improver.recorder.index["waiting01"].get("stored") != "compact") is kept
