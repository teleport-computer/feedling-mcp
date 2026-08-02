"""Correctness of admin_data_track_retention_daily (classic day-N cohort
retention, post-freeze-only). Seeds two cohorts with controlled signup days and
day-offset activity, plus one pre-freeze cohort that must be excluded.
"""
from __future__ import annotations

import base64
import itertools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from accounts import registry  # noqa: E402

_BJ = ZoneInfo("Asia/Shanghai")
_pk = itertools.count(1)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _register(client) -> str:
    raw = next(_pk).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode(), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def _set_signup(user_id: str, day: str) -> None:
    # 12:00 Beijing on `day` → unambiguously that Beijing calendar day.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE users SET created_at=%s WHERE user_id=%s",
            (f"{day}T12:00:00+08:00", user_id),
        )


def _activity(user_id: str, day: str) -> None:
    ts = datetime.fromisoformat(f"{day}T14:00:00+08:00").timestamp()
    db.chat_append(user_id, f"m_{user_id}_{day}", ts,
                   {"role": "user", "source": "chat"}, 500)


def _today_minus(n: int) -> str:
    return (datetime.now(_BJ).date() - timedelta(days=n)).isoformat()


def _cohort(result: dict, day: str) -> dict:
    return next((c for c in result["cohorts"] if c["cohort"] == day), {})


def test_day_n_retention_math_and_freeze_floor(client):
    freeze = _today_minus(50)

    # Cohort A: mature (signup 40d ago). 10 users; 4 back on D1, 2 back on D7.
    a_day = _today_minus(40)
    a_users = [_register(client) for _ in range(10)]
    for u in a_users:
        _set_signup(u, a_day)
    a_d1 = (datetime.fromisoformat(a_day) + timedelta(days=1)).date().isoformat()
    a_d7 = (datetime.fromisoformat(a_day) + timedelta(days=7)).date().isoformat()
    for u in a_users[:4]:
        _activity(u, a_d1)
    for u in a_users[:2]:
        _activity(u, a_d7)

    # Cohort B: young (signup 2d ago). 5 users; 3 back on D1. D7+ immature → None.
    b_day = _today_minus(2)
    b_users = [_register(client) for _ in range(5)]
    for u in b_users:
        _set_signup(u, b_day)
    b_d1 = (datetime.fromisoformat(b_day) + timedelta(days=1)).date().isoformat()
    for u in b_users[:3]:
        _activity(u, b_d1)

    # Pre-freeze cohort: signup 100d ago → must be EXCLUDED (< freeze floor).
    old = _register(client)
    _set_signup(old, _today_minus(100))

    result = db.admin_data_track_retention_daily(since_day=freeze)

    ca = _cohort(result, a_day)
    assert ca.get("size") == 10, result
    assert ca["cells"][1] == 40.0, ca      # 4/10 on D1
    assert ca["cells"][3] == 0.0, ca       # none seeded on D3 (mature → 0, not None)
    assert ca["cells"][7] == 20.0, ca      # 2/10 on D7
    assert ca["cells"][30] == 0.0, ca      # mature, none returned

    cb = _cohort(result, b_day)
    assert cb.get("size") == 5, result
    assert cb["cells"][1] == 60.0, cb      # 3/5 on D1 (mature)
    assert cb["cells"][7] is None, cb      # immature → None, not 0
    assert cb["cells"][30] is None, cb

    assert _today_minus(100) not in [c["cohort"] for c in result["cohorts"]], \
        "pre-freeze cohort leaked past the freeze floor"

    # Weekly granularity: same users, cohort keyed by ISO week (Monday).
    wk = db.admin_data_track_retention_daily(since_day=freeze, granularity="week")
    assert wk["granularity"] == "week"
    assert sum(c["size"] for c in wk["cohorts"]) == 10 + 5, wk  # A + B users


def _acct_row(result: dict, day: str) -> dict:
    return next((r for r in result["rows"] if r["day"] == day), {})


def test_growth_accounting_new_resurrected_retained_churned(client):
    a_day = _today_minus(6)   # freeze = baseline day
    b_day = _today_minus(5)   # first day with deltas

    u1, u2, u3, u4 = (_register(client) for _ in range(4))
    _set_signup(u1, a_day); _set_signup(u2, a_day)
    _set_signup(u3, b_day)   # signs up on B → new on B
    _set_signup(u4, a_day)   # existed before B → resurrected candidate

    _activity(u1, a_day); _activity(u1, b_day)   # active both → retained on B
    _activity(u2, a_day)                          # active A only → churns on B
    _activity(u3, b_day)                          # new on B
    _activity(u4, b_day)                          # active B, not A → resurrected

    result = db.admin_data_track_growth_accounting(since_day=a_day)

    ra = _acct_row(result, a_day)
    assert ra.get("active") == 2 and ra.get("new") == 2, ra
    assert ra.get("retained") is None, ra  # baseline day → no prior comparison

    rb = _acct_row(result, b_day)
    assert rb == {
        "day": b_day, "active": 3, "new": 1,
        "resurrected": 1, "retained": 1, "churned": 1, "quick_ratio": 2.0,
    }, rb
