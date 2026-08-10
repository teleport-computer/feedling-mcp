"""Content-free cache/concurrency contracts for screen-frame decryption."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402


def _reset_cache() -> None:
    with serve_worker._SCREEN_FRAME_CACHE_LOCK:
        serve_worker._SCREEN_FRAME_CACHE.clear()
        serve_worker._SCREEN_FRAME_INFLIGHT.clear()


def test_screen_frame_decrypt_cache_is_keyed_by_user_and_frame(monkeypatch):
    _reset_cache()
    calls = []
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda uid: f"store:{uid}")
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda uid: f"rt:{uid}")

    def _decrypt(store, frame_id, **kwargs):
        calls.append((store, frame_id, kwargs["runtime_token"]))
        return ScreenResult(
            200,
            raw_body=json.dumps(
                {"image_b64": "YWJj", "image_mime": "image/png", "ts": 1.0}
            ).encode(),
        )

    monkeypatch.setattr(serve_worker.screen_read_core, "frame_decrypt", _decrypt)

    first, first_hit = serve_worker._read_screen_frame_cached("u1", "f1")
    second, second_hit = serve_worker._read_screen_frame_cached("u1", "f1")
    other_user, other_hit = serve_worker._read_screen_frame_cached("u2", "f1")

    assert first == second == other_user
    assert (first_hit, second_hit, other_hit) == (False, True, False)
    assert calls == [
        ("store:u1", "f1", "rt:u1"),
        ("store:u2", "f1", "rt:u2"),
    ]


def test_screen_frame_cache_single_flights_concurrent_decrypt(monkeypatch):
    _reset_cache()
    calls = 0
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: object())
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")

    def _decrypt(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return ScreenResult(200, json_body={"image_b64": "YWJj", "ts": 1.0})

    monkeypatch.setattr(serve_worker.screen_read_core, "frame_decrypt", _decrypt)
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                serve_worker._read_screen_frame_cached("u1", "f1")
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=2)
    time.sleep(0.02)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert calls == 1
    assert len(results) == 2
    assert sorted(hit for _value, hit in results) == [False, True]


def test_failed_screen_decrypt_is_briefly_negative_cached(monkeypatch):
    _reset_cache()
    calls = []
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: object())
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.screen_read_core,
        "frame_decrypt",
        lambda *_args, **_kwargs: calls.append("decrypt") or ScreenResult(502),
    )

    first = serve_worker._read_screen_frame_cached("u1", "f1")
    second = serve_worker._read_screen_frame_cached("u1", "f1")

    assert first == (None, False)
    assert second == (None, True)
    assert calls == ["decrypt"]


def test_screen_frame_batch_is_bounded_to_six(monkeypatch):
    seen = []
    monkeypatch.setattr(
        serve_worker,
        "_read_screen_frame_cached",
        lambda _uid, frame_id: (seen.append(frame_id) or {"image_b64": "YWJj"}, False),
    )

    batch = serve_worker._read_screen_frames(
        "u1", [f"f{i}" for i in range(10)]
    )

    assert seen == [f"f{i}" for i in range(6)]
    assert set(batch["frames"]) == set(seen)
    assert batch["cache_hits"] == 0
    assert batch["cache_misses"] == 6
