from __future__ import annotations

import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased
from starlette.middleware.sessions import SessionMiddleware

from images_app.config import PACKAGE_ROOT, settings
from images_app.db import get_db
from images_app.models import Image, SourceTaxon, SyncState, Taxon, TaxonAlias, User, Vote, utcnow
from images_app.security import (
    csrf_token,
    enforce_rate_limit,
    hash_password,
    require_admin,
    require_csrf,
    require_user,
    session_user,
    verify_password,
)
from images_app.importers.common import insert_ignore_rows
from images_app.importers.common import refresh_image_counts
from images_app.importers.mushroom_observer import recompute_mo_image_taxa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("images_app")

TEMPLATE_DIR = PACKAGE_ROOT / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="Dikarya Images", docs_url=None if settings.app_env == "production" else "/docs", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="dikarya_images_session",
    max_age=60 * 60 * 24 * 14,
    same_site="lax",
    https_only=settings.secure_cookies,
)
app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' https://inaturalist-open-data.s3.amazonaws.com https://*.inaturalist.org "
        "https://mushroomobserver.org https://*.mushroomobserver.org",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def context(request: Request, db: Session, **values):
    user = session_user(request, db)
    return {
        "request": request,
        "user": user,
        "csrf": csrf_token(request),
        "base_url": settings.base_url,
        **values,
    }


def taxon_url(taxon: Taxon) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", taxon.scientific_name.lower()).strip("-")[:90]
    return f"/taxon/{taxon.id}/{slug}"


def fungi_filter():
    root = str(settings.fungi_taxon_id)
    return or_(Taxon.inat_taxon_id == settings.fungi_taxon_id, Taxon.ancestry.like(f"%/{root}/%"))


def page_url(taxon: Taxon, page: int) -> str:
    return f"{taxon_url(taxon)}?page={page}"


def render_gallery(db: Session, request: Request, taxon: Taxon, user: User | None, page: int):
    return templates.TemplateResponse(request, "_gallery.html", gallery_context(db, request, taxon, user, page))


def gallery_context(db: Session, request: Request, taxon: Taxon, user: User | None, page: int):
    page_size = 72
    total = db.scalar(select(func.count(Image.id)).where(Image.taxon_id == taxon.id, Image.active.is_(True), Image.display_eligible.is_(True), Image.dna_verified.is_(True))) or 0
    vote = aliased(Vote)
    my_vote = aliased(Vote)
    rank_one = func.coalesce(func.sum(case((vote.rank == 1, 1), else_=0)), 0)
    picked_by = func.count(vote.id)
    average_rank = func.avg(vote.rank)
    my_rank = my_vote.rank.label("my_rank")
    stmt = (
        select(Image, picked_by.label("picked_by"), rank_one.label("rank_one"), average_rank.label("average_rank"), my_rank)
        .outerjoin(vote, and_(vote.image_id == Image.id, vote.taxon_id == taxon.id))
        .outerjoin(my_vote, and_(my_vote.image_id == Image.id, my_vote.taxon_id == taxon.id, my_vote.user_id == (user.id if user else -1)))
        .where(Image.taxon_id == taxon.id, Image.active.is_(True), Image.display_eligible.is_(True), Image.dna_verified.is_(True))
        .group_by(Image.id, my_vote.rank)
        .order_by(
            case((my_vote.rank.is_not(None), 0), else_=1),
            my_vote.rank.asc(),
            rank_one.desc(),
            picked_by.desc(),
            Image.id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    images = db.execute(stmt).all()
    data = context(
        request,
        db,
        taxon=taxon,
        images=images,
        page=page,
        page_count=max(1, (total + page_size - 1) // page_size),
        total_images=total,
        previous_url=page_url(taxon, page - 1) if page > 1 else None,
        next_url=page_url(taxon, page + 1) if page * page_size < total else None,
    )
    return data


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
    except SQLAlchemyError as exc:
        logger.exception("Health check failed: database unavailable")
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    fungi = db.scalars(
        select(Taxon)
        .where(fungi_filter(), Taxon.active.is_(True), Taxon.image_count > 0)
        .order_by(Taxon.image_count.desc(), Taxon.scientific_name.asc())
        .limit(8)
    ).all()
    return templates.TemplateResponse(request, "home.html", context(request, db, taxa=fungi))


@app.get("/taxa", response_class=HTMLResponse)
def directory(
    request: Request,
    q: str = "",
    letter: str = "",
    rank: str = "species",
    page: int = 1,
    db: Session = Depends(get_db),
):
    page = max(1, page)
    rank = rank.strip().lower()[:60] or "all"
    available_ranks = db.scalars(
        select(Taxon.rank)
        .where(
            fungi_filter(),
            Taxon.active.is_(True),
            Taxon.image_count > 0,
            Taxon.rank.is_not(None),
        )
        .distinct()
        .order_by(Taxon.rank)
    ).all()
    if rank != "all" and rank not in available_ranks:
        rank = "species" if "species" in available_ranks else "all"
    query = select(Taxon).where(fungi_filter(), Taxon.active.is_(True), Taxon.image_count > 0)
    if rank != "all":
        query = query.where(func.lower(Taxon.rank) == rank)
    normalized = q.strip()[:120]
    if normalized:
        exact_alias = select(TaxonAlias.taxon_id).where(func.lower(TaxonAlias.name) == normalized.lower())
        query = query.where(
            or_(Taxon.scientific_name.ilike(normalized), Taxon.scientific_name.ilike(f"%{normalized}%"), Taxon.id.in_(exact_alias), Taxon.id.in_(select(TaxonAlias.taxon_id).where(TaxonAlias.name.ilike(f"%{normalized}%"))))
        )
        query = query.order_by(
            case((func.lower(Taxon.scientific_name) == normalized.lower(), 0), (Taxon.id.in_(exact_alias), 1), else_=2),
            Taxon.scientific_name.asc(),
        )
    else:
        if letter and re.fullmatch(r"[A-Za-z]", letter):
            query = query.where(Taxon.scientific_name.ilike(f"{letter.upper()}%"))
            letter = letter.upper()
        query = query.order_by(Taxon.scientific_name.asc())
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    taxa = db.scalars(query.offset((page - 1) * 50).limit(50)).all()
    return templates.TemplateResponse(
        request,
        "directory.html",
        context(
            request,
            db,
            taxa=taxa,
            q=normalized,
            letter=letter,
            rank=rank,
            available_ranks=available_ranks,
            page=page,
            page_count=max(1, (total + 49) // 50),
            total=total,
        ),
    )


@app.get("/taxon/{taxon_id}/{slug}", response_class=HTMLResponse)
@app.get("/taxon/{taxon_id}", response_class=HTMLResponse)
def taxon_page(request: Request, taxon_id: int, slug: str = "", page: int = 1, db: Session = Depends(get_db)):
    taxon = db.get(Taxon, taxon_id)
    if not taxon or not taxon.active:
        raise HTTPException(404, "Taxon not found")
    if slug == "gallery":
        return render_gallery(db, request, taxon, session_user(request, db), max(1, page))
    canonical = taxon_url(taxon)
    if request.url.path != canonical:
        return RedirectResponse(f"{canonical}?page={max(1, page)}" if page > 1 else canonical, status_code=308)
    page = max(1, page)
    ancestors: list[Taxon] = []
    for token in (taxon.ancestry or "").split("/"):
        if token.isdigit():
            item = db.scalar(select(Taxon).where(Taxon.inat_taxon_id == int(token)))
            if item:
                ancestors.append(item)
    if taxon not in ancestors:
        ancestors.append(taxon)
    voted_users = db.scalar(select(func.count(func.distinct(Vote.user_id))).where(Vote.taxon_id == taxon.id)) or 0
    total_images = db.scalar(select(func.count(Image.id)).where(Image.taxon_id == taxon.id, Image.active.is_(True), Image.display_eligible.is_(True), Image.dna_verified.is_(True))) or 0
    page = max(1, page)
    data = gallery_context(db, request, taxon, session_user(request, db), page)
    data.update({"ancestors": ancestors, "voted_users": voted_users, "total_images": total_images})
    return templates.TemplateResponse(
        request,
        "taxon.html",
        data,
    )


@app.get("/taxon/{taxon_id}/gallery", response_class=HTMLResponse)
def gallery(request: Request, taxon_id: int, page: int = 1, db: Session = Depends(get_db)):
    taxon = db.get(Taxon, taxon_id)
    if not taxon or not taxon.active:
        raise HTTPException(404, "Taxon not found")
    return render_gallery(db, request, taxon, session_user(request, db), max(1, page))


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "auth.html", context(request, db, mode="register", error=None))


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    name: str = Form(max_length=120),
    email: str = Form(max_length=320),
    password: str = Form(min_length=12, max_length=256),
    csrf: str = Form(),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(db, "register", ip, maximum=5, seconds=3600)
    cleaned_name = name.strip()
    cleaned_email = email.strip().lower()
    if not cleaned_name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cleaned_email):
        db.rollback()
        return templates.TemplateResponse(request, "auth.html", context(request, db, mode="register", error="Enter a name and valid email."), status_code=422)
    db.add(User(name=cleaned_name, email=cleaned_email, password_hash=hash_password(password)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(request, "auth.html", context(request, db, mode="register", error="An account already uses that email."), status_code=409)
    request.session.clear()
    request.session["user_id"] = db.scalar(select(User.id).where(User.email == cleaned_email))
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "auth.html", context(request, db, mode="login", error=None))


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(max_length=320),
    password: str = Form(max_length=256),
    csrf: str = Form(),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(db, "login_ip", ip, maximum=30, seconds=900)
    enforce_rate_limit(db, "login", f"{ip}:{email.strip().lower()}", maximum=10, seconds=900)
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower(), User.is_active.is_(True)))
    valid = bool(user and verify_password(user.password_hash, password))
    db.commit()
    if not valid:
        return templates.TemplateResponse(request, "auth.html", context(request, db, mode="login", error="Email or password did not match."), status_code=401)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form(), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/taxon/{taxon_id}/image/{image_id}/toggle", response_class=HTMLResponse)
def toggle_vote(
    request: Request,
    taxon_id: int,
    image_id: int,
    page: int = Form(default=1),
    csrf: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf)
    user = require_user(request, db)
    taxon = db.get(Taxon, taxon_id)
    image = db.get(Image, image_id)
    if not taxon or not image or image.taxon_id != taxon.id:
        raise HTTPException(404, "Image not found for this taxon")
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    existing = db.scalar(select(Vote).where(Vote.user_id == user.id, Vote.taxon_id == taxon.id, Vote.image_id == image.id))
    votes = list(db.scalars(select(Vote).where(Vote.user_id == user.id, Vote.taxon_id == taxon.id).order_by(Vote.rank)).all())
    if existing:
        db.delete(existing)
        db.flush()
        votes = [v for v in votes if v.id != existing.id]
        _persist_vote_order(db, votes)
    else:
        if not image.active or not image.display_eligible or not image.dna_verified:
            raise HTTPException(409, "This image is no longer available for ranking")
        db.add(Vote(user_id=user.id, taxon_id=taxon.id, image_id=image.id, rank=len(votes) + 1))
    db.commit()
    page = max(1, page)
    if request.headers.get("HX-Request") == "true":
        return render_gallery(db, request, taxon, user, page)
    destination = taxon_url(taxon) + (f"?page={page}" if page > 1 else "")
    return RedirectResponse(destination, status_code=303)


@app.post("/taxon/{taxon_id}/move", response_class=HTMLResponse)
def move_vote(
    request: Request,
    taxon_id: int,
    image_id: int = Form(),
    direction: str = Form(pattern="^(up|down)$"),
    page: int = Form(default=1),
    csrf: str = Form(),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf)
    user = require_user(request, db)
    taxon = db.get(Taxon, taxon_id)
    if not taxon:
        raise HTTPException(404, "Taxon not found")
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    votes = list(db.scalars(select(Vote).where(Vote.user_id == user.id, Vote.taxon_id == taxon.id).order_by(Vote.rank)).all())
    index = next((i for i, row in enumerate(votes) if row.image_id == image_id), None)
    if index is not None:
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(votes):
            votes[index], votes[target] = votes[target], votes[index]
            _persist_vote_order(db, votes)
    db.commit()
    page = max(1, page)
    if request.headers.get("HX-Request") == "true":
        return render_gallery(db, request, taxon, user, page)
    destination = taxon_url(taxon) + (f"?page={page}" if page > 1 else "")
    return RedirectResponse(destination, status_code=303)


@app.post("/taxon/{taxon_id}/reorder", response_class=HTMLResponse)
def reorder_votes(
    request: Request,
    taxon_id: int,
    image_ids: list[int] = Form(),
    page: int = Form(default=1),
    csrf: str = Form(),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf)
    user = require_user(request, db)
    taxon = db.get(Taxon, taxon_id)
    if not taxon:
        raise HTTPException(404, "Taxon not found")
    if not image_ids or len(image_ids) > 72 or len(image_ids) != len(set(image_ids)):
        raise HTTPException(422, "Provide the selected image order from one gallery page.")

    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    votes = list(
        db.scalars(
            select(Vote)
            .where(Vote.user_id == user.id, Vote.taxon_id == taxon.id)
            .order_by(Vote.rank)
        ).all()
    )
    votes_by_image = {vote.image_id: vote for vote in votes}
    if any(image_id not in votes_by_image for image_id in image_ids):
        raise HTTPException(422, "The order includes an image you have not selected.")
    ordered_set = set(image_ids)
    slots = [index for index, vote in enumerate(votes) if vote.image_id in ordered_set]
    for index, image_id in zip(slots, image_ids, strict=True):
        votes[index] = votes_by_image[image_id]
    _persist_vote_order(db, votes)
    db.commit()
    page = max(1, page)
    if request.headers.get("HX-Request") == "true":
        return render_gallery(db, request, taxon, user, page)
    return RedirectResponse(taxon_url(taxon) + (f"?page={page}" if page > 1 else ""), status_code=303)


def _persist_vote_order(db: Session, votes: list[Vote]) -> None:
    if not votes:
        return
    user_id, taxon_id = votes[0].user_id, votes[0].taxon_id
    db.execute(
        update(Vote)
        .where(Vote.user_id == user_id, Vote.taxon_id == taxon_id)
        .values(rank=Vote.rank + 1_000_000, updated_at=utcnow())
    )
    db.flush()
    for rank, row in enumerate(votes, start=1):
        row.rank = rank
        row.updated_at = utcnow()
    db.flush()


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, page: int = 1, db: Session = Depends(get_db)):
    require_admin(request, db)
    counts = {
        "taxa": db.scalar(select(func.count(Taxon.id))) or 0,
        "images": db.scalar(select(func.count(Image.id))) or 0,
        "users": db.scalar(select(func.count(User.id))) or 0,
        "votes": db.scalar(select(func.count(Vote.id))) or 0,
    }
    states = db.scalars(select(SyncState).order_by(SyncState.source)).all()
    unresolved_query = select(SourceTaxon).where(
        SourceTaxon.source == "mushroom_observer", SourceTaxon.taxon_id.is_(None)
    )
    unresolved_total = db.scalar(select(func.count()).select_from(unresolved_query.subquery())) or 0
    page = max(1, min(page, max(1, (unresolved_total + 99) // 100)))
    unresolved = db.scalars(
        unresolved_query.order_by(SourceTaxon.original_name, SourceTaxon.id).offset((page - 1) * 100).limit(100)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin.html",
        context(
            request,
            db,
            counts=counts,
            states=states,
            unresolved=unresolved,
            unresolved_total=unresolved_total,
            page=page,
            page_count=max(1, (unresolved_total + 99) // 100),
        ),
    )


@app.post("/admin/source-taxon/{source_taxon_id}/map", response_class=HTMLResponse)
def map_source_taxon(
    request: Request,
    source_taxon_id: int,
    taxon_id: int = Form(),
    csrf: str = Form(),
    db: Session = Depends(get_db),
):
    require_csrf(request, csrf)
    require_admin(request, db)
    source_taxon = db.get(SourceTaxon, source_taxon_id)
    taxon = db.get(Taxon, taxon_id)
    if not source_taxon or not taxon or source_taxon.source != "mushroom_observer":
        raise HTTPException(404, "Mapping record or taxon not found")
    if not taxon.active or not (taxon.inat_taxon_id == settings.fungi_taxon_id or f"/{settings.fungi_taxon_id}/" in (taxon.ancestry or "")):
        raise HTTPException(422, "Choose an active taxon in the current Fungi directory.")
    source_taxon.taxon_id = taxon.id
    source_taxon.mapping_status = "mapped"
    source_taxon.mapping_method = "manual"
    insert_ignore_rows(
        db,
        TaxonAlias,
        [{"taxon_id": taxon.id, "name": source_taxon.original_name, "source": "Mushroom Observer"}],
        ["taxon_id", "name", "source"],
    )
    changed_taxa = recompute_mo_image_taxa(db, source_taxon_id=source_taxon.id)
    refresh_image_counts(db, changed_taxa)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return Response("User-agent: *\nAllow: /\n", media_type="text/plain")
