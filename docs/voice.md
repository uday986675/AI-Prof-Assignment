# Voice Architecture (Phase 7)

How speech is wired into the platform without touching the agent's brain.

```
Browser (voice-demo.html, MediaRecorder)
    │  POST /agent/sessions/{id}/voice  (multipart: audio + mime_type)
    ▼
voice.py (thin route)  ── ensure_owned_session BEFORE any paid work
    │
    ├─► speech.transcribe_wav          STT boundary (SpeechError on any failure)
    │       ├─ gemini (default): google-genai inline_data, STT_MODEL bucket
    │       └─ groq   (explicit): Whisper via GROQ_API_KEY
    │
    ├─► AgentService.handle_message(..., audio_origin="voice")
    │       └─ the UNCHANGED Phase 5/6 turn: state machine → capabilities →
    │          scheduling engine → booking → EHR sync/reconciliation →
    │          questionnaire collection; events persisted with provenance
    │
    └─► speech.synthesize_speech       TTS boundary (best-effort, None on failure)
            └─ gemini-2.5-flash-preview-tts → PCM wrapped in RIFF/WAVE (stdlib)
```

## Design rules

1. **Voice is a transport, not a brain.** The dialogue logic (slot filling,
   capabilities, EHR honesty) is exactly the typed path's code. Voice only
   changes how the user's utterance arrives and how the reply is rendered.
2. **One speech boundary.** All provider code lives in
   `backend/app/speech/`; no route or service imports a cloud SDK. Swapping
   providers means editing one module.
3. **Gemini-first, Groq never required.** STT defaults to Gemini audio
   understanding with the same `GEMINI_API_KEY` (separate `STT_MODEL` so the
   chat model's quota bucket is untouched). Whisper remains available via
   `STT_PROVIDER=groq`.
4. **Text fallback is a contract, not a feature.** STT failure → `SpeechError`
   → HTTP 422 (never an unhandled 500 — provider exceptions are wrapped).
   TTS failure → `None` → text-only reply. Typed input always works.
5. **Privacy.** Audio lives in memory only; the transcript is persisted as a
   normal `ConversationEvent` with `audio_origin="voice"|"text"`. No audio
   bytes, no transcripts in logs.
6. **Cheap checks first.** The route validates auth and session ownership
   before decoding/transcribing audio, so rejected requests cost nothing.

## Why a separate STT model

Free-tier Gemini quotas are **per model per day**. Pointing STT at the chat
model (`GEMINI_MODEL`) would burn the conversation quota on transcription;
`STT_MODEL=gemini-2.5-flash` isolates voice traffic in its own bucket.

## Test strategy

`tests/test_phase7_voice.py` mocks `_gemini_transcribe` / `_gemini_speak` /
`_groq_transcribe` at the speech boundary: the suite verifies platform
behavior (auth, RBAC, ownership, provenance, fallbacks, WAV container
correctness) without network, API keys, or quota. Live behavior is covered by
the manual walkthrough in `README7.md` (real TTS→STT round trip).
