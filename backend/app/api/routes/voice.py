"""Voice API routes (Phase 7) — STT -> agent turn -> TTS, text always available.

    POST /agent/sessions/{id}/voice   audio upload -> transcript -> agent turn ->
                                      reply text (+ base64 WAV TTS when available)
    POST /agent/voice/transcribe      STT-only probe (authed) — demo/diagnostics
    GET  /agent/voice/status          which speech providers are configured

Authorization mirrors the agent routes: sessions are patient-owned (cross-owner
access returns 404, existence hidden); /transcribe and /status accept any
authenticated user (they never touch patient data). Routes stay thin — STT/TTS
live in the speech package, dialogue in AgentService, and the conversation
event log records the user turn as ``audio_origin="voice"``.
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from ...agent.service import AgentService
from ...auth import get_current_user, require_patient
from ...core.config import settings
from ...database.base import get_db
from ...models import User
from ...schemas.agent import VoiceMessageOut, VoiceStatusOut
from ...speech import SpeechError, provider_status, synthesize_speech, transcribe_wav

router = APIRouter(prefix="/agent", tags=["agent-voice"])

_AUDIO_FIELD = File(description="Audio recording (WAV/PCM 16-bit mono, webm, or ogg)")
_MIME_FIELD = Form(default="audio/wav", description="MIME type of the upload")


def _voice_reply_payload(result: dict, transcript: str) -> dict:
    """Attach best-effort TTS to an agent turn result (never fails the turn)."""
    audio_base64, audio_mime = None, None
    wav = synthesize_speech(result["reply"])
    if wav:
        audio_base64 = base64.b64encode(wav).decode("ascii")
        audio_mime = "audio/wav"
    return {**result, "transcript": transcript,
            "audio_base64": audio_base64, "audio_mime_type": audio_mime}


@router.post("/sessions/{conversation_id}/voice", response_model=VoiceMessageOut)
def speak_to_session(
    conversation_id: str,
    audio: UploadFile = _AUDIO_FIELD,
    mime_type: str = _MIME_FIELD,
    user: User = Depends(require_patient),
    db: Session = Depends(get_db),
):
    """One full voice turn: transcribe -> agent dialogue -> (optional) TTS."""
    # Ownership FIRST — never spend a paid STT call on a request we'd reject.
    AgentService(db).ensure_owned_session(conversation_id, user)
    blob = audio.file.read()
    try:
        transcript = transcribe_wav(blob, (mime_type or "audio/wav").strip())
    except SpeechError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    result = AgentService(db).handle_message(
        conversation_id, user, transcript, audio_origin="voice"
    )
    return _voice_reply_payload(result, transcript)


@router.post("/voice/transcribe")
def transcribe_only(
    audio: UploadFile = _AUDIO_FIELD,
    mime_type: str = _MIME_FIELD,
    user: User = Depends(get_current_user),
):
    """STT probe without touching a conversation (diagnostics/demo)."""
    blob = audio.file.read()
    try:
        transcript = transcribe_wav(blob, (mime_type or "audio/wav").strip())
    except SpeechError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"transcript": transcript, "stt_provider": provider_status()["stt_provider"]}


@router.get("/voice/status", response_model=VoiceStatusOut)
def voice_status(user: User = Depends(get_current_user)):
    info = provider_status()
    return {**info, "voice_mode": "web_stt_server_tts", "tts_model": settings.tts_model if info["tts_available"] else None}
