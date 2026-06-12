from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


def test_relationship_days_use_calendar_dates_and_memory_anchor(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parent.parent
    backend_dir = repo_root / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    monkeypatch.setenv("FEEDLING_DATA_DIR", str(tmp_path / "data"))

    import backend.app as app

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 22, 1, 30, tzinfo=tz)

    monkeypatch.setattr(app, "datetime", FakeDatetime)
    import identity.service as identity_service
    monkeypatch.setattr(identity_service, "datetime", FakeDatetime)
    # Moments now live in PostgreSQL (see backend/db.py), so seed them through
    # the persistence layer instead of writing a memory.json file.
    app.db.memory_replace_all("usr_test", [
        {"id": "m1", "occurred_at": "2026-04-10T09:00:00"},
        {"id": "m2", "occurred_at": "2026-04-12T09:00:00"},
    ])
    store = SimpleNamespace(
        user_id="usr_test",
        memory_lock=threading.Lock(),
    )

    assert app._anchor_from_days(41, store=store, prefer_memory=True) == "2026-04-10"

    # Old anchors created after the server had crossed UTC midnight can be one
    # date later than the user's first memory. Existing identities have no
    # relationship_anchor_source, so read-time repair uses the earlier memory.
    old_identity = {"relationship_started_at": "2026-04-11T01:30:00"}
    assert app._live_days_with_user(old_identity, store=store) == 42

    # Explicit user calibration should not be overridden by an older card.
    calibrated_identity = {
        "relationship_started_at": "2026-04-11",
        "relationship_anchor_source": "user_calibrated",
    }
    assert app._live_days_with_user(calibrated_identity, store=store) == 41
