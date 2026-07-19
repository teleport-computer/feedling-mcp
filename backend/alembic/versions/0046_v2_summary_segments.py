"""Immutable encrypted conversation-summary segments and checkpoints.

Revision ID: 0046_v2_summary_segments
Revises: 0045_drop_retired_supervisor
"""

from alembic import op


revision = "0046_v2_summary_segments"
down_revision = "0045_drop_retired_supervisor"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE v2_conversation_summary
  ADD COLUMN IF NOT EXISTS materialized_segment_ids BIGINT[]
  NOT NULL DEFAULT '{}'::BIGINT[];

CREATE TABLE IF NOT EXISTS v2_conversation_summary_segments (
  segment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  format_version SMALLINT NOT NULL DEFAULT 1,
  coverage_kind TEXT NOT NULL DEFAULT 'exact',
  level SMALLINT NOT NULL,
  start_seq BIGINT NOT NULL,
  end_seq BIGINT NOT NULL,
  source_message_count INT NOT NULL,
  legacy_opaque_through_seq BIGINT NOT NULL DEFAULT 0,
  child_segment_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
  summary_envelope JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_v2_summary_segment_format CHECK (format_version = 1),
  CONSTRAINT ck_v2_summary_segment_level CHECK (level >= 0),
  CONSTRAINT ck_v2_summary_segment_coverage CHECK (
    (coverage_kind = 'exact'
      AND start_seq > 0 AND end_seq >= start_seq
      AND source_message_count > 0 AND legacy_opaque_through_seq = 0)
    OR
    (coverage_kind = 'legacy_opaque'
      AND start_seq = 0 AND end_seq >= legacy_opaque_through_seq
      AND legacy_opaque_through_seq >= 0 AND source_message_count >= 0)
  ),
  CONSTRAINT ck_v2_summary_segment_children CHECK (
    (level = 0 AND cardinality(child_segment_ids) = 0)
    OR (level > 0 AND cardinality(child_segment_ids) > 0)
  ),
  CONSTRAINT uq_v2_summary_segment_cover UNIQUE (
    user_id, level, start_seq, end_seq
  )
);

CREATE INDEX IF NOT EXISTS ix_v2_summary_segments_user_range
  ON v2_conversation_summary_segments (user_id, start_seq, end_seq, level);

CREATE OR REPLACE FUNCTION v2_reject_summary_segment_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'v2 conversation summary segments are immutable'
    USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_summary_segments_immutable
  ON v2_conversation_summary_segments;
CREATE TRIGGER trg_v2_summary_segments_immutable
BEFORE UPDATE ON v2_conversation_summary_segments
FOR EACH ROW EXECUTE FUNCTION v2_reject_summary_segment_update();

CREATE OR REPLACE FUNCTION v2_guard_segmented_summary_head()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (
       SELECT 1 FROM v2_conversation_summary_segments
       WHERE user_id=NEW.user_id
     )
     AND NEW.version <> OLD.version
     AND NEW.materialized_segment_ids IS NOT DISTINCT FROM OLD.materialized_segment_ids
  THEN
    RAISE EXCEPTION 'segmented summary head update lacks canonical provenance'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_segmented_summary_head
  ON v2_conversation_summary;
CREATE TRIGGER trg_v2_segmented_summary_head
BEFORE UPDATE ON v2_conversation_summary
FOR EACH ROW EXECUTE FUNCTION v2_guard_segmented_summary_head();

CREATE OR REPLACE FUNCTION v2_delete_summary_segments_with_head()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM v2_conversation_summary_segments WHERE user_id=OLD.user_id;
  RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_v2_summary_head_delete_segments
  ON v2_conversation_summary;
CREATE TRIGGER trg_v2_summary_head_delete_segments
BEFORE DELETE ON v2_conversation_summary
FOR EACH ROW EXECUTE FUNCTION v2_delete_summary_segments_with_head();
"""


_DOWN = r"""
DROP TRIGGER IF EXISTS trg_v2_summary_head_delete_segments
  ON v2_conversation_summary;
DROP FUNCTION IF EXISTS v2_delete_summary_segments_with_head();
DROP TRIGGER IF EXISTS trg_v2_segmented_summary_head
  ON v2_conversation_summary;
DROP FUNCTION IF EXISTS v2_guard_segmented_summary_head();
DROP TRIGGER IF EXISTS trg_v2_summary_segments_immutable
  ON v2_conversation_summary_segments;
DROP FUNCTION IF EXISTS v2_reject_summary_segment_update();
DROP TABLE IF EXISTS v2_conversation_summary_segments;
ALTER TABLE v2_conversation_summary
  DROP COLUMN IF EXISTS materialized_segment_ids;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
