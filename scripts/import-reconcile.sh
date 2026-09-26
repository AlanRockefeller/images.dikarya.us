#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/var/www/images.dikarya.us/app"
IMPORT_DIR="/var/lib/images-import"
SNAPSHOT_DIR="$IMPORT_DIR/inaturalist-open-data"
mkdir -p "$SNAPSHOT_DIR"

TAXONOMY_ARCHIVE="$SNAPSHOT_DIR/inaturalist-taxonomy.dwca.zip"
echo "Downloading the current 80 MB official iNaturalist taxonomy archive"
curl --fail --location --retry 4 --retry-delay 15 \
  --output "${TAXONOMY_ARCHIVE}.part" \
  "https://www.inaturalist.org/taxa/inaturalist-taxonomy.dwca.zip"
mv "${TAXONOMY_ARCHIVE}.part" "$TAXONOMY_ARCHIVE"

echo "Reconciling taxonomy and licensed photos from DNA Barcode ITS observations only"
"$APP_DIR/.venv/bin/python" -m images_app.importers.inaturalist_reconcile \
  --taxonomy-dwca "$TAXONOMY_ARCHIVE" \
  --stage-dir "$IMPORT_DIR"

echo "Monthly iNaturalist reconciliation completed"
