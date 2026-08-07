"""Batch 2 tests: enclave history readside routes + backend 协调层 + store helpers.

覆盖 spec §8.7/§8.8/§8.9 的 enclave 侧：叶子命中/legacy_opaque 标记、scan 命中
+ snippet + last_checked_seq 语义（密集命中提前停）、deadline 逐行生效、
unavailable 计数、fetch 结构、预算 clamp、空 query 纯时间模式；外加
history_readside 协调层（fake enclave/monkeypatch store）与三个新增
jobs_store 密文行查询的 DB 侧（真 Postgres，走 conftest 供给的库——因此
本文件**不进** conftest 的 _PURE_UNIT 白名单）。

enclave 路由测试形态照 tests/test_enclave_routes_memory.py（_AsgiTestClient +
monkeypatch envelope.decrypt_envelope）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402
from enclave.routes import history as history_routes  # noqa: E402
from model_api_runtime.v2 import history_readside, history_search  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _authed(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def _patch_decrypt(monkeypatch, plaintext_by_id: dict):
    """按信封 id 查表解密；不在表里 = 解密失败（DecryptFailure）。"""
    def fake(env, user, sk):
        key = str(env.get("id"))
        if key not in plaintext_by_id:
            raise envmod.DecryptFailure(f"no plaintext for {key}")
        return plaintext_by_id[key]
    monkeypatch.setattr(envmod, "decrypt_envelope", fake)


def _text_row(seq: int, *, role: str = "user", **extra) -> dict:
    mid = extra.pop("mid", f"m{seq}")
    row = {
        "id": mid, "seq": seq, "ts": float(seq), "role": role,
        "content_type": "text", "v": 1, "owner_user_id": "usr_a",
        "body_ct": f"ct-{mid}", "nonce": "n", "K_enclave": "k",
    }
    row.update(extra)
    return row


def _leaf(segment_id: int, start: int, end: int, *, kind: str = "exact") -> dict:
    return {
        "segment_id": segment_id,
        "coverage_kind": kind,
        "start_seq": start,
        "end_seq": end,
        "legacy_opaque_through_seq": end if kind == "legacy_opaque" else 0,
        "summary_envelope": {
            "id": f"s{segment_id}", "v": 1, "body_ct": "ct", "nonce": "n",
            "K_enclave": "k", "owner_user_id": "usr_a",
        },
    }


# ---------------------------------------------------------------------------
# enclave 路由：/v1/history/leaf-hints
# ---------------------------------------------------------------------------


def test_history_routes_missing_key_space_spelling(client):
    r = client.post("/v1/history/scan", json={"rows": []})
    assert r.status_code == 401
    assert r.get_json() == {"error": "missing api_key"}  # 空格拼法


def test_scan_rows_must_be_list(client, _authed):
    r = client.post("/v1/history/scan", json={"rows": "nope"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "rows must be a list"}


def test_leaf_hints_query_required(client, _authed):
    r = client.post("/v1/history/leaf-hints", json={"leaves": []},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "query required"}


def test_leaf_hints_exact_ranges_and_legacy_marker_only(client, _authed, monkeypatch):
    _patch_decrypt(monkeypatch, {
        "s11": "我们聊过 Blue-Orchid 那家餐厅".encode(),
        "s12": b"legacy blob mentioning blue-orchid",
        "s13": b"nothing relevant",
    })
    leaves = [
        _leaf(11, 10, 20),
        _leaf(12, 0, 900, kind="legacy_opaque"),
        _leaf(13, 21, 30),
    ]
    r = client.post(
        "/v1/history/leaf-hints",
        json={"leaves": leaves, "query": "BLUE-orchid"},
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 200
    body = r.get_json()
    # exact 命中回精确段；legacy_opaque 命中只回标记、绝不回范围（spec §4）
    assert body["hits"] == [{"segment_id": 11, "start_seq": 10, "end_seq": 20}]
    assert body["legacy_opaque_hits"] == ["12"]
    assert body["checked_count"] == 3
    assert body["truncated"] is False


def test_leaf_hints_budget_truncates(client, _authed, monkeypatch):
    _patch_decrypt(monkeypatch, {"s1": b"match target", "s2": b"match target"})
    leaves = [_leaf(1, 1, 5), _leaf(2, 6, 9)]
    r = client.post(
        "/v1/history/leaf-hints",
        json={"leaves": leaves, "query": "target", "max_leaves": 1},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["checked_count"] == 1
    assert body["truncated"] is True
    assert body["hits"] == [{"segment_id": 1, "start_seq": 1, "end_seq": 5}]


def test_leaf_hints_undecryptable_counts_unavailable(client, _authed, monkeypatch):
    _patch_decrypt(monkeypatch, {})
    r = client.post(
        "/v1/history/leaf-hints",
        json={"leaves": [_leaf(1, 1, 5)], "query": "x"},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["unavailable_count"] == 1
    assert body["hits"] == []


# ---------------------------------------------------------------------------
# enclave 路由：/v1/history/scan
# ---------------------------------------------------------------------------


def test_scan_dense_hits_early_stop_pins_last_checked(client, _authed, monkeypatch):
    rows = [_text_row(seq) for seq in range(60, 54, -1)]  # 60..55 全命中
    _patch_decrypt(monkeypatch, {r["id"]: b"the target word" for r in rows})
    r = client.post(
        "/v1/history/scan",
        json={"rows": rows, "query": "TARGET", "stop_after_hits": 3},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert [h["seq"] for h in body["hits"]] == [60, 59, 58]
    # cursor 硬规则（spec §4）：提前停时停在最后实际检查的候选，不跳批末尾
    assert body["last_checked_seq"] == 58
    assert body["checked_count"] == 3
    assert body["stopped"] == "hits"
    assert "target" in body["hits"][0]["snippet"].lower()


def test_scan_deadline_checked_inside_row_loop(client, _authed, monkeypatch):
    rows = [_text_row(seq) for seq in (30, 29, 28)]
    _patch_decrypt(monkeypatch, {r["id"]: b"body text" for r in rows})
    # 时钟序列：deadline 基点 0.0 → 第 1 行检查时 0.0（放行）→ 第 2 行检查时
    # 99.0（超时停）。deadline 判定必须发生在逐行循环里（spec §5）。
    ticks = iter([0.0, 0.0, 99.0])
    monkeypatch.setattr(history_routes, "_monotonic", lambda: next(ticks, 99.0))
    r = client.post(
        "/v1/history/scan",
        json={"rows": rows, "query": "body", "deadline_ms": 1000},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["stopped"] == "deadline"
    assert body["checked_count"] == 1
    assert body["last_checked_seq"] == 30  # 如实返回实际停下的位置


def test_scan_unavailable_row_still_advances_last_checked(client, _authed, monkeypatch):
    unreadable = _text_row(10)
    unreadable.pop("K_enclave")
    readable = _text_row(9)
    _patch_decrypt(monkeypatch, {readable["id"]: b"nothing matching"})
    r = client.post(
        "/v1/history/scan",
        json={"rows": [unreadable, readable], "query": "zzz"},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["hits"] == []
    assert body["unavailable_count"] == 1
    assert body["checked_count"] == 2
    # 坏行也算"检查过"，否则 cursor 永远卡在它前面重扫
    assert body["last_checked_seq"] == 9


def test_scan_empty_query_time_mode_matches_all(client, _authed, monkeypatch):
    text = _text_row(5)
    image = {
        "id": "img1", "seq": 4, "ts": 4.0, "role": "user",
        "content_type": "image", "v": 1, "owner_user_id": "usr_a",
    }
    _patch_decrypt(monkeypatch, {text["id"]: b"plain words"})
    r = client.post(
        "/v1/history/scan",
        json={"rows": [text, image], "query": ""},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert [h["seq"] for h in body["hits"]] == [5, 4]
    # 无 caption 的附件行在时间模式下以标记兜底出现，不算 unavailable
    assert body["hits"][1]["snippet"] == "[image]"
    assert body["unavailable_count"] == 0
    assert body["stopped"] == "exhausted"


def test_scan_attachment_matches_caption_only_never_body(client, _authed, monkeypatch):
    image = {
        "id": "img1", "seq": 7, "ts": 7.0, "role": "user",
        "content_type": "image", "v": 1, "owner_user_id": "usr_a",
        # 即便调用方（bug）把 body 密文带进来，也绝不解它——caption-only
        "body_ct": "raw-image-bytes-ct", "nonce": "n", "K_enclave": "k",
        "caption_id": "cap1", "caption_v": 1, "caption_body_ct": "capct",
        "caption_nonce": "n", "caption_K_enclave": "k",
        "caption_owner_user_id": "usr_a",
    }
    seen: list[str] = []

    def fake(env, user, sk):
        seen.append(str(env.get("id")))
        assert env.get("id") == "cap1", "attachment body must never be decrypted"
        return "这张照片是 blue-orchid 餐厅".encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", fake)
    r = client.post(
        "/v1/history/scan",
        json={"rows": [image], "query": "blue-orchid"},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert seen == ["cap1"]
    assert len(body["hits"]) == 1
    assert "blue-orchid" in body["hits"][0]["snippet"]


def test_scan_row_budget_clamps_and_reports_truncated(client, _authed, monkeypatch):
    rows = [_text_row(seq) for seq in (3, 2, 1)]
    _patch_decrypt(monkeypatch, {r["id"]: b"x" for r in rows})
    r = client.post(
        "/v1/history/scan",
        json={"rows": rows, "query": "zzz", "max_rows": 2},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["checked_count"] == 2
    assert body["truncated"] is True
    assert body["stopped"] == "budget"
    assert body["last_checked_seq"] == 2


def test_scan_snippet_bounded_and_marks_truncation(client, _authed, monkeypatch):
    row = _text_row(1)
    long_text = ("前情提要。" * 100) + "关键词藏在这里" + ("后续内容。" * 100)
    _patch_decrypt(monkeypatch, {row["id"]: long_text.encode()})
    r = client.post(
        "/v1/history/scan",
        json={"rows": [row], "query": "关键词藏在这里"},
        headers={"X-API-Key": "k"},
    )
    hit = r.get_json()["hits"][0]
    assert len(hit["snippet"]) <= 240
    assert "关键词藏在这里" in hit["snippet"]
    assert hit["content_truncated"] is True


# ---------------------------------------------------------------------------
# enclave 路由：/v1/history/fetch
# ---------------------------------------------------------------------------


def test_fetch_structure_and_unavailable_count(client, _authed, monkeypatch):
    anchor = _text_row(5)
    older_bad = _text_row(3)
    older_bad.pop("K_enclave")
    older_ok = _text_row(4)
    newer_ok = _text_row(6, role="openclaw")
    _patch_decrypt(monkeypatch, {
        anchor["id"]: b"anchor body",
        older_ok["id"]: b"older body",
        newer_ok["id"]: b"newer body",
    })
    r = client.post(
        "/v1/history/fetch",
        json={"anchor": anchor, "before": [older_bad, older_ok],
              "after": [newer_ok]},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["anchor"]["message_id"] == "m5"
    assert body["anchor"]["content"] == "anchor body"
    assert body["anchor"]["content_truncated"] is False
    assert [i["message_id"] for i in body["before"]] == ["m3", "m4"]
    assert body["before"][0]["content"] is None
    assert body["before"][0]["unavailable"] is True
    assert body["after"][0]["content"] == "newer body"
    assert body["unavailable_count"] == 1


def test_fetch_clips_per_message_content(client, _authed, monkeypatch):
    anchor = _text_row(1)
    _patch_decrypt(monkeypatch, {anchor["id"]: ("长" * 500).encode()})
    r = client.post(
        "/v1/history/fetch",
        json={"anchor": anchor, "before": [], "after": [],
              "max_chars_per_message": 80},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert len(body["anchor"]["content"]) == 80
    assert body["anchor"]["content_truncated"] is True


def test_fetch_anchor_must_be_object(client, _authed):
    r = client.post("/v1/history/fetch", json={"anchor": "m1"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "anchor must be an object"}


# ---------------------------------------------------------------------------
# backend 协调层（fake enclave + monkeypatch store）
# ---------------------------------------------------------------------------

_KEY = b"unit-test-cursor-key-32-bytes!!!"


def _patch_store(monkeypatch, *, max_seq=100, generation=7, watermark=60,
                 leaves=None, candidate_fn=None):
    monkeypatch.setattr(history_readside.db, "chat_max_seq", lambda uid: max_seq)
    monkeypatch.setattr(
        history_readside.db, "get_runtime_generation", lambda uid: generation)
    monkeypatch.setattr(
        history_readside.jobs_store, "get_summary_frontier_state",
        lambda uid: {"watermark_seq": watermark})
    monkeypatch.setattr(
        history_readside.jobs_store, "list_level0_summary_leaves",
        lambda uid, *, through_seq: list(leaves or []))
    calls: list[tuple] = []

    def default_candidates(uid, *, min_seq, max_seq, start_ts=None, end_ts=None, limit):
        calls.append((min_seq, max_seq, limit))
        seqs = list(range(max_seq, min_seq - 1, -1))[:limit]
        return [
            {"seq": s, "msg_id": f"m{s}", "ts": float(s), "role": "user",
             "content_type": "text", "has_ciphertext": True}
            for s in seqs
        ]

    fn = candidate_fn or default_candidates
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_candidate_rows",
        lambda uid, **kw: fn(uid, **kw))
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_rows_by_seqs",
        lambda uid, seqs: [_text_row(int(s)) for s in sorted(seqs, reverse=True)])
    return calls


def test_run_history_search_pages_with_cursor_and_leaf_priority(monkeypatch):
    leaves = [_leaf(1, 30, 40)]
    calls = _patch_store(monkeypatch, leaves=leaves)
    posts: list[tuple] = []

    def fake_post(op, payload):
        posts.append((op, payload))
        if op == "leaf-hints":
            return {"hits": [{"segment_id": 1, "start_seq": 30, "end_seq": 40}],
                    "legacy_opaque_hits": [], "checked_count": 1,
                    "unavailable_count": 0, "truncated": False}
        rows = payload["rows"]
        if rows and rows[0]["seq"] == 100:
            # 未压缩区间第一行即命中，密集停：只检查了 1 行
            return {"hits": [{"message_id": rows[0]["id"], "seq": 100,
                              "ts": 100.0, "role": "openclaw",
                              "snippet": "…target…", "content_truncated": False}],
                    "checked_count": 1, "unavailable_count": 0,
                    "last_checked_seq": 100, "stopped": "hits",
                    "truncated": False}
        return {"hits": [], "checked_count": len(rows), "unavailable_count": 0,
                "last_checked_seq": rows[-1]["seq"] if rows else None,
                "stopped": "exhausted", "truncated": False}

    page1 = history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, query="target", limit=1,
        post_enclave=fake_post, now=1000.0)
    assert [m["message_id"] for m in page1["matches"]] == ["m100"]
    assert page1["matches"][0]["role"] == "assistant"  # openclaw → assistant
    assert "seq" not in page1["matches"][0]  # spec §3.1 的 matches 字段集
    assert page1["complete"] is False
    token = page1["next_cursor"]
    cur = history_search.decode_cursor(token, key=_KEY, now=1000.0)
    # ①只跑一批后让位给摘要命中段；resume 从快照顶开始，floor 记在 100
    assert cur.phase == history_search.PHASE_LEAF_HITS
    assert cur.uncompressed_floor == 100
    assert calls == [(61, 100, 128)]

    calls.clear()
    page2 = history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, cursor=token,
        post_enclave=fake_post, now=1001.0)
    assert page2["matches"] == []
    assert page2["complete"] is True
    assert "next_cursor" not in page2
    assert page2["coverage_gap"] is False
    # 命中段 (30,40) 先扫，然后 recent 兜底跳过已覆盖的 (30,40)+(100,100)
    assert calls == [(30, 40, 128), (41, 99, 128), (1, 29, 128)]


def test_run_history_search_time_only_skips_leaf_hints(monkeypatch):
    _patch_store(
        monkeypatch, leaves=[_leaf(1, 30, 40)],
        candidate_fn=lambda uid, **kw: [])
    posts: list[tuple] = []

    def fake_post(op, payload):
        posts.append((op, payload))
        return {"hits": [], "checked_count": 0, "unavailable_count": 0,
                "last_checked_seq": None, "stopped": "exhausted",
                "truncated": False}

    out = history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY,
        start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z",
        post_enclave=fake_post)
    # 纯时间模式跳过摘要提示（spec §4）：没有任何 leaf-hints 投递
    assert all(op != "leaf-hints" for op, _ in posts)
    assert out["matches"] == []
    assert out["complete"] is True


def test_run_history_search_requires_query_or_range(monkeypatch):
    _patch_store(monkeypatch)
    with pytest.raises(history_search.HistorySearchInputError):
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, post_enclave=lambda op, p: {})


def test_run_history_search_cross_user_cursor_rejected(monkeypatch):
    _patch_store(monkeypatch, candidate_fn=lambda uid, **kw: [])
    fake_post = lambda op, payload: {  # noqa: E731
        "hits": [], "checked_count": 0, "unavailable_count": 0,
        "last_checked_seq": None, "stopped": "exhausted", "truncated": False}
    cursor = history_search.encode_cursor(
        history_search.HistoryCursor(
            user_id="usr_other", snapshot_through_seq=100,
            summary_watermark_seq=60, runtime_generation=7, query="target",
            start_ts=None, end_ts=None,
            phase=history_search.PHASE_RECENT, resume_seq=50,
            uncompressed_floor=0, expires_at=2000.0),
        key=_KEY)
    with pytest.raises(history_search.CursorInvalid):
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, cursor=cursor,
            post_enclave=fake_post, now=1000.0)


def test_run_history_search_coverage_gap_demotes_complete(monkeypatch):
    legacy = _leaf(9, 0, 900, kind="legacy_opaque")
    _patch_store(
        monkeypatch, max_seq=50, watermark=50, leaves=[legacy],
        candidate_fn=lambda uid, **kw: [])  # raw 一条都不剩（retention 清理过）
    out = history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, start="2026-01-01T00:00:00Z",
        post_enclave=lambda op, p: {
            "hits": [], "checked_count": 0, "unavailable_count": 0,
            "last_checked_seq": None, "stopped": "exhausted", "truncated": False})
    # spec §3.1 三态之三：扫完但 legacy 覆盖区原文已不存在 → 不许声称 complete
    assert out["coverage_gap"] is True
    assert out["complete"] is False
    assert "next_cursor" not in out


def test_run_history_search_attachment_rows_project_caption_only(monkeypatch):
    image_row = {
        "id": "img1", "seq": 100, "ts": 100.0, "role": "user",
        "content_type": "image", "v": 1, "owner_user_id": "usr_a",
        "body_ct": "HUGE-IMAGE-CT", "nonce": "n", "K_enclave": "k",
        "caption_id": "cap1", "caption_v": 1, "caption_body_ct": "capct",
        "caption_nonce": "n", "caption_K_enclave": "ck",
        "caption_owner_user_id": "usr_a",
    }
    _patch_store(
        monkeypatch, leaves=[],
        candidate_fn=lambda uid, **kw: (
            [{"seq": 100, "msg_id": "img1", "ts": 100.0, "role": "user",
              "content_type": "image", "has_ciphertext": True}]
            if kw.get("limit", 0) > 1 else []))
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_rows_by_seqs",
        lambda uid, seqs: [image_row])
    sent_rows: list[dict] = []

    def fake_post(op, payload):
        sent_rows.extend(payload["rows"])
        return {"hits": [], "checked_count": len(payload["rows"]),
                "unavailable_count": 0, "last_checked_seq": 100,
                "stopped": "exhausted", "truncated": False}

    history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, start="2026-01-01T00:00:00Z",
        post_enclave=fake_post)
    assert len(sent_rows) >= 1
    projected = sent_rows[0]
    # caption-only 投影（spec §7）：附件 body 密文绝不出 backend
    assert "body_ct" not in projected
    assert projected["caption_body_ct"] == "capct"
    assert projected["caption_K_enclave"] == "ck"


def test_run_history_fetch_not_found_and_structure(monkeypatch):
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_anchor_row",
        lambda uid, mid: None)
    out = history_readside.run_history_fetch(
        "usr_a", message_id="ghost", post_enclave=lambda op, p: {})
    assert out == {"error": "not_found_or_not_visible"}

    anchor_row = _text_row(5)
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_anchor_row",
        lambda uid, mid: dict(anchor_row))
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_neighbor_rows",
        lambda uid, anchor_seq, *, before, after: (
            [_text_row(4, role="openclaw")], [_text_row(6)]))

    def fake_post(op, payload):
        assert op == "fetch"
        def item(row, content):
            return {"message_id": row["id"], "seq": row["seq"], "ts": row["ts"],
                    "role": row["role"], "content": content,
                    "content_truncated": False}
        return {
            "anchor": item(payload["anchor"], "anchor body"),
            "before": [item(payload["before"][0], "older body")],
            "after": [item(payload["after"][0], "newer body")],
            "unavailable_count": 0,
        }

    out = history_readside.run_history_fetch(
        "usr_a", message_id="m5", post_enclave=fake_post)
    assert out["anchor"]["message_id"] == "m5"
    assert "seq" not in out["anchor"]
    assert out["before"][0]["role"] == "assistant"  # openclaw → assistant
    assert out["after"][0]["content"] == "newer body"
    assert out["unavailable_count"] == 0


def test_post_enclave_history_version_skew_and_http_errors(monkeypatch):
    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "boom"

        def json(self):
            return {}

    class _Client:
        resp = None

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return _Client.resp

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(history_readside.httpx, "Client", _Client)
    # 版本错位（spec §6/§8.9）：enclave 还没带上 history 路由 → 明确报
    # capability-unavailable，绝不静默当空结果
    _Client.resp = _Resp(404)
    with pytest.raises(RuntimeError, match="enclave_history_capability_unavailable"):
        history_readside._post_enclave_history("tok", "scan", {})
    _Client.resp = _Resp(500)
    with pytest.raises(RuntimeError, match="enclave_http_500"):
        history_readside._post_enclave_history("tok", "scan", {})
    with pytest.raises(RuntimeError, match="runtime_token_unavailable"):
        history_readside._post_enclave_history(None, "scan", {})


def test_derive_cursor_hmac_key_domain_separated(monkeypatch):
    key = history_readside.derive_cursor_hmac_key(b"deploy-secret")
    assert len(key) == 32
    assert key != history_readside.derive_cursor_hmac_key(b"other-secret")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "")
    with pytest.raises(RuntimeError):
        history_readside.derive_cursor_hmac_key()


# ---------------------------------------------------------------------------
# jobs_store 密文行查询（真 DB）
# ---------------------------------------------------------------------------


def _db_modules():
    import conftest
    import db
    from core import store as core_store
    from model_api_runtime.v2 import jobs_store
    return conftest, db, core_store, jobs_store


def _db_reset(uid: str):
    conftest, db, _, _ = _db_modules()
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def _db_append(uid: str, index: int, *, ts: float, role: str = "user", **doc_extra) -> int:
    _, db, core_store, _ = _db_modules()
    message_id = f"{uid}-m{index}"
    doc = {"id": message_id, "role": role, "body_ct": f"ct-{index}",
           "nonce": "n", "K_enclave": "k", "owner_user_id": uid}
    doc.update(doc_extra)
    db.chat_append_strict(uid, message_id, ts, doc, core_store.MAX_CHAT_MESSAGES)
    return int(db.chat_seq_for_msg_id(uid, message_id))


def test_store_rows_by_seqs_full_ciphertext_and_visibility():
    _, _, _, jobs_store = _db_modules()
    uid = "u_hist_rows_by_seq"
    _db_reset(uid)
    s0 = _db_append(uid, 0, ts=1.0)
    s1 = _db_append(uid, 1, ts=2.0, role="openclaw")
    synthetic = _db_append(uid, 2, ts=3.0, source="verify_ping")

    rows = jobs_store.chat_history_rows_by_seqs(uid, [s0, s1, synthetic])
    # 可见性谓词再套一遍：合成行即便被点名也取不回
    assert [row["seq"] for row in rows] == [s1, s0]
    assert rows[0]["id"] == f"{uid}-m1"
    assert rows[0]["body_ct"] == "ct-1"  # 完整密文列在场（供 enclave 解密）
    assert rows[0]["K_enclave"] == "k"
    assert jobs_store.chat_history_rows_by_seqs(uid, []) == []


def test_store_anchor_and_neighbors_visibility_and_order():
    _, _, _, jobs_store = _db_modules()
    uid = "u_hist_anchor"
    _db_reset(uid)
    seqs = [_db_append(uid, i, ts=float(i)) for i in range(3)]
    _db_append(uid, 3, ts=3.0, role="system")  # 不可见，邻居必须跳过
    s4 = _db_append(uid, 4, ts=4.0, role="agent")

    anchor = jobs_store.chat_history_anchor_row(uid, f"{uid}-m1")
    assert anchor is not None and anchor["seq"] == seqs[1]
    assert anchor["body_ct"] == "ct-1"
    # 不存在与不可见统一 None（not_found_or_not_visible 不区分）
    assert jobs_store.chat_history_anchor_row(uid, "ghost") is None
    assert jobs_store.chat_history_anchor_row(uid, f"{uid}-m3") is None

    older, newer = jobs_store.chat_history_neighbor_rows(
        uid, seqs[1], before=2, after=2)
    assert [row["seq"] for row in older] == [seqs[0]]  # 旧→新
    assert [row["seq"] for row in newer] == [seqs[2], s4]  # system 行被跳过
