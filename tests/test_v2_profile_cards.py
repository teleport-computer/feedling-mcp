import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def _wire(monkeypatch, *, index_body, fetched_items=()):
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: object())
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt-profile")
    auth = []

    def _post(_api_key, _candidates, *, operation, payload=None, runtime_token=None):
        auth.append((operation, runtime_token))
        return {"items": []}

    monkeypatch.setattr(
        serve_worker.memory_readside_core,
        "post_enclave_readside",
        _post,
    )

    def _index(_store, api_key, payload, *, post_enclave):
        assert api_key is None
        assert payload == {"limit": 0, "include_sensitive": True}
        post_enclave(None, [], operation="index", payload={})
        return index_body, 200

    def _fetch(_store, api_key, payload, *, post_enclave):
        assert api_key is None
        post_enclave(None, [], operation="fetch", payload={})
        return {"items": list(fetched_items)}, 200

    monkeypatch.setattr(serve_worker.memory_core, "index", _index)
    monkeypatch.setattr(serve_worker.memory_core, "fetch", _fetch)
    return auth


def test_profile_cards_zero_is_complete_without_fetch(monkeypatch):
    auth = _wire(
        monkeypatch,
        index_body={"items": [], "user_card_count": 0, "truncated": False},
    )
    monkeypatch.setattr(
        serve_worker.memory_core,
        "fetch",
        lambda *_args, **_kwargs: pytest.fail("zero cards must not fetch"),
    )

    assert serve_worker._read_profile_cards("u") == ("", 0)
    assert auth == [("index", "rt-profile")]


def test_profile_cards_include_summary_content_bucket_and_time(monkeypatch):
    item = {"id": "m1"}
    fetched = {
        "id": "m1",
        "summary": "一句总结",
        "content": "完整正文",
        "bucket": "我们的关系",
        "occurred_at": "2026-07-01T00:00:00Z",
    }
    auth = _wire(
        monkeypatch,
        index_body={"items": [item], "user_card_count": 1, "truncated": False},
        fetched_items=[fetched],
    )

    rendered, count = serve_worker._read_profile_cards("u")

    assert count == 1
    assert "summary=一句总结" in rendered
    assert "content=完整正文" in rendered
    assert "bucket=我们的关系" in rendered
    assert "occurred_at=2026-07-01T00:00:00Z" in rendered
    assert auth == [("index", "rt-profile"), ("fetch", "rt-profile")]


def test_profile_cards_content_only_fallback_and_1000_cards(monkeypatch):
    items = [{"id": f"m{i}"} for i in range(1000)]
    fetched = [{"id": f"m{i}", "content": f"正文{i}"} for i in range(1000)]
    _wire(
        monkeypatch,
        index_body={
            "items": items,
            "user_card_count": 1000,
            "truncated": False,
        },
        fetched_items=fetched,
    )

    rendered, count = serve_worker._read_profile_cards("u")

    assert count == 1000
    assert len(rendered.splitlines()) == 1000
    assert "content=正文999" in rendered


@pytest.mark.parametrize(
    "body",
    [
        {"items": [{"id": "m1"}], "user_card_count": 2, "truncated": True},
        {"items": [{"id": "m1"}], "user_card_count": 2, "truncated": False},
    ],
)
def test_profile_cards_truncation_fails_before_generation(monkeypatch, body):
    _wire(monkeypatch, index_body=body)

    with pytest.raises(RuntimeError, match=r"^profile_cards_truncated:2/1$"):
        serve_worker._read_profile_cards("u")


def test_profile_card_content_has_explicit_per_card_bound():
    rendered = serve_worker._render_profile_card(
        {"id": "m", "content": "中" * 3000}
    )
    assert rendered.count("中") == serve_worker._PROFILE_CARD_CONTENT_MAX_CHARS
