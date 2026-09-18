"""Pydantic schemas for the AI agent API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ..core.validators import validate_nonempty


class AgentSessionCreate(BaseModel):
    """Empty body; a session always starts with a clean slot-filling state."""


class AgentSessionOut(BaseModel):
    conversation_id: str
    status: str
    state: dict
    hospital_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentSessionDetailOut(AgentSessionOut):
    events: list["ConversationEventOut"] = []


class AgentMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def _message_ok(cls, v: str) -> str:
        value = validate_nonempty(v, "message", max_len=1000)
        return value


class ConversationEventOut(BaseModel):
    role: str
    content: str
    meta: dict | None = None
    audio_origin: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentMessageOut(BaseModel):
    conversation_id: str
    status: str
    reply: str
    meta: dict
    state: dict


AgentSessionDetailOut.model_rebuild()


class VoiceMessageOut(BaseModel):
    """Reply to POST /agent/sessions/{id}/voice — one voice turn.

    ``transcript`` is what the patient's utterance was understood as (STT);
    ``reply`` is the agent's answer text (always present — the source of
    truth); ``audio_base64`` is a WAV rendering of that reply when TTS was
    available, else None (the client then renders the text itself).
    """

    conversation_id: str
    status: str
    transcript: str
    reply: str
    meta: dict
    state: dict
    audio_base64: str | None = None
    audio_mime_type: str | None = None


class VoiceStatusOut(BaseModel):
    """Voice capability report — the demo page uses it to show what works."""

    stt_provider: str | None
    stt_model: str | None
    tts_available: bool
    tts_model: str | None
    voice_mode: str = "web_stt_server_tts"
