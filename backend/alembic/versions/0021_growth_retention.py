"""Freeze completed user-growth days and weekly retention cohorts.

Revision ID: 0021_growth_retention
Revises: 0020_dau_median_user_sec
Create Date: 2026-07-20

Account deletion cascades (reset/delete removes the user row and all messages),
so growth and retention computed live are distorted retroactively: past signups
undercount, and retention is inflated because the hardest churners delete. Same
reason DAU got dau_daily_snapshot. These two tables freeze one immutable row per
completed Beijing day (growth) and per completed (cohort-week, period) cell
(retention); writers use ``ON CONFLICT DO NOTHING``.
"""

from alembic import op


revision = "0021_growth_retention"
down_revision = "0020_dau_median_user_sec"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS user_growth_daily_snapshot (
    day        TEXT PRIMARY KEY,
    new_users  INTEGER NOT NULL DEFAULT 0,
    frozen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_growth_daily_snapshot_day_format
        CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
);

CREATE TABLE IF NOT EXISTS retention_cohort_snapshot (
    cohort_week   TEXT NOT NULL,
    period_index  INTEGER NOT NULL,
    cohort_size   INTEGER NOT NULL DEFAULT 0,
    active_count  INTEGER NOT NULL DEFAULT 0,
    frozen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cohort_week, period_index),
    CONSTRAINT retention_cohort_week_format
        CHECK (cohort_week ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    CONSTRAINT retention_period_nonneg CHECK (period_index >= 0)
);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS retention_cohort_snapshot")
    op.execute("DROP TABLE IF EXISTS user_growth_daily_snapshot")
