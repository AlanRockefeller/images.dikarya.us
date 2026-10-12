"""Saving a user's ranking of images for a taxon."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from images_app.gallery import shown_in
from images_app.models import Image, Taxon, User, Vote
from images_app.sync.maintenance import refresh_cover

MAX_RANKED = 500


def set_ranking(db: Session, user: User, taxon: Taxon, image_ids: list[int]) -> list[int]:
    """Replace the user's ranking for `taxon` with `image_ids` (best first).

    Images not currently shown in the taxon's gallery are ignored. Votes on
    images that have since been hidden or re-identified elsewhere are kept for
    the record and placed after the visible ones, so visible ranks are always
    1..n. Returns the saved order.
    """
    # Serialize concurrent saves by the same user (e.g. two open tabs).
    db.execute(select(User.id).where(User.id == user.id).with_for_update())

    existing = list(
        db.scalars(
            select(Vote).where(Vote.user_id == user.id, Vote.taxon_id == taxon.id).order_by(Vote.rank)
        ).all()
    )
    requested = list(dict.fromkeys(image_ids))[:MAX_RANKED]
    candidates = set(requested) | {vote.image_id for vote in existing}
    shown = set(
        db.scalars(select(Image.id).where(Image.id.in_(candidates), shown_in(db, taxon))).all()
    )
    wanted = [image_id for image_id in requested if image_id in shown]
    wanted_set = set(wanted)
    by_image = {vote.image_id: vote for vote in existing}

    for vote in existing:
        if vote.image_id in shown and vote.image_id not in wanted_set:
            db.delete(vote)

    rank = 0
    for rank, image_id in enumerate(wanted, start=1):
        vote = by_image.get(image_id)
        if vote is None:
            db.add(Vote(user_id=user.id, taxon_id=taxon.id, image_id=image_id, rank=rank))
        elif vote.rank != rank:
            vote.rank = rank
    for vote in existing:
        if vote.image_id not in shown:
            rank += 1
            if vote.rank != rank:
                vote.rank = rank

    db.flush()
    refresh_cover(db, taxon.id)
    db.commit()
    return wanted

