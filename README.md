# Dikarya Images

A server-rendered FastAPI site for browsing licensed taxon photographs and letting registered users rank useful representations. iNaturalist is the canonical taxonomy. The first public directory is Fungi; the database and routes use generic taxa so other life groups can be enabled later.

## Architecture

- FastAPI, Jinja2, locally vendored HTMX 2.0.4, and plain CSS; the browser loads source-hosted images directly. The pinned HTMX license is in `third_party/`.
- PostgreSQL through SQLAlchemy 2.x, with Alembic migrations.
- Argon2id password hashing and signed, HttpOnly session cookies with per-session CSRF tokens.
- [iNaturalist Open Data](https://github.com/inaturalist/inaturalist-open-data) monthly metadata for bulk taxonomy/photo imports, the rate-limited iNaturalist API for bounded daily updates, and [Mushroom Observer's official nightly CSV exports](https://github.com/MushroomObserver/mushroom-observer/blob/main/README_API.md#csv-files) for its bulk import.
- No image files are stored here. Each displayed image includes source, creator, license, and a source record link.
- A restrictive same-origin Content Security Policy keeps executable code local; external hosts are permitted only as image sources.

## Local development

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# Edit .env for your local PostgreSQL role/database and generate SESSION_SECRET.
createdb images
alembic upgrade head
uvicorn images_app.main:app --reload
```

For peer auth, run the app under a matching PostgreSQL role or set `DATABASE_URL` to the local socket and role. Production uses `postgresql+psycopg:///images` under the `images` Unix account.

## Database and admin

Apply schema changes with `alembic upgrade head`; inspect with `alembic current` and roll back one migration with `alembic downgrade -1` when appropriate. Set a first administrator without placing a password in shell history:

```sh
python -m images_app.cli create-admin
```

Run the command as the `images` account so PostgreSQL peer authentication uses the existing Unix role. It prompts for name, email, and password without putting the password in shell history. A current admin can grant admin access to another account with `python -m images_app.cli promote-admin EMAIL`.

## Tests

Run `pytest`. Tests use an isolated SQLite database where possible and never contact external sites. PostgreSQL-specific migration/import checks can use a disposable PostgreSQL database by setting `TEST_DATABASE_URL`.

## Import and synchronization

Import commands are restartable and upsert by source identifiers. They stream tab-separated/gzip input and report counts. Do not download or serve photos without an allowed license; the iNaturalist adapter allows CC0, the Public Domain Mark, CC BY, and CC BY-SA for display by default. Change that policy only after review.

### iNaturalist bulk import

The importer can read the official bucket's `taxa.csv.gz`, `observations.csv.gz`, `observers.csv.gz`, and `photos.csv.gz` directly over HTTPS, and reads the current taxonomy archive from a local ZIP. The four compressed CSVs total more than 33 GB, so do not download them to this server. The open-data files are tab-separated and gzip-compressed; the DwC-A ZIP's `taxa.csv` is streamed directly from the archive. Then run:

```sh
python -m images_app.importers.inaturalist \
  --taxa https://inaturalist-open-data.s3.amazonaws.com/taxa.csv.gz \
  --observations https://inaturalist-open-data.s3.amazonaws.com/observations.csv.gz \
  --observers https://inaturalist-open-data.s3.amazonaws.com/observers.csv.gz \
  --photos https://inaturalist-open-data.s3.amazonaws.com/photos.csv.gz \
  --taxonomy-dwca /var/lib/images-import/inaturalist-taxonomy.dwca.zip \
  --full-reconciliation
```

The DwC-A supplies the accepted fungal taxonomy (including taxa without images); the licensed observation metadata supplies image references, observer attribution, and photo licenses. The importer streams remote gzip data into a temporary SQLite staging file to build ancestry and join observation/photo records without saving the source dumps or holding them in memory. It validates HTTPS hosts and follows only same-source redirects. Original and medium-size S3 URLs are derived from photo ID and extension; only metadata is imported. A successful reconciliation deactivates stale images missing from the snapshot after a 45-day grace period, which protects recent daily API records not present in the monthly metadata yet. Votes remain intact. The monthly job streams roughly 33 GB over the network and runs at low CPU and I/O priority.

### Daily incremental synchronization

`python -m images_app.importers.inaturalist_incremental` asks the iNaturalist API only for observations updated since the persisted cursor (page size 200, custom User-Agent, at least one second between requests, bounded with an ID cursor). If a long scan hits its request cap, it resumes by ID, then replays the entire same update window from ID zero before advancing the cursor. That replay catches lower-ID records edited while the backlog was being processed. The API is not used for bulk backfill. `python -m images_app.importers.mushroom_observer --observations https://mushroomobserver.org/observations.csv --images-observations https://mushroomobserver.org/images_observations.csv --images https://mushroomobserver.org/images.csv --names https://mushroomobserver.org/names.csv` streams the official nightly exports. Despite their `.csv` names, the live export files are tab-delimited; the four endpoint headers and sample rows were checked on 2026-09-24. The importer bounds SQLite name-ID lookups and persists every image-to-name association. MO names are matched by exact accepted scientific name, MO synonym links, or a unique known alias. If an image has conflicting names, it remains unpublished until every associated name maps to the same iNaturalist taxon. Ambiguous/unmatched names remain in `source_taxa` for review; no fuzzy guess is published. Manual mappings recompute affected images and image counts.

### Full reconciliation

Run the iNaturalist bulk command monthly with a fresh open-data snapshot and taxonomy DwC-A using `--full-reconciliation`. Reconciliation marks inactive/missing taxa based on the full accepted taxonomy and deactivates absent image references after the grace period. The importer streams all four metadata files and keeps only the taxonomy archive and bounded SQLite join state on disk. MO's current structured source is its nightly database CSV export; rerunning that importer is idempotent and updates source records in place.

All import commands create/update `sync_state` records and return nonzero on failure. `flock` in the provided systemd units prevents overlapping daily jobs. Store downloaded files under a separately managed data directory, not the source tree.

## Production deployment

The production service runs as Unix user `images`, binds only to `127.0.0.1:8017`, and has read/execute access to code. Keep the source owned by `alan`; do not grant the service account write access. The unit uses `/var/www/images.dikarya.us/.env` as a systemd `EnvironmentFile`, so systemd reads it without exposing it to the app process as a file. The app reads `APP_ENV`, `BASE_URL`, `DATABASE_URL`, and `SESSION_SECRET` from the environment. The PostgreSQL role and database already exist; Unix-socket peer auth matches `User=images`. Uvicorn trusts forwarded headers only from `127.0.0.1`; nginx must overwrite `X-Forwarded-For` with `$remote_addr` so login and registration throttles use the connecting client address.

Example service, timer, and nginx files are in `deploy/`. Before installing them, inspect the host's active vhosts and units. Back up any file being changed, run `nginx -t`, then reload nginx. The singular hostname redirects with `$request_uri` to the plural canonical HTTPS host. The nginx example expects a certificate covering both `images.dikarya.us` and `image.dikarya.us`; issue/expand it with the host's existing Certbot setup after DNS points at the server. Do not reload until certificate paths exist and `nginx -t` succeeds.

For a new certificate, first install `deploy/images-acme-bootstrap.nginx.conf` as a new vhost, enable it, run `nginx -t`, and reload. It serves only ACME challenge files and returns 503 for other HTTP requests while the certificate is being issued. Then run `certbot certonly --webroot -w /var/www/letsencrypt -d images.dikarya.us -d image.dikarya.us`. After Certbot succeeds, back up the bootstrap vhost file, replace it with `deploy/images.nginx.conf`, run `nginx -t`, and reload. The final vhost redirects both HTTP names to HTTPS and redirects the singular HTTPS hostname to the canonical plural hostname while preserving path and query string.

Example install flow (run as an administrator after reviewing the sample files). The recursive ownership and mode commands are idempotent; let each finish before continuing. They leave `alan` as owner and give `images` read/execute access only.

```sh
python3.12 -m venv /var/www/images.dikarya.us/app/.venv
systemctl stop images.service images-import-daily.timer images-import-reconcile.timer
systemctl stop images-import-daily.service images-import-reconcile.service
/var/www/images.dikarya.us/app/.venv/bin/pip install /var/www/images.dikarya.us/app
chown -R alan:images /var/www/images.dikarya.us/app
chmod -R g+rX,g-w,o-rwx /var/www/images.dikarya.us/app
install -o root -g root -m 0644 deploy/images-migrate.service /etc/systemd/system/images-migrate.service
install -o root -g root -m 0644 deploy/images.service /etc/systemd/system/images.service
install -o root -g root -m 0644 deploy/images-import-daily.service /etc/systemd/system/images-import-daily.service
install -o root -g root -m 0644 deploy/images-import-daily.timer /etc/systemd/system/images-import-daily.timer
install -o root -g root -m 0644 deploy/images-import-reconcile.service /etc/systemd/system/images-import-reconcile.service
install -o root -g root -m 0644 deploy/images-import-reconcile.timer /etc/systemd/system/images-import-reconcile.timer
systemctl daemon-reload
systemctl reset-failed images.service images-import-daily.service images-import-reconcile.service
systemctl start images-migrate.service
systemctl enable --now images.service
# Run the initial imports manually, then review their status.
systemctl start images-import-daily.service
systemctl start --no-block images-import-reconcile.service
journalctl -fu images-import-reconcile.service
# After the initial reconciliation succeeds, enable unattended schedules.
systemctl enable --now images-import-daily.timer images-import-reconcile.timer
```

`journalctl -fu` follows the log and does not exit on its own. Pressing Ctrl-C there stops only the log viewer; a job started with `systemctl start --no-block` continues in the background. Check it with `systemctl status images-import-reconcile.service`.

If resuming after an interrupted deployment, skip the virtual-environment and `pip install` lines when the venv and dependencies are already present. The systemd units put `src/` first on `PYTHONPATH`, so the app runs from the checked-out source; installing the package again is not needed to pick up source changes. The ownership and mode repair is safe to repeat.

The daily unit streams the official nightly MO CSVs and runs the bounded iNaturalist API synchronizer. The monthly reconcile unit streams full iNaturalist metadata, including the large photo metadata table, and stores only the taxonomy archive and bounded join state. Check available bandwidth before enabling the monthly timer. Run the initial imports manually and review their results before enabling unattended schedules. Both import units create a private `/var/lib/images-import` state directory and use file locks to prevent overlapping runs of the same importer.

After deployment, create the first administrator interactively with `sudo -u images env APP_ROOT=/var/www/images.dikarya.us/app PYTHONPATH=/var/www/images.dikarya.us/app/src /var/www/images.dikarya.us/app/.venv/bin/python -m images_app.cli create-admin`.

## Nginx and systemd

The sample `deploy/images.nginx.conf` is a standalone server block. It does not replace the global nginx configuration or another site's file. It supports Certbot HTTP challenges from `/var/www/letsencrypt`. Verify DNS and certificate availability, install a new site file, then:

```sh
nginx -t
systemctl reload nginx
```

The service logs to journald (`journalctl -u images -f`); importer status/errors are also recorded in `sync_state`. `/healthz` performs a database query and returns 503 when PostgreSQL cannot be reached. Timer logs can be inspected with `journalctl -u images-import-daily.service`, `journalctl -u images-import-reconcile.service`, and `systemctl list-timers`.

## Browsing and rankings

`/taxa` defaults to fungal species with images. The rank selector can show any other rank or all ranks. Galleries are paginated at 72 image references per page, with source, attribution, license, and external record links. Signed-in users can select or remove images immediately, reorder with the arrow controls, or drag selected cards by their handle; server-side validation persists contiguous ranks. Aggregates are queried from the individual rankings rather than stored as a fixed score.

## Backups

Back up PostgreSQL regularly with `pg_dump -Fc images` and test restores. Keep the `.env` and database backups restricted and encrypted; neither belongs in Git. The app stores no image bytes. The importers stream upstream data and retain no full source dumps.

## Licensing

The site displays remote images and must honor the image-specific license, creator attribution, and source link. iNaturalist's open image collection includes licensed Creative Commons/public-domain media, but each photo has its own terms. The default importer admits CC0, the Public Domain Mark, CC BY, and CC BY-SA and records attribution. Mushroom Observer image licensing can vary; the importer stores license and attribution metadata and publishes only entries with an explicit allowed license. If licensing, attribution, or source URL cannot be validated, keep the record inactive until reviewed. A source can remove or change an image; reconciliation deactivates records rather than deleting vote history.
