from __future__ import annotations

import argparse
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.db import SessionLocal
from images_app.importers.common import (
    begin_sync,
    fail_sync,
    finish_sync,
    insert_ignore_rows,
    import_lock,
    normalized_name,
    parse_bool,
    refresh_image_counts,
    stream_tsv,
    upsert_rows,
)
from images_app.models import Image, SourceTaxon, Taxon, TaxonAlias

logger = logging.getLogger("images_app.importers.mushroom_observer")

# The first item is Mushroom Observer's explicit Wikipedia-compatible license.
# Other entries are admitted only when the full, non-commercial-free BY-SA
# license text is explicit. Unknown labels are retained as metadata and hidden.
MO_WIKIPEDIA_LICENSE = "creative commons wikipedia compatible v3.0"
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def mo_license_allowed(value: str) -> bool:
    normalized = " ".join(value.strip().casefold().split())
    if normalized in {MO_WIKIPEDIA_LICENSE, "public domain", "public-domain", "cc0", "cc0 1.0", "creative commons zero v1.0"}:
        return True
    if "noncommercial" in normalized or "non-commercial" in normalized or "no derivatives" in normalized:
        return False
    explicit_by_sa = "attribution-sharealike" in normalized or "attribution share-alike" in normalized or "cc by-sa" in normalized
    return bool(explicit_by_sa and any(version in normalized for version in ("2.0", "2.5", "3.0", "4.0")))


def _sqlite_stage(path: Path) -> sqlite3.Connection:
    stage = sqlite3.connect(path)
    stage.execute("PRAGMA journal_mode=OFF")
    stage.execute("PRAGMA synchronous=OFF")
    stage.executescript(
        """
        CREATE TABLE mo_names(id TEXT PRIMARY KEY, text_name TEXT, deprecated TEXT, correct_spelling_id TEXT, synonym_id TEXT);
        CREATE TABLE mo_observations(id TEXT PRIMARY KEY, name_id TEXT);
        CREATE TABLE image_observations(image_id TEXT, observation_id TEXT);
        CREATE INDEX ix_mo_links_image ON image_observations(image_id);
        CREATE INDEX ix_mo_links_observation ON image_observations(observation_id);
        CREATE TABLE mo_images(id TEXT PRIMARY KEY, content_type TEXT, copyright_holder TEXT, license TEXT, ok_for_export TEXT);
        CREATE TABLE accepted_names(name TEXT, taxon_id INTEGER);
        CREATE INDEX ix_accepted_name ON accepted_names(name);
        CREATE TABLE aliases(name TEXT, taxon_id INTEGER);
        CREATE INDEX ix_alias_name ON aliases(name);
        CREATE TABLE mapped_names(mo_name_id TEXT PRIMARY KEY, taxon_id INTEGER, source_taxon_row_id INTEGER, status TEXT, method TEXT, original_name TEXT, normalized_name TEXT);
        """
    )
    return stage


def _load_csv(stage: sqlite3.Connection, table: str, path: str, columns: tuple[str, ...], insert_sql: str) -> int:
    batch = []
    count = 0
    for row in stream_tsv(path, columns):
        batch.append(tuple(row[column] for column in columns))
        if len(batch) >= 20_000:
            stage.executemany(insert_sql, batch)
            stage.commit()
            count += len(batch)
            logger.info("staged %s rows: %d", table, count)
            batch.clear()
    if batch:
        stage.executemany(insert_sql, batch)
        stage.commit()
        count += len(batch)
    return count


def _load_taxon_index(session: Session, stage: sqlite3.Connection, root_id: int) -> None:
    taxa = session.execute(
        select(Taxon.scientific_name, Taxon.id).where(
            Taxon.active.is_(True),
            (Taxon.inat_taxon_id == root_id) | Taxon.ancestry.like(f"%/{root_id}/%"),
        )
    ).yield_per(5000)
    accepted_batch = []
    for name, taxon_id in taxa:
        accepted_batch.append((normalized_name(name), taxon_id))
        if len(accepted_batch) >= 10_000:
            stage.executemany("INSERT INTO accepted_names VALUES (?,?)", accepted_batch)
            accepted_batch.clear()
    if accepted_batch:
        stage.executemany("INSERT INTO accepted_names VALUES (?,?)", accepted_batch)
    aliases = session.execute(
        select(TaxonAlias.name, TaxonAlias.taxon_id)
        .join(Taxon, Taxon.id == TaxonAlias.taxon_id)
        .where(Taxon.active.is_(True), (Taxon.inat_taxon_id == root_id) | Taxon.ancestry.like(f"%/{root_id}/%"))
    ).yield_per(5000)
    alias_batch = []
    for name, taxon_id in aliases:
        alias_batch.append((normalized_name(name), taxon_id))
        if len(alias_batch) >= 10_000:
            stage.executemany("INSERT INTO aliases VALUES (?,?)", alias_batch)
            alias_batch.clear()
    if alias_batch:
        stage.executemany("INSERT INTO aliases VALUES (?,?)", alias_batch)
    stage.commit()


def _candidate_ids(stage: sqlite3.Connection, table: str, normalized: str) -> set[int]:
    return {int(row[0]) for row in stage.execute(f"SELECT DISTINCT taxon_id FROM {table} WHERE name=?", (normalized,))}


def _map_name(stage: sqlite3.Connection, name_id: str, name: str, correct_spelling_id: str, synonym_id: str) -> tuple[int | None, str, str]:
    normalized = normalized_name(name)
    accepted = _candidate_ids(stage, "accepted_names", normalized)
    if len(accepted) == 1:
        return next(iter(accepted)), "mapped", "exact_accepted_name"
    if len(accepted) > 1:
        return None, "unresolved", "ambiguous_exact_name"

    linked_ids = [candidate for candidate in (correct_spelling_id, synonym_id) if candidate and candidate != "NULL"]
    linked_names = []
    for linked_id in linked_ids:
        row = stage.execute("SELECT text_name FROM mo_names WHERE id=?", (linked_id,)).fetchone()
        if row and row[0]:
            linked_names.append(normalized_name(row[0]))
    synonym_candidates: set[int] = set()
    for linked_name in linked_names:
        synonym_candidates.update(_candidate_ids(stage, "accepted_names", linked_name))
    if len(synonym_candidates) == 1:
        return next(iter(synonym_candidates)), "mapped", "known_mo_synonym"
    if len(synonym_candidates) > 1:
        return None, "unresolved", "ambiguous_mo_synonym"

    alias_candidates = _candidate_ids(stage, "aliases", normalized)
    for linked_name in linked_names:
        alias_candidates.update(_candidate_ids(stage, "aliases", linked_name))
    if len(alias_candidates) == 1:
        return next(iter(alias_candidates)), "mapped", "known_taxon_alias"
    if len(alias_candidates) > 1:
        return None, "unresolved", "ambiguous_alias"
    return None, "unresolved", "no_confident_match"


def _write_mo_source_taxa(session: Session, stage: sqlite3.Connection, started: datetime) -> None:
    # Only names represented by a public observation image enter the review queue.
    used_ids = stage.execute(
        "SELECT DISTINCT o.name_id FROM mo_observations o "
        "JOIN image_observations l ON l.observation_id=o.id "
        "JOIN mo_images i ON i.id=l.image_id "
        "WHERE o.name_id IS NOT NULL AND lower(trim(i.ok_for_export)) IN ('1','true','t','yes','y')"
    )
    used = {str(row[0]) for row in used_ids}
    if not used:
        return
    manual = {}
    values = list(used)
    for offset in range(0, len(values), 500):
        manual.update(
            dict(
                session.execute(
                    select(SourceTaxon.source_taxon_id, SourceTaxon.taxon_id).where(
                        SourceTaxon.source == "mushroom_observer",
                        SourceTaxon.source_taxon_id.in_(values[offset : offset + 500]),
                        SourceTaxon.mapping_status == "mapped",
                        SourceTaxon.mapping_method == "manual",
                    )
                ).all()
            )
        )
    names_cursor = stage.execute(
        "SELECT id,text_name,correct_spelling_id,synonym_id FROM mo_names WHERE id IN (" + ",".join("?" for _ in values) + ")",
        values,
    )
    mapping_batch: list[dict] = []
    stage_batch: list[tuple] = []
    alias_batch: list[dict] = []
    for name_id, original, correct_spelling_id, synonym_id in names_cursor:
        if not original:
            continue
        taxon_id, status, method = _map_name(stage, str(name_id), original, correct_spelling_id or "", synonym_id or "")
        if str(name_id) in manual:
            taxon_id, status, method = int(manual[str(name_id)]), "mapped", "manual"
        mapping_batch.append(
            {
                "source": "mushroom_observer",
                "source_taxon_id": str(name_id),
                "original_name": original,
                "normalized_name": normalized_name(original),
                "taxon_id": taxon_id,
                "mapping_status": status,
                "mapping_method": method,
                "updated_at": started,
            }
        )
        stage_batch.append((str(name_id), taxon_id, status, method, original, normalized_name(original)))
        if taxon_id is not None:
            alias_batch.append({"taxon_id": taxon_id, "name": original.strip(), "source": "Mushroom Observer"})
        if len(mapping_batch) >= 500:
            upsert_rows(session, SourceTaxon, mapping_batch, ["source", "source_taxon_id"], ["original_name", "normalized_name", "taxon_id", "mapping_status", "mapping_method", "updated_at"])
            session.commit()
            stage.executemany("INSERT OR REPLACE INTO mapped_names(mo_name_id,taxon_id,status,method,original_name,normalized_name) VALUES (?,?,?,?,?,?)", stage_batch)
            stage.commit()
            insert_ignore_rows(session, TaxonAlias, alias_batch, ["taxon_id", "name", "source"])
            session.commit()
            mapping_batch.clear()
            stage_batch.clear()
            alias_batch.clear()
    if mapping_batch:
        upsert_rows(session, SourceTaxon, mapping_batch, ["source", "source_taxon_id"], ["original_name", "normalized_name", "taxon_id", "mapping_status", "mapping_method", "updated_at"])
        session.commit()
        stage.executemany("INSERT OR REPLACE INTO mapped_names(mo_name_id,taxon_id,status,method,original_name,normalized_name) VALUES (?,?,?,?,?,?)", stage_batch)
        stage.commit()
        insert_ignore_rows(session, TaxonAlias, alias_batch, ["taxon_id", "name", "source"])
        session.commit()

    # Resolve the app's internal SourceTaxon keys in bounded queries. External ids stay audit data.
    source_ids = [str(item) for item in used]
    for offset in range(0, len(source_ids), 500):
        rows = session.execute(
            select(SourceTaxon.source_taxon_id, SourceTaxon.id)
            .where(SourceTaxon.source == "mushroom_observer", SourceTaxon.source_taxon_id.in_(source_ids[offset : offset + 500]))
        ).all()
        stage.executemany("UPDATE mapped_names SET source_taxon_row_id=? WHERE mo_name_id=?", [(row_id, ext_id) for ext_id, row_id in rows])
    stage.commit()


def import_exports(
    session: Session,
    *,
    observations_path: str,
    image_observations_path: str,
    images_path: str,
    names_path: str,
    stage_dir: str = "/var/lib/images-import",
    full_reconciliation: bool = True,
    root_taxon_id: int = settings.fungi_taxon_id,
) -> int:
    started = begin_sync(session, "mushroom_observer")
    processed = 0
    stage_root = Path(stage_dir)
    try:
        stage_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mo-", dir=stage_root) as temp_dir:
            stage = _sqlite_stage(Path(temp_dir) / "join.sqlite3")
            _load_csv(stage, "names", names_path, ("id", "text_name", "deprecated", "correct_spelling_id", "synonym_id"), "INSERT OR REPLACE INTO mo_names VALUES (?,?,?,?,?)")
            _load_csv(stage, "observations", observations_path, ("id", "name_id"), "INSERT OR REPLACE INTO mo_observations VALUES (?,?)")
            _load_csv(stage, "image_observations", image_observations_path, ("image_id", "observation_id"), "INSERT INTO image_observations VALUES (?,?)")
            _load_csv(stage, "images", images_path, ("id", "content_type", "copyright_holder", "license", "ok_for_export"), "INSERT OR REPLACE INTO mo_images VALUES (?,?,?,?,?)")
            stage.execute("CREATE INDEX ix_mo_obs_name ON mo_observations(name_id)")
            stage.execute("CREATE INDEX ix_mo_names_id ON mo_names(id)")
            stage.commit()
            _load_taxon_index(session, stage, root_taxon_id)
            _write_mo_source_taxa(session, stage, started)

            query = stage.execute(
                "SELECT i.id,i.content_type,i.copyright_holder,i.license,i.ok_for_export," 
                "o.id,n.id,n.text_name,m.taxon_id,m.source_taxon_row_id "
                "FROM mo_images i "
                "JOIN image_observations l ON l.image_id=i.id "
                "JOIN mo_observations o ON o.id=l.observation_id "
                "JOIN mo_names n ON n.id=o.name_id "
                "LEFT JOIN mapped_names m ON m.mo_name_id=n.id "
                "ORDER BY i.id,o.id"
            )
            current_image = None
            image_fields = None
            associated = []
            image_count = 0

            def flush_image():
                nonlocal image_count, associated, current_image, image_fields
                if current_image is None or not image_fields or not associated:
                    return
                image_id, content_type, holder, license_name, ok_export, observation_id = image_fields
                if not parse_bool(ok_export):
                    return
                extension = EXTENSIONS.get((content_type or "").casefold())
                if not extension:
                    return
                mapped_ids = {int(row[0]) for row in associated if row[0] is not None}
                all_names_mapped = all(row[0] is not None and row[1] is not None for row in associated)
                taxon_id = next(iter(mapped_ids)) if all_names_mapped and len(mapped_ids) == 1 else None
                source_taxon_id = associated[0][1]
                display_ok = mo_license_allowed(license_name or "")
                active = bool(display_ok and taxon_id is not None)
                photographer = (holder or "").strip() or None
                if photographer and license_name:
                    attribution = f"© {photographer}; {license_name.strip()}"
                else:
                    attribution = photographer or (license_name or "License not recorded").strip()
                db_image = {
                    "taxon_id": taxon_id,
                    "source_taxon_id": source_taxon_id,
                    "source": "mushroom_observer",
                    "source_image_id": str(image_id),
                    "source_observation_id": str(observation_id),
                    "medium_url": f"https://mushroomobserver.org/images/640/{image_id}.{extension}",
                    "thumbnail_url": f"https://mushroomobserver.org/images/240/{image_id}.{extension}",
                    "source_page_url": f"https://mushroomobserver.org/observations/{observation_id}",
                    "photographer": photographer,
                    "license": (license_name or "License not recorded").strip()[:100],
                    "attribution": attribution,
                    "display_eligible": display_ok,
                    "active": active,
                    "source_last_seen_at": started,
                    "updated_at": started,
                }
                upsert_rows(
                    session,
                    Image,
                    [db_image],
                    ["source", "source_image_id"],
                    ["taxon_id", "source_taxon_id", "source_observation_id", "medium_url", "thumbnail_url", "source_page_url", "photographer", "license", "attribution", "display_eligible", "active", "source_last_seen_at", "updated_at"],
                )
                image_count += 1
                if image_count % 500 == 0:
                    session.commit()
                    logger.info("imported %d Mushroom Observer image records", image_count)
                associated = []

            for row in query:
                image_id = str(row[0])
                if current_image != image_id:
                    flush_image()
                    current_image = image_id
                    image_fields = (row[0], row[1], row[2], row[3], row[4], row[5])
                # Keep all associated names; an image shared across conflicting names stays unresolved.
                association = (row[8], row[9], row[6], row[7])
                if association not in associated:
                    associated.append(association)
            flush_image()
            session.commit()
            if full_reconciliation:
                session.execute(
                    update(Image)
                    .where(
                        Image.source == "mushroom_observer",
                        or_(Image.source_last_seen_at.is_(None), Image.source_last_seen_at < started),
                    )
                    .values(active=False)
                )
            refresh_image_counts(session)
            processed = image_count
            stage.close()
        finish_sync(session, "mushroom_observer", started, processed, cursor=started.isoformat())
        return processed
    except BaseException as exc:
        fail_sync(session, "mushroom_observer", started, exc)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official Mushroom Observer nightly CSV exports")
    parser.add_argument("--observations", required=True)
    parser.add_argument("--images-observations", required=True)
    parser.add_argument("--images", required=True)
    parser.add_argument("--names", required=True)
    parser.add_argument("--stage-dir", default="/var/lib/images-import")
    parser.add_argument("--no-full-reconciliation", action="store_true")
    parser.add_argument("--root-taxon-id", type=int, default=settings.fungi_taxon_id)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with import_lock(Path(args.stage_dir) / "mushroom_observer.lock"), SessionLocal() as session:
        count = import_exports(
            session,
            observations_path=args.observations,
            image_observations_path=args.images_observations,
            images_path=args.images,
            names_path=args.names,
            stage_dir=args.stage_dir,
            full_reconciliation=not args.no_full_reconciliation,
            root_taxon_id=args.root_taxon_id,
        )
        logger.info("Mushroom Observer import done (%d images)", count)


if __name__ == "__main__":
    main()
