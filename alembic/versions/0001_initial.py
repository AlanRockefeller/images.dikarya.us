"""Initial schema for the rebuilt application.

If tables from the first version of the app are present (it tracked migrations
in "alembic_version"), they are renamed with a "legacy_" prefix (along with
their indexes and sequences) rather than dropped, and user accounts are copied
into the new users table. Renaming only needs table ownership, unlike creating
a schema. Once nothing else is needed from them, drop the legacy_* tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-07
"""

from alembic import op
from sqlalchemy import inspect, text

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

LEGACY_TABLES = (
    "alembic_version",
    "votes",
    "image_source_taxa",
    "images",
    "source_taxa",
    "taxon_aliases",
    "taxa",
    "sync_state",
    "rate_limit_events",
    "users",
)

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE users (
    id bigserial PRIMARY KEY,
    name varchar(120) NOT NULL,
    email varchar(320) NOT NULL,
    password_hash varchar(255) NOT NULL,
    is_admin boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email));

CREATE TABLE user_sessions (
    token_hash bytea PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id);

CREATE TABLE rate_limit_hits (
    id bigserial PRIMARY KEY,
    bucket varchar(40) NOT NULL,
    key_hash bytea NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rate_limit_hits_lookup ON rate_limit_hits (bucket, key_hash, occurred_at);

CREATE TABLE taxa (
    id bigserial PRIMARY KEY,
    inat_taxon_id integer NOT NULL UNIQUE,
    parent_id bigint REFERENCES taxa(id) ON DELETE SET NULL,
    name varchar(300) NOT NULL,
    rank varchar(40) NOT NULL,
    rank_level integer NOT NULL,
    ancestor_ids integer[] NOT NULL DEFAULT '{}',
    common_name varchar(300),
    is_active boolean NOT NULL DEFAULT true,
    image_count integer NOT NULL DEFAULT 0,
    subtree_image_count integer NOT NULL DEFAULT 0,
    cover_image_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_synced_at timestamptz
);
CREATE INDEX ix_taxa_parent_id ON taxa (parent_id);
CREATE INDEX ix_taxa_ancestor_ids ON taxa USING gin (ancestor_ids);
CREATE INDEX ix_taxa_name_lower ON taxa (lower(name) text_pattern_ops);
CREATE INDEX ix_taxa_public_name ON taxa (name) WHERE is_active AND subtree_image_count > 0;
CREATE INDEX ix_taxa_public_count ON taxa (subtree_image_count DESC) WHERE is_active AND subtree_image_count > 0;

CREATE TABLE taxon_names (
    id bigserial PRIMARY KEY,
    taxon_id bigint NOT NULL REFERENCES taxa(id) ON DELETE CASCADE,
    name varchar(300) NOT NULL,
    kind varchar(20) NOT NULL,
    source varchar(40) NOT NULL,
    language varchar(10),
    CONSTRAINT uq_taxon_names_taxon_name_kind UNIQUE (taxon_id, name, kind),
    CONSTRAINT ck_taxon_names_kind CHECK (kind IN ('scientific', 'synonym', 'common'))
);
CREATE INDEX ix_taxon_names_taxon_id ON taxon_names (taxon_id);
CREATE INDEX ix_taxon_names_lower ON taxon_names (lower(name) text_pattern_ops);
CREATE INDEX ix_taxon_names_trgm ON taxon_names USING gin (lower(name) gin_trgm_ops);

CREATE TABLE source_names (
    id bigserial PRIMARY KEY,
    source varchar(40) NOT NULL,
    source_name_id varchar(100) NOT NULL,
    name varchar(300) NOT NULL,
    author varchar(300),
    rank varchar(40),
    taxon_id bigint REFERENCES taxa(id) ON DELETE SET NULL,
    status varchar(20) NOT NULL DEFAULT 'unresolved',
    method varchar(20),
    note text,
    candidate_taxon_ids integer[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_source_names_source_id UNIQUE (source, source_name_id),
    CONSTRAINT ck_source_names_status CHECK (status IN ('mapped', 'ambiguous', 'unresolved'))
);
CREATE INDEX ix_source_names_status ON source_names (source, status);

CREATE TABLE images (
    id bigserial PRIMARY KEY,
    source varchar(40) NOT NULL,
    source_image_id varchar(100) NOT NULL,
    source_observation_id varchar(100) NOT NULL,
    observation_url text NOT NULL,
    taxon_id bigint REFERENCES taxa(id) ON DELETE SET NULL,
    source_name_id bigint REFERENCES source_names(id) ON DELETE SET NULL,
    position integer NOT NULL DEFAULT 0,
    thumb_url text NOT NULL,
    medium_url text NOT NULL,
    large_url text NOT NULL,
    width integer,
    height integer,
    photographer varchar(300) NOT NULL,
    license_code varchar(40) NOT NULL,
    attribution text NOT NULL,
    observed_on date,
    faves integer NOT NULL DEFAULT 0,
    its_length integer,
    is_active boolean NOT NULL DEFAULT false,
    inactive_reason varchar(40),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_images_source_image UNIQUE (source, source_image_id),
    CONSTRAINT ck_images_source CHECK (source IN ('inaturalist', 'mushroom_observer'))
);
CREATE INDEX ix_images_taxon_active ON images (taxon_id) WHERE is_active;
CREATE INDEX ix_images_source_observation ON images (source, source_observation_id);
CREATE INDEX ix_images_source_name ON images (source_name_id);

ALTER TABLE taxa ADD CONSTRAINT taxa_cover_image_id_fkey
    FOREIGN KEY (cover_image_id) REFERENCES images(id) ON DELETE SET NULL;

CREATE TABLE votes (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    taxon_id bigint NOT NULL REFERENCES taxa(id) ON DELETE CASCADE,
    image_id bigint NOT NULL REFERENCES images(id) ON DELETE RESTRICT,
    rank integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_votes_user_taxon_image UNIQUE (user_id, taxon_id, image_id),
    CONSTRAINT uq_votes_user_taxon_rank UNIQUE (user_id, taxon_id, rank)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_votes_rank_positive CHECK (rank >= 1)
);
CREATE INDEX ix_votes_taxon_image ON votes (taxon_id, image_id);
CREATE INDEX ix_votes_image ON votes (image_id);
CREATE INDEX ix_votes_updated_at ON votes (updated_at);

-- Votes on images currently shown in the voted taxon's gallery. Galleries at
-- genus rank and below (rank_level <= 20) include photos of subtaxa.
CREATE VIEW visible_votes AS
SELECT v.id, v.user_id, v.taxon_id, v.image_id, v.rank, v.updated_at
FROM votes v
JOIN taxa vt ON vt.id = v.taxon_id
JOIN images i ON i.id = v.image_id AND i.is_active
JOIN taxa it ON it.id = i.taxon_id
WHERE i.taxon_id = v.taxon_id
   OR (vt.rank_level <= 20 AND vt.inat_taxon_id = ANY (it.ancestor_ids));

CREATE TABLE sync_state (
    job varchar(40) PRIMARY KEY,
    cursor jsonb NOT NULL DEFAULT '{}',
    last_started_at timestamptz,
    last_finished_at timestamptz,
    last_success_at timestamptz,
    last_status varchar(20) NOT NULL DEFAULT 'never',
    last_error text,
    records_processed bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sync_runs (
    id bigserial PRIMARY KEY,
    job varchar(40) NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status varchar(20) NOT NULL DEFAULT 'running',
    records_processed bigint NOT NULL DEFAULT 0,
    stats jsonb NOT NULL DEFAULT '{}',
    error text
);
CREATE INDEX ix_sync_runs_job_started ON sync_runs (job, started_at);
"""


def _retire_legacy_tables(bind) -> None:
    existing = set(inspect(bind).get_table_names())
    for table in LEGACY_TABLES:
        if table not in existing:
            continue
        indexes = bind.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
            {"t": table},
        ).scalars()
        for index in list(indexes):
            op.execute(f'ALTER INDEX public."{index}" RENAME TO "{("legacy_" + index)[:63]}"')
        sequences = bind.execute(
            text(
                """
                SELECT DISTINCT s.relname FROM pg_class s
                JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
                JOIN pg_class t ON t.oid = d.refobjid
                WHERE s.relkind = 'S' AND t.relname = :t
                  AND t.relnamespace = 'public'::regnamespace
                """
            ),
            {"t": table},
        ).scalars()
        for sequence in list(sequences):
            op.execute(f'ALTER SEQUENCE public."{sequence}" RENAME TO "{("legacy_" + sequence)[:63]}"')
        op.execute(f'ALTER TABLE public."{table}" RENAME TO "legacy_{table}"')


def upgrade() -> None:
    bind = op.get_bind()
    legacy_present = inspect(bind).has_table("alembic_version")
    if legacy_present:
        _retire_legacy_tables(bind)

    op.execute(SCHEMA)

    if legacy_present and inspect(bind).has_table("legacy_users"):
        op.execute(
            """
            INSERT INTO users (id, name, email, password_hash, is_admin, is_active,
                               created_at, updated_at)
            SELECT id, name, lower(email), password_hash, is_admin, is_active,
                   created_at, updated_at
            FROM legacy_users
            """
        )
        op.execute("SELECT setval('users_id_seq', GREATEST((SELECT max(id) FROM users), 1))")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS visible_votes")
    for table in (
        "sync_runs",
        "sync_state",
        "votes",
        "taxa",
        "images",
        "source_names",
        "taxon_names",
        "rate_limit_hits",
        "user_sessions",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
