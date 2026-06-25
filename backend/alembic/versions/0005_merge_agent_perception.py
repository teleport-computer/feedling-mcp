"""merge the agent_runtime_instances + perception_daily branches into one head

Revision ID: 0005_merge_agent_perception
Revises: 0004_agent_runtime_instances, 0004_perception_daily
Create Date: 2026-06-25

NOTE: keep the revision id <= 32 chars — alembic's ``alembic_version.version_num``
is ``VARCHAR(32)``; a longer id fails the stamp with StringDataRightTruncation.

After a rebase, ``0004_agent_runtime_instances`` and ``0004_perception_daily``
both branch from ``0003_drop_perception_permissions``, leaving alembic with two
heads (``alembic upgrade head`` would error "multiple heads").

This is a no-op MERGE that rejoins the lineage into a single head WITHOUT
renaming or removing either ``0004`` revision id — a deployed database may
already be stamped with ``0004_agent_runtime_instances`` (or ``0004_perception_daily``),
and dropping a revision id would break ``alembic upgrade head`` with an unknown
revision before any DDL runs. Both 0004 revisions are independent DDL, so the
merge carries no schema changes itself.
"""

# Both branch DDL already ran on their own revisions; the merge only joins lineage.
revision = "0005_merge_agent_perception"
down_revision = ("0004_agent_runtime_instances", "0004_perception_daily")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
