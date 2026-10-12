from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from images_app import catalog, gallery, logs, ranking
from images_app.db import get_db
from images_app.models import Image, Taxon, User
from images_app.security import rate_limited
from images_app.templating import taxon_url
from images_app.web import current_user, require_user, verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


class RankingIn(BaseModel):
    image_ids: list[int] = Field(max_length=ranking.MAX_RANKED)


class ClientProblem(BaseModel):
    kind: str = Field(pattern=r"^[a-z_]{1,40}$")
    message: str = Field(max_length=500)
    page: str = Field(default="", max_length=300)


def _taxon(db: Session, inat_taxon_id: int, request: Request) -> Taxon:
    taxon = catalog.get_public_taxon(db, inat_taxon_id)
    if taxon is None:
        logs.event(logs.attack, "ranking.unknown_taxon", request, logging.WARNING, taxon=inat_taxon_id)
        raise HTTPException(404, "Taxon not found.")
    return taxon


def _save(request: Request, db: Session, user: User, taxon: Taxon, requested: list[int], via: str) -> list[int]:
    before = [image.id for image in gallery.user_ranking(db, user, taxon)]
    saved = ranking.set_ranking(db, user, taxon, requested)
    ignored = [image_id for image_id in dict.fromkeys(requested) if image_id not in saved]
    if ignored:
        # Our pages only offer photos shown for this taxon. Photos hidden by a sync
        # while the page was open are harmless; anything else was made up.
        hidden = set(db.scalars(select(Image.id).where(Image.id.in_(ignored), ~Image.is_active)).all())
        forged = [image_id for image_id in ignored if image_id not in hidden]
        if forged:
            logs.event(logs.attack, "ranking.tampering", request, logging.WARNING,
                       taxon=taxon.inat_taxon_id, foreign_image_ids=forged)
        if hidden:
            logs.event(logs.activity, "ranking.stale_photos", request, taxon=taxon.inat_taxon_id,
                       image_ids=sorted(hidden))
    added = [image_id for image_id in saved if image_id not in before]
    removed = [image_id for image_id in before if image_id not in saved]
    kept_before = [image_id for image_id in before if image_id in saved]
    kept_after = [image_id for image_id in saved if image_id in before]
    logs.event(
        logs.activity, "ranking.save", request, via=via, taxon=taxon.inat_taxon_id, name=taxon.name,
        ranking=saved or None, added=added or None, removed=removed or None,
        reordered=kept_before != kept_after,
    )
    return saved


@router.put("/api/taxa/{inat_taxon_id}/ranking")
def save_ranking(
    request: Request,
    inat_taxon_id: int,
    body: RankingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Replace the signed-in user's ranking for a taxon. Returns the saved order."""
    taxon = _taxon(db, inat_taxon_id, request)
    return {"image_ids": _save(request, db, user, taxon, body.image_ids, via="api")}


@router.post("/taxon/{inat_taxon_id}/toggle")
def toggle_form(
    request: Request,
    inat_taxon_id: int,
    image_id: int = Form(),
    back: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Fallback for browsers without JavaScript: add or remove one image, then return."""
    taxon = _taxon(db, inat_taxon_id, request)
    current = [image.id for image in gallery.user_ranking(db, user, taxon)]
    order = [i for i in current if i != image_id] if image_id in current else [*current, image_id]
    _save(request, db, user, taxon, order, via="form")
    if back and not back.startswith(taxon_url(taxon)):
        logs.event(logs.attack, "open_redirect.attempt", request, logging.WARNING, target=back)
    destination = back if back.startswith(taxon_url(taxon)) else taxon_url(taxon)
    return RedirectResponse(f"{destination}#photo-{image_id}", status_code=303)


@router.post("/api/client-problem", status_code=204)
def client_problem(
    request: Request,
    problem: ClientProblem,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    """Problems seen in the browser (failed saves, script errors), for the activity log."""
    if rate_limited(db, "client-problem", logs.client_ip(request), 30, timedelta(hours=1)):
        return None
    logs.event(logs.activity, f"browser.{problem.kind}", request, logging.WARNING,
               message=problem.message, page=problem.page or None)
    return None
