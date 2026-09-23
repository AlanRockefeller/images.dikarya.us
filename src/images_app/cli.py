from __future__ import annotations

import argparse
import getpass
import re

from sqlalchemy import func, select

from images_app.db import SessionLocal
from images_app.models import User
from images_app.security import hash_password


def create_admin() -> None:
    name = input("Name: ").strip()
    email = input("Email: ").strip().lower()
    password = getpass.getpass("Password (12+ characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise SystemExit("A name and valid email are required.")
    if len(password) < 12 or password != confirm:
        raise SystemExit("Password must have at least 12 characters and both entries must match.")
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(func.lower(User.email) == email)):
            raise SystemExit("That email already has an account.")
        db.add(User(name=name, email=email, password_hash=hash_password(password), is_admin=True))
        db.commit()
    print("Administrator account created.")


def promote_admin(email: str) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
        if user is None:
            raise SystemExit("No user found for that email.")
        user.is_admin = True
        db.commit()
    print("Administrator access granted.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dikarya Images account administration")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("create-admin", help="create the first administrator interactively")
    promote = actions.add_parser("promote-admin", help="grant admin privileges to an existing user")
    promote.add_argument("email")
    args = parser.parse_args()
    if args.action == "create-admin":
        create_admin()
    else:
        promote_admin(args.email)


if __name__ == "__main__":
    main()
