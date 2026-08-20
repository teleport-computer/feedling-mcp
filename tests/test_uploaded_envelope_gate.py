"""客户端上传信封的写闸，按行形状路由（Phase 2 Task 2.2 最后一步）。

chat / memory / identity 的 9 处写闸原本消息**逐字相同**，故收成一个校验器；
信封分支的判据与消息全部沿用改造前，明文分支是新增的。

明文分支默认**关闭**：只有生效形状为 "off" 才收。所以
`PLAINTEXT_WRITES_ACCEPTED is False` 期间，客户端就算发明文也会被拒——这正是
「iOS 先发版也写不坏」的服务端一侧保障。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core import envelope as core_envelope  # noqa: E402


def _sealed(**extra) -> dict:
    env = {"v": 1, "id": "itm-1", "body_ct": "Y3Q=", "nonce": "bm9uY2U=",
           "K_user": "a3U=", "K_enclave": "a2U=",
           "owner_user_id": "usr_owner", "visibility": "shared"}
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
        "id": "itm-binary",
        "owner_user_id": "usr_owner",
        "visibility": "shared",
    }
    env.update(extra)
    return env


def _prefer(monkeypatch, value: str) -> None:
    monkeypatch.setattr(core_envelope, "resolve_content_encryption",
                        lambda uid: value)


def _check(envelope, user_id="usr_owner"):
    return core_envelope.validate_uploaded_envelope(envelope, user_id=user_id)


# --------------------------------------------------------------------------- #
# 信封分支：判据与消息必须逐字沿用改造前
# --------------------------------------------------------------------------- #

def test_valid_sealed_envelope_passes():
    assert _check(_sealed()) is None


def test_missing_sealed_fields_keeps_the_existing_error_shape():
    """三处写闸原本都返回 {"error": "envelope_missing_fields", "detail": [...]}，
    客户端按这个形状读，不能改。"""
    env = _sealed()
    env.pop("nonce")

    err = _check(env)

    assert err == {"error": "envelope_missing_fields", "detail": ["nonce"]}


def test_bad_visibility_keeps_the_existing_message():
    err = _check(_sealed(visibility="whatever"))
    assert err == {"error": "envelope.visibility must be 'shared' or 'local_only'"}


def test_shared_sealed_still_requires_k_enclave():
    """加密档的既有闸不能因为放开明文而松掉。消息逐字沿用。"""
    env = _sealed()
    env.pop("K_enclave")

    err = _check(env)

    assert err == {"error": "envelope with visibility=shared requires K_enclave"}


def test_local_only_sealed_needs_no_k_enclave():
    env = _sealed(visibility="local_only")
    env.pop("K_enclave")
    assert _check(env) is None


def test_missing_envelope_is_rejected():
    assert _check(None) == {"error": "envelope required"}


# --------------------------------------------------------------------------- #
# 明文分支：默认关闭
# --------------------------------------------------------------------------- #

def test_plaintext_is_rejected_while_the_account_is_encrypted(monkeypatch):
    """生效形状为 on 时，客户端发明文必须被拒。

    否则任何客户端都能**单方面把自己的加密档降级成明文**——用户在设置页开着
    加密，实际内容已经明文落库。这是本计划最严重的失败模式的客户端版本。
    """
    _prefer(monkeypatch, "on")

    err = _check(_plain())

    assert err is not None
    assert err["error"] == "plaintext_envelope_not_enabled_for_this_account"


def test_plaintext_is_accepted_once_the_account_is_plaintext(monkeypatch):
    _prefer(monkeypatch, "off")
    assert _check(_plain()) is None


def test_plaintext_cannot_be_local_only(monkeypatch):
    """明文 + local_only 是假的隐私承诺——local_only 靠的是没有 K_enclave，
    而一行明文服务端天然读得到。与 swap 通道同一条边界。"""
    _prefer(monkeypatch, "off")

    err = _check(_plain(visibility="local_only"))

    assert err == {"error": "plaintext_envelope_cannot_be_local_only"}


def test_plaintext_still_needs_owner_and_visibility(monkeypatch):
    _prefer(monkeypatch, "off")
    env = _plain()
    env.pop("owner_user_id")

    err = _check(env)

    assert err == {"error": "envelope_missing_fields", "detail": ["owner_user_id"]}


def test_unknown_shape_reports_the_sealed_field_set(monkeypatch):
    """既无 body_ct 也无 body：按信封报缺失，把「想传信封但漏了字段」这个更
    常见的意图放在前面。"""
    _prefer(monkeypatch, "off")

    err = _check({"visibility": "shared", "owner_user_id": "usr_owner"})

    assert err["error"] == "envelope_missing_fields"
    assert "body_ct" in err["detail"]


def test_empty_plaintext_body_is_valid(monkeypatch):
    """空正文是合法内容（只有附件的消息）。真值判会把它当成形状不认识。"""
    _prefer(monkeypatch, "off")
    assert _check(_plain(body="")) is None


def test_sealed_wins_when_both_shapes_present(monkeypatch):
    """两者并存按信封校验——与读侧 body_ct 优先一致。"""
    _prefer(monkeypatch, "on")
    assert _check(_sealed(body="stale")) is None


# --------------------------------------------------------------------------- #
# Chat-only binary plaintext shape
# --------------------------------------------------------------------------- #

def _check_chat(envelope, content_type="file"):
    return core_envelope.validate_uploaded_chat_envelope(
        envelope,
        user_id="usr_owner",
        content_type=content_type,
    )


def test_binary_plaintext_is_chat_file_image_only(monkeypatch):
    _prefer(monkeypatch, "off")

    assert _check_chat(_binary_plain(), "file") is None
    assert _check_chat(_binary_plain(), "image") is None
    assert _check_chat(_binary_plain(), "text") == {
        "error": "body_b64_requires_file_or_image"
    }
    assert _check(_binary_plain())["error"] == "envelope_missing_fields"


def test_binary_plaintext_respects_effective_encryption_gate(monkeypatch):
    _prefer(monkeypatch, "on")
    assert _check_chat(_binary_plain()) == {
        "error": "plaintext_envelope_not_enabled_for_this_account"
    }


@pytest.mark.parametrize(
    ("env", "error"),
    [
        (_binary_plain(body_b64="not base64!"), "body_b64_invalid_base64"),
        (_binary_plain(body="mixed"), "envelope_body_shapes_are_mutually_exclusive"),
        (_binary_plain(nonce="n"), "plaintext_envelope_cannot_include_crypto_fields"),
        (_binary_plain(visibility="local_only"), "plaintext_envelope_cannot_be_local_only"),
        (_binary_plain(body_size_bytes=3), "body_size_bytes_mismatch"),
    ],
)
def test_binary_plaintext_rejects_invalid_shapes(monkeypatch, env, error):
    _prefer(monkeypatch, "off")
    assert _check_chat(env)["error"] == error


def test_binary_plaintext_enforces_decoded_byte_limit(monkeypatch):
    _prefer(monkeypatch, "off")
    err = core_envelope.validate_uploaded_chat_envelope(
        _binary_plain(),
        user_id="usr_owner",
        content_type="file",
        max_binary_bytes=1,
    )
    assert err == {"error": "body_b64_too_large", "max_bytes": 1}
