"""Request helpers shared by the route modules."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.db import get_db
from images_app.models import User
from images_app.security import CSRF_COOKIE, SESSION_COOKIE, csrf_matches, load_session_user
from images_app.templating import templates


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not hasattr(request.state, "user"):
        request.state.user = load_session_user(db, request.cookies.get(SESSION_COOKIE))
    return request.state.user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(401, "Please log in first.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Administrators only.")
    return user


async def verify_csrf(request: Request) -> None:
    """Reject cross-site state changes.

    Browsers send Origin (and Sec-Fetch-Site) on POST/PUT; a foreign origin is
    refused outright. A token must also match the per-browser CSRF cookie
    (double-submit), supplied as a form field or X-CSRF-Token header.
    """
    origin = request.headers.get("origin")
    if origin and origin != "null":
        parts = urlsplit(origin)
        if parts.netloc not in (request.url.netloc, settings.canonical_host):
            raise HTTPException(403, "Cross-site request refused.")
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        raise HTTPException(403, "Cross-site request refused.")
    submitted = request.headers.get("x-csrf-token")
    if submitted is None and "form" in request.headers.get("content-type", ""):
        submitted = (await request.form()).get("csrf_token")
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), submitted):
        raise HTTPException(403, "Your session expired. Reload the page and try again.")


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("user", getattr(request.state, "user", None))
    context["csrf_token"] = request.state.csrf_token
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow redirects to local paths."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default
