import asyncio
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile, serve_worker


def _wire(monkeypatch, *, index_body, fetched_items=(), fetch_calls=None):
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
        if fetch_calls is not None:
            fetch_calls.append(tuple(payload["ids"]))
        post_enclave(None, [], operation="fetch", payload={})
        by_id = {str(item.get("id") or ""): item for item in fetched_items}
        return {
            "items": [by_id[memory_id] for memory_id in payload["ids"] if memory_id in by_id]
        }, 200

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

    assert serve_worker._read_profile_cards("u") == (
        "",
        0,
        {"lane": "profile", "profile_cards_truncated": False},
    )
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

    rendered, count, tail_window = serve_worker._read_profile_cards("u")

    assert count == 1
    assert "summary=一句总结" in rendered
    assert "content=完整正文" in rendered
    assert "bucket=我们的关系" in rendered
    assert "occurred_at=2026-07-01T00:00:00Z" in rendered
    assert tail_window == {
        "lane": "profile",
        "profile_cards_truncated": False,
    }
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

    rendered, count, tail_window = serve_worker._read_profile_cards("u")

    assert count == 1000
    assert len(rendered.splitlines()) == 1000
    assert "content=正文999" in rendered
    assert tail_window["profile_cards_truncated"] is False


def test_profile_cards_554_are_read_in_nine_stable_batches(monkeypatch):
    items = [{"id": f"m{i}"} for i in range(554)]
    fetched = [
        {"id": f"m{i}", "content": f"正文{i}"}
        for i in reversed(range(554))
    ]
    fetch_calls = []
    progress = []
    _wire(
        monkeypatch,
        index_body={
            "items": items,
            "user_card_count": 554,
            "truncated": False,
        },
        fetched_items=fetched,
        fetch_calls=fetch_calls,
    )

    rendered, count, tail_window = serve_worker._read_profile_cards(
        "u", progress.append
    )

    assert count == 554
    assert [len(batch) for batch in fetch_calls] == [64] * 8 + [42]
    assert rendered.splitlines()[0].startswith("- id=m0 ")
    assert rendered.splitlines()[-1].startswith("- id=m553 ")
    assert tail_window == {
        "lane": "profile",
        "profile_cards_truncated": False,
    }
    assert progress == [
        "profile_index_completed",
        *[
            f"profile_fetch_batch_completed:{index}:9:{min(index * 64, 554)}:554"
            for index in range(1, 10)
        ],
    ]


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


def test_profile_card_content_has_no_second_per_card_bound():
    content = "中" * (
        profile.PROFILE_MEMORY_MAX_CHARS + profile.PROFILE_STYLE_MAX_CHARS
    )
    rendered = serve_worker._render_profile_card(
        {"id": "m", "content": content}
    )
    assert rendered.endswith(f"content={content}")


def test_profile_card_full_content_reaches_provider_request_trace(monkeypatch):
    tail_sentinel = "T062_PROFILE_CARD_TAIL_REACHES_PROVIDER"
    full_body = (
        "Q" * (profile.PROFILE_MEMORY_MAX_CHARS + profile.PROFILE_STYLE_MAX_CHARS)
        + tail_sentinel
    )
    _wire(
        monkeypatch,
        index_body={
            "items": [{"id": "m1"}],
            "user_card_count": 1,
            "truncated": False,
        },
        fetched_items=[{"id": "m1", "content": full_body}],
    )
    rendered, count, tail_window = serve_worker._read_profile_cards("u")
    provider_messages = []
    events = []

    async def _llm(_config, messages, **_kwargs):
        provider_messages.append(messages)
        return {"reply": '{"memory":"事实","style":"方式"}'}

    async def _trajectory(kind, payload):
        events.append((kind, payload))

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards=rendered,
            llm=_llm,
            trajectory_out=_trajectory,
            tail_window=tail_window,
        )
    )

    assert count == 1
    assert result.fields == {"memory": "事实", "style": "方式"}
    provider_payload = json.dumps(provider_messages, ensure_ascii=False)
    assert full_body in provider_payload
    assert tail_sentinel in provider_payload
    request = next(payload for kind, payload in events if kind == "provider_request")
    assert request == {
        "tail_window": {
            "lane": "profile",
            "profile_cards_truncated": False,
        }
    }
    assert "Q" not in json.dumps(request, ensure_ascii=False)
