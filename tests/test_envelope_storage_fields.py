"""形状无关的信封落库字段取值（Phase 2 Task 2.2 第一步）。

29 个硬拆包点全都在拼「站点自有字段（id/role/ts/type…）+ 一组固定的信封字段」。
`envelope_storage_fields` 只负责后者，站点自有字段仍由调用方写——这样迁移是纯
机械的，且**在写侧形状真正切换前行为逐字不变**（生产者此刻仍恒出信封）。

本文件锁的是 helper 自身的契约。迁移是否漏改由 L1 全量回归兜。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core.envelope import envelope_storage_fields  # noqa: E402


def _sealed(**extra) -> dict:
    env = {
        "v": 1,
        "id": "itm-1",
        "body_ct": "Y3Q=",
        "nonce": "bm9uY2U=",
        "K_user": "a3U=",
        "K_enclave": "a2U=",
        "enclave_pk_fpr": "fpr-enclave",
        "owner_user_id": "usr_owner",
        "visibility": "shared",
    }
    env.update(extra)
    return env


def test_sealed_envelope_yields_every_crypto_field():
    out = envelope_storage_fields(_sealed())

    assert out == {
        "body_ct": "Y3Q=",
        "nonce": "bm9uY2U=",
        "K_user": "a3U=",
        "K_enclave": "a2U=",
        "enclave_pk_fpr": "fpr-enclave",
        "owner_user_id": "usr_owner",
        "visibility": "shared",
    }


def test_missing_k_enclave_is_omitted_not_null():
    """local_only 信封没有 K_enclave。写成 None 会让「有这个键但为空」与
    「没有这个键」混淆——尾账判据（有 K_user 无 K_enclave = 孤岛）正是靠它。"""
    env = _sealed()
    env.pop("K_enclave")

    out = envelope_storage_fields(env)

    assert "K_enclave" not in out


def test_content_pk_fpr_is_carried_when_present():
    """封装时用的用户公钥指纹：rewrap 的跳过逻辑从落库行上读它。
    老客户端不传，此时不能凭空造一个空串以外的值。"""
    out = envelope_storage_fields(_sealed(content_pk_fpr="fpr-user"))
    assert out["content_pk_fpr"] == "fpr-user"

    assert "content_pk_fpr" not in envelope_storage_fields(_sealed())


def test_plaintext_envelope_yields_body_and_no_crypto_fields():
    out = envelope_storage_fields({
        "body": "hello",
        "id": "itm-2",
        "owner_user_id": "usr_owner",
        "visibility": "shared",
    })

    assert out == {"body": "hello", "owner_user_id": "usr_owner", "visibility": "shared"}
    assert not ({"body_ct", "nonce", "K_user", "K_enclave"} & set(out))


def test_body_ct_wins_when_both_shapes_present():
    """迁移中间态：密文是真源。反过来会把过期的明文残留落成新行。"""
    env = _sealed(body="stale plaintext")

    out = envelope_storage_fields(env)

    assert out["body_ct"] == "Y3Q="
    assert "body" not in out


def test_owner_and_visibility_fall_back_to_caller_defaults():
    """服务端自产的信封不一定带 owner_user_id——落库行必须有，否则跨用户
    读取的归属判定会落空。"""
    out = envelope_storage_fields(
        {"body_ct": "Y3Q=", "nonce": "bm9uY2U=", "K_user": "a3U="},
        default_owner_user_id="usr_fallback")

    assert out["owner_user_id"] == "usr_fallback"
    assert out["visibility"] == "shared"


def test_explicit_owner_beats_default():
    out = envelope_storage_fields(_sealed(), default_owner_user_id="usr_other")
    assert out["owner_user_id"] == "usr_owner"


def test_unrecognized_shape_raises():
    """既无 body_ct 也无 body：不能猜，更不能落一行没有正文的空壳。"""
    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        envelope_storage_fields({"id": "itm-3", "owner_user_id": "usr_owner"})


def test_non_dict_raises():
    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        envelope_storage_fields(None)  # type: ignore[arg-type]


def test_empty_string_body_is_still_a_plaintext_row():
    """空正文是合法内容（比如一条只有附件的消息）。用 `is None` 判在场，
    不能用真值判——否则空串会被当成「形状不认识」而抛错。"""
    out = envelope_storage_fields({"body": "", "owner_user_id": "u", "visibility": "shared"})
    assert out["body"] == ""
