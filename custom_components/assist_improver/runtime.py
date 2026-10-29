"""What a loaded Assist Improver holds: its config entry's runtime_data, the parts async_setup_entry builds, each in
its own field."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from .conversations import Transcripts
    from .improvements import Improvements
    from .pipeline import Pipeline
    from .recorder import Recorder
    from .reflective import RoleRuns
    from .simulate import Replays


@dataclass
class RuntimeData:
    recorder: Recorder  # The Assist conversations, recorded and kept.
    role_runs: RoleRuns  # The run log: each AI role call and each pipeline run.
    replays: Replays  # Each conversation's replays, without a change and with one, by their plans.
    improvements: Improvements  # What goes to the household for review, and what they decided.
    pipeline: Pipeline | None = None  # Set once the rest has started.
    flow: object | None = None  # The model's engine and records (flow.Flow), set with the pipeline.
    panel: bool = False  # Whether this entry registered the panel.
    retention: dict | None = None  # What the last tidy-up did.
    transcripts: Transcripts | None = None  # What each kept conversation said, for the conversations view.


def loaded(hass) -> RuntimeData | None:
    """The loaded Assist Improver's runtime data (there's one per Home Assistant), or None while it isn't loaded."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None
