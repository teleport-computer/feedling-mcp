from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from enclave import readside, visual  # noqa: E402


def test_raw_image_mime_signatures():
    assert visual.raw_image_mime(b"\xff\xd8\xff" + b"0" * 16) == "image/jpeg"
    assert visual.raw_image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
    assert visual.raw_image_mime(b"RIFF0000WEBP") == "image/webp"
    assert visual.raw_image_mime(b"not an image") is None


def test_parse_visual_plaintext_json_wrapper():
    inner = {"image": "abc", "ocr_text": "hi"}
    out = visual.parse_visual_plaintext(json.dumps(inner).encode())
    assert out["ocr_text"] == "hi"


def test_parse_visual_plaintext_raw_photo_fallback():
    jpeg = b"\xff\xd8\xff" + b"j" * 32
    out = visual.parse_visual_plaintext(jpeg)
    assert out["image_mime"] == "image/jpeg"
    assert base64.b64decode(out["image"]) == jpeg


def test_parse_visual_plaintext_garbage_fails_closed():
    with pytest.raises(Exception):
        visual.parse_visual_plaintext(b"\x00\x01 garbage not json not image")


def test_readside_effective_limit(monkeypatch):
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_LIMIT", raising=False)
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    assert readside.memory_readside_effective_limit() == 50
    assert readside.memory_readside_effective_limit(0) == 1000  # 0 = full window, hard cap
    assert readside.memory_readside_effective_limit(7) == 7
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "100")
    assert readside.memory_readside_effective_limit(0) == 100


def test_memory_inner_to_v1_passthrough_and_legacy():
    v1 = {"summary": "s", "content": "c", "bucket": "b", "threads": ["t"]}
    assert readside.memory_inner_to_v1(dict(v1))["bucket"] == "b"
    legacy = {"title": "旧标题", "description": "描述", "type": "moment"}
    adapted = readside.memory_inner_to_v1(legacy)
    assert adapted["bucket"] == "我们的关系"
    assert "描述" in adapted["content"]


def test_memory_index_filter_items():
    items = [
        {"bucket": "a", "threads": ["x"], "content": "Cat food notes"},
        {"bucket": "b", "threads": [], "content": "Quarterly tax notes"},
    ]
    assert len(readside.memory_index_filter_items(items, {"bucket": "a"})) == 1
    assert len(readside.memory_index_filter_items(items, {"thread": "x"})) == 1
    assert len(readside.memory_index_filter_items(items, {})) == 2
    assert readside.memory_index_filter_items(items, {"query": "CAT FOOD"}) == [items[0]]
    assert readside.memory_index_filter_items(items, {"query": "tax"}) == [items[1]]
    assert readside.memory_index_filter_items(items, {"query": "missing"}) == []


def test_memory_search_item_matches_private_content_without_changing_index_shape():
    envelope = {"id": "m1"}
    inner = {
        "summary": "ordinary summary",
        "content": "hidden blue-orchid-47 detail",
        "bucket": "notes",
        "threads": [],
    }
    public_item = readside.build_memory_index_item(envelope, inner)
    search_item = readside.build_memory_search_item(envelope, inner)

    assert "content" not in public_item
    assert "_search_content" not in public_item
    assert readside.memory_index_filter_items(
        [search_item], {"query": "BLUE-ORCHID-47"}
    ) == [search_item]


def test_decrypt_readside_items_skips_local_only(monkeypatch):
    # 不做真解密：patch decrypt_envelope，验证 local_only / 缺 K_enclave 分流。
    from enclave import envelope as envmod
    monkeypatch.setattr(
        envmod, "decrypt_envelope",
        lambda env, uid, sk: json.dumps({"summary": "s", "content": "c",
                                         "bucket": "b", "threads": []}).encode())
    moments = [
        {"id": "m1", "K_enclave": "x", "visibility": "shared"},
        {"id": "m2", "visibility": "local_only"},
        {"id": "m3"},  # 无 K_enclave
    ]
    items, unavailable = readside.decrypt_readside_items(
        moments, "usr_a", object(), item_builder=readside.build_memory_fetch_item)
    assert [i["id"] for i in items] == ["m1"]
    assert unavailable == ["m2", "m3"]
