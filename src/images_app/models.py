from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email_ci", text("lower(email)"), unique=True),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    votes: Mapped[list[Vote]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Taxon(Base):
    __tablename__ = "taxa"
    __table_args__ = (
        UniqueConstraint("inat_taxon_id", name="uq_taxa_inat_taxon_id"),
        Index("ix_taxa_scientific_name", text("lower(scientific_name)")),
        Index("ix_taxa_name_trgm", "scientific_name", postgresql_using="gin", postgresql_ops={"scientific_name": "gin_trgm_ops"}),
        Index("ix_taxa_ancestry_trgm", "ancestry", postgresql_using="gin", postgresql_ops={"ancestry": "gin_trgm_ops"}),
        Index("ix_taxa_parent_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    inat_taxon_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("taxa.id", ondelete="SET NULL"), nullable=True)
    scientific_name: Mapped[str] = mapped_column(String(300), nullable=False)
    rank: Mapped[str | None] = mapped_column(String(60))
    ancestry: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    parent: Mapped[Taxon | None] = relationship(remote_side="Taxon.id", back_populates="children")
    children: Mapped[list[Taxon]] = relationship(back_populates="parent")
    aliases: Mapped[list[TaxonAlias]] = relationship(back_populates="taxon", cascade="all, delete-orphan")


class TaxonAlias(Base):
    __tablename__ = "taxon_aliases"
    __table_args__ = (
        UniqueConstraint("taxon_id", "name", "source", name="uq_taxon_alias_taxon_name_source"),
        Index("ix_taxon_aliases_name", text("lower(name)")),
        Index("ix_taxon_aliases_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    taxon_id: Mapped[int] = mapped_column(ForeignKey("taxa.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    taxon: Mapped[Taxon] = relationship(back_populates="aliases")


class SourceTaxon(Base):
    """A third-party name record retained for safe, auditable MO mapping."""

    __tablename__ = "source_taxa"
    __table_args__ = (
        UniqueConstraint("source", "source_taxon_id", name="uq_source_taxa_external_id"),
        Index("ix_source_taxa_status", "source", "mapping_status"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_taxon_id: Mapped[str | None] = mapped_column(String(100))
    original_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    taxon_id: Mapped[int | None] = mapped_column(ForeignKey("taxa.id", ondelete="SET NULL"))
    mapping_status: Mapped[str] = mapped_column(String(30), default="unresolved", nullable=False)
    mapping_method: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    taxon: Mapped[Taxon | None] = relationship()
    image_associations: Mapped[list[ImageSourceTaxon]] = relationship(back_populates="source_taxon")


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (
        UniqueConstraint("source", "source_image_id", name="uq_images_source_image"),
        CheckConstraint("source IN ('inaturalist', 'mushroom_observer')", name="ck_images_source"),
        CheckConstraint(
            "source_observation_favorites IS NULL OR source_observation_favorites >= 0",
            name="ck_images_source_observation_favorites_nonnegative",
        ),
        Index("ix_images_taxon_active", "taxon_id", "active", "id"),
        Index("ix_images_source_observation", "source", "source_observation_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    taxon_id: Mapped[int | None] = mapped_column(ForeignKey("taxa.id", ondelete="SET NULL"))
    source_taxon_id: Mapped[int | None] = mapped_column(ForeignKey("source_taxa.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_image_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_observation_id: Mapped[str | None] = mapped_column(String(100))
    source_observation_favorites: Mapped[int | None] = mapped_column(Integer)
    medium_url: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    source_page_url: Mapped[str] = mapped_column(Text, nullable=False)
    photographer: Mapped[str | None] = mapped_column(String(300))
    license: Mapped[str | None] = mapped_column(String(100))
    attribution: Mapped[str | None] = mapped_column(Text)
    display_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dna_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    taxon: Mapped[Taxon | None] = relationship()
    source_taxon: Mapped[SourceTaxon | None] = relationship()
    source_taxa: Mapped[list[ImageSourceTaxon]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )


class ImageSourceTaxon(Base):
    """Every source name attached to an externally hosted image."""

    __tablename__ = "image_source_taxa"
    __table_args__ = (
        PrimaryKeyConstraint("image_id", "source_taxon_id", name="pk_image_source_taxa"),
        Index("ix_image_source_taxa_taxon_image", "source_taxon_id", "image_id"),
    )

    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    source_taxon_id: Mapped[int] = mapped_column(
        ForeignKey("source_taxa.id", ondelete="CASCADE"), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    image: Mapped[Image] = relationship(back_populates="source_taxa")
    source_taxon: Mapped[SourceTaxon] = relationship(back_populates="image_associations")


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("user_id", "taxon_id", "image_id", name="uq_votes_user_taxon_image"),
        UniqueConstraint("user_id", "taxon_id", "rank", name="uq_votes_user_taxon_rank"),
        CheckConstraint("rank > 0", name="ck_votes_rank_positive"),
        Index("ix_votes_user_taxon_rank", "user_id", "taxon_id", "rank"),
        Index("ix_votes_taxon_image", "taxon_id", "image_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    taxon_id: Mapped[int] = mapped_column(ForeignKey("taxa.id", ondelete="CASCADE"), nullable=False)
    image_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("images.id", ondelete="RESTRICT"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="votes")
    taxon: Mapped[Taxon] = relationship()
    image: Mapped[Image] = relationship()


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("source", name="uq_sync_state_source"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="never", nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    records_processed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class RateLimitEvent(Base):
    __tablename__ = "rate_limit_events"
    __table_args__ = (Index("ix_rate_limit_scope_key_time", "scope", "key_hash", "occurred_at"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    scope: Mapped[str] = mapped_column(String(30), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
