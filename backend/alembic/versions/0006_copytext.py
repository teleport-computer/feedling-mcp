"""copytext: server-managed bilingual UI copy override store

Revision ID: 0006_copytext
Revises: 0005_merge_agent_perception
Create Date: 2026-06-26

The iOS app ships all user-visible copy compiled into the bundle
(Localizable.xcstrings; ~980 keys, en + zh-Hans). Changing one line means an
App Store release. This revision backs a server-side OVERRIDE layer: the app
fetches a bundle of managed keys and overlays them on top of the local
xcstrings (which stays the offline fallback). Editing copy then needs neither
an app release nor a backend deploy — just a row change.

Two tables:
  copytext_strings — one row per (key, lang); value is the override text.
  copytext_meta    — single-row counter bumped on every write, used as the
                     monotonic `revision` / ETag the client caches against.
                     A counter (not max(updated_at)) is used so deletes also
                     advance the revision.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_copytext"
down_revision = "0005_merge_agent_perception"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS copytext_strings (
    key        TEXT NOT NULL,
    lang       TEXT NOT NULL,              -- 'en' | 'zh-Hans'
    value      TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (key, lang)
);

CREATE TABLE IF NOT EXISTS copytext_meta (
    id        BOOLEAN PRIMARY KEY DEFAULT TRUE,   -- single-row guard
    revision  BIGINT NOT NULL DEFAULT 0,
    CHECK (id)
);

INSERT INTO copytext_meta (id, revision) VALUES (TRUE, 0)
    ON CONFLICT (id) DO NOTHING;
"""

_DROP = """
DROP TABLE IF EXISTS copytext_strings;
DROP TABLE IF EXISTS copytext_meta;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)
