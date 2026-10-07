import httpx
import pytest
from sqlalchemy import select

from images_app.models import Image, SourceName
from images_app.sync import mushroom_observer
from images_app.sync.runs import tracked_run

from .conftest import csrf, login

BASES = "ACGT" * 40
NAMES_CSV = "\n".join(
    [
        "id\ttext_name\tauthor\tdeprecated\tcorrect_spelling_id\tsynonym_id\trank",
        "1\tAmanita muscaria\t(L.) Lam.\t0\tNULL\t50\t4",
        "2\tAgaricus muscarius\tL.\t1\tNULL\t50\t4",
        "3\tRussula foo group\t\t0\tNULL\tNULL\t4",
        "4\tAmanita muscaria var. guessowii\tVeselý\t0\tNULL\tNULL\t4",
        "5\tAmbigua\t\t0\tNULL\tNULL\t9",
        "6\tBoletus mysterius\t\t0\tNULL\tNULL\t4",
    ]
)


def mo_observation(obs_id, name_id, name, rank="species", license="Creative Commons Non-commercial v3.0", links=()):
    return {
        "id": obs_id,
        "date": "2023-10-01",
        "consensus": {"id": name_id, "name": name, "author": "", "rank": rank},
        "external_links": [{"url": url} for url in links],
        "images": [
            {"id": obs_id * 10, "license": license, "copyright_holder": "Mo Photographer",
             "owner": {"login_name": "mo"}, "original_url": f"https://mushroomobserver.org/images/orig/{obs_id * 10}.jpg"},
        ],
    }


OBSERVATIONS = {
    100: mo_observation(100, 1, "Amanita muscaria"),
    101: mo_observation(101, 2, "Agaricus muscarius"),
    102: mo_observation(102, 3, "Russula foo group"),
    103: mo_observation(103, 4, "Amanita muscaria var. guessowii", rank="variety"),
    104: mo_observation(104, 1, "Amanita muscaria", license="Copyright © Someone"),
    105: mo_observation(105, 5, "Ambigua", rank="genus"),
    106: mo_observation(106, 1, "Amanita muscaria", links=["https://www.inaturalist.org/observations/777"]),
    107: mo_observation(107, 6, "Boletus mysterius"),
}
# MO lets one image belong to several observations.
OBSERVATIONS[100]["images"].append(dict(OBSERVATIONS[101]["images"][0]))
SEQUENCES = [
    {"id": 1, "observation_id": 100, "locus": "ITS", "bases": BASES, "accession": ""},
    {"id": 2, "observation_id": 101, "locus": "DNA Barcode ITS", "bases": "", "accession": "MK000001"},
    {"id": 3, "observation_id": 102, "locus": "nrITS", "bases": BASES, "accession": ""},
    {"id": 4, "observation_id": 103, "locus": "its1f-its4", "bases": BASES, "accession": ""},
    {"id": 5, "observation_id": 104, "locus": "ITS", "bases": BASES, "accession": ""},
    {"id": 6, "observation_id": 105, "locus": "ITS", "bases": BASES, "accession": ""},
    {"id": 7, "observation_id": 106, "locus": "ITS", "bases": BASES, "accession": ""},
    {"id": 8, "observation_id": 107, "locus": "ITS", "bases": BASES, "accession": ""},
    {"id": 9, "observation_id": 108, "locus": "nrLSU", "bases": BASES, "accession": ""},
    {"id": 10, "observation_id": 109, "locus": "ITS", "bases": "pending", "accession": ""},
]


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/names.csv":
        return httpx.Response(200, text=NAMES_CSV)
    if path == "/api2/sequences":
        page = int(request.url.params["page"])
        results = SEQUENCES[:6] if page == 1 else SEQUENCES[6:]
        return httpx.Response(200, json={"number_of_pages": 2, "results": results})
    if path == "/api2/observations":
        ids = [int(i) for i in request.url.params["id"].split(",")]
        assert 108 not in ids and 109 not in ids
        return httpx.Response(200, json={"results": [OBSERVATIONS[i] for i in ids if i in OBSERVATIONS]})
    return httpx.Response(404)


def run_sync():
    client = mushroom_observer.make_client(transport=httpx.MockTransport(handler), sleep=lambda _: None)
    with tracked_run("mushroom_observer") as run, client:
        mushroom_observer.sync(run, client)
    return run


@pytest.fixture
def taxa(make):
    amanita = make.taxon(48419, "Amanita", "genus")
    muscaria = make.taxon(48715, "Amanita muscaria", parent=amanita)
    guessowii = make.taxon(321524, "Amanita muscaria guessowii", "variety", parent=muscaria)
    make.taxon(1, "Ambigua", "genus")
    make.taxon(2, "Ambigua", "genus")
    inat_copy = make.taxon(48702, "Amanita phalloides", parent=amanita)
    make.image(inat_copy, observation=777)
    return {"muscaria": muscaria, "guessowii": guessowii}


def state(db):
    return {
        image.source_observation_id: (image.is_active, image.inactive_reason, image.taxon_id)
        for image in db.scalars(select(Image).where(Image.source == "mushroom_observer")).all()
    }


def test_mo_sync_maps_names_and_filters_photos(db, taxa):
    run_sync()
    muscaria, guessowii = taxa["muscaria"].id, taxa["guessowii"].id
    assert state(db) == {
        "100": (True, None, muscaria),
        "101": (True, None, muscaria),  # deprecated MO name -> accepted MO synonym
        "102": (False, "unmapped_name", None),  # group names are never guessed
        "103": (True, None, guessowii),  # "var." connector normalized
        "104": (False, "license", muscaria),
        "105": (False, "unmapped_name", None),  # two iNat taxa share the name
        "106": (False, "duplicate", muscaria),  # same observation already imported from iNat
        "107": (False, "unmapped_name", None),  # no such iNat taxon
    }
    names = {n.name: (n.status, n.method) for n in db.scalars(select(SourceName)).all()}
    assert names["Agaricus muscarius"] == ("mapped", "synonym")
    assert names["Ambigua"] == ("ambiguous", None)
    assert names["Russula foo group"][0] == "unresolved"
    image = db.scalar(select(Image).where(Image.source_observation_id == "100"))
    assert image.medium_url == "https://mushroomobserver.org/images/640/1000.jpg"
    assert image.license_code == "cc-by-nc-sa"
    assert image.observation_url == "https://mushroomobserver.org/obs/100"


def test_mo_sync_is_idempotent(db, taxa):
    run_sync()
    first = {i.source_image_id: i.id for i in db.scalars(select(Image)).all()}
    run_sync()
    db.expire_all()
    assert {i.source_image_id: i.id for i in db.scalars(select(Image)).all()} == first


def test_manual_mapping_publishes_photos_and_survives_resync(client, db, taxa, make):
    run_sync()
    make.user(email="admin@example.org", admin=True)
    login(client, email="admin@example.org")
    pending = db.scalar(select(SourceName).where(SourceName.name == "Boletus mysterius"))
    assert "Boletus mysterius" in client.get("/admin/names").text
    response = client.post(
        f"/admin/names/{pending.id}/map",
        data={"inat_taxon_id": 48715, "csrf_token": csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.expire_all()
    assert state(db)["107"] == (True, None, taxa["muscaria"].id)
    run_sync()
    db.expire_all()
    assert state(db)["107"] == (True, None, taxa["muscaria"].id)
    assert db.get(SourceName, pending.id).method == "manual"


def test_admin_dashboard(client, db, taxa, make):
    run_sync()
    make.user(email="admin@example.org", admin=True)
    login(client, email="admin@example.org")
    page = client.get("/admin").text
    assert "mushroom_observer" in page and "success" in page
