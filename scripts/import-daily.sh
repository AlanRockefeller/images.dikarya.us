#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/var/www/images.dikarya.us/app"
IMPORT_DIR="/var/lib/images-import"
mkdir -p "$IMPORT_DIR"

echo "Starting bounded iNaturalist incremental sync"
"$APP_DIR/.venv/bin/python" -m images_app.importers.inaturalist_incremental --stage-dir "$IMPORT_DIR"

echo "Streaming official Mushroom Observer nightly CSV exports"
"$APP_DIR/.venv/bin/python" -m images_app.importers.mushroom_observer \
  --observations "https://mushroomobserver.org/observations.csv" \
  --images-observations "https://mushroomobserver.org/images_observations.csv" \
  --images "https://mushroomobserver.org/images.csv" \
  --names "https://mushroomobserver.org/names.csv" \
  --stage-dir "$IMPORT_DIR"

echo "Daily image metadata synchronization completed"
