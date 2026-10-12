#!/usr/bin/env bash
# Called by images-import-daily.service. Kept as a thin wrapper so installed
# systemd units keep working; the logic lives in `python -m images_app.cli`.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export IMPORT_DATA_DIR="${IMPORT_DATA_DIR:-/var/lib/images-import}"
exec "$APP_DIR/.venv/bin/python" -m images_app.cli sync daily
