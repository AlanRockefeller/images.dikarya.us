from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.db import get_db
from images_app.models import Image, Taxon, User, visible_votes
from images_app.security import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    create_session,
    end_session,
    hash_password,
    rate_limited,
    verify_password,
)
from images_app.web import current_user, render, require_user, safe_next, verify_csrf

router = APIRouter()

EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
MIN_PASSWORD = 10


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", next=safe_next(next), email="", error=None)


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(max_length=320),
    password: str = Form(max_length=1024),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    destination = safe_next(next)
    if rate_limited(db, "login-ip", _client_ip(request), 30, timedelta(minutes=15)) or rate_limited(
        db, "login-email", email, 10, timedelta(minutes=15)
    ):
        return render(request, "login.html", 429, next=destination, email=email,
                      error="Too many attempts. Please wait a few minutes and try again.")
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if not verify_password(user.password_hash if user else None, password) or not user.is_active:
        return render(request, "login.html", 401, next=destination, email=email,
                      error="That email and password don’t match an account.")
    return _logged_in(db, user, destination)


@router.get("/register")
def register_form(request: Request, next: str = "/", user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "register.html", next=safe_next(next), name="", email="", error=None)


@router.post("/register", dependencies=[Depends(verify_csrf)])
def register(
    request: Request,
    name: str = Form(max_length=120),
    email: str = Form(max_length=320),
    password: str = Form(max_length=1024),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    name, email, destination = name.strip(), email.strip().lower(), safe_next(next)

    def again(error: str, status: int = 422):
        return render(request, "register.html", status, next=destination, name=name, email=email, error=error)

    if rate_limited(db, "register-ip", _client_ip(request), 10, timedelta(hours=1)):
        return again("Too many new accounts from this network. Please try again later.", 429)
    if not name:
        return again("Please enter your name.")
    if not EMAIL.fullmatch(email):
        return again("Please enter a valid email address.")
    if len(password) < MIN_PASSWORD:
        return again(f"Please choose a password of at least {MIN_PASSWORD} characters.")
    user = User(name=name, email=email, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return again("An account with that email already exists. Try logging in.", 409)
    return _logged_in(db, user, destination)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    end_session(db, request.cookies.get(SESSION_COOKIE))
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
