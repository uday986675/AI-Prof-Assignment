"""Conversation persistence for the AI agent.

Two tables, deliberately minimal:

- ``AgentConversation`` — one dialogue session. The ``state`` JSON column is
  the checkpoint: slot-filling progress, the selected offer, and the created
  appointment reference survive process restarts, so a patient can continue
  the conversation in a new tab/device. ``conversation_id`` is a short public
  identifier (the PK is a platform-internal id).
- ``ConversationEvent`` — append-only turn log (one row per user message and
  per assistant reply) with the actor role; this is what ``GET /agent/sessions/{id}``
  replays and what makes the dialogue auditable alongside AuditEvent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database.base import Base
from .user import new_id, utcnow


class AgentConversation(Base):
    """One patient↔agent dialogue (owner = the patient's user id)."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_created", "user_id", "created_at"),
        Index("ix_conversations_hospital", "hospital_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(12), unique=True, nullable=False, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Tenant context once the dialogue points at a hospital (None until then).
    hospital_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    # active | booked | abandoned
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # Slot-filling checkpoint (see backend/app/agent/state.py).
    state: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    events = relationship(
        "ConversationEvent",
        lazy="raise",
        cascade="all, delete-orphan",
    )


class ConversationEvent(Base):
    """One turn of the dialogue (append-only)."""

    __tablename__ = "conversation_events"
    __table_args__ = (
        Index("ix_conversation_events_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # user | agent
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional structured hint recorded with agent replies (e.g. next_action).
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # How the USER turn arrived: "text" (typed) or "voice" (STT-transcribed).
    # Agent turns are always None. Phase 7 — additive, nullable for old rows.
    audio_origin: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Strictly monotonic wall clock (same pattern as IntegrationOperation):
    # a turn writes the user event and the agent reply back-to-back, and coarse
    # OS clock granularity (Windows) can yield identical utcnow() values — a
    # tie would make ORDER BY created_at non-deterministic when replaying the
    # dialogue. The per-process guard keeps event order == write order.
    _last_created_at: ClassVar[datetime | None] = None

    @staticmethod
    def _next_created_at() -> datetime:
        now = utcnow()
        last = ConversationEvent._last_created_at
        if last is None or now > last:
            ConversationEvent._last_created_at = now
            return now
        ConversationEvent._last_created_at = last + timedelta(microseconds=1)
        return ConversationEvent._last_created_at

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_next_created_at, nullable=False)
