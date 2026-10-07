"""Bookkeeping shared by all synchronization jobs: locking, run history and cursors."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from images_app.db import SessionLocal, engine
from images_app.models import SyncRun, SyncState

log = logging.getLogger(__name__)

# One lock for every job that writes image data, so jobs never overlap.
SYNC_LOCK_KEY = 0x1D1C_A2A1


class SyncAlreadyRunning(RuntimeError):
    pass


@dataclass
class Run:
    job: str
    run_id: int
    started_at: datetime
    records: int = 0
    stats: dict = field(default_factory=dict)

    def count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount


@contextmanager
def sync_lock():
    """Hold a PostgreSQL advisory lock for the duration of a sync."""
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": SYNC_LOCK_KEY})
        if not acquired:
            raise SyncAlreadyRunning("another synchronization job is running")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SYNC_LOCK_KEY})
            connection.commit()


def get_cursor(db: Session, job: str) -> dict:
    state = db.get(SyncState, job)
    return dict(state.cursor) if state else {}


def save_cursor(db: Session, job: str, cursor: dict) -> None:
    state = db.get(SyncState, job) or SyncState(job=job)
    state.cursor = cursor
    db.add(state)
    db.commit()


@contextmanager
def tracked_run(job: str):
    """Record a run in sync_runs/sync_state, marking it failed if the body raises."""
    started = datetime.now(timezone.utc)
    with SessionLocal() as db:
        state = db.get(SyncState, job) or SyncState(job=job, cursor={})
        state.last_started_at = started
        state.last_status = "running"
        db.add(state)
        record = SyncRun(job=job, started_at=started, status="running")
        db.add(record)
        db.commit()
        run = Run(job=job, run_id=record.id, started_at=started)
    log.info("%s: started", job)
    try:
        yield run
    except BaseException as exc:
        _finish(run, "failed", f"{type(exc).__name__}: {exc}")
        log.exception("%s: failed", job)
        raise
    else:
        _finish(run, "success", None)
        log.info("%s: finished, %d records, %s", job, run.records, run.stats)


def _finish(run: Run, status: str, error: str | None) -> None:
    finished = datetime.now(timezone.utc)
    with SessionLocal() as db:
        record = db.get(SyncRun, run.run_id)
        record.finished_at = finished
        record.status = status
        record.records_processed = run.records
        record.stats = run.stats
        record.error = error
        state = db.get(SyncState, run.job)
        state.last_finished_at = finished
        state.last_status = status
        state.last_error = error
        state.records_processed = run.records
        if status == "success":
            state.last_success_at = finished
        db.commit()
