"""写侧格式路由：服务端自产内容按生效形状选形状（Phase 2 Task 2.2）。

43 个服务端封装点里 40 处都经 core.envelope._build_shared_envelope_for_store，
而它已经接收 store —— 偏好唾手可得，故在此一处收口。
（客户端上传走各写闸、有独立的硬形状校验，是另一条路径；swap 通道见
tests/test_swap_shape_transition.py。）

这个 Task 前后回退过两次，教训值得留着：

1. 第一次直接按用户偏好一刀切改明文 → test_v2_encrypted_effect_payload 抓到明文
   出现在存储内容里、L1 从 2 failed 涨到 16 failed。真根因不是「该路径必须始终
   加密」，而是 _get_user_content_encryption 当时把「用户不存在」与「未设偏好」
   都返回 None，helper 把前者也当成了明文档。
2. 第二次三态 + 0068 都就位后重做 → L1 仍 15 failed。真实阻塞在**生产代码**：
   下游消费者硬编码 envelope["body_ct"] 拆包重组 doc（hosted/history_import.py
   :3024 等）。范围是「40 个调用点 × 各自的下游消费代码」，不是「一处收口」。

第三次（2026-07-30）先做完 29 个拆包点的普查与迁移，才动这里。
B 类（凭证）的特殊处理经用户 2026-07-29 拍板取消，40 处统一按偏好。
"""
from __future__ import annotations

import os
import base64
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from conftest import seed_user  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402


def _prefer(monkeypatch, value: str) -> None:
    """装配层注入的「生效形状」解析。

    生产里由 `asgi/lifespan.py` 接到 `registry.effective_content_encryption`
    ——用 effective 而不是原始偏好，是为了让服务端自产内容与客户端上传闸受
    **同一个开关**（`PLAINTEXT_WRITES_ACCEPTED`）管，不会一边放开一边没放。
    """
    monkeypatch.setattr(core_envelope, "resolve_content_encryption",
                        lambda uid: value)


@pytest.fixture()
def store(backend_env):
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id, api_key_hash="h", doc={})
    return core_store.UserStore(user_id)


def test_unwired_resolver_defaults_to_encrypt():
    """装配层没接线时必须走加密。

    写侧的安全失败方向是加密：某个 worker 进程漏接线不能变成「把用户内容明文
    落库」。这也让所有不接装配层的纯单元测试行为与改造前逐字一致。
    """
    assert core_envelope.resolve_content_encryption("usr_whatever") == "on"


def test_plaintext_tier_gets_plaintext_shape(store, monkeypatch):
    """生效形状 off → 明文形状，字段集与 TEE 侧实测一致。"""
    _prefer(monkeypatch, "off")

    out, err = core_envelope._build_shared_envelope_for_store(store, b"hello")

    assert err == "", err
    assert out is not None
    assert set(out) == {"body", "id", "owner_user_id", "visibility"}
    assert out["body"] == "hello"
    assert out["owner_user_id"] == store.user_id
    assert out["visibility"] == "shared"
    assert "body_ct" not in out and "K_enclave" not in out


def test_plaintext_tier_does_not_need_enclave(store, monkeypatch):
    """明文档必须在 enclave 完全不可达时照样写入成功。

    这是 v6「明文档读写不经 enclave」的兑现点，也是 cutover 后 enclave 只服务
    加密档用户的前提。若这里仍依赖 enclave 公钥或用户内容公钥，明文用户会被
    enclave 故障 / 老账号缺公钥连坐。
    """
    _prefer(monkeypatch, "off")

    def boom(*a, **kw):
        raise AssertionError("明文档不应触碰 enclave / 内容公钥")

    monkeypatch.setattr(core_envelope.enclave, "_get_enclave_info", boom)
    monkeypatch.setattr(core_envelope, "get_user_public_key", boom)

    out, err = core_envelope._build_shared_envelope_for_store(store, b"hi")

    assert err == ""
    assert out is not None and out["body"] == "hi"


def test_plaintext_tier_rejects_non_utf8_body(store, monkeypatch):
    """明文列是 text。二进制正文走 R2 指针，不该走到这里——报错而不是静默乱码。"""
    _prefer(monkeypatch, "off")

    out, err = core_envelope._build_shared_envelope_for_store(store, b"\xff\xfe")

    assert out is None and err == "plaintext_body_not_utf8"


def test_plaintext_tier_binary_body_uses_base64_shape(store, monkeypatch):
    """Callers that own binary content can request the existing binary shape."""
    _prefer(monkeypatch, "off")
    raw = b"\xff\x00%PDF"

    out, err = core_envelope._build_shared_envelope_for_store(
        store,
        raw,
        content_kind="binary",
    )

    assert err == "", err
    assert out is not None
    assert set(out) == {
        "body_b64", "body_size_bytes", "id", "owner_user_id", "visibility",
    }
    assert base64.b64decode(out["body_b64"], validate=True) == raw
    assert out["body_size_bytes"] == len(raw)
    assert out["owner_user_id"] == store.user_id
    assert out["visibility"] == "shared"
    assert "body" not in out and "body_ct" not in out


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


def test_encrypted_tier_still_gets_dual_recipient_envelope(store, monkeypatch):
    """生效形状 on → 维持现有双收件人信封（加密档功能不降级）。"""
    _prefer(monkeypatch, "on")
    _wire_assembly(monkeypatch)

    out, err = core_envelope._build_shared_envelope_for_store(store, b"secret")

    assert err == "", err
    assert out is not None
    assert "body_ct" in out and "K_enclave" in out and "K_user" in out
    assert "body" not in out, "加密档不得同时留明文 body"


def test_encrypted_tier_never_silently_falls_back_to_plaintext(store, monkeypatch):
    """加密档在 enclave 不可用时必须失败，绝不能降级写明文。

    悄悄把加密档用户的内容写成明文是本计划最严重的失败模式——用户以为自己
    开了加密，实际服务端可读。
    """
    _prefer(monkeypatch, "on")
    _wire_assembly(monkeypatch)
    monkeypatch.setattr(core_envelope.enclave, "_get_enclave_info",
                        lambda *a, **kw: None)

    out, err = core_envelope._build_shared_envelope_for_store(store, b"secret")

    assert out is None, "加密档绝不能在 enclave 不可用时降级成明文"
    assert err == "enclave_info_unavailable"


def test_item_id_is_honored_in_both_shapes(store, monkeypatch):
    """调用方传的 item_id 必须落到行的 id 上——读侧与对账都靠它定位行。"""
    _prefer(monkeypatch, "off")
    out, err = core_envelope._build_shared_envelope_for_store(
        store, b"x", item_id="itm-42")
    assert err == "" and out["id"] == "itm-42"

    _prefer(monkeypatch, "on")
    _wire_assembly(monkeypatch)
    out, err = core_envelope._build_shared_envelope_for_store(
        store, b"x", item_id="itm-42")
    assert err == "" and out["id"] == "itm-42"


def test_plaintext_shape_gets_an_id_when_caller_omits_it(store, monkeypatch):
    """不传 item_id 时也必须有 id：对账与 swap 都按 id 定位行。"""
    _prefer(monkeypatch, "off")

    out, _ = core_envelope._build_shared_envelope_for_store(store, b"x")

    assert isinstance(out["id"], str) and out["id"]
