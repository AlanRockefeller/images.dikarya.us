from __future__ import annotations

import argparse
import logging
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from images_app.config import settings
from images_app.db import SessionLocal
from images_app.importers.common import (
    begin_sync,
    delete_unverified_images_without_votes,
    fail_sync,
    finish_sync,
    import_lock,
    refresh_image_counts,
)
from images_app.importers.inaturalist import (
    OBSERVATIONS_API_URL,
    _link_taxon_parents,
    dna_barcode_its_status,
    import_dwca_taxonomy,
    upsert_inaturalist_images,
)
from images_app.importers.inaturalist_incremental import _make_image
from images_app.models import Image, Taxon

logger = logging.getLogger("images_app.importers.inaturalist_reconcile")
USER_AGENT = "DikaryaImages/0.1 (https://images.dikarya.us; DNA-verified metadata reconciliation)"
PAGE_SIZE = 200
MAX_API_REQUESTS = 4000
MAX_REQUEST_ATTEMPTS = 6


def _iter_observation_pages(
    client: httpx.Client,
    *,
    root_taxon_id: int,
    request_interval: float,
) -> Iterator[list[dict]]:
    last_id = 0
    api_requests = 0
    page_number = 0
    while True:
        params = {
            "taxon_id": root_taxon_id,
            "field:DNA Barcode ITS": "",
            "per_page": PAGE_SIZE,
            "order_by": "id",
            "order": "asc",
            "id_above": last_id,
        }
        response = None
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            if api_requests >= MAX_API_REQUESTS:
                raise RuntimeError("iNaturalist DNA-filtered reconciliation reached its request limit")
            api_requests += 1
            try:
                response = client.get(OBSERVATIONS_API_URL, params=params)
            except httpx.TransportError as exc:
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise
                delay = max(request_interval, min(60.0, 2.0**attempt))
                logger.warning(
                    "iNaturalist connection failed (%s); retrying in %.1fs",
                    exc.__class__.__name__,
                    delay,
                )
                time.sleep(delay)
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                delay = (
                    max(float(retry_after), request_interval)
                    if retry_after
                    else max(request_interval, min(60.0, 2.0**attempt))
                )
            except ValueError:
                delay = max(request_interval, min(60.0, 2.0**attempt))
            logger.warning("iNaturalist API returned %d; retrying in %.1fs", response.status_code, delay)
            time.sleep(delay)
        if response is None:
            raise RuntimeError("No response from iNaturalist API")
        response.raise_for_status()
        results = response.json().get("results")
        if not isinstance(results, list):
            raise ValueError("Unexpected iNaturalist observations API response")
        if not results:
            return

        page_ids = []
        for observation in results:
            if not isinstance(observation, dict):
                continue
            try:
                page_ids.append(int(observation["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not page_ids or max(page_ids) <= last_id:
            raise ValueError("iNaturalist API pagination made no forward progress")
        last_id = max(page_ids)
        page_number += 1
        yield [observation for observation in results if isinstance(observation, dict)]
        if page_number % 100 == 0:
            logger.info("read %d iNaturalist DNA-filtered observation pages", page_number)
        if len(results) < PAGE_SIZE:
            return
        time.sleep(request_interval)


def synchronize_dna_images(
    session: Session,
    taxonomy_archive: str,
    *,
    root_taxon_id: int = settings.fungi_taxon_id,
    stage_dir: str = "/var/lib/images-import",
    client: httpx.Client | None = None,
    request_interval: float = 2.0,
) -> int:
    """Reconcile fungal taxonomy and images with an ITS sequence or record pointer."""
    started = begin_sync(session, "inaturalist")
    stage_root = Path(stage_dir)
    owns_client = client is None
    http = client or httpx.Client(
        timeout=60,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    processed = 0
    try:
        stage_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="inat-dna-", dir=stage_root) as temp_dir:
            stage = sqlite3.connect(Path(temp_dir) / "taxonomy.sqlite3")
            try:
                stage.execute(
                    "CREATE TABLE fungal_taxa (inat_taxon_id INTEGER PRIMARY KEY, "
                    "parent_inat_taxon_id INTEGER, ancestry TEXT NOT NULL)"
                )
                taxa_count, _ = import_dwca_taxonomy(
                    session, taxonomy_archive, stage, started, root_taxon_id
                )
                _link_taxon_parents(session, stage, taxa_count)
            finally:
                stage.close()

        taxon_conflicts: set[str] = set()
        metadata_conflicts: set[str] = set()
        changed_taxa: set[int] = set()
        observation_count = 0
        image_count = 0
        field_counts = {
            "sequence": 0,
            "observation_reference": 0,
            "empty": 0,
            "missing": 0,
        }
        image_batch: list[dict] = []

        for page in _iter_observation_pages(
            http,
            root_taxon_id=root_taxon_id,
            request_interval=request_interval,
        ):
            eligible_page = []
            for observation in page:
                status = dna_barcode_its_status(observation)
                field_counts[status] += 1
                if status in {"sequence", "observation_reference"}:
                    eligible_page.append(observation)
            observation_count += len(eligible_page)
            external_ids: set[int] = set()
            for observation in eligible_page:
                try:
                    external_ids.add(int((observation.get("taxon") or {}).get("id")))
                except (TypeError, ValueError):
                    continue
            taxon_map = {}
            if external_ids:
                taxon_map = dict(
                    session.execute(
                        select(Taxon.inat_taxon_id, Taxon.id).where(
                            Taxon.inat_taxon_id.in_(external_ids),
                            Taxon.active.is_(True),
                        )
                    ).all()
                )

            for observation in eligible_page:
                try:
                    external_taxon_id = int((observation.get("taxon") or {}).get("id"))
                except (TypeError, ValueError):
                    continue
                internal_taxon_id = taxon_map.get(external_taxon_id)
                if internal_taxon_id is None:
                    continue
                for photo in observation.get("photos") or []:
                    image = _make_image(photo, observation, internal_taxon_id, started)
                    if image is None:
                        continue
                    image_batch.append(image)
                    changed_taxa.add(internal_taxon_id)
                    if len(image_batch) >= 500:
                        inserted, touched = upsert_inaturalist_images(
                            session,
                            image_batch,
                            taxon_conflicts=taxon_conflicts,
                            metadata_conflicts=metadata_conflicts,
                        )
                        session.commit()
                        image_count += inserted
                        changed_taxa.update(touched)
                        image_batch.clear()

            processed += len(page)
            if processed and processed % 10_000 < len(page):
                logger.info(
                    "processed %d DNA-verified fungal observations and %d licensed images",
                    processed,
                    image_count + len(image_batch),
                )

        if observation_count == 0:
            raise ValueError("iNaturalist returned no fungal observations with an ITS sequence or observation pointer")
        if image_batch:
            inserted, touched = upsert_inaturalist_images(
                session,
                image_batch,
                taxon_conflicts=taxon_conflicts,
                metadata_conflicts=metadata_conflicts,
            )
            session.commit()
            image_count += inserted
            changed_taxa.update(touched)

        # Only a completed field-filtered scan establishes which old images
        # are no longer eligible. Earlier batches can safely commit without
        # exposing stale records from observations seen later in the scan.
        session.execute(
            update(Taxon)
            .where(
                or_(Taxon.inat_taxon_id == root_taxon_id, Taxon.ancestry.like(f"%/{root_taxon_id}/%")),
                Taxon.last_synced_at < started,
            )
            .values(active=False, updated_at=started)
        )
        session.execute(
            update(Image)
            .where(
                Image.source == "inaturalist",
                or_(Image.source_last_seen_at.is_(None), Image.source_last_seen_at < started),
            )
            .values(active=False, dna_verified=False, updated_at=started)
        )
        delete_unverified_images_without_votes(session, "inaturalist")
        refresh_image_counts(session)
        session.commit()
        processed += image_count
        finish_sync(session, "inaturalist", started, processed, cursor=started.isoformat())
        logger.info(
            "iNaturalist ITS field values: %d sequences, %d observation pointers, %d empty, %d missing",
            field_counts["sequence"],
            field_counts["observation_reference"],
            field_counts["empty"],
            field_counts["missing"],
        )
        logger.info(
            "iNaturalist DNA-only reconciliation completed: %d eligible observations, %d licensed photos",
            observation_count,
            image_count,
        )
        return processed
    except BaseException as exc:
        fail_sync(session, "inaturalist", started, exc)
        raise
    finally:
        if owns_client:
            http.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile fungal taxonomy and ITS-qualified image metadata from iNaturalist"
    )
    parser.add_argument("--taxonomy-dwca", required=True)
    parser.add_argument("--stage-dir", default="/var/lib/images-import")
    parser.add_argument("--root-taxon-id", type=int, default=settings.fungi_taxon_id)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with import_lock(Path(args.stage_dir) / "inaturalist.lock"), SessionLocal() as session:
        synchronize_dna_images(
            session,
            args.taxonomy_dwca,
            root_taxon_id=args.root_taxon_id,
            stage_dir=args.stage_dir,
        )


if __name__ == "__main__":
    main()
