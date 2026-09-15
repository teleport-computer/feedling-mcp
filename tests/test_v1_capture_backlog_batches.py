"""Resident V1 capture drains a backlog oldest-first, one exact batch at a time.

Before: the V1 window was "newest ≤64 live messages after the cursor", the
consumer read the newest 160 history rows and kept ``selected[-window_count:]``,
and completion moved the cursor to the newest message. A backlog larger than one
window (account broken for days, consumer offline) was silently never captured.

After (when the consumer advertises ``capture_batch_window_v1``):
- the job window is the OLDEST contiguous batch (≤60 live rows) after the
  cursor, with exact ``after_seq`` / ``through_seq`` bounds;
- the consumer pages ``/v1/chat/history?after_seq=`` and processes exactly
  ``(after_seq, through_seq]``;
- completion moves the cursor to that batch's end (never backwards);
- while a full batch is still pending the next one is scheduled on the next tick
  without waiting for min_interval / quiet.

Old consumers (no capability) keep today's window and an honest trace is left.

These tests run the real scheduler against real PostgreSQL and drive the real
consumer selection code with a history fake backed by the same DB page query the
backend route uses.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="feedling-v1-capture-backlog-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_v1_capture_backlog_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
import debug_trace  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import capture_scheduler  # noqa: E402

import tools.chat_resident_consumer as crc  # noqa: E402

from conftest import seed_user  # noqa: E402

CAP = capture_scheduler.CAPTURE_BATCH_WINDOW_CAPABILITY


def _store(tmp_path, monkeypatch, user_id: str, *, batch_capable: bool):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()
    store = core_store.UserStore(user_id)
    seed_user(store.user_id)
    caps = ["vision_observer_v1", CAP] if batch_capable else ["vision_observer_v1"]
    db.set_blob(store.user_id, "consumer_state", {
        "consumer_name": "feedling-chat-resident",
        "consumer_capabilities": caps,
    })
    return store


def _seed_backlog(store, n: int) -> list[str]:
    ids = []
    for i in range(n):
        msg_id = f"m{i:04d}"
        store.append_chat("user" if i % 2 == 0 else "openclaw", "chat", {
            "id": msg_id,
            "body_ct": f"ct_{msg_id}",
            "nonce": f"nonce_{msg_id}",
            "K_user": f"ku_{msg_id}",
            "K_enclave": f"ke_{msg_id}",
        })
        ids.append(msg_id)
    return ids


def _history_fake(store, calls: list):
    """Stand-in for the decrypt source: same durable page query as the route."""

    def fake(since=0, limit=20, include_image_body=True, after_seq=None):
        calls.append({"since": since, "limit": limit, "after_seq": after_seq})
        limit = max(1, min(int(limit), 200))
        if after_seq is None:
            rows = db.chat_history_page_by_seq_strict(store.user_id, limit=limit, latest=True)
        else:
            rows = db.chat_history_page_by_seq_strict(
                store.user_id, limit=limit, after_seq=int(after_seq))
        return [{**row, "content": f"text of {row['id']}"} for row in rows]

    return fake


def _complete(store, job, now):
    done = store.update_proactive_job(job["job_id"], {
        "status": "completed",
        # Real consumers echo the window plus a content-free fingerprint.
        "capture_window": {**job["window"], "message_count": 1, "window_chars": 10},
    })
    capture_scheduler.record_capture_job_status(store, done, status="completed", now=now)


def test_backlog_of_150_drains_in_three_exact_batches(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_150", batch_capable=True)
    ids = _seed_backlog(store, 150)
    calls: list = []
    monkeypatch.setattr(crc, "get_decrypted_history", _history_fake(store, calls))
    last_ts = float(db.chat_history_page_by_seq_strict(
        store.user_id, limit=1, latest=True)[0]["ts"])

    processed: list[str] = []
    sizes: list[int] = []
    cursors: list[int] = []
    now = last_ts + 5.0  # user is NOT quiet (quiet window is 20 min)

    for _batch in range(3):
        tick = capture_scheduler.tick_quiet_capture(store, now=now)
        if not tick["enqueued"]:
            # The final partial batch (30 rows, 15 user turns) is an ordinary
            # window again: it waits for the normal quiet/backstop trigger.
            assert _batch == 2 and tick["reason"] == "quiet_not_due", tick
            now += max(capture_scheduler.quiet_sec(), capture_scheduler.min_interval_sec()) + 1
            tick = capture_scheduler.tick_quiet_capture(store, now=now)
        assert tick["enqueued"] is True, tick
        job = tick["job"]
        window = job["window"]
        messages = crc._capture_window_messages(job)
        got = [crc._capture_message_id(m) for m in messages]
        assert got, window
        assert got[-1] == window["until_message_id"]
        processed.extend(got)
        sizes.append(len(got))
        now += 1.0
        _complete(store, job, now)
        state = capture_scheduler.load_capture_state(store)
        assert state["last_captured_until_message_id"] == window["until_message_id"]
        assert state["last_captured_until_seq"] == window["through_seq"]
        cursors.append(state["last_captured_until_seq"])
        now += 1.0

    assert sizes == [60, 60, 30]
    assert processed == ids  # no gap, no duplicate, oldest first
    assert cursors == sorted(cursors) and len(set(cursors)) == 3
    # The consumer paged by seq; it never fell back to "newest page".
    assert calls and all(c["after_seq"] is not None for c in calls)

    after = capture_scheduler.tick_quiet_capture(store, now=now + 10_000)
    assert after["enqueued"] is False
    assert after["reason"] in {"no_new_messages", "already_captured"}


def test_backlog_drains_without_waiting_for_quiet_or_min_interval(tmp_path, monkeypatch):
    """Mostly AI-side rows: too few user turns for the backstop, user still active."""
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_drain", batch_capable=True)
    for i in range(130):
        msg_id = f"d{i:04d}"
        store.append_chat("user" if i % 10 == 0 else "openclaw", "chat", {
            "id": msg_id, "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        })
    last_ts = float(db.chat_history_page_by_seq_strict(
        store.user_id, limit=1, latest=True)[0]["ts"])
    first = capture_scheduler.tick_quiet_capture(store, now=last_ts + 1)
    assert first["enqueued"] is True and first["job"]["trigger"] == "backlog_drain"
    _complete(store, first["job"], last_ts + 2)
    second = capture_scheduler.tick_quiet_capture(store, now=last_ts + 3)
    assert second["enqueued"] is True and second["job"]["trigger"] == "backlog_drain"
    assert second["job"]["window"]["after_seq"] == first["job"]["window"]["through_seq"]
    _complete(store, second["job"], last_ts + 4)
    # 10 rows left: back to the ordinary gates.
    third = capture_scheduler.tick_quiet_capture(store, now=last_ts + 5)
    assert third["enqueued"] is False and third["reason"] == "quiet_not_due"


def test_batch_window_carries_exact_bounds_through_the_job_row(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_bounds", batch_capable=True)
    _seed_backlog(store, 75)
    first_seq = int(db.chat_history_page_by_seq_strict(
        store.user_id, limit=1, after_seq=0)[0]["seq"])
    result = capture_scheduler.force_capture(store)
    window = result["job"]["window"]
    rows = db.chat_history_page_by_seq_strict(store.user_id, limit=60, after_seq=0)
    assert window["after_seq"] == 0
    assert window["after_message_id"] == ""
    assert window["message_count"] == 60
    assert window["through_seq"] == rows[-1]["seq"]
    assert window["until_message_id"] == rows[-1]["id"]
    assert window["backlog_remaining"] is True
    assert rows[0]["seq"] == first_seq


def test_stale_completion_replay_never_moves_cursor_backwards(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_replay", batch_capable=True)
    _seed_backlog(store, 130)
    first = capture_scheduler.force_capture(store)["job"]
    _complete(store, first, 100.0)
    second = capture_scheduler.force_capture(store)["job"]
    _complete(store, second, 101.0)
    advanced = capture_scheduler.load_capture_state(store)
    assert advanced["last_captured_until_seq"] == second["window"]["through_seq"]

    # A replayed/delayed completion of batch 1 arrives again (status flip).
    store.update_proactive_job(first["job_id"], {"status": "realizing"})
    _complete(store, first, 102.0)
    state = capture_scheduler.load_capture_state(store)
    assert state["last_captured_until_seq"] == second["window"]["through_seq"]
    assert state["last_captured_until_message_id"] == second["window"]["until_message_id"]


def test_old_consumer_keeps_legacy_window_and_backend_says_so(tmp_path, monkeypatch):
    """Old consumer (no capability): today's newest window, plus an honest trace.

    Handing it the oldest batch would be worse: it only reads the newest 160
    history rows, cannot find a batch that lies before them, and would fail the
    job over and over until the poison-window escape skipped it.
    """
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_old_consumer", batch_capable=False)
    ids = _seed_backlog(store, 300)
    events: list[dict] = []
    real_trace = debug_trace.trace_event

    def spy(store_arg, **kwargs):
        events.append(kwargs)
        return real_trace(store_arg, **kwargs)

    monkeypatch.setattr(debug_trace, "trace_event", spy)
    result = capture_scheduler.force_capture(store)
    window = result["job"]["window"]
    assert "through_seq" not in window
    assert window["until_message_id"] == ids[-1]
    assert any(e.get("type") == "memory.capture.legacy_window_backlog" for e in events)

    # Why the gate exists: the pre-change consumer selection (no through_seq)
    # on an oldest-first batch that lies before its newest-160 read gets nothing.
    calls: list = []
    monkeypatch.setattr(crc, "get_decrypted_history", _history_fake(store, calls))
    rows = db.chat_history_page_by_seq_strict(store.user_id, limit=60, after_seq=0)
    batch_as_old_consumer_sees_it = {"window": {
        "after_message_id": "",
        "until_message_id": rows[-1]["id"],
        "until_ts": float(rows[-1]["ts"]),
        "message_count": 60,
    }}
    assert crc._capture_window_messages(batch_as_old_consumer_sees_it) == []


def test_new_consumer_on_old_backend_window_uses_legacy_selection(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_old_backend", batch_capable=False)
    ids = _seed_backlog(store, 10)
    calls: list = []
    monkeypatch.setattr(crc, "get_decrypted_history", _history_fake(store, calls))
    old_backend_job = {"window": {
        "after_message_id": ids[3],
        "until_message_id": ids[9],
        "until_ts": 0,
        "message_count": 6,
        "after_seq": 0,  # old backends send after_seq but never through_seq
    }}
    got = [crc._capture_message_id(m) for m in crc._capture_window_messages(old_backend_job)]
    assert got == ids[4:10]
    assert calls and all(c["after_seq"] is None for c in calls)


def test_v2_submit_path_window_is_unchanged_even_with_capability(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, "usr_v1_backlog_v2_seam", batch_capable=True)
    ids = _seed_backlog(store, 90)
    seen: list[dict] = []

    def submit(_store, *, trigger, now, window, capture_key):
        seen.append(dict(window))
        return {"enqueued": True, "reason": "enqueued", "job": {"capture_key": capture_key}}

    capture_scheduler.force_capture(store, submit=submit)
    assert seen and "through_seq" not in seen[0]
    assert seen[0]["until_message_id"] == ids[-1]


def test_consumer_batch_paging_is_exact_and_refuses_partial(monkeypatch):
    rows = [
        {"id": f"r{i}", "seq": i, "ts": float(i), "role": "user" if i % 3 else "system",
         "source": "chat", "content": f"row {i}"}
        for i in range(1, 451)
    ]

    def fake(since=0, limit=20, include_image_body=True, after_seq=None):
        assert after_seq is not None and limit == crc.CAPTURE_BATCH_PAGE_LIMIT
        return [r for r in rows if r["seq"] > after_seq][:limit]

    monkeypatch.setattr(crc, "get_decrypted_history", fake)
    got = crc._capture_batch_window_messages(150, 420)
    seqs = [m["seq"] for m in got]
    # Spans three pages; only (150, 420]; non-conversation roles are dropped by
    # the existing live-history filter, never by trimming the batch's head.
    assert seqs == [i for i in range(151, 421) if i % 3]

    broken = [dict(r) for r in rows]
    broken[200]["seq"] = None
    monkeypatch.setattr(
        crc, "get_decrypted_history",
        lambda since=0, limit=20, include_image_body=True, after_seq=None:
            [r for r in broken if r["seq"] is None or r["seq"] > after_seq][:limit],
    )
    assert crc._capture_batch_window_messages(150, 420) == []

    monkeypatch.setattr(
        crc, "get_decrypted_history",
        lambda since=0, limit=20, include_image_body=True, after_seq=None: None,
    )
    assert crc._capture_batch_window_messages(150, 420) == []


def test_job_window_sanitizer_keeps_batch_bounds():
    kept = capture_jobs._safe_window({
        "after_message_id": "a", "after_seq": 5, "until_message_id": "u",
        "until_ts": 9.0, "message_count": 60, "through_seq": 99,
        "backlog_remaining": True,
    })
    assert kept["through_seq"] == 99 and kept["after_seq"] == 5
    assert kept["backlog_remaining"] is True
    legacy = capture_jobs._safe_window({"after_message_id": "a", "until_message_id": "u"})
    assert "through_seq" not in legacy and "backlog_remaining" not in legacy


def test_capability_and_batch_size_agree_across_consumer_backend_and_v2():
    from model_api_runtime.v2 import worker as v2_worker

    assert crc.CAPTURE_BATCH_WINDOW_CAPABILITY == CAP
    for hosted in (False, True):
        advertised = {c.strip() for c in crc._consumer_capabilities(hosted).split(",")}
        assert CAP in advertised
    assert capture_scheduler.CAPTURE_V1_BATCH_LIMIT == v2_worker._CAPTURE_BATCH_LIMIT
    # "message_count >= one batch" is only a backlog signal while discovery
    # reads more rows than one batch.
    assert max(64, min(1000, capture_scheduler.turn_backstop() * 2)) > \
        capture_scheduler.CAPTURE_V1_BATCH_LIMIT


def test_mixed_history_keeps_page_seq_for_single_decrypted_rows(monkeypatch):
    page = {"messages": [
        {"id": "p1", "seq": 7, "ts": 1.0, "role": "user", "source": "chat", "body": "hi"},
        {"id": "s1", "seq": 8, "ts": 2.0, "role": "user", "source": "chat", "body_ct": "x"},
    ]}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return page

    monkeypatch.setattr(crc._HTTP, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        crc, "_fetch_message_body_from_enclave",
        # The enclave single-message reply carries "seq": None.
        lambda message_id: {"id": message_id, "seq": None, "content": "decrypted"},
    )
    handled, rows = crc._fetch_plaintext_or_mixed_history(
        0, 200, include_image_body=False, after_seq=6)
    assert handled is True
    assert [r["seq"] for r in rows] == [7, 8]


def _route_consumer_http_to_real_backend(monkeypatch, api_key: str) -> list[str]:
    """Send the consumer's backend GETs to the real assembled ASGI app."""
    import urllib.parse

    from asgi_test_client import make_client

    client = make_client()
    paths: list[str] = []

    class _Resp:
        def __init__(self, shim):
            self._shim = shim
            self.status_code = shim.status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._shim.get_json()

    def fake_get(url, params=None, headers=None, timeout=None):
        path = url[len(crc.FEEDLING_API_URL):]
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        paths.append(path)
        return _Resp(client.get(path, headers=dict(headers or {})))

    monkeypatch.setattr(crc._HTTP, "get", fake_get)
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "")
    # This consumer authenticates as the registered user with its API key only
    # (other tests may leave a hosted runtime token in the shared header dict).
    headers = {k: v for k, v in crc._HEADERS.items() if k != "X-Feedling-Runtime-Token"}
    headers["X-API-Key"] = api_key
    monkeypatch.setattr(crc, "_HEADERS", headers)
    return paths


def test_batch_window_is_complete_past_thousands_of_excluded_rows(backend_env, monkeypatch):
    """Codex review 2026-09-15 (C1).

    Before: the consumer gave up after 10 history pages (2000 raw rows). The
    backend cuts the batch counting only live user/openclaw rows, so >2000
    non-live rows between the cursor and ``through_seq`` made every attempt
    return an empty window -> ``capture_window_unavailable`` -> the escape valve
    eventually skipped 60 real messages that were never read.

    After: paging continues while seq advances, so the window is complete.
    Runs the real scheduler, the real ``/v1/chat/history`` route and the real
    consumer history reader (plaintext rows, no history fake).
    """
    import base64

    from asgi_test_client import make_client

    registered = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(b"\x5a" * 32).decode(), "archive_language": "zh"},
    )
    assert registered.status_code == 201, registered.get_data(as_text=True)
    api_key = registered.get_json()["api_key"]
    store = core_store.get_store(registered.get_json()["user_id"])
    db.set_blob(store.user_id, "consumer_state", {
        "consumer_name": "feedling-chat-resident",
        "consumer_capabilities": ["vision_observer_v1", CAP],
    })

    store.append_chat("user", "chat", {"id": "live_first", "body": "我下周要去大阪出差"})
    # More non-live rows than the old 10-page (2000-row) reach. The backend batch
    # cut never counts them (server-authored maintenance prompts here).
    excluded = 2250
    with monkeypatch.context() as quiet:
        quiet.setattr(capture_scheduler, "record_chat_append", lambda *a, **k: {})
        for i in range(excluded):
            store.append_chat(
                "system", "resident_maintenance",
                {"id": f"sys{i:05d}", "body": f"maintenance {i}"},
            )
    store.append_chat("openclaw", "chat", {"id": "live_last", "body": "好的，记得带护照"})

    paths = _route_consumer_http_to_real_backend(monkeypatch, api_key)
    last_ts = float(db.chat_history_page_by_seq_strict(
        store.user_id, limit=1, latest=True)[0]["ts"])
    now = last_ts + max(capture_scheduler.quiet_sec(), capture_scheduler.min_interval_sec()) + 5
    tick = capture_scheduler.tick_quiet_capture(store, now=now)
    assert tick["enqueued"] is True, tick
    job = tick["job"]
    window = job["window"]
    assert window["until_message_id"] == "live_last"
    assert window["through_seq"] - window["after_seq"] > excluded

    messages = crc._capture_window_messages(job)
    ids = [crc._capture_message_id(m) for m in messages]
    assert ids and ids[0] == "live_first" and ids[-1] == "live_last", ids[:3]
    assert "我下周要去大阪出差" in crc._capture_window_text(messages)
    # Really went through the real route, past the old 10-page reach.
    assert len(paths) > 10 and all("/v1/chat/history?" in p and "after_seq=" in p for p in paths)
