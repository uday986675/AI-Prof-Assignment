"""Authentication and authorization dependencies.

Guards used by API routers:
- get_current_user: resolves the JWT bearer token to an active User.
- require_roles(...): restricts an endpoint to the given roles.
- require_hospital_admin / require_doctor: tenant-scoped role guards.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..core.security import decode_access_token
from ..database.base import SessionLocal
from ..models import User

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

bearer_scheme = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    if credentials is None:
        raise _CREDENTIALS_ERROR

    token = credentials.credentials

    try:
        payload = decode_access_token(token)
    except ValueError as exc:
        raise _CREDENTIALS_ERROR from exc

    user_id = payload.get("sub")
    if not user_id:
        raise _CREDENTIALS_ERROR

    # Each request gets a short-lived session; dependency-scope only.
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
    finally:
        db.close()

    if user is None or not user.is_active:
        raise _CREDENTIALS_ERROR

    return user


def require_roles(*roles: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return checker


def require_hospital_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "hospital_admin" or not user.hospital_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hospital admin required")
    return user


def require_doctor(user: User = Depends(get_current_user)) -> User:
    if user.role != "doctor" or not user.hospital_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Doctor role required")
    return user


def require_patient(user: User = Depends(get_current_user)) -> User:
    if user.role != "patient":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Patient role required")
    return user


def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "platform_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin required")
    return user
