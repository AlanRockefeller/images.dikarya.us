"""store source observation favorite counts

Revision ID: 91d2c0ab7f64
Revises: ab38d1e0f5d7
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa


revision = "91d2c0ab7f64"
down_revision = "ab38d1e0f5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.add_column(sa.Column("source_observation_favorites", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_images_source_observation_favorites_nonnegative",
            "source_observation_favorites IS NULL OR source_observation_favorites >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.drop_constraint("ck_images_source_observation_favorites_nonnegative", type_="check")
        batch_op.drop_column("source_observation_favorites")
