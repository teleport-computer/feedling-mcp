"""v1 envelope construction helpers shared by every write path.

The user's content pubkey lives in the accounts registry, which sits
ABOVE core in the dependency stack — the assembly layer (asgi/lifespan.py) injects
``get_user_public_key`` at startup instead of core importing accounts.
"""

import base64
import hashlib

import content_encryption
from content_encryption import build_envelope

from core import enclave


# Injected by the assembly layer: returns the user's base64 X25519 content
# pubkey, or "" if the user predates v1 registration.
def get_user_public_key(user_id: str) -> str:
    raise RuntimeError("core.envelope.get_user_public_key not wired by assembly layer")


# Injected by the assembly layer (accounts sits ABOVE core, so core must not
# import it): the user's **生效**内容形状，"on" | "off"。
#
# 默认 "on" 而不是 raise：写侧的安全失败方向是**加密**。装配层没接线（纯单元测试、
# 某个 worker 进程漏接）时全体走加密，行为与改造前逐字一致；绝不能因为一处漏接线
# 就把用户内容明文落库。
def resolve_content_encryption(user_id: str) -> str:
    return "on"


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


def read_envelope_body(envelope: dict, api_key: str | None, *,
                       purpose: str, runtime_token: str = "") -> bytes:
    """读出内容行的正文，按行形状路由。

    两种形状并存是 TEE 扶正期与 v6 加密可选之后的常态：
      - 信封行（加密档 / 现存量）：有 ``body_ct`` → 走 enclave 解密。
      - 二进制明文行：有 ``body_b64`` → 严格 base64 解码，本地直读。
      - UTF-8 明文行：``{body, id, owner_user_id, visibility}`` → **本地直读**。

    明文分支绝不打 enclave：cutover 后 enclave 只服务加密档用户，明文档的读路径
    不该再依赖它——这既是「读路径不经 enclave 更快」的兑现点，也让 enclave 故障
    不再连坐明文档用户。

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
            envelope, api_key, purpose=purpose, **kwargs)
    if envelope.get("body_b64") is not None:
        try:
            return base64.b64decode(envelope["body_b64"], validate=True)
        except Exception as exc:
            raise ValueError("envelope_body_b64_invalid") from exc
    body = envelope.get("body")
    if isinstance(body, str):
        return body.encode("utf-8")
    raise ValueError("envelope_shape_unrecognized")


def decrypt_provider_key_envelope(envelope: dict, api_key: str | None, *,
                                  runtime_token: str = "") -> bytes:
    """取出 BYOK provider key（Task 1.1）。purpose 钉死的薄 wrapper。"""
    return read_envelope_body(envelope, api_key,
                              purpose="model_api_provider_key",
                              runtime_token=runtime_token)


_SEALED_FIELDS = ("body_ct", "nonce", "K_user", "K_enclave",
                  "enclave_pk_fpr", "content_pk_fpr")
_PLAINTEXT_BINARY_FIELDS = ("body_b64", "body_size_bytes")
_POINTER_FIELDS = ("body_key", "body_object_format", "body_sha256",
                   "body_ct_len", "body_size_bytes")


def envelope_storage_fields(envelope: dict, *,
                            default_owner_user_id: str = "") -> dict:
    """取出信封的**落库字段**，供各写入点合进自己的 doc。

    各写入点原本都硬拆 ``envelope["body_ct"]`` 等固定字段名，一旦写侧改出明文
    形状就 KeyError。本函数按行形状路由，让那些站点变成形状无关：

      - 信封行 → ``body_ct/nonce/K_user/[K_enclave]/[enclave_pk_fpr]/…``
      - 二进制明文行 → ``body_b64/[body_size_bytes]``
      - UTF-8 明文行 → ``body``

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
    elif envelope.get("body_b64") is not None:
        for key in _PLAINTEXT_BINARY_FIELDS:
            if envelope.get(key) is not None:
                out[key] = envelope[key]
    elif envelope.get("body") is not None:
        out["body"] = envelope["body"]
    else:
        raise ValueError("envelope_shape_unrecognized")

    out["owner_user_id"] = envelope.get("owner_user_id") or default_owner_user_id
    out["visibility"] = envelope.get("visibility") or "shared"
    return out


_UPLOAD_COMMON_REQUIRED = ("visibility", "owner_user_id")
_UPLOAD_SEALED_REQUIRED = ("body_ct", "nonce", "K_user", *_UPLOAD_COMMON_REQUIRED)


def upload_shape_gate(envelope, *, user_id: str) -> tuple[tuple[str, ...], dict | None]:
    """客户端上传信封的**形状策略**：返回 ``(该校验的必填字段, 拒绝原因或 None)``。

    抽出来单独一个是因为各写闸的错误消息**格式互不相同**（``envelope_missing_fields``
    带 ``detail`` / 带 ``missing`` / f-string / 加前缀四种方言），而客户端按各自
    的格式读。策略集中在这里，消息仍由各站点自己写。

    形状不认识（既无 ``body_ct`` 也无 ``body``）时按信封报必填——「想传信封但漏了
    字段」是更常见的意图。
    """
    if not isinstance(envelope, dict):
        return _UPLOAD_SEALED_REQUIRED, None
    if envelope.get("body_ct") or envelope.get("body") is None:
        return _UPLOAD_SEALED_REQUIRED, None
    if resolve_content_encryption(user_id) != "off":
        # 否则任何客户端都能单方面把自己的加密档降级成明文——用户在设置页开着
        # 加密、内容却已明文落库。这是本计划最严重失败模式的客户端版本。
        return _UPLOAD_COMMON_REQUIRED, {
            "error": "plaintext_envelope_not_enabled_for_this_account"}
    if envelope.get("visibility") == "local_only":
        # local_only 靠的是没有 K_enclave；一行明文服务端天然读得到，标成
        # local_only 等于给用户一个假的隐私承诺。与 swap 通道同一条边界。
        return _UPLOAD_COMMON_REQUIRED, {
            "error": "plaintext_envelope_cannot_be_local_only"}
    return _UPLOAD_COMMON_REQUIRED, None


def requires_enclave_key(envelope) -> bool:
    """这一行是否受「shared 必须有 K_enclave」那道闸约束（只有信封行受）。"""
    return (isinstance(envelope, dict)
            and bool(envelope.get("body_ct"))
            and envelope.get("visibility") == "shared"
            and not envelope.get("K_enclave"))


def validate_uploaded_envelope(envelope, *, user_id: str) -> dict | None:
    """校验**客户端上传**的内容信封，按行形状路由。

    返回 ``None`` 表示通过，否则是错误 body（调用方补 400）。chat / memory /
    identity 的 9 处写闸原本消息逐字相同，故收成这一个；**信封分支的判据与消息
    全部沿用改造前**，明文分支是新增的。

    明文分支默认**关闭**：只有生效形状为 ``"off"`` 才收。否则任何客户端都能
    单方面把自己的加密档降级成明文——用户在设置页开着加密、内容却已明文落库，
    这是本计划最严重失败模式的客户端版本。
    """
    if not isinstance(envelope, dict):
        return {"error": "envelope required"}

    required, shape_err = upload_shape_gate(envelope, user_id=user_id)
    if shape_err is not None:
        return shape_err

    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return {"error": "envelope_missing_fields", "detail": missing}
    if envelope["visibility"] not in ("shared", "local_only"):
        return {"error": "envelope.visibility must be 'shared' or 'local_only'"}
    if requires_enclave_key(envelope):
        return {"error": "envelope with visibility=shared requires K_enclave"}
    return None


def validate_uploaded_chat_envelope(
    envelope,
    *,
    user_id: str,
    content_type: str,
    max_binary_bytes: int = 10 * 1024 * 1024,
) -> dict | None:
    """Validate chat's file/image-only ``body_b64`` plaintext wire shape.

    Other content families keep using :func:`validate_uploaded_envelope` and
    therefore cannot acquire a binary plaintext shape accidentally.
    """
    if not isinstance(envelope, dict) or envelope.get("body_b64") is None:
        return validate_uploaded_envelope(envelope, user_id=user_id)
    if content_type not in ("file", "image"):
        return {"error": "body_b64_requires_file_or_image"}
    if envelope.get("body_ct") is not None or envelope.get("body") is not None:
        return {"error": "envelope_body_shapes_are_mutually_exclusive"}
    if any(envelope.get(key) is not None for key in _SEALED_FIELDS[1:]):
        return {"error": "plaintext_envelope_cannot_include_crypto_fields"}
    if resolve_content_encryption(user_id) != "off":
        return {"error": "plaintext_envelope_not_enabled_for_this_account"}
    missing = [
        field
        for field in _UPLOAD_COMMON_REQUIRED
        if not envelope.get(field)
    ]
    if missing:
        return {"error": "envelope_missing_fields", "detail": missing}
    if envelope.get("visibility") != "shared":
        return {"error": "plaintext_envelope_cannot_be_local_only"}
    try:
        raw = base64.b64decode(envelope["body_b64"], validate=True)
    except Exception:
        return {"error": "body_b64_invalid_base64"}
    if len(raw) > int(max_binary_bytes):
        return {
            "error": "body_b64_too_large",
            "max_bytes": int(max_binary_bytes),
        }
    declared_size = envelope.get("body_size_bytes")
    if declared_size is not None and (
        type(declared_size) is not int or declared_size != len(raw)
    ):
        return {"error": "body_size_bytes_mismatch"}
    return None


def replace_record_shape(record: dict, envelope: dict) -> None:
    """原地把 ``record`` 的内容字段换成 ``envelope`` 的形状（swap / rewrap 用）。

    与 ``envelope_storage_fields`` 的关键区别：这里要**跨形状**。信封转明文时必须
    把所有密文字段删干净——读侧的规则是 ``body_ct`` 优先于 ``body``，留下一个旧
    ``body_ct`` 就会让读到的永远是换之前的过期内容，而且完全静默。反向同理，
    留下 ``body`` 等于服务端仍能读到本该被加密的正文。

    只碰内容字段：站点自有字段（role / ts / type / content_type…）原样保留。
    """
    for key in _SEALED_FIELDS:
        record.pop(key, None)
    record.pop("body", None)
    record.pop("body_b64", None)
    for key in _POINTER_FIELDS:
        record.pop(key, None)
    record.update(envelope_storage_fields(envelope))
    if envelope.get("body_ct"):
        # 落库行历来恒有此键（缺省空串）：rewrap 的跳过逻辑直接从行上读它。
        record.setdefault("enclave_pk_fpr", "")


def envelope_prefixed_fields(envelope: dict, prefix: str) -> dict:
    """把**子信封**摊平成 ``<prefix>_*`` 字段，合进父聊天行（thinking / caption）。

    与 ``envelope_storage_fields`` 的两个区别：

    1. 子信封没有独立的行，``v`` 与 ``id`` 也必须摊进来（普通信封的这两个字段
       由站点自己写）。
    2. 值一律 str 化——父行的 extra 是 str→str 映射。

    ⚠️ 新增的 ``<prefix>_body``（明文形状）必须同时登记进 ``core/store.py`` 的
    extra 白名单，否则会被**静默丢掉**：thinking/caption 正文凭空消失且不报错。
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope_shape_unrecognized")

    fields = envelope_storage_fields(envelope)
    out = {
        f"{prefix}_v": str(envelope.get("v", 1)),
        f"{prefix}_id": str(envelope.get("id") or ""),
    }
    for key, value in fields.items():
        out[f"{prefix}_{key}"] = str(value)
    return out


def _build_shared_envelope_for_store(
    store,
    plaintext: bytes,
    *,
    item_id: str | None = None,
) -> tuple[dict | None, str]:
    if resolve_content_encryption(store.user_id) == "off":
        # 明文档：不取 enclave 公钥，因此 enclave 故障不连坐明文档用户的写入。
        try:
            body = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            # 明文列是 text；二进制正文走 R2 指针，不该到这里。
            return None, "plaintext_body_not_utf8"
        return {
            "body": body,
            "id": item_id or content_encryption.random_item_id(),
            "owner_user_id": store.user_id,
            "visibility": "shared",
        }, ""

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
