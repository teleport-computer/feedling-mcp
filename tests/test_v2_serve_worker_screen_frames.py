"""Content-free cache/concurrency contracts for screen-frame decryption."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client  # noqa: E402
from model_api_runtime.v2 import screen_chat, serve_worker, tool_loop, worker  # noqa: E402
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


def test_screen_frame_batch_is_bounded_to_latest_four_contract(monkeypatch):
    seen = []
    monkeypatch.setattr(
        serve_worker,
        "_read_screen_frame_cached",
        lambda _uid, frame_id: (seen.append(frame_id) or {"image_b64": "YWJj"}, False),
    )

    batch = serve_worker._read_screen_frames(
        "u1", [f"f{i}" for i in range(10)]
    )

    assert seen == [f"f{i}" for i in range(4)]
    assert set(batch["frames"]) == set(seen)
    assert batch["cache_hits"] == 0
    assert batch["cache_misses"] == 4


def test_screen_pixel_gate_blocks_only_explicit_unsupported():
    assert worker._screen_vision_allows_pixels(
        {"vision_test_status": "ok"}
    )
    assert worker._screen_vision_allows_pixels(
        {"vision_test_status": "untested"}
    )
    assert worker._screen_vision_allows_pixels(
        {"vision_test_status": "failed"}
    )
    assert worker._screen_vision_allows_pixels(None)
    assert worker._screen_vision_allows_pixels({})
    assert not worker._screen_vision_allows_pixels(
        {"vision_test_status": "unsupported"}
    )


def test_untested_rejection_learns_unsupported_and_closes_next_turn(
    monkeypatch,
):
    verdict = {
        "id": "route-1",
        "vision_test_status": "untested",
        "updated_at": "2026-08-10T12:00:00Z",
    }
    marks = []

    def _mark(user_id, route_id, **kwargs):
        marks.append((user_id, route_id, kwargs))
        verdict["vision_test_status"] = kwargs["status"]
        return True

    monkeypatch.setattr(
        worker.db, "model_api_route_mark_vision_test", _mark
    )

    responses = [
        provider_client.ProviderError("images rejected", status_code=415),
        {"reply": "text fallback", "tool_calls": [], "usage": {}},
        {"reply": "next turn", "tool_calls": [], "usage": {}},
    ]
    calls = []

    async def _provider(config, messages, *, tools=None, **kwargs):
        calls.append(list(messages))
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    tagged = {
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,AAAA"},
        }],
        screen_chat.MESSAGE_TAG: True,
    }
    plain = {"role": "user", "content": "hello"}

    async def _dispatch(_calls):
        return []

    async def _reply(_text, *, final, **_kwargs):
        return None

    async def _fold():
        return []

    async def _learn(_exc):
        await asyncio.to_thread(
            worker._mark_screen_route_vision_unsupported,
            "u1",
            dict(verdict),
        )

    config = provider_client.ProviderConfig(
        provider="anthropic", model="vision-test", api_key="test"
    )
    assert worker._screen_vision_allows_pixels(verdict)
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=config,
        build_messages=lambda _transcript: [tagged, plain],
        dispatch_tools=_dispatch,
        on_reply=_reply,
        fold_new_messages=_fold,
        add_usage=lambda _usage: None,
        max_calls=2,
        tagged_image_message_key=screen_chat.MESSAGE_TAG,
        on_tagged_images_rejected=_learn,
    ))

    assert verdict["vision_test_status"] == "unsupported"
    assert marks == [(
        "u1",
        "route-1",
        {
            "status": "unsupported",
            "error": "vision_model_incompatible",
            "expected_updated_at": "2026-08-10T12:00:00Z",
        },
    )]
    assert tagged in calls[0]
    assert tagged not in calls[1]

    # The next foreground turn observes the learned verdict and never attaches
    # the screen block, so the provider receives only one text request.
    next_messages = [tagged, plain] if worker._screen_vision_allows_pixels(verdict) else [plain]
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=config,
        build_messages=lambda _transcript: next_messages,
        dispatch_tools=_dispatch,
        on_reply=_reply,
        fold_new_messages=_fold,
        add_usage=lambda _usage: None,
        max_calls=1,
        tagged_image_message_key=screen_chat.MESSAGE_TAG,
        on_tagged_images_rejected=_learn,
    ))
    assert len(calls) == 3
    assert tagged not in calls[2]
