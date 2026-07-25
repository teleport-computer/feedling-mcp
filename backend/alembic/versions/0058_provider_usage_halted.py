"""0058 provider usage kill switch: `provider_usage_halted` on the single-row
`v2_runtime_control` table.

Default `false` = not halted = feature ON — same semantics as `turns_halted`
(0044) and the `web_search_halted`/`web_fetch_halted` pair (0050): this is a
rollback lever, not a feature gate. Flip it to `true` to stop provider-usage
reporting without a redeploy if it misbehaves in production.

Revision ID: 0058_provider_usage_halted
"""
from alembic import op

revision = "0058_provider_usage_halted"
down_revision = "0057_provider_health"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE v2_runtime_control
  ADD COLUMN IF NOT EXISTS provider_usage_halted BOOLEAN NOT NULL DEFAULT false;
"""

_DOWN = """
ALTER TABLE v2_runtime_control DROP COLUMN IF EXISTS provider_usage_halted;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
