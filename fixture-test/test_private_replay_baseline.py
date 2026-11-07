"""The household's stored conversations, replayed as they went (Simulate's replay without a change, its assistant a
scripted stand-in repeating the recorded calls: no model is called), against a baseline kept with them (on demand).

What each replay does (its turns' tool results, state changes, matches against the recording, status) stays the same
from commit to commit, or the difference is a fix the commit explains; and the replays together don't get slower.
Household data stays out of the checkout: the conversations are tools/preflight.py's copy in the private folder
(tools/private.py), and the baseline sits beside it.

  .venv/bin/python -m pytest fixture-test/test_private_replay_baseline.py --private -q -p no:cacheprovider
                                                                               compare with the baseline
  IMPROVER_REPLAY_BASELINE=write ...                                           (re)write it, on main
"""

import json
import os
import time

import pytest

from conftest import private, private_folder

STORES = ("preflight", "storage")  # In the private folder.
WRITE = os.environ.get("IMPROVER_REPLAY_BASELINE") == "write"
# When it ran (to the microsecond: Home Assistant's own jitter, and the real speed inside a turn), what the stand-in
# counted, and a camera's picture link (its token, renewed at random every 5 minutes), not what the replay did.
DROPPED = {"at", "created", "usage", "last_triggered", "entity_picture"}
TOOK = {}  # {conversation: seconds its replay took}, this run.


def conversations(config):
    """The stored conversations, once --private asks for them."""
    stores = private_folder().joinpath(*STORES)
    return sorted(p.name.removeprefix("assist_improver.session_") for p in stores.glob("assist_improver.session_*")
                  ) if config.getoption("--private") else []


def pytest_generate_tests(metafunc):
    if "conversation" in metafunc.fixturenames:
        metafunc.parametrize("conversation", conversations(metafunc.config) or ["none"])


def _kept(value):
    if isinstance(value, dict):
        return {k: sorted(_kept(v), key=lambda t: t["entity_id"]) if k.endswith(("transitions", "commands"))
                else _kept(v) for k, v in value.items() if k not in DROPPED}  # Devices acting at once: either order.
    if isinstance(value, list):
        return [_kept(v) for v in value]
    return value


@pytest.mark.asyncio
async def test_a_stored_conversation_replays_as_its_baseline_says(hass, monkeypatch, request, conversation):
    stores = private(*STORES, on_demand=request.config)
    from custom_components.assist_improver.cases import CaseError
    from custom_components.assist_improver.retention import unpack
    from custom_components.assist_improver.simulate import Untestable, agent_of
    from test_simulate import replayed_as_it_went
    record = unpack(json.loads((stores / f"assist_improver.session_{conversation}").read_text())["data"])
    started = time.monotonic()
    try:
        agent_of(record)  # What Simulate itself says can't replay, before the stand-in is set up.
        for turn in record["turns"]:  # The stand-in reads the model as Anthropic's options name it.
            options = turn["prepared"]["provider_config"][0]["options"]
            options.setdefault("chat_model", options.get("model"))
        found = _kept(await replayed_as_it_went(hass, monkeypatch, record))
    except (Untestable, CaseError) as err:
        found = {"status": "can't replay", "why": str(err)}
    TOOK[conversation] = seconds = round(time.monotonic() - started, 2)
    baseline = stores.parent / "replay-baseline"
    kept = baseline / f"{conversation}.json"
    if WRITE:
        baseline.mkdir(exist_ok=True)
        kept.write_text(json.dumps({"replay": found, "seconds": seconds}, indent=1, sort_keys=True, default=str))
        return
    if not kept.exists():
        pytest.skip(f"{conversation} has no baseline yet")
    assert json.loads(json.dumps(found, sort_keys=True, default=str)) == _kept(json.loads(kept.read_text())["replay"])


def test_the_stored_conversations_replay_no_slower_than_their_baseline_together(request):
    """After the replays, when all ran: their time together, the first's aside (it starts the replay worker)."""
    baseline = private(*STORES, on_demand=request.config).parent / "replay-baseline"
    every = conversations(request.config)
    later = [c for c in every[1:] if (baseline / f"{c}.json").exists()]
    if WRITE or not later or set(TOOK) != set(every):
        pytest.skip("Only after replaying every stored conversation against the baseline")
    took = sum(TOOK[c] for c in later)
    seconds = sum(json.loads((baseline / f"{c}.json").read_text())["seconds"] for c in later)
    assert took <= seconds * 1.5, (took, seconds)  # Not slower, load aside.
