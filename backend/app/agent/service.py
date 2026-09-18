"""AgentService — session orchestration and persistence for the AI agent.

Owns the AgentConversation/ConversationEvent rows; the graph (graph.py) does
one conversational turn, this service makes it durable:

    start_session      → new conversation row (checkpoint = empty state)
    handle_message     → load state → run_turn (graph) → persist state + turns
    get_session        → owner/platform-admin-scoped read with event log

Persistence means a conversation survives process restarts: the checkpoint
(slot-filling progress, offered slots, appointment reference) lives in the
platform database, so the next message continues exactly where the last
ended — even in a different process.
"""
from __future__ import annotations

import secrets
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import AgentConversation, ConversationEvent, User
from .graph import run_turn
from .state import ConversationState


class AgentService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def start_session(self, user: User) -> AgentConversation:
        conv = AgentConversation(user_id=user.id, state=ConversationState().state_dict())
        self.db.add(conv)
        self.db.commit()
        self.db.refresh(conv)
        return conv

    def list_sessions(self, user: User) -> list[AgentConversation]:
        return list(
            self.db.query(AgentConversation)
            .filter(AgentConversation.user_id == user.id)
            .order_by(AgentConversation.created_at.desc())
        )

    def _get_owned(self, conversation_id: str, user: User) -> AgentConversation:
        conv = (
            self.db.query(AgentConversation)
            .filter(AgentConversation.conversation_id == conversation_id)
            .first()
        )
        if conv is None:
            raise HTTPException(404, "Conversation not found")
        if user.role != "platform_admin" and conv.user_id != user.id:
            # Existence hidden across owners (same convention as tenant 404s).
            raise HTTPException(404, "Conversation not found")
        return conv

    def ensure_owned_session(self, conversation_id: str, user: User) -> AgentConversation:
        """Public ownership check — cheap guard used before expensive work
        (e.g. the voice route verifies the session BEFORE paying for STT)."""
        return self._get_owned(conversation_id, user)

    def get_session(self, conversation_id: str, user: User) -> dict:
        """Conversation + full turn log (owner or platform admin scope).

        Returns a plain dict rather than the ORM object: the events
        relationship is lazy="raise" (audit rows must be read deliberately,
        not accidentally), so the route serializes this dict directly.
        """
        conv = self._get_owned(conversation_id, user)
        events = (
            self.db.query(ConversationEvent)
            .filter(ConversationEvent.conversation_id == conv.id)
            .order_by(ConversationEvent.created_at.asc(), ConversationEvent.id.asc())
            .all()
        )
        return {
            "conversation_id": conv.conversation_id,
            "status": conv.status,
            "state": conv.state or {},
            "hospital_id": conv.hospital_id,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "events": [
                {
                    "role": e.role,
                    "content": e.content,
                    "meta": e.meta,
                    "audio_origin": e.audio_origin,
                    "created_at": e.created_at,
                }
                for e in events
            ],
        }

    # ------------------------------------------------------------------
    # one conversational turn
    # ------------------------------------------------------------------

    def handle_message(self, conversation_id: str, user: User, message: str,
                       audio_origin: str = "text") -> dict:
        """One conversational turn (typed text, or transcribed voice — Phase 7).

        ``audio_origin`` records how the USER utterance arrived ("text" | "voice")
        so the event log shows the provenance of every turn; agent replies are
        always None. The dialogue itself is identical for both origins.
        """
        if audio_origin not in ("text", "voice"):
            raise HTTPException(422, "audio_origin must be 'text' or 'voice'")
        conv = self._get_owned(conversation_id, user)
        message = (message or "").strip()
        if not message:
            raise HTTPException(422, "Message must not be empty")
        if len(message) > 1000:
            raise HTTPException(422, "Message too long (max 1000 chars)")

        state = ConversationState.from_state(conv.state)

        correlation_id = uuid.uuid4().hex  # one per turn; book_appointment stamps it
        reply, meta = run_turn(self.db, user, state, message, correlation_id=correlation_id)

        conv.state = state.state_dict()  # checkpoint after the turn
        conv.hospital_id = state.hospital_id or conv.hospital_id
        if meta.get("next_action") == "booked" and meta.get("appointment_id"):
            conv.status = "booked"
        self.db.add(
            ConversationEvent(
                conversation_id=conv.id,
                role="user",
                content=message,
                audio_origin=audio_origin,
            )
        )
        self.db.add(
            ConversationEvent(
                conversation_id=conv.id,
                role="agent",
                content=reply,
                meta=meta or None,
            )
        )
        self.db.commit()
        self.db.refresh(conv)
        return {
            "conversation_id": conv.conversation_id,
            "status": conv.status,
            "reply": reply,
            "meta": meta,
            "state": state.state_dict(),
        }
