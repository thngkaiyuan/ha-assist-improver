# Install and manage Assist Improver

Assist Improver works with Home Assistant Core 2026.11 or later (it is tested on 2026.11.0). It records conversations and replays them in a sandboxed Home Assistant instance, so device actions in replays are always simulated. Installation and recording make no model requests.

## Install

1. Extract `dist/assist-improver-<version>.zip` (`python build_package.py`) into HA's configuration directory, preserving `custom_components/assist_improver/` and the files' modification times. When upgrading, remove only the component's generated `__pycache__` directories to prevent stale bytecode; keep its `.storage` records.
2. Restart Home Assistant Core. Existing automations are briefly unavailable during that restart.
3. For a first installation, open **Settings → Devices & services → Add integration → Assist Improver** and submit the setup form. An existing integration entry needs no replacement.

The package has no additional Python dependencies. Do not replace your AI provider's integration or change the selected voice assistant. The recorder listens alongside it.

## Inspect recordings

In **Developer tools → Actions**, administrators can use:

- **Assist Improver: Recording status** to check recording and pending writes/errors.
- **Assist Improver: List recordings** (developer mode) to obtain recording IDs, time ranges and user-turn counts.
- **Assist Improver: Export recording** with one recording ID to receive its JSON response.

The integration's diagnostic download contains a status summary, not raw conversations. Exported recordings contain household conversations and the home as recorded, with passwords, API keys, tokens and locations redacted. They're matched by key name, so one kept under another name (an address held as a state) still goes out. The stored recording stays whole. Share exports deliberately. No public repository or external telemetry receives them.

## Replays

The ordinary assistant's settings are not changed. Hosted web/code/search settings remain as the assistant has them. Provider-hosted tools run at the provider in replays too; their results are labelled outside receipts, not a claim that the remote call was prevented. What a replay doesn't simulate is reported as not simulated and never falls back to a real device or service.

Paid calls go through the household's own HA provider integration at its normal charges; this component has no client or credential store of its own. HA's ordinary ten-round limit per user turn and its SDK's two qualifying transport/status retries remain in effect; no strict monetary cap is added. A baseline gets up to three tries, each retained; nothing else is resampled.

Replays and results persist privately. Active provider-native history is kept in memory only. Restarting/reloading interrupts an unfinished replay, which runs again later.

## What is recorded

The common HA ChatLog supplies user/assistant text, rendered system context and HA tool calls/results. When its selected LLM API provides them, the recorder also retains user/context IDs, requesting device ID, tool schemas, the answering assistant's settings (its conversation subentry's data, whole), its voice assistant, and every action call in the request's context chain. The requesting device's readable name is in the registry snapshot when available. The first user turn keeps the moment whole: every state, the entity, device, area, floor and label registries, HA's configuration, exposure, script, automation and scene definitions, to-do items and the household knowledge; later turns keep the states. Every state change during the session is a timestamped observation, not an assumed action effect; readings (sensor values and the like) are labelled and listed last.

Snapshots distinguish disabled entities without live state. No config entry's data or options are read except the answering provider's, handed unread to a replay; what HA gives otherwise is kept as it is, with nothing redacted, in HA's own storage.

## Storage and lifecycle

Files live under HA's configuration directory:

- `.storage/assist_improver.index`
- `.storage/assist_improver.session_<opaque-id>`
- other `.storage/assist_improver.*` files

HA's versioned storage writes each file atomically with private file permissions. Writes are debounced by one second, or up to 15 for an active recording's changes between turns; abrupt power loss can lose the pending interval. Existing records survive restart. Open captures are marked interrupted, and new captures never overwrite older episodes of the same conversation.

Disable the integration to stop recording. Removing it deletes these files; older HA backups may still contain them. Removing the component folder after removing its config entry uninstalls the software.

Keep both the component folder and its `.storage` files in a full HA configuration backup. Offline tests restored those records into a fresh HA runtime.

## Current coverage

- Common ChatLog capture is tested with HA's actual Anthropic streaming adapter and scripted responses. No Anthropic account was contacted during validation.
- HA does not emit every final-response event. The recorder observes active logs once per second to capture missing tails; it does not replace listeners or execute tools. If cleanup is not observed after 15 minutes without conversation activity, the capture is marked timed out and retained.
- Interactions outside HA's common ChatLog are not covered. Missing identity/tool context and omitted provider-native history are explicit limitations. Arbitrary provider history, attachments and unsupported template/state fields are not yet replay-ready.
