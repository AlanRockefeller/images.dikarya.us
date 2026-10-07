"""Runtime settings, read once from the environment.

Production values come from the systemd EnvironmentFile. For local development
a `.env` file in the project root is loaded when APP_ENV is not already set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

if "APP_ENV" not in os.environ:
    load_dotenv(PROJECT_ROOT / ".env")

# Licenses whose images may be displayed. NC licenses are allowed because the
# site is non-commercial; ND licenses are excluded because galleries resize and
# crop thumbnails. Override with DISPLAY_LICENSES (comma separated) if needed.
DEFAULT_DISPLAY_LICENSES = "cc0,pd,cc-by,cc-by-sa,cc-by-nc,cc-by-nc-sa"


def _int_list(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.replace(" ", "").split(",") if part)


@dataclass(frozen=True)
class Settings:
    app_env: str
    base_url: str
    database_url: str
    secret_key: str
    root_taxon_ids: tuple[int, ...]
    display_licenses: frozenset[str]
    data_dir: Path
    contact_email: str

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def canonical_host(self) -> str:
        return self.base_url.split("://", 1)[-1].split("/", 1)[0]


def load_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development")
    # SESSION_SECRET is the historical name; SECRET_KEY is accepted too.
    secret = os.getenv("SECRET_KEY") or os.getenv("SESSION_SECRET", "")
    if app_env == "production" and len(secret) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters in production")
    database_url = os.getenv("DATABASE_URL", "postgresql+psycopg:///images")
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    licenses = os.getenv("DISPLAY_LICENSES", DEFAULT_DISPLAY_LICENSES)
    return Settings(
        app_env=app_env,
        base_url=os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        database_url=database_url,
        secret_key=secret or "development-only-secret-key-not-for-production",
        root_taxon_ids=_int_list(os.getenv("ROOT_TAXON_IDS", os.getenv("FUNGI_TAXON_ID", "47170"))),
        display_licenses=frozenset(code.strip() for code in licenses.split(",") if code.strip()),
        data_dir=Path(os.getenv("IMPORT_DATA_DIR", "/var/lib/images-import")),
        contact_email=os.getenv("CONTACT_EMAIL", ""),
    )


settings = load_settings()
