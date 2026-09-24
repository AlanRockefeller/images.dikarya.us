from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
    refresh_image_counts,
    upsert_rows,
)
from images_app.importers.inaturalist import (
    DISPLAY_LICENSES,
    DISPLAY_LICENSE_LABELS,
    normalize_ancestry,
    upsert_inaturalist_images,
)
from images_app.models import Image, SyncState, Taxon, utcnow

logger = logging.getLogger("images_app.importers.inaturalist_incremental")
API_URL = "https://api.inaturalist.org/v1/observations"
USER_AGENT = "DikaryaImages/0.1 (https://images.dikarya.us; taxonomy/image metadata synchronization)"
PAGE_SIZE = 200
MAX_REQUESTS_PER_RUN = 1000


def _cursor_from_state(
    session: Session, started: datetime
) -> tuple[str, int, str, str | None, bool]:
    state = session.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
    if state and state.cursor:
        try:
            data = json.loads(state.cursor)
            if isinstance(data, dict) and data.get("since"):
                id_above = int(data.get("id_above", 0))
                phase = data.get("phase")
                if phase not in {"window", "resume", "replay"}:
                    # Older partial cursors were an ID continuation without a
                    # replay phase; replay the same update window once caught up.
                    phase = "resume" if id_above else "window"
                    legacy = True
                else:
                    legacy = False
                return (
                    str(data["since"]),
                    id_above,
                    phase,
                    str(data["replay_started"]) if data.get("replay_started") else None,
                    legacy,
                )
            if isinstance(data, str):
                return data, 0, "window", None, True
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    if state and state.last_success_at:
        last_success = state.last_success_at
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        return (last_success - timedelta(days=1)).isoformat(), 0, "window", None, False
    return (started - timedelta(days=365)).isoformat(), 0, "window", None, False


def _in_fungi(taxon: dict, root_id: int) -> tuple[bool, str, list[int]]:
    try:
        taxon_id = int(taxon["id"])
    except (KeyError, TypeError, ValueError):
        return False, "", []
    parents, path = normalize_ancestry(str(taxon.get("ancestry") or ""), taxon_id)
    return taxon_id == root_id or root_id in parents, path, parents


def _taxon_ids_for_page(session: Session, items: list[dict], root_id: int, started: datetime) -> dict[int, int]:
    candidates: dict[int, tuple[dict, str, list[int]]] = {}
    taxon_ids: set[int] = set()
    for observation in items:
        taxon = observation.get("taxon") or {}
        try:
            taxon_ids.add(int(taxon["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not taxon_ids:
        return {}
    existing = {
        row.inat_taxon_id: row
        for row in session.scalars(select(Taxon).where(Taxon.inat_taxon_id.in_(list(taxon_ids)))).all()
    }
    for observation in items:
        taxon = observation.get("taxon") or {}
        try:
            external_id = int(taxon["id"])
        except (KeyError, TypeError, ValueError):
            continue
        allowed, ancestry, parents = _in_fungi(taxon, root_id)
        if not allowed and external_id in existing:
            stored = existing[external_id]
            allowed = stored.inat_taxon_id == root_id or f"/{root_id}/" in (stored.ancestry or "")
            if allowed:
                taxon = {
                    **taxon,
                    "name": taxon.get("name") or stored.scientific_name,
                    "rank": taxon.get("rank") or stored.rank,
                    "ancestry": taxon.get("ancestry") or stored.ancestry,
                }
                ancestry = stored.ancestry or f"/{external_id}/"
                parents = [int(value) for value in ancestry.strip("/").split("/") if value.isdigit()][:-1]
        if allowed:
            candidates[external_id] = (taxon, ancestry, parents)
    if not candidates:
        return {}
    rows = []
    for external_id, (taxon, ancestry, parents) in candidates.items():
        rows.append(
            {
                "inat_taxon_id": external_id,
                "parent_id": None,
                "scientific_name": str(taxon.get("name") or f"iNaturalist taxon {external_id}")[:300],
                "rank": str(taxon.get("rank") or "")[:60] or None,
                "ancestry": ancestry,
                "active": True,
                "last_synced_at": started,
                "updated_at": started,
            }
        )
    upsert_rows(session, Taxon, rows, ["inat_taxon_id"], ["scientific_name", "rank", "ancestry", "active", "last_synced_at", "updated_at"])
    session.flush()
    internal = dict(
        session.execute(
            select(Taxon.inat_taxon_id, Taxon.id).where(Taxon.inat_taxon_id.in_(list(candidates)))
        ).all()
    )
    parent_by_child = {
        external_id: parents[-1]
        for external_id, (_, _, parents) in candidates.items()
        if parents and parents[-1] != external_id
    }
    for child_id, parent_external_id in parent_by_child.items():
        parent_id = internal.get(parent_external_id) or session.scalar(
            select(Taxon.id).where(Taxon.inat_taxon_id == parent_external_id)
        )
        if parent_id:
            session.execute(update(Taxon).where(Taxon.inat_taxon_id == child_id).values(parent_id=parent_id))
    session.commit()
    return internal


def _medium_url(photo_url: str) -> tuple[str, str] | None:
    parsed = urlparse(photo_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.hostname != "inaturalist-open-data.s3.amazonaws.com" and not parsed.hostname.endswith(".inaturalist.org"):
        return None
    if not re.search(r"/(?:square|small|thumb|medium)\.[A-Za-z0-9]{2,8}$", parsed.path):
        return None
    medium_path = re.sub(r"/(?:square|small|thumb|medium)\.", "/medium.", parsed.path)
    thumb_path = re.sub(r"/(?:square|small|thumb|medium)\.", "/small.", parsed.path)
    return urlunparse(parsed._replace(path=medium_path)), urlunparse(parsed._replace(path=thumb_path))


def _make_image(photo: dict, observation: dict, taxon_id: int, seen_at: datetime) -> dict | None:
    license_code = str(photo.get("license_code") or "").casefold()
    if license_code not in DISPLAY_LICENSES:
        return None
    urls = _medium_url(str(photo.get("url") or ""))
    if not urls:
        return None
    try:
        photo_id = int(photo["id"])
        observation_id = int(observation["id"])
    except (KeyError, ValueError, TypeError):
        return None
    photo_user = photo.get("user") or {}
    observer = photo_user.get("name") or photo_user.get("login")
    if not observer:
        observer_user = observation.get("user") or {}
        observer = observer_user.get("name") or observer_user.get("login")
    observer = str(observer).strip() if observer else None
    attribution_raw = str(photo.get("attribution") or "").strip()
    display_license = DISPLAY_LICENSE_LABELS[license_code]
    if attribution_raw:
        attribution = attribution_raw
    elif observer and license_code == "cc0":
        attribution = f"{observer}, no rights reserved (CC0)"
    elif observer and license_code == "cc-pdm":
        attribution = f"{observer}, Public Domain Mark 1.0"
    elif observer:
        attribution = f"© {observer}, some rights reserved ({display_license})"
    else:
        attribution = display_license
    return {
        "taxon_id": taxon_id,
        "source_taxon_id": None,
        "source": "inaturalist",
        "source_image_id": str(photo_id),
        "source_observation_id": str(observation_id),
        "medium_url": urls[0],
        "thumbnail_url": urls[1],
        "source_page_url": f"https://www.inaturalist.org/observations/{observation_id}",
        "photographer": observer,
        "license": display_license,
        "attribution": attribution,
        "display_eligible": True,
        "active": True,
        "source_last_seen_at": seen_at,
        "updated_at": seen_at,
    }


def incremental_sync(session: Session, *, root_taxon_id: int = settings.fungi_taxon_id, max_requests: int = MAX_REQUESTS_PER_RUN, client: httpx.Client | None = None) -> int:
    started = utcnow()
    since, id_above, phase, replay_started, legacy_cursor = _cursor_from_state(session, started)
    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        if legacy_cursor and phase == "window":
            # Legacy successful cursors stored the run start without the overlap.
            since_dt -= timedelta(days=1)
        since = since_dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        since = (started - timedelta(days=365)).isoformat()
        id_above = 0
        phase = "window"
        replay_started = None
    if phase == "replay" and replay_started is None:
        replay_started = started.isoformat()
    started = begin_sync(session, "inaturalist")
    processed = 0
    page_requests = 0
    last_id = id_above
    complete = False
    taxon_conflicts: set[str] = set()
    metadata_conflicts: set[str] = set()
    owns_client = client is None
    http = client or httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        while page_requests < max_requests:
            params = {
                "taxon_id": root_taxon_id,
                "updated_since": since,
                "per_page": PAGE_SIZE,
                "order_by": "id",
                "order": "asc",
                "id_above": last_id,
            }
            response = http.get(API_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("Unexpected iNaturalist observations API response")
            page_requests += 1
            if not results:
                complete = True
                break
            id_map = _taxon_ids_for_page(session, results, root_taxon_id, started)
            observation_ids = []
            image_rows = []
            touched_taxa: set[int] = set()
            for observation in results:
                try:
                    obs_id = int(observation["id"])
                except (KeyError, ValueError, TypeError):
                    continue
                observation_ids.append(str(obs_id))
                try:
                    ext_taxon = int((observation.get("taxon") or {}).get("id"))
                except (KeyError, ValueError, TypeError):
                    continue
                internal_taxon = id_map.get(ext_taxon)
                if not internal_taxon:
                    continue
                for photo in observation.get("photos") or []:
                    image = _make_image(photo, observation, internal_taxon, started)
                    if image:
                        image_rows.append(image)
            if observation_ids:
                touched_taxa.update(
                    taxon_id
                    for taxon_id in session.scalars(
                        select(Image.taxon_id)
                        .where(
                            Image.source == "inaturalist",
                            Image.source_observation_id.in_(observation_ids),
                            Image.taxon_id.is_not(None),
                        )
                        .distinct()
                    ).all()
                    if taxon_id is not None
                )
                session.execute(
                    update(Image)
                    .where(Image.source == "inaturalist", Image.source_observation_id.in_(observation_ids))
                    .values(active=False, updated_at=started)
                )
            if image_rows:
                _, image_taxa = upsert_inaturalist_images(
                    session,
                    image_rows,
                    taxon_conflicts=taxon_conflicts,
                    metadata_conflicts=metadata_conflicts,
                )
                touched_taxa.update(image_taxa)
            refresh_image_counts(session, touched_taxa)
            processed += len(results)
            page_ids = [int(item["id"]) for item in results if str(item.get("id", "")).isdigit()]
            if not page_ids:
                raise ValueError("iNaturalist returned a page with no numeric observation IDs")
            last_id = max(last_id, max(page_ids))
            session.commit()
            logger.info("iNaturalist incremental batch %d: %d observations (%d total)", page_requests, len(results), processed)
            if len(results) < PAGE_SIZE:
                complete = True
                break
            if page_requests < max_requests:
                time.sleep(1.05)
        if not complete:
            partial_phase = "resume" if phase == "window" else phase
            next_cursor = json.dumps(
                {
                    "version": 2,
                    "since": since,
                    "id_above": last_id,
                    "phase": partial_phase,
                    "replay_started": replay_started if partial_phase == "replay" else None,
                }
            )
            state = session.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
            state.status = "partial"
            state.last_started_at = started
            state.last_completed_at = utcnow()
            state.records_processed = processed
            state.cursor = next_cursor
            state.error_message = f"Paused at the {max_requests}-request run limit; will resume from cursor."
            session.commit()
            return processed

        if phase == "resume":
            # The ascending-ID continuation may have skipped low-ID observations
            # edited while the backlog was being paged. Replay this whole window
            # from ID zero before advancing its time cursor.
            state = session.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
            state.status = "partial"
            state.last_started_at = started
            state.last_completed_at = utcnow()
            state.records_processed = processed
            state.cursor = json.dumps(
                {
                    "version": 2,
                    "since": since,
                    "id_above": 0,
                    "phase": "replay",
                    "replay_started": None,
                }
            )
            state.error_message = "Backlog scan complete; replaying its full update window for changes to earlier IDs."
            session.commit()
            return processed

        completed_window_start = replay_started if phase == "replay" else started.isoformat()
        completed_dt = datetime.fromisoformat(completed_window_start.replace("Z", "+00:00"))
        if completed_dt.tzinfo is None:
            completed_dt = completed_dt.replace(tzinfo=timezone.utc)
        next_cursor = json.dumps(
            {
                "version": 2,
                "since": (completed_dt - timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                "id_above": 0,
                "phase": "window",
            }
        )
        finish_sync(session, "inaturalist", started, processed, cursor=next_cursor)
        return processed
    except BaseException as exc:
        fail_sync(session, "inaturalist", started, exc)
        raise
    finally:
        if owns_client:
            http.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally synchronize recent iNaturalist fungal observations")
    parser.add_argument("--root-taxon-id", type=int, default=settings.fungi_taxon_id)
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS_PER_RUN)
    parser.add_argument("--stage-dir", default="/var/lib/images-import")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with import_lock(Path(args.stage_dir) / "inaturalist.lock"), SessionLocal() as session:
        incremental_sync(session, root_taxon_id=args.root_taxon_id, max_requests=args.max_requests)


if __name__ == "__main__":
    main()
