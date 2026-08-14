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

import random
import string
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


def test_scan_first_row_over_byte_budget_is_never_decrypted(client, _authed, monkeypatch):
    """首条就超字节闸的行：绝不解密（可能是 MB 级密文），按 checked +
    unavailable 占位推进 last_checked_seq——不能因为"放不下"就永远卡在它前面。"""
    huge = _text_row(9, body_ct="C" * 5000)
    decrypted: list[str] = []

    def fake(env, user, sk):
        decrypted.append(str(env.get("id")))
        return b"target"
    monkeypatch.setattr(envmod, "decrypt_envelope", fake)

    r = client.post(
        "/v1/history/scan",
        json={"rows": [huge, _text_row(8)], "query": "target",
              "max_ciphertext_bytes": 1024},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert decrypted == []                    # 一次解密都没发生
    assert body["checked_count"] == 1
    assert body["unavailable_count"] == 1
    assert body["hits"] == []
    assert body["last_checked_seq"] == 9      # cursor 能越过它
    assert body["truncated"] is True


def test_scan_row_crossing_byte_budget_is_left_for_the_next_page(
        client, _authed, monkeypatch):
    """后续行跨界：加进批之前就拦下，绝不先超支再停。"""
    rows = [_text_row(3, body_ct="A" * 600), _text_row(2, body_ct="B" * 600),
            _text_row(1)]
    _patch_decrypt(monkeypatch, {r["id"]: b"target" for r in rows})
    r = client.post(
        "/v1/history/scan",
        json={"rows": rows, "query": "target", "max_ciphertext_bytes": 1024},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    # 600 进得去，600+600=1200 > 1024 → 第二行留给下一页
    assert body["checked_count"] == 1
    assert [h["seq"] for h in body["hits"]] == [3]
    assert body["last_checked_seq"] == 3
    assert body["truncated"] is True and body["stopped"] == "budget"


def test_leaf_hints_leaf_crossing_byte_budget_is_not_decrypted(
        client, _authed, monkeypatch):
    """叶子提示同一条规则：跨界的叶子加进批之前拦下，超大单叶不解密。"""
    small = _leaf(1, 1, 5)
    small["summary_envelope"]["body_ct"] = "a" * 600
    big = _leaf(2, 6, 9)
    big["summary_envelope"]["body_ct"] = "b" * 600
    decrypted: list[str] = []

    def fake(env, user, sk):
        decrypted.append(str(env.get("id")))
        return b"match target"
    monkeypatch.setattr(envmod, "decrypt_envelope", fake)

    r = client.post(
        "/v1/history/leaf-hints",
        json={"leaves": [small, big], "query": "target",
              "max_ciphertext_bytes": 1024},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert decrypted == ["s1"]
    assert body["checked_count"] == 1
    assert body["truncated"] is True
    assert body["hits"] == [{"segment_id": 1, "start_seq": 1, "end_seq": 5}]


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


def test_fetch_hard_cap_fits_the_full_31_row_window(client, _authed, monkeypatch):
    """spec §3.2 请求上限 31 条（1 + 15 + 15）。enclave hard cap 装不下就会
    把窗口静默砍回去——所有上游都放宽了、结果还是老样子。"""
    assert history_routes._FETCH_ROWS_HARD_MAX >= 31
    anchor = _text_row(100)
    before = [_text_row(seq) for seq in range(85, 100)]   # 15 条，旧→新
    after = [_text_row(seq) for seq in range(101, 116)]   # 15 条，旧→新
    _patch_decrypt(
        monkeypatch, {r["id"]: b"hi" for r in [anchor] + before + after})
    r = client.post(
        "/v1/history/fetch",
        json={"anchor": anchor, "before": before, "after": after},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert len(body["before"]) == 15
    assert len(body["after"]) == 15
    assert body["anchor"]["message_id"] == "m100"


def test_fetch_deadline_checked_inside_the_neighbor_loop(
    client, _authed, monkeypatch
):
    """fetch 也必须逐行看 deadline（spec §5），不只 scan。

    没有超时保护的解密循环 = enclave 可以被一次调用长时间占住；外层
    ``anyio.to_thread`` 的取消停不掉一个正在同步解密的线程，唯一可靠的止损点
    就在这个循环里——和 /scan 同一套机制、同一个可注入时钟。
    """
    anchor = _text_row(100)
    before = [_text_row(seq) for seq in range(85, 100)]   # 15 条，旧→新
    after = [_text_row(seq) for seq in range(101, 116)]   # 15 条，旧→新
    _patch_decrypt(
        monkeypatch, {r["id"]: b"hi" for r in [anchor] + before + after})
    # 时钟序列：deadline 基点 0.0 → 头两个邻居放行 → 之后一律超时。
    ticks = iter([0.0, 0.0, 0.0])
    monkeypatch.setattr(history_routes, "_monotonic", lambda: next(ticks, 99.0))
    r = client.post(
        "/v1/history/fetch",
        json={"anchor": anchor, "before": before, "after": after,
              "deadline_ms": 1000},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["stopped"] == "deadline"
    # 锚点永远解密（1 行的下界，同 /scan 的"首行永远放行"）
    assert body["anchor"]["message_id"] == "m100"
    # 由近及远处理：停下时保住的是贴身邻居，不是最远的
    assert [i["message_id"] for i in body["before"]] == ["m99"]
    assert [i["message_id"] for i in body["after"]] == ["m101"]
    # 没来得及解密的如实上报（不是 unavailable——它们压根没被碰过）
    assert body["omitted_before"] == 14
    assert body["omitted_after"] == 14
    assert body["unavailable_count"] == 0


def test_fetch_reports_zero_omissions_when_the_whole_window_fits(
    client, _authed, monkeypatch
):
    anchor = _text_row(5)
    _patch_decrypt(monkeypatch, {anchor["id"]: b"hi"})
    r = client.post(
        "/v1/history/fetch",
        json={"anchor": anchor, "before": [], "after": []},
        headers={"X-API-Key": "k"},
    )
    body = r.get_json()
    assert body["stopped"] == "complete"
    assert body["omitted_before"] == 0 and body["omitted_after"] == 0


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


def test_run_history_search_pages_raw_snapshot_newest_to_oldest(monkeypatch):
    calls = _patch_store(monkeypatch)
    posts: list[tuple] = []

    def fake_post(op, payload):
        posts.append((op, payload))
        assert op == "scan"
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
    assert cur.snapshot_through_seq == 100
    assert cur.resume_seq == 99
    assert calls == [(1, 100, 128)]

    calls.clear()
    page2 = history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, cursor=token,
        post_enclave=fake_post, now=1001.0)
    assert page2["matches"] == []
    assert page2["complete"] is True
    assert "next_cursor" not in page2
    assert calls == [(1, 99, 128)]


def test_run_history_search_time_only_uses_raw_scan(monkeypatch):
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
    assert all(op == "scan" for op, _ in posts)
    assert out["matches"] == []
    assert out["complete"] is True


def test_run_history_search_requires_query_or_range(monkeypatch):
    _patch_store(monkeypatch)
    with pytest.raises(history_search.HistorySearchInputError):
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, post_enclave=lambda op, p: {})


def test_cursor_overflow_is_raised_only_after_the_scan_already_ran(monkeypatch):
    """``cursor_overflow`` 是**扫描后**的编码失败，不是入参校验失败。

    它由 ``encode_cursor`` 在扫描循环结束后抛出（history_search.encode_cursor
    的长度兜底）。这条测试钉死那个时序：enclave 已经解过行，错误才发生——
    所以回合预算绝不能把它当"一行都没读过"豁免掉（见 worker 侧的同名回归）。
    """
    _patch_store(monkeypatch)
    posts: list[str] = []

    def fake_post(op, payload):
        posts.append(op)
        rows = payload["rows"]
        return {
            "hits": [{"message_id": rows[0]["id"], "seq": rows[0]["seq"],
                      "ts": 1.0, "role": "user", "snippet": "x",
                      "content_truncated": False}],
            "checked_count": 1, "unavailable_count": 0,
            "last_checked_seq": rows[0]["seq"], "stopped": "hits",
            "truncated": False,
        }

    # 超长且不可压缩的 user_id 把 cursor payload 顶过 CURSOR_MAX_CHARS
    # （Codex 的复现形态：user_id 是 TEXT 列，长度不由这层保证）。
    long_uid = "".join(
        random.Random(7).choices(string.ascii_letters + string.digits, k=2000))
    with pytest.raises(history_search.HistorySearchInputError) as excinfo:
        history_readside.run_history_search(
            long_uid, cursor_hmac_key=_KEY, query="target", limit=1,
            post_enclave=fake_post)
    assert excinfo.value.code == "cursor_overflow"
    assert posts.count("scan") >= 1  # 扫描确实跑过了


def test_route_missing_after_a_successful_scan_is_no_longer_pre_scan(monkeypatch):
    """已经扫过之后再出 enclave 404 → 不许再报 route-missing 那个词面。

    ``enclave_history_capability_unavailable`` 会被 facade 翻成
    ``capability_unavailable``，而那是 PRE_SCAN_ERROR_CODES 里的豁免码（结算
    0 行）。第一次投递就 404（真正的版本错位）保持原样；跑过之后再 404 必须
    降级成普通 upstream 错误，才能按 lease 满额扣。
    """
    _patch_store(monkeypatch, max_seq=200)
    posts: list[str] = []

    def fake_post(op, payload):
        posts.append(op)
        if posts.count("scan") > 1:
            raise RuntimeError("enclave_history_capability_unavailable")
        rows = payload["rows"]
        return {"hits": [], "checked_count": len(rows), "unavailable_count": 0,
                "last_checked_seq": rows[-1]["seq"] if rows else None,
                "stopped": "exhausted", "truncated": False}

    with pytest.raises(RuntimeError) as excinfo:
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, query="target",
            post_enclave=fake_post)
    assert posts.count("scan") == 2
    assert str(excinfo.value) == "enclave_error_after_scan"

    # 第一次投递就 404 = 真的版本错位，词面保持不变（那时确实一行都没读过）。
    posts.clear()

    def always_404(op, payload):
        posts.append(op)
        raise RuntimeError("enclave_history_capability_unavailable")

    with pytest.raises(RuntimeError) as excinfo:
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, query="target",
            post_enclave=always_404)
    assert str(excinfo.value) == "enclave_history_capability_unavailable"


def test_run_history_search_cross_user_cursor_rejected(monkeypatch):
    _patch_store(monkeypatch, candidate_fn=lambda uid, **kw: [])
    fake_post = lambda op, payload: {  # noqa: E731
        "hits": [], "checked_count": 0, "unavailable_count": 0,
        "last_checked_seq": None, "stopped": "exhausted", "truncated": False}
    cursor = history_search.encode_cursor(
        history_search.HistoryCursor(
            user_id="usr_other", snapshot_through_seq=100,
            runtime_generation=7, query="target",
            start_ts=None, end_ts=None,
            resume_seq=50, expires_at=2000.0),
        key=_KEY)
    with pytest.raises(history_search.CursorInvalid):
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, cursor=cursor,
            post_enclave=fake_post, now=1000.0)


def test_run_history_search_cursor_with_explicit_limit_rejected(monkeypatch):
    """续页只传 cursor：同时带 limit 会改页大小，按 cursor_mismatch 拒。"""
    _patch_store(monkeypatch, candidate_fn=lambda uid, **kw: [])
    fake_post = lambda op, payload: {  # noqa: E731
        "hits": [], "checked_count": 0, "unavailable_count": 0,
        "last_checked_seq": None, "stopped": "exhausted", "truncated": False}
    cursor = history_search.encode_cursor(
        history_search.HistoryCursor(
            user_id="usr_a", snapshot_through_seq=100,
            runtime_generation=7, query="target",
            start_ts=None, end_ts=None,
            resume_seq=50, expires_at=2000.0),
        key=_KEY)
    with pytest.raises(history_search.CursorMismatch):
        history_readside.run_history_search(
            "usr_a", cursor_hmac_key=_KEY, cursor=cursor, limit=5,
            post_enclave=fake_post, now=1000.0)
    # 只传 cursor 仍然放行（limit 省略 = None，走 cursor 页大小）
    out = history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, cursor=cursor,
        post_enclave=fake_post, now=1000.0)
    assert out["complete"] is True


def test_readside_row_byte_budget_checked_before_the_row_joins_the_batch(monkeypatch):
    """backend 侧字节闸同样是"加进批之前检查"。

    先加后检查时，首条 5 MiB 的密文已经躺在 payload 里飞向 enclave —— 512 KiB
    的闸等于没有。跨界行留给下一页；单行自身即超限时只送不含密文的占位（行仍
    然算检查过，cursor 才推得动）。
    """
    _patch_store(monkeypatch, max_seq=3, watermark=3, leaves=[])
    rows = {3: "A" * 600, 2: "B" * 600, 1: "C" * 20}
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_rows_by_seqs",
        lambda uid, seqs: [_text_row(int(s), body_ct=rows[int(s)])
                           for s in sorted(seqs, reverse=True)])
    sent: list[list[dict]] = []

    def fake_post(op, payload):
        sent.append(payload["rows"])
        return {"hits": [], "checked_count": len(payload["rows"]),
                "unavailable_count": 0,
                "last_checked_seq": payload["rows"][-1]["seq"],
                "stopped": "budget", "truncated": True}

    history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, start="2026-01-01T00:00:00Z",
        budget=history_readside.HistorySearchBudget(raw_batch_bytes=1000),
        post_enclave=fake_post)
    # 第一批只装得下 seq=3（600），seq=2 会把总量顶到 1200 > 1000 → 留下一页
    assert [r["seq"] for r in sent[0]] == [3]


def test_readside_single_row_over_byte_budget_sent_as_placeholder(monkeypatch):
    """单行自身即超限：不送密文，但必须作为占位送出去让 cursor 越过它。"""
    _patch_store(monkeypatch, max_seq=2, watermark=2, leaves=[])
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_rows_by_seqs",
        lambda uid, seqs: [_text_row(int(s), body_ct="X" * 5000)
                           for s in sorted(seqs, reverse=True)])
    sent: list[list[dict]] = []

    def fake_post(op, payload):
        sent.append(payload["rows"])
        return {"hits": [], "checked_count": len(payload["rows"]),
                "unavailable_count": len(payload["rows"]),
                "last_checked_seq": payload["rows"][-1]["seq"],
                "stopped": "budget", "truncated": True}

    history_readside.run_history_search(
        "usr_a", cursor_hmac_key=_KEY, start="2026-01-01T00:00:00Z",
        budget=history_readside.HistorySearchBudget(raw_batch_bytes=1024),
        post_enclave=fake_post)
    first = sent[0]
    assert [r["seq"] for r in first] == [2]
    assert "body_ct" not in first[0] and "K_enclave" not in first[0]
    assert first[0]["oversize"] is True


def _exhausted_post(op, payload):
    rows = payload.get("rows") or []
    return {"hits": [], "checked_count": len(rows), "unavailable_count": 0,
            "last_checked_seq": rows[-1]["seq"] if rows else None,
            "stopped": "exhausted", "truncated": False}


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
        lambda uid, anchor_seq, *, before, after, **kw: (
            [_text_row(4, role="openclaw")], [_text_row(6)],
            {"before": 1, "after": 1}))

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
    # 窗口完整装下时不报删减（模型必须能区分"就这么多"和"删过"）
    assert out["omitted_before"] == 0 and out["omitted_after"] == 0
    assert out["after"][0]["content"] == "newer body"
    assert out["unavailable_count"] == 0


def _patch_fetch_store(monkeypatch, *, anchor_seq=50,
                       exists_before=999, exists_after=999):
    """假 store。``exists_*`` = 该侧真正存在多少条邻居（默认"历史很长"）。

    真 store 现在同时给出**请求窗口内实际可见的条数**（witness），omitted_* 只
    能由它算出来——这里如实模拟那个契约，别再让假 store 谎报"要多少有多少"。
    """
    anchor_row = _text_row(anchor_seq)
    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_anchor_row",
        lambda uid, mid: dict(anchor_row))
    asked: dict = {}

    def _neighbors(uid, seq, *, before, after,
                   before_requested=None, after_requested=None):
        asked["before"], asked["after"] = before, after
        took_before = min(before, exists_before)
        took_after = min(after, exists_after)
        return (
            [_text_row(s) for s in range(seq - took_before, seq)],
            [_text_row(s) for s in range(seq + 1, seq + 1 + took_after)],
            {
                "before": min(
                    before if before_requested is None else before_requested,
                    exists_before),
                "after": min(
                    after if after_requested is None else after_requested,
                    exists_after),
            },
        )

    monkeypatch.setattr(
        history_readside.jobs_store, "chat_history_neighbor_rows", _neighbors)
    return asked


def _echo_fetch_post(sent: dict, *, omitted_before=0, omitted_after=0):
    def _post(op, payload):
        assert op == "fetch"
        sent["deadline_ms"] = payload.get("deadline_ms")
        sent["before"] = len(payload["before"])
        sent["after"] = len(payload["after"])
        return {
            "anchor": {"message_id": payload["anchor"]["id"], "seq": 0,
                       "ts": 1.0, "role": "user", "content": "a"},
            "before": [], "after": [], "unavailable_count": 0,
            "omitted_before": omitted_before, "omitted_after": omitted_after,
        }
    return _post


def test_run_history_fetch_spends_the_lease_rows_and_deadline(monkeypatch):
    """回合租约必须真的作用到 fetch（spec §5 的「两工具合计」）。

    以前 fetch 只把 lease 收进 budget 就丢掉了：不转发 deadline、按原始
    before/after 拉满行。结果是回合闸只约束 search，fetch 想扫多少扫多少。
    """
    asked = _patch_fetch_store(monkeypatch)
    sent: dict = {}
    out = history_readside.run_history_fetch(
        "usr_a", message_id="m50", before=15, after=4,
        budget=history_readside.HistorySearchBudget(
            call_max_rows=1, call_deadline_ms=1),
        post_enclave=_echo_fetch_post(sent))
    # deadline 必须转发到 enclave，否则那一侧根本无从止损
    assert sent["deadline_ms"] == 1
    # 行数也钳：锚点单独占 1 行，邻居保底 2（优先 before——线索在前文里）
    assert asked == {"before": 2, "after": 0}
    assert sent["before"] == 2 and sent["after"] == 0
    # 钳掉的如实告诉模型
    assert out["omitted_before"] == 13
    assert out["omitted_after"] == 4


def test_run_history_fetch_full_lease_keeps_the_whole_window(monkeypatch):
    """额度充足时窗口一行不少（钳制只在剩余额度真的不够时生效）。"""
    asked = _patch_fetch_store(monkeypatch)
    sent: dict = {}
    out = history_readside.run_history_fetch(
        "usr_a", message_id="m50", before=15, after=4,
        post_enclave=_echo_fetch_post(sent))
    assert asked == {"before": 15, "after": 4}
    assert sent["deadline_ms"] == 2500
    assert out["omitted_before"] == 0 and out["omitted_after"] == 0


def test_run_history_fetch_merges_enclave_side_omissions(monkeypatch):
    """enclave 因 deadline / hard cap 少解的行，和预算钳掉的行合并上报。"""
    _patch_fetch_store(monkeypatch)
    sent: dict = {}
    out = history_readside.run_history_fetch(
        "usr_a", message_id="m50", before=15, after=4,
        budget=history_readside.HistorySearchBudget(call_max_rows=6),
        post_enclave=_echo_fetch_post(sent, omitted_before=3, omitted_after=1))
    # 预算钳：邻居额度 5 → before 5 / after 0；enclave 又少解 3 条 before
    assert sent["before"] == 5 and sent["after"] == 0
    assert out["omitted_before"] == (15 - 5) + 3
    assert out["omitted_after"] == 4 + 1


def test_run_history_fetch_does_not_invent_neighbors_that_never_existed(monkeypatch):
    """omitted_* 只能来自 store 的真实 witness，不许从"没取满"推断。

    锚点贴着历史两头时，请求窗口里本来就没那么多行；报"删了 13 条 / 4 条"会让
    模型以为还有前后文可挖，白翻一页。**一条都没查过的那一侧也一样**——
    ``keep_after=0`` 时旧写法恒报满 4 条，哪怕锚点就是最后一条消息。
    """
    _patch_fetch_store(monkeypatch, anchor_seq=2, exists_before=1, exists_after=0)
    sent: dict = {}
    out = history_readside.run_history_fetch(
        "usr_a", message_id="m2", before=15, after=4,
        budget=history_readside.HistorySearchBudget(call_max_rows=1),
        post_enclave=_echo_fetch_post(sent))
    assert sent["before"] == 1 and sent["after"] == 0
    assert out["omitted_before"] == 0     # 到头了，没藏东西
    assert out["omitted_after"] == 0      # 锚点后面根本没有消息


def test_run_history_fetch_reports_the_side_it_never_asked_about(monkeypatch):
    """反过来：after 侧确实有消息、只是预算没给额度 → 必须如实报满。

    与上一条成对——这两条一起才钉死"报的是真实缺口"，不是"永远 0"或"永远满"。
    """
    _patch_fetch_store(monkeypatch, exists_before=999, exists_after=999)
    sent: dict = {}
    out = history_readside.run_history_fetch(
        "usr_a", message_id="m50", before=15, after=4,
        budget=history_readside.HistorySearchBudget(call_max_rows=1),
        post_enclave=_echo_fetch_post(sent))
    assert sent["before"] == 2 and sent["after"] == 0
    assert out["omitted_before"] == 13
    assert out["omitted_after"] == 4


def test_run_history_fetch_full_window_that_is_all_there_is_reports_zero(monkeypatch):
    """``returned == kept`` 不等于"后面还有"：正好取完时不许报缺口。

    预算充足、请求 4 条 after 而历史上就剩 4 条 → omitted_after 必须是 0。
    旧写法在 ``returned == kept`` 时一律取保守的"还有更多"，于是钳到 0 的那一侧
    恒报满、正好取完的那一侧也可能虚报。
    """
    _patch_fetch_store(monkeypatch, exists_before=15, exists_after=4)
    sent: dict = {}
    out = history_readside.run_history_fetch(
        "usr_a", message_id="m50", before=15, after=4,
        post_enclave=_echo_fetch_post(sent))
    assert sent["before"] == 15 and sent["after"] == 4
    assert out["omitted_before"] == 0 and out["omitted_after"] == 0


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

    older, newer, available = jobs_store.chat_history_neighbor_rows(
        uid, seqs[1], before=2, after=2)
    assert [row["seq"] for row in older] == [seqs[0]]  # 旧→新
    assert [row["seq"] for row in newer] == [seqs[2], s4]  # system 行被跳过
    # 取多少就报多少存在（可见性谓词同一套：system 行不算数）
    assert available == {"before": 1, "after": 2}


def test_neighbor_window_is_one_statement_so_rows_and_counts_share_a_snapshot():
    """取行与计数必须在**同一条语句**里，否则 omitted_* 又会开始撒谎。

    连接池是 autocommit（READ COMMITTED），每条语句各取一次快照；而
    chat_messages 并非 append-only（存在单条删除路径）。拆成两条 SQL 时，
    并发删除落在中间就会出现"取回的行已消失、计数数到后面补位的行"，
    ``available - len(rows)`` 于是凭空造出 omitted——正是这个字段要根除的
    谎报。并发时序难以在测试里稳定复现，所以这里锁住它的**充分条件**：
    一条语句、四个分支共享快照。有人把它拆回多条时这里会红。
    """
    jobs_store = history_readside.jobs_store
    sql = jobs_store._neighbor_window_sql()
    assert sql.count(";") == 0, "必须是单条语句"
    for branch in ("before_rows", "after_rows", "before_probe", "after_probe"):
        assert branch in sql, f"缺少 CTE 分支 {branch}"
    # 四个分支共用同一份可见性谓词（谓词不一致 = 计数与取行口径不同 = 新谎报）
    assert sql.count(jobs_store._HISTORY_VISIBLE_PREDICATE) == 4
    # 每个分支各自 LIMIT，计数在请求窗口处提前终止，不会全表扫
    assert sql.count("LIMIT") == 4


def test_store_neighbor_rows_report_how_many_exist_in_the_requested_window():
    """预算钳窄了取回条数时，store 仍要如实说出**请求窗口里真有几条**。

    这是 ``omitted_before/omitted_after`` 唯一诚实的来源：光看"取回 < 钳制值"
    分不清"到头了"和"还有更多"，从"取回 == 钳制值"更推不出"没取的那些都存在"。
    """
    _, _, _, jobs_store = _db_modules()
    uid = "u_hist_neighbor_witness"
    _db_reset(uid)
    seqs = [_db_append(uid, i, ts=float(i)) for i in range(9)]
    anchor = seqs[6]

    # 预算只允许取 2 条 before / 0 条 after，但窗口请求的是 15 / 4
    older, newer, available = jobs_store.chat_history_neighbor_rows(
        uid, anchor, before=2, after=0, before_requested=15, after_requested=4)
    assert [row["seq"] for row in older] == [seqs[4], seqs[5]]
    assert newer == []
    # before 侧只有 6 条真的存在（钳到 15 也拿不出更多）；after 侧有 2 条
    assert available == {"before": 6, "after": 2}

    # 请求窗口比历史短时按请求窗口封顶（"最近 n 条里有几条"）
    _, _, capped = jobs_store.chat_history_neighbor_rows(
        uid, anchor, before=1, after=1, before_requested=3, after_requested=1)
    assert capped == {"before": 3, "after": 1}

    # 一侧到头：锚点贴着历史开头 → before 侧一条都不存在
    _, _, edge = jobs_store.chat_history_neighbor_rows(
        uid, seqs[0], before=0, after=0, before_requested=15, after_requested=4)
    assert edge == {"before": 0, "after": 4}

    # 请求 0 条 = 不问，不发查询
    _, _, none_asked = jobs_store.chat_history_neighbor_rows(
        uid, anchor, before=0, after=0)
    assert none_asked == {"before": 0, "after": 0}
