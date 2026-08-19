"""Safe timestamp parser for the resident lane-rollup terminal-time bucketing.

Revision ID: 0092_lane_rollup_safe_ts
Revises: 0091_lane_daily_rollup
Create Date: 2026-08-18

``user_logs.doc`` carries terminal timestamps as free text (``completed_at`` /
``posted_at`` / ``failed_at``), and the rollup must bucket by the terminal
time — a job created on day D but finishing on D+5 has to land in D+5's cell,
which is still open, otherwise it is counted nowhere at all: D was frozen
while it was still pending, and once it terminates it is no longer "stuck"
either. (codex2 reproduced exactly that disappearance on 2026-08-18.)

Parsing that text inline is not safe. Measured on PG 16, all of these RAISE
rather than degrade — one bad row would abort the whole day's aggregate:

    '2026-99-99T00:00:00Z'::timestamptz       → date/time field value out of range
    to_timestamp('2026-99-99','YYYY-MM-DD')   → same (to_timestamp is NOT lenient)
    to_timestamp('2026-02-30','YYYY-MM-DD')   → same

A shape-only regex cannot close this either: '2026-02-29' is well-formed and
still raises in a non-leap year. Hence a function with a real exception
handler — the only airtight inline option.

**STABLE, not IMMUTABLE.** ``text::timestamptz`` on a value carrying no UTC
offset is resolved against the session ``TimeZone``, so the same input is a
different instant per session — measured on PG 16:

    SET TimeZone='UTC';           '2026-01-01T00:00:00'::timestamptz → …+00
    SET TimeZone='Asia/Shanghai'; '2026-01-01T00:00:00'::timestamptz → …+08

Declaring that IMMUTABLE is a lie to the planner: it licenses constant
folding across sessions and use in index expressions, where a rebuild under
a different TimeZone would silently produce a corrupt index. STABLE is the
honest volatility (fixed within one statement, not across sessions).

STRICT so NULL input returns NULL without entering the body. The caller
supplies its own fallback (``COALESCE(safe_ts(...), to_timestamp(l.ts))``)
rather than baking one in, so "unparseable" and "absent" degrade identically
at the call site and the fallback is visible where it matters.
"""

from alembic import op


revision = "0092_lane_rollup_safe_ts"
down_revision = "0091_lane_daily_rollup"
branch_labels = None
depends_on = None


_DDL = """
CREATE OR REPLACE FUNCTION lane_rollup_safe_ts(raw text)
RETURNS timestamptz
LANGUAGE plpgsql STABLE STRICT
AS $$
BEGIN
    RETURN raw::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS lane_rollup_safe_ts(text)")
