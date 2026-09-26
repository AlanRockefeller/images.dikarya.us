"""require a DNA sequence record for public images

Revision ID: ab38d1e0f5d7
Revises: 7a2d91e6a0b3
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa


revision = "ab38d1e0f5d7"
down_revision = "7a2d91e6a0b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "images",
        sa.Column("dna_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column("images", "dna_verified", server_default=None)
    op.create_index(
        "ix_images_taxon_dna_active",
        "images",
        ["taxon_id", "dna_verified", "active", "id"],
    )
    op.create_index(
        "ix_images_source_observation",
        "images",
        ["source", "source_observation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_images_source_observation", table_name="images")
    op.drop_index("ix_images_taxon_dna_active", table_name="images")
    op.drop_column("images", "dna_verified")
