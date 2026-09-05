# Natural hardware goals V1

The canonical realtime entrypoint and direct Agent Loop call the same hardware
service before free-form generation. Typed intent is converted into a capability
plan; no response text becomes shell. Hardware information, documentation,
project creation/build, LED on/off/blink and project resume are recognized.
Display, button, sensor and Web-server goals use general engineering with explicit
component/API evidence; absent physical wiring or unsupported targets still block.

Goals record target, project, plan, executed steps, state, evidence, source URLs,
artifacts, errors and timestamps. At most 30 recent goals and 40 steps each are
kept. Information and no-device responses are synchronous; long work becomes a
Task Engine V2 task with zero automatic task retries to avoid replaying flash.
The build engine has its own smaller diagnostic repair loop. Device changes
invalidate the old target, and compatible firmware replaces build/flash with
the direct runtime-control plan. Ambiguous physical targets are not flashed.

Blocked goals create deduplicated Open Loops. Tasks already supply relevant
events to the existing Proactive Presence. Native USB disconnects close owned
serial handles. World State adds short-lived hardware activity/recent-removal
slots alongside the existing connected_usb snapshot, not an infinite log.
Project summaries use Memory V2, and generated files register in Artifact
Context. Hardware task details and research sources appear within the existing
USB/Hardware page; no parallel project or notification UI is created.

Project identity/build state survives restart; serial handles and observed
device presence do not. Interrupted goals require revalidation, not automatic
flash replay. Shutdown cancels owned tasks, closes serial and research sockets,
and delegates process-tree cleanup to SystemShell. The existing Tauri
`quit_kazumi`/Tray Exit coordinator and X-to-hide behavior remain unchanged.

## Continuation and revisions

`ProjectContext` resolves indexed projects using explicit references, the visible
VS Code workspace, recent artifacts, project memory, Open Loops and the persisted
active project. Ambiguity is not resolved by writing to multiple workspaces.
Requests such as "agora adiciona", "muda o delay" and "continua aquele projeto"
use the software-only continuation path. A fresh "conectei um ESP32" claim still
requires real discovery and cannot borrow a reference project's identity.

`PlanRevision` separates assumptions, observed evidence, completed/current/pending
steps, invalidated steps, revision history and blockers. Execution uses revision
tickets; stale tickets are rejected. New target evidence invalidates incompatible
pinout, environment, libraries, build and upload state. The same workspace is
retargeted only to a trusted compatible board definition; chip-only evidence does
not identify a board. Compiler evidence can rebuild the remaining repair plan.

Completed features, pending goals, source hashes, dependencies and last-known-good
build are stored with the project. Modified code registers as a verified artifact
and resolves "abre o código que você mexeu" through the existing Artifact Context.
Blocked goals retain Open Loops. An observed connect event triggers at most one
fresh revalidation/resume attempt under FULL, not unconditional firmware replay.

Technical profiles separate OBSERVED from REFERENCE and keep unknown values null.
Official definition limits are labeled as build limits, not measured RAM/flash.
Electrical fields require matching document evidence. No technical profile proves
the LED, sensor, serial protocol or firmware is functioning.
