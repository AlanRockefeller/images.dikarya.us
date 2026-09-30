from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from images_app.importers.inaturalist import import_bulk, upsert_inaturalist_images
from images_app.importers.inaturalist_incremental import incremental_sync
from images_app.importers.mushroom_observer import (
    _sqlite_stage,
    _write_mo_source_taxa,
    fetch_mo_dna_observation_ids,
    import_exports,
    recompute_mo_image_taxa,
)
import images_app.importers.common as importer_common
from images_app.importers.common import delete_unverified_images_without_votes, stream_tsv
from images_app.models import Image, ImageSourceTaxon, SourceTaxon, SyncState, Taxon, TaxonAlias, User, Vote
from images_app.importers.inaturalist_reconcile import _iter_observation_pages, synchronize_dna_images


def write_tsv(path, headers, rows, gz=False):
    opener = gzip.open if gz else open
    with opener(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
    return str(path)


def test_inaturalist_bulk_import_is_idempotent(database, tmp_path):
    taxa = write_tsv(tmp_path / "taxa.csv.gz", ["taxon_id", "ancestry", "rank", "name", "active"], [[47170, "1", "kingdom", "Fungi", "true"], [555001, "1/47170", "species", "Amanita muscaria", "true"], [555002, "1/47170", "species", "Boletus edulis", "true"], [999999, "1/2", "species", "Animal", "true"]], True)
    observations = write_tsv(tmp_path / "observations.csv.gz", ["observation_uuid", "observer_id", "taxon_id", "quality_grade"], [["obs-fungi", "15", "555001", "research"], ["obs-conflict", "15", "555002", "research"], ["obs-no-dna", "15", "555001", "research"], ["obs-animal", "16", "999999", "research"]], True)
    observers = write_tsv(tmp_path / "observers.csv.gz", ["observer_id", "login", "name"], [["15", "mushroomfan", "Mushroom Fan"]], True)
    photos = write_tsv(tmp_path / "photos.csv.gz", ["photo_uuid", "photo_id", "observation_uuid", "observer_id", "extension", "license"], [["photo-1", "987654", "obs-fungi", "15", "jpeg", "cc-by"], ["photo-pdm", "987655", "obs-fungi", "15", "jpeg", "cc-pdm"], ["photo-shared-a", "987656", "obs-fungi", "15", "jpeg", "cc-by"], ["photo-shared-b", "987656", "obs-conflict", "15", "jpeg", "cc-by"], ["photo-no-dna", "987657", "obs-no-dna", "15", "jpeg", "cc-by"], ["photo-2", "999999", "obs-animal", "16", "jpeg", "cc-by"]], True)
    dna_observations = write_tsv(tmp_path / "dna-observations.tsv", ["observation_uuid"], [["obs-fungi"], ["obs-conflict"]])
    for _ in range(2):
        with database() as db:
            import_bulk(db, taxa, observations, observers, photos, dna_observations_path=dna_observations, stage_dir=str(tmp_path / "stage"))
    with database() as db:
        assert db.scalar(select(func.count(Taxon.id))) == 3
        assert db.scalar(select(func.count(Image.id))) == 3
        assert db.scalar(select(Image.id).where(Image.source_image_id == "987657")) is None
        image = db.scalar(select(Image).where(Image.source_image_id == "987654"))
        assert image.source_image_id == "987654"
        assert image.license == "CC BY"
        assert image.photographer == "Mushroom Fan"
        public_domain = db.scalar(select(Image).where(Image.source_image_id == "987655"))
        assert public_domain.license == "Public Domain Mark 1.0"
        shared = db.scalar(select(Image).where(Image.source_image_id == "987656"))
        assert shared.taxon_id is None
        assert not shared.active
        safe_taxon_id = db.scalar(select(Taxon.id).where(Taxon.inat_taxon_id == 555001))
        upsert_inaturalist_images(
            db,
            [
                {
                    "taxon_id": safe_taxon_id,
                    "source_taxon_id": None,
                    "source": "inaturalist",
                    "source_image_id": "987656",
                    "source_observation_id": "single-association-on-incremental-page",
                    "medium_url": shared.medium_url,
                    "thumbnail_url": shared.thumbnail_url,
                    "source_page_url": "https://www.inaturalist.org/observations/single-association-on-incremental-page",
                    "photographer": shared.photographer,
                    "license": shared.license,
                    "attribution": shared.attribution,
                    "display_eligible": True,
                    "active": True,
                    "source_last_seen_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            ],
            taxon_conflicts=set(),
            metadata_conflicts=set(),
        )
        db.refresh(shared)
        assert shared.taxon_id is None and not shared.active
        state = db.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
        assert state.status == "success"


def test_inaturalist_full_reconcile_fetches_only_its_observations_and_prunes_old_rows(database, taxon, tmp_path):
    archive_path = tmp_path / "taxonomy.dwca.zip"
    rows = [
        ["47170", "https://www.inaturalist.org/taxa/47170", "", "Fungi", "Fungi", "kingdom", ""],
        ["444", "https://www.inaturalist.org/taxa/444", "https://www.inaturalist.org/taxa/47170", "Fungi", "Amanita", "genus", ""],
        ["555001", "https://www.inaturalist.org/taxa/555001", "https://www.inaturalist.org/taxa/444", "Fungi", "Amanita muscaria", "species", ""],
    ]
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        data = io.StringIO()
        writer = csv.writer(data, lineterminator="\n")
        writer.writerow(["id", "taxonID", "parentNameUsageID", "kingdom", "scientificName", "taxonRank", "references"])
        writer.writerows(rows)
        archive.writestr("taxa.csv", data.getvalue())
    with database() as db:
        db.add_all(
            [
                Image(
                    source="inaturalist",
                    source_image_id="legacy-unfiltered-image",
                    source_observation_id="legacy-observation",
                    medium_url="https://static.inaturalist.org/photos/1/medium.jpg",
                    source_page_url="https://www.inaturalist.org/observations/legacy-observation",
                    taxon_id=taxon,
                    display_eligible=True,
                    dna_verified=True,
                    active=True,
                ),
                Image(
                    source="inaturalist",
                    source_image_id="990001",
                    source_observation_id="old-conflicting-observation",
                    medium_url="https://static.inaturalist.org/photos/990001/medium.jpg",
                    source_page_url="https://www.inaturalist.org/observations/old-conflicting-observation",
                    taxon_id=None,
                    display_eligible=False,
                    dna_verified=False,
                    active=False,
                ),
            ]
        )
        db.commit()

    def respond(request):
        assert request.url.params["taxon_id"] == "47170"
        assert request.url.params["field:DNA Barcode ITS"] == ""
        assert request.url.params["per_page"] == "200"
        assert request.url.params["id_above"] == "0"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 700001,
                        "uuid": "dna-observation",
                        "taxon": {"id": 555001},
                        "user": {"login": "fungi-user", "name": "Fungi User"},
                        "ofvs": [{"field_id": 2330, "value": "ACGT"}],
                        "photos": [
                            {
                                "id": 990001,
                                "url": "https://static.inaturalist.org/photos/990001/square.jpg",
                                "license_code": "cc-by",
                                "attribution": "© Fungi User, CC BY",
                            },
                            {
                                "id": 990002,
                                "url": "https://static.inaturalist.org/photos/990002/square.jpg",
                                "license_code": None,
                            },
                        ],
                    },
                    {
                        "id": 700002,
                        "uuid": "blank-field",
                        "taxon": {"id": 555001},
                        "ofvs": [{"field_id": 2330, "value": "  "}],
                        "photos": [],
                    },
                    {
                        "id": 700003,
                        "uuid": "different-field",
                        "taxon": {"id": 555001},
                        "ofvs": [{"field_id": 999, "name": "DNA Barcode ITS", "value": "ACGT"}],
                        "photos": [],
                    },
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    for _ in range(2):
        with database() as db:
            synchronize_dna_images(
                db,
                str(archive_path),
                client=client,
                request_interval=0,
                stage_dir=str(tmp_path / "stage"),
            )
    client.close()
    with database() as db:
        assert db.scalar(select(func.count(Image.id))) == 1
        image = db.scalar(select(Image).where(Image.source_image_id == "990001"))
        assert image.dna_verified and image.active
        assert image.source_observation_id == "dna-observation"
        assert db.scalar(select(Image.id).where(Image.source_image_id == "990002")) is None
        assert db.scalar(select(Image.id).where(Image.source_image_id == "legacy-unfiltered-image")) is None
        assert db.get(Taxon, taxon).image_count == 1


def test_unverified_cleanup_keeps_any_image_with_a_vote(database, taxon):
    with database() as db:
        user = User(name="Voter", email="voter@example.org", password_hash="test-hash")
        db.add(user)
        db.flush()
        unvoted = Image(
            source="inaturalist",
            source_image_id="unverified-unvoted",
            medium_url="https://static.inaturalist.org/photos/1/medium.jpg",
            source_page_url="https://www.inaturalist.org/observations/1",
            taxon_id=taxon,
            dna_verified=False,
            display_eligible=True,
            active=True,
        )
        voted = Image(
            source="inaturalist",
            source_image_id="unverified-voted",
            medium_url="https://static.inaturalist.org/photos/2/medium.jpg",
            source_page_url="https://www.inaturalist.org/observations/2",
            taxon_id=taxon,
            dna_verified=False,
            display_eligible=True,
            active=True,
        )
        db.add_all([voted, unvoted] if voted_first else [unvoted, voted])
        db.flush()
        db.add(Vote(user_id=user.id, taxon_id=taxon, image_id=voted.id, rank=1))
        db.commit()
        unvoted_id, voted_id = unvoted.id, voted.id

        assert delete_unverified_images_without_votes(db, "inaturalist", batch_size=1) == 1
        assert db.get(Image, unvoted_id) is None
        kept = db.get(Image, voted_id)
        assert kept is not None and not kept.active and not kept.dna_verified
        assert db.scalar(select(func.count(Vote.id)).where(Vote.image_id == voted_id)) == 1
        db.refresh(kept)
        kept_updated_at = kept.updated_at
        assert delete_unverified_images_without_votes(db, "inaturalist", batch_size=1) == 0
        db.refresh(kept)
        assert kept.updated_at == kept_updated_at


def test_inaturalist_reconcile_retries_connection_drop_and_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr("images_app.importers.inaturalist_reconcile.time.sleep", sleeps.append)
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("connection closed")
        if calls == 2:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert list(_iter_observation_pages(client, root_taxon_id=47170, request_interval=2)) == []
    finally:
        client.close()

    assert calls == 3
    assert sleeps == [2, 3]


def test_full_reconciliation_imports_dwca_taxonomy_and_marks_missing_taxa_inactive(database, tmp_path):
    with database() as db:
        db.add(Taxon(inat_taxon_id=999999, scientific_name="Old inactive name", rank="species", ancestry="/1/47170/999999/", active=True, last_synced_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        db.commit()
    dwca = tmp_path / "taxonomy.dwca.zip"
    rows = [
        ["47170", "https://www.inaturalist.org/taxa/47170", "", "Fungi", "Fungi", "kingdom", ""],
        ["444", "https://www.inaturalist.org/taxa/444", "https://www.inaturalist.org/taxa/47170", "Fungi", "Amanita", "genus", ""],
        ["555001", "https://www.inaturalist.org/taxa/555001", "https://www.inaturalist.org/taxa/444", "Fungi", "Amanita muscaria", "species", ""],
        ["777", "https://www.inaturalist.org/taxa/777", "https://www.inaturalist.org/taxa/1", "Animalia", "Animalia", "kingdom", ""],
    ]
    with zipfile.ZipFile(dwca, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        data = io.StringIO()
        writer = csv.writer(data, lineterminator="\n")
        writer.writerow(["id", "taxonID", "parentNameUsageID", "kingdom", "scientificName", "taxonRank", "references"])
        writer.writerows(rows)
        archive.writestr("taxa.csv", data.getvalue())
    taxa = write_tsv(tmp_path / "taxa.tsv", ["taxon_id", "ancestry", "rank", "name", "active"], [[47170, "1", "kingdom", "Fungi", "true"], [555001, "1/47170/444", "species", "Amanita muscaria", "true"]])
    observations = write_tsv(tmp_path / "observations.tsv", ["observation_uuid", "observer_id", "taxon_id"], [["obs-a", "15", "555001"], ["obs-no-dna", "15", "555001"]])
    observers = write_tsv(tmp_path / "observers.tsv", ["observer_id", "login", "name"], [["15", "fungi_user", "Fungi User"]])
    photos = write_tsv(tmp_path / "photos.tsv", ["photo_id", "observation_uuid", "observer_id", "extension", "license"], [["99001", "obs-a", "15", "jpeg", "cc-by-sa"]])
    dna_observations = write_tsv(tmp_path / "dwca-dna-observations.tsv", ["observation_uuid"], [["obs-a"]])
    kwargs = dict(taxa_path=taxa, observations_path=observations, observers_path=observers, photos_path=photos, dna_observations_path=dna_observations, stage_dir=str(tmp_path / "stage"), taxonomy_archive=str(dwca), full_reconciliation=True)
    for run in range(2):
        with database() as db:
            import_bulk(db, **kwargs)
            if run == 0:
                db.add(
                    Image(
                        source="inaturalist",
                        source_image_id="old-image-with-removed-ITS-field",
                        source_observation_id="obs-no-dna",
                        medium_url="https://inaturalist-open-data.s3.amazonaws.com/photos/old/medium.jpg",
                        source_page_url="https://www.inaturalist.org/observations/obs-no-dna",
                        display_eligible=True,
                        dna_verified=True,
                        active=True,
                        taxon_id=db.scalar(select(Taxon.id).where(Taxon.inat_taxon_id == 555001)),
                    )
                )
                db.commit()
    with database() as db:
        taxa_by_external = {taxon.inat_taxon_id: taxon for taxon in db.scalars(select(Taxon)).all()}
        assert set(taxa_by_external) == {444, 47170, 555001, 999999}
        assert taxa_by_external[555001].parent_id == taxa_by_external[444].id
        assert not taxa_by_external[999999].active
        assert db.scalar(select(func.count(Image.id))) == 2
        old_image = db.scalar(select(Image).where(Image.source_image_id == "old-image-with-removed-ITS-field"))
        assert not old_image.active and not old_image.dna_verified


def test_incremental_inaturalist_sync_uses_mock_api_and_persists_cursor(database, taxon):
    def respond(request):
        assert request.url.params.get("per_page") == "200"
        payload = {
            "results": [
                {
                    "id": 700001,
                    "uuid": "obs-uuid-700001",
                    "taxon": {"id": 555001, "name": "Amanita muscaria", "rank": "species", "ancestry": "1/47170"},
                    "user": {"login": "fungi_user", "name": "Fungi User"},
                    "ofvs": [{"field_id": 2330, "name": "DNA Barcode ITS", "value": "ACGT"}],
                    "photos": [
                        {
                            "id": 880001,
                            "url": "https://inaturalist-open-data.s3.amazonaws.com/photos/880001/square.jpeg",
                            "license_code": "cc-by",
                            "attribution": "© Fungi User, CC BY",
                        }
                    ],
                }
            ]
        }
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with database() as db:
        count = incremental_sync(db, client=client)
    client.close()
    with database() as db:
        image = db.scalar(select(Image).where(Image.source_image_id == "880001"))
        state = db.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
        imported_taxon = db.get(Taxon, taxon)
        assert count == 1
        assert image.taxon_id == taxon
        assert image.dna_verified
        assert image.source_observation_id == "obs-uuid-700001"
        assert image.medium_url.endswith("/medium.jpeg")
        assert state.status == "success"
        assert '"since"' in state.cursor
        assert imported_taxon.image_count == 1


def test_incremental_inaturalist_hides_existing_image_when_its_field_is_removed(database, taxon):
    with database() as db:
        db.add(
            Image(
                source="inaturalist",
                source_image_id="880002",
                source_observation_id="obs-uuid-700002",
                medium_url="https://inaturalist-open-data.s3.amazonaws.com/photos/880002/medium.jpeg",
                source_page_url="https://www.inaturalist.org/observations/700002",
                display_eligible=True,
                dna_verified=True,
                active=True,
                taxon_id=taxon,
            )
        )
        db.commit()

    def respond(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 700002,
                        "uuid": "obs-uuid-700002",
                        "taxon": {"id": 555001, "name": "Amanita muscaria", "rank": "species", "ancestry": "1/47170"},
                        "photos": [],
                        "ofvs": [],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with database() as db:
        incremental_sync(db, client=client)
    client.close()
    with database() as db:
        image = db.scalar(select(Image).where(Image.source_image_id == "880002"))
        assert not image.active
        assert image.dna_verified


def test_incremental_inaturalist_replays_window_after_capped_backlog(database, taxon):
    request_ids = []

    def respond(request):
        id_above = int(request.url.params["id_above"])
        request_ids.append(id_above)
        if len(request_ids) == 1:
            results = [
                {
                    "id": observation_id,
                    "taxon": {
                        "id": 555001,
                        "name": "Amanita muscaria",
                        "rank": "species",
                        "ancestry": "1/47170",
                    },
                    "photos": [],
                }
                for observation_id in range(1, 201)
            ]
        else:
            results = []
        return httpx.Response(200, json={"results": results})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with database() as db:
        assert incremental_sync(db, client=client, max_requests=1) == 200
    with database() as db:
        cursor = json.loads(db.scalar(select(SyncState.cursor).where(SyncState.source == "inaturalist")))
        assert cursor["phase"] == "resume"
        assert cursor["id_above"] == 200
        assert incremental_sync(db, client=client, max_requests=1) == 0
    with database() as db:
        cursor = json.loads(db.scalar(select(SyncState.cursor).where(SyncState.source == "inaturalist")))
        assert cursor["phase"] == "replay"
        assert cursor["id_above"] == 0
        assert incremental_sync(db, client=client, max_requests=1) == 0
    client.close()
    assert request_ids == [0, 200, 0]
    with database() as db:
        state = db.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
        assert state.status == "success"
        assert json.loads(state.cursor)["phase"] == "window"


def test_mushroom_observer_import_idempotency_and_unresolved_name(database, tmp_path):
    with database() as db:
        root = Taxon(inat_taxon_id=47170, scientific_name="Fungi", rank="kingdom", ancestry="/1/47170/", active=True)
        db.add(root)
        db.flush()
        accepted = Taxon(inat_taxon_id=555001, parent_id=root.id, scientific_name="Amanita muscaria", rank="species", ancestry="/1/47170/555001/", active=True)
        db.add(accepted)
        db.commit()
        accepted_id = accepted.id
    names = write_tsv(
        tmp_path / "names.csv",
        ["id", "text_name", "author", "deprecated", "correct_spelling_id", "synonym_id", "rank"],
        [["100", "Amanita muscaria", "(L.) Lam.", "0", "NULL", "NULL", "400"], ["101", "Mystery fungus", "Auth.", "0", "NULL", "NULL", "400"]],
    )
    observations = write_tsv(tmp_path / "observations.csv", ["id", "name_id"], [["800", "100"], ["801", "101"]])
    image_observations = write_tsv(tmp_path / "images_observations.csv", ["image_id", "observation_id"], [["900", "800"], ["901", "801"], ["902", "800"], ["902", "801"]])
    images = write_tsv(
        tmp_path / "images.csv",
        ["id", "content_type", "copyright_holder", "license", "ok_for_export"],
        [["900", "image/jpeg", "Photo Person", "Creative Commons Wikipedia Compatible v3.0", "1"], ["901", "image/jpeg", "Unknown Person", "All rights reserved", "1"], ["902", "image/jpeg", "Shared Photographer", "Creative Commons Wikipedia Compatible v3.0", "1"]],
    )
    kwargs = dict(observations_path=observations, image_observations_path=image_observations, images_path=images, names_path=names, dna_observation_ids={"800", "801"}, stage_dir=str(tmp_path / "stage"))
    for _ in range(2):
        with database() as db:
            import_exports(db, **kwargs)
    with database() as db:
        assert db.scalar(select(func.count(Image.id))) == 3
        assert db.scalar(select(func.count(SourceTaxon.id))) == 2
        unresolved = db.scalar(select(SourceTaxon).where(SourceTaxon.original_name == "Mystery fungus"))
        assert unresolved.mapping_status == "unresolved"
        resolved = db.scalar(select(SourceTaxon).where(SourceTaxon.original_name == "Amanita muscaria"))
        assert resolved.taxon_id == accepted_id
        eligible = db.scalar(select(Image).where(Image.source_image_id == "900", Image.source == "mushroom_observer"))
        excluded = db.scalar(select(Image).where(Image.source_image_id == "901", Image.source == "mushroom_observer"))
        shared = db.scalar(select(Image).where(Image.source_image_id == "902", Image.source == "mushroom_observer"))
        assert eligible.display_eligible and eligible.active, (eligible.taxon_id, eligible.source_taxon_id, resolved.id)
        assert not excluded.display_eligible and not excluded.active
        assert shared.taxon_id is None and not shared.active
        assert db.scalar(select(func.count(ImageSourceTaxon.image_id)).where(ImageSourceTaxon.image_id == shared.id)) == 2
        unresolved.taxon_id = accepted_id
        unresolved.mapping_status = "mapped"
        unresolved.mapping_method = "manual"
        changed_taxa = recompute_mo_image_taxa(db, source_taxon_id=unresolved.id)
        from images_app.importers.common import refresh_image_counts
        refresh_image_counts(db, changed_taxa)
        db.commit()
        assert shared.taxon_id == accepted_id and shared.active
        assert db.get(Taxon, accepted_id).image_count == 2


def test_mushroom_observer_name_lookups_are_batched_below_sqlite_variable_limit(database, tmp_path):
    stage = _sqlite_stage(tmp_path / "many-mo-names.sqlite3")
    stage.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    names = [(str(index), f"Unmapped name {index}", "0", "", "") for index in range(1200)]
    observations = [(f"obs-{index}", str(index)) for index in range(1200)]
    links = [("shared-image", f"obs-{index}") for index in range(1200)]
    stage.executemany("INSERT INTO mo_names VALUES (?,?,?,?,?)", names)
    stage.executemany("INSERT INTO mo_observations VALUES (?,?)", observations)
    stage.executemany("INSERT INTO mo_dna_observations VALUES (?)", [(f"obs-{index}",) for index in range(1200)])
    stage.executemany("INSERT INTO image_observations VALUES (?,?)", links)
    stage.execute("INSERT INTO mo_images VALUES (?,?,?,?,?)", ("shared-image", "image/jpeg", "", "", "1"))
    stage.commit()
    with database() as db:
        _write_mo_source_taxa(db, stage, datetime.now(timezone.utc))
        assert db.scalar(select(func.count(SourceTaxon.id))) == 1200
    assert stage.execute("SELECT count(*) FROM mapped_names").fetchone()[0] == 1200
    stage.close()


def test_mushroom_observer_uses_known_taxon_alias(database, tmp_path):
    with database() as db:
        root = Taxon(inat_taxon_id=47170, scientific_name="Fungi", rank="kingdom", ancestry="/1/47170/", active=True)
        db.add(root)
        db.flush()
        accepted = Taxon(inat_taxon_id=555001, parent_id=root.id, scientific_name="Amanita muscaria", rank="species", ancestry="/1/47170/555001/", active=True)
        db.add(accepted)
        db.flush()
        db.add(TaxonAlias(taxon_id=accepted.id, name="Agaricus muscarius", source="iNaturalist"))
        db.commit()
        accepted_id = accepted.id
    names = write_tsv(tmp_path / "names.csv", ["id", "text_name", "author", "deprecated", "correct_spelling_id", "synonym_id", "rank"], [["102", "Agaricus muscarius", "Auth.", "1", "NULL", "NULL", "400"]])
    observations = write_tsv(tmp_path / "observations.csv", ["id", "name_id"], [["802", "102"]])
    links = write_tsv(tmp_path / "links.csv", ["image_id", "observation_id"], [["902", "802"]])
    images = write_tsv(tmp_path / "images.csv", ["id", "content_type", "copyright_holder", "license", "ok_for_export"], [["902", "image/jpeg", "Photo Person", "Creative Commons Wikipedia Compatible v3.0", "1"]])
    with database() as db:
        import_exports(db, observations_path=observations, image_observations_path=links, images_path=images, names_path=names, dna_observation_ids={"802"}, stage_dir=str(tmp_path / "stage"))
    with database() as db:
        source_taxon = db.scalar(select(SourceTaxon).where(SourceTaxon.source_taxon_id == "102"))
        assert source_taxon.mapping_method == "known_taxon_alias"
        assert source_taxon.taxon_id == accepted_id


def test_mushroom_observer_sequence_api_finds_observations_with_sequence_data():
    def respond(request):
        assert request.url.params["detail"] == "low"
        assert request.url.params["format"] == "json"
        assert request.url.params["page"] == "1"
        return httpx.Response(
            200,
            json={
                "number_of_pages": 1,
                "results": [
                    {"observation_id": 801, "bases": "ACGT", "accession": "AB123"},
                    {"observation_id": 802, "bases": "", "accession": ""},
                    {"observation_id": None, "bases": "ACGT", "accession": None},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    assert fetch_mo_dna_observation_ids(client=client, request_interval=0) == {"801"}
    client.close()


def test_mushroom_observer_filters_images_by_qualifying_observation(database, tmp_path):
    with database() as db:
        root = Taxon(inat_taxon_id=47170, scientific_name="Fungi", rank="kingdom", ancestry="/1/47170/", active=True)
        db.add(root)
        db.flush()
        accepted = Taxon(inat_taxon_id=555001, parent_id=root.id, scientific_name="Amanita muscaria", rank="species", ancestry="/1/47170/555001/", active=True)
        db.add(accepted)
        db.commit()
        accepted_id = accepted.id
    names = write_tsv(tmp_path / "dna-names.csv", ["id", "text_name", "author", "deprecated", "correct_spelling_id", "synonym_id", "rank"], [["100", "Amanita muscaria", "", "0", "", "", "400"], ["101", "Mystery fungus", "", "0", "", "", "400"]])
    observations = write_tsv(tmp_path / "dna-observations.csv", ["id", "name_id"], [["800", "100"], ["801", "101"]])
    links = write_tsv(tmp_path / "dna-links.csv", ["image_id", "observation_id"], [["900", "800"], ["901", "801"], ["902", "800"], ["902", "801"]])
    images = write_tsv(tmp_path / "dna-images.csv", ["id", "content_type", "copyright_holder", "license", "ok_for_export"], [[str(image_id), "image/jpeg", "Photo Person", "Creative Commons Wikipedia Compatible v3.0", "1"] for image_id in (900, 901, 902)])
    with database() as db:
        import_exports(db, observations_path=observations, image_observations_path=links, images_path=images, names_path=names, dna_observation_ids={"800"}, stage_dir=str(tmp_path / "stage"))
    with database() as db:
        assert db.scalar(select(func.count(Image.id)).where(Image.source == "mushroom_observer")) == 2
        excluded = db.scalar(select(Image).where(Image.source == "mushroom_observer", Image.source_image_id == "901"))
        assert excluded is None
        shared = db.scalar(select(Image).where(Image.source == "mushroom_observer", Image.source_image_id == "902"))
        assert shared.dna_verified and shared.active
        assert shared.taxon_id == accepted_id
        assert db.scalar(select(func.count(ImageSourceTaxon.image_id)).where(ImageSourceTaxon.image_id == shared.id)) == 1


def test_vote_history_remains_when_source_moves_image_to_another_taxon(database, taxon):
    with database() as db:
        first_taxon = db.get(Taxon, taxon)
        user = db.scalar(select(User).limit(1))
        if user is None:
            user = User(name="Voter", email="voter@example.test", password_hash="not-used")
            db.add(user)
            db.flush()
        new_taxon = Taxon(
            inat_taxon_id=555002,
            scientific_name="Amanita example",
            rank="species",
            ancestry=first_taxon.ancestry.rsplit("/", 2)[0] + "/555002/",
            active=True,
        )
        image = Image(
            taxon_id=first_taxon.id,
            source="inaturalist",
            source_image_id="move-test",
            medium_url="https://inaturalist-open-data.s3.amazonaws.com/photos/1/medium.jpg",
            source_page_url="https://www.inaturalist.org/observations/1",
            display_eligible=True,
            active=True,
        )
        db.add_all([new_taxon, image])
        db.flush()
        vote = Vote(user_id=user.id, taxon_id=first_taxon.id, image_id=image.id, rank=1)
        db.add(vote)
        db.commit()
        image.taxon_id = new_taxon.id
        db.commit()
        assert db.scalar(select(func.count(Vote.id)).where(Vote.image_id == image.id)) == 1


def test_https_gzip_source_is_streamed_and_redirect_hosts_are_checked(monkeypatch):
    compressed = gzip.compress(b"id\tname\n1\tAmanita\n")

    class Response:
        def __init__(self, final_url):
            self.url = httpx.URL(final_url)

        def raise_for_status(self):
            return None

        def iter_raw(self):
            yield compressed[:5]
            yield compressed[5:]

    @contextmanager
    def fake_stream(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["follow_redirects"] is True
        yield Response(url)

    monkeypatch.setattr(importer_common.httpx, "stream", fake_stream)
    rows = list(stream_tsv("https://inaturalist-open-data.s3.amazonaws.com/taxa.csv.gz", ("id",)))
    assert rows == [{"id": "1", "name": "Amanita"}]

    @contextmanager
    def redirected_stream(method, url, **kwargs):
        yield Response("https://attacker.example/data.csv.gz")

    monkeypatch.setattr(importer_common.httpx, "stream", redirected_stream)
    with pytest.raises(ValueError, match="redirected to an unsupported host"):
        list(stream_tsv("https://inaturalist-open-data.s3.amazonaws.com/taxa.csv.gz"))
