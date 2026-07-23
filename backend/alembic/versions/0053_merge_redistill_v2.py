"""Merge the test redistill-exclusivity head into the pre Runtime V2 chain.

The `test` branch added `0023_redistill_job_exclusivity` (a partial unique
index enforcing one active redistill job per user) directly off the shared
`0022_notify_relay` ancestor, while the `pre` branch independently advanced its
Runtime V2 chain up to `0052_dual_runtime_coexistence` (itself already past the
earlier `0049_merge_test_pre_heads` rejoin). Merging `test` into `pre` again
left `0023` dangling as a second head — its own docstring anticipates this and
calls for exactly this re-base. This is a no-op merge revision that rejoins the
two into a single head so `alembic upgrade head` stays linearizable; the two
chains touch disjoint objects (a genesis-jobs index vs. the dual-runtime tables
and columns), so either application order is safe.

Revision ID: 0053_merge_redistill_v2
Revises: 0052_dual_runtime_coexistence, 0023_redistill_job_exclusivity
"""

revision = "0053_merge_redistill_v2"
down_revision = ("0052_dual_runtime_coexistence", "0023_redistill_job_exclusivity")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No schema change: this revision only rejoins two independent chains.
    pass


def downgrade() -> None:
    pass
