# Natural Conversation Runtime V1

Implemented on top of the current ConversationEngine, RealtimeOrchestrator and
SpeechQueue. There is no additional microphone, model, Persona, Emotion, Memory,
Hardware or tool executor. Default `natural_conversation_enabled=true`; existing
listening/PTT/wake-word, interruption and proactive preferences remain authoritative.

## Ownership and flow

Desktop Presence WebView owns getUserMedia, mono PCM16/48 kHz framing, browser
echo cancellation and the existing energy gate. Silero remains in local STT;
Deepgram canonical partials inform endpointing without entering memory. The
existing authenticated localhost/Tauri bridge and STT registry remain the sole
recognition path. A second utterance may wait at most two seconds for the same
provider worker; all transport/audio queues are bounded. Overload reports failure,
never a fabricated transcript.

Canonical final -> ConversationEngine -> one volatile conversation_id -> existing
RealtimeOrchestrator -> Persona/Relationship/Memory/World State/Open Loops/Artifacts
-> existing LLM/tools -> SentenceAssembler -> Speech Planner -> SpeechQueue -> player
-> VTS lip sync. A final transcript is accepted independently of response generation,
so the STT socket closes without cancelling the response or blocking capture.
Tasks stay with Task Engine. Session flags permit LISTENING + TOOL_RUNNING.

## Turn taking and interruption

Always-listening framing is 2048 samples at 48 kHz. Endpointing keeps the configured
provider word-gap event, uses an 850 ms lower natural silence window and extends
unfinished clauses to 1500 ms. Partial corrections remain inside the same utterance;
there is no action on partial text. This is bounded timing plus partial-content
heuristics, not a claim of semantic end-of-turn detection for every pause.

Browser AEC remains primary. A short, volatile output spectral reference rejects
strongly correlated residual self-voice while distinct near-end input can barge in.
At detected speech the same player stops locally before a backend round trip;
queued chunks are cancelled by response_id, not by clearing unrelated tasks.
This supplements AEC, not a certified acoustic echo canceller. Speaker/microphone
geometry and real simultaneous speech require acoustic validation.

Generation completion no longer means playback completion. Session ledger tracks
generated_text, spoken_text, cancelled_text, interrupted, chunk acknowledgements,
partial chunk fraction and official emotion. Only complete, actually acknowledged
chunks enter the existing short-term memory policy. Fractional playback does not
invent word alignment. The bounded session context explains interruption; the
permanent Memory V2 policy is not expanded. Text chat keeps its previous policy.

## Speech realization and providers

Speech Planner consumes the official Emotion Runtime state and existing style
options; it does not choose personality or facts. Structured nonverbals include
laugh, light_laugh, chuckle, sigh, hesitation, thinking_pause, surprise, breath and
pause. Unsupported performance tags are removed, never spoken literally. Current
adapters do not claim phonemes, word timestamps or native nonverbals.

Local synthesis remains sentence-buffered (not native acoustic streaming). LLM
streaming already feeds sentence synthesis before full text completion. Online
adapters now expose cancellable S16LE/24 kHz packets through the same bounded
SpeechQueue and player. PCM is volatile and excluded from event history and
Intelligence event persistence. The browser plays incremental 400 ms packets;
packet-boundary continuity needs physical/cloud validation and is not advertised
as sample-accurate gapless playback.

OpenAI and ElevenLabs remain optional and NOT_CONFIGURED without Broker credentials.
Only approved speech text/style reaches their endpoints, not conversation context.
No key is sent to frontend or Satellite. Fallback before any audio uses local;
failure after audio starts cannot replay the already-heard prefix. Provider changes
take effect between sentences. API contracts checked in official documentation:

- [OpenAI Speech / PCM streaming](https://developers.openai.com/api/docs/guides/text-to-speech)
- [ElevenLabs streaming endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/stream)

## Tools, proactive speech and lifecycle

Hardware/Web/Universal Operator continue using their canonical grounding/presenter.
Session task association uses observed Task Engine events and source_turn. Explicit
natural cancellation resolves only an unambiguous session-owned task, delegates to
Task Engine and refuses to cut an active flash/bootloader/recovery operation. A user
claim never becomes device existence or verification. Proactive Presence keeps its
existing relevance, quiet mode, cooldown and dedup; output uses the same player queue.

On shutdown ConversationEngine cancels its own response tasks and closes its volatile
session; existing shutdown owners stop capture/recognition/TTS/queue. No external VTS
process is killed. Restart creates a fresh session and player state; project/Open Loop
persistence remains owned by the existing engines. Window X remains hide-to-tray;
explicit quit uses the existing quit_kazumi coordinator. The incompatible tray helper
must not be used.

## Diagnostics and validation

Voice page exposes the session flags, queue depth and latency distributions. Timing
starts at a real client VAD-end report and ends at actual player acknowledgement,
not at WAV generation. Missing observations produce null/count=0, never invented
latency. Barge-in latency is measured between local detection and real stop call.
Session data and PCM are not continuously recorded. Controlled tests are explicitly
fixtures; neither mock serial nor synthetic speech proves a physical device/voice.

### Release validation — 2026-09-05

Baseline: `a56e3ccf19901a0255a9aa692f11fcc16c703dde`, with a local checkpoint.
169 relevant backend tests and 27 frontend tests passed in the final targeted suites. Frontend production build,
PyInstaller, Tauri release/NSIS and source/package fingerprint comparison passed.
No Rust source changed. Backend artifact SHA-256:
`db1a9ba11838625d33dad1cf01daeb4f6bebaf19f06f74ee7a4030ee86f67e2b`.

The corrected release was launched through the existing Desktop KAZUMI.lnk. The
existing microphone was acquired, listening lease active, and the new session
started with zero turns/tasks/queued speech. Ten controlled synthetic utterances
remained in one session, including three casual turns and an interrupted response.
The local path used Faster-Whisper tiny, Ollama qwen3.5:9b and Kokoro pf_dora.
The selected/configured Deepgram Nova-3 pt-BR path was also exercised through the
official native STT bridge: real interim, final, SpeechStarted and UtteranceEnd.
No credential or Deepgram preference was changed. This is real provider/player
validation with synthetic input, **not a human microphone/acoustic E2E PASS**.

The local detector simulation stopped real playback in 2.4 ms (one observation).
For the three casual turns, STT-final -> first-token average/p50/p95 was
2504.43/2141.12/3344.81 ms; first-token -> TTS request was
791.73/811.84/1302.32 ms; TTS request -> actual first audio was
4154.01/5515.81/5775.45 ms. A later Web turn took considerably longer: first-token
arrived after 52.18 s including research/answer preparation; its first audio took
another 24.25 s after the first TTS request. These are not low-latency guarantees.
The primary user-speech-end -> audio metric remains unmeasured because uploaded
synthetic samples did not provide a real microphone VAD-end observation.

Both callback and promise-rejection failures in a PCM packet now invalidate its
remaining response/end marker, with regression tests: a failed packet must never
be acknowledged as an entirely heard sentence. Late callbacks remain idempotent.

The voice request to open Notepad succeeded. Its follow-up resolved the same
application but the existing operator could not confirm text insertion; KAZUMI
reported that failure. This complete positive two-operation E2E remains pending.
Faster-Whisper tiny mistranscribed several technical sample words. Deepgram also
omitted the synthetic pronunciation of `pio run`; no acoustic accuracy claim is
made. A separate clear voice request about Python triggered real Web search via
the existing engine, retrieving official Python changelog/tutorial documentation.
The exact text query for `pio run` selected the official command-specific page.
Real USB discovery found five USB devices and zero serial ports; the ESP32 request
correctly returned that no ESP32 was found, without LED/serial success claims.

The first validation exposed an existing hardware planner rule that interpreted
any standalone `agora` as project modification. The rule was narrowed to actual
project-edit language, with seven regression cases preserving explicit continuation.
The resulting accidental edit to a controlled Uno test project was restored from
its exact pre-test checkpoint after hash verification. Metadata marks its build
stale so the old build cannot be flashed as if it matched restored sources. Build
history was preserved; there was no flash or physical effect.

VTS remained connected/authenticated with Spout active and HEAD_EYES tracking
unchanged. The active model has no mapped acoustic/visual emotion-specific controls;
the existing graceful neutral presentation was reported, not fabricated expression
support. Proactive voice stayed OFF per the existing operator preference; its
arbitration/failure behavior passed targeted tests, but no live proactive acoustic
test is claimed. OpenAI/ElevenLabs TTS remained NOT_CONFIGURED and were tested via
controlled HTTP streams/failures only, not paid/cloud speech synthesis.

The official quit_kazumi coordinator exited successfully: backend=0, desktop=0,
port 8000 FREE, VTube Studio still alive. Restart had already demonstrated a new
empty session and reacquired microphone. X/hide-to-tray source was preserved;
programmatic window-close validation was denied by the existing Tauri permission.
That permission was not broadened; physical X/Tray clicking remains manual.

Microbenchmarks (2000 iterations) measured about 0.01 ms average/p50/p95 for the
Speech Planner and session snapshot; a controlled 40-turn ledger allocated about
39.9 KB. A 10-second idle sample measured whole backend/desktop working sets of
865.05/61.33 MiB and CPU of 39.41%/3.41% of one logical core. These totals include
existing engines/VTS traffic and loaded models; they do not isolate incremental
voice/VAD/WebView overhead. No new polling loop was introduced in the backend.

Remaining physical validation: human near-end speech over speaker playback, echo
rejection in the operator's room, cloud PCM packet continuity, and real hardware
flash/serial/effect. No physical device was available, and none was simulated as real.
