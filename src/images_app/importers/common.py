from __future__ import annotations

import csv
import gzip
import io
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from images_app.models import Image, SyncState, Taxon, Vote, utcnow

logger = logging.getLogger("images_app.importers")
STREAM_HOSTS = {"inaturalist-open-data.s3.amazonaws.com", "mushroomobserver.org"}


class _IteratorRaw(io.RawIOBase):
    """Adapt a streamed HTTP byte iterator to the file interface gzip expects."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buffer = bytearray()

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        while not self._buffer:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                return 0
        size = min(len(target), len(self._buffer))
        target[:size] = self._buffer[:size]
        del self._buffer[:size]
        return size


def parse_bool(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "t", "yes", "y"}


def normalized_name(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def stream_tsv(path: str | Path, required: Iterable[str] = ()) -> Iterator[dict[str, str]]:
    path_text = str(path)
    parsed = urlparse(path_text)
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.hostname not in STREAM_HOSTS:
            raise ValueError(f"Unsupported import URL host: {parsed.hostname or parsed.scheme}")
        with httpx.stream(
            "GET",
            path_text,
            headers={"User-Agent": "DikaryaImages/0.1 (images.dikarya.us; licensed metadata sync)", "Accept": "text/csv,application/octet-stream,*/*"},
            timeout=httpx.Timeout(connect=30, read=180, write=30, pool=30),
            follow_redirects=True,
        ) as response:
            if response.url.scheme != "https" or response.url.host not in STREAM_HOSTS:
                raise ValueError(f"Import URL redirected to an unsupported host: {response.url.host}")
            response.raise_for_status()
            raw_stream = io.BufferedReader(_IteratorRaw(response.iter_raw()))
            binary_stream = gzip.GzipFile(fileobj=raw_stream, mode="rb") if parsed.path.endswith(".gz") else raw_stream
            with io.TextIOWrapper(binary_stream, encoding="utf-8-sig", newline="") as stream:
                yield from _parse_tsv_stream(stream, path_text, required)
        return

    file_path = Path(path_text)
    opener = gzip.open if file_path.name.endswith(".gz") else open
    with opener(file_path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield from _parse_tsv_stream(stream, path_text, required)


def _parse_tsv_stream(stream, path: str, required: Iterable[str]) -> Iterator[dict[str, str]]:
    reader = csv.DictReader(stream, delimiter="\t")
    if not reader.fieldnames:
        raise ValueError(f"Missing tab-separated header in {path}")
    missing = set(required) - set(reader.fieldnames)
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(sorted(missing))}")
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"Malformed row {row_number} in {path}: too many fields")
        if row and any(value is None for value in row.values()):
            raise ValueError(f"Malformed row {row_number} in {path}: too few fields")
        yield {key: value or "" for key, value in row.items()}


def upsert_rows(session: Session, model, rows: list[dict[str, Any]], keys: list[str], update_fields: list[str]) -> None:
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert_fn is None:
        raise RuntimeError(f"Importer upserts do not support database dialect {dialect}")
    statement = insert_fn(model).values(rows)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[getattr(model, key) for key in keys],
        set_={key: getattr(excluded, key) for key in update_fields},
    )
    session.execute(statement)


def insert_ignore_rows(session: Session, model, rows: list[dict[str, Any]], keys: list[str]) -> None:
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert_fn is None:
        raise RuntimeError(f"Importer insert-ignore does not support database dialect {dialect}")
    statement = insert_fn(model).values(rows).on_conflict_do_nothing(index_elements=[getattr(model, key) for key in keys])
    session.execute(statement)


def begin_sync(session: Session, source: str) -> datetime:
    started = utcnow()
    state = session.scalar(select(SyncState).where(SyncState.source == source))
    if state is None:
        state = SyncState(source=source)
        session.add(state)
    state.status = "running"
    state.last_started_at = started
    state.error_message = None
    session.commit()
    return started


def finish_sync(session: Session, source: str, started: datetime, count: int, cursor: str | None = None) -> None:
    state = session.scalar(select(SyncState).where(SyncState.source == source))
    if state is None:
        raise RuntimeError(f"Missing sync state for {source}")
    now = utcnow()
    state.status = "success"
    state.last_started_at = started
    state.last_completed_at = now
    state.last_success_at = now
    state.records_processed = count
    state.cursor = cursor
    state.error_message = None
    session.commit()
    logger.info("%s sync completed: %d records", source, count)


def fail_sync(session: Session, source: str, started: datetime, error: BaseException) -> None:
    session.rollback()
    state = session.scalar(select(SyncState).where(SyncState.source == source))
    if state is None:
        state = SyncState(source=source)
        session.add(state)
    now = utcnow()
    state.status = "failed"
    state.last_started_at = started
    state.last_completed_at = now
    state.error_message = f"{type(error).__name__}: {error}"[:4000]
    session.commit()
    logger.exception("%s sync failed", source, exc_info=error)


def commit_batch(session: Session, count: int, every: int = 1000) -> None:
    if count and count % every == 0:
        session.commit()
        logger.info("processed %d records", count)


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


@contextmanager
def import_lock(path: str | Path):
    import fcntl

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Importer already running: {lock_path.name}") from exc
        yield


def parse_license(value: str) -> str:
    return " ".join(value.strip().split())


def refresh_image_counts(session: Session, taxon_ids: Iterable[int] | None = None) -> None:
    selected_ids = sorted(set(taxon_ids)) if taxon_ids is not None else None
    if selected_ids == []:
        return
    counts_stmt = select(
        Image.taxon_id.label("taxon_id"), func.count(Image.id).label("image_count")
    ).where(
        Image.taxon_id.is_not(None),
        Image.active.is_(True),
        Image.display_eligible.is_(True),
        Image.dna_verified.is_(True),
    )
    if selected_ids is not None:
        counts_stmt = counts_stmt.where(Image.taxon_id.in_(selected_ids))
    counts = counts_stmt.group_by(Image.taxon_id).subquery()
    count_for_taxon = (
        select(counts.c.image_count)
        .where(counts.c.taxon_id == Taxon.id)
        .scalar_subquery()
    )
    statement = update(Taxon).values(image_count=func.coalesce(count_for_taxon, 0))
    if selected_ids is not None:
        statement = statement.where(Taxon.id.in_(selected_ids))
    session.execute(statement)


def delete_unverified_images_without_votes(
    session: Session, source: str, *, batch_size: int = 5000
) -> int:
    """Remove obsolete unverified image metadata while retaining any voted image row."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    has_votes = exists(select(1).select_from(Vote).where(Vote.image_id == Image.id))
    deleted = 0
    last_id = 0
    while True:
        image_ids = list(
            session.scalars(
                select(Image.id)
                .where(
                    Image.source == source,
                    Image.dna_verified.is_(False),
                    Image.id > last_id,
                )
                .order_by(Image.id)
                .limit(batch_size)
            ).all()
        )
        if not image_ids:
            break
        # Delete unvoted rows directly: updating them first creates unnecessary
        # PostgreSQL row versions and WAL. Only retained voted rows need hiding.
        session.execute(
            update(Image)
            .where(Image.id.in_(image_ids), has_votes, Image.active.is_(True))
            .values(active=False)
        )
        result = session.execute(
            delete(Image).where(Image.id.in_(image_ids), ~has_votes)
        )
        session.commit()
        deleted += result.rowcount
        last_id = image_ids[-1]
        if result.rowcount and deleted % 50_000 < result.rowcount:
            logger.info("deleted %d obsolete %s image references without votes", deleted, source)
    if deleted:
        logger.info("deleted %d obsolete %s image references without votes", deleted, source)
    return deleted
