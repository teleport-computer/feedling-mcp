"""加密档用户的行按原样搬进 TEE，不解密（Task 2.4 实现）。

## 判据是「用户意图」，不是「行形状」

设计初稿写的是「按行形状搬运」，实现时发现有洞：现在 `PLAINTEXT_WRITES_ACCEPTED`
是 False、effective 恒 "on"，**所有行都是信封**。按行形状搬运会让影子库立刻整体
变密文，明文排查通道当场失效——那是过渡期回归，不是终态该有的样子。

按**用户意图**（`content_encryption` 偏好）分流则没有这个问题：

- 显式选了加密的用户 → 原样搬运（他们要的就是服务端读不到）
- 其余用户 → 维持现状解密成明文（他们的行现在是信封，只是因为平台还没放开明文，
  这不代表他们要加密）

平台放开明文后，意图与形状自然一致，本分流退化成「按行形状搬运」，即设计的终态。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from accounts import registry  # noqa: E402
from conftest import seed_user  # noqa: E402
from tee_replicator import transforms  # noqa: E402
from tee_replicator import worker as tee_worker  # noqa: E402


def _sealed_chat_doc() -> dict:
    return {
        "id": "msg-1", "role": "user", "ts": 1.0, "source": "app",
        "v": 1, "body_ct": "Y3Q=", "nonce": "bm9uY2U=", "K_user": "a3U=",
        "K_enclave": "a2U=", "enclave_pk_fpr": "fpr",
        "visibility": "shared", "owner_user_id": "usr_owner",
        "content_type": "text",
    }


@pytest.fixture()
def uid(backend_env):
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id, api_key_hash="h", doc={})
    tee_worker._carry_verbatim_cache.clear()
    return user_id


class _Cfg:
    """最小 table cfg：只需要 transform 字段。"""

    def __init__(self, transform):
        self.transform = transform


def _boom(*_a, **_kw):
    raise AssertionError("加密档不应触碰 enclave")


def test_encrypted_tier_row_is_carried_untouched(uid, monkeypatch):
    """加密档：整行原样进 TEE，且**完全不调 enclave**。"""
    registry._set_user_content_encryption(uid, "on")
    tee_worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(tee_worker, "_get_decrypt", _boom)

    doc = _sealed_chat_doc()
    out = tee_worker._transform_with_retry(
        _Cfg(transforms.plaintext_chat_doc), doc, uid)

    assert out == doc
    assert out["body_ct"] == "Y3Q=" and out["K_enclave"] == "a2U="
    assert "body" not in out, "加密档搬运后不得出现明文正文"


def test_default_tier_row_is_still_decrypted(uid, monkeypatch):
    """未选加密的用户维持现状：解密成明文、丢掉加密学字段。

    他们的行现在是信封只是因为平台还没放开明文，不代表他们要加密——影子库的
    明文排查通道对他们必须继续可用。
    """
    monkeypatch.setattr(tee_worker, "_get_decrypt",
                        lambda user_id, **kw: lambda env, purpose: b"hello")

    out = tee_worker._transform_with_retry(
        _Cfg(transforms.plaintext_chat_doc), _sealed_chat_doc(), uid)

    assert out["body"] == "hello"
    for key in ("body_ct", "nonce", "K_user", "K_enclave", "enclave_pk_fpr"):
        assert key not in out, f"明文档搬运后不得残留 {key}"


def test_explicit_off_is_also_decrypted(uid, monkeypatch):
    """显式选明文与未设置同样处理——只有显式 "on" 才原样搬运。"""
    registry._set_user_content_encryption(uid, "off")
    tee_worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(tee_worker, "_get_decrypt",
                        lambda user_id, **kw: lambda env, purpose: b"hi")

    out = tee_worker._transform_with_retry(
        _Cfg(transforms.plaintext_chat_doc), _sealed_chat_doc(), uid)

    assert out["body"] == "hi"


def test_unknown_user_is_carried_verbatim(backend_env, monkeypatch):
    """查不到用户 → 原样搬运（fail-safe 不解密）。

    与写侧 fail-safe 方向一致：拿不准时**不要**把密文变明文。搬运的失败方向是
    「多留了密文」，可以事后重放修；解密的失败方向是「明文泄漏」，不可逆。
    """
    tee_worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(tee_worker, "_get_decrypt", _boom)

    doc = _sealed_chat_doc()
    out = tee_worker._transform_with_retry(
        _Cfg(transforms.plaintext_chat_doc), doc, "usr_definitely_not_here")

    assert out == doc


def test_already_plaintext_row_is_carried_as_is(uid, monkeypatch):
    """加密档用户若有明文行（切档前的存量），原样搬运即可，不该去解密它。"""
    registry._set_user_content_encryption(uid, "on")
    tee_worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(tee_worker, "_get_decrypt", _boom)

    doc = {"id": "msg-2", "body": "plain", "owner_user_id": uid,
           "visibility": "shared", "role": "user", "ts": 2.0}

    out = tee_worker._transform_with_retry(
        _Cfg(transforms.plaintext_chat_doc), doc, uid)

    assert out == doc


def test_carry_verbatim_scrubs_nul_bytes(uid, monkeypatch):
    """原样搬运也必须剥 NUL：PostgreSQL 的 text/JSONB 存不了它。

    这是历史事故（tee-sync NUL 卡死重试循环），搬运路径不能重新引入。
    """
    registry._set_user_content_encryption(uid, "on")
    tee_worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(tee_worker, "_get_decrypt", _boom)

    doc = dict(_sealed_chat_doc(), source="app\x00bad")

    out = tee_worker._transform_with_retry(
        _Cfg(transforms.plaintext_chat_doc), doc, uid)

    assert out["source"] == "appbad"


def test_preference_is_cached_but_bounded(uid, monkeypatch):
    """按行查 registry 会 O(用户数) 扫全表，必须缓存；但不能永久缓存，
    否则用户切档后复制层要到进程重启才跟上。"""
    calls = []
    real = registry._get_user_content_encryption
    monkeypatch.setattr(registry, "_get_user_content_encryption",
                        lambda u: calls.append(u) or real(u))
    tee_worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(tee_worker, "_get_decrypt",
                        lambda user_id, **kw: lambda env, purpose: b"x")

    for _ in range(5):
        tee_worker._transform_with_retry(
            _Cfg(transforms.plaintext_chat_doc), _sealed_chat_doc(), uid)

    assert len(calls) == 1, "同一用户连续多行只应查一次偏好"
    assert tee_worker._CARRY_VERBATIM_TTL_SEC > 0


# ---------------------------------------------------------------------------
# verify 侧：对账口径必须跟着搬运口径走
#
# 复制层对加密档不解密了，verify 若还按「RDS 密文解开 == TEE 明文」比对，
# 每一行都会报 mismatch——一趟全红，等于失去量测。加密档应当**密文逐字比对**，
# 反而比解密后比对更快也更可靠。

def test_verify_compares_encrypted_tier_ciphertext_verbatim(uid, monkeypatch):
    from tee_shadow import verify

    registry._set_user_content_encryption(uid, "on")
    tee_worker._carry_verbatim_cache.clear()

    def boom(*_a, **_kw):
        raise AssertionError("加密档对账不该调 enclave")

    monkeypatch.setattr(verify, "_get_decrypt", boom)

    doc = _sealed_chat_doc()
    expected, err = verify._expected_doc(
        uid, doc, transforms.plaintext_chat_doc, {})

    assert err is None
    assert expected == doc, "加密档应当密文逐字比对"


def test_verify_still_decrypts_for_default_tier(uid, monkeypatch):
    from tee_shadow import verify

    monkeypatch.setattr(verify, "_get_decrypt",
                        lambda cache, user_id: lambda env, purpose: b"hello")

    expected, err = verify._expected_doc(
        uid, _sealed_chat_doc(), transforms.plaintext_chat_doc, {})

    assert err is None and expected["body"] == "hello"


def test_verify_reports_decrypt_failure_without_aborting(uid, monkeypatch):
    """解不开只让这一行失败，不能冲垮整趟 verify（2026-07-28 prod 事故）。"""
    from tee_shadow import verify

    def failing(cache, user_id):
        def _d(env, purpose):
            raise RuntimeError("enclave 403 decrypt_failed")
        return _d

    monkeypatch.setattr(verify, "_get_decrypt", failing)

    expected, err = verify._expected_doc(
        uid, _sealed_chat_doc(), transforms.plaintext_chat_doc, {})

    assert expected is None
    assert err == "enclave 403 decrypt_failed"


def test_verify_skips_rows_pending_device_migration(uid, monkeypatch):
    """local_only / 无 K_enclave：不是内容 mismatch，跳过而不是记错。"""
    from tee_shadow import verify

    monkeypatch.setattr(verify, "_get_decrypt",
                        lambda cache, user_id: lambda env, purpose: b"x")

    doc = _sealed_chat_doc()
    doc.pop("K_enclave")

    expected, err = verify._expected_doc(
        uid, doc, transforms.plaintext_chat_doc, {})

    assert expected is None and err is None, "PendingDeviceMigration 应表示为跳过"
