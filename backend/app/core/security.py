"""Security primitives.

- Password hashing with bcrypt (used directly; passlib not required).
- JWT (HS256) implemented with the standard library: HMAC-SHA256 signing is
  small and auditable. Tokens carry: sub (user id), role, hospital_id, exp, jti.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import bcrypt

from .config import settings


# ---------- passwords ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ---------- JSON Web Tokens (HS256, stdlib) ----------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(*, user_id: str, role: str, hospital_id: str | None = None) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "role": role,
        "hospital_id": hospital_id,
        "iat": now,
        "exp": now + settings.jwt_expires_minutes * 60,
        "jti": uuid.uuid4().hex,
    }
    signing_input = (
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = hmac.new(
        settings.jwt_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return signing_input + "." + _b64url_encode(signature)


def decode_access_token(token: str) -> dict[str, Any]:
    """Return the verified payload. Raises ValueError on any problem."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("malformed token")
        header_b64, payload_b64, signature_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(
            settings.jwt_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
            raise ValueError("bad signature")
        if json.loads(_b64url_decode(header_b64)).get("alg") != "HS256":
            raise ValueError("unsupported algorithm")
        payload = json.loads(_b64url_decode(payload_b64))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("token expired")
        return payload
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid token: {exc}") from exc
