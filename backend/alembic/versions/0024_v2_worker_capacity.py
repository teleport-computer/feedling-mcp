"""Advertise executable turn slots per V2 worker heartbeat.

Revision ID: 0024_v2_worker_capacity
"""
from alembic import op

revision = "0024_v2_worker_capacity"
down_revision = "0023_v2_job_liveness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE v2_worker_heartbeats "
        "ADD COLUMN IF NOT EXISTS capacity INT NOT NULL DEFAULT 1 "
        "CHECK (capacity >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE v2_worker_heartbeats DROP COLUMN IF EXISTS capacity")
