from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from images_app import catalog, gallery, ranking
from images_app.db import get_db
from images_app.models import Taxon, User
from images_app.templating import taxon_url
from images_app.web import require_user, verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


class RankingIn(BaseModel):
    image_ids: list[int] = Field(max_length=ranking.MAX_RANKED)


def _taxon(db: Session, inat_taxon_id: int) -> Taxon:
    taxon = catalog.get_public_taxon(db, inat_taxon_id)
    if taxon is None:
        raise HTTPException(404, "Taxon not found.")
    return taxon


@router.put("/api/taxa/{inat_taxon_id}/ranking")
def save_ranking(
    inat_taxon_id: int,
    body: RankingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Replace the signed-in user's ranking for a taxon. Returns the saved order."""
    saved = ranking.set_ranking(db, user, _taxon(db, inat_taxon_id), body.image_ids)
    return {"image_ids": saved}


@router.post("/taxon/{inat_taxon_id}/toggle")
def toggle_form(
    inat_taxon_id: int,
    image_id: int = Form(),
    back: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Fallback for browsers without JavaScript: add or remove one image, then return."""
    taxon = _taxon(db, inat_taxon_id)
    current = [image.id for image in gallery.user_ranking(db, user, taxon)]
    ranking.toggle(db, user, taxon, image_id, current)
    destination = back if back.startswith(taxon_url(taxon)) else taxon_url(taxon)
    return RedirectResponse(f"{destination}#photo-{image_id}", status_code=303)
