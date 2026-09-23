from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from images_app.models import RateLimitEvent, User, utcnow

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def session_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    user = db.get(User, user_id)
    return user if user and user.is_active else None


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf_token")
    provided = submitted or request.headers.get("x-csrftoken")
    if not isinstance(expected, str) or not isinstance(provided, str) or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="The form expired. Reload the page and try again.")


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def enforce_rate_limit(db: Session, scope: str, key: str, maximum: int, seconds: int) -> None:
    now = utcnow()
    floor = now - timedelta(seconds=seconds)
    hashed = _key_hash(key)
    db.execute(delete(RateLimitEvent).where(RateLimitEvent.occurred_at < now - timedelta(days=1)))
    count = db.scalar(
        select(func.count(RateLimitEvent.id)).where(
            RateLimitEvent.scope == scope,
            RateLimitEvent.key_hash == hashed,
            RateLimitEvent.occurred_at >= floor,
        )
    ) or 0
    if count >= maximum:
        db.commit()
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")
    db.add(RateLimitEvent(scope=scope, key_hash=hashed, occurred_at=now))
    db.commit()


def require_user(request: Request, db: Session) -> User:
    user = session_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Please log in to rank images.")
    return user


def require_admin(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user
