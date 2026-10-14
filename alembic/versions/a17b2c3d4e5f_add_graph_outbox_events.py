"""add transactional graph outbox events

Revision ID: a17b2c3d4e5f
Revises: 9f3e1a7b2c4d
Create Date: 2026-10-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a17b2c3d4e5f"
down_revision: Union[str, None] = "9f3e1a7b2c4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_outbox_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_graph_outbox_events_event_type", "graph_outbox_events", ["event_type"])
    op.create_index("ix_graph_outbox_events_status", "graph_outbox_events", ["status"])
    op.create_index("ix_graph_outbox_events_next_attempt_at", "graph_outbox_events", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_graph_outbox_events_next_attempt_at", table_name="graph_outbox_events")
    op.drop_index("ix_graph_outbox_events_status", table_name="graph_outbox_events")
    op.drop_index("ix_graph_outbox_events_event_type", table_name="graph_outbox_events")
    op.drop_table("graph_outbox_events")
