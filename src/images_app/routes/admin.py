from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from images_app import logs
from images_app.db import get_db
from images_app.models import Image, SourceName, SyncRun, SyncState, Taxon, User, Vote
from images_app.sync.maintenance import refresh_derived_data
from images_app.sync.mushroom_observer import set_manual_mapping
from images_app.web import render, require_admin, verify_csrf

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
PER_PAGE = 50


@router.get("")
def dashboard(request: Request, db: Session = Depends(get_db)):
    counts = {
        "Taxa with photos": db.scalar(select(func.count()).select_from(Taxon).where(Taxon.image_count > 0)),
        "Photos shown": db.scalar(select(func.count()).select_from(Image).where(Image.is_active)),
        "Photos hidden": db.scalar(select(func.count()).select_from(Image).where(~Image.is_active)),
        "Users": db.scalar(select(func.count()).select_from(User)),
        "Votes": db.scalar(select(func.count()).select_from(Vote)),
    }
    hidden = db.execute(
        select(Image.source, Image.inactive_reason, func.count())
        .where(~Image.is_active)
        .group_by(Image.source, Image.inactive_reason)
        .order_by(Image.source, func.count().desc())
    ).all()
    name_status = dict(
        db.execute(
            select(SourceName.status, func.count()).group_by(SourceName.status)
        ).all()
    )
    return render(
        request,
        "admin/dashboard.html",
        counts=counts,
        hidden=hidden,
        name_status=name_status,
        states=db.scalars(select(SyncState).order_by(SyncState.job)).all(),
        runs=db.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(25)).all(),
    )


@router.get("/names")
def names(request: Request, status: str = "unresolved", page: int = 1, db: Session = Depends(get_db)):
    status = status if status in ("unresolved", "ambiguous", "mapped") else "unresolved"
    images = (
        select(Image.source_name_id, func.count().label("images"),
               func.count(func.distinct(Image.source_observation_id)).label("observations"))
        .group_by(Image.source_name_id)
        .subquery()
    )
    query = (
        select(SourceName, func.coalesce(images.c.images, 0), func.coalesce(images.c.observations, 0))
        .outerjoin(images, images.c.source_name_id == SourceName.id)
        .where(SourceName.status == status)
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = max(page, 1)
    rows = db.execute(
        query.order_by(func.coalesce(images.c.images, 0).desc(), SourceName.name)
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    ).all()
    candidate_ids = {i for row in rows for i in row[0].candidate_taxon_ids}
    candidates = {
        taxon.id: taxon
        for taxon in db.scalars(select(Taxon).where(Taxon.id.in_(candidate_ids))).all()
    }
    return render(
        request,
        "admin/names.html",
        rows=rows,
        candidates=candidates,
        status=status,
        page=page,
        pages=max(1, -(-total // PER_PAGE)),
        total=total,
    )


@router.post("/names/{source_name_id}/map", dependencies=[Depends(verify_csrf)])
def map_name(
    request: Request,
    source_name_id: int,
    inat_taxon_id: int = Form(),
    status: str = Form("unresolved"),
    db: Session = Depends(get_db),
):
    source_name = db.get(SourceName, source_name_id)
    taxon = db.scalar(
        select(Taxon).where(Taxon.inat_taxon_id == inat_taxon_id, Taxon.is_active)
    )
    if source_name is None:
        raise HTTPException(404, "Unknown source name.")
    if taxon is None:
        raise HTTPException(422, "No active iNaturalist taxon with that ID is loaded.")
    previous = source_name.taxon_id
    set_manual_mapping(db, source_name, taxon)
    db.commit()
    logs.event(logs.activity, "admin.map_name", request, source_name=source_name.name,
               mo_name_id=source_name.source_name_id, taxon=taxon.inat_taxon_id, taxon_name=taxon.name,
               previous_taxon_id=previous)
    refresh_derived_data(db)
    return RedirectResponse(f"/admin/names?status={status}", status_code=303)
