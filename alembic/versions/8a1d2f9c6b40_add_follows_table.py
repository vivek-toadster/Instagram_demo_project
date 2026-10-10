"""add follows table

Revision ID: 8a1d2f9c6b40
Revises: cd9ce65e62d4
Create Date: 2026-10-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a1d2f9c6b40"
down_revision: Union[str, None] = "1cd8ce33cb7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("follower_id", sa.Integer(), nullable=False),
        sa.Column("followed_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["follower_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["followed_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("follower_id", "followed_id", name="uq_follower_followed"),
    )
    op.create_index(op.f("ix_follows_id"), "follows", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_follows_id"), table_name="follows")
    op.drop_table("follows")