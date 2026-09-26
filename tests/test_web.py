from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from images_app.models import Image, ImageSourceTaxon, SourceTaxon, SyncState, Taxon, TaxonAlias, User, Vote
from images_app.db import get_db
from images_app.main import app
from images_app.security import hash_password
from tests.conftest import csrf_from, register_client


def taxon_path(client, taxon_id: int) -> str:
    response = client.get(f"/taxon/{taxon_id}", follow_redirects=False)
    assert response.status_code == 308
    return response.headers["location"]


def rank_badges(html: str) -> list[str]:
    import re
    return re.findall(r'class="rank-badge">(\d+)</span>', html)


def test_registration_duplicate_email_login_and_logout(client, database):
    page = client.get("/register")
    token = csrf_from(page.text)
    response = client.post("/register", data={"csrf": token, "name": "Mina", "email": "Mina@Example.org", "password": "field-guide-password-42"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Mina" in client.get("/").text

    page = client.get("/register")
    response = client.post("/register", data={"csrf": csrf_from(page.text), "name": "Other", "email": "mina@example.org", "password": "another-long-password-42"})
    assert response.status_code == 409
    assert "already uses" in response.text

    logout_page = client.get("/")
    assert client.post("/logout", data={"csrf": csrf_from(logout_page.text)}, follow_redirects=False).status_code == 303
    page = client.get("/login")
    response = client.post("/login", data={"csrf": csrf_from(page.text), "email": "MINA@example.org", "password": "field-guide-password-42"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Mina" in client.get("/").text


def test_unauthorized_voting_rejected(client, taxon, add_image):
    image_id = add_image(101)
    path = taxon_path(client, taxon)
    page = client.get(path)
    response = client.post(f"/taxon/{taxon}/image/{image_id}/toggle", data={"csrf": csrf_from(page.text)})
    assert response.status_code == 401


def test_unverified_images_are_hidden_and_cannot_be_voted_on(client, taxon, add_image):
    verified_id = add_image(151)
    unverified_id = add_image(152, dna_verified=False)
    page = client.get(taxon_path(client, taxon))
    assert f"/photos/151/medium.jpeg" in page.text
    assert f"/photos/152/medium.jpeg" not in page.text
    csrf = register_client(client)
    response = client.post(
        f"/taxon/{taxon}/image/{unverified_id}/toggle",
        data={"csrf": csrf},
    )
    assert response.status_code == 409
    assert verified_id != unverified_id


def test_select_and_remove_normalizes_ranks(client, database, taxon, add_image):
    first, second, third, fourth = [add_image(i) for i in range(201, 205)]
    csrf = register_client(client)
    path = taxon_path(client, taxon)
    assert rank_badges(client.get(path).text) == []
    for image_id in (first, second, third, fourth):
        response = client.post(f"/taxon/{taxon}/image/{image_id}/toggle", data={"csrf": csrf}, headers={"HX-Request": "true"})
        assert response.status_code == 200
        csrf = csrf_from(response.text)
    assert set(rank_badges(response.text)) == {"1", "2", "3", "4"}

    response = client.post(f"/taxon/{taxon}/image/{second}/toggle", data={"csrf": csrf}, headers={"HX-Request": "true"})
    assert sorted(map(int, rank_badges(response.text))) == [1, 2, 3]
    with database() as db:
        ranks = db.scalars(select(Vote.rank).order_by(Vote.rank)).all()
        assert ranks == [1, 2, 3]


def test_reorder_and_two_users_are_independent(client, database, taxon, add_image):
    first, second = add_image(301), add_image(302)
    csrf = register_client(client, "Mina", "mina@example.org")
    path = taxon_path(client, taxon)
    for image_id in (first, second):
        response = client.post(f"/taxon/{taxon}/image/{image_id}/toggle", data={"csrf": csrf}, headers={"HX-Request": "true"})
        csrf = csrf_from(response.text)
    moved = client.post(f"/taxon/{taxon}/move", data={"csrf": csrf, "image_id": second, "direction": "up"}, headers={"HX-Request": "true"})
    assert rank_badges(moved.text)
    moved = client.post(
        f"/taxon/{taxon}/reorder",
        data={"csrf": csrf, "page": 1, "image_ids": [first, second]},
        headers={"HX-Request": "true"},
    )
    assert moved.status_code == 200
    with database() as db:
        first_user = db.scalar(select(User).where(User.email == "mina@example.org"))
        rows = db.scalars(select(Vote).where(Vote.user_id == first_user.id).order_by(Vote.rank)).all()
        assert [row.image_id for row in rows] == [first, second]

    client.post("/logout", data={"csrf": csrf_from(client.get("/").text)})
    new_csrf = register_client(client, "Sol", "sol@example.org")
    client.post(f"/taxon/{taxon}/image/{first}/toggle", data={"csrf": new_csrf}, headers={"HX-Request": "true"})
    with database() as db:
        users = {u.email: u for u in db.scalars(select(User)).all()}
        mine = db.scalars(select(Vote).where(Vote.user_id == users["mina@example.org"].id)).all()
        theirs = db.scalars(select(Vote).where(Vote.user_id == users["sol@example.org"].id)).all()
        assert len(mine) == 2
        assert len(theirs) == 1


def test_vote_constraints_prevent_duplicate_rank_or_image(database, taxon, add_image):
    image_a, image_b = add_image(401), add_image(402)
    with database() as db:
        user = User(name="Mina", email="mina@example.org", password_hash=hash_password("test-password-long"))
        db.add(user)
        db.commit()
        db.add(Vote(user_id=user.id, taxon_id=taxon, image_id=image_a, rank=1))
        db.commit()
        db.add(Vote(user_id=user.id, taxon_id=taxon, image_id=image_a, rank=2))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Vote(user_id=user.id, taxon_id=taxon, image_id=image_b, rank=1))
        with pytest.raises(IntegrityError):
            db.commit()


def test_source_image_unique_constraint(database, taxon, add_image):
    add_image(450)
    with database() as db:
        db.add(
            Image(
                taxon_id=taxon,
                source="inaturalist",
                source_image_id="450",
                medium_url="https://inaturalist-open-data.s3.amazonaws.com/photos/450/medium.jpeg",
                source_page_url="https://www.inaturalist.org/photos/450",
                display_eligible=True,
                active=True,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_directory_searches_alias_and_taxon_page(client, database, taxon, add_image):
    add_image(501)
    with database() as db:
        db.add(TaxonAlias(taxon_id=taxon, name="Fly agaric", source="test synonym"))
        from images_app.models import Taxon
        record = db.get(Taxon, taxon)
        record.image_count = 1
        db.commit()
    page = client.get("/taxa?q=Fly+agaric")
    assert "Amanita muscaria" in page.text
    assert f"/taxon/{taxon}" in page.text
    taxon_page = client.get(taxon_path(client, taxon))
    assert taxon_page.status_code == 200
    assert "DNA sequence evidence recorded" in taxon_page.text


def test_directory_defaults_to_species_and_all_ranks_is_available(client, database, taxon, add_image):
    add_image(502)
    with database() as db:
        species = db.get(Taxon, taxon)
        species.image_count = 1
        fungi = db.get(Taxon, species.parent_id)
        fungi.image_count = 1
        db.commit()
        fungi_id = fungi.id
    add_image(503, fungi_id)
    default_page = client.get("/taxa")
    assert "Amanita muscaria" in default_page.text
    assert f'href="/taxon/{fungi_id}"' not in default_page.text
    all_ranks_page = client.get("/taxa?rank=all")
    assert f'href="/taxon/{fungi_id}"' in all_ranks_page.text


def test_healthz_checks_database_and_static_assets_are_local(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "default-src 'self'" in response.headers["content-security-policy"]
    homepage = client.get("/")
    assert "/static/htmx-2.0.4.min.js" in homepage.text
    assert "unpkg.com" not in homepage.text


def test_healthz_returns_unavailable_when_database_query_fails(client):
    previous = app.dependency_overrides[get_db]

    class BrokenDatabase:
        def execute(self, _statement):
            raise SQLAlchemyError("database failure detail")

    def broken_database():
        yield BrokenDatabase()

    app.dependency_overrides[get_db] = broken_database
    try:
        response = client.get("/healthz")
    finally:
        app.dependency_overrides[get_db] = previous
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_login_has_a_separate_per_ip_rate_limit(client):
    csrf = csrf_from(client.get("/login").text)
    for index in range(30):
        response = client.post(
            "/login",
            data={"csrf": csrf, "email": f"user-{index}@example.org", "password": "wrong"},
        )
        assert response.status_code == 401
    response = client.post(
        "/login",
        data={"csrf": csrf, "email": "another@example.org", "password": "wrong"},
    )
    assert response.status_code == 429


def test_gallery_pagination(client, database, taxon, add_image):
    for i in range(74):
        add_image(600 + i)
    path = taxon_path(client, taxon)
    first_page = client.get(path)
    assert first_page.status_code == 200
    assert first_page.text.count("class=\"image-card") == 72
    assert "More images" in first_page.text
    second_page = client.get(f"/taxon/{taxon}/gallery?page=2")
    assert second_page.status_code == 200
    assert second_page.text.count("class=\"image-card") == 2


def test_vote_form_works_without_htmx(client, taxon, add_image):
    image_id = add_image(699)
    register_client(client)
    response = client.post(
        f"/taxon/{taxon}/image/{image_id}/toggle",
        data={"csrf": csrf_from(client.get(f"/taxon/{taxon}/amanita-muscaria").text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/taxon/{taxon}/amanita-muscaria"


def test_admin_can_manually_map_unresolved_mushroom_observer_name(client, database, taxon):
    csrf = register_client(client, "Admin", "admin@example.org")
    with database() as db:
        admin = db.scalar(select(User).where(User.email == "admin@example.org"))
        admin.is_admin = True
        source_taxon = SourceTaxon(
            source="mushroom_observer",
            source_taxon_id="mo-name-100",
            original_name="Amanita muscaria",
            normalized_name="amanita muscaria",
            mapping_status="unresolved",
        )
        second_name = SourceTaxon(
            source="mushroom_observer",
            source_taxon_id="mo-name-101",
            original_name="Amanita muscaria (alternate record)",
            normalized_name="amanita muscaria alternate record",
            mapping_status="unresolved",
        )
        db.add_all([source_taxon, second_name])
        db.flush()
        image = Image(
            source="mushroom_observer",
            source_image_id="mo-image-200",
            medium_url="https://mushroomobserver.org/images/640/200.jpg",
                source_page_url="https://mushroomobserver.org/observations/300",
                display_eligible=True,
                dna_verified=True,
                active=False,
        )
        db.add(image)
        db.flush()
        db.add_all(
            [
                ImageSourceTaxon(image_id=image.id, source_taxon_id=source_taxon.id),
                ImageSourceTaxon(image_id=image.id, source_taxon_id=second_name.id),
            ]
        )
        db.commit()
        source_taxon_id = source_taxon.id
        second_source_taxon_id = second_name.id
        image_id = image.id

    page = client.get("/admin")
    assert page.status_code == 200
    response = client.post(
        f"/admin/source-taxon/{source_taxon_id}/map",
        data={"csrf": csrf, "taxon_id": taxon},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with database() as db:
        mapped = db.get(SourceTaxon, source_taxon_id)
        image = db.get(Image, image_id)
        assert mapped.taxon_id == taxon
        assert mapped.mapping_method == "manual"
        assert image.taxon_id is None and not image.active

    response = client.post(
        f"/admin/source-taxon/{second_source_taxon_id}/map",
        data={"csrf": csrf, "taxon_id": taxon},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with database() as db:
        image = db.get(Image, image_id)
        taxon_record = db.get(Taxon, taxon)
        associations = db.scalars(
            select(ImageSourceTaxon).where(ImageSourceTaxon.image_id == image_id)
        ).all()
        assert image.taxon_id == taxon and image.active
        assert len(associations) == 2
        assert taxon_record.image_count == 1
        assert db.scalar(select(TaxonAlias.id).where(TaxonAlias.taxon_id == taxon, TaxonAlias.name == "Amanita muscaria"))
