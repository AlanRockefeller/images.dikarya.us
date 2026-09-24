from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

def _project_root() -> Path:
    configured = os.getenv("APP_ROOT")
    if configured:
        return Path(configured).resolve()
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


APP_ROOT = _project_root()
PACKAGE_ROOT = Path(__file__).resolve().parent
if "APP_ENV" not in os.environ:
    load_dotenv(APP_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    app_env: str
    base_url: str
    database_url: str
    session_secret: str
    fungi_taxon_id: int
    secure_cookies: bool


def get_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development")
    secret = os.getenv("SESSION_SECRET", "")
    if app_env == "production" and len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters in production")
    return Settings(
        app_env=app_env,
        base_url=os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        database_url=os.getenv("DATABASE_URL", "postgresql+psycopg:///images"),
        session_secret=secret or "local-development-only-session-key",
        fungi_taxon_id=int(os.getenv("FUNGI_TAXON_ID", "47170")),
        secure_cookies=app_env == "production",
    )


settings = get_settings()
