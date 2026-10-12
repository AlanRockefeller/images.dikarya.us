"""Image galleries, community results and per-user rankings for a taxon."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session, selectinload

from images_app.models import SUBTREE_MAX_RANK_LEVEL, Image, Taxon, User, visible_votes

PAGE_SIZE = 60
SORTS = {
    "faves": "Most faved",
    "community": "Community picks",
    "newest": "Newest",
    "random": "Random",
}
SOURCES = {"all": "All sources", "inaturalist": "iNaturalist", "mushroom_observer": "Mushroom Observer"}


def includes_subtaxa(taxon: Taxon) -> bool:
    return taxon.rank_level <= SUBTREE_MAX_RANK_LEVEL


def shown_in(db: Session, taxon: Taxon, exact: bool = False):
    """SQL condition: the image appears in this taxon's gallery."""
    if exact or not includes_subtaxa(taxon):
        return Image.is_active & (Image.taxon_id == taxon.id)
    # Resolving the subtree first lets PostgreSQL use the per-taxon image index
    # (a subquery here makes it scan every image).
    subtaxa = db.scalars(select(Taxon.id).where(Taxon.ancestor_ids.contains([taxon.inat_taxon_id]))).all()
    return Image.is_active & Image.taxon_id.in_([taxon.id, *subtaxa])


@dataclass
class GalleryPage:
    images: list[Image]
    total: int
    page: int
    has_more: bool
    has_mo: bool


def gallery(
    db: Session,
    taxon: Taxon,
    *,
    sort: str = "faves",
    source: str = "all",
    exact: bool = False,
    page: int = 1,
    seed: str = "",
) -> GalleryPage:
    filters = [shown_in(db, taxon, exact)]
    total, from_mo = db.execute(
        select(func.count(), func.count().filter(Image.source == "mushroom_observer")).where(*filters)
    ).one()
    if source in ("inaturalist", "mushroom_observer"):
        filters.append(Image.source == source)
        total = from_mo if source == "mushroom_observer" else total - from_mo

    query = select(Image).where(*filters).options(selectinload(Image.taxon))
    if sort == "community":
        votes = visible_votes.c
        tallies = (
            select(
                votes.image_id,
                func.count().label("voters"),
                func.count().filter(votes.rank == 1).label("firsts"),
                func.avg(votes.rank).label("mean_rank"),
            )
            .where(votes.taxon_id == taxon.id)
            .group_by(votes.image_id)
            .subquery()
        )
        query = query.outerjoin(tallies, tallies.c.image_id == Image.id).order_by(
            func.coalesce(tallies.c.firsts, 0).desc(),
            func.coalesce(tallies.c.voters, 0).desc(),
            tallies.c.mean_rank.asc().nulls_last(),
            Image.faves.desc(),
            Image.position,
            Image.id,
        )
    elif sort == "newest":
        query = query.order_by(Image.observed_on.desc().nulls_last(), Image.id.desc())
    elif sort == "random":
        query = query.order_by(func.md5(cast(Image.id, String) + seed), Image.id)
    else:
        query = query.order_by(Image.faves.desc(), Image.position, Image.id)

    page = max(page, 1)
    rows = list(db.scalars(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all())
    return GalleryPage(rows[:PAGE_SIZE], total, page, len(rows) > PAGE_SIZE, from_mo > 0)


def user_ranking(db: Session, user: User | None, taxon: Taxon) -> list[Image]:
    """The user's selected images for a taxon, best first."""
    if user is None:
        return []
    votes = visible_votes.c
    return list(
        db.scalars(
            select(Image)
            .join(visible_votes, votes.image_id == Image.id)
            .where(votes.user_id == user.id, votes.taxon_id == taxon.id)
            .order_by(votes.rank)
        ).all()
    )


def ranker_count(db: Session, taxon: Taxon) -> int:
    votes = visible_votes.c
    return db.scalar(
        select(func.count(func.distinct(votes.user_id))).where(votes.taxon_id == taxon.id)
    ) or 0


@dataclass
class Result:
    image: Image
    voters: int
    firsts: int
    seconds: int
    thirds: int
    mean_rank: float


def community_results(db: Session, taxon: Taxon, limit: int = 100) -> list[Result]:
    """Every image anyone selected, with its rank distribution.

    Rows are ordered like a medal table (most first places, then seconds,
    thirds, voters). Raw votes are kept, so other aggregations can be added.
    """
    votes = visible_votes.c
    firsts = func.count().filter(votes.rank == 1)
    seconds = func.count().filter(votes.rank == 2)
    thirds = func.count().filter(votes.rank == 3)
    voters = func.count()
    mean_rank = func.avg(votes.rank)
    rows = db.execute(
        select(Image, voters, firsts, seconds, thirds, mean_rank)
        .join(visible_votes, votes.image_id == Image.id)
        .where(votes.taxon_id == taxon.id)
        .group_by(Image.id)
        .order_by(firsts.desc(), seconds.desc(), thirds.desc(), voters.desc(), mean_rank, Image.id)
        .limit(limit)
    ).all()
    return [Result(image, v, f, s, t, float(m)) for image, v, f, s, t, m in rows]
