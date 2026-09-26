from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from io import TextIOWrapper
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.db import SessionLocal
from images_app.importers.common import (
    begin_sync,
    fail_sync,
    finish_sync,
    import_lock,
    parse_bool,
    refresh_image_counts,
    stream_tsv,
    upsert_rows,
)
from images_app.models import Image, Taxon, TaxonAlias

logger = logging.getLogger("images_app.importers.inaturalist")
DISPLAY_LICENSES = {"cc0", "cc-pdm", "cc-by", "cc-by-sa"}
DISPLAY_LICENSE_LABELS = {
    "cc0": "CC0 1.0",
    "cc-pdm": "Public Domain Mark 1.0",
    "cc-by": "CC BY",
    "cc-by-sa": "CC BY-SA",
}
DATASET_HOST = "inaturalist-open-data.s3.amazonaws.com"
OBSERVATIONS_API_URL = "https://api.inaturalist.org/v1/observations"
DNA_BARCODE_ITS_FIELD_ID = 2330
DNA_BARCODE_ITS_FIELD_NAME = "dna barcode its"


def has_dna_barcode_its(observation: dict) -> bool:
    """Whether the observation has a non-empty value in iNat's ITS barcode field."""
    fields = observation.get("ofvs") or []
    if not isinstance(fields, list):
        return False
    for field in fields:
        if not isinstance(field, dict):
            continue
        field_id = field.get("field_id")
        if field_id is None:
            matching_field = str(field.get("name") or "").strip().casefold() == DNA_BARCODE_ITS_FIELD_NAME
        else:
            try:
                matching_field = int(field_id) == DNA_BARCODE_ITS_FIELD_ID
            except (TypeError, ValueError):
                matching_field = False
        if matching_field and str(field.get("value") or "").strip():
            return True
    return False


def normalize_ancestry(value: str, own_id: int) -> tuple[list[int], str]:
    tokens = [part for part in re.split(r"[/\\]+", value.strip("/\\")) if part]
    ids = [int(token) for token in tokens if token.isdigit()]
    if ids and ids[-1] == own_id:
        ids.pop()
    path_ids = ids + [own_id]
    return ids, "/" + "/".join(map(str, path_ids)) + "/"


def _upsert_taxa(session: Session, batch: list[dict], started: datetime) -> None:
    if not batch:
        return
    rows = [
        {
            "inat_taxon_id": row["id"],
            "parent_id": None,
            "scientific_name": row["name"],
            "rank": row["rank"] or None,
            "ancestry": row["ancestry"],
            "active": row["active"],
            "last_synced_at": started,
            "updated_at": started,
        }
        for row in batch
    ]
    upsert_rows(session, Taxon, rows, ["inat_taxon_id"], ["scientific_name", "rank", "ancestry", "active", "last_synced_at", "updated_at"])
    session.commit()


def upsert_inaturalist_images(
    session: Session,
    rows: list[dict],
    *,
    taxon_conflicts: set[str],
    metadata_conflicts: set[str],
) -> tuple[int, set[int]]:
    """Deduplicate photo IDs and hide records that point at conflicting taxa."""
    if not rows:
        return 0, set()

    grouped: dict[str, dict] = {}
    grouped_conflicts: dict[str, set[str]] = defaultdict(set)
    touched_taxa: set[int] = set()
    for incoming in rows:
        photo_id = str(incoming["source_image_id"])
        row = dict(incoming)
        current = grouped.get(photo_id)
        if current is not None:
            if current["taxon_id"] != row["taxon_id"]:
                grouped_conflicts[photo_id].add("taxon")
            if any(current[field] != row[field] for field in ("license", "photographer", "medium_url")):
                grouped_conflicts[photo_id].add("metadata")
            old_observation = str(current.get("source_observation_id") or "")
            new_observation = str(row.get("source_observation_id") or "")
            if new_observation < old_observation:
                grouped[photo_id] = row
        else:
            grouped[photo_id] = row

    items = list(grouped.items())
    for offset in range(0, len(items), 500):
        batch = items[offset : offset + 500]
        photo_ids = [photo_id for photo_id, _ in batch]
        existing = {
            photo_id: (
                taxon_id,
                observation_id,
                license_name,
                photographer,
                medium_url,
                display_eligible,
            )
            for photo_id, taxon_id, observation_id, license_name, photographer, medium_url, display_eligible in session.execute(
                select(
                    Image.source_image_id,
                    Image.taxon_id,
                    Image.source_observation_id,
                    Image.license,
                    Image.photographer,
                    Image.medium_url,
                    Image.display_eligible,
                ).where(Image.source == "inaturalist", Image.source_image_id.in_(photo_ids))
            )
        }
        to_upsert = []
        for photo_id, row in batch:
            conflicts = grouped_conflicts.get(photo_id, set())
            previous = existing.get(photo_id)
            # Null taxon / ineligible rows are the durable marker for a photo
            # whose source observations disagree. Do not reactivate it just
            # because an incremental page contains only one association.
            if previous and previous[0] is None:
                conflicts.add("taxon")
            if previous and not previous[5]:
                conflicts.add("metadata")
            different_observation = bool(
                previous and previous[1] != row.get("source_observation_id")
            )
            if different_observation and previous[0] is not None and previous[0] != row["taxon_id"]:
                conflicts.add("taxon")
            if different_observation and any(
                previous[index] != row[field]
                for index, field in ((2, "license"), (3, "photographer"), (4, "medium_url"))
            ):
                conflicts.add("metadata")
            if "taxon" in conflicts:
                if photo_id not in taxon_conflicts:
                    logger.warning("iNaturalist photo %s is linked to multiple taxa; leaving it unresolved", photo_id)
                taxon_conflicts.add(photo_id)
            if "metadata" in conflicts:
                if photo_id not in metadata_conflicts:
                    logger.warning("iNaturalist photo %s has conflicting license or attribution metadata; hiding it", photo_id)
                metadata_conflicts.add(photo_id)
            if photo_id in taxon_conflicts:
                row["taxon_id"] = None
                row["active"] = False
            if photo_id in metadata_conflicts:
                row["display_eligible"] = False
                row["active"] = False
            if row["taxon_id"] is not None:
                touched_taxa.add(row["taxon_id"])
            if previous and previous[0] is not None:
                touched_taxa.add(previous[0])
            to_upsert.append(row)
        upsert_rows(
            session,
            Image,
            to_upsert,
            ["source", "source_image_id"],
            ["taxon_id", "source_observation_id", "medium_url", "thumbnail_url", "source_page_url", "photographer", "license", "attribution", "display_eligible", "dna_verified", "active", "source_last_seen_at", "updated_at"],
        )
    return len(items), touched_taxa


def _dwca_id(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(?:/|^)(\d+)(?:-[^/]*)?/?$", value.strip())
    if match:
        return int(match.group(1))
    return int(value) if value.strip().isdigit() else None


def import_dwca_taxonomy(session: Session, archive_path: str, stage: sqlite3.Connection, started: datetime, root_taxon_id: int) -> tuple[int, set[int]]:
    """Import the active iNaturalist taxonomy DwC-A; stream the archive into SQLite staging."""
    stage.execute("CREATE TABLE dwca_fungal (inat_taxon_id INTEGER PRIMARY KEY, parent_inat_taxon_id INTEGER, scientific_name TEXT NOT NULL, rank TEXT)")
    stage.execute("CREATE INDEX ix_dwca_parent ON dwca_fungal(parent_inat_taxon_id)")
    with zipfile.ZipFile(archive_path) as archive:
        member = next((item for item in archive.namelist() if item.rsplit("/", 1)[-1].casefold() == "taxa.csv"), None)
        if not member:
            raise ValueError("The iNaturalist taxonomy archive does not contain taxa.csv")
        with archive.open(member) as binary:
            stream = TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            header = stream.readline()
            if not header:
                raise ValueError("The iNaturalist taxonomy taxa.csv is empty")
            delimiter = "\t" if header.count("\t") > header.count(",") else ","
            reader = csv.DictReader(_prepend_header(header, stream), delimiter=delimiter)
            required = {"taxonID", "parentNameUsageID", "kingdom", "scientificName", "taxonRank"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"iNaturalist DwC-A taxa.csv is missing: {', '.join(sorted(missing))}")
            rows = []
            for line_number, row in enumerate(reader, start=2):
                external_id = _dwca_id(row.get("taxonID")) or _dwca_id(row.get("id"))
                name = (row.get("scientificName") or "").strip()
                rank = (row.get("taxonRank") or "").strip()
                is_fungi = (row.get("kingdom") or "").strip().casefold() == "fungi" or (external_id == root_taxon_id and name.casefold() == "fungi")
                if not is_fungi or external_id is None or not name:
                    continue
                parent_id = _dwca_id(row.get("parentNameUsageID"))
                rows.append((external_id, parent_id, name, rank))
                if len(rows) >= 20_000:
                    stage.executemany("INSERT OR REPLACE INTO dwca_fungal VALUES (?,?,?,?)", rows)
                    stage.commit()
                    rows.clear()
            if rows:
                stage.executemany("INSERT OR REPLACE INTO dwca_fungal VALUES (?,?,?,?)", rows)
                stage.commit()

    if stage.execute("SELECT 1 FROM dwca_fungal WHERE inat_taxon_id=?", (root_taxon_id,)).fetchone() is None:
        raise ValueError(f"iNaturalist taxonomy DwC-A did not contain configured Fungi taxon {root_taxon_id}")
    stage.execute(
        """CREATE TABLE dwca_lineage AS
        WITH RECURSIVE lineage(inat_taxon_id,parent_inat_taxon_id,scientific_name,rank,ancestry) AS (
          SELECT taxon.inat_taxon_id,taxon.parent_inat_taxon_id,taxon.scientific_name,taxon.rank,
                 '/' || taxon.inat_taxon_id || '/'
          FROM dwca_fungal taxon
          WHERE taxon.parent_inat_taxon_id IS NULL
             OR NOT EXISTS (SELECT 1 FROM dwca_fungal parent WHERE parent.inat_taxon_id=taxon.parent_inat_taxon_id)
          UNION ALL
          SELECT child.inat_taxon_id,child.parent_inat_taxon_id,child.scientific_name,child.rank,
                 lineage.ancestry || child.inat_taxon_id || '/'
          FROM dwca_fungal child JOIN lineage ON child.parent_inat_taxon_id=lineage.inat_taxon_id
          WHERE instr(lineage.ancestry, '/' || child.inat_taxon_id || '/')=0
        ) SELECT * FROM lineage"""
    )
    stage.commit()
    imported = 0
    ids: set[int] = set()
    taxon_batch = []
    cursor = stage.execute("SELECT inat_taxon_id,parent_inat_taxon_id,scientific_name,rank,ancestry FROM dwca_lineage ORDER BY inat_taxon_id")
    while rows := cursor.fetchmany(1000):
        for external_id, parent_id, name, rank, ancestry in rows:
            ancestry_ids = {int(token) for token in ancestry.strip("/").split("/") if token.isdigit()}
            if root_taxon_id not in ancestry_ids:
                ancestry = f"/{root_taxon_id}/" + ancestry.strip("/") + "/"
            ids.add(external_id)
            taxon_batch.append({"id": external_id, "name": name, "rank": rank or "", "ancestry": ancestry, "active": True})
            stage.execute(
                "INSERT OR REPLACE INTO fungal_taxa(inat_taxon_id,parent_inat_taxon_id,ancestry) VALUES (?,?,?)",
                (external_id, parent_id, ancestry),
            )
        if taxon_batch:
            _upsert_taxa(session, taxon_batch, started)
            imported += len(taxon_batch)
            logger.info("imported %d accepted taxa from iNaturalist DwC-A", imported)
            taxon_batch.clear()
        stage.commit()
    if taxon_batch:
        _upsert_taxa(session, taxon_batch, started)
        imported += len(taxon_batch)
    stage.commit()
    if root_taxon_id not in ids:
        raise ValueError("Could not build the Fungi ancestry from the iNaturalist taxonomy archive")
    return imported, ids


def _prepend_header(header: str, stream):
    yield header
    yield from stream


def _link_taxon_parents(session: Session, stage: sqlite3.Connection, count: int) -> None:
    cursor = stage.execute("SELECT inat_taxon_id, parent_inat_taxon_id FROM fungal_taxa WHERE parent_inat_taxon_id IS NOT NULL")
    while rows := cursor.fetchmany(500):
        if session.get_bind().dialect.name == "postgresql":
            values = ",".join(f"(:child{i}, :parent{i})" for i in range(len(rows)))
            params = {}
            for index, (child_id, parent_id) in enumerate(rows):
                params[f"child{index}"] = child_id
                params[f"parent{index}"] = parent_id
            session.execute(
                text(
                    "UPDATE taxa AS child SET parent_id = parent.id "
                    "FROM taxa AS parent JOIN (VALUES " + values + ") AS lineage(child_external_id, parent_external_id) "
                    "ON parent.inat_taxon_id = lineage.parent_external_id "
                    "WHERE child.inat_taxon_id = lineage.child_external_id"
                ),
                params,
            )
        else:
            for child_id, parent_id in rows:
                session.execute(
                    text("UPDATE taxa SET parent_id=(SELECT id FROM taxa WHERE inat_taxon_id=:parent) WHERE inat_taxon_id=:child"),
                    {"child": child_id, "parent": parent_id},
                )
        session.commit()


def _insert_alias_file(session: Session, path: str | None) -> int:
    if not path:
        return 0
    count = 0
    batch = []
    for row in stream_tsv(path, ("taxon_id", "name")):
        taxon = session.scalar(select(Taxon).where(Taxon.inat_taxon_id == int(row["taxon_id"])))
        if not taxon or not row["name"].strip():
            continue
        batch.append({"taxon_id": taxon.id, "name": row["name"].strip(), "source": row.get("source", "iNaturalist").strip() or "iNaturalist"})
        if len(batch) >= 500:
            _upsert_aliases(session, batch)
            count += len(batch)
            batch = []
    if batch:
        _upsert_aliases(session, batch)
        count += len(batch)
    return count


def _upsert_aliases(session: Session, rows: list[dict]) -> None:
    for row in rows:
        exists = session.scalar(
            select(TaxonAlias.id).where(
                TaxonAlias.taxon_id == row["taxon_id"],
                TaxonAlias.name == row["name"],
                TaxonAlias.source == row["source"],
            )
        )
        if not exists:
            session.add(TaxonAlias(**row))
    session.commit()


def _stage_observations(path: str, stage: sqlite3.Connection, fungal_ids: set[int]) -> int:
    stage.execute("CREATE TABLE fungal_observations (uuid TEXT PRIMARY KEY, taxon_id INTEGER NOT NULL, observer_id TEXT)")
    stage.execute("CREATE INDEX ix_fungal_observations_taxon ON fungal_observations(taxon_id)")
    batch: list[tuple[str, int, str]] = []
    count = 0
    for row in stream_tsv(path, ("observation_uuid", "taxon_id", "observer_id")):
        try:
            taxon_id = int(row["taxon_id"])
        except ValueError:
            continue
        if taxon_id not in fungal_ids:
            continue
        batch.append((row["observation_uuid"], taxon_id, row["observer_id"]))
        if len(batch) >= 10_000:
            stage.executemany("INSERT OR REPLACE INTO fungal_observations VALUES (?,?,?)", batch)
            stage.commit()
            count += len(batch)
            logger.info("staged %d fungal observations", count)
            batch.clear()
    if batch:
        stage.executemany("INSERT OR REPLACE INTO fungal_observations VALUES (?,?,?)", batch)
        stage.commit()
        count += len(batch)
    return count


def _stage_dna_observations(path: str, stage: sqlite3.Connection) -> int:
    stage.execute("CREATE TABLE dna_observations (uuid TEXT PRIMARY KEY)")
    batch: list[tuple[str]] = []
    count = 0
    for row in stream_tsv(path, ("observation_uuid",)):
        observation_uuid = row["observation_uuid"].strip()
        if not observation_uuid:
            continue
        batch.append((observation_uuid,))
        if len(batch) >= 10_000:
            stage.executemany("INSERT OR IGNORE INTO dna_observations VALUES (?)", batch)
            stage.commit()
            count += len(batch)
            logger.info("staged %d iNaturalist DNA-verified observations", count)
            batch.clear()
    if batch:
        stage.executemany("INSERT OR IGNORE INTO dna_observations VALUES (?)", batch)
        stage.commit()
        count += len(batch)
    if count == 0:
        raise ValueError("The iNaturalist DNA-observation list was empty")
    return count


def _deactivate_observations_without_dna(session: Session, stage: sqlite3.Connection, started: datetime) -> int:
    """Hide existing photos for fungal observations no longer carrying the ITS field."""
    cursor = stage.execute(
        "SELECT o.uuid FROM fungal_observations o "
        "LEFT JOIN dna_observations d ON d.uuid=o.uuid "
        "WHERE d.uuid IS NULL"
    )
    count = 0
    while rows := cursor.fetchmany(500):
        observation_uuids = [str(row[0]) for row in rows if row[0]]
        if not observation_uuids:
            continue
        session.execute(
            update(Image)
            .where(
                Image.source == "inaturalist",
                Image.source_observation_id.in_(observation_uuids),
            )
            .values(active=False, dna_verified=False, updated_at=started)
        )
        session.commit()
        count += len(observation_uuids)
        if count % 50_000 < len(observation_uuids):
            logger.info("checked %d fungal observations without ITS evidence", count)
    return count


def import_bulk(
    session: Session,
    taxa_path: str,
    observations_path: str,
    observers_path: str,
    photos_path: str,
    *,
    dna_observations_path: str,
    stage_dir: str = "/var/lib/images-import",
    aliases_path: str | None = None,
    taxonomy_archive: str | None = None,
    full_reconciliation: bool = False,
    root_taxon_id: int = settings.fungi_taxon_id,
) -> int:
    started = begin_sync(session, "inaturalist")
    processed = 0
    stage_root = Path(stage_dir)
    try:
        if full_reconciliation and not taxonomy_archive:
            raise ValueError("Full reconciliation requires the current iNaturalist taxonomy DwC-A archive")
        stage_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="inat-", dir=stage_root) as temp_dir:
            stage_path = Path(temp_dir) / "join.sqlite3"
            stage = sqlite3.connect(stage_path)
            stage.execute("PRAGMA journal_mode=OFF")
            stage.execute("PRAGMA synchronous=OFF")
            stage.execute("CREATE TABLE fungal_taxa (inat_taxon_id INTEGER PRIMARY KEY, parent_inat_taxon_id INTEGER, ancestry TEXT NOT NULL)")
            fungal_ids: set[int] = set()
            taxa_batch: list[dict] = []
            taxa_count = 0
            if taxonomy_archive:
                dwca_count, dwca_ids = import_dwca_taxonomy(session, taxonomy_archive, stage, started, root_taxon_id)
                fungal_ids.update(dwca_ids)
                taxa_count += dwca_count
            for row in stream_tsv(taxa_path, ("taxon_id", "ancestry", "rank", "name", "active")):
                try:
                    external_id = int(row["taxon_id"])
                except ValueError:
                    continue
                ancestry_ids, path = normalize_ancestry(row["ancestry"], external_id)
                if external_id != root_taxon_id and root_taxon_id not in ancestry_ids:
                    continue
                parent_external_id = ancestry_ids[-1] if ancestry_ids else None
                fungal_ids.add(external_id)
                stage.execute("INSERT OR REPLACE INTO fungal_taxa VALUES (?,?,?)", (external_id, parent_external_id, path))
                taxa_batch.append({"id": external_id, "name": row["name"].strip(), "rank": row["rank"].strip(), "ancestry": path, "active": parse_bool(row["active"])})
                if len(taxa_batch) >= 1000:
                    _upsert_taxa(session, taxa_batch, started)
                    taxa_count += len(taxa_batch)
                    stage.commit()
                    logger.info("imported %d fungal taxa", taxa_count)
                    taxa_batch.clear()
            if taxa_batch:
                _upsert_taxa(session, taxa_batch, started)
                taxa_count += len(taxa_batch)
                stage.commit()
            if not fungal_ids:
                raise ValueError(f"No taxa descended from configured root taxon {root_taxon_id}")
            _link_taxon_parents(session, stage, taxa_count)
            stage.execute("ALTER TABLE fungal_taxa ADD COLUMN app_taxon_id INTEGER")
            taxon_mapping = session.execute(
                select(Taxon.inat_taxon_id, Taxon.id).where(
                    or_(Taxon.inat_taxon_id == root_taxon_id, Taxon.ancestry.like(f"%/{root_taxon_id}/%"))
                )
            ).yield_per(5000)
            mapping_batch = []
            for external_id, internal_id in taxon_mapping:
                mapping_batch.append((internal_id, external_id))
                if len(mapping_batch) >= 10_000:
                    stage.executemany("UPDATE fungal_taxa SET app_taxon_id=? WHERE inat_taxon_id=?", mapping_batch)
                    stage.commit()
                    mapping_batch.clear()
            if mapping_batch:
                stage.executemany("UPDATE fungal_taxa SET app_taxon_id=? WHERE inat_taxon_id=?", mapping_batch)
                stage.commit()
            stage.execute("CREATE INDEX ix_fungal_taxa_app_id ON fungal_taxa(app_taxon_id)")
            alias_count = _insert_alias_file(session, aliases_path)
            logger.info("loaded %d fungal taxa and %d supplied aliases", taxa_count, alias_count)

            obs_count = _stage_observations(observations_path, stage, fungal_ids)
            logger.info("staged %d fungal observations", obs_count)
            dna_observation_count = _stage_dna_observations(dna_observations_path, stage)
            logger.info("staged %d DNA-verified observation IDs", dna_observation_count)
            stage.execute("CREATE TABLE observers (observer_id TEXT PRIMARY KEY, login TEXT, name TEXT)")
            observer_batch = []
            observer_count = 0
            for row in stream_tsv(observers_path, ("observer_id", "login", "name")):
                observer_batch.append((row["observer_id"], row["login"], row["name"]))
                if len(observer_batch) >= 20_000:
                    stage.executemany("INSERT OR REPLACE INTO observers VALUES (?,?,?)", observer_batch)
                    stage.commit()
                    observer_count += len(observer_batch)
                    observer_batch.clear()
            if observer_batch:
                stage.executemany("INSERT OR REPLACE INTO observers VALUES (?,?,?)", observer_batch)
                stage.commit()
                observer_count += len(observer_batch)
            logger.info("staged %d observer attribution rows", observer_count)

            lookup = stage.cursor()
            image_batch = []
            image_count = 0
            taxon_conflicts: set[str] = set()
            metadata_conflicts: set[str] = set()
            for row in stream_tsv(photos_path, ("photo_id", "observation_uuid", "observer_id", "extension", "license")):
                try:
                    photo_id = str(int(row["photo_id"]))
                except ValueError:
                    continue
                license_code = row["license"].strip().casefold()
                if license_code not in DISPLAY_LICENSES:
                    continue
                extension = row["extension"].strip().lower().lstrip(".")
                if not re.fullmatch(r"[a-z0-9]{2,8}", extension):
                    continue
                obs = lookup.execute(
                    "SELECT o.taxon_id, o.observer_id, t.app_taxon_id FROM fungal_observations o "
                    "JOIN fungal_taxa t ON t.inat_taxon_id=o.taxon_id "
                    "JOIN dna_observations d ON d.uuid=o.uuid WHERE o.uuid=?",
                    (row["observation_uuid"],),
                ).fetchone()
                if not obs or obs[2] is None:
                    continue
                observer_id = row["observer_id"] or obs[1]
                observer = lookup.execute("SELECT name, login FROM observers WHERE observer_id=?", (observer_id,)).fetchone()
                photographer = ((observer[0] if observer else "") or (observer[1] if observer else "") or "").strip() or None
                display_license = DISPLAY_LICENSE_LABELS[license_code]
                if license_code == "cc0" and photographer:
                    attribution = f"{photographer}, no rights reserved (CC0 1.0)"
                elif license_code == "cc-pdm" and photographer:
                    attribution = f"{photographer}, Public Domain Mark 1.0"
                elif photographer and license_code in {"cc-by", "cc-by-sa"}:
                    attribution = f"© {photographer}, some rights reserved ({display_license})"
                else:
                    attribution = display_license
                medium = f"https://{DATASET_HOST}/photos/{photo_id}/medium.{extension}"
                thumbnail = f"https://{DATASET_HOST}/photos/{photo_id}/small.{extension}"
                if urlparse(medium).hostname != DATASET_HOST:
                    continue
                image_batch.append(
                    {
                        "taxon_id": obs[2],
                        "source_taxon_id": None,
                        "source": "inaturalist",
                        "source_image_id": photo_id,
                        "source_observation_id": row["observation_uuid"],
                        "medium_url": medium,
                        "thumbnail_url": thumbnail,
                        "source_page_url": f"https://www.inaturalist.org/observations/{row['observation_uuid']}",
                        "photographer": photographer,
                        "license": display_license,
                        "attribution": attribution,
                        "display_eligible": True,
                        "dna_verified": True,
                        "active": True,
                        "source_last_seen_at": started,
                        "updated_at": started,
                    }
                )
                if len(image_batch) >= 500:
                    unique_count, _ = upsert_inaturalist_images(
                        session,
                        image_batch,
                        taxon_conflicts=taxon_conflicts,
                        metadata_conflicts=metadata_conflicts,
                    )
                    session.commit()
                    image_count += unique_count
                    logger.info("imported %d licensed fungal images", image_count)
                    image_batch.clear()
            if image_batch:
                unique_count, _ = upsert_inaturalist_images(
                    session,
                    image_batch,
                    taxon_conflicts=taxon_conflicts,
                    metadata_conflicts=metadata_conflicts,
                )
                session.commit()
                image_count += unique_count

            processed = taxa_count + obs_count + image_count
            if full_reconciliation:
                unverified_observations = _deactivate_observations_without_dna(session, stage, started)
                logger.info("deactivated images for %d fungal observations without ITS evidence", unverified_observations)
                session.execute(
                    update(Taxon)
                    .where(
                        or_(Taxon.inat_taxon_id == root_taxon_id, Taxon.ancestry.like(f"%/{root_taxon_id}/%")),
                        Taxon.last_synced_at < started,
                    )
                    .values(active=False)
                )
                session.execute(
                    update(Image)
                    .where(
                        Image.source == "inaturalist",
                        Image.dna_verified.is_(False),
                    )
                    .values(active=False)
                )
                session.execute(
                    update(Image)
                    .where(
                        Image.source == "inaturalist",
                        or_(Image.source_last_seen_at.is_(None), Image.source_last_seen_at < started - timedelta(days=45)),
                    )
                    .values(active=False)
                )
            refresh_image_counts(session)
            session.commit()
            stage.close()
        finish_sync(session, "inaturalist", started, processed, cursor=started.isoformat())
        return processed
    except BaseException as exc:
        fail_sync(session, "inaturalist", started, exc)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream and import iNaturalist Open Data metadata")
    parser.add_argument("--taxa", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--observers", required=True)
    parser.add_argument("--photos", required=True)
    parser.add_argument("--dna-observations", required=True, help="TSV of iNaturalist observation UUIDs with the DNA Barcode ITS field")
    parser.add_argument("--aliases")
    parser.add_argument("--taxonomy-dwca", help="iNaturalist taxonomy Darwin Core Archive ZIP")
    parser.add_argument("--stage-dir", default="/var/lib/images-import")
    parser.add_argument("--root-taxon-id", type=int, default=settings.fungi_taxon_id)
    parser.add_argument("--full-reconciliation", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock = Path(args.stage_dir) / "inaturalist.lock"
    with import_lock(lock), SessionLocal() as session:
        count = import_bulk(
            session,
            args.taxa,
            args.observations,
            args.observers,
            args.photos,
            dna_observations_path=args.dna_observations,
            stage_dir=args.stage_dir,
            aliases_path=args.aliases,
            taxonomy_archive=args.taxonomy_dwca,
            full_reconciliation=args.full_reconciliation,
            root_taxon_id=args.root_taxon_id,
        )
        logger.info("iNaturalist import done (%d rows/records)", count)


if __name__ == "__main__":
    main()
