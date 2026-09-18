"""Mock EHR API-key authentication.

The platform's connector sends `X-API-Key: <MOCK_EHR_API_KEY>`. Comparison is
constant-time; missing/invalid keys get 401. The key comes from the environment
— it is never hardcoded.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import settings


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.mock_ehr_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
