"""0030 v2 runtime control: single-row `v2_runtime_control` table backing the
live turn kill switch (PR D Task 1 / D4). `turns_halted=true` fail-closes new
V2 chat admission (503), stops `_slot_loop` from claiming new jobs, and fences
active write effects — all without a redeploy. Genesis is untouched: it reads
a separate `genesis_import_jobs` table + heartbeat and never consults this one.

Revision ID: 0030_v2_runtime_control
"""
from alembic import op

revision = "0030_v2_runtime_control"
down_revision = "0029_v2_turn_metrics_whole_turn"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE IF NOT EXISTS v2_runtime_control ("
        "id INT PRIMARY KEY DEFAULT 1, turns_halted BOOLEAN NOT NULL DEFAULT false, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), CHECK (id=1))"
    )
    op.execute("INSERT INTO v2_runtime_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade():
    op.execute("DROP TABLE IF EXISTS v2_runtime_control")
