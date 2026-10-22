"""add carousel media and post drafts"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "post_media",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("image_path", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_post_media_id", "post_media", ["id"])
    op.create_index("ix_post_media_post_id", "post_media", ["post_id"])
    op.create_table(
        "draft_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("image_paths", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_draft_posts_id", "draft_posts", ["id"])
    op.create_index("ix_draft_posts_user_id", "draft_posts", ["user_id"])
    op.create_index("ix_draft_posts_updated_at", "draft_posts", ["updated_at"])


def downgrade():
    op.drop_table("draft_posts")
    op.drop_table("post_media")
