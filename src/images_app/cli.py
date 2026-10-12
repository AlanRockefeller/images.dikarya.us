"""Command line administration.

    python -m images_app.cli create-admin
    python -m images_app.cli promote-admin someone@example.org
    python -m images_app.cli sync daily        # iNat incremental + MO + derived data
    python -m images_app.cli sync full         # taxonomy + iNat full scan + MO + derived data
    python -m images_app.cli sync taxonomy [--archive FILE]
    python -m images_app.cli sync inaturalist [--full] [--max-requests N]
    python -m images_app.cli sync mushroom-observer
    python -m images_app.cli sync maintenance
"""

from __future__ import annotations

import argparse
import getpass
import logging
import re
import sys
from pathlib import Path

from sqlalchemy import func, select

from images_app.db import SessionLocal
from images_app.models import User
from images_app.security import hash_password


def create_admin() -> None:
    name = input("Name: ").strip()
    email = input("Email: ").strip().lower()
    password = getpass.getpass("Password (10+ characters): ")
    if getpass.getpass("Confirm password: ") != password:
        sys.exit("Passwords do not match.")
    if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(password) < 10:
        sys.exit("A name, a valid email and a password of at least 10 characters are required.")
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(func.lower(User.email) == email)):
            sys.exit("That email already has an account; use promote-admin.")
        db.add(User(name=name, email=email, password_hash=hash_password(password), is_admin=True))
        db.commit()
    print("Administrator created.")


def promote_admin(email: str) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
        if user is None:
            sys.exit("No account uses that email.")
        user.is_admin = True
        db.commit()
    print(f"{user.email} is now an administrator.")


def run_sync(args: argparse.Namespace) -> int:
    from images_app import sync
    from images_app.sync.runs import SyncAlreadyRunning

    archive = Path(args.archive) if getattr(args, "archive", None) else None
    jobs = {
        "daily": lambda: sync.daily(),
        "full": lambda: sync.full(archive),
        "taxonomy": lambda: sync.run_steps([("taxonomy", lambda: sync.run_taxonomy(archive))]),
        "inaturalist": lambda: sync.run_steps(
            [("inaturalist", lambda: sync.run_inaturalist(args.full, args.max_requests))]
        ),
        "mushroom-observer": lambda: sync.run_steps(
            [("mushroom_observer", lambda: sync.run_mushroom_observer(args.max_requests))]
        ),
        "maintenance": lambda: sync.run_steps([("maintenance", sync.run_maintenance)]),
    }
    try:
        ok = jobs[args.job]()
    except SyncAlreadyRunning as exc:
        logging.getLogger("images_app").warning("%s; exiting", exc)
        return 75  # EX_TEMPFAIL
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="images", description="Dikarya Images administration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create-admin", help="create an administrator interactively")
    promote = commands.add_parser("promote-admin", help="make an existing user an administrator")
    promote.add_argument("email")
    sync_parser = commands.add_parser("sync", help="import or refresh data")
    sync_parser.add_argument(
        "job",
        choices=["daily", "full", "taxonomy", "inaturalist", "mushroom-observer", "maintenance"],
    )
    sync_parser.add_argument("--full", action="store_true", help="iNaturalist: scan everything")
    sync_parser.add_argument("--max-requests", type=int, default=None)
    sync_parser.add_argument("--archive", help="use a local iNaturalist taxonomy DwC-A zip")
    args = parser.parse_args(argv)

    if args.command == "create-admin":
        create_admin()
    elif args.command == "promote-admin":
        promote_admin(args.email)
    else:
        return run_sync(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
