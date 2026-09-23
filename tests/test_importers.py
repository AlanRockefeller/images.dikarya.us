from __future__ import annotations

import csv
import gzip
import io
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from images_app.importers.inaturalist import import_bulk
from images_app.importers.inaturalist_incremental import incremental_sync
from images_app.importers.mushroom_observer import import_exports
import images_app.importers.common as importer_common
from images_app.importers.common import stream_tsv
from images_app.models import Image, SourceTaxon, SyncState, Taxon, TaxonAlias, User, Vote


def write_tsv(path, headers, rows, gz=False):
    opener = gzip.open if gz else open
    with opener(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
    return str(path)


def test_inaturalist_bulk_import_is_idempotent(database, tmp_path):
    taxa = write_tsv(tmp_path / "taxa.csv.gz", ["taxon_id", "ancestry", "rank", "name", "active"], [[47170, "1", "kingdom", "Fungi", "true"], [555001, "1/47170", "species", "Amanita muscaria", "true"], [999999, "1/2", "species", "Animal", "true"]], True)
    observations = write_tsv(tmp_path / "observations.csv.gz", ["observation_uuid", "observer_id", "taxon_id", "quality_grade"], [["obs-fungi", "15", "555001", "research"], ["obs-animal", "16", "999999", "research"]], True)
    observers = write_tsv(tmp_path / "observers.csv.gz", ["observer_id", "login", "name"], [["15", "mushroomfan", "Mushroom Fan"]], True)
    photos = write_tsv(tmp_path / "photos.csv.gz", ["photo_uuid", "photo_id", "observation_uuid", "observer_id", "extension", "license"], [["photo-1", "987654", "obs-fungi", "15", "jpeg", "cc-by"], ["photo-pdm", "987655", "obs-fungi", "15", "jpeg", "cc-pdm"], ["photo-2", "999999", "obs-animal", "16", "jpeg", "cc-by"]], True)
    for _ in range(2):
        with database() as db:
            import_bulk(db, taxa, observations, observers, photos, stage_dir=str(tmp_path / "stage"))
    with database() as db:
        assert db.scalar(select(func.count(Taxon.id))) == 2
        assert db.scalar(select(func.count(Image.id))) == 2
        image = db.scalar(select(Image).where(Image.source_image_id == "987654"))
        assert image.source_image_id == "987654"
        assert image.license == "CC BY"
        assert image.photographer == "Mushroom Fan"
        public_domain = db.scalar(select(Image).where(Image.source_image_id == "987655"))
        assert public_domain.license == "Public Domain Mark 1.0"
        state = db.scalar(select(SyncState).where(SyncState.source == "inaturalist"))
        assert state.status == "success"


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
    observations = write_tsv(tmp_path / "observations.tsv", ["observation_uuid", "observer_id", "taxon_id"], [["obs-a", "15", "555001"]])
    observers = write_tsv(tmp_path / "observers.tsv", ["observer_id", "login", "name"], [["15", "fungi_user", "Fungi User"]])
    photos = write_tsv(tmp_path / "photos.tsv", ["photo_id", "observation_uuid", "observer_id", "extension", "license"], [["99001", "obs-a", "15", "jpeg", "cc-by-sa"]])
    kwargs = dict(taxa_path=taxa, observations_path=observations, observers_path=observers, photos_path=photos, stage_dir=str(tmp_path / "stage"), taxonomy_archive=str(dwca), full_reconciliation=True)
    for _ in range(2):
        with database() as db:
            import_bulk(db, **kwargs)
    with database() as db:
        taxa_by_external = {taxon.inat_taxon_id: taxon for taxon in db.scalars(select(Taxon)).all()}
        assert set(taxa_by_external) == {444, 47170, 555001, 999999}
        assert taxa_by_external[555001].parent_id == taxa_by_external[444].id
        assert not taxa_by_external[999999].active
        assert db.scalar(select(func.count(Image.id))) == 1


def test_incremental_inaturalist_sync_uses_mock_api_and_persists_cursor(database, taxon):
    def respond(request):
        assert request.url.params.get("per_page") == "200"
        payload = {
            "results": [
                {
                    "id": 700001,
                    "taxon": {"id": 555001, "name": "Amanita muscaria", "rank": "species", "ancestry": "1/47170"},
                    "user": {"login": "fungi_user", "name": "Fungi User"},
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
        assert count == 1
        assert image.taxon_id == taxon
        assert image.medium_url.endswith("/medium.jpeg")
        assert state.status == "success"
        assert '"since"' in state.cursor


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
    image_observations = write_tsv(tmp_path / "images_observations.csv", ["image_id", "observation_id"], [["900", "800"], ["901", "801"]])
    images = write_tsv(
        tmp_path / "images.csv",
        ["id", "content_type", "copyright_holder", "license", "ok_for_export"],
        [["900", "image/jpeg", "Photo Person", "Creative Commons Wikipedia Compatible v3.0", "1"], ["901", "image/jpeg", "Unknown Person", "All rights reserved", "1"]],
    )
    kwargs = dict(observations_path=observations, image_observations_path=image_observations, images_path=images, names_path=names, stage_dir=str(tmp_path / "stage"))
    for _ in range(2):
        with database() as db:
            import_exports(db, **kwargs)
    with database() as db:
        assert db.scalar(select(func.count(Image.id))) == 2
        assert db.scalar(select(func.count(SourceTaxon.id))) == 2
        unresolved = db.scalar(select(SourceTaxon).where(SourceTaxon.original_name == "Mystery fungus"))
        assert unresolved.mapping_status == "unresolved"
        resolved = db.scalar(select(SourceTaxon).where(SourceTaxon.original_name == "Amanita muscaria"))
        assert resolved.taxon_id == accepted_id
        eligible = db.scalar(select(Image).where(Image.source_image_id == "900", Image.source == "mushroom_observer"))
        excluded = db.scalar(select(Image).where(Image.source_image_id == "901", Image.source == "mushroom_observer"))
        assert eligible.display_eligible and eligible.active, (eligible.taxon_id, eligible.source_taxon_id, resolved.id)
        assert not excluded.display_eligible and not excluded.active


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
        import_exports(db, observations_path=observations, image_observations_path=links, images_path=images, names_path=names, stage_dir=str(tmp_path / "stage"))
    with database() as db:
        source_taxon = db.scalar(select(SourceTaxon).where(SourceTaxon.source_taxon_id == "102"))
        assert source_taxon.mapping_method == "known_taxon_alias"
        assert source_taxon.taxon_id == accepted_id


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
