"""0050 v2 web kill switch: `web_search_halted` / `web_fetch_halted` on the
single-row `v2_runtime_control` table.

Two columns rather than one because search and fetch are distinct incident
surfaces: a DuckDuckGo rate-limit or markup change only needs search stopped,
while a URL/parser/redirect defect (or a spike in page-borne prompt injection)
should stop full-page fetching while keeping search snippets available. Their
availability, latency and monitoring signals differ too.

"Halted" rather than "enabled" so the flag reads the same way as the existing
`turns_halted` and nothing has to double-negate.

Semantics differ from `turns_halted`: halting web REMOVES the two tools from
the turn's offered catalog, it never fences the turn itself — the chat carries
on with the model answering tool-less.

Revision ID: 0050_v2_web_halted_columns
"""
from alembic import op

revision = "0050_v2_web_halted_columns"
down_revision = "0049_merge_test_pre_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE v2_runtime_control "
        "ADD COLUMN IF NOT EXISTS web_search_halted BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE v2_runtime_control "
        "ADD COLUMN IF NOT EXISTS web_fetch_halted BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade():
    op.execute("ALTER TABLE v2_runtime_control DROP COLUMN IF EXISTS web_fetch_halted")
    op.execute("ALTER TABLE v2_runtime_control DROP COLUMN IF EXISTS web_search_halted")
