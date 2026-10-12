"""Database schema.

Everything is expressed in generic terms (taxon, image, source, vote) so other
branches of the tree of life can be enabled by adding root taxon IDs.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SOURCES = ("inaturalist", "mushroom_observer")
# Galleries of taxa at this rank level or below (genus = 20) include the photos
# of all their subtaxa. Must match the visible_votes view in the migrations.
SUBTREE_MAX_RANK_LEVEL = 20


class Base(DeclarativeBase):
    pass


def created_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()


class UserSession(Base):
    """Server-side login sessions. The cookie holds a random token; only its hash is stored."""

    __tablename__ = "user_sessions"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = created_column()
    last_seen_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="joined")


class RateLimitHit(Base):
    __tablename__ = "rate_limit_hits"
    __table_args__ = (Index("ix_rate_limit_hits_lookup", "bucket", "key_hash", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(40))
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    occurred_at: Mapped[datetime] = created_column()


class Taxon(Base):
    __tablename__ = "taxa"
    __table_args__ = (
        Index("ix_taxa_parent_id", "parent_id"),
        Index("ix_taxa_ancestor_ids", "ancestor_ids", postgresql_using="gin"),
        Index("ix_taxa_name_lower", text("lower(name) text_pattern_ops")),
        Index(
            "ix_taxa_public_name", "name",
            postgresql_where=text("is_active AND subtree_image_count > 0"),
        ),
        Index(
            "ix_taxa_public_count", text("subtree_image_count DESC"),
            postgresql_where=text("is_active AND subtree_image_count > 0"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    inat_taxon_id: Mapped[int] = mapped_column(Integer, unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("taxa.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(300))
    rank: Mapped[str] = mapped_column(String(40))
    rank_level: Mapped[int] = mapped_column(Integer)
    # iNaturalist IDs of all ancestors, root first (excluding the taxon itself).
    ancestor_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default="{}")
    common_name: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Denormalized counts and cover photo, refreshed after imports and votes.
    image_count: Mapped[int] = mapped_column(Integer, server_default="0")
    subtree_image_count: Mapped[int] = mapped_column(Integer, server_default="0")
    cover_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL", use_alter=True)
    )
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cover_image: Mapped[Image | None] = relationship(foreign_keys=[cover_image_id], lazy="joined")


class TaxonName(Base):
    """Searchable names: the accepted name, synonyms, common names and mapped source names."""

    __tablename__ = "taxon_names"
    __table_args__ = (
        UniqueConstraint("taxon_id", "name", "kind", name="uq_taxon_names_taxon_name_kind"),
        CheckConstraint("kind IN ('scientific', 'synonym', 'common')", name="ck_taxon_names_kind"),
        Index("ix_taxon_names_lower", text("lower(name) text_pattern_ops")),
        Index(
            "ix_taxon_names_trgm",
            text("lower(name) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    taxon_id: Mapped[int] = mapped_column(ForeignKey("taxa.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(40))
    language: Mapped[str | None] = mapped_column(String(10))


class SourceName(Base):
    """A taxon name used by a secondary source (Mushroom Observer) and how it maps to iNat."""

    __tablename__ = "source_names"
    __table_args__ = (
        UniqueConstraint("source", "source_name_id", name="uq_source_names_source_id"),
        CheckConstraint(
            "status IN ('mapped', 'ambiguous', 'unresolved')", name="ck_source_names_status"
        ),
        Index("ix_source_names_status", "source", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    source_name_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(300))
    author: Mapped[str | None] = mapped_column(String(300))
    rank: Mapped[str | None] = mapped_column(String(40))
    taxon_id: Mapped[int | None] = mapped_column(ForeignKey("taxa.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), server_default="unresolved")
    # exact | rank | synonym | alias | manual
    method: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    candidate_taxon_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default="{}")
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()

    taxon: Mapped[Taxon | None] = relationship()


class Image(Base):
    """Metadata for an externally hosted photograph. Image files are never stored here."""

    __tablename__ = "images"
    __table_args__ = (
        UniqueConstraint("source", "source_image_id", name="uq_images_source_image"),
        CheckConstraint(f"source IN {SOURCES!r}", name="ck_images_source"),
        Index("ix_images_taxon_active", "taxon_id", postgresql_where=text("is_active")),
        Index("ix_images_source_observation", "source", "source_observation_id"),
        Index("ix_images_source_name", "source_name_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    source_image_id: Mapped[str] = mapped_column(String(100))
    source_observation_id: Mapped[str] = mapped_column(String(100))
    observation_url: Mapped[str] = mapped_column(Text)
    taxon_id: Mapped[int | None] = mapped_column(ForeignKey("taxa.id", ondelete="SET NULL"))
    source_name_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_names.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    thumb_url: Mapped[str] = mapped_column(Text)
    medium_url: Mapped[str] = mapped_column(Text)
    large_url: Mapped[str] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    photographer: Mapped[str] = mapped_column(String(300))
    license_code: Mapped[str] = mapped_column(String(40))
    attribution: Mapped[str] = mapped_column(Text)
    observed_on: Mapped[date | None] = mapped_column(Date)
    faves: Mapped[int] = mapped_column(Integer, server_default="0")
    its_length: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Why an image is hidden: license, no_its, unmapped_name, missing_upstream, duplicate
    inactive_reason: Mapped[str | None] = mapped_column(String(40))
    last_seen_at: Mapped[datetime] = created_column()
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()

    taxon: Mapped[Taxon | None] = relationship(foreign_keys=[taxon_id])

    @property
    def aspect_ratio(self) -> float:
        if self.width and self.height:
            return round(min(max(self.width / self.height, 0.5), 2.4), 3)
        return 1.333


class Vote(Base):
    """One user's placement of one image within their ranking for a taxon.

    Ranks are contiguous from 1 for each (user, taxon). The rank uniqueness
    constraint is deferred so a ranking can be rewritten inside a transaction.
    """

    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("user_id", "taxon_id", "image_id", name="uq_votes_user_taxon_image"),
        UniqueConstraint(
            "user_id",
            "taxon_id",
            "rank",
            name="uq_votes_user_taxon_rank",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("rank >= 1", name="ck_votes_rank_positive"),
        Index("ix_votes_taxon_image", "taxon_id", "image_id"),
        Index("ix_votes_image", "image_id"),
        Index("ix_votes_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    taxon_id: Mapped[int] = mapped_column(ForeignKey("taxa.id", ondelete="CASCADE"))
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"))
    rank: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()


class SyncState(Base):
    """Durable cursor and last-success bookkeeping for each synchronization job."""

    __tablename__ = "sync_state"

    job: Mapped[str] = mapped_column(String(40), primary_key=True)
    cursor: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(20), server_default="never")
    last_error: Mapped[str | None] = mapped_column(Text)
    records_processed: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = updated_column()


class SyncRun(Base):
    """History of synchronization runs, for the admin page and debugging."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_job_started", "job", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = created_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), server_default="running")
    records_processed: Mapped[int] = mapped_column(BigInteger, server_default="0")
    stats: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)


# A read-only view (defined in the migrations) of votes on images that are
# currently shown in the voted taxon's gallery: active, and filed under that
# taxon or, for genus rank and below, one of its subtaxa. Votes on images that
# were hidden or re-identified elsewhere stay in `votes` for the record.
visible_votes = Table(
    "visible_votes",
    MetaData(),
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger),
    Column("taxon_id", BigInteger),
    Column("image_id", BigInteger),
    Column("rank", Integer),
    Column("updated_at", DateTime(timezone=True)),
)
