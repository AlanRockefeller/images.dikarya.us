from __future__ import annotations

import logging
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from images_app import logs
from images_app.config import settings
from images_app.db import get_db
from images_app.models import Image, Taxon, User, visible_votes
from images_app.security import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    create_session,
    end_session,
    form_stamp,
    form_stamp_age,
    hash_password,
    rate_limited,
    verify_password,
)
from images_app.web import check_fields, current_user, render, require_user, safe_next, verify_csrf

router = APIRouter()

EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
MIN_PASSWORD = 10


# A person needs at least this long to fill in the sign-up form; bots are faster.
MIN_FORM_SECONDS = 2.0
MAX_FORM_AGE = 24 * 3600


def _logged_in(db: Session, user: User, destination: str) -> RedirectResponse:
    token = create_session(db, user)
    db.commit()
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=settings.production,
        path="/", max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    return response


@router.get("/login")
def login_form(request: Request, next: str = "/", user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse(safe_next(next, request=request), status_code=303)
    return render(request, "login.html", next=safe_next(next, request=request), email="", error=None)


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(max_length=320),
    password: str = Form(max_length=1024),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    destination = safe_next(next, request=request)
    check_fields(request, email=email)
    ip = logs.client_ip(request)
    if rate_limited(db, "login-ip", ip, 30, timedelta(minutes=15)) or rate_limited(
        db, "login-email", email, 10, timedelta(minutes=15)
    ):
        # Repeated failures are password guessing or credential stuffing.
        logs.event(logs.attack, "login.rate_limited", request, logging.WARNING, email=email)
        return render(request, "login.html", 429, next=destination, email=email,
                      error="Too many attempts. Please wait a few minutes and try again.")
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if not verify_password(user.password_hash if user else None, password) or not user.is_active:
        reason = "unknown_email" if user is None else ("inactive" if not user.is_active else "wrong_password")
        logs.event(logs.activity, "login.failed", request, logging.WARNING, email=email, reason=reason)
        return render(request, "login.html", 401, next=destination, email=email,
                      error="That email and password don’t match an account.")
    request.state.user = user
    logs.event(logs.activity, "login.ok", request)
    return _logged_in(db, user, destination)


def _register_page(request: Request, status: int = 200, **values):
    values.setdefault("name", "")
    values.setdefault("email", "")
    values.setdefault("error", None)
    return render(request, "register.html", status, form_stamp=form_stamp("register"), **values)


@router.get("/register")
def register_form(request: Request, next: str = "/", user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse(safe_next(next, request=request), status_code=303)
    return _register_page(request, next=safe_next(next, request=request))


@router.post("/register", dependencies=[Depends(verify_csrf)])
def register(
    request: Request,
    name: str = Form(max_length=120),
    email: str = Form(max_length=320),
    password: str = Form(max_length=1024),
    next: str = Form("/"),
    website: str = Form(""),  # honeypot: hidden from people, filled in by bots
    stamp: str = Form(""),
    db: Session = Depends(get_db),
):
    name, email, destination = name.strip(), email.strip().lower(), safe_next(next, request=request)
    check_fields(request, name=name, email=email)

    def again(error: str, status: int = 422):
        return _register_page(request, status, next=destination, name=name, email=email, error=error)

    age = form_stamp_age("register", stamp)
    bot_reason = (
        "honeypot" if website
        else "missing_or_forged_stamp" if age is None
        else "too_fast" if age < MIN_FORM_SECONDS
        else None
    )
    if bot_reason:
        logs.event(logs.scanner, "register.bot", request, reason=bot_reason, name=name, email=email,
                   seconds=round(age, 1) if age is not None else None, honeypot=website or None)
        return again("Something went wrong. Please check the form and submit it again.", 400)
    if age > MAX_FORM_AGE:
        logs.event(logs.activity, "register.stale_form", request, email=email)
        return again("This form was open for a long time. Please submit it again.", 400)
    if rate_limited(db, "register-ip", logs.client_ip(request), 10, timedelta(hours=1)):
        logs.event(logs.attack, "register.rate_limited", request, logging.WARNING, email=email)
        return again("Too many new accounts from this network. Please try again later.", 429)
    problem = (
        "Please enter your name." if not name
        else "Please enter a valid email address." if not EMAIL.fullmatch(email)
        else f"Please choose a password of at least {MIN_PASSWORD} characters." if len(password) < MIN_PASSWORD
        else None
    )
    if problem:
        logs.event(logs.activity, "register.invalid", request, email=email, problem=problem)
        return again(problem)
    user = User(name=name, email=email, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        logs.event(logs.activity, "register.duplicate_email", request, email=email)
        return again("An account with that email already exists. Try logging in.", 409)
    request.state.user = user
    logs.event(logs.activity, "register.ok", request, name=name, seconds=round(age, 1))
    return _logged_in(db, user, destination)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    end_session(db, request.cookies.get(SESSION_COOKIE))
    logs.event(logs.activity, "logout", request)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.production, httponly=True)
    return response


@router.get("/me")
def my_rankings(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    """Every taxon the user has ranked, with their top picks."""
    votes = visible_votes.c
    rows = db.execute(
        select(Taxon, Image, votes.rank, votes.updated_at)
        .join(visible_votes, votes.taxon_id == Taxon.id)
        .join(Image, Image.id == votes.image_id)
        .where(votes.user_id == user.id)
        .order_by(Taxon.name, votes.rank)
    ).all()
    rankings: dict[int, dict] = {}
    for taxon, image, rank, updated_at in rows:
        entry = rankings.setdefault(taxon.id, {"taxon": taxon, "images": [], "count": 0, "updated": updated_at})
        entry["count"] += 1
        entry["updated"] = max(entry["updated"], updated_at)
        if rank <= 5:
            entry["images"].append(image)
    ordered = sorted(rankings.values(), key=lambda entry: entry["updated"], reverse=True)
    return render(request, "me.html", rankings=ordered)
