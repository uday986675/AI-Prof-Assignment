"""Voice (Phase 7) — speech boundary for the AI agent.

Exports:
- transcribe_wav: audio bytes -> text (STT; Gemini-first, Groq Whisper fallback)
- synthesize_speech: text -> WAV bytes (TTS; Gemini-first, graceful None)
- SpeechError: raised when STT cannot complete (caller falls back to text)
- provider_status: which providers are configured, for /agent/voice/status
"""
from .speech import (
    SpeechError,
    provider_status,
    synthesize_speech,
    transcribe_wav,
)

__all__ = [
    "SpeechError",
    "provider_status",
    "synthesize_speech",
    "transcribe_wav",
]
