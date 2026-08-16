"""做梦的「值不值得」判据。

重点守两条容易被后人「优化」掉的规则：

  1. 只数非做梦产生的卡 —— 否则会自激（做梦产生新卡 → 又触发 → 每晚空转）
  2. 种子集合从**全部卡**里筛，不是从可用卡里筛 —— 否则做梦退休旧卡会让
     水位线回退，下次又触发
"""
from __future__ import annotations

import pytest

from memory_garden.dreaming import (
    DREAM_SOURCE,
    DreamLedger,
    DreamSnapshot,
    dream_idempotency_key,
    dream_snapshot,
    needs_dream,
    seed_cards,
    snapshot_signature,
)


def _card(cid: str, source: str = "chat", **extra) -> dict:
    return {"id": cid, "source": source, "created_at": "2026-08-01", **extra}


# --------------------------------------------------------------------------- #
# 种子卡的筛选规则
# --------------------------------------------------------------------------- #


def test_dream_produced_cards_do_not_count():
    cards = [_card("a"), _card("b", DREAM_SOURCE), _card("c")]
    assert [c["id"] for c in seed_cards(cards)] == ["a", "c"]


def test_seed_set_is_order_independent():
    cards = [_card("c"), _card("a"), _card("b")]
    assert [c["id"] for c in seed_cards(cards)] == ["a", "b", "c"]


def test_retired_seed_cards_still_count():
    """做梦退休掉的旧种子卡仍算数 —— 水位线只增不减。

    入参是「全部卡」，其中 a 已被取代（status=superseded）但来源不是做梦，
    所以仍在种子集合里。
    """
    all_cards = [_card("a", status="superseded"), _card("b")]
    snap = dream_snapshot(available_cards=[_card("b")], all_cards=all_cards)
    assert snap.seed_card_count == 2, "退休的种子卡被漏掉了，水位线会回退"
    assert snap.card_count == 1


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #


def test_signature_is_stable_for_the_same_cards():
    cards = [_card("a"), _card("b")]
    assert snapshot_signature(cards) == snapshot_signature(list(cards))


def test_signature_changes_when_a_card_is_added():
    before = snapshot_signature([_card("a")])
    after = snapshot_signature([_card("a"), _card("b")])
    assert before != after


def test_signature_ignores_read_traffic():
    """被读一次不该改变签名 —— 否则每次查记忆都会触发做梦。"""
    plain = [_card("a")]
    touched = [_card("a", last_referenced_at="2026-08-14T10:00:00")]
    assert snapshot_signature(plain) == snapshot_signature(touched)


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #


def test_empty_garden_is_not_due():
    snap = DreamSnapshot(card_count=0, seed_card_count=0, signature="x")
    v = needs_dream(snap, DreamLedger(), min_new_cards=5)
    assert not v.needed and v.reason == "no_memory_cards"


def test_same_signature_means_already_dreamed():
    snap = DreamSnapshot(card_count=10, seed_card_count=10, signature="sig-1")
    ledger = DreamLedger(last_seed_card_count=0, last_signature="sig-1")
    v = needs_dream(snap, ledger, min_new_cards=1)
    assert not v.needed and v.reason == "already_dreamed"


def test_not_enough_new_cards():
    snap = DreamSnapshot(card_count=103, seed_card_count=103, signature="sig-2")
    ledger = DreamLedger(last_seed_card_count=100, last_signature="sig-1")
    v = needs_dream(snap, ledger, min_new_cards=10)
    assert not v.needed and v.reason == "not_enough_new_cards"
    assert v.new_cards == 3


def test_enough_new_cards_is_due():
    snap = DreamSnapshot(card_count=147, seed_card_count=147, signature="sig-2")
    ledger = DreamLedger(last_seed_card_count=100, last_signature="sig-1")
    v = needs_dream(snap, ledger, min_new_cards=10)
    assert v.needed and v.new_cards == 47


def test_first_ever_dream_has_no_ledger():
    """第一次做梦：账本是空的，只要卡够多就该跑。"""
    snap = DreamSnapshot(card_count=30, seed_card_count=30, signature="sig-1")
    v = needs_dream(snap, DreamLedger(), min_new_cards=10)
    assert v.needed and v.new_cards == 30


def test_seed_count_going_backwards_does_not_produce_negative():
    """水位线理论上只增不减，但真退了也不能算出负数。"""
    snap = DreamSnapshot(card_count=5, seed_card_count=5, signature="sig-2")
    ledger = DreamLedger(last_seed_card_count=100, last_signature="sig-1")
    v = needs_dream(snap, ledger, min_new_cards=1)
    assert v.new_cards == 0
    assert not v.needed


def test_verdict_never_looks_at_content():
    """判据只吃数量与指纹 —— 内容在密文里，看不了也不该看。"""
    snap = DreamSnapshot(card_count=50, seed_card_count=50, signature="s")
    v = needs_dream(snap, DreamLedger(last_seed_card_count=10), min_new_cards=5)
    assert v.needed  # 没有任何入参携带 bucket/summary/content


# --------------------------------------------------------------------------- #
# 幂等键
# --------------------------------------------------------------------------- #


def test_idempotency_key_is_stable_for_same_state():
    snap = DreamSnapshot(card_count=10, seed_card_count=10, signature="sig-2")
    ledger = DreamLedger(last_seed_card_count=5, last_signature="sig-1")
    assert dream_idempotency_key(ledger, snap) == dream_idempotency_key(ledger, snap)


def test_idempotency_key_changes_with_the_garden():
    ledger = DreamLedger(last_seed_card_count=5, last_signature="sig-1")
    a = dream_idempotency_key(ledger, DreamSnapshot(10, 10, "sig-2"))
    b = dream_idempotency_key(ledger, DreamSnapshot(11, 11, "sig-3"))
    assert a != b


@pytest.mark.parametrize("min_new", [0, 1, 10, 100])
def test_min_new_cards_is_a_host_parameter(min_new):
    """阈值由宿主传入 —— 它是可配置的运行参数，不是 Garden 的常量。"""
    snap = DreamSnapshot(card_count=60, seed_card_count=60, signature="s2")
    ledger = DreamLedger(last_seed_card_count=50, last_signature="s1")
    v = needs_dream(snap, ledger, min_new_cards=min_new)
    assert v.needed is (10 >= min_new)


# --------------------------------------------------------------------------- #
# 与改造前的算法对拍
# --------------------------------------------------------------------------- #


def _legacy_signature(all_cards):
    """改造前 proactive/dream_scheduler.py 里那段签名算法的直译（勿改）。"""
    import hashlib

    seed_moments = sorted(
        [c for c in all_cards if str(c.get("source") or "").strip() != "memory_dream"],
        key=lambda item: str(item.get("id") or ""),
    )
    return hashlib.sha256(
        "\n".join(
            "|".join(
                str(moment.get(key) or "")
                for key in ("id", "source", "created_at", "occurred_at")
            )
            for moment in seed_moments
        ).encode("utf-8")
    ).hexdigest()[:32]


def _legacy_key(state, snapshot):
    """改造前 dream_key_for_snapshot 的直译（勿改）。"""
    import hashlib

    material = "|".join(
        [
            str(state.get("last_dream_signature") or ""),
            str(snapshot.get("signature") or ""),
            str(snapshot.get("seed_card_count") or 0),
            str(snapshot.get("turn_count") or 0),
        ]
    )
    return "dream:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@pytest.mark.parametrize(
    "cards",
    [
        [],
        [_card("a")],
        [_card("b"), _card("a")],
        [_card("a"), _card("d", DREAM_SOURCE), _card("c")],
        [_card("a", occurred_at="2026-01-01"), _card("b", created_at="")],
        [_card(f"m_{i:03d}") for i in range(50)],
    ],
)
def test_signature_matches_legacy_algorithm(cards):
    """签名算法搬家后必须逐字节相同 —— 否则部署那天全体用户会误触发一次做梦。"""
    assert snapshot_signature(seed_cards(cards)) == _legacy_signature(cards)


@pytest.mark.parametrize(
    "last_sig,sig,seed_count,turn_count",
    [
        ("", "", 0, 0),
        ("sig-1", "sig-2", 10, 3),
        ("", "sig-2", 0, 0),
        ("sig-1", "", 999, 12345),
    ],
)
def test_idempotency_key_matches_legacy_algorithm(last_sig, sig, seed_count, turn_count):
    """幂等键同理 —— 变了会让已排队的活重复排一次。"""
    ledger = DreamLedger(last_seed_card_count=0, last_signature=last_sig)
    snap = DreamSnapshot(card_count=0, seed_card_count=seed_count, signature=sig)
    mine = dream_idempotency_key(ledger, snap, extra=(turn_count,))
    theirs = _legacy_key(
        {"last_dream_signature": last_sig},
        {"signature": sig, "seed_card_count": seed_count, "turn_count": turn_count},
    )
    assert mine == theirs
