#!/usr/bin/env bash
# Deploy the code checked out in this directory: fix ownership and permissions
# so the `images` service can read (but not write) it, then restart. The
# restart runs database migrations first (images-migrate.service).
#
#   cd /var/www/images.dikarya.us/app && git pull --ff-only && scripts/deploy.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c %U "$APP_DIR")"

cd "$APP_DIR"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Uncommitted changes in $APP_DIR; commit or stash them first." >&2
  exit 1
fi

sudo chown -R "$OWNER:images" "$APP_DIR"
sudo chmod -R g+rX,g-w,o-rwx "$APP_DIR"
sudo systemctl restart images.service

for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8017/healthz >/dev/null 2>&1; then
    echo "Deployed $(git rev-parse --short HEAD); the site is answering."
    exit 0
  fi
  sleep 1
done
echo "The site did not come up. Check: journalctl -u images-migrate.service -u images.service -n 80" >&2
exit 1
