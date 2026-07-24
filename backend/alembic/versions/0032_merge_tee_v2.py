"""Merge the tee-pg shadow chain with the Hosted Runtime V2 chain.

Two independent lineages branched off ``0014_model_api_profiles`` on separate
long-lived branches and are joined here when ``feat/hosted-runtime-v2`` is
rebased onto ``test``:

- tee-pg shadow: ``0015_tee_sync_runs`` → ``0016_tee_sync_table_failures``
  (landed on ``test``).
- Hosted Runtime V2: ``0015_v2_worker_heartbeats`` → … →
  ``0031_v2_summary_watermark_seq`` (the four-PR series A–D on the feature
  branch; already merges the model_api_profiles sibling at 0021).

Neither chain touches the other's tables, so the merge is a pure no-op join —
same pattern as ``0021_merge_v2_profiles``. Databases stamped at either head
execute the missing sibling chain before arriving here, so a test DB already at
``0016_tee_sync_table_failures`` still runs the whole V2 chain (and vice-versa)
rather than silently skipping it.

Revision ID: 0032_merge_tee_v2
"""

revision = "0032_merge_tee_v2"
down_revision = ("0016_tee_sync_table_failures", "0031_v2_summary_watermark_seq")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
