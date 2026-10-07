"""Finding taxa: search, directory listings and the taxonomic tree."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, case, func, literal, or_, select
from sqlalchemy.orm import Session

from images_app.models import Taxon, TaxonName, Vote

MAJOR_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus", "species")
DIRECTORY_RANKS = {
    "species": ("Species", Taxon.rank_level == 10),
    "infraspecies": ("Varieties & forms", Taxon.rank_level < 10),
    "genus": ("Genera", Taxon.rank == "genus"),
    "family": ("Families", Taxon.rank == "family"),
    "order": ("Orders", Taxon.rank == "order"),
    "all": ("All ranks", literal(True)),
}
DIRECTORY_SORTS = {"name": "A–Z", "photos": "Most photos"}


def public_taxa() -> Select:
    """Active taxa with at least one photo in their subtree.

    The taxonomy import only loads descendants of ROOT_TAXON_IDS and marks
    everything else inactive, so no root filter is needed here.
    """
    return select(Taxon).where(Taxon.is_active, Taxon.subtree_image_count > 0)


def get_public_taxon(db: Session, inat_taxon_id: int) -> Taxon | None:
    return db.scalar(public_taxa().where(Taxon.inat_taxon_id == inat_taxon_id))


@dataclass
class DirectoryPage:
    taxa: list[Taxon]
    total: int
    page: int
    pages: int


def directory(
    db: Session,
    *,
    rank: str = "species",
    letter: str = "",
    sort: str = "name",
    within: Taxon | None = None,
    page: int = 1,
    per_page: int = 48,
) -> DirectoryPage:
    query = public_taxa().where(DIRECTORY_RANKS.get(rank, DIRECTORY_RANKS["species"])[1])
    if within is not None:
        query = query.where(Taxon.ancestor_ids.contains([within.inat_taxon_id]))
    if letter:
        query = query.where(func.lower(Taxon.name).startswith(letter.lower()))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    order = (
        (Taxon.subtree_image_count.desc(), Taxon.name)
        if sort == "photos"
        else (Taxon.name,)
    )
    taxa = db.scalars(query.order_by(*order).offset((page - 1) * per_page).limit(per_page)).all()
    return DirectoryPage(list(taxa), total, page, pages)


def search(
    db: Session, text: str, limit: int = 20, public_only: bool = True
) -> list[tuple[Taxon, str | None]]:
    """Taxa whose scientific name, synonym or common name matches `text`.

    Exact matches come first, then prefix matches, then fuzzy (trigram) matches;
    within each group, taxa with more photos rank higher. Returns (taxon,
    matched_name) where matched_name is set when the hit was not the accepted name.
    """
    needle = " ".join(text.split()).lower()[:100]
    if len(needle) < 2:
        return []
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    lowered = func.lower(TaxonName.name)
    quality = case(
        (lowered == needle, 0),
        (lowered.like(escaped + "%"), 1),
        (lowered.like("% " + escaped + "%"), 2),
        else_=3,
    )
    matches = (
        select(
            TaxonName.taxon_id,
            TaxonName.name,
            TaxonName.kind,
            quality.label("quality"),
            func.similarity(lowered, needle).label("similarity"),
        )
        .where(
            or_(
                lowered.like(escaped + "%"),
                lowered.like("% " + escaped + "%"),
                lowered.op("%")(needle),
            )
        )
        .subquery()
    )
    best = (
        select(
            matches.c.taxon_id,
            matches.c.name,
            matches.c.kind,
            matches.c.quality,
            matches.c.similarity,
        )
        .distinct(matches.c.taxon_id)
        .order_by(
            matches.c.taxon_id,
            matches.c.quality,
            (matches.c.kind != "scientific"),
            matches.c.similarity.desc(),
        )
        .subquery()
    )
    scope = public_taxa() if public_only else select(Taxon).where(Taxon.is_active)
    allowed = scope.with_only_columns(Taxon.id)
    rows = db.execute(
        select(Taxon, best.c.name, best.c.kind)
        .join(best, best.c.taxon_id == Taxon.id)
        .where(Taxon.id.in_(allowed))
        .order_by(
            best.c.quality,
            best.c.similarity.desc(),
            Taxon.subtree_image_count.desc(),
            Taxon.name,
        )
        .limit(limit)
    ).all()
    return [(taxon, None if kind == "scientific" else name) for taxon, name, kind in rows]


def ancestors(db: Session, taxon: Taxon, major_only: bool = True) -> list[Taxon]:
    if not taxon.ancestor_ids:
        return []
    rows = {
        t.inat_taxon_id: t
        for t in db.scalars(select(Taxon).where(Taxon.inat_taxon_id.in_(taxon.ancestor_ids))).all()
    }
    chain = [rows[i] for i in taxon.ancestor_ids if i in rows]
    if major_only:
        chain = [t for t in chain if t.rank in MAJOR_RANKS or t is chain[-1]]
    return chain


def children(db: Session, taxon: Taxon, limit: int = 60) -> tuple[list[Taxon], int]:
    query = public_taxa().where(Taxon.parent_id == taxon.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(Taxon.subtree_image_count.desc(), Taxon.name).limit(limit)
    ).all()
    return list(rows), total


def recently_ranked(db: Session, limit: int = 8) -> list[Taxon]:
    latest = (
        select(Vote.taxon_id, func.max(Vote.updated_at).label("at"))
        .group_by(Vote.taxon_id)
        .order_by(func.max(Vote.updated_at).desc())
        .limit(limit * 2)
        .subquery()
    )
    return list(
        db.scalars(
            public_taxa()
            .join(latest, latest.c.taxon_id == Taxon.id)
            .order_by(latest.c.at.desc())
            .limit(limit)
        ).all()
    )


def needs_ranking(db: Session, limit: int = 8) -> list[Taxon]:
    """Well-photographed species nobody has ranked yet."""
    ranked = select(Vote.taxon_id).where(Vote.taxon_id == Taxon.id).exists()
    return list(
        db.scalars(
            public_taxa()
            .where(Taxon.rank_level == 10, Taxon.subtree_image_count >= 8, ~ranked)
            .order_by(Taxon.subtree_image_count.desc())
            .limit(limit)
        ).all()
    )


def mosaic(db: Session, count: int = 6) -> list[Taxon]:
    """A changing handful of well-photographed species for the home page."""
    popular = (
        public_taxa()
        .where(Taxon.rank_level == 10, Taxon.cover_image_id.is_not(None))
        .order_by(Taxon.subtree_image_count.desc())
        .limit(300)
        .subquery()
    )
    return list(db.scalars(select(Taxon).join(popular, popular.c.id == Taxon.id).order_by(func.random()).limit(count)).all())
