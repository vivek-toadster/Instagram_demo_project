"""add indexes used by feed pagination and engagement counts

Revision ID: 9f3e1a7b2c4d
Revises: 8a1d2f9c6b40
Create Date: 2026-10-13

"""
from typing import Sequence, Union

from alembic import op


revision: str = "9f3e1a7b2c4d"
down_revision: Union[str, None] = "8a1d2f9c6b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_posts_created_at", "posts", ["created_at"], unique=False)
    op.create_index("ix_likes_post_id", "likes", ["post_id"], unique=False)
    op.create_index("ix_comments_post_id", "comments", ["post_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_comments_post_id", table_name="comments")
    op.drop_index("ix_likes_post_id", table_name="likes")
    op.drop_index("ix_posts_created_at", table_name="posts")
