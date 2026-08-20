"""swap 通道的跨形状原地替换（Phase 2 Task 2.3 / 普查 B3）。

swap 是加密开关切换的执行通道：用户把偏好从 on 改成 off，存量行要**逐条**从
双收件人信封换成明文；反之亦然。也是那 7 条 local_only 转明文的通道。

跨形状替换的要害不是「写新字段」，而是「删掉对面形状的残留字段」——读侧的规则
是 `body_ct` 优先于 `body`，所以信封转明文时若留下旧 `body_ct`，读到的会是**换
之前的过期内容**，而且完全静默。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from conftest import seed_user  # noqa: E402
from content import content_core  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402


def _sealed(**extra) -> dict:
    env = {
        "v": 1, "id": "itm-1", "body_ct": "Y3Q=", "nonce": "bm9uY2U=",
        "K_user": "a3U=", "K_enclave": "a2U=", "enclave_pk_fpr": "fpr",
        "owner_user_id": "usr_owner", "visibility": "shared",
    }
    env.update(extra)
    return env


def _plain(**extra) -> dict:
    env = {"body": "hello", "id": "itm-1",
           "owner_user_id": "usr_owner", "visibility": "shared"}
    env.update(extra)
    return env


def _binary_plain(**extra) -> dict:
    env = {
        "body_b64": "AAE=",
        "body_size_bytes": 2,
        "id": "itm-1",
        "owner_user_id": "usr_owner",
        "visibility": "shared",
    }
    env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# replace_record_shape —— 跨形状替换的唯一落点
# --------------------------------------------------------------------------- #

def test_sealed_to_plaintext_clears_every_crypto_field():
    """信封 → 明文：所有密文字段都必须消失。

    留一个 body_ct 就够坏事了：读侧「body_ct 优先」会一直读到换之前的内容，
    用户以为已经转明文、实际读的还是旧密文，且不报错。
    """
    record = dict(_sealed(), role="user", ts=1.0, content_type="text")

    core_envelope.replace_record_shape(record, _plain(body="new text"))

    assert record["body"] == "new text"
    for field in ("body_ct", "nonce", "K_user", "K_enclave",
                  "enclave_pk_fpr", "content_pk_fpr"):
        assert field not in record, f"{field} 残留"
    # 站点自有字段不能被碰。
    assert record["role"] == "user" and record["content_type"] == "text"


def test_plaintext_to_sealed_clears_body():
    """明文 → 信封：`body` 必须消失，否则服务端仍能读到本该被加密的正文。"""
    record = {"body": "secret", "id": "itm-1", "owner_user_id": "usr_owner",
              "visibility": "shared", "role": "user"}

    core_envelope.replace_record_shape(record, _sealed())

    assert "body" not in record
    assert record["body_ct"] == "Y3Q=" and record["K_enclave"] == "a2U="
    assert record["role"] == "user"


def test_plaintext_pointer_to_binary_plaintext_clears_internal_fields():
    record = {
        "body_key": "chatfiles/usr_owner/g1/itm-1/version",
        "body_object_format": "plaintext_v1",
        "body_sha256": "a" * 64,
        "body_size_bytes": 2,
        "owner_user_id": "usr_owner",
        "visibility": "shared",
        "role": "user",
    }

    core_envelope.replace_record_shape(
        record,
        {
            "body_b64": "AAE=",
            "body_size_bytes": 2,
            "owner_user_id": "usr_owner",
            "visibility": "shared",
        },
    )

    assert record["body_b64"] == "AAE="
    assert record["body_size_bytes"] == 2
    assert not ({"body_key", "body_object_format", "body_sha256"} & set(record))
    assert record["role"] == "user"


def test_sealed_rows_always_carry_an_enclave_pk_fpr_key():
    """老客户端不传这个指纹，但落库行历来恒有此键（缺省空串）——rewrap 的跳过
    逻辑直接从行上读它，键不在会让判断走空。"""
    env = _sealed()
    env.pop("enclave_pk_fpr")
    record: dict = {}

    core_envelope.replace_record_shape(record, env)

    assert record["enclave_pk_fpr"] == ""


def test_plaintext_rows_get_no_enclave_pk_fpr_key():
    record: dict = {}
    core_envelope.replace_record_shape(record, _plain())
    assert "enclave_pk_fpr" not in record


# --------------------------------------------------------------------------- #
# 校验：两种形状都收，但明文 + local_only 必须拒
# --------------------------------------------------------------------------- #

def test_missing_check_accepts_plaintext_shape():
    assert content_core._swap_envelope_missing(_plain()) == []


def test_missing_check_accepts_binary_plaintext_shape():
    assert content_core._swap_envelope_missing(_binary_plain()) == []


def test_missing_check_still_demands_the_sealed_field_set():
    env = _sealed()
    env.pop("nonce")
    assert "nonce" in content_core._swap_envelope_missing(env)


def test_missing_check_reports_shape_when_neither_body_nor_body_ct():
    missing = content_core._swap_envelope_missing(
        {"visibility": "shared", "owner_user_id": "usr_owner"})
    assert "body_ct" in missing or "body" in missing


def test_missing_check_still_demands_owner_and_visibility_for_plaintext():
    env = _plain()
    env.pop("owner_user_id")
    assert "owner_user_id" in content_core._swap_envelope_missing(env)


@pytest.fixture()
def store(backend_env):
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id, api_key_hash="h", doc={})
    return core_store.UserStore(user_id)


def _swap_one(store, env) -> str:
    body, status = content_core.swap(
        store, {"items": [{"type": "chat", "id": "no-such-row", "envelope": env}]})
    assert status == 200
    return body["results"][0]["status"]


def test_plaintext_local_only_is_rejected(store):
    """明文 + local_only 自相矛盾，必须拒。

    local_only 的含义是「只有设备解得开」——它靠的就是没有 K_enclave。一行明文
    服务端天然读得到，标成 local_only 等于给用户一个假的隐私承诺。
    """
    status = _swap_one(store, _plain(owner_user_id=store.user_id,
                                    visibility="local_only"))

    assert status.startswith("error:")
    assert "local_only" in status


def test_plaintext_shared_passes_validation(store):
    """明文 + shared 要过校验（找不到行是另一回事）。

    原来的闸是「shared 必须有 K_enclave」，明文信封没有 K_enclave 会被它拦死。
    """
    status = _swap_one(store, _plain(owner_user_id=store.user_id))

    assert status == "not_found", status


def test_sealed_shared_still_requires_k_enclave(store):
    """加密档的既有闸不能因为放开明文而松掉。"""
    env = _sealed(owner_user_id=store.user_id)
    env.pop("K_enclave")

    status = _swap_one(store, env)

    assert status.startswith("error:") and "K_enclave" in status


def test_owner_mismatch_still_rejected_for_plaintext(store):
    status = _swap_one(store, _plain(owner_user_id="usr_someone_else"))
    assert status.startswith("error:") and "owner_user_id" in status


# --------------------------------------------------------------------------- #
# memory 的原地替换
# --------------------------------------------------------------------------- #

def test_memory_inplace_sealed_to_plaintext_leaves_no_residue():
    moments = [dict(_sealed(), id="mom-1", type="fact", occurred_at="t")]

    assert content_core._swap_memory_inplace(
        moments, "mom-1", _plain(id="mom-1", body="new")) == "ok"

    m = moments[0]
    assert m["body"] == "new" and "body_ct" not in m and "K_enclave" not in m
    assert m["type"] == "fact" and m["occurred_at"] == "t"


def test_memory_inplace_not_found_is_reported():
    assert content_core._swap_memory_inplace([], "nope", _plain()) == "not_found"
