"""Bound user_logs vacuum eligibility to a few hours of observed churn.

T688, PG17 production snapshots 2026-09-21 07:11:34Z -> 16:48:52Z:
9.62h yielded INSERT +19,385 (~2,015/h), UPDATE+DELETE +15,155
(~1,575/h), and estimated dead tuples +15,155. reltuples=1,412,858.
Dead eligibility: existing base 50 + 0.005 * reltuples = ~7,114 (~4.5h).
Insert eligibility: base 1,000 + 0.005 * reltuples = ~8,064 (~4h).
The insert base retains the measured global default; the scale is lowered
from 0.2. These are eligibility estimates, not a guaranteed vacuum cadence:
pruning, statistics lag, concurrent snapshots and worker capacity matter.

Only the TEE chain is tuned: these measurements concern its live user_logs;
the separate RDS chain has no calibrated workload in this task. No rows are
deleted: autovacuum reclaims obsolete tuple versions and maintains visibility.
No ANALYZE thresholds, global cost settings, or other tables are tuned.

SET/RESET autovacuum storage parameters take SHARE UPDATE EXCLUSIVE; keep
this metadata statement in its own autocommit transaction, apart from other
DDL. Downgrade restores inheritance of exactly these three global settings.
https://www.postgresql.org/docs/17/routine-vacuuming.html#AUTOVACUUM
https://www.postgresql.org/docs/17/sql-altertable.html
"""
from alembic import op

revision = "0050_user_logs_autovacuum"
down_revision = "0049_agent_canvas_cards"
branch_labels = None
depends_on = None

SET_OPTIONS_SQL = """ALTER TABLE user_logs SET (
    autovacuum_vacuum_scale_factor = 0.005,
    autovacuum_vacuum_insert_threshold = 1000,
    autovacuum_vacuum_insert_scale_factor = 0.005
)"""
RESET_OPTIONS_SQL = """ALTER TABLE user_logs RESET (
    autovacuum_vacuum_scale_factor,
    autovacuum_vacuum_insert_threshold,
    autovacuum_vacuum_insert_scale_factor
)"""


def _prepared_head_sql(head: str) -> str:
    # The existing prepared-primary marker must follow the TEE migration head.
    return """UPDATE server_config SET value=convert_to(
        jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
        '["%s"]'::jsonb)::text, 'UTF8')
        WHERE key='phase4_primary_prepared'
        AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared','false')='true'""" % head


# Keep the current-head preflight inspection contract on the SQL we execute.
_UPDATE_PREPARED_HEAD = _prepared_head_sql(revision)
_RESTORE_PREPARED_HEAD = _prepared_head_sql(down_revision)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(SET_OPTIONS_SQL)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(RESET_OPTIONS_SQL)
    op.execute(_RESTORE_PREPARED_HEAD)
