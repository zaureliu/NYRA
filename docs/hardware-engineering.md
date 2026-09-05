# Hardware Engineering V1

## Ownership and boundaries

`hardware_engine` composes the existing native USB monitor, SystemShell,
DesktopController, Task Engine V2, Memory V2, Open Loops, World State and Proactive
Presence. It creates no second hardware watcher, microphone pipeline or notifier.
USB refresh is mandatory before physical operations; descriptors and COM ports
come from SetupAPI. A user's statement remains `user_claim`, never presence.
Recent-device context uses observed connection timestamps, not user assertions.
Chip identification is distinct from a retail-board identity. Friendly aliases
and generic serial bridges do not prove which board or GPIO is present.

FULL is an explicit operator-only local configuration, off on fresh installs.
The Hardware card/API enables it. It authorizes only deterministic local recipes
for the selected observed device and generated workspace. Each shell recipe uses
the existing risk classifier, one-use command/cwd/timeout/run-bound approval,
redaction, history and cancellation. The approval source records the operator's
FULL policy. Neither the LLM nor a web document can enable FULL or supply a shell
recipe. SSH/UAC/security policies are unchanged. Revocation disables new recipes.

## Projects and toolchains

Projects default to `<USER_HOME>/KAZUMI-Projects`, outside the repository, or the
operator-selected `KAZUMI_PROJECTS_ROOT` environment override.
`.kazumi-project.json` records target, evidence, sources, source hashes, build/flash
state and history; it contains no credentials. A local index resumes the same
workspace across restart. Source, config and README are filesystem writes.
The existing Desktop Operator opens a generated VS Code workspace and verifies
the visible window. VS Code is operator-owned and not killed at KAZUMI shutdown.

PlatformIO Core 6.1.18 and esptool 5.0.2 install in one managed Python venv using
PyPI, with a post-install version probe. Board definitions are checked against
the expected MCU/framework from the official PlatformIO repository. External
build hooks, local library scripts and changed/unreviewed source are rejected.
Other installed toolchains are detected but do not have autonomous build drivers
in V1. Installation still respects OS permissions and requires a host Python.

The compiler loop records diagnostics and hashes actual firmware artifacts.
At most five repair cycles are possible, and repeated diagnostics stop earlier.
The original two deterministic repairs remain fast paths. The general engineering
layer can research compiler diagnostics, revise incompatible API/library choices,
propose a typed patch and rebuild. Free model text never becomes shell.

`CodeEngineering` inspects the existing indexed workspace, asks the configured
local model for a bounded plan, researches official APIs, validates exact edits,
reviews feature preservation and compiles. New C/C++ modules are allowed under
src/include; build configuration and host scripts are not model-editable. Source
hash preconditions reject concurrent changes. Small official libraries can be
imported as reviewed static sources with license/compatibility evidence and hashes;
downloaded installers, binary blobs and build hooks are not executed. Unsupported
licenses or dependencies require review instead of being silently installed.

The local proposal transport validates Pydantic schemas, with one schema correction
and one code-review correction at most. The observed Ollama 0.33.3 runner disconnected
with grammar-format requests; this path uses normal completions with deterministic
validation instead. Normal chat/model configuration is unchanged. Compiler/static
checks and model code review are explicitly not physical effect verification.

## Serial, flash and verification

Serial uses pySerial with bounded line/byte capture, timeouts, per-device locks,
fresh enumeration, handle cleanup and a nonce-bound `kazumi/1` protocol. Compatible
firmware takes the direct LED control path without rebuilding. State readback is
electrical/serial evidence, not an optical observation. A stale line, open port,
generic success, build or upload does not prove an LED's state. Test transports
are marked simulated and cannot produce physical verification.

The initial flash adapter uses PlatformIO for Espressif only after source/hash
association, chip probe, complete flash backup and a fresh target check. It then
rediscovers COM by stable identity and requires firmware verification. Failed
backup or incompatible chip blocks writing. Automatic erase, forced bootloader
recovery, secure-boot/encrypted flash migration and recovery for other families
are not implemented: capabilities explicitly report a missing recovery adapter.
No arbitrary flash/erase argument surface is exposed to the model.

## Actual support and limits

Family recognition includes ESP32/S2/S3/C3, ESP8266, Arduino AVR, RP2040, STM32,
nRF52, M5Stack/Cardputer, LILYGO and generic serial. Recognition is not a claim
of full build/flash support for every board. Exact initial board recipes are
Arduino Uno, Raspberry Pi Pico and ESP32-S3-DevKitC-1 descriptors. GPIO LED
generation uses the existing official variant fast path. General project changes
are not limited to LED templates: buttons, displays, sensors and Web-server code
can use the same research/edit/build interface when their component, API and target
requirements are evidenced. Unknown physical components/pins remain factual
blockers. Recognition alone still does not supply a retail-board adapter or a
verified electrical specification. No generic ESP32 GPIO is substituted.

Project-only requests reuse the indexed project without requiring physical presence.
This never grants flash/serial authority. Reference projects are marked REFERENCE
and explicitly rejected by FlashEngine. Operator-provided wiring is a design
specification, not an observed connection. FULL remains the operator's authority
for deterministic tools; project source is never sent to a remote model.

Real validation: six USB devices, zero COM ports and zero identified ESP32.
A controlled Uno project was created without claiming an attached Uno. Its
official board definition and LED pin were fetched, an intentional compiler
error was repaired, and the second build produced a hashed firmware.hex.
Physical flash, serial and LED behavior could not be validated without a device.

### Targeted continuation validation (2026-09-05)

The same REFERENCE Uno workspace was evolved using the real local model, public
documentation and the managed PlatformIO compiler: nonblocking blink, 500 ms
interval, button pause/resume and an additional UART `UPTIME` feature with no
dedicated recipe. Follow-up diagnostic requests corrected generated debounce and
fragmented-input bugs. Model review/compiler success alone had missed those bugs;
they must not be described as behavioral or physical verification.

The resulting actual firmware.hex was run in isolated AVR8js 0.21.1 simulation.
500 ms GPIO transitions, one debounced action per press, resume and fragmented
`UPTIME\n` input passed. This is **SIMULATED**, with physical verification false.
Firmware SHA-256: `5609697723b29649c19c27e70fe582b2613c34b8166535fb2a8764ce8320307e`.
The project identity/workspace also survived reopening the project store.
The targeted backend suite passed 147 tests (one existing framework deprecation).

The final PyInstaller/Tauri release was opened through the Desktop `KAZUMI.lnk`.
Fresh native discovery returned five USB devices, zero COM ports and no ESP32.
The canonical chat rejected the ESP32/LED user claim and device-info request with
the factual no-device response. Natural `pio run` research answered from
`https://docs.platformio.org/en/latest/core/userguide/cmd_run.html`; the Uno profile
endpoint explicitly returned REFERENCE/connected=false and retained unknown fields.
An initial research request was interrupted by concurrent voice input (HTTP 409);
the subsequent uninterrupted request passed without modifying voice settings.
The official `quit_kazumi` handler returned successfully; readback confirmed backend=0,
desktop=0 and port 8000 FREE. No visual tray automation was used.
