"""Mock EHR — a separate external hospital EHR simulation.

Deliberately isolated from backend/app: own config, database, models, API.
The platform talks to it ONLY over HTTP through backend.app.integrations.ehr.
"""
