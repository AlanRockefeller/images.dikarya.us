"""Derived data refreshed after every import: vote order, image counts and cover photos."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Votes stay attached to images that were hidden or re-identified, so history is
# auditable; they are moved behind the user's visible choices so displayed ranks
# remain 1, 2, 3...
RENUMBER_VOTES = """
WITH ordered AS (
    SELECT v.id,
           row_number() OVER (
               PARTITION BY v.user_id, v.taxon_id
               ORDER BY (vv.id IS NOT NULL) DESC, v.rank, v.id
           ) AS new_rank
    FROM votes v LEFT JOIN visible_votes vv ON vv.id = v.id
)
UPDATE votes SET rank = ordered.new_rank
FROM ordered
WHERE votes.id = ordered.id AND votes.rank <> ordered.new_rank
"""

IMAGE_COUNTS = """
UPDATE taxa SET image_count = counts.n
FROM (
    SELECT t.id, count(i.id) AS n
    FROM taxa t LEFT JOIN images i ON i.taxon_id = t.id AND i.is_active
    GROUP BY t.id
) AS counts
WHERE taxa.id = counts.id AND taxa.image_count <> counts.n
"""

SUBTREE_COUNTS = """
WITH contributions AS (
    SELECT inat_taxon_id AS taxon, image_count AS n FROM taxa WHERE image_count > 0
    UNION ALL
    SELECT unnest(ancestor_ids), image_count FROM taxa WHERE image_count > 0
), sums AS (
    SELECT taxon, sum(n)::integer AS n FROM contributions GROUP BY taxon
)
UPDATE taxa SET subtree_image_count = coalesce(sums.n, 0)
FROM taxa AS t LEFT JOIN sums ON sums.taxon = t.inat_taxon_id
WHERE taxa.id = t.id AND taxa.subtree_image_count <> coalesce(sums.n, 0)
"""

# A taxon's cover is the community's favourite photo for it (most first-place
# votes, then most voters). Unranked species use their most-faved photo.
DIRECT_COVERS = """
WITH tallies AS (
    SELECT taxon_id, image_id, count(*) AS voters, count(*) FILTER (WHERE rank = 1) AS firsts
    FROM visible_votes
    WHERE CAST(:taxon_id AS bigint) IS NULL OR taxon_id = :taxon_id
    GROUP BY taxon_id, image_id
), voted AS (
    SELECT DISTINCT ON (t.taxon_id) t.taxon_id, t.image_id
    FROM tallies t JOIN images i ON i.id = t.image_id
    ORDER BY t.taxon_id, t.firsts DESC, t.voters DESC, i.faves DESC, i.id
), faved AS (
    SELECT DISTINCT ON (i.taxon_id) i.taxon_id, i.id AS image_id
    FROM images i JOIN taxa t ON t.id = i.taxon_id
    WHERE i.is_active AND t.rank_level <= 10
      AND (CAST(:taxon_id AS bigint) IS NULL OR i.taxon_id = :taxon_id)
      AND i.taxon_id NOT IN (SELECT taxon_id FROM voted)
    ORDER BY i.taxon_id, i.faves DESC, i.position, i.id
), chosen AS (
    SELECT * FROM voted UNION ALL SELECT * FROM faved
)
UPDATE taxa SET cover_image_id = chosen.image_id
FROM chosen
WHERE taxa.id = chosen.taxon_id AND taxa.cover_image_id IS DISTINCT FROM chosen.image_id
"""

# Everything else borrows the cover of its best-photographed species (or lower).
BORROWED_COVERS = """
WITH donor AS (
    SELECT DISTINCT ON (ancestor) ancestor, t.cover_image_id
    FROM taxa t CROSS JOIN LATERAL unnest(t.ancestor_ids) AS ancestor
    WHERE t.rank_level <= 10 AND t.image_count > 0 AND t.cover_image_id IS NOT NULL
    ORDER BY ancestor, t.image_count DESC, t.id
)
UPDATE taxa SET cover_image_id = donor.cover_image_id
FROM donor
WHERE taxa.inat_taxon_id = donor.ancestor
  AND NOT EXISTS (SELECT 1 FROM visible_votes vv WHERE vv.taxon_id = taxa.id)
  AND (taxa.rank_level > 10 OR taxa.image_count = 0)
  AND taxa.cover_image_id IS DISTINCT FROM donor.cover_image_id
"""

# BORROWED_COVERS for one taxon, looking up its donor through the ancestor_ids index.
BORROWED_COVER = """
UPDATE taxa SET cover_image_id = donor.cover_image_id
FROM (
    SELECT t.cover_image_id
    FROM taxa t, taxa target
    WHERE target.id = :taxon_id
      AND t.ancestor_ids @> ARRAY[target.inat_taxon_id]
      AND t.rank_level <= 10 AND t.image_count > 0 AND t.cover_image_id IS NOT NULL
    ORDER BY t.image_count DESC, t.id
    LIMIT 1
) AS donor
WHERE taxa.id = :taxon_id
  AND NOT EXISTS (SELECT 1 FROM visible_votes vv WHERE vv.taxon_id = taxa.id)
  AND (taxa.rank_level > 10 OR taxa.image_count = 0)
  AND taxa.cover_image_id IS DISTINCT FROM donor.cover_image_id
"""

CLEAR_COVERS = """
UPDATE taxa SET cover_image_id = NULL
WHERE cover_image_id IS NOT NULL
  AND (subtree_image_count = 0
       OR NOT EXISTS (SELECT 1 FROM images i WHERE i.id = taxa.cover_image_id AND i.is_active))
"""


def refresh_derived_data(db: Session) -> None:
    db.execute(text(RENUMBER_VOTES))
    db.execute(text(IMAGE_COUNTS))
    db.execute(text(SUBTREE_COUNTS))
    db.execute(text(CLEAR_COVERS))
    db.execute(text(DIRECT_COVERS), {"taxon_id": None})
    db.execute(text(BORROWED_COVERS))
    db.commit()


def refresh_cover(db: Session, taxon_id: int) -> None:
    """Cheap per-taxon cover refresh after someone changes their ranking."""
    db.execute(text(DIRECT_COVERS), {"taxon_id": taxon_id})
    # Without votes, a higher taxon goes back to borrowing a descendant's cover.
    db.execute(text(BORROWED_COVER), {"taxon_id": taxon_id})
