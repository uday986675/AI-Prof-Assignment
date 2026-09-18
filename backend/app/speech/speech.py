"""STT/TTS providers — one boundary, no provider logic anywhere else.

Phase 7 ground rules (mirrors the Phase 5 LLM philosophy):
- **Gemini first, Groq NOT required.** STT uses the same ``GEMINI_API_KEY`` the
  chat LLM uses (audio understanding via ``google-genai`` inline_data) — no new
  dependency, no Groq key. ``groq`` STT remains available as an explicit
  fallback via ``STT_PROVIDER=groq`` (uses GROQ_API_KEY + WHISPER_MODEL).
- **Text always works.** Every failure mode (no key, provider error, timeout,
  empty transcript, unsupported format) raises SpeechError; the API layer
  answers with a JSON error the client can catch and switch to typing.
- No secrets are logged; audio bytes are never persisted anywhere.
"""
from __future__ import annotations

import io
import logging
import wave
from typing import Any

from ..core.config import settings

logger = logging.getLogger("speech")

# Only what a browser microphone produces and the Gemini API accepts.
SUPPORTED_MIME_TYPES = {"audio/wav", "audio/wave", "audio/x-wav", "audio/webm", "audio/ogg"}
MAX_AUDIO_BYTES = 8 * 1024 * 1024  # 8 MiB — a short clinical utterance is <1 MiB


class SpeechError(Exception):
    """STT/TTS could not be completed — caller must fall back (e.g. to text)."""


def _stt_provider() -> str:
    forced = (settings.stt_provider or "").strip().lower()
    if forced == "groq" and settings.groq_api_key:
        return "groq"
    if forced == "groq":
        logger.warning("STT_PROVIDER=groq but GROQ_API_KEY is empty; using Gemini STT")
    if settings.gemini_api_key:
        return "gemini"
    if settings.groq_api_key:
        return "groq"
    raise SpeechError(
        "no speech provider configured (set GEMINI_API_KEY, or GROQ_API_KEY for Whisper)"
    )


# --------------------------------------------------------------------------- STT


def _gemini_transcribe(audio: bytes, mime_type: str) -> str:
    """Gemini audio understanding — the model writes out exactly what it hears."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.stt_model or "gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=audio, mime_type=mime_type),
            "Transcribe this audio recording. Output ONLY the transcribed words, "
            "no commentary, no punctuation commentary, no markdown.",
        ],
    )
    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise SpeechError("transcription returned no text")
    return text


def _groq_transcribe(audio: bytes, mime_type: str) -> str:
    """Groq Whisper STT — explicit fallback provider (GROQ_API_KEY required)."""
    try:
        from groq import Groq
    except ImportError as exc:  # pragma: no cover - env without the package
        raise SpeechError("groq SDK not installed") from exc

    client = Groq(api_key=settings.groq_api_key)
    ext = "webm" if "webm" in mime_type else ("ogg" if "ogg" in mime_type else "wav")
    with tempfile_speech_file(audio, ext) as path:
        try:
            transcription = client.audio.transcriptions.create(
                model=(settings.whisper_model or "whisper-large-v3-turbo"),
                file=open(path, "rb"),
            )
        except Exception as exc:
            raise SpeechError(f"whisper transcription failed: {exc}") from exc
    return (getattr(transcription, "text", "") or "").strip()


def _wrap_pcm_in_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM in a RIFF/WAVE header (stdlib only)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def tempfile_speech_file(audio: bytes, ext: str):
    """Context manager yielding a temp path for SDKs that need a real file."""
    import contextlib
    import os
    import tempfile

    @contextlib.contextmanager
    def _ctx():
        fd, path = tempfile.mkstemp(suffix=f".{ext}", prefix="speech-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio)
            yield path
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    return _ctx()


def transcribe_wav(audio: bytes, mime_type: str = "audio/wav") -> str:
    """Speech -> text. Raises SpeechError when STT cannot be completed.

    Accepts a WAV (optionally raw 16-bit mono PCM, which gets wrapped in a
    RIFF header first) plus common browser formats (webm/ogg) when the
    provider supports them.
    """
    if not audio:
        raise SpeechError("empty audio upload")
    if len(audio) > MAX_AUDIO_BYTES:
        raise SpeechError("audio upload too large (max 8 MiB)")
    mime_type = (mime_type or "audio/wav").lower()
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise SpeechError(f"unsupported audio format: {mime_type}")

    provider = _stt_provider()
    logger.info("STT via %s (%s bytes, %s)", provider, len(audio), mime_type)

    # Raw PCM (e.g. from recorders emitting plain 16-bit mono) has no RIFF tag —
    # wrap it so downstream SDKs see a normal WAV file.
    if "wav" in mime_type and audio[:4] != b"RIFF" and audio[:4] != b"RIFX":
        audio = _wrap_pcm_in_wav(audio, 16000)

    if provider == "gemini":
        try:
            return _gemini_transcribe(audio, mime_type)
        except SpeechError:
            raise
        except Exception as exc:
            # Quota exhaustion, auth errors, outages — the client must get a
            # clean 422 and fall back to typing, never an unhandled 500.
            logger.warning("gemini STT failed (%s); surfacing as SpeechError", exc)
            raise SpeechError(f"stt provider failed: {exc}") from exc
    try:
        return _groq_transcribe(audio, mime_type)
    except SpeechError:
        raise
    except Exception as exc:
        logger.warning("groq STT failed (%s); surfacing as SpeechError", exc)
        raise SpeechError(f"stt provider failed: {exc}") from exc


# --------------------------------------------------------------------------- TTS


# Cheap, pleasant default; kept in config so deployments can switch freely.
_TTS_VOICE_NAME = "Zephyr"


def _gemini_speak(text: str) -> bytes | None:
    """Gemini TTS -> 24kHz 16-bit mono PCM, wrapped in a WAV container."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.tts_model,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=settings.tts_voice_name or _TTS_VOICE_NAME
                    )
                )
            ),
        ),
    )
    try:
        inline = response.candidates[0].content.parts[0].inline_data
    except (AttributeError, IndexError, TypeError) as exc:
        raise SpeechError(f"tts response had no audio payload: {exc}") from exc
    if not inline or not inline.data:
        raise SpeechError("tts response had no audio payload")
    sample_rate = 24000
    if inline.mime_type and "rate=" in inline.mime_type:
        try:
            sample_rate = int(inline.mime_type.split("rate=")[1].split(";")[0])
        except (IndexError, ValueError):
            pass
    return _wrap_pcm_in_wav(inline.data, sample_rate)


def synthesize_speech(text: str) -> bytes | None:
    """Text -> WAV bytes, or None when TTS is unavailable/failed.

    Phase 7 contract: TTS is *best-effort*. The agent reply text is always the
    source of truth; when no provider is configured or the call fails, the
    client silently falls back to displaying/rendering the text locally.
    """
    text = (text or "").strip()
    if not text:
        return None
    if not settings.gemini_api_key:
        return None  # TTS is Gemini-only; text fallback always remains
    try:
        return _gemini_speak(text[:1000])
    except Exception as exc:
        logger.warning("TTS failed (%s); returning text-only reply", exc)
        return None


def provider_status() -> dict[str, Any]:
    """Small capability report for the voice demo page / diagnostics."""
    stt: str | None = None
    try:
        stt = _stt_provider()
    except SpeechError:
        stt = None
    return {
        "stt_provider": stt,
        "stt_model": (settings.whisper_model if stt == "groq" else None)
        or (settings.stt_model if stt == "gemini" else None),
        "tts_available": bool(settings.gemini_api_key),
        "tts_model": settings.tts_model if settings.gemini_api_key else None,
    }
