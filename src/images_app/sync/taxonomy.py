"""Import the iNaturalist taxonomy from its Darwin Core Archive.

The archive (about 80 MB) lists every active iNaturalist taxon with its parent.
We stream it twice: once to learn the tree, once to load the configured root
taxa (Fungi by default) and all their descendants. Taxa that disappear from the
archive are marked inactive, never deleted, so votes and images stay intact.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.db import SessionLocal
from images_app.models import Taxon, TaxonName
from images_app.sync.http import PoliteClient
from images_app.sync.runs import Run

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://www.inaturalist.org/taxa/inaturalist-taxonomy.dwca.zip"
ARCHIVE_MAX_AGE_DAYS = 20
COMMON_NAME_FILES = ("VernacularNames-english.csv",)
TAXA_API = "https://api.inaturalist.org/v1/taxa/"

RANK_LEVELS = {
    "stateofmatter": 100, "kingdom": 70, "subkingdom": 67, "phylum": 60, "subphylum": 57,
    "superclass": 53, "class": 50, "subclass": 47, "infraclass": 45, "subterclass": 44,
    "superorder": 43, "order": 40, "suborder": 37, "infraorder": 35, "parvorder": 34.5,
    "zoosection": 34, "zoosubsection": 33.5, "superfamily": 33, "epifamily": 32,
    "family": 30, "subfamily": 27, "supertribe": 26, "tribe": 25, "subtribe": 24,
    "genus": 20, "genushybrid": 20, "subgenus": 15, "section": 13, "subsection": 12,
    "complex": 11, "species": 10, "hybrid": 10, "subspecies": 5, "variety": 5, "form": 5,
    "infrahybrid": 5,
}


def _id_from_uri(value: str | None) -> int | None:
    value = (value or "").rstrip("/")
    tail = value.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _rows(archive: zipfile.ZipFile, member: str) -> Iterator[dict]:
    with archive.open(member) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))


def ensure_archive(path: Path | None = None) -> Path:
    """Return a local copy of the taxonomy archive, downloading it if missing or stale."""
    if path is not None:
        return path
    target = settings.data_dir / "inaturalist-taxonomy.dwca.zip"
    fresh = target.exists() and time.time() - target.stat().st_mtime < ARCHIVE_MAX_AGE_DAYS * 86400
    if not fresh:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        log.info("downloading %s", ARCHIVE_URL)
        with PoliteClient(min_interval=1.0) as client:
            client.download(ARCHIVE_URL, partial)
        partial.replace(target)
    return target


def import_taxonomy(run: Run, archive_path: Path, client: PoliteClient | None = None) -> None:
    roots = set(settings.root_taxon_ids)
    with zipfile.ZipFile(archive_path) as archive:
        parents: dict[int, int | None] = {}
        for row in _rows(archive, "taxa.csv"):
            taxon_id = _id_from_uri(row["id"])
            if taxon_id is not None:
                parents[taxon_id] = _id_from_uri(row["parentNameUsageID"])
        missing = roots - parents.keys()
        if missing:
            raise ValueError(f"root taxa {sorted(missing)} are not in the taxonomy archive")
        ancestry = _ancestry_within_roots(parents, roots)
        del parents
        log.info("taxonomy archive: %d taxa under roots %s", len(ancestry), sorted(roots))

        with SessionLocal() as db:
            previously_active = set(
                db.scalars(select(Taxon.inat_taxon_id).where(Taxon.is_active)).all()
            )
            if previously_active and len(ancestry) < 0.8 * len(previously_active):
                raise ValueError(
                    f"archive has {len(ancestry)} taxa but {len(previously_active)} are active; "
                    "refusing to deactivate so many (truncated archive?)"
                )
            batch: list[dict] = []
            for row in _rows(archive, "taxa.csv"):
                taxon_id = _id_from_uri(row["id"])
                if taxon_id not in ancestry:
                    continue
                rank = (row["taxonRank"] or "").strip().lower() or "unranked"
                batch.append(
                    {
                        "inat_taxon_id": taxon_id,
                        "name": row["scientificName"].strip(),
                        "rank": rank,
                        "rank_level": int(RANK_LEVELS.get(rank, 0)),
                        "ancestor_ids": ancestry[taxon_id],
                        "is_active": True,
                        "last_synced_at": run.started_at,
                    }
                )
                if len(batch) >= 2000:
                    _upsert_taxa(db, batch)
                    run.records += len(batch)
                    batch.clear()
            _upsert_taxa(db, batch)
            run.records += len(batch)
            _link_parents_and_names(db)
            deactivated = _deactivate_missing(db, run.started_at)
            run.stats["taxa"] = len(ancestry)
            run.stats["deactivated"] = len(deactivated)
            run.stats["common_names"] = _import_common_names(db, archive, ancestry.keys())
            db.commit()
            if deactivated and client is not None:
                run.stats["synonyms"] = record_synonyms(db, client, deactivated)


def _ancestry_within_roots(parents: dict[int, int | None], roots: set[int]) -> dict[int, list[int]]:
    """Map each taxon under one of `roots` to its ancestor IDs (root first)."""
    result: dict[int, list[int]] = {root: [] for root in roots}
    outside: set[int] = set()
    for start in parents:
        path: list[int] = []
        node: int | None = start
        while node is not None and node not in result and node not in outside:
            path.append(node)
            node = parents.get(node)
            if len(path) > 100:  # defensive: cycles in malformed data
                node = None
        if node is None or node in outside:
            outside.update(path)
            continue
        lineage = result[node] + [node]
        for taxon_id in reversed(path):
            result[taxon_id] = lineage
            lineage = lineage + [taxon_id]
    return result


def _upsert_taxa(db: Session, rows: list[dict]) -> None:
    if not rows:
        return
    statement = insert(Taxon).values(rows)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[Taxon.inat_taxon_id],
            set_={
                "name": statement.excluded.name,
                "rank": statement.excluded.rank,
                "rank_level": statement.excluded.rank_level,
                "ancestor_ids": statement.excluded.ancestor_ids,
                "is_active": True,
                "last_synced_at": statement.excluded.last_synced_at,
                "updated_at": func.now(),
            },
        )
    )
    db.commit()


def _link_parents_and_names(db: Session) -> None:
    db.execute(
        text(
            """
            UPDATE taxa AS child SET parent_id = parent.id
            FROM taxa AS parent
            WHERE parent.inat_taxon_id = child.ancestor_ids[cardinality(child.ancestor_ids)]
              AND child.parent_id IS DISTINCT FROM parent.id
            """
        )
    )
    # Renamed taxa keep their old name as a searchable synonym.
    db.execute(
        text(
            """
            INSERT INTO taxon_names (taxon_id, name, kind, source)
            SELECT n.taxon_id, n.name, 'synonym', 'inaturalist'
            FROM taxon_names n JOIN taxa t ON t.id = n.taxon_id
            WHERE n.kind = 'scientific' AND n.name <> t.name
            ON CONFLICT DO NOTHING
            """
        )
    )
    db.execute(
        text(
            """
            DELETE FROM taxon_names n USING taxa t
            WHERE t.id = n.taxon_id AND n.kind = 'scientific' AND n.name <> t.name
            """
        )
    )
    db.execute(
        text(
            """
            INSERT INTO taxon_names (taxon_id, name, kind, source)
            SELECT id, name, 'scientific', 'inaturalist' FROM taxa
            ON CONFLICT DO NOTHING
            """
        )
    )


def _deactivate_missing(db: Session, started_at: datetime) -> list[int]:
    return list(
        db.scalars(
            update(Taxon)
            .where(Taxon.is_active, Taxon.last_synced_at < started_at)
            .values(is_active=False, updated_at=func.now())
            .returning(Taxon.id)
        ).all()
    )


def _import_common_names(db: Session, archive: zipfile.ZipFile, members) -> int:
    members = set(members)
    internal_ids = dict(db.execute(select(Taxon.inat_taxon_id, Taxon.id)).all())
    names: dict[int, list[str]] = {}
    available = set(archive.namelist())
    for filename in COMMON_NAME_FILES:
        if filename not in available:
            log.warning("taxonomy archive has no %s", filename)
            continue
        for row in _rows(archive, filename):
            taxon_id = _id_from_uri(row["id"])
            name = (row.get("vernacularName") or "").strip()
            if taxon_id in members and name and len(name) <= 300:
                bucket = names.setdefault(taxon_id, [])
                if name not in bucket:
                    bucket.append(name)
    db.execute(text("DELETE FROM taxon_names WHERE kind = 'common' AND source = 'inaturalist'"))
    rows = [
        {"taxon_id": internal_ids[taxon_id], "name": name, "kind": "common",
         "source": "inaturalist", "language": "en"}
        for taxon_id, bucket in names.items()
        for name in bucket
    ]
    for offset in range(0, len(rows), 5000):
        db.execute(insert(TaxonName).values(rows[offset : offset + 5000]).on_conflict_do_nothing())
    db.execute(update(Taxon).values(common_name=None).where(Taxon.common_name.is_not(None)))
    for offset in range(0, len(names), 5000):
        chunk = list(names.items())[offset : offset + 5000]
        db.execute(
            text(
                "UPDATE taxa SET common_name = v.name "
                "FROM unnest(CAST(:ids AS integer[]), CAST(:names AS text[])) AS v(inat_id, name) "
                "WHERE taxa.inat_taxon_id = v.inat_id"
            ),
            {"ids": [taxon_id for taxon_id, _ in chunk], "names": [bucket[0] for _, bucket in chunk]},
        )
    return len(rows)


def record_synonyms(db: Session, client: PoliteClient, taxon_ids: list[int]) -> int:
    """For newly inactive taxa, ask iNaturalist which taxon replaced them and keep the old
    name as a searchable synonym of the replacement."""
    inactive = dict(
        db.execute(select(Taxon.inat_taxon_id, Taxon.name).where(Taxon.id.in_(taxon_ids))).all()
    )
    internal_ids = dict(db.execute(select(Taxon.inat_taxon_id, Taxon.id).where(Taxon.is_active)).all())
    added = 0
    ids = sorted(inactive)
    for offset in range(0, len(ids), 30):
        chunk = ids[offset : offset + 30]
        payload = client.get(TAXA_API + ",".join(map(str, chunk))).json()
        for result in payload.get("results", []):
            replacements = [i for i in result.get("current_synonymous_taxon_ids") or [] if i in internal_ids]
            old_name = inactive.get(result.get("id"))
            if len(replacements) == 1 and old_name:
                db.execute(
                    insert(TaxonName)
                    .values(taxon_id=internal_ids[replacements[0]], name=old_name,
                            kind="synonym", source="inaturalist")
                    .on_conflict_do_nothing()
                )
                added += 1
        db.commit()
    return added
