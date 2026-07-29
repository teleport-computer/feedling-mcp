"""v1 envelope construction helpers shared by every write path.

The user's content pubkey lives in the accounts registry, which sits
ABOVE core in the dependency stack — the assembly layer (asgi/lifespan.py) injects
``get_user_public_key`` at startup instead of core importing accounts.
"""

import base64
import hashlib

from content_encryption import build_envelope

from core import enclave


# Injected by the assembly layer: returns the user's base64 X25519 content
# pubkey, or "" if the user predates v1 registration.
def get_user_public_key(user_id: str) -> str:
    raise RuntimeError("core.envelope.get_user_public_key not wired by assembly layer")


# 服务端是否接受明文形状的内容写入。**Task 2.2 完成前必须是 False**：客户端
# 写闸仍硬校验 K_enclave（worldbook_core._validate_envelope:51 等），明文写入
# 会 400。这里是唯一的开关点——翻成 True 前先确认所有写闸都已接受明文。
PLAINTEXT_WRITES_ACCEPTED = False


def _decode_content_public_key(public_key: str) -> tuple[bytes | None, str]:
    raw = (public_key or "").strip()
    if not raw:
        return None, "public_key required"
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "public_key invalid base64"
    if len(decoded) != 32:
        return None, "public_key must decode to 32 bytes"
    return decoded, ""


def _content_public_key_fingerprint(public_key: str | bytes | None) -> str:
    if public_key is None:
        return ""
    if isinstance(public_key, str):
        key_bytes, err = _decode_content_public_key(public_key)
        if err or key_bytes is None:
            return "invalid"
    else:
        key_bytes = public_key
    return hashlib.sha256(key_bytes).hexdigest()[:16]


def _model_api_key_encryption_material(store) -> tuple[bytes, bytes] | tuple[None, str]:
    user_pk_b64 = get_user_public_key(store.user_id)
    if not user_pk_b64:
        return None, "user_content_public_key_missing"
    try:
        user_pk = base64.b64decode(user_pk_b64)
    except Exception:
        return None, "user_content_public_key_invalid_base64"
    if len(user_pk) != 32:
        return None, "user_content_public_key_invalid_length"

    enclave_info = enclave._get_enclave_info()
    if not enclave_info:
        return None, "enclave_info_unavailable"
    try:
        enclave_pk = bytes.fromhex(str(enclave_info.get("content_pk_hex") or ""))
    except Exception:
        return None, "enclave_content_public_key_invalid_hex"
    if len(enclave_pk) != 32:
        return None, "enclave_content_public_key_invalid_length"
    return user_pk, enclave_pk


def decrypt_provider_key_envelope(envelope: dict, api_key: str | None, *,
                                  runtime_token: str = "") -> bytes:
    """取出 BYOK provider key，按行形状路由。

    两种形状并存是 TEE 扶正期的常态：
      - RDS（现状真源）：双收件人信封，有 ``body_ct`` → 走 enclave 解密。
      - TEE 主库：表同步在复制时已解密，形状是
        ``{body, id, owner_user_id, visibility}``、无 ``body_ct`` → **本地直读**。

    明文分支绝不打 enclave：cutover 后 enclave 只服务加密档用户，明文档的读路径
    不该再依赖它（也是「读路径不经 enclave 更快」的兑现点）。

    ``body_ct`` 优先于 ``body``：两者并存只可能出现在迁移中间态，此时密文是真源，
    反过来会读到过期的明文残留。
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope_shape_unrecognized")
    if envelope.get("body_ct"):
        # 只在 runtime_token 非空时透传：api-key 调用方的下游入参必须与改造前
        # 逐字一致（原调用点用 `**decrypt_kwargs` 达到同样效果）。无脑传空串会
        # 让 tests/test_model_api_profiles_config_store.py 的断言失败。
        kwargs = {"runtime_token": runtime_token} if runtime_token else {}
        return enclave._decrypt_envelope_via_enclave(
            envelope, api_key, purpose="model_api_provider_key", **kwargs)
    body = envelope.get("body")
    if isinstance(body, str):
        return body.encode("utf-8")
    raise ValueError("envelope_shape_unrecognized")


_SEALED_FIELDS = ("body_ct", "nonce", "K_user", "K_enclave",
                  "enclave_pk_fpr", "content_pk_fpr")


def envelope_storage_fields(envelope: dict, *,
                            default_owner_user_id: str = "") -> dict:
    """取出信封的**落库字段**，供各写入点合进自己的 doc。

    各写入点原本都硬拆 ``envelope["body_ct"]`` 等固定字段名，一旦写侧改出明文
    形状就 KeyError。本函数按行形状路由，让那些站点变成形状无关：

      - 信封行 → ``body_ct/nonce/K_user/[K_enclave]/[enclave_pk_fpr]/…``
      - 明文行 → ``body``

    站点自有字段（id / role / ts / type / source…）仍由调用方写——迁移因此是纯
    机械的，且在写侧形状真正切换前**行为逐字不变**。

    ``body_ct`` 优先于 ``body``：两者并存只出现在迁移中间态，此时密文是真源。
    缺失的可选字段**整键省略**而不是写 None——尾账判据「有 K_user 无 K_enclave
    = 孤岛」靠的正是键在不在。
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope_shape_unrecognized")

    out: dict = {}
    if envelope.get("body_ct"):
        for key in _SEALED_FIELDS:
            if envelope.get(key) is not None:
                out[key] = envelope[key]
    elif envelope.get("body") is not None:
        out["body"] = envelope["body"]
    else:
        raise ValueError("envelope_shape_unrecognized")

    out["owner_user_id"] = envelope.get("owner_user_id") or default_owner_user_id
    out["visibility"] = envelope.get("visibility") or "shared"
    return out


def _build_shared_envelope_for_store(
    store,
    plaintext: bytes,
    *,
    item_id: str | None = None,
) -> tuple[dict | None, str]:
    material = _model_api_key_encryption_material(store)
    if material[0] is None:
        return None, str(material[1])
    user_pk, enclave_pk = material  # type: ignore[misc]
    try:
        return build_envelope(
            plaintext=plaintext,
            owner_user_id=store.user_id,
            user_pk_bytes=user_pk,  # type: ignore[arg-type]
            enclave_pk_bytes=enclave_pk,  # type: ignore[arg-type]
            visibility="shared",
            item_id=item_id,
        ), ""
    except Exception as e:
        return None, f"envelope_build_failed:{type(e).__name__}:{str(e)[:160]}"


def _enclave_content_public_key_material() -> tuple[bytes | None, str, str]:
    enclave_info = enclave._get_enclave_info()
    if not enclave_info:
        return None, "", "enclave_info_unavailable"
    raw_hex = str(enclave_info.get("content_pk_hex") or "")
    try:
        enclave_pk = bytes.fromhex(raw_hex)
    except Exception:
        return None, "", "enclave_content_public_key_invalid_hex"
    if len(enclave_pk) != 32:
        return None, "", "enclave_content_public_key_invalid_length"
    return enclave_pk, _content_public_key_fingerprint(enclave_pk), ""
