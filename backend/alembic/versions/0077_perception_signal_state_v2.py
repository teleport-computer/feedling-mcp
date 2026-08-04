"""Add durable Runtime V2 perception signal baselines.

Revision ID: 0077_perception_signal_state_v2
Revises: 0076_plaintext_job_exclusivity
"""

from alembic import op


revision = "0077_perception_signal_state_v2"
down_revision = "0076_plaintext_job_exclusivity"
branch_labels = None
depends_on = None


_UP = """
CREATE TABLE perception_signal_state_v2 (
    user_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    value_fingerprint TEXT NOT NULL,
    fingerprint_key_id TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    last_changed_at TIMESTAMPTZ NOT NULL,
    source_event_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, signal),
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute("DROP TABLE perception_signal_state_v2")
