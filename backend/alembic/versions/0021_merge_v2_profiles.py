"""Merge the historical V2 chain with the model API profiles branch.

Databases may already be stamped at ``0020_v2_heartbeat_kind``. Keeping the
new profiles migration as a sibling branch lets Alembic execute it for those
databases before joining here, instead of silently skipping it via a rewritten
historical parent.

Revision ID: 0021_merge_v2_profiles
"""

revision = "0021_merge_v2_profiles"
down_revision = ("0020_v2_heartbeat_kind", "0014_model_api_profiles")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
