# Phase 7 — Voice: Setup, API & Manual Walkthrough

Phase 7 closes the loop of the PRD's *"Pre-Visit Voice Agent"*: a patient can now
**speak** to the AI appointment assistant instead of typing. The pipeline is

```
browser microphone (MediaRecorder)
   →  POST /agent/sessions/{id}/voice     (audio upload)
   →  STT  (Gemini audio understanding — same GEMINI_API_KEY, no Groq needed)
   →  the EXISTING Phase 5/6 agent turn (state machine + capabilities + EHR)
   →  reply text  +  TTS audio (base64 WAV, best-effort)
```

**Ground rules (kept from Phases 5/6):**
- **Groq is never required.** STT/TTS are Gemini-first, using the same
  `GEMINI_API_KEY` the chat LLM already uses. `STT_PROVIDER=groq` (Whisper)
  remains available as an *explicit* fallback.
- **Text always works.** Any STT failure (no provider, quota, bad format) is a
  clean `422` — the client switches to typing. Any TTS failure is silent —
  the reply text is still served. The typed path is untouched.
- **No audio is ever persisted.** Only the transcript reaches the database
  (as a normal conversation event with `audio_origin="voice"`).
- The agent/dialogue logic is 100% reused — voice is a new *transport*, not a
  new brain.

---

## 1. Configuration (`.env`)

```ini
# Voice (Phase 7) — Gemini-first; Groq never required
STT_PROVIDER=gemini                  # gemini (default) | groq (Whisper fallback)
STT_MODEL=gemini-2.5-flash           # STT uses its OWN model/quota bucket,
                                     # NOT the chat model (GEMINI_MODEL)
WHISPER_MODEL=whisper-large-v3-turbo # only used when STT_PROVIDER=groq
TTS_MODEL=gemini-2.5-flash-preview-tts
TTS_VOICE_NAME=Zephyr
```

> Why a separate `STT_MODEL`? On the free Gemini tier the chat model
> (`gemini-3.6-flash`) has a very small per-day quota. Transcription does not
> need the smartest model — routing STT to `gemini-2.5-flash` keeps the chat
> quota for the conversation itself.

---

## 2. New API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/agent/sessions/{id}/voice` | **patient** (owner) | Full voice turn: audio → transcript → agent reply (+TTS audio) |
| `POST` | `/agent/voice/transcribe` | any authenticated | STT-only probe (no conversation touched) |
| `GET`  | `/agent/voice/status` | any authenticated | Which speech providers are configured |

`POST /agent/sessions/{id}/voice` (multipart form):
- `audio` — file (WAV / raw 16-bit mono PCM / webm / ogg, ≤ 8 MiB)
- `mime_type` — form field, default `audio/wav`

Response (`200`):
```json
{
  "conversation_id": "…", "status": "active",
  "transcript": "I need to see an orthopedic doctor this week",
  "reply": "Would you prefer an in-person visit or a video consultation?",
  "meta": { "next_action": "collect_details" }, "state": { },
  "audio_base64": "UklGRi…", "audio_mime_type": "audio/wav"
}
```
`audio_base64` is `null` whenever TTS is unavailable or fails — play the text
instead. STT failures return `422 {"detail": "…"}`; client falls back to text.

Authorization: the voice turn is **patient-only** and **owner-scoped**
(cross-owner → `404`, doctor/admin → `403`), identical to the typed agent path.
Ownership is verified **before** the audio is transcribed.

---

## 3. Browser demo page

```
http://127.0.0.1:8000/voice-demo
```

A single self-contained page (`backend/app/static/voice-demo.html`): log in
with a patient account → new conversation → record → the transcript and the
agent's reply appear in a chat log, and the reply plays back as audio
(`<audio controls>`, auto-play attempt + controls). A typed input at the
bottom is always available — voice and text can interleave freely in the same
conversation.

---

## 4. New tests (27 → total 189)

`python -m unittest tests.test_phase7_voice -v`

- **A. Speech boundary (11)** — transcribe happy path; empty/oversize/bad-format
  rejection; no-provider `SpeechError`; gemini-first selection; groq fallback;
  explicit-groq-without-key falls back to gemini; provider errors surface;
  TTS wraps PCM in a real RIFF/WAVE container; TTS None without key/text;
  TTS failure is soft.
- **B. Status & transcribe probe (4)** — 401 unauthenticated; capability report;
  transcribe happy path; STT failure → 422.
- **C. Voice turn (8)** — happy path (transcript + reply + WAV audio); a voice
  conversation books a real appointment; STT failure → 422 (text fallback);
  TTS failure → 200 with text-only reply; doctor role → 403; foreign patient →
  404; unknown session → 404; missing audio → 422.
- **D. Persistence & privacy (4)** — user event recorded with
  `audio_origin="voice"`; typed turns stay `text`; mixed conversations keep
  both origins in order; **no audio bytes ever reach the database**.

All providers are mocked in tests — the suite is fully offline and hermetic.

---

## 5. Manual smoke test (Git Bash)

Start the services (Mock EHR first, then the platform):

```bash
python -m uvicorn mock_ehr.app.main:app --port 8001        # terminal 1
python -m uvicorn backend.app.main:app --port 8000         # terminal 2
python -m backend.scripts.seed_demo                        # once
```

### 5.1 Round-trip with curl (no microphone needed)

Generate a real WAV with Gemini TTS, then speak it to the API:

```bash
API=http://127.0.0.1:8000
TOK=$(curl -s -X POST $API/auth/login -H "Content-Type: application/json" \
  -d '{"email":"patient@demo.health","password":"Demo1234!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# capability check
curl -s $API/agent/voice/status -H "Authorization: Bearer $TOK"; echo

# create a session
CONV=$(curl -s -X POST $API/agent/sessions -H "Authorization: Bearer $TOK" | python -c "import sys,json;print(json.load(sys.stdin)['conversation_id'])")

# TTS -> WAV -> voice turn (synthesize the patient's sentence, send it as audio)
python - <<EOF
from backend.app.speech import synthesize_speech
open("speech.wav","wb").write(synthesize_speech(
  "I need to see an orthopedic doctor for my shoulder pain sometime this week"))
EOF

curl -s -X POST $API/agent/sessions/$CONV/voice \
  -H "Authorization: Bearer $TOK" \
  -F "mime_type=audio/wav" -F "audio=@speech.wav" | python -m json.tool
```

Expected: `transcript` is the sentence verbatim; `reply` asks the visit-type
question; `audio_base64` is a WAV rendering of the reply. Continue the
conversation by voice or by typing:

```bash
curl -s -X POST $API/agent/sessions/$CONV/messages \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"message":"in person"}' | python -m json.tool          # availability
curl -s -X POST $API/agent/sessions/$CONV/messages \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"message":"1"}' | python -m json.tool                  # book + EHR sync
```

### 5.2 Browser demo

1. Open `http://127.0.0.1:8000/voice-demo`
2. Log in (`patient@demo.health` / `Demo1234!`)
3. **＋ New conversation** → hold the mic button and speak the sentence above
4. Watch the transcript appear, the agent ask the visit-type question, and the
   reply play as audio; answer by voice ("in person") or by typing ("1")
5. Verify in Swagger (`/docs` → GET `/agent/sessions/{id}`) that user events
   show `audio_origin: "voice"` and typed ones `"text"`.

### 5.3 Text-fallback demo (optional)

Temporarily set `STT_PROVIDER=none` in `.env`, restart the platform, and repeat
a voice turn: you should get `422` with a clear detail message — the typed path
still works. Restore `STT_PROVIDER=gemini` afterwards.

---

## 6. What Phase 7 deliberately does NOT do

- No streaming/duplex voice (WebSocket audio) — one-shot recordings only.
- No automatic speech-to-speech barge-in; the demo plays TTS via an audio element.
- No audio retention: recordings are processed in-memory and discarded (privacy).
