"""Synchronize photo metadata from iNaturalist observations that carry an ITS barcode.

The observations API can filter on an observation field, so we only ever ask
for observations under the root taxa that have "DNA Barcode ITS" set. With 200
observations per page a full scan is roughly a thousand requests, paced at
just over one per second as iNaturalist's API guidelines ask.

* full scan: walks every matching observation by ascending ID. Progress is
  saved after each page so an interrupted scan resumes where it stopped. When
  a scan completes, photos that were not seen are deactivated (deleted
  observations, removed photos, removed ITS fields).
* incremental: asks only for observations updated since the last successful
  run and reconciles their photos.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from images_app import licenses
from images_app.config import settings
from images_app.db import SessionLocal
from images_app.models import Image, Taxon
from images_app.sync.http import PoliteClient
from images_app.sync.its import INAT_ITS_FIELD_NAME, inat_its_evidence
from images_app.sync.runs import Run, get_cursor, save_cursor

log = logging.getLogger(__name__)

JOB = "inaturalist"
API_URL = "https://api.inaturalist.org/v2/observations"
PAGE_SIZE = 200
MIN_INTERVAL = 1.1
PHOTO_HOSTS = {"inaturalist-open-data.s3.amazonaws.com", "static.inaturalist.org"}
FIELDS = ",".join(
    [
        "id",
        "observed_on",
        "faves_count",
        "taxon.id",
        "user.login",
        "user.name",
        "photos.id",
        "photos.url",
        "photos.license_code",
        "photos.attribution",
        "photos.original_dimensions.width",
        "photos.original_dimensions.height",
        "ofvs.field_id",
        "ofvs.name",
        "ofvs.value",
    ]
)
# Re-request a little before the previous run started, to cover clock skew and
# records that were being edited while that run was in progress.
INCREMENTAL_OVERLAP = timedelta(hours=1)
_ATTRIBUTION_NAME = re.compile(r"^\(c\)\s*(.+?),\s*(?:some|all|no) rights reserved", re.IGNORECASE)


def make_client(max_requests: int | None = None, **kwargs) -> PoliteClient:
    return PoliteClient(min_interval=MIN_INTERVAL, max_requests=max_requests, **kwargs)


def _base_params() -> dict:
    return {
        "taxon_id": ",".join(map(str, settings.root_taxon_ids)),
        f"field:{INAT_ITS_FIELD_NAME}": "",
        "per_page": PAGE_SIZE,
        "order_by": "id",
        "order": "asc",
        "fields": FIELDS,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def full_scan(run: Run, client: PoliteClient) -> None:
    """Scan every ITS observation, resuming an interrupted scan if there is one."""
    with SessionLocal() as db:
        cursor = get_cursor(db, JOB)
        progress = cursor.get("full_scan")
        if progress:
            log.info("resuming full scan from observation %s", progress["id_above"])
        else:
            progress = {"started_at": _now().isoformat(), "id_above": 0}
        scan_started = datetime.fromisoformat(progress["started_at"])
        taxa = _taxon_map(db)
        while True:
            params = _base_params() | {"id_above": progress["id_above"]}
            results = client.get(API_URL, params=params).json().get("results", [])
            if not results:
                break
            _store_observations(db, run, results, taxa)
            progress["id_above"] = max(obs["id"] for obs in results)
            cursor["full_scan"] = progress
            save_cursor(db, JOB, cursor)
            if run.stats.get("observations", 0) % 10_000 < PAGE_SIZE:
                log.info("full scan: %d observations so far", run.stats["observations"])
        missing = db.execute(
            text(
                """
                UPDATE images SET is_active = false, inactive_reason = 'missing_upstream',
                                  updated_at = now()
                WHERE source = 'inaturalist' AND last_seen_at < :started
                  AND (is_active OR inactive_reason IS DISTINCT FROM 'missing_upstream')
                """
            ),
            {"started": scan_started},
        ).rowcount
        run.count("deactivated_missing", missing)
        cursor.pop("full_scan", None)
        cursor["updated_since"] = (scan_started - INCREMENTAL_OVERLAP).isoformat()
        save_cursor(db, JOB, cursor)


def incremental(run: Run, client: PoliteClient) -> None:
    """Fetch observations updated since the last successful run.

    Falls back to a full scan when there is no previous run, or when the backlog
    is too large to page through within the request budget.
    """
    with SessionLocal() as db:
        cursor = get_cursor(db, JOB)
    if not cursor.get("updated_since") or cursor.get("full_scan"):
        log.info("no completed full scan yet; running a full scan instead")
        return full_scan(run, client)

    window_start = _now()
    since = cursor["updated_since"]
    id_above = 0
    with SessionLocal() as db:
        taxa = _taxon_map(db)
        while True:
            params = _base_params() | {"updated_since": since, "id_above": id_above}
            payload = client.get(API_URL, params=params).json()
            if id_above == 0:
                total = payload.get("total_results", 0)
                run.stats["updated_observations"] = total
                budget = client.max_requests or float("inf")
                if total / PAGE_SIZE > budget / 2:
                    log.info("%d updated observations; a full scan is cheaper", total)
                    return full_scan(run, client)
            results = payload.get("results", [])
            if not results:
                break
            observation_ids = _store_observations(db, run, results, taxa)
            # Photos removed from an updated observation are no longer listed in it.
            removed = db.execute(
                text(
                    """
                    UPDATE images SET is_active = false, inactive_reason = 'missing_upstream',
                                      updated_at = now()
                    WHERE source = 'inaturalist' AND source_observation_id = ANY(:ids)
                      AND last_seen_at < :started AND is_active
                    """
                ),
                {"ids": observation_ids, "started": run.started_at},
            ).rowcount
            run.count("deactivated_missing", removed)
            db.commit()
            id_above = max(obs["id"] for obs in results)
        cursor = get_cursor(db, JOB)
        cursor["updated_since"] = (window_start - INCREMENTAL_OVERLAP).isoformat()
        save_cursor(db, JOB, cursor)


def _taxon_map(db: Session) -> dict[int, int]:
    return dict(db.execute(select(Taxon.inat_taxon_id, Taxon.id).where(Taxon.is_active)).all())


def _store_observations(db: Session, run: Run, observations: list[dict], taxa: dict[int, int]) -> list[str]:
    rows: dict[str, dict] = {}
    observation_ids = []
    for obs in observations:
        observation_ids.append(str(obs["id"]))
        run.count("observations")
        for row in observation_rows(obs, taxa):
            rows[row["source_image_id"]] = row
            run.count("photos")
            if row["is_active"]:
                run.count("active_photos")
            else:
                run.count(f"inactive_{row['inactive_reason']}")
    run.records += len(observations)
    if rows:
        seen_at = _now()  # same clock as the scan start, which staleness is measured against
        values = [row | {"last_seen_at": seen_at} for row in rows.values()]
        statement = insert(Image).values(values)
        updatable = [c for c in values[0] if c not in ("source", "source_image_id")]
        db.execute(
            statement.on_conflict_do_update(
                constraint="uq_images_source_image",
                set_={column: statement.excluded[column] for column in updatable} | {"updated_at": func.now()},
            )
        )
    db.commit()
    return observation_ids


def observation_rows(obs: dict, taxa: dict[int, int]) -> list[dict]:
    """Translate one API observation into image rows (active or with a reason they are hidden)."""
    its = inat_its_evidence(obs)
    taxon_id = taxa.get((obs.get("taxon") or {}).get("id"))
    user = obs.get("user") or {}
    observer = (user.get("name") or "").strip() or user.get("login") or "Unknown"
    observed_on = _parse_date(obs.get("observed_on"))
    observation_url = f"https://www.inaturalist.org/observations/{obs['id']}"
    rows = []
    for position, photo in enumerate(obs.get("photos") or []):
        urls = _photo_urls(photo.get("url"))
        if not photo.get("id") or urls is None:
            continue
        license_code = licenses.normalize_inat(photo.get("license_code"))
        attribution = (photo.get("attribution") or "").strip()
        match = _ATTRIBUTION_NAME.match(attribution)
        dimensions = photo.get("original_dimensions") or {}
        if not its.present:
            reason = "no_its"
        elif not licenses.displayable(license_code):
            reason = "license"
        elif taxon_id is None:
            reason = "unknown_taxon"
        else:
            reason = None
        rows.append(
            {
                "source": "inaturalist",
                "source_image_id": str(photo["id"]),
                "source_observation_id": str(obs["id"]),
                "observation_url": observation_url,
                "taxon_id": taxon_id,
                "position": position,
                "thumb_url": urls[0],
                "medium_url": urls[1],
                "large_url": urls[2],
                "width": _positive_int(dimensions.get("width")),
                "height": _positive_int(dimensions.get("height")),
                "photographer": (match.group(1) if match else observer)[:300],
                "license_code": license_code,
                "attribution": attribution or f"© {observer}",
                "observed_on": observed_on,
                "faves": max(int(obs.get("faves_count") or 0), 0),
                "its_length": its.length,
                "is_active": reason is None,
                "inactive_reason": reason,
            }
        )
    return rows


def _photo_urls(url: str | None) -> tuple[str, str, str] | None:
    """Derive small/medium/large URLs from the square URL the API returns."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in PHOTO_HOSTS or "/square." not in parsed.path:
        return None
    return tuple(url.replace("/square.", f"/{size}.", 1) for size in ("small", "medium", "large"))


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _positive_int(value) -> int | None:
    return value if isinstance(value, int) and value > 0 else None
