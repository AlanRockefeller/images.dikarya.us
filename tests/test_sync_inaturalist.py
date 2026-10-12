import httpx
import pytest
from sqlalchemy import select

from images_app.models import Image, SyncState, Vote
from images_app.sync import inaturalist
from images_app.sync.http import RequestBudgetExceeded
from images_app.sync.its import inat_its_evidence, is_its_locus, sequence_length
from images_app.sync.runs import get_cursor, tracked_run

SEQUENCE = "TCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATAAACTTGGTGTAGGCTGTTGCTGGCTCTTCGGGGCATGTGCACGTC"


def observation(obs_id, taxon=48715, its=SEQUENCE, photos=((None, "cc-by"),), faves=0):
    return {
        "id": obs_id,
        "observed_on": "2024-09-01",
        "faves_count": faves,
        "taxon": {"id": taxon},
        "user": {"login": "ann", "name": "Ann Observer"},
        "photos": [
            {
                "id": photo_id or obs_id * 10 + index,
                "url": f"https://inaturalist-open-data.s3.amazonaws.com/photos/{photo_id or obs_id * 10 + index}/square.jpg",
                "license_code": license_code,
                "attribution": "(c) Ann Observer, some rights reserved (CC BY)",
                "original_dimensions": {"width": 2048, "height": 1536},
            }
            for index, (photo_id, license_code) in enumerate(photos)
        ],
        "ofvs": [{"field_id": 2330, "name": "DNA Barcode ITS", "value": its}],
    }


class FakeINat:
    """Serves observations the way the API does: id_above paging, optional updated_since."""

    def __init__(self, observations, updated=None):
        self.observations = observations
        self.updated = updated  # observations returned for updated_since queries
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        self.calls.append(dict(params))
        assert params["field:DNA Barcode ITS"] == ""
        assert params["taxon_id"] == "47170"
        pool = self.updated if "updated_since" in params and self.updated is not None else self.observations
        above = int(params.get("id_above", 0))
        matching = sorted((o for o in pool if o["id"] > above), key=lambda o: o["id"])
        return httpx.Response(200, json={"total_results": len(matching), "results": matching[: int(params["per_page"])]})


def client_for(fake, max_requests=None):
    return inaturalist.make_client(max_requests, transport=httpx.MockTransport(fake), sleep=lambda _: None)


def run_full(fake, max_requests=None):
    with tracked_run("inaturalist") as run, client_for(fake, max_requests) as client:
        inaturalist.full_scan(run, client)
    return run


def run_incremental(fake):
    with tracked_run("inaturalist") as run, client_for(fake) as client:
        inaturalist.incremental(run, client)
    return run


@pytest.fixture
def species(make):
    make.taxon(48715, "Amanita muscaria")
    return make.taxon(48702, "Amanita phalloides")


def images(db):
    return {i.source_image_id: i for i in db.scalars(select(Image)).all()}


def test_full_scan_imports_only_displayable_its_photos(db, species):
    fake = FakeINat([
        observation(1, photos=((None, "cc-by"), (None, "cc-by-nc"))),
        observation(2, photos=((None, None),)),  # all rights reserved
        observation(3, its="pending"),
        observation(4, its="see https://www.inaturalist.org/observations/1"),
        observation(5, taxon=123456789),  # taxon not loaded
    ])
    run_full(fake)
    rows = images(db)
    assert {k: (v.is_active, v.inactive_reason) for k, v in rows.items()} == {
        "10": (True, None), "11": (True, None), "20": (False, "license"),
        "30": (False, "no_its"), "40": (True, None), "50": (False, "unknown_taxon"),
    }
    photo = rows["10"]
    assert photo.medium_url.endswith("/photos/10/medium.jpg")
    assert photo.large_url.endswith("/photos/10/large.jpg")
    assert (photo.photographer, photo.license_code, photo.width, photo.its_length) == ("Ann Observer", "cc-by", 2048, len(SEQUENCE))
    assert rows["40"].its_length is None  # evidence is a pointer to another observation


def test_full_scan_is_idempotent(db, species):
    fake = FakeINat([observation(1), observation(2)])
    run_full(fake)
    first = {k: v.id for k, v in images(db).items()}
    run_full(fake)
    db.expire_all()
    assert {k: v.id for k, v in images(db).items()} == first


def test_completed_full_scan_deactivates_missing_photos_but_keeps_votes(db, species, make):
    run_full(FakeINat([observation(n) for n in range(1, 11)]))
    user = make.user()
    gone = images(db)["10"]
    db.add(Vote(user_id=user.id, taxon_id=gone.taxon_id, image_id=gone.id, rank=1))
    db.commit()
    run_full(FakeINat([observation(n) for n in range(2, 11)]))
    db.expire_all()
    assert (images(db)["10"].is_active, images(db)["10"].inactive_reason) == (False, "missing_upstream")
    assert db.scalar(select(Vote.image_id)) == gone.id


def test_full_scan_refuses_to_hide_most_photos(db, species):
    run_full(FakeINat([observation(n) for n in range(1, 11)]))
    with pytest.raises(RuntimeError, match="refusing"):
        run_full(FakeINat([observation(1)]))  # e.g. an API outage returning almost nothing
    db.expire_all()
    assert all(image.is_active for image in images(db).values())
    assert "full_scan" not in get_cursor(db, "inaturalist")  # the next attempt starts over


def test_interrupted_full_scan_resumes(db, species):
    fake = FakeINat([observation(n) for n in range(1, 451)])  # three pages of 200
    with pytest.raises(RequestBudgetExceeded):
        run_full(fake, max_requests=1)
    assert get_cursor(db, "inaturalist")["full_scan"]["id_above"] == 200
    assert db.get(SyncState, "inaturalist").last_status == "failed"
    run_full(fake)
    assert [call.get("id_above") for call in fake.calls] == ["0", "200", "400", "450"]
    db.expire_all()
    assert all(image.is_active for image in images(db).values())
    assert len(images(db)) == 450
    assert "full_scan" not in get_cursor(db, "inaturalist")


def test_incremental_falls_back_to_full_scan_first_time(db, species):
    fake = FakeINat([observation(1)])
    run_incremental(fake)
    assert "updated_since" in get_cursor(db, "inaturalist")
    assert len(images(db)) == 1


def test_incremental_applies_reidentifications_and_removed_photos(db, species, make):
    run_full(FakeINat([observation(1, photos=((None, "cc-by"), (None, "cc-by")))]))
    since = get_cursor(db, "inaturalist")["updated_since"]
    changed = observation(1, taxon=48702, photos=((10, "cc-by"),))  # re-identified, photo 11 removed
    fake = FakeINat([], updated=[changed])
    run_incremental(fake)
    assert fake.calls[0]["updated_since"] == since
    db.expire_all()
    rows = images(db)
    assert rows["10"].taxon_id == species.id and rows["10"].is_active
    assert (rows["11"].is_active, rows["11"].inactive_reason) == (False, "missing_upstream")
    assert get_cursor(db, "inaturalist")["updated_since"] > since


def test_rejects_unexpected_photo_hosts(db, species):
    obs = observation(1)
    obs["photos"][0]["url"] = "https://evil.example/photos/10/square.jpg"
    run_full(FakeINat([obs]))
    assert images(db) == {}


@pytest.mark.parametrize(
    "value, present, length",
    [
        (SEQUENCE, True, len(SEQUENCE)),
        (">sample header\n" + SEQUENCE[:40] + "\n" + SEQUENCE[40:], True, len(SEQUENCE)),
        (">JLF9530 Agaricus OM010236 " + SEQUENCE, True, len(SEQUENCE)),
        ("see https://www.inaturalist.org/observations/101058580", True, None),
        ("sequence in observation #123456789", True, None),
        ("pending", False, None),
        ("yes", False, None),
        ("", False, None),
        ("ACGT", False, None),
    ],
)
def test_its_evidence(value, present, length):
    evidence = inat_its_evidence({"ofvs": [{"field_id": 2330, "value": value}]})
    assert (evidence.present, evidence.length) == (present, length)


def test_its_locus_names():
    for locus in ("ITS", "DNA Barcode ITS", "nrITS", "its1f-its4", "ITS2", "nrITS-LSU", "Partial ITS1"):
        assert is_its_locus(locus), locus
    for locus in ("nrLSU", "multi-locus", "TEF-1-alpha", "rpb2", "", None):
        assert not is_its_locus(locus), locus
    assert sequence_length("acgt " * 30) == 120
