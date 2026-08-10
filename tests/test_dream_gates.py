"""dream_gates 纯函数 + V1/V2 两条 lane 的判据一致性。

2026-08-05 阀门重构(usr_a40e 墓碑卡事故)的回归锁:
  - 出口只拦「明显不对」:卡id泄漏、爆炸半径;绝不判内容质量。
  - V1 consumer 与 V2 extraction 共用同一套结构判据 —— 同一份输入必须给出
    同样的 supersede 集合,否则就是当年「栅栏两处复制各自漂移」的复辟。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

# consumer 模块在 import 时读这些 env(与 test_chat_resident_consumer 同一套默认值)。
for _k, _v in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402

from memory import dream_gates  # noqa: E402


# ---------------------------------------------------------------------------
# known_id_in_text / result_id_leak
# ---------------------------------------------------------------------------


def test_known_id_leak_exact_match_only():
    known = {"c42ebb9618ae447df9d52107ea15de85", "1a1f94f9fdc9ec8649f81a7fbc0bee08"}
    assert dream_gates.known_id_in_text(
        "已被 c42ebb9618ae447df9d52107ea15de85 取代——绿豆汤", known
    ) == "c42ebb9618ae447df9d52107ea15de85"
    # 不在花园里的 hex 串不算(用户聊 git SHA 之类不误伤)
    assert dream_gates.known_id_in_text("部署了 4c1c750799 版本", known) == ""
    assert dream_gates.known_id_in_text("", known) == ""
    assert dream_gates.known_id_in_text("正常内容", set()) == ""


def test_known_id_leak_ignores_too_short_ids():
    # 短 token 当子串匹配会误伤 —— 低于下限的 known_id 不参与。
    assert dream_gates.known_id_in_text("abc 在文本里", {"abc"}) == ""


def test_result_id_leak_reports_field():
    known = {"c42ebb9618ae447df9d52107ea15de85"}
    assert dream_gates.result_id_leak(
        summary="已被 c42ebb9618ae447df9d52107ea15de85 取代",
        content="正文",
        known_ids=known,
    ) == "summary_contains_card_id"
    assert dream_gates.result_id_leak(
        summary="正常摘要",
        content="合并自 c42ebb9618ae447df9d52107ea15de85",
        known_ids=known,
    ) == "content_contains_card_id"
    assert dream_gates.result_id_leak(
        summary="正常摘要", content="正常正文", known_ids=known
    ) is None


# ---------------------------------------------------------------------------
# blast_radius_exceeded
# ---------------------------------------------------------------------------


def test_fuse_needs_both_ratio_and_floor():
    assert dream_gates.blast_radius_exceeded(13, 15) is True     # 87% 且 ≥10
    assert dream_gates.blast_radius_exceeded(12, 15) is False    # 恰 80% 不超
    assert dream_gates.blast_radius_exceeded(9, 10) is False     # 90% 但 <10 张
    assert dream_gates.blast_radius_exceeded(4, 5) is False      # 小花园正常整理
    assert dream_gates.blast_radius_exceeded(0, 0) is False
    assert dream_gates.blast_radius_exceeded(834, 834) is True   # 当年的事故形状


def test_fuse_env_overrides(monkeypatch):
    monkeypatch.setenv("FEEDLING_DREAM_FUSE_RATIO", "0.5")
    monkeypatch.setenv("FEEDLING_DREAM_FUSE_MIN_CARDS", "3")
    assert dream_gates.blast_radius_exceeded(4, 6) is True       # 67% > 50% 且 ≥3
    monkeypatch.setenv("FEEDLING_DREAM_FUSE_RATIO", "not-a-number")
    monkeypatch.setenv("FEEDLING_DREAM_FUSE_MIN_CARDS", "-1")
    assert dream_gates.blast_radius_exceeded(4, 6) is False      # 坏值回默认 0.8/10


# ---------------------------------------------------------------------------
# 跨 lane 一致性:同一份 consolidations,V1 与 V2 必须退休同一批卡
# ---------------------------------------------------------------------------


def _cross_lane_fixture():
    cards = [
        {"id": f"m{i}", "summary": f"S{i}", "content": f"C{i} 的旧正文。"}
        for i in range(5)
    ]
    rows = [
        {"op": "merge", "card_ids": ["m0", "m1"], "rationale": "同一线索",
         "result": {"summary": "合并摘要", "content": "合并后的完整正文。"}},
        {"op": "supersede", "card_ids": ["m2"], "rationale": "同一事实更新",
         "result": {"summary": "更新摘要", "content": "更短但更准。"}},   # 1:1 无栅栏
        {"op": "merge", "card_ids": ["m1", "m3"], "rationale": "重复退休 m1",
         "result": {"summary": "x", "content": "y。"}},                  # 重复 → 拒
        {"op": "merge", "card_ids": ["ghost", "m4"], "rationale": "目标不存在",
         "result": {"summary": "x", "content": "y。"}},                  # 未知 → 拒
        {"op": "merge", "card_ids": ["m3", "m4"], "rationale": "",
         "result": {"summary": "x", "content": "y。"}},                  # 无 rationale → 拒
    ]
    return cards, rows


def test_v1_and_v2_mappers_retire_the_same_cards(monkeypatch):
    cards, rows = _cross_lane_fixture()

    from model_api_runtime.v2 import extraction as v2_extraction

    v2_actions, _added, v2_superseded = v2_extraction.consolidations_to_actions(
        [dict(row) for row in rows],
        occurred_at="2026-08-05T00:00:00Z",
        source_ids=[],
        build_envelope=lambda inner: {"id": "env", **inner},
        existing_cards=cards,
    )

    import chat_resident_consumer as crc

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(crc, "_refresh_whoami_for_encrypted_reply", lambda: True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_x", "user_pk": b"u" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(
        crc,
        "_build_envelope",
        lambda **kwargs: {"v": 1, "id": "env", "visibility": kwargs["visibility"],
                          "owner_user_id": kwargs["owner_user_id"],
                          "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke"},
    )
    v1_actions, _m, _t, v1_superseded, _o, _mc = crc._dream_actions_from_consolidations(
        [dict(row) for row in rows],
        card_map={card["id"]: card for card in cards},
        occurred_at="2026-08-05T00:00:00Z",
    )

    v1_sets = [action["supersedes"] for action in v1_actions]
    v2_sets = [action["supersedes"] for action in v2_actions]
    assert v1_sets == v2_sets == [["m0", "m1"], ["m2"]]
    assert v1_superseded == v2_superseded == 3


def test_v1_and_v2_fuse_trip_identically(monkeypatch):
    cards = [
        {"id": f"m{i}", "summary": f"S{i}", "content": f"C{i}"} for i in range(15)
    ]
    rows = [
        {"op": "merge", "card_ids": [f"m{i}", f"m{i + 1}"], "rationale": "同一线索",
         "result": {"summary": "合并", "content": "合并正文。"}}
        for i in range(0, 12, 2)
    ] + [
        {"op": "supersede", "card_ids": ["m12"], "rationale": "更新",
         "result": {"summary": "更新", "content": "更新正文。"}},
    ]
    # V2 的保险丝在 worker 层(见 test_v2_extraction_lanes),纯函数口径在这里对齐:
    assert dream_gates.blast_radius_exceeded(13, 15) is True

    import chat_resident_consumer as crc

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(crc, "_refresh_whoami_for_encrypted_reply", lambda: True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_x", "user_pk": b"u" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(
        crc,
        "_build_envelope",
        lambda **kwargs: {"v": 1, "id": "env", "visibility": kwargs["visibility"],
                          "owner_user_id": kwargs["owner_user_id"],
                          "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke"},
    )
    with pytest.raises(ValueError, match="dream_blast_radius_exceeded"):
        crc._dream_actions_from_consolidations(
            rows,
            card_map={card["id"]: card for card in cards},
            occurred_at="2026-08-05T00:00:00Z",
        )
