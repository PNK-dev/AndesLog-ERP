"""Give new inventory movements an idempotency key; preserve historic rows.

Revision ID: 9dbde198e2a1
Revises: 2f5b9c6d7a10
"""
from alembic import op
import sqlalchemy as sa

revision = "9dbde198e2a1"
down_revision = "2f5b9c6d7a10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stock_movement", sa.Column("operation_id", sa.String(length=36), nullable=True))
    op.create_index("ix_stock_movement_operation_id", "stock_movement", ["operation_id"], unique=True)


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM stock_movement WHERE operation_id IS NOT NULL")).scalar_one():
        raise RuntimeError("Cannot downgrade while inventory idempotency keys exist")
    op.drop_index("ix_stock_movement_operation_id", table_name="stock_movement")
    op.drop_column("stock_movement", "operation_id")
