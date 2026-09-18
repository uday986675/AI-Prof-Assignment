"""Mock EHR entrypoint.

Run:  python -m uvicorn mock_ehr.app.main:app --port 8001

The FastAPI app itself lives in mock_ehr.app.api.routes (single module keeps the
prototype small); this module re-exports it so uvicorn's "module:app" target
matches the documented command.
"""
from .api.routes import app  # noqa: F401

__all__ = ["app"]
