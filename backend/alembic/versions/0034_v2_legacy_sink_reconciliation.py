"""Repair legacy sink markers whose delivery outcome is ambiguous.

The first build of migration 0033 interpreted every pre-two-phase sink marker
as completed.  That build reached the pre environment, so editing 0033 alone
cannot repair an already-stamped database.  A legacy marker is identifiable by
``completed_at = applied_at`` (0033 copied the timestamp); when its outbox row
is still pending, the old claim-before-write process may have died on either
side of the target write.  Stop it for explicit reconciliation instead of
guessing between loss and duplication.

Revision ID: 0034_v2_legacy_sink_reconcile
"""
from alembic import op


revision = "0034_v2_legacy_sink_reconcile"
down_revision = "0033_v2_seq_cursor_effect_order"
branch_labels = None
depends_on = None


_UP = r"""
-- An early build of 0033 assigned identity values to existing effects in
-- physical heap order.  Editing 0033 repairs fresh databases, but pre was
-- already stamped with that build.  Re-run the deterministic created_at /
-- effect_id ordering here and reseed the identity before recreating the
-- uniqueness guard.  The ALTER lock is held for this migration transaction,
-- so producers cannot interleave an insert with the renumber.
DROP INDEX IF EXISTS v2_effect_outbox_enqueue_seq_unique;

WITH ordered AS (
  SELECT effect_id,
         ROW_NUMBER() OVER (ORDER BY created_at ASC, effect_id ASC) AS new_seq
  FROM v2_effect_outbox
)
UPDATE v2_effect_outbox AS effects
SET enqueue_seq = ordered.new_seq
FROM ordered
WHERE effects.effect_id = ordered.effect_id;

SELECT setval(
  pg_get_serial_sequence('v2_effect_outbox', 'enqueue_seq'),
  COALESCE((SELECT MAX(enqueue_seq) FROM v2_effect_outbox), 1),
  EXISTS (SELECT 1 FROM v2_effect_outbox)
);

CREATE UNIQUE INDEX IF NOT EXISTS v2_effect_outbox_enqueue_seq_unique
  ON v2_effect_outbox (enqueue_seq);

-- 0033 introduced v2_runtime_state after some users had already been opted in
-- through the model_api_runtime blob.  Those users otherwise remain in the
-- row default (resident), so the old resident and the new V2 worker can both
-- believe they own the turn.  Treat the already-persisted routing flag as the
-- migration source of truth and reconcile the authoritative row to V2.
--
-- This is a schema/backfill repair, not a live cutover: keep the existing
-- generation so already-valid queued jobs and effects are not discarded.  A
-- later operator-initiated flip still uses the normal two-generation fence.
INSERT INTO v2_runtime_state (
  user_id, hosted_runtime_state, runtime_generation, updated_at
)
SELECT blobs.user_id, 'v2', 1, now()
FROM user_blobs AS blobs
JOIN users ON users.user_id = blobs.user_id
WHERE blobs.kind = 'model_api_runtime'
  AND blobs.doc ->> 'hosted_runtime_mode' = 'db_action_v2'
ON CONFLICT (user_id) DO UPDATE
SET hosted_runtime_state = 'v2',
    updated_at = now()
WHERE v2_runtime_state.hosted_runtime_state IS DISTINCT FROM 'v2';

UPDATE v2_effect_outbox AS effect
SET status = 'needs_reconciliation',
    last_error = 'legacy sink delivery uncertain: manual reconciliation required',
    attempt_count = GREATEST(effect.attempt_count, 1),
    last_attempt_at = COALESCE(effect.last_attempt_at, now())
FROM v2_effect_sink_applied AS sink
WHERE sink.effect_id = effect.effect_id
  AND effect.status = 'pending'
  AND sink.claim_state = 'completed'
  AND sink.completed_at = sink.applied_at;

UPDATE v2_effect_outbox
SET payload = '{"legacy_payload_scrubbed": true}'::jsonb
WHERE status = 'needs_reconciliation'
  AND last_error = 'legacy sink delivery uncertain: manual reconciliation required'
  AND effect_type IN ('memory', 'identity', 'schedule');

UPDATE v2_effect_sink_applied AS sink
SET claim_state = 'claimed',
    completed_at = NULL
FROM v2_effect_outbox AS effect
WHERE effect.effect_id = sink.effect_id
  AND effect.status = 'needs_reconciliation'
  AND effect.last_error = 'legacy sink delivery uncertain: manual reconciliation required'
  AND sink.claim_state = 'completed'
  AND sink.completed_at = sink.applied_at;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Reconciliation is a monotonic safety decision. Restoring the ambiguous
    # automatic-replay state on downgrade would reintroduce loss/duplication.
    pass
