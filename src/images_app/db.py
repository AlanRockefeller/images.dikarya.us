from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from images_app.config import settings

# Data lives in RAM or on SSD, so random reads are nearly as cheap as sequential
# ones; without this PostgreSQL prefers full scans over the gallery indexes. JIT
# compilation only adds latency to the short queries a web app runs.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    connect_args={"options": "-c random_page_cost=1.1 -c jit=off"},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
