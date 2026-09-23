#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/var/www/images.dikarya.us/app"
IMPORT_DIR="/var/lib/images-import"
SNAPSHOT_DIR="$IMPORT_DIR/inaturalist-open-data"
mkdir -p "$SNAPSHOT_DIR"

# The four official metadata files total more than 33 GB compressed. The importer
# reads them as HTTPS streams and retains only its bounded SQLite join tables.
# Never save those source dumps on this host's comparatively small system disk.
TAXONOMY_ARCHIVE="$SNAPSHOT_DIR/inaturalist-taxonomy.dwca.zip"
echo "Downloading the current 80 MB official iNaturalist taxonomy archive"
curl --fail --location --retry 4 --retry-delay 15 \
  --output "${TAXONOMY_ARCHIVE}.part" \
  "https://www.inaturalist.org/taxa/inaturalist-taxonomy.dwca.zip"
mv "${TAXONOMY_ARCHIVE}.part" "$TAXONOMY_ARCHIVE"

echo "Reconciling against streamed iNaturalist metadata snapshot"
"$APP_DIR/.venv/bin/python" -m images_app.importers.inaturalist \
  --taxa "https://inaturalist-open-data.s3.amazonaws.com/taxa.csv.gz" \
  --observations "https://inaturalist-open-data.s3.amazonaws.com/observations.csv.gz" \
  --observers "https://inaturalist-open-data.s3.amazonaws.com/observers.csv.gz" \
  --photos "https://inaturalist-open-data.s3.amazonaws.com/photos.csv.gz" \
  --taxonomy-dwca "$TAXONOMY_ARCHIVE" \
  --stage-dir "$IMPORT_DIR" \
  --full-reconciliation

echo "Monthly iNaturalist reconciliation completed"
