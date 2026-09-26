from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from images_app.db import get_db
from images_app.main import app
from images_app.models import Base, Image, Taxon, TaxonAlias


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestingSession
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(database):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def taxon(database):
    with database() as db:
        root = Taxon(inat_taxon_id=47170, scientific_name="Fungi", rank="kingdom", ancestry="/1/47170/", active=True)
        db.add(root)
        db.flush()
        species = Taxon(inat_taxon_id=555001, parent_id=root.id, scientific_name="Amanita muscaria", rank="species", ancestry="/1/47170/555001/", active=True)
        db.add(species)
        db.commit()
        return species.id


@pytest.fixture
def add_image(database, taxon):
    def _add(source_id: int, taxon_id: int | None = None, *, active: bool = True, license: str = "CC BY-SA", dna_verified: bool = True):
        with database() as db:
            image = Image(
                taxon_id=taxon_id or taxon,
                source="inaturalist",
                source_image_id=str(source_id),
                source_observation_id=f"obs-{source_id}",
                medium_url=f"https://inaturalist-open-data.s3.amazonaws.com/photos/{source_id}/medium.jpeg",
                thumbnail_url=f"https://inaturalist-open-data.s3.amazonaws.com/photos/{source_id}/small.jpeg",
                source_page_url=f"https://www.inaturalist.org/photos/{source_id}",
                photographer="A. Photographer",
                license=license,
                attribution=f"© A. Photographer ({license})",
                display_eligible=True,
                dna_verified=dna_verified,
                active=active,
            )
            db.add(image)
            db.commit()
            return image.id
    return _add


def csrf_from(response_text: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response_text)
    if not match:
        match = re.search(r'name="csrf" value="([^"]+)"', response_text)
    assert match, "CSRF token missing from page"
    return match.group(1)


def register_client(client: TestClient, name: str = "Mina", email: str = "mina@example.org") -> str:
    page = client.get("/register")
    token = csrf_from(page.text)
    response = client.post(
        "/register",
        data={"csrf": token, "name": name, "email": email, "password": "field-guide-password-42"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf_from(client.get("/").text)
