"""Generation-fenced effect outbox (Hosted Runtime V2 PR A / spec A4).

Every side effect a V2 turn produces (reply, status, cursor, job, memory,
identity, schedule) is first written here, carrying the runtime generation
that was pinned when the effect was produced. Applying an effect is a single
transaction that re-checks the user's CURRENT generation (FOR UPDATE on
v2_runtime_state) against the pinned `expected_generation`: match -> dispatch
and mark applied; mismatch -> mark discarded, no dispatch. `effect_id` is
globally unique (see v2/effect_id.py), so re-enqueueing the same logical
effect is a no-op (ON CONFLICT DO NOTHING), and the partial index only ever
has to scan rows still awaiting application.

Revision ID: 0027_v2_effect_outbox
"""
from alembic import op

revision = "0027_v2_effect_outbox"
down_revision = "0026_v2_job_expected_generation"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_effect_outbox (
  effect_id        TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  job_id           BIGINT,
  effect_type      TEXT NOT NULL,
  expected_generation BIGINT NOT NULL,
  payload          JSONB NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at       TIMESTAMPTZ,
  attempt_count    INT NOT NULL DEFAULT 0,
  last_error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS v2_effect_outbox_pending ON v2_effect_outbox (user_id, created_at)
  WHERE status = 'pending';
"""

_DOWN = """
DROP INDEX IF EXISTS v2_effect_outbox_pending;
DROP TABLE IF EXISTS v2_effect_outbox;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
