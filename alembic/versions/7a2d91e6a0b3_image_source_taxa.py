"""persist all source taxon associations for an image

Revision ID: 7a2d91e6a0b3
Revises: 5c4bab6e41b6
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa


revision = "7a2d91e6a0b3"
down_revision = "5c4bab6e41b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_source_taxa",
        sa.Column("image_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("source_taxon_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["image_id"], ["images.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_taxon_id"], ["source_taxa.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("image_id", "source_taxon_id", name="pk_image_source_taxa"),
    )
    op.create_index(
        "ix_image_source_taxa_taxon_image",
        "image_source_taxa",
        ["source_taxon_id", "image_id"],
    )
    op.execute(
        "INSERT INTO image_source_taxa (image_id, source_taxon_id, last_seen_at) "
        "SELECT id, source_taxon_id, source_last_seen_at FROM images "
        "WHERE source = 'mushroom_observer' AND source_taxon_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_image_source_taxa_taxon_image", table_name="image_source_taxa")
    op.drop_table("image_source_taxa")
