"""Backfill first-chat activation for successful Runtime V2 replies.

Revision ID: 0087_v2_first_chat_activation
Revises: 0086_v2_worker_pool_heartbeats
"""

from alembic import op


revision = "0087_v2_first_chat_activation"
down_revision = "0086_v2_worker_pool_heartbeats"
branch_labels = None
depends_on = None


_BACKFILL_SQL = """
WITH first_success AS (
  SELECT parent.user_id, MIN(reply.ts) AS first_ok_ts
  FROM chat_messages AS reply
  JOIN chat_messages AS parent
    ON parent.user_id=reply.user_id
   AND parent.msg_id=reply.doc->>'reply_to_message_id'
  WHERE parent.doc->>'role' IN ('user','human')
    AND parent.doc->>'source' IN ('chat','model_api')
    -- The transactional V2 reply sink first existed on 2026-07-15. Bound the
    -- repair to its full possible lifetime and use ix_chat_messages_ts.
    AND reply.ts >= EXTRACT(EPOCH FROM TIMESTAMPTZ '2026-07-15 00:00:00+00')
    AND reply.doc->>'role' IN ('agent','openclaw')
    AND reply.doc->>'source'='model_api'
    AND COALESCE(reply.doc->>'turn_failure_error_class','')=''
  GROUP BY parent.user_id
), activation AS (
  SELECT user_id,
         to_char(
           timezone('UTC', to_timestamp(first_ok_ts)),
           'YYYY-MM-DD"T"HH24:MI:SS.US'
         ) || 'Z' AS first_chat_ok_at
  FROM first_success
)
INSERT INTO user_blobs (user_id,kind,doc)
SELECT user_id,
       'proactive_settings',
       jsonb_build_object(
         'version', 2,
         'first_chat_ok_at', first_chat_ok_at,
         'updated_at', first_chat_ok_at
       )
FROM activation
ON CONFLICT (user_id,kind) DO UPDATE SET doc=
  user_blobs.doc || EXCLUDED.doc
WHERE COALESCE(user_blobs.doc->>'first_chat_ok_at','')=''
"""


def upgrade() -> None:
    op.execute(_BACKFILL_SQL)


def downgrade() -> None:
    # The marker is also written by live V1/V2 replies; its origin cannot be
    # distinguished safely, so downgrade must not erase it.
    pass
