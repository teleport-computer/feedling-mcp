"""Merge the test data-track/notify-relay chain into the pre Runtime V2 chain.

The `test` branch added migrations 0020-0022 (DAU median, growth/cohort
retention, notify relay) while the `pre` branch independently added 0041-0048
(Runtime V2 mutation attempts, workspaces, trajectories, summary segments,
retired-supervisor drop, turn-metrics FK). Both chains fork from a shared
ancestor and never rejoin, so merging `test` into `pre` produced two alembic
heads. This is a no-op merge revision that rejoins them into a single head so
`alembic upgrade head` stays linearizable; the two chains touch disjoint tables,
so either application order is safe.

Revision ID: 0049_merge_test_pre_heads
Revises: 0022_notify_relay, 0048_v2_turn_metrics_user_fk
"""

revision = "0049_merge_test_pre_heads"
down_revision = ("0022_notify_relay", "0048_v2_turn_metrics_user_fk")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No schema change: this revision only rejoins two independent chains.
    pass


def downgrade() -> None:
    pass
