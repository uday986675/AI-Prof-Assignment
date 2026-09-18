"""Pydantic schemas for pre-visit questionnaires (Phase 6)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..core.validators import validate_nonempty

QUESTION_KIND = Literal["free_text", "boolean", "single_choice", "multiple_choice", "scale_1_10"]


class QuestionIn(BaseModel):
    """One question in a template definition."""

    key: str
    kind: QUESTION_KIND
    prompt: str
    required: bool = False
    options: list[str] | None = None

    @field_validator("key", "prompt")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return validate_nonempty(value, "value")

    model_config = {"extra": "forbid"}


class QuestionnaireTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["standard", "specialty"] = "standard"
    description: str | None = None
    questions: list[QuestionIn] = Field(min_length=1)


class QuestionnaireTemplateOut(BaseModel):
    id: str
    name: str
    kind: str
    description: str | None
    questions: list[dict[str, Any]]
    is_active: bool
    version: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AnswerIn(BaseModel):
    """One conversational answer: question key + raw value (validated against
    the template by QuestionnaireService before storage)."""

    key: str = Field(min_length=1, max_length=100)
    value: Any = None


class AnswersIn(BaseModel):
    """Batch answers (the form-submission path)."""

    answers: dict[str, Any] = Field(default_factory=dict)


class AssignmentOut(BaseModel):
    id: str
    appointment_id: str
    template_id: str
    template_name: str | None = None
    kind: str | None = None
    questions: list[dict[str, Any]] = []
    status: str
    answers: dict[str, Any] = {}
    conversation_id: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
