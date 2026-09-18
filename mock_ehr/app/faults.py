"""Fault injection for the Mock EHR — deterministic and easy to toggle.

Two controls, both OFF by default:

1. Environment default:  MOCK_EHR_FAULT_MODE=server_error|timeout|rejected|delay
2. Per-request override: X-EHR-Fault header (dev/test only — ignored when the
   EHR runs with environment=production, and /health is never faulted).

Modes (chosen so Phase 4's failure-recovery scenarios are all reachable):

  server_error → immediate HTTP 500, request NOT processed (clean failure).
  timeout      → processes the request FIRST (the write becomes durable), THEN
                 stalls the response for MOCK_EHR_FAULT_DELAY_SECONDS. A client
                 with a shorter timeout gives up (unknown outcome) while the EHR
                 has definitively recorded the appointment — exactly the situation
                 Phase 4's "query the EHR to learn the truth" flow resolves.
  rejected     → immediate HTTP 422, request NOT processed.
  delay        → processes, then sleeps briefly (capped at 2s) before responding.

This middleware is isolated from business logic on purpose: no route or service
knows it exists, and it is inert unless explicitly enabled.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import settings

FAULT_MODES = {"none", "server_error", "timeout", "rejected", "delay"}


def effective_mode(request: Request) -> str:
    mode = settings.mock_ehr_fault_mode if settings.mock_ehr_fault_mode in FAULT_MODES else "none"
    if not settings.is_production:  # header override is a dev/test control only
        mode = request.headers.get("X-EHR-Fault", mode)
        if mode not in FAULT_MODES:
            mode = "none"
    # Optional path targeting: an empty list faults every endpoint; otherwise
    # only EXACTLY the listed paths are faulted. Exact (not prefix) matching is
    # deliberate: POST /ehr/appointments must be faultable without also
    # faulting GET /ehr/appointments/{id} or the by-platform-ref lookup that
    # Phase 4 reconciliation depends on.
    targeted = {p.strip() for p in (settings.mock_ehr_fault_paths or "").split(",") if p.strip()}
    if targeted and request.url.path not in targeted:
        return "none"
    return mode


class FaultInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/health":
            return await call_next(request)

        mode = effective_mode(request)
        if mode == "none":
            return await call_next(request)

        if mode == "server_error":
            return JSONResponse(
                status_code=500,
                content={"detail": "Injected EHR server error", "fault": "server_error"},
            )
        if mode == "rejected":
            return JSONResponse(
                status_code=422,
                content={"detail": "Injected EHR rejection", "fault": "rejected"},
            )
        if mode in ("timeout", "delay"):
            # Process FIRST so the write is durable even if the client times out
            # and disconnects (uvicorn cancels pending handlers otherwise),
            # THEN stall the response to simulate the lost reply.
            response = await call_next(request)
            cap = settings.mock_ehr_fault_delay_seconds if mode == "timeout" else 2.0
            await asyncio.sleep(min(settings.mock_ehr_fault_delay_seconds, cap))
            return response


def install_fault_middleware(app: FastAPI) -> None:
    app.add_middleware(FaultInjectionMiddleware)
