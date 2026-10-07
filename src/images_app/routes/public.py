from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from images_app import catalog, gallery
from images_app.db import get_db
from images_app.models import Taxon, User, Vote
from images_app.templating import plain_name, taxon_url
from images_app.web import current_user, render

router = APIRouter()


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    stats = db.execute(
        select(
            select(func.coalesce(func.sum(Taxon.image_count), 0)).where(Taxon.is_active).scalar_subquery(),
            select(func.count()).where(Taxon.is_active, Taxon.image_count > 0).scalar_subquery(),
            select(func.count(func.distinct(Vote.user_id))).scalar_subquery(),
        )
    ).one()
    return render(
        request,
        "home.html",
        stats={"photos": stats[0], "taxa": stats[1], "rankers": stats[2]},
        recent=catalog.recently_ranked(db),
        unranked=catalog.needs_ranking(db),
        mosaic=catalog.mosaic(db),
        groups=db.scalars(
            catalog.public_taxa()
            .where(Taxon.rank == "order")
            .order_by(Taxon.subtree_image_count.desc())
            .limit(12)
        ).all(),
    )


@router.get("/taxa")
def directory(
    request: Request,
    rank: str = "species",
    letter: str = "",
    sort: str = "name",
    within: int | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    rank = rank if rank in catalog.DIRECTORY_RANKS else "species"
    sort = sort if sort in catalog.DIRECTORY_SORTS else "name"
    letter = letter.upper() if re.fullmatch(r"[A-Za-z]", letter) else ""
    within_taxon = catalog.get_public_taxon(db, within) if within else None
    listing = catalog.directory(db, rank=rank, letter=letter, sort=sort, within=within_taxon, page=page)
    return render(
        request,
        "directory.html",
        listing=listing,
        rank=rank,
        letter=letter,
        sort=sort,
        within=within_taxon,
        ranks=catalog.DIRECTORY_RANKS,
        sorts=catalog.DIRECTORY_SORTS,
    )


@router.get("/search")
def search_page(request: Request, q: str = "", db: Session = Depends(get_db),
                user: User | None = Depends(current_user)):
    results = catalog.search(db, q, limit=40)
    exact = [taxon for taxon, alias in results if alias is None and taxon.name.lower() == q.strip().lower()]
    if len(exact) == 1:
        return RedirectResponse(taxon_url(exact[0]), status_code=303)
    return render(request, "search.html", q=q.strip(), results=results)


@router.get("/api/search")
def search_api(q: str = "", scope: str = "public", db: Session = Depends(get_db)):
    """Typeahead suggestions. scope=all also finds taxa without photos (for admin mapping)."""
    results = []
    for taxon, alias in catalog.search(db, q, limit=8, public_only=scope != "all"):
        cover = taxon.cover_image
        results.append(
            {
                "id": taxon.inat_taxon_id,
                "name": plain_name(taxon),
                "italic": taxon.rank_level <= 20,
                "rank": taxon.rank,
                "common_name": taxon.common_name,
                "matched": alias,
                "photos": taxon.subtree_image_count,
                "thumb": cover.thumb_url if cover else None,
                "url": taxon_url(taxon),
            }
        )
    return JSONResponse({"results": results}, headers={"Cache-Control": "public, max-age=300"})


def _load_taxon(db: Session, inat_taxon_id: int) -> Taxon:
    taxon = catalog.get_public_taxon(db, inat_taxon_id)
    if taxon is None:
        raise HTTPException(404, "We have no photos of that taxon yet.")
    return taxon


def _gallery_options(request: Request, sort: str, source: str) -> tuple[str, str, str]:
    sort = sort if sort in gallery.SORTS else "faves"
    source = source if source in gallery.SOURCES else "all"
    # A per-browser seed keeps "random" order stable while paging.
    seed = hashlib.sha256(request.state.csrf_token.encode()).hexdigest()[:12]
    return sort, source, seed


@router.get("/taxon/{inat_taxon_id}")
def taxon_redirect(inat_taxon_id: int, db: Session = Depends(get_db)):
    return RedirectResponse(taxon_url(_load_taxon(db, inat_taxon_id)), status_code=301)


@router.get("/taxon/{inat_taxon_id}/{slug}")
def taxon_page(
    request: Request,
    inat_taxon_id: int,
    slug: str,
    sort: str = "faves",
    source: str = "all",
    only: bool = False,
    page: int = 1,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    taxon = _load_taxon(db, inat_taxon_id)
    if slug != taxon_url(taxon).rsplit("/", 1)[-1]:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(taxon_url(taxon) + query, status_code=301)
    sort, source, seed = _gallery_options(request, sort, source)
    kids, kids_total = catalog.children(db, taxon, limit=24)
    ranked = gallery.user_ranking(db, user, taxon)
    return render(
        request,
        "taxon.html",
        taxon=taxon,
        lineage=catalog.ancestors(db, taxon),
        children=kids,
        children_total=kids_total,
        rankers=gallery.ranker_count(db, taxon),
        listing=gallery.gallery(db, taxon, sort=sort, source=source, exact=only, page=page, seed=seed),
        sort=sort,
        source=source,
        only=only,
        subtaxa_included=gallery.includes_subtaxa(taxon) and kids_total > 0,
        sorts=gallery.SORTS,
        sources=gallery.SOURCES,
        ranked=ranked,
        ranks={image.id: index for index, image in enumerate(ranked, start=1)},
    )


@router.get("/taxon/{inat_taxon_id}/{slug}/photos")
def gallery_fragment(
    request: Request,
    inat_taxon_id: int,
    slug: str,
    sort: str = "faves",
    source: str = "all",
    only: bool = False,
    page: int = 2,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    """The next page of gallery tiles, appended by the browser while scrolling."""
    taxon = _load_taxon(db, inat_taxon_id)
    sort, source, seed = _gallery_options(request, sort, source)
    ranked = gallery.user_ranking(db, user, taxon)
    return render(
        request,
        "partials/tiles.html",
        taxon=taxon,
        listing=gallery.gallery(db, taxon, sort=sort, source=source, exact=only, page=page, seed=seed),
        sort=sort,
        source=source,
        only=only,
        ranks={image.id: index for index, image in enumerate(ranked, start=1)},
    )


@router.get("/taxon/{inat_taxon_id}/{slug}/results")
def results_page(
    request: Request,
    inat_taxon_id: int,
    slug: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
):
    taxon = _load_taxon(db, inat_taxon_id)
    return render(
        request,
        "results.html",
        taxon=taxon,
        lineage=catalog.ancestors(db, taxon),
        rankers=gallery.ranker_count(db, taxon),
        results=gallery.community_results(db, taxon),
    )


@router.get("/about")
def about(request: Request, user: User | None = Depends(current_user)):
    return render(request, "about.html")


@router.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(
        "User-agent: *\nDisallow: /api/\nDisallow: /admin\nDisallow: /taxon/*/photos\n"
    )
