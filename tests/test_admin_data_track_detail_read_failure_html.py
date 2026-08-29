"""T367 作者侧补充:真实 snapshot 失败必须在 detail 输出中可见。"""
from __future__ import annotations

from tests.test_admin_data_track_detail_read_failure import (
    INJECTED_TIMEOUT_MS,
    RealReadFailure,
    _detail,
    _healthy_then_broken,
)
from tests.test_data_track import _admin_headers, _register, client  # noqa: F401

import db


def test_detail_html_visibly_warns_that_snapshot_is_unread(client, monkeypatch):
    user_id, _ = _register(client)
    # Warm the legacy store cache before locking frame_envelopes. Otherwise
    # the HTML route's pre-snapshot compatibility read waits on the same lock
    # without a statement timeout and the lock expires before the target read.
    assert _detail(client, user_id)["snapshot_read_status"]["level"] == "ok"
    monkeypatch.setattr(db, "_ADMIN_DATA_TRACK_READ_TIMEOUT_MS", INJECTED_TIMEOUT_MS)

    with RealReadFailure():
        response = client.get(
            f"/admin/data-track/users/{user_id}?days=3",
            headers=_admin_headers(),
        )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "data-quality-warning" in body
    assert "snapshot_read_failed" in body


def test_detail_payload_marks_poll_evidence_unknown(client, monkeypatch):
    _, broken, _ = _healthy_then_broken(client, monkeypatch)

    responder = broken["responder"]
    assert responder["read_status"] == "read_failed"
    assert responder["poll_evidence_status"] == "unknown"
