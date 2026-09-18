"""Agent API routes — patient-facing AI conversation endpoints.

    POST /agent/sessions                  start a new conversation
    GET  /agent/sessions                  list the caller's conversations
    GET  /agent/sessions/{id}             one conversation + full event log
    POST /agent/sessions/{id}/messages    send a patient utterance, get the reply

Authorization: patient role only for sessions/messages (agents are a patient
surface); session reads also allow the platform admin. Cross-owner access
returns 404 (existence hidden), matching the platform's tenant convention.
Routes stay thin: logic lives in AgentService and the capability layer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ...agent.service import AgentService
from ...auth import get_current_user, require_patient
from ...database.base import get_db
from ...models import User
from ...schemas.agent import (
    AgentMessageIn,
    AgentMessageOut,
    AgentSessionDetailOut,
    AgentSessionOut,
)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/sessions", response_model=AgentSessionOut, status_code=status.HTTP_201_CREATED)
def create_session(user: User = Depends(require_patient), db: Session = Depends(get_db)):
    return AgentService(db).start_session(user)


@router.get("/sessions", response_model=list[AgentSessionOut])
def list_sessions(user: User = Depends(require_patient), db: Session = Depends(get_db)):
    return AgentService(db).list_sessions(user)


@router.get("/sessions/{conversation_id}", response_model=AgentSessionDetailOut)
def get_session(conversation_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return AgentService(db).get_session(conversation_id, user)


@router.post("/sessions/{conversation_id}/messages", response_model=AgentMessageOut)
def send_message(conversation_id: str, payload: AgentMessageIn,
                 user: User = Depends(require_patient), db: Session = Depends(get_db)):
    return AgentService(db).handle_message(conversation_id, user, payload.message)
