"""frame_envelopes: offload body_ct to R2 — add env_meta + body_key, doc nullable

Revision ID: 0007_frame_body_to_r2
Revises: 0006_copytext
Create Date: 2026-06-26

The heavy ChaCha20-Poly1305 screenshot blob (``doc.body_ct``, >150KB) is moved
to Cloudflare R2 (see backend/object_storage.py). Postgres keeps only the small
envelope metadata + a pointer:

  - ``env_meta`` JSONB : the v1 envelope minus ``body_ct`` (nonce, K_user,
                         K_enclave, enclave_pk_fpr, visibility, v, id, owner).
  - ``body_key`` TEXT  : the R2 object key (``frames/<user>/<frame>``).

``doc`` becomes nullable: R2-backed rows store doc=NULL, env_meta/body_key set;
legacy rows (and any row written while R2 is unconfigured) keep doc and leave
the two new columns NULL. db.frame_get reconstructs the full envelope from
whichever shape is present.

NOTE: keep the revision id <= 32 chars — ``alembic_version.version_num`` is
VARCHAR(32).

DDL is idempotent (IF [NOT] EXISTS) to match the baseline's safety property.
This migration only changes schema; it does NOT move existing row data — that
is done out-of-band by backend/backfill_frames_to_r2.py.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_frame_body_to_r2"
down_revision = "0006_copytext"
branch_labels = None
depends_on = None


_UPGRADE = """
ALTER TABLE frame_envelopes ADD COLUMN IF NOT EXISTS env_meta JSONB;
ALTER TABLE frame_envelopes ADD COLUMN IF NOT EXISTS body_key TEXT;
ALTER TABLE frame_envelopes ALTER COLUMN doc DROP NOT NULL;
"""

# Downgrade restores NOT NULL on doc; it will error if any R2-backed rows still
# have doc=NULL — backfill doc (inline from R2) before downgrading.
_DOWNGRADE = """
ALTER TABLE frame_envelopes ALTER COLUMN doc SET NOT NULL;
ALTER TABLE frame_envelopes DROP COLUMN IF EXISTS body_key;
ALTER TABLE frame_envelopes DROP COLUMN IF EXISTS env_meta;
"""


def upgrade() -> None:
    op.execute(_UPGRADE)


def downgrade() -> None:
    op.execute(_DOWNGRADE)
