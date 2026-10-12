"""Synchronization jobs. Each step is recorded in sync_runs; see images_app.cli for commands."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from images_app.db import SessionLocal
from images_app.sync import inaturalist, maintenance, mushroom_observer, taxonomy
from images_app.sync.runs import sync_lock, tracked_run

log = logging.getLogger(__name__)


def run_taxonomy(archive: Path | None = None) -> None:
    with tracked_run("taxonomy") as run:
        path = taxonomy.ensure_archive(archive)
        with inaturalist.make_client(max_requests=500) as client:
            taxonomy.import_taxonomy(run, path, client)


def run_inaturalist(full: bool, max_requests: int | None = None) -> None:
    with tracked_run("inaturalist") as run, inaturalist.make_client(max_requests) as client:
        (inaturalist.full_scan if full else inaturalist.incremental)(run, client)
        run.stats["requests"] = client.requests


def run_mushroom_observer(max_requests: int | None = None) -> None:
    with tracked_run("mushroom_observer") as run, mushroom_observer.make_client(max_requests) as client:
        mushroom_observer.sync(run, client)
        run.stats["requests"] = client.requests


def run_maintenance() -> None:
    with tracked_run("maintenance"), SessionLocal() as db:
        maintenance.refresh_derived_data(db)


def run_steps(steps: list[tuple[str, Callable[[], None]]]) -> bool:
    """Run steps in order under the sync lock. A failed step does not stop later ones."""
    ok = True
    with sync_lock():
        for name, step in steps:
            try:
                step()
            except Exception:
                log.error("step %s failed; continuing with the remaining steps", name)
                ok = False
    return ok


def daily() -> bool:
    return run_steps(
        [
            ("inaturalist", lambda: run_inaturalist(full=False, max_requests=2000)),
            ("mushroom_observer", lambda: run_mushroom_observer(max_requests=600)),
            ("maintenance", run_maintenance),
        ]
    )


def full(archive: Path | None = None) -> bool:
    return run_steps(
        [
            ("taxonomy", lambda: run_taxonomy(archive)),
            ("inaturalist", lambda: run_inaturalist(full=True, max_requests=4000)),
            ("mushroom_observer", lambda: run_mushroom_observer(max_requests=600)),
            ("maintenance", run_maintenance),
        ]
    )
