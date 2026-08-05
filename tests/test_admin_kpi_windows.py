"""KPI window edge semantics + funnel-outage honesty (SQL review S1/S2).

S1: ``recent_admin_product_kpis`` period-over-period windows must be
half-open on the upper edge when ``offset_hours > 0`` (matching
``recent_token_usage_by_lane``) so a row exactly on the shared edge lands in
exactly one window — while ``offset_hours == 0`` keeps the historical closed
upper bound byte-for-byte (behavior and query plan unchanged).

S2: ``admin_onboarding_funnel`` returns ``None`` on query failure instead of
``[]`` so a funnel outage in a zero-registration window surfaces as
coverage_complete=False (未知) rather than a confident 0 / 0.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402

requires_pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="SQL-capture window tests require PostgreSQL",
)

_NOW_TS = 2_000_000.0
_CUTOFF_TS = _NOW_TS - 24 * 3600.0


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Just enough connection for ``recent_admin_product_kpis``: a fixed
    bounds/count row plus ``info.server_version`` for the timestamp-parse SQL
    branch. Lets tests pin ``now_ts``/``cutoff_ts`` exactly — impossible
    against a live ``clock_timestamp()``."""

    class _Info:
        server_version = 160000

    info = _Info()

    def __init__(self, row):
        self._row = row
        self.sql: str | None = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        return _FakeCursor(self._row)


def _fake_pool(monkeypatch, *, new_registered: int) -> _FakeConn:
    # row: (now_ts, cutoff_ts, window_app_users, app_sessions,
    #       new_registered, unparseable, account_rows)
    conn = _FakeConn(
        (_NOW_TS, _CUTOFF_TS, 0, 0, new_registered, 0, new_registered)
    )

    class _Pool:
        @contextlib.contextmanager
        def connection(self):
            yield conn

    monkeypatch.setattr(db, "get_pool", lambda: _Pool())
    return conn


def _funnel_row(user_id: str, t0: float | None) -> dict:
    return {"user_id": user_id, "route": "resident", "t0": t0,
            "t1": None, "t2": None, "t3": None}


# --------------------------------------------------------------------------- #
# S1 (b): shared-edge row lands in exactly one window when offset > 0
# --------------------------------------------------------------------------- #


def test_cohort_filter_offset_window_excludes_upper_edge(monkeypatch):
    _fake_pool(monkeypatch, new_registered=2)
    rows = [
        _funnel_row("u_edge_upper", _NOW_TS),        # exactly on shared edge
        _funnel_row("u_edge_lower", _CUTOFF_TS),      # lower edge stays closed
        _funnel_row("u_mid", (_NOW_TS + _CUTOFF_TS) / 2),
        _funnel_row("u_no_t0", None),
    ]
    monkeypatch.setattr(
        db, "admin_onboarding_funnel", lambda **_kw: list(rows)
    )

    out = db.recent_admin_product_kpis(within_hours=24, offset_hours=24)

    # Upper edge excluded (it belongs to the NEXT window's closed lower
    # edge), lower edge included: cohort == {u_edge_lower, u_mid} == SQL
    # count 2, so coverage stays complete — Python filter and SQL CTEs agree.
    assert out["onboarding"]["coverage_complete"] is True
    assert out["onboarding"]["cohort_accounts"] == 2


def test_cohort_filter_offset_zero_keeps_closed_upper_edge(monkeypatch):
    _fake_pool(monkeypatch, new_registered=3)
    rows = [
        _funnel_row("u_edge_upper", _NOW_TS),
        _funnel_row("u_edge_lower", _CUTOFF_TS),
        _funnel_row("u_mid", (_NOW_TS + _CUTOFF_TS) / 2),
    ]
    monkeypatch.setattr(
        db, "admin_onboarding_funnel", lambda **_kw: list(rows)
    )

    out = db.recent_admin_product_kpis(within_hours=24)

    # offset=0 preserves today's closed-closed behavior exactly: all three
    # rows in-window, coverage complete.
    assert out["onboarding"]["coverage_complete"] is True
    assert out["onboarding"]["cohort_accounts"] == 3


def test_sql_upper_edge_operator_matches_offset(monkeypatch):
    conn0 = _fake_pool(monkeypatch, new_registered=0)
    monkeypatch.setattr(db, "admin_onboarding_funnel", lambda **_kw: [])
    db.recent_admin_product_kpis(within_hours=24)
    sql0 = conn0.sql or ""

    conn24 = _fake_pool(monkeypatch, new_registered=0)
    db.recent_admin_product_kpis(within_hours=24, offset_hours=24)
    sql24 = conn24.sql or ""

    # offset=0: closed upper bound, byte-for-byte the historical predicates
    # (behavior AND query plan must not change).
    assert "AND logs.ts >= bounds.cutoff_ts AND logs.ts <= bounds.now_ts" in sql0
    assert re.search(r"logs\.ts < bounds\.now_ts", sql0) is None
    assert "<= bounds.now_ts" in sql0.split("registrations AS")[1]

    # offset>0: strict upper bound in BOTH CTEs (sessions + registrations).
    assert "AND logs.ts >= bounds.cutoff_ts AND logs.ts < bounds.now_ts" in sql24
    reg24 = sql24.split("registrations AS")[1].split("SELECT\n")[0]
    assert re.search(r"(?<!<)<\s+bounds\.now_ts", reg24)
    assert "<= bounds.now_ts" not in reg24


@requires_pg
def test_shipped_operators_put_edge_row_in_exactly_one_window(monkeypatch):
    """Empirical shared-clock check: extract the upper-edge operators the
    function actually ships (offset=0 vs offset=24) and evaluate a synthetic
    row at exactly the shared edge under ONE clock — it must land in exactly
    one window (the closed lower edge of the newer one)."""
    ops = {}
    for offset in (0, 24):
        conn = _fake_pool(monkeypatch, new_registered=0)
        monkeypatch.setattr(db, "admin_onboarding_funnel", lambda **_kw: [])
        db.recent_admin_product_kpis(within_hours=24, offset_hours=offset)
        m = re.search(r"AND logs\.ts (<=|<) bounds\.now_ts", conn.sql or "")
        assert m, "sessions upper-edge predicate not found in shipped SQL"
        ops[offset] = m.group(1)
    monkeypatch.undo()

    with db.get_pool().connection() as conn:
        row = conn.execute(
            f"""
            SELECT (x >= n - 86400  AND x {ops[0]}  n)          AS in_current,
                   (x >= n - 172800 AND x {ops[24]} n - 86400)  AS in_previous
            FROM (SELECT 1754300000.0::float8 AS n,
                         1754300000.0 - 86400 AS x) s
            """
        ).fetchone()
    assert row[0] is True and row[1] is False
    assert int(row[0]) + int(row[1]) == 1


# --------------------------------------------------------------------------- #
# S2 (c): funnel outage must read as 未知, never as a confident 0 / 0
# --------------------------------------------------------------------------- #


def test_admin_onboarding_funnel_returns_none_on_query_failure(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_pool", boom)
    assert db.admin_onboarding_funnel() is None
    assert db.admin_onboarding_funnel(registered_cutoff_ts=123.0) is None


def test_kpis_map_funnel_outage_to_incomplete_coverage(monkeypatch):
    # Zero-registration window + funnel OUTAGE: exactly the case where the
    # old []-on-error return made len(cohort) == 0 == new_registered render
    # a confident 0 / 0 instead of the honest 未知.
    _fake_pool(monkeypatch, new_registered=0)
    monkeypatch.setattr(db, "admin_onboarding_funnel", lambda **_kw: None)

    out = db.recent_admin_product_kpis(within_hours=24, offset_hours=24)

    onboarding = out["onboarding"]
    assert onboarding["coverage_complete"] is False
    assert onboarding["configured"] is None
    assert onboarding["content_ready"] is None
    assert onboarding["first_genuine_reply"] is None
    assert onboarding["completion_rate"] is None
    assert onboarding["cohort_accounts"] == 0


def test_kpis_keep_complete_coverage_for_honest_empty_cohort(monkeypatch):
    # A funnel that SUCCEEDS with an empty list in a zero-registration
    # window is genuinely complete coverage — only None (outage) degrades.
    _fake_pool(monkeypatch, new_registered=0)
    monkeypatch.setattr(db, "admin_onboarding_funnel", lambda **_kw: [])

    out = db.recent_admin_product_kpis(within_hours=24, offset_hours=24)

    onboarding = out["onboarding"]
    assert onboarding["coverage_complete"] is True
    assert onboarding["configured"] == 0
    assert onboarding["completion_rate"] is None  # 0-denominator rate stays None
