"""All SQLAlchemy models. Importing this package registers every table with Base.metadata."""
from .appointment import (
    ALLOWED_TRANSITIONS,
    APPOINTMENT_TYPES,
    EHR_SYNC_TRANSITIONS,
    SLOT_BLOCKING_STATUSES,
    Appointment,
    ehr_sync_can_transition,
)
from .audit import AuditEvent
from .conversation import AgentConversation, ConversationEvent
from .doctor import BlockedPeriod, Doctor, DoctorAvailability
from .integration import IntegrationOperation, OPERATION_OUTCOMES
from .patient import Patient
from .questionnaire import (
    ASSIGNMENT_STATUSES,
    QUESTION_KINDS,
    TEMPLATE_KINDS,
    QuestionnaireAssignment,
    QuestionnaireTemplate,
)
from .user import Hospital, User

__all__ = [
    "ALLOWED_TRANSITIONS",
    "APPOINTMENT_TYPES",
    "EHR_SYNC_TRANSITIONS",
    "AgentConversation",
    "Appointment",
    "AuditEvent",
    "ConversationEvent",
    "BlockedPeriod",
    "Doctor",
    "DoctorAvailability",
    "EHR_SYNC_TRANSITIONS",
    "ehr_sync_can_transition",
    "Hospital",
    "IntegrationOperation",
    "OPERATION_OUTCOMES",
    "Patient",
    "QUESTION_KINDS",
    "TEMPLATE_KINDS",
    "ASSIGNMENT_STATUSES",
    "QuestionnaireAssignment",
    "QuestionnaireTemplate",
    "SLOT_BLOCKING_STATUSES",
    "User",
]
