"""Record which regional ingress accepted an outbox event.

Revision ID: 2f5b9c6d7a10
Revises: c81e4da50732
"""

from alembic import op
import sqlalchemy as sa

revision = "2f5b9c6d7a10"
down_revision = "c81e4da50732"
branch_labels = None
depends_on = None


def upgrade():
    # NULL preserves historical events delivered before regional failover.
    op.add_column("outbox_event", sa.Column("delivered_destination", sa.String(length=16), nullable=True))


def downgrade():
    # Do not silently erase the only record of which archive holds an event.
    connection = op.get_bind()
    secondary_count = connection.execute(sa.text(
        "SELECT count(*) FROM outbox_event WHERE delivered_destination = 'secondary'"
    )).scalar_one()
    if secondary_count:
        raise RuntimeError("Cannot downgrade while events reference the secondary archive")
    op.drop_column("outbox_event", "delivered_destination")
