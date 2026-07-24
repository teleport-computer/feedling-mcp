"""Scrub plaintext V2 action payloads, results, and legacy errors.

Planner payloads and capability results are derived from decrypted conversation,
memory, perception, and web data. The runtime consumes them in-process and has
no durable resume reader, so retaining their full JSON contradicts the encrypted
conversation boundary. Keep only trajectory shape/status until encrypted
trajectory storage is implemented.

Revision ID: 0022_v2_action_queue_privacy
"""
from alembic import op

revision = "0022_v2_action_queue_privacy"
down_revision = "0021_merge_v2_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_action_queue "
        "SET payload_json = '{}'::jsonb, "
        "    last_error = CASE WHEN last_error IS NULL THEN NULL "
        "                      ELSE 'legacy_error' END, "
        "    result_json = CASE "
        "      WHEN result_json IS NULL THEN NULL "
        "      ELSE jsonb_build_object('ok', "
        "        CASE WHEN lower(result_json->>'ok') IN ('true','false') "
        "             THEN (result_json->>'ok')::boolean ELSE true END) "
        "    END"
    )
    # Previous workers persisted ``Type: str(exception)`` in both tables.  The
    # suffix can contain decrypted prompts, provider bodies, URLs, or queries;
    # even colon-less values are not safe to retain verbatim.
    op.execute(
        "UPDATE agent_jobs SET last_error='legacy_job_error' "
        "WHERE last_error IS NOT NULL"
    )


def downgrade() -> None:
    # Scrubbed plaintext cannot and must not be reconstructed.
    pass
