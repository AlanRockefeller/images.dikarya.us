"""Test fixtures.

Tests need a disposable PostgreSQL database (the app relies on arrays, trigram
search and deferred constraints). Point TEST_DATABASE_URL at one; it is wiped.

    TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/images_test pytest
"""

from __future__ import annotations

import itertools
import logging
import os
import re

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.exit("Set TEST_DATABASE_URL to a disposable PostgreSQL database to run the tests.", 2)

os.environ.update(
    APP_ENV="test",
    DATABASE_URL=TEST_DATABASE_URL,
    SECRET_KEY="test-secret-key-test-secret-key-0123456789",
    BASE_URL="http://testserver",
    ROOT_TAXON_IDS="47170",
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from images_app.config import PROJECT_ROOT  # noqa: E402
from images_app.db import SessionLocal, engine  # noqa: E402
from images_app.models import Image, Taxon, User  # noqa: E402
from images_app.security import hash_password  # noqa: E402
from images_app.sync.maintenance import refresh_derived_data  # noqa: E402

TABLES = (
    "votes, images, source_names, taxon_names, taxa, user_sessions, rate_limit_hits, "
    "users, sync_state, sync_runs"
)


def reset_schema() -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def migrate() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def schema():
    reset_schema()
    migrate()
    yield


@pytest.fixture(autouse=True)
def clean_tables(schema):
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client():
    from images_app.main import app

    with TestClient(app) as test_client:
        yield test_client


_image_ids = itertools.count(1000)


class Factory:
    """Small helpers for building a taxonomy and photos."""

    def __init__(self, session):
        self.db = session
        self.fungi = self.taxon(47170, "Fungi", "kingdom", parent=None)

    def taxon(self, inat_id: int, name: str, rank: str = "species", parent: Taxon | None = ...):
        from images_app.sync.taxonomy import RANK_LEVELS

        if parent is ...:
            parent = self.fungi
        ancestors = [*parent.ancestor_ids, parent.inat_taxon_id] if parent else []
        taxon = Taxon(
            inat_taxon_id=inat_id, name=name, rank=rank, rank_level=int(RANK_LEVELS[rank]),
            ancestor_ids=ancestors, parent_id=parent.id if parent else None,
        )
        self.db.add(taxon)
        self.db.flush()
        self.db.execute(
            text("INSERT INTO taxon_names (taxon_id, name, kind, source) VALUES (:t, :n, 'scientific', 'inaturalist')"),
            {"t": taxon.id, "n": name},
        )
        self.db.commit()
        return taxon

    def image(self, taxon: Taxon, *, source="inaturalist", active=True, faves=0, observation=None, **extra) -> Image:
        number = next(_image_ids)
        image = Image(
            source=source, source_image_id=str(number), source_observation_id=str(observation or number),
            observation_url=f"https://www.inaturalist.org/observations/{observation or number}",
            taxon_id=taxon.id, thumb_url=f"https://inaturalist-open-data.s3.amazonaws.com/photos/{number}/small.jpg",
            medium_url=f"https://inaturalist-open-data.s3.amazonaws.com/photos/{number}/medium.jpg",
            large_url=f"https://inaturalist-open-data.s3.amazonaws.com/photos/{number}/large.jpg",
            photographer="Photographer", license_code="cc-by", attribution="(c) Photographer, some rights reserved (CC BY)",
            is_active=active, inactive_reason=None if active else "license", faves=faves, **extra,
        )
        self.db.add(image)
        self.db.commit()
        return image

    def user(self, email="ranker@example.org", password="a good long password", name="Ranker", admin=False) -> User:
        user = User(name=name, email=email, password_hash=hash_password(password), is_admin=admin)
        self.db.add(user)
        self.db.commit()
        return user

    def refresh(self):
        refresh_derived_data(self.db)


@pytest.fixture
def make(db):
    return Factory(db)


def csrf(client) -> str:
    """The double-submit CSRF token, fetching a page first if needed."""
    if "csrf" not in client.cookies:
        client.get("/about")
    return client.cookies["csrf"]


def login(client, email="ranker@example.org", password="a good long password"):
    response = client.post(
        "/login", data={"email": email, "password": password, "csrf_token": csrf(client)}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return response


def form_stamp(purpose: str = "register", age: float = 30) -> str:
    """A validly signed form timestamp issued `age` seconds ago."""
    import hashlib
    import hmac
    import time

    issued = str(int(time.time() - age))
    signature = hmac.new(os.environ["SECRET_KEY"].encode(), f"{purpose}:{issued}".encode(), hashlib.sha256)
    return f"{issued}.{signature.hexdigest()[:24]}"


@pytest.fixture(autouse=True)
def forget_watched_ips():
    from images_app import logs

    logs._watched.clear()
    yield


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[tuple[str, str]] = []

    def emit(self, record):
        self.records.append((record.name.split(".")[-1], record.getMessage()))

    def events(self, stream: str | None = None) -> list[str]:
        return [m.split()[0].removeprefix("event=") for s, m in self.records if stream in (None, s)]

    def find(self, stream: str, event: str) -> str:
        matches = [m for s, m in self.records if s == stream and m.split()[0] == f"event={event}"]
        assert matches, f"no {stream} event {event}; got {self.records}"
        return matches[-1]


@pytest.fixture
def logged():
    from images_app import logs

    capture = LogCapture()
    for logger in logs.STREAMS:
        logger.addHandler(capture)
    yield capture
    for logger in logs.STREAMS:
        logger.removeHandler(capture)


def page_csrf(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
