from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def _wire_reader(monkeypatch, body: dict, *, exercise_post: bool = False):
    monkeypatch.setattr(
        serve_worker.core_store,
        "get_store",
        lambda uid: types.SimpleNamespace(user_id=uid),
    )
    monkeypatch.setattr(
        serve_worker,
        "_mint_runtime_token",
        lambda _uid: "runtime-token",
    )
    monkeypatch.setattr(
        serve_worker.db,
        "memory_profile_source_snapshot",
        lambda _uid: {
            "card_count": int(body.get("source_card_count", body.get("user_card_count", 0))),
            "max_updated_at": "2026-07-31T00:00:00Z",
        },
    )
    observed = {}

    def _index(_store, api_key, params, *, post_enclave):
        observed.update(api_key=api_key, params=params)
        if exercise_post:
            post_enclave(
                api_key,
                [{"id": "candidate"}],
                operation="memory.index",
                payload={"limit": 0},
            )
        return body, 200

    monkeypatch.setattr(serve_worker.memory_core, "index", _index)
    return observed


def test_profile_reader_keeps_summary_and_bounded_content(monkeypatch):
    body = {
        "items": [
            {
                "id": "m1",
                "bucket": "关系",
                "occurred_at": "2026-07-01",
                "summary": "Seven 喜欢直接反馈",
                "content": "不要使用空泛套话",
            }
        ],
        "user_card_count": 1,
        "truncated": False,
    }
    _wire_reader(monkeypatch, body)

    result = serve_worker._read_profile_cards("u-cards")

    assert result["eligible_card_count"] == 1
    assert result["card_count"] == 1
    assert "关系" in result["rendered"]
    assert "Seven 喜欢直接反馈" in result["rendered"]
    assert "不要使用空泛套话" in result["rendered"]


def test_profile_reader_accepts_content_only_card(monkeypatch):
    body = {
        "items": [{"id": "m1", "content": "只有正文也必须进入画像输入"}],
        "user_card_count": 1,
        "truncated": False,
    }
    _wire_reader(monkeypatch, body)
    assert "只有正文也必须进入画像输入" in serve_worker._read_profile_cards(
        "u-content"
    )["rendered"]


@pytest.mark.parametrize(
    "body",
    [
        {"items": [], "user_card_count": 1, "truncated": True},
        {"items": [], "user_card_count": 1, "truncated": False},
    ],
)
def test_profile_reader_rejects_any_partial_garden(monkeypatch, body):
    _wire_reader(monkeypatch, body)
    with pytest.raises(RuntimeError, match=r"profile_cards_truncated:1/0"):
        serve_worker._read_profile_cards("u-truncated")


def test_profile_reader_empty_garden_is_complete(monkeypatch):
    body = {"items": [], "user_card_count": 0, "truncated": False}
    _wire_reader(monkeypatch, body)
    result = serve_worker._read_profile_cards("u-empty")
    assert result["eligible_card_count"] == 0
    assert result["rendered"] == ""


def test_profile_reader_accepts_full_default_hard_max(monkeypatch):
    items = [
        {"id": f"m{i}", "content": f"card {i}"}
        for i in range(1000)
    ]
    body = {"items": items, "user_card_count": 1000, "truncated": False}
    _wire_reader(monkeypatch, body)

    result = serve_worker._read_profile_cards("u-thousand")

    assert result["eligible_card_count"] == 1000
    assert "[card m0]" in result["rendered"]
    assert "[card m999]" in result["rendered"]


def test_profile_reader_authenticates_readside_with_runtime_token(monkeypatch):
    body = {"items": [], "user_card_count": 0, "truncated": False}
    observed = _wire_reader(monkeypatch, body, exercise_post=True)
    readside = {}

    def _post(api_key, candidates, *, operation, payload, runtime_token):
        readside.update(
            api_key=api_key,
            candidates=candidates,
            operation=operation,
            payload=payload,
            runtime_token=runtime_token,
        )
        return {"items": []}, 200

    monkeypatch.setattr(
        serve_worker.memory_readside_core,
        "post_enclave_readside",
        _post,
    )

    serve_worker._read_profile_cards("u-runtime-token")

    assert observed == {"api_key": None, "params": {"limit": 0}}
    assert readside["api_key"] is None
    assert readside["runtime_token"] == "runtime-token"
