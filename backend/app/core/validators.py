"""Lightweight validators (no external validation libraries required)."""
from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\- ]{5,19}$")


def validate_email(value: str) -> str:
    value = (value or "").strip().lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("invalid email address")
    return value


def validate_phone(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip()
    if not _PHONE_RE.match(value):
        raise ValueError("invalid phone number")
    return value


def validate_time_str(value: str) -> str:
    """Validate 'HH:MM' 24-hour time strings; returns the normalized string."""
    value = (value or "").strip()
    if not _TIME_RE.match(value):
        raise ValueError("time must be HH:MM (24-hour)")
    return value


def validate_nonempty(value: str, field: str, max_len: int = 200) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return value
