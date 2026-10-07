"""Synchronize photo metadata from Mushroom Observer observations with ITS sequences.

1. Page through MO's sequences API and keep observations with an ITS locus.
2. Fetch those observations (with consensus name and images) by ID.
3. Map each MO consensus name to an iNaturalist taxon:
     exact accepted name -> MO synonym -> known alias -> unresolved
   Nothing is guessed: names with several plausible taxa are left "ambiguous"
   and, like unresolved names, wait in the admin review queue. The original MO
   name is stored on every image so mappings can be audited.

The whole set is small (about 15,000 observations), so every run is a full
refresh. MO asks API clients to be gentle; requests are spaced five seconds apart.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from images_app import licenses
from images_app.db import SessionLocal
from images_app.models import Image, SourceName, Taxon, TaxonName
from images_app.sync.http import PoliteClient
from images_app.sync.its import is_its_locus, sequence_length
from images_app.sync.runs import Run, check_staleness

log = logging.getLogger(__name__)

SOURCE = "mushroom_observer"
API = "https://mushroomobserver.org/api2"
NAMES_CSV = "https://mushroomobserver.org/names.csv"
MIN_INTERVAL = 5.0
OBSERVATIONS_PER_REQUEST = 100
_INAT_OBSERVATION = re.compile(r"inaturalist\.org/observations/(\d+)")
_RANK_CONNECTORS = re.compile(r"\s+(?:var|subsp|ssp|f|forma|subvar)\.\s+", re.IGNORECASE)
_UNMAPPABLE = re.compile(r"\b(?:group|sensu|s\.\s?l\.|s\.\s?s\.|cf\.|aff\.|nom\. prov\.)|['\"]", re.IGNORECASE)


def make_client(max_requests: int | None = None, **kwargs) -> PoliteClient:
    return PoliteClient(min_interval=MIN_INTERVAL, max_requests=max_requests, **kwargs)


def sync(run: Run, client: PoliteClient) -> None:
    its_observations = fetch_its_observations(client)
    run.stats["its_observations"] = len(its_observations)
    synonyms = fetch_name_synonyms(client)
    with SessionLocal() as db:
        inat_observations = set(
            db.scalars(
                select(Image.source_observation_id)
                .where(Image.source == "inaturalist", Image.is_active)
                .distinct()
            ).all()
        )
        ids = sorted(its_observations)
        for offset in range(0, len(ids), OBSERVATIONS_PER_REQUEST):
            chunk = ids[offset : offset + OBSERVATIONS_PER_REQUEST]
            payload = client.get(
                f"{API}/observations",
                params={"format": "json", "detail": "high", "id": ",".join(map(str, chunk))},
            ).json()
            _store_observations(db, run, payload.get("results", []), its_observations, inat_observations)
            if offset and offset % 2000 == 0:
                log.info("Mushroom Observer: %d of %d observations", offset, len(ids))
        check_staleness(db, SOURCE, run.started_at)
        missing = db.execute(
            update(Image)
            .where(Image.source == SOURCE, Image.last_seen_at < run.started_at, Image.is_active)
            .values(is_active=False, inactive_reason="missing_upstream")
        ).rowcount
        run.count("deactivated_missing", missing)
        db.commit()
        run.stats["names"] = map_names(db, synonyms)
        db.commit()


def fetch_its_observations(client: PoliteClient) -> dict[int, int]:
    """Observation ID -> ITS length (0 when only an accession number is recorded)."""
    found: dict[int, int] = {}
    page, pages = 1, 1
    while page <= pages:
        payload = client.get(
            f"{API}/sequences", params={"format": "json", "detail": "low", "page": page}
        ).json()
        pages = int(payload.get("number_of_pages") or 0)
        for sequence in payload.get("results", []):
            observation_id = sequence.get("observation_id")
            if not isinstance(observation_id, int) or not is_its_locus(sequence.get("locus")):
                continue
            length = sequence_length(sequence.get("bases"))
            if length or (sequence.get("accession") or "").strip():
                found[observation_id] = max(found.get(observation_id, 0), length)
        page += 1
    return found


def fetch_name_synonyms(client: PoliteClient) -> dict[str, list[str]]:
    """MO name text -> accepted MO names it is a synonym or misspelling of."""
    response = client.get(NAMES_CSV)
    reader = csv.DictReader(io.StringIO(response.text), delimiter="\t")
    by_id: dict[str, dict] = {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in reader:
        by_id[row["id"]] = row
        if row.get("synonym_id") not in (None, "", "NULL"):
            groups[row["synonym_id"]].append(row)
    result: dict[str, list[str]] = {}
    for row in by_id.values():
        accepted: list[str] = []
        spelling = by_id.get(row.get("correct_spelling_id") or "")
        if spelling is not None:
            accepted.append(spelling["text_name"])
        if row.get("deprecated") == "1":
            accepted.extend(
                other["text_name"]
                for other in groups.get(row.get("synonym_id") or "", [])
                if other["deprecated"] == "0" and other["id"] != row["id"]
            )
        if accepted:
            result[row["text_name"]] = accepted
    return result


def _store_observations(
    db: Session,
    run: Run,
    observations: list[dict],
    its_lengths: dict[int, int],
    inat_observations: set[str],
) -> None:
    rows: dict[str, dict] = {}  # keyed by image ID: one image can belong to several observations
    for obs in observations:
        run.count("observations")
        run.records += 1
        consensus = obs.get("consensus") or {}
        if not consensus.get("id"):
            continue
        source_name = _upsert_source_name(db, consensus)
        linked_inat = {match for link in obs.get("external_links") or []
                       for match in _INAT_OBSERVATION.findall(str(link.get("url") or ""))}
        duplicate = bool(linked_inat & inat_observations)
        for position, image in enumerate(obs.get("images") or []):
            if not isinstance(image.get("id"), int):
                continue
            license_code = licenses.normalize_mo(image.get("license"))
            owner = image.get("owner") or {}
            photographer = (
                (image.get("copyright_holder") or "").strip()
                or owner.get("legal_name")
                or owner.get("login_name")
                or "Unknown"
            )[:300]
            if duplicate:
                reason = "duplicate"
            elif not licenses.displayable(license_code):
                reason = "license"
            elif source_name.status != "mapped":
                reason = "unmapped_name"
            else:
                reason = None
            run.count("photos")
            run.count("active_photos" if reason is None else f"inactive_{reason}")
            base = "https://mushroomobserver.org/images"
            rows[str(image["id"])] = (
                {
                    "source": SOURCE,
                    "source_image_id": str(image["id"]),
                    "source_observation_id": str(obs["id"]),
                    "observation_url": f"https://mushroomobserver.org/obs/{obs['id']}",
                    "taxon_id": source_name.taxon_id,
                    "source_name_id": source_name.id,
                    "position": position,
                    "thumb_url": f"{base}/320/{image['id']}.jpg",
                    "medium_url": f"{base}/640/{image['id']}.jpg",
                    "large_url": f"{base}/1280/{image['id']}.jpg",
                    "photographer": photographer,
                    "license_code": license_code,
                    "attribution": f"© {photographer}, {licenses.label(license_code)}",
                    "observed_on": _parse_date(obs.get("date")),
                    "its_length": its_lengths.get(obs["id"]) or None,
                    "is_active": reason is None,
                    "inactive_reason": reason,
                }
            )
    if rows:
        seen_at = datetime.now(timezone.utc)  # compared with run.started_at, so use the same clock
        rows = [row | {"last_seen_at": seen_at} for row in rows.values()]
        statement = insert(Image).values(rows)
        db.execute(
            statement.on_conflict_do_update(
                constraint="uq_images_source_image",
                set_={c: statement.excluded[c] for c in rows[0] if c not in ("source", "source_image_id")}
                | {"updated_at": func.now()},
            )
        )
    db.commit()


def _upsert_source_name(db: Session, consensus: dict) -> SourceName:
    statement = insert(SourceName).values(
        source=SOURCE,
        source_name_id=str(consensus["id"]),
        name=str(consensus.get("name") or "")[:300],
        author=(consensus.get("author") or None),
        rank=(consensus.get("rank") or None),
    )
    return db.scalars(
        statement.on_conflict_do_update(
            constraint="uq_source_names_source_id",
            set_={"name": statement.excluded.name, "author": statement.excluded.author,
                  "rank": statement.excluded.rank},
        )
        .returning(SourceName),
        execution_options={"populate_existing": True},
    ).one()


def _key(name: str) -> str:
    return _RANK_CONNECTORS.sub(" ", name.strip()).lower()


def resolve(name: str, rank: str | None, synonyms: list[str], db: Session) -> tuple[str, str | None, list[int], str | None]:
    """Decide how an MO name maps to iNat taxa: (status, method, candidate taxon IDs, note)."""
    if _UNMAPPABLE.search(name):
        return "unresolved", None, [], "MO name is a group, sensu or provisional name"

    def exact(text_name: str) -> list[Taxon]:
        return list(
            db.scalars(
                select(Taxon).where(Taxon.is_active, func.lower(Taxon.name) == _key(text_name))
            ).all()
        )

    matches = exact(name)
    if len(matches) == 1:
        return "mapped", "exact", [matches[0].id], None
    if len(matches) > 1:
        same_rank = [taxon for taxon in matches if rank and taxon.rank == rank]
        if len(same_rank) == 1:
            return "mapped", "rank", [same_rank[0].id], None
        return "ambiguous", None, [t.id for t in matches], "several iNaturalist taxa share this name"

    via_synonym = {taxon.id for accepted in synonyms for taxon in exact(accepted)}
    if len(via_synonym) == 1:
        return "mapped", "synonym", list(via_synonym), None
    if via_synonym:
        return "ambiguous", None, sorted(via_synonym), "MO synonyms match several iNaturalist taxa"

    aliases = set(
        db.scalars(
            select(TaxonName.taxon_id)
            .join(Taxon, Taxon.id == TaxonName.taxon_id)
            .where(Taxon.is_active, TaxonName.kind != "common", func.lower(TaxonName.name) == _key(name))
        ).all()
    )
    if len(aliases) == 1:
        return "mapped", "alias", list(aliases), None
    if aliases:
        return "ambiguous", None, sorted(aliases), "known aliases match several iNaturalist taxa"
    return "unresolved", None, [], None


def map_names(db: Session, synonyms: dict[str, list[str]]) -> dict[str, int]:
    """(Re)map every MO name not mapped by hand, then refresh its images."""
    totals: dict[str, int] = defaultdict(int)
    names = db.scalars(
        select(SourceName).where(SourceName.source == SOURCE, SourceName.method.is_distinct_from("manual"))
    ).all()
    for source_name in names:
        status, method, candidates, note = resolve(
            source_name.name, source_name.rank, synonyms.get(source_name.name, []), db
        )
        source_name.status = status
        source_name.method = method
        source_name.note = note
        source_name.candidate_taxon_ids = candidates
        source_name.taxon_id = candidates[0] if status == "mapped" else None
        totals[status] += 1
    db.flush()
    apply_name_mappings(db)
    return dict(totals)


def set_manual_mapping(db: Session, source_name: SourceName, taxon: Taxon) -> None:
    source_name.taxon_id = taxon.id
    source_name.status = "mapped"
    source_name.method = "manual"
    source_name.note = None
    db.execute(
        insert(TaxonName)
        .values(taxon_id=taxon.id, name=source_name.name, kind="synonym", source=SOURCE)
        .on_conflict_do_nothing()
    )
    db.flush()
    apply_name_mappings(db, source_name.id)


def apply_name_mappings(db: Session, source_name_id: int | None = None) -> None:
    """Copy each MO name's mapped taxon onto its images and (de)activate them accordingly.

    Images hidden for another reason (license, duplicate, gone upstream) still
    record the taxon but stay hidden.
    """
    db.execute(
        text(
            """
            UPDATE images AS i
            SET taxon_id = n.taxon_id,
                is_active = CASE WHEN i.inactive_reason IS NULL OR i.inactive_reason = 'unmapped_name'
                                 THEN n.status = 'mapped' ELSE i.is_active END,
                inactive_reason = CASE WHEN i.inactive_reason IS NULL OR i.inactive_reason = 'unmapped_name'
                                       THEN CASE WHEN n.status = 'mapped' THEN NULL ELSE 'unmapped_name' END
                                       ELSE i.inactive_reason END,
                updated_at = now()
            FROM source_names AS n
            WHERE i.source_name_id = n.id
              AND i.source = :source
              AND (CAST(:only_id AS bigint) IS NULL OR n.id = :only_id)
              AND (i.taxon_id IS DISTINCT FROM n.taxon_id
                   OR ((i.inactive_reason IS NULL OR i.inactive_reason = 'unmapped_name')
                       AND i.is_active IS DISTINCT FROM (n.status = 'mapped')))
            """
        ),
        {"source": SOURCE, "only_id": source_name_id},
    )


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None
