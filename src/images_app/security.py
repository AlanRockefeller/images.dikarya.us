"""Passwords, server-side sessions, CSRF tokens and rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.models import RateLimitHit, User, UserSession

SESSION_LIFETIME = timedelta(days=30)
SESSION_TOUCH_INTERVAL = timedelta(hours=1)
# __Host- cookies cannot be set by sibling subdomains and require HTTPS.
SESSION_COOKIE = "__Host-session" if settings.production else "session"
CSRF_COOKIE = "__Host-csrf" if settings.production else "csrf"

_hasher = PasswordHasher()  # argon2id with library defaults
# A real hash to verify against when the email is unknown, so timing is similar.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token_hash=_token_hash(token), user_id=user.id, expires_at=_now() + SESSION_LIFETIME))
    db.execute(delete(UserSession).where(UserSession.expires_at < _now()))
    return token


def load_session_user(db: Session, token: str | None) -> User | None:
    if not token or len(token) > 100:
        return None
    session = db.get(UserSession, _token_hash(token))
    now = _now()
    if session is None or session.expires_at < now or not session.user.is_active:
        return None
    if now - session.last_seen_at > SESSION_TOUCH_INTERVAL:
        session.last_seen_at = now
        session.expires_at = now + SESSION_LIFETIME
        db.commit()
    return session.user


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        db.commit()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_token: str | None, submitted: str | None) -> bool:
    return bool(cookie_token and submitted) and hmac.compare_digest(cookie_token, submitted)


def rate_limited(db: Session, bucket: str, key: str, limit: int, window: timedelta) -> bool:
    """Record an attempt and report whether the caller exceeded `limit` within `window`."""
    key_hash = hmac.new(settings.secret_key.encode(), key.encode(), hashlib.sha256).digest()
    now = _now()
    db.execute(delete(RateLimitHit).where(RateLimitHit.occurred_at < now - timedelta(days=1)))
    recent = db.scalar(
        select(func.count())
        .select_from(RateLimitHit)
        .where(
            RateLimitHit.bucket == bucket,
            RateLimitHit.key_hash == key_hash,
            RateLimitHit.occurred_at > now - window,
        )
    )
    db.add(RateLimitHit(bucket=bucket, key_hash=key_hash, occurred_at=now))
    db.commit()
    return recent >= limit
