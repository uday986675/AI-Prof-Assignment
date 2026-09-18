"""AI agent package (Phase 5).

Layering (mirrors the platform's strict boundaries):

    API route (backend/app/api/routes/agent.py)
        -> AgentService (session orchestration, persistence)
            -> agent graph (LangGraph; LLM classifies/extracts ONLY)
                -> capabilities (controlled tools)
                    -> SchedulingService / EHRSyncService / EHRRecoveryService

The agent NEVER imports SQLAlchemy models for writes, never touches HTTP, and
never talks to the Mock EHR. Every side effect goes through capabilities, which
delegate to the same services the REST API uses (revalidation, idempotency,
correlation IDs, RBAC and tenant isolation are inherited, not reimplemented).
"""
