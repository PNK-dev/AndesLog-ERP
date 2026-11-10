"""Add the transactional outbox used by the Azure integration worker.

Revision ID: c81e4da50732
Revises: 729a354b7cfd
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa


revision = "c81e4da50732"
down_revision = "729a354b7cfd"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_type", sa.String(length=40), nullable=False),
        sa.Column("aggregate_id", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'processing', 'delivered', 'dead')"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_event_dispatch", "outbox_event", ["status", "available_at", "created_at"])


def downgrade():
    op.drop_index("ix_outbox_event_dispatch", table_name="outbox_event")
    op.drop_table("outbox_event")
