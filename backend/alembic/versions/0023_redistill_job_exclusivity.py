"""redistill job exclusivity: one active resident_redistill job per user.

Revision ID: 0023_redistill_job_exclusivity
Revises: 0022_notify_relay
Create Date: 2026-07-23

Terminal ``identity-redistill`` (io_cli → consumer IPC → the existing resident
sealed lane, ``genesis_core._resident_sealed_import``) reuses the SAME
``genesis_import_jobs`` table as onboarding/import, tagged via the job-kind
discriminator column so a stray double-submit (retry, double terminal
command) can't spin up two concurrent distill jobs for the same user.

DEVIATION FROM THE ORIGINAL PLAN SKETCH: the plan's pseudocode named the
table ``genesis_imports`` and the column ``job_kind``. Neither exists —
the real table is ``genesis_import_jobs`` (0008_genesis_imports.py) and the
real job-kind discriminator is ``source_kind`` (already used for every other
job-kind distinction in this table: ``history``, ``identity_update``,
``resident``, ``companion_persona_backfill``, ...). This migration targets
``genesis_import_jobs.source_kind = 'resident_redistill'`` accordingly — see
task-10-report.md for the full rationale.

The partial unique index only fires for ``source_kind = 'resident_redistill'``
+ the two "still working" statuses (``awaiting_resident``, ``processing``),
so onboarding/import concurrency for every OTHER job kind is untouched.

Before creating the index, defensively fail any pre-existing duplicate active
redistill rows for the same user (expected to be zero rows in practice — this
kind doesn't exist before this migration ships) so the index creation itself
can never fail on latent duplicate data. Keeps the most recently updated row
per user active; older duplicates are marked ``failed`` (ties broken by
``job_id`` so the choice is deterministic).

V2 NOTE (2026-07-27 pre-merge): when ``test`` merges into ``pre`` for the
Runtime V2 cutover, this revision must be re-based as a merge revision against
pre's alembic head (0052+ at last check) — the two lines have diverged past
0022. Also re-evaluate whether the TEE mirror schema (``backend/alembic_tee``,
which mirrors ``genesis_import_jobs`` — see ``alembic_tee/versions/0001_tee_baseline.py``)
needs the same partial unique index, or whether the mirror can stay
index-free since tee_shadow only replicates writes and never contends for the
exclusivity slot itself.
"""

from alembic import op

revision = "0023_redistill_job_exclusivity"
down_revision = "0022_notify_relay"
branch_labels = None
depends_on = None

_ACTIVE_STATUSES = "('awaiting_resident', 'processing')"
_REDISTILL_KIND = "resident_redistill"

_UP = f"""
WITH ranked AS (
    SELECT user_id, job_id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id ORDER BY updated_at DESC, job_id DESC
           ) AS rn
    FROM genesis_import_jobs
    WHERE source_kind = '{_REDISTILL_KIND}'
      AND status IN {_ACTIVE_STATUSES}
)
UPDATE genesis_import_jobs g
SET status = 'failed',
    error = 'superseded_by_migration_0023_redistill_exclusivity',
    updated_at = now()
FROM ranked
WHERE g.user_id = ranked.user_id
  AND g.job_id = ranked.job_id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS genesis_jobs_redistill_active_uidx
    ON genesis_import_jobs (user_id)
    WHERE source_kind = '{_REDISTILL_KIND}' AND status IN {_ACTIVE_STATUSES};
"""

_DOWN = """
DROP INDEX IF EXISTS genesis_jobs_redistill_active_uidx;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
