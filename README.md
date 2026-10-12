# Dikarya Images

<https://images.dikarya.us> lets registered users rank the most useful photographs of each fungus. Every photo comes from an observation with an **ITS DNA barcode**, so identifications are as reliable as field photos get. iNaturalist is the canonical taxonomy. Photos come from iNaturalist and Mushroom Observer and are hot-linked, never copied.

The public site covers Fungi, but nothing in the schema or code assumes that. Add root taxa (`ROOT_TAXON_IDS`) to expose other groups.

## How it fits together

```
iNaturalist taxonomy DwC-A ─┐
iNaturalist observations API ├─► sync jobs (images_app.sync) ─► PostgreSQL ◄─ FastAPI + Jinja (images_app.main)
Mushroom Observer API ───────┘        systemd timers                           nginx ─► browsers
```

- **Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Jinja2, PostgreSQL 16** (with `pg_trgm`). Server-rendered pages. One dependency-free script (`static/app.js`) adds search suggestions, infinite scroll, the photo viewer and ranking. Without JavaScript, pages still browse and ranking falls back to plain form posts.
- `src/images_app/`
  - `models.py`: the schema (see below). `alembic/versions/` holds the migrations.
  - `catalog.py`: taxon search, directory and tree queries. `gallery.py`: galleries, results and rankings. `ranking.py`: saving a ranking.
  - `routes/`: `public`, `account`, `ranking` (JSON API plus no-JS fallback) and `admin`.
  - `security.py`: argon2id passwords, server-side sessions, CSRF and rate limits.
  - `sync/`: `taxonomy.py`, `inaturalist.py` and `mushroom_observer.py` import data; `its.py` decides what counts as ITS evidence; `maintenance.py` recomputes counts, covers and vote order; `runs.py` handles locking and run history.
  - `cli.py`: the `images` command (admin accounts, syncs).

### Data model

| Table | Purpose |
|---|---|
| `taxa` | iNaturalist taxa. `inat_taxon_id` (unique) is the identity used in URLs (`/taxon/48715/amanita-muscaria`; the slug is cosmetic). `ancestor_ids` is an array of iNat IDs (GIN indexed). Also holds denormalized `image_count`, `subtree_image_count` and `cover_image_id`. |
| `taxon_names` | Searchable names: accepted, synonyms (renamed taxa, iNat taxon swaps, manual MO mappings) and English common names. Trigram indexed. |
| `images` | Metadata for one remote photo: source, source IDs, URLs (thumb/medium/large), dimensions, photographer, license, attribution, observation date, faves and ITS length. Unique on `(source, source_image_id)`. Hidden photos keep their row, with `is_active = false` and an `inactive_reason`. |
| `source_names` | Mushroom Observer names and how each maps to an iNat taxon (`exact`, `rank`, `synonym`, `alias`, `manual`, or `ambiguous`/`unresolved`), with candidate taxa, for auditing and review. |
| `votes` | One row per (user, taxon, image) with a `rank`. Ranks are contiguous from 1 per user and taxon. Unique on (user, taxon, image) and on (user, taxon, rank), the latter deferred so a whole ranking can be rewritten in one transaction. |
| `visible_votes` (view) | Votes on photos currently shown in the voted taxon's gallery. Aggregates and covers read from it. |
| `users`, `user_sessions`, `rate_limit_hits` | Accounts (case-insensitive unique email), server-side sessions (only a hash of the cookie token is stored) and login/registration throttling. |
| `sync_state`, `sync_runs` | Cursor, last start/finish/success, status, error and record counts per job, plus a run history with statistics. |

**Galleries.** Taxa at genus rank and below show their own photos plus those of their subtaxa, so a species page includes its varieties. A "Hide photos of subtaxa" toggle narrows this. Votes always belong to the taxon page where they were cast. Higher ranks show their subtaxa as cards. (`SUBTREE_MAX_RANK_LEVEL` in `models.py` and the `visible_votes` view must agree.)

**Votes are never thrown away.** If a photo disappears upstream, loses its license or is re-identified, its votes stay in `votes` but drop out of `visible_votes`. Maintenance renumbers each affected ranking so the visible picks are still 1, 2, 3… and the hidden ones follow. No score is stored. Results are computed from the raw votes: the *Community results* tab shows 1st/2nd/3rd-place counts, number of people and mean rank, ordered like a medal table. Other algorithms can be added later without changing the schema.

## What gets imported

**Only observations with ITS evidence.**
- iNaturalist: the **DNA Barcode ITS** observation field (id 2330) must contain a nucleotide sequence of at least 50 bases (FASTA headers, wrapping and numbering are fine) or point to the sequenced observation ("see inaturalist.org/observations/123…"). Placeholders like "pending" don't count. The API's field filter means only those observations are ever requested.
- Mushroom Observer: the observation needs a sequence record whose locus names ITS (`ITS`, `DNA Barcode ITS`, `nrITS`, `ITS1F-ITS4`, …) and that has bases or a GenBank/UNITE accession.

**Only displayable licenses:** CC0, public domain, CC BY, CC BY-SA, CC BY-NC and CC BY-NC-SA (`DISPLAY_LICENSES`). NC licenses are allowed because this site is non-commercial. They cover about three quarters of the CC-licensed ITS photos on iNaturalist (October 2026: 480k of 630k), so excluding them would shrink the site from ~600k photos to ~120k. If the site ever becomes commercial, remove the NC codes and run `sync full`. ND licenses are excluded because thumbnails are cropped. Photos with other licenses are stored hidden (`inactive_reason = 'license'`), never shown.

**Mushroom Observer names** map to iNat taxa by exact accepted name (MO's "var."/"f." connectors normalized), then MO's synonym/misspelling links, then known aliases. If several iNat taxa fit, the name is marked *ambiguous*. Group, "sensu", "cf." and provisional names stay *unresolved*. Their photos stay hidden until an admin maps them at **/admin/names**. Manual mappings are never overwritten. MO observations that link to an iNat observation already imported are hidden as duplicates.

## Local development

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env            # set DATABASE_URL and SECRET_KEY
createdb images && psql images -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'
alembic upgrade head
python -m images_app.cli sync full   # ~65 min: taxonomy, every iNat ITS observation, MO
uvicorn images_app.main:app --reload
```

For a quicker start, `python -m images_app.cli sync inaturalist --full --max-requests 50` loads about 10,000 observations. It stops with a "budget" error and resumes from there next time.

### Tests

The tests need a disposable PostgreSQL database (they drop and recreate its `public` schema). They never touch the network: the HTTP clients get mock transports.

```sh
createdb images_test
TEST_DATABASE_URL=postgresql+psycopg:///images_test pytest
```

The suite covers registration, duplicate emails, login/logout, session fixation, CSRF, rate limiting, the singular-hostname redirect, ranking (first pick = 1, gaps closing, reordering, uniqueness constraints, independent users, hidden and re-identified photos, subtaxa), the directory, search, gallery paging, results, iNat and MO import idempotency, ITS detection, name mapping, the taxonomy import, and the migration from the first version's schema.

## Command line

```sh
python -m images_app.cli create-admin                  # prompts for name, email, password
python -m images_app.cli promote-admin person@example.org
python -m images_app.cli sync daily        # iNat incremental + MO + maintenance
python -m images_app.cli sync full         # taxonomy + iNat full scan + MO + maintenance
python -m images_app.cli sync taxonomy [--archive FILE]
python -m images_app.cli sync inaturalist [--full] [--max-requests N]
python -m images_app.cli sync mushroom-observer
python -m images_app.cli sync maintenance  # recount, covers, vote order
```

All sync commands take one PostgreSQL advisory lock, so two never run at once (a second one exits with status 75). Each step is recorded in `sync_runs`/`sync_state` and shown at **/admin**. A failed step doesn't stop the later ones, but the command exits non-zero.

### Synchronization details

- **Taxonomy** (part of `sync full`): downloads `inaturalist-taxonomy.dwca.zip` (~80 MB, reused if under 20 days old) to `IMPORT_DATA_DIR`. It streams the archive twice and upserts every descendant of the root taxa (~74,000 for Fungi). Taxa missing from a new archive are marked inactive, never deleted. Renamed taxa keep their old name as a synonym, and for taxa that disappear, iNat's API is asked which taxon replaced them. The job refuses to deactivate more than 20% of taxa at once, in case an archive is truncated.
- **iNaturalist full scan**: pages through `/v2/observations?taxon_id=47170&field:DNA Barcode ITS=` by ascending ID, 200 per page, about 1 request per second with a descriptive User-Agent and retries with backoff. That's about 950 requests, well within iNat's [recommended limits](https://www.inaturalist.org/pages/api+recommended+practices). Progress is saved after every page, so an interrupted scan resumes. Only after a *complete* scan are photos that weren't seen marked `missing_upstream`. This catches deleted observations, removed photos, license changes and removed ITS fields.
- **iNaturalist incremental** (daily): fetches only observations updated since the last success (with an hour's overlap). It updates re-identifications, licenses and faves, and hides photos removed from those observations. With no previous full scan, or a backlog larger than half its request budget, it runs a full scan instead.
- **Mushroom Observer**: reads all sequences (~33 requests), then fetches the ITS observations by ID with consensus names and images (about 29,000 observations in ~290 requests, 5 seconds apart: roughly half an hour). Every run is a complete refresh; photos no longer present are hidden.
- **Safety checks**: a run that would hide more than 20% of the photos currently shown from a source (or deactivate more than 20% of taxa) stops with an error instead. That usually means an upstream outage or a truncated response. Investigate, then rerun.
- **Maintenance**: renumbers votes, recounts photos per taxon and subtree, and picks cover photos. A cover is the community's favorite (most first places); without votes, a species uses its most-faved photo and higher taxa borrow their best-photographed species' cover.

## Production

The server layout (already in place) is:

- Code: `/var/www/images.dikarya.us/app` (this repository, owned by the deploying user, readable by group `images`). Virtualenv: `app/.venv`. Services run from `src/` via `PYTHONPATH`, so a `git pull` plus a restart deploys.
- Secrets: `/var/www/images.dikarya.us/.env`, read by systemd as `EnvironmentFile`. Needs `APP_ENV=production`, `BASE_URL=https://images.dikarya.us`, `DATABASE_URL=postgresql+psycopg:///images` (peer auth as Unix user `images`) and `SECRET_KEY` (or the older name `SESSION_SECRET`), at least 32 random characters. Optional: `CONTACT_EMAIL`, `ROOT_TAXON_IDS`, `DISPLAY_LICENSES`.
- `deploy/images.service`: uvicorn on `127.0.0.1:8017`, as user `images`, sandboxed. It `Requires=` `images-migrate.service`, which runs `alembic upgrade head` first.
- `deploy/images-import-daily.{service,timer}`: daily at 04:10 UTC plus a random delay of up to 30 minutes, runs `scripts/import-daily.sh` (→ `sync daily`).
- `deploy/images-import-reconcile.{service,timer}`: monthly, runs `scripts/import-reconcile.sh` (→ `sync full`).
- `deploy/images.nginx.conf`: TLS for `images.dikarya.us`, and a permanent redirect from `image.dikarya.us` (HTTP and HTTPS) to `https://images.dikarya.us$request_uri`, keeping the path and query. The app also redirects that hostname itself. Certificates come from the existing Certbot setup. `deploy/images-acme-bootstrap.nginx.conf` is the HTTP-only vhost used to issue the first certificate.

### Deploying an update

```sh
cd /var/www/images.dikarya.us/app && git pull --ff-only && scripts/deploy.sh
```

`scripts/deploy.sh` resets ownership to `<owner>:images` and permissions to group-read-only. New files would otherwise get the owner's group and be unreadable by the service. It then restarts `images.service`, which runs migrations first, and waits for `/healthz`. If something fails: `journalctl -u images-migrate.service -u images.service -n 80`.

If unit files in `deploy/` changed, copy them to `/etc/systemd/system/` (back up the old ones first) and run `sudo systemctl daemon-reload`. If nginx config changes, back up the current vhost, install the new one, run `sudo nginx -t`, then `sudo systemctl reload nginx`.

### Upgrading from the first version of the site

Migration `0001` detects the first version's tables (it used the `alembic_version` table). It renames them, with their indexes and sequences, to `legacy_*`, copies user accounts into the new `users` table, and creates the new schema. Nothing is dropped. Photo and taxon data is then rebuilt by a full sync:

```sh
sudo systemctl restart images.service
sudo systemctl start images-import-reconcile.service      # full sync, about an hour
journalctl -fu images-import-reconcile.service
```

When you no longer need the old tables: `sudo -u images psql images -c 'DROP TABLE legacy_votes, legacy_image_source_taxa, legacy_images, legacy_source_taxa, legacy_taxon_aliases, legacy_taxa, legacy_sync_state, legacy_rate_limit_events, legacy_users, legacy_alembic_version'`.

### First administrator

Run the CLI as the `images` user with the production environment:

```sh
sudo systemd-run --pty --wait --collect --uid=images --gid=images \
  -p EnvironmentFile=/var/www/images.dikarya.us/.env \
  -p WorkingDirectory=/var/www/images.dikarya.us/app \
  -E PYTHONPATH=/var/www/images.dikarya.us/app/src \
  /var/www/images.dikarya.us/app/.venv/bin/python -m images_app.cli create-admin
```

Or register on the site and run `promote-admin you@example.org` the same way. Accounts from the first version of the site carry over.

### Monitoring

- `/healthz` returns `{"status": "ok"}` when the database answers.
- `/admin` shows each job's status, last success, errors, per-run statistics and why photos are hidden.
- `journalctl -u images-import-daily.service` and `systemctl list-timers 'images*'` show scheduled jobs.

### Activity and security logs

Besides the service log (`journalctl -u images`), the app writes three event logs to the journal, each under its own name:

| Log | Contents |
|---|---|
| `images-activity` | Sign-ups, logins and logouts; every ranking change (what was added, removed or reordered); searches; admin name mappings. Problems people hit: failed logins, expired forms, invalid input, broken internal links, server errors (with the reference shown on the error page), and failed saves or script errors reported by browsers. |
| `images-attack` | Requests that look aimed at this site, whether or not the sender is logged in: SQL, script, template or command injection and path traversal sent to real pages or form fields, attack tools (sqlmap, Nikto, …), forged cross-site requests, made-up session cookies, password guessing (rate limit reached), members probing `/admin`, ranking submissions with photo IDs the page never offered, open-redirect attempts and malformed API calls. After any of these, **everything that IP does for the next hour is also logged here**, so you can see what the attacker tried next. |
| `images-scanner` | Background noise from bots that probe every server: requests for WordPress, PHP, `.env`, `.git` and other software this site doesn't run, payloads sent to pages that don't exist, known scanner user agents, anonymous `/admin` probes, blind form posts and blocked sign-up bots. |

Each line is `event=<name> key=value …` with the IP, user (`id:email`), path, request ID (`rid`, also sent as the `X-Request-ID` header) and user agent. Client-supplied values are quoted and escaped, so they can't break or forge log lines. Passwords are never logged.

```sh
journalctl -t images-attack -f                      # follow targeted attacks live
journalctl -t images-attack --since today -o cat     # today's, without the syslog prefix
journalctl -t images-activity -g 'ranking.save'      # ranking changes
journalctl -t images-activity -g 'rid=3f9a0c1b2d4e'  # everything about one request (reference from an error page)
journalctl -t images-scanner --since -1d -o cat | grep -o 'ip=[^ ]*' | sort | uniq -c | sort -rn | head   # noisiest scanners
```

To also have them as files in `/var/log/images/{activity,attack,scanner}.log`, install `deploy/images-logs.rsyslog.conf` and `deploy/images-logs.logrotate`; the commands are at the top of the rsyslog file.

**Sign-up bots** are stopped by a hidden "website" field that people never see but bots fill in, and by a signed timestamp: forms submitted less than 2 seconds after the page loaded, or with a missing or forged timestamp, are refused. A person caught by this sees "please submit it again" with their entries kept, and the second attempt goes through. Blocked attempts appear in `images-scanner` as `register.bot`.

### Backups

`pg_dump -Fc images > images-$(date +%F).dump` daily, with copies off the server. Restore tests matter more than the dump. The irreplaceable data is `users` and `votes`; everything else can be rebuilt with `sync full`. Keep `.env` out of git and backups encrypted.

## Licensing notes

Photos stay on iNaturalist's and Mushroom Observer's servers and are shown with photographer, license (linked to the deed) and a link to the source observation. Only the licenses listed above are displayed. Photos with unclear or restrictive licenses are kept as hidden metadata. The site is non-commercial, which the CC BY-NC photos require. Taxonomy data is from iNaturalist (CC0/CC BY).

## Security

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License

The code is under the [MIT License](LICENSE). Photos are not part of this repository; each remains under its photographer's license on iNaturalist or Mushroom Observer.
