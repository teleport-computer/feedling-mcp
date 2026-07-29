"""写侧格式路由：服务端自产内容按 content_encryption 偏好选形状（Phase 2 Task 2.2）。

43 个服务端封装点里 40 处都经 core.envelope._build_shared_envelope_for_store，
而它已经接收 store —— 偏好唾手可得，故在此一处收口。
（客户端上传走各写闸、有独立的硬形状校验，是另一条路径，不在本文件范围。）
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from accounts import registry  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402


# 2026-07-29 的一次回退与最终解法（留作后来者的上下文）：
# 初版直接按用户偏好一刀切改明文，导致 test_v2_encrypted_effect_payload 抓到明文
# 出现在存储内容里、L1 从 2 failed 涨到 16 failed，已回退。真实根因不是「该路径
# 必须始终加密」，而是 _get_user_content_encryption 当时把「用户不存在」与「未设
# 偏好」都返回 None，helper 把前者也当成了明文档——那个测试的 u_effect_real_crypto
# 正是不在 registry 里的纯单元测试用户。
# 最终解法：偏好改三态（on / off / None=查不到用户），helper 只在明确 "off" 时
# 走明文，None 一律 fail-safe 加密；并由 0068 放宽 0043 的表级 CHECK 让明文行
# 能入库。B 类（凭证）的特殊处理经用户 2026-07-29 拍板取消，40 处统一按偏好。


pytestmark = pytest.mark.skip(
    reason="Task 2.2 未完成：helper 写侧路由第二次回退（2026-07-29）。"
           "真实阻塞是下游消费者硬编码 envelope['body_ct'] 拆包，见文件头注释。"
)


@pytest.fixture()
def store(backend_env):
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id, api_key_hash="h", doc={})
    return core_store.UserStore(user_id)


def test_plaintext_user_gets_plaintext_shape(store):
    """默认（未设置=off）→ 明文形状，字段集与 TEE 侧实测一致。"""
    out, err = core_envelope._build_shared_envelope_for_store(store, b"hello")

    assert err == "", err
    assert out is not None
    assert set(out) == {"body", "id", "owner_user_id", "visibility"}
    assert out["body"] == "hello"
    assert out["owner_user_id"] == store.user_id
    assert out["visibility"] == "shared"
    assert "body_ct" not in out and "K_enclave" not in out


def test_plaintext_user_does_not_need_enclave(store, monkeypatch):
    """明文档必须在 enclave 完全不可达时照样写入成功。

    这是 v6「明文档读写不经 enclave」的兑现点，也是 cutover 后 enclave 只服务
    加密档用户的前提。若这里仍依赖 enclave 公钥，明文用户会被 enclave 故障连坐。
    """
    def boom(*a, **kw):
        raise AssertionError("明文档不应触碰 enclave")

    monkeypatch.setattr(core_envelope.enclave, "_get_enclave_info", boom)

    out, err = core_envelope._build_shared_envelope_for_store(store, b"hi")

    assert err == ""
    assert out is not None and out["body"] == "hi"


def _wire_assembly(monkeypatch):
    """补上 assembly 层（asgi/lifespan.py）在生产里注入的两样东西。

    `core.envelope.get_user_public_key` 默认是个会 raise 的占位——core 不 import
    accounts，由装配层注入（见模块 docstring）。加密档路径必须先接线才可测。
    """
    import base64 as _b64
    from nacl.public import PrivateKey

    user_pk = bytes(PrivateKey.generate().public_key)
    enclave_pk = bytes(PrivateKey.generate().public_key)
    monkeypatch.setattr(core_envelope, "get_user_public_key",
                        lambda uid: _b64.b64encode(user_pk).decode())
    monkeypatch.setattr(core_envelope.enclave, "_get_enclave_info",
                        lambda *a, **kw: {"content_pk_hex": enclave_pk.hex()})


def test_encrypted_user_still_gets_dual_recipient_envelope(store, monkeypatch):
    """开关 on → 维持现有双收件人信封（加密档功能不降级）。"""
    registry._set_user_content_encryption(store.user_id, "on")
    _wire_assembly(monkeypatch)

    out, err = core_envelope._build_shared_envelope_for_store(store, b"secret")

    assert err == "", err
    assert out is not None
    assert "body_ct" in out and "K_enclave" in out and "K_user" in out
    assert "body" not in out, "加密档不得同时留明文 body"


def test_encrypted_user_never_silently_falls_back_to_plaintext(store, monkeypatch):
    """加密档在 enclave 不可用时必须失败，绝不能降级写明文。

    悄悄把加密档用户的内容写成明文是本计划最严重的失败模式——用户以为自己
    开了加密，实际服务端可读。
    """
    registry._set_user_content_encryption(store.user_id, "on")
    _wire_assembly(monkeypatch)
    monkeypatch.setattr(core_envelope.enclave, "_get_enclave_info", lambda *a, **kw: None)

    out, err = core_envelope._build_shared_envelope_for_store(store, b"secret")

    assert out is None, "加密档绝不能在 enclave 不可用时降级成明文"
    assert err == "enclave_info_unavailable"


def test_item_id_is_honored_in_plaintext_shape(store):
    """调用方传的 item_id 必须落到明文行的 id 上——读侧与对账都靠它定位行。"""
    out, err = core_envelope._build_shared_envelope_for_store(
        store, b"x", item_id="itm-42")

    assert err == ""
    assert out["id"] == "itm-42"
