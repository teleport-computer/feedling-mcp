"""Add `source` to the PerceptKit source-mirror identity.

Revision ID: 0041_perceptkit_mirror_source
Revises: 0040_perceptkit_objects

A full sync declaring one source was deleting another source's rows. The
snapshot step removes "everything inside coverage that this round did not
mention", and rows belonging to a different source were of course not in
this round -- so one `source="ios"` sync emptied the user's Google calendar,
irreversibly, with nothing reporting it.

The mirror tables had no `source` column at all, so there was nothing to
scope the delete by. Upstream perceptkit made `source` a required field of
the mirror records in 0.3.0; this brings the tables in line.

``_UP`` is byte-identical to the paired revision on the other chain.

Existing rows all came from the iOS producer -- it is the only one wired --
so the backfill is a statement of fact, not a guess.
"""

from alembic import op


revision = "0041_perceptkit_mirror_source"
down_revision = "0040_perceptkit_objects"
branch_labels = None
depends_on = None


_UP = """-- Existing mirror rows predate the column. Every one of them came from the
-- iOS producer -- it is the only one wired -- so backfilling 'ios' is a
-- statement of fact, not a guess.
--
-- The DEFAULT exists only to fill those rows. It is dropped immediately
-- after: leaving it means a caller that forgets `source` silently gets
-- 'ios', which is the exact silent failure this column was added to stop.
ALTER TABLE perceptkit_calendar_mirror
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ios';
ALTER TABLE perceptkit_calendar_mirror ALTER COLUMN source DROP DEFAULT;

ALTER TABLE perceptkit_reminder_mirror
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ios';
ALTER TABLE perceptkit_reminder_mirror ALTER COLUMN source DROP DEFAULT;

-- Widen the primary key to include it. Two rows that differ only by source
-- are two different facts; before this they collided and overwrote each
-- other, and a full sync for one source deleted the other's rows.
ALTER TABLE perceptkit_calendar_mirror
  DROP CONSTRAINT IF EXISTS perceptkit_calendar_mirror_pkey;
ALTER TABLE perceptkit_calendar_mirror
  ADD CONSTRAINT perceptkit_calendar_mirror_pkey
  PRIMARY KEY (subject_id, source, source_account_id, source_calendar_id,
               source_event_id);

ALTER TABLE perceptkit_reminder_mirror
  DROP CONSTRAINT IF EXISTS perceptkit_reminder_mirror_pkey;
ALTER TABLE perceptkit_reminder_mirror
  ADD CONSTRAINT perceptkit_reminder_mirror_pkey
  PRIMARY KEY (subject_id, source, source_account_id, source_list_id,
               source_reminder_id);

-- The sync-state table used an older field vocabulary than SourceSyncState:
-- `last_sync_id`/`cursor` where the record says `sync_cursor`, and no columns
-- at all for snapshot_kind / last_attempted_at / last_error_code. The reader
-- passed a keyword the record does not have, so every read raised -- nothing
-- called it, so nothing noticed.
--
-- Renaming rather than dropping: the old columns hold whatever a partial
-- write left behind, and `cursor` is the one that maps onto `sync_cursor`.
-- RENAME COLUMN has no IF EXISTS, and this migration has to be re-runnable
-- (everything else here is). Guard it on the old column still being there.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'perceptkit_sync_state'
               AND column_name = 'cursor') THEN
    ALTER TABLE perceptkit_sync_state RENAME COLUMN cursor TO sync_cursor;
  END IF;
END $$;
ALTER TABLE perceptkit_sync_state
  ADD COLUMN IF NOT EXISTS sync_cursor TEXT;
ALTER TABLE perceptkit_sync_state DROP COLUMN IF EXISTS last_sync_id;
ALTER TABLE perceptkit_sync_state
  ADD COLUMN IF NOT EXISTS snapshot_kind TEXT,
  ADD COLUMN IF NOT EXISTS last_attempted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_error_code TEXT;
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0041_perceptkit_mirror_source"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    # Deliberately a no-op, same as 0040/0106 for these tables.
    #
    # Dropping `source` would not just narrow a key -- it erases which source
    # each row came from. A later re-upgrade backfills every surviving row as
    # 'ios', so a Google calendar entry comes back mislabelled, and the next
    # full sync for either source deletes rows belonging to the other. A
    # rollback that quietly corrupts the identity is worse than a schema that
    # sits one column ahead of the chain.
    #
    # Removing the column is a deliberate, manual operation.
    pass
