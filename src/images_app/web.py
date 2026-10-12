"""Request helpers shared by the route modules."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from images_app import logs, threats
from images_app.config import settings
from images_app.db import get_db
from images_app.models import User
from images_app.security import CSRF_COOKIE, SESSION_COOKIE, csrf_matches, load_session_user
from images_app.templating import templates

# Session tokens are secrets.token_urlsafe(32): 43 URL-safe characters.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not hasattr(request.state, "user"):
        token = request.cookies.get(SESSION_COOKIE)
        request.state.user = load_session_user(db, token)
        if token and request.state.user is None:
            if _TOKEN_SHAPE.fullmatch(token):
                logs.event(logs.activity, "session.expired", request)
            else:
                # We only ever issue well-formed tokens; anything else was made up.
                logs.event(logs.attack, "session.tampered_cookie", request, logging.WARNING, cookie=token)
    return request.state.user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(401, "Please log in first.")
    return user


def require_admin(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        logs.event(logs.scanner, "admin.probe_anonymous", request)
        raise HTTPException(401, "Please log in first.")
    if not user.is_admin:
        logs.event(logs.attack, "admin.probe_by_member", request, logging.WARNING)
        raise HTTPException(403, "Administrators only.")
    return user


async def verify_csrf(request: Request) -> None:
    """Reject cross-site state changes.

    Browsers send Origin (and Sec-Fetch-Site) on POST/PUT; a foreign origin is
    refused outright. A token must also match the per-browser CSRF cookie
    (double-submit), supplied as a form field or X-CSRF-Token header.
    """
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    foreign_origin = bool(origin and origin != "null"
                          and urlsplit(origin).netloc not in (request.url.netloc, settings.canonical_host))
    if foreign_origin or fetch_site in ("cross-site", "same-site"):
        # Another website made this visitor's browser submit to us: a forgery attempt.
        logs.event(logs.attack, "csrf.cross_site", request, logging.WARNING,
                   origin=origin, fetch_site=fetch_site, referer=request.headers.get("referer"))
        raise HTTPException(403, "Cross-site request refused.")
    cookie = request.cookies.get(CSRF_COOKIE)
    submitted = request.headers.get("x-csrf-token")
    if submitted is None and "form" in request.headers.get("content-type", ""):
        submitted = (await request.form()).get("csrf_token")
    if not csrf_matches(cookie, submitted):
        if cookie is None and submitted is None:
            # Never loaded a page from us: a bot posting blindly to a form URL.
            logs.event(logs.scanner, "csrf.blind_post", request)
        else:
            logs.event(logs.activity, "csrf.expired_form", request, logging.WARNING,
                       had_cookie=cookie is not None, had_token=submitted is not None)
        raise HTTPException(403, "Your session expired. Reload the page and try again.")


def check_fields(request: Request, **fields: str | None) -> None:
    """Log attack payloads submitted in form fields (the values are still handled safely)."""
    for name, value in fields.items():
        found = threats.find_payload(value)
        if found:
            logs.event(logs.attack, f"payload.{found[0]}", request, logging.WARNING,
                       field=name, sample=found[1])
            return


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("user", getattr(request.state, "user", None))
    context["csrf_token"] = request.state.csrf_token
    context.setdefault("request_id", getattr(request.state, "request_id", None))
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def safe_next(target: str | None, default: str = "/", request: Request | None = None) -> str:
    """Only allow redirects to local paths."""
    if not target or (target.startswith("/") and not target.startswith("//") and "\\" not in target):
        return target or default
    if request is not None:
        logs.event(logs.attack, "open_redirect.attempt", request, logging.WARNING, target=target)
    return default
