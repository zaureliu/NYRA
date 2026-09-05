# Hardware grounding hotfix

## Cause observed in the canonical runtime

The ESP32/LED request was classified as conversation, not as an observation/action.
The USB chat matcher did not recognize ESP32 and the general routing vocabulary did
not recognize the request. The realtime conversational stream bypassed the Agent
Loop grounding ledger. Its generated device/network/LED/serial assertions had no
hardware evidence. No hardware Agent Run was recorded for that request.

The concurrent Remote Shell activity was background gateway health polling
(`ubus call system info`, read-only, exit 0, no turn ID), not ESP32 tools. The chat
activity collector previously accepted those events without turn correlation.
No simulated ESP32 output was found in this path. An existing generic mutation
verifier is also not sufficient proof of a physical effect.

## Targeted boundaries

- `usb.hardware.hardware_request` separates the user's target/action/claim from
  observations. Live board/LED/GPIO requests cannot take the free conversational
  stream. Educational explanations still can.
- `hardware_discover` reuses the existing SetupAPI USB/PnP enumeration, including
  COM-port metadata. No new watcher, serial connection, firmware writer, GPIO
  controller or network scan is introduced.
- A fresh successful enumeration is required per request. A failed/timed-out
  enumeration produces unknown, never absent or cached connected. Only raw
  hardware descriptors identify a board; operator-friendly names and generic
  USB-to-serial adapters do not identify an ESP32 by themselves.
- Recently referenced hardware IDs originate only from a matching observed
  device. A later physical request refreshes presence before using that ID.
- Results carry source, request source, time, turn ID, device ID, simulation
  marker and explicit effect verification. Unknown/test discovery providers and
  marked simulated rows produce a SIMULATED response, not physical success.
- USB World State expires (fresh 45 s, expired after 90 s). Only successful
  native reconciliation renews it; user claims cannot renew the snapshot.
- Hardware replies are presented deterministically before tokens reach chat or
  speech. No-device is blocked with a natural response. Direct Agent Loop use
  also performs mandatory discovery and records the blocking reason.
- The tool presenter rejects unsupported physical claims. Shell output, echoed
  text, build/upload success and generic `open`/`ready`/`effect_verified` do not
  establish LED, GPIO, heartbeat or serial effects. Reserved physical-evidence
  adapters require specific facts, real provenance, same-turn correlation and
  freshness; they are not fabricated adapters registered by this hotfix.
- Chat tool activities require the current turn ID. Background health polling
  remains in its existing diagnostics/history, not attributed to a user turn.
  Genuine approval requests remain visible.

## Limits and validation

This baseline has USB discovery but no implemented, verified ESP32 LED-control
protocol. Finding a board therefore does not produce an LED success claim. No
feature is removed and no Hardware Engine is rewritten. Network presence is not
inferred from USB. A generic serial bridge may require future explicit chip
identification; it is reported as unidentified, not guessed.

Targeted tests cover the exact no-device request, user claim routing, disconnect
and refresh, simulated data, failed enumeration, stale World State, fabricated
effects, presenter enforcement and background tool-event isolation. Controlled
fixtures prove those code paths only; they are not a physical-device test.
The official no-device scenario must additionally run against the release
launched by `KAZUMI.lnk`, using actual native enumeration. No private audio,
operator database or runtime logs belong in this repository.

## Validation performed

- Backend: 111 targeted tests passed (35 specific hardware regressions plus
  USB, grounding, Agent Loop, World State and realtime integration tests).
- Frontend: 15 targeted tests passed; frontend build, PyInstaller and Tauri
  release completed. Voice/STT implementation and settings were not changed.
- Official `KAZUMI.lnk` runtime: native enumeration found four USB devices,
  no COM interfaces and no identified ESP32. Both the original ESP32/LED request
  and the assertion "o ESP32 está conectado" triggered real `hardware_discover`
  executions. Both replies reported that no ESP32 was found on USB/serial,
  without LED/heartbeat/serial success claims. No background tool group was
  attributed to either chat turn.
- Disconnect/revalidation and fake effects were tested with controlled fixtures.
  A physical connect/disconnect or LED test was **not** performed: no compatible
  device was available. These are not counted as real-device passes.
