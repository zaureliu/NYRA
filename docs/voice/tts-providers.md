# Universal TTS Provider Layer V2

Implementation baseline: `36f4e107c6956071d90453c7ae372beb3f740df4`.
Native Gradium and declarative Custom extend the existing logical registry:
`local`, `openai`, `elevenlabs`, `gradium`, `custom`. No replacement conversation,
emotion, STT, speech queue, avatar or hardware engine.

## Canonical path

Natural Conversation Runtime → existing Speech Planner → TtsProviderRegistry →
SpeechQueue → provider AudioPacket → existing TTS_PCM_CHUNK transport → desktop
player/analyser → output echo reference + VTS lip sync. Sentence chunks start
before the full LLM response completes. Metadata-only alignment packets never
become empty playback packets. No microphone capture or STT settings change.

SpeechQueue remains bounded (48 items). Its PCM publisher applies backpressure
at one second of audio ahead of wall time; WS receive buffers are limited to
eight frames, each at most 1 MiB. Audio is bounded to 180 seconds / 17.28 MB per
request, with 12,000-character text and bounded JSON templates. Custom REST JSON
base64 has a separate bounded encoded-response limit. Barge-in cancellation is
not swallowed when ready/audio and cancellation arrive simultaneously.

Provider/settings changes do not cancel current playback. A request snapshots
its settings; the next speech item uses the new selection. Local remains the
default and default fallback. Custom can explicitly disable fallback. A remote
failure before any audio permits local fallback; a partially heard sentence is
never replayed wholesale. Retry delays grow to a maximum of 30 seconds. There
is no background retry storm, network access at metadata lookup, or cloud key
requirement at startup. Remote connection errors become known on attempted use
or explicit connection test; configuration readiness is not proof of Internet
connectivity or successful synthesis.

## Native Gradium

Official protocol checked 2026-09-05:
[TTS WebSocket](https://docs.gradium.ai/api-reference/endpoint/tts-websocket),
[lifecycle](https://docs.gradium.ai/guides/websocket-lifecycle),
[multiplexing](https://docs.gradium.ai/guides/multiplexing).

The backend authenticates with the `x-api-key` header. It sends setup first,
validates ready and negotiated rate, streams text/audio, and waits for the
matching end event. Every request has its own `client_req_id`.
`close_ws_on_eos=false` permits socket reuse across sentence requests. Successful
requests reuse the connection; cancellation closes it because the documented
protocol has no immediate request-abort command. The next request reconnects;
old audio cannot leak into a new response. Shutdown closes idle sockets too.

Defaults: official global WSS endpoint, model `default`, PCM S16LE mono 48 kHz.
16/24 kHz are supported explicitly, without resampling PCM. Global/EU/US
official endpoints are selectable. Voice ID is required; a friendly name is
never used as identity. The explicit catalog action queries
[Get Voices](https://docs.gradium.ai/api-reference/endpoint/get-voices) including
catalog voices; manual IDs remain usable without the catalog.

Advanced controls are the documented `temp`, `cfg_coef`, `padding_bonus`,
language rewrite alias and pronunciation dictionary ID. See
[Voice Settings](https://docs.gradium.ai/guides/voice-settings). Padding bonus
is not a percentage rate: negative is faster, positive slower. No invented
emotion/nonverbal tags or acoustic emotion guarantees. Word/segment timestamps
are canonical SpeechTimestamp values and a bounded diagnostics alignment list,
not a playback prerequisite.

## Custom / Other

Custom supports these declarative contracts, **not every proprietary TTS API**:

| Transport | Response |
| --- | --- |
| REST POST | RAW_AUDIO_BYTES or JSON_BASE64_AUDIO |
| WebSocket | WEBSOCKET_BINARY_FRAMES or WEBSOCKET_JSON_BASE64 |

The operator configures endpoint, voice, model, language, sample rate, auth
type, response mapping, and JSON request/setup/text/end/cancel templates.
JSON fields may use dotted paths. No eval, executable expressions, scripts,
filesystem lookup, arbitrary headers or shell interpolation. Known placeholders
are `text`, `voice_id`, `model`, `language`, `sample_rate`, `output_format`,
`emotion`, `style`, `speed`. Whole-value placeholders preserve JSON types;
substitution into parsed values cannot break JSON quoting. Custom metadata
fields transmit only if explicitly included in the operator's template; that
does not assert acoustic support by the remote service.

Real incremental playback requires PCM S16LE and raw REST or WS audio frames.
WAV, MP3, OGG and single REST JSON base64 responses use bounded buffering and
the already-installed PyAV decoder, then the canonical player. They do not
claim incremental compressed-audio decoding. WS profiles should identify an
explicit end event; protocol-specific session multiplexing requires a native
adapter. The Gradium native adapter is preferred over a Custom replica.

Multiple profiles persist with unique IDs. Import uses a new ID, so an imported
file cannot bind itself to a pre-existing secret. Export uses the backend's
strict secret-free profile schema. The Advanced JSON editor is configuration,
not code generation. Static secret-shaped fields and known Broker secret values
are rejected; never paste credentials into innocuously named template fields.

## Credentials and endpoints

Only the existing Credential Broker stores keys: Gradium `gradium_api_key`;
OpenAI/ElevenLabs keep their existing IDs. Custom credentials use a stable
`custom_tts_` + SHA-256-derived per-profile identifier, valid for the Broker and
collision-separated even for hyphen/underscore profile names. The UI only
temporarily holds a password during explicit save; it clears it on completion,
does not read it back, and does not use localStorage. The Broker retains its
Windows Credential Manager / DPAPI storage ownership.

Only HTTPS/WSS endpoints by default. Explicit local profiles allow HTTP/WS
only for localhost/127.0.0.1/::1. Queries, fragments and URL userinfo are rejected
to keep secrets out of URLs. Non-public resolved addresses are blocked unless
they are the explicitly authorized loopback target. Connections pin the checked
IP; TLS retains original-host certificate verification. Redirects are disabled
on both transports and environment proxies are not implicitly trusted. Custom
configuration is a local operator settings surface, not an LLM fetch tool.
Remote bodies, auth headers and underlying wire exceptions are not logged.

## Voice UI / diagnostics

Speech Synthesis lives in the existing Voice page, with local 14 px labels,
15 px controls, 14 px buttons/status, 20 px heading and 13+ px explanations.
Provider selection, credential actions, catalog, connection test and voice test
are explicit. Voice tests use the real SpeechQueue/player, not a chat turn.
The test phrase is “Oi, eu sou a Kazumi. Como foi seu dia?”; this is not a rebrand.

Gradium connection test validates setup without playback. Custom has no generic
health URL: its test synthesizes a minimal phrase, validates audio and discards
it without playback; this may consume credits. Voice test success requires a
player acknowledgement, not merely a returned audio file.

Diagnostics retain request→ready, text→first audio, request→first audio, observed
completion and actual player buffer delay. First-audio samples are bounded to
100; average is available after one, p50 after five and p95 after twenty. No
cloud latency numbers are populated by local fixtures. Timing of stream
completion includes applied consumer backpressure, not just provider compute.

## Verification and limitations

Targeted tests exercise registry, missing credentials/offline startup, native
setup/text/PCM/alignment/reuse/cancellation, REST and actual loopback WS
contracts, four auth types, safe substitution, URL/DNS/redirect restrictions,
fallback, runtime switching, schema persistence and export secret rejection.
Local fixtures are explicitly test data, never real Gradium cloud evidence.
Cloud authentication, Portuguese pronunciation/naturalness and Gradium latency
remain untested until an operator configures a valid Broker key and voice ID.

Shutdown uses the existing coordinator: SpeechQueue stops, provider close hooks
release sockets, receiver/sender work is cancelled, and external VTube Studio
is not terminated. Restart loads settings/Broker references, not audio buffers
or sockets. X remains hide-to-tray; `quit_kazumi` is the explicit full-exit path.

### Local validation run, 2026-09-05

100 relevant backend tests and 12 frontend tests passed. PyInstaller and Tauri
release builds were generated in the canonical repository and opened with the
official desktop shortcut. Custom REST and WS played the same locally generated
Kokoro test phrase (227,328 PCM bytes, 48 kHz) through the existing player, without
a WAV upload to any remote provider. The controlled WS run confirmed first
playback at 63.5 ms while the fixture continued sending for roughly two seconds.
These figures are **not Gradium latency**. A real VTS observation collected 60
samples: mouth-open range 1.5634, eye-X range 0.49, head-X range 10.6808.

An intentional HTTP 401 from the loopback fixture used the configured Kokoro
fallback. The official speech-only interruption cancelled the active WS test
without cancelling a task. Follow-up regressions cover resetting streaming
health on cancel and preserving the first playback acknowledgement instead of
overwriting it with later PCM packets. Voice settings expose an explicit retry
when the initial query races a cold backend startup. Gradium remains
NOT_CONFIGURED; no claim of cloud audio, authentication or PT-BR voice quality
is made. Test profiles are removed and original operator preferences restored.
