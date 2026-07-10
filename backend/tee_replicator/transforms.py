"""密文 doc → 明文 doc。纯函数 + 注入 decrypt 回调，方便测试。

字段决策（对照 core/store.py:334-421 的权威写路径）：

- **信封加密学字段一律丢弃**（绝不残留进明文 doc，测试守这条）：
  ``v / body_ct / nonce / K_user / K_enclave / enclave_pk_fpr / content_pk_fpr``。
  这些是 AEAD 载荷 + 包装密钥，TEE 明文库读路径不再过 enclave，留着既无用又危险。
- **语义/元数据字段一律保留**：``id / role / ts / source / content_type /
  visibility / owner_user_id / occurred_at / importance / …`` 等明文元数据原样透传。
- **子信封**（chat 的 ``thinking_*`` / ``caption_*``）：剥前缀后同样丢加密学字段、
  保留语义字段（``kind / source / model / native / id / visibility / owner_user_id``），
  解密出的正文塞进 ``out[key]["body"]``，整体嵌套成 ``out["thinking"]`` / ``out["caption"]``。

``decrypt(envelope: dict, purpose: str) -> bytes``：worker 注入的回调，把信封子集
交给 enclave；测试里打桩成可预测映射。
"""
from __future__ import annotations

# AEAD 载荷 + 包装密钥 + 信封版本/指纹：解密后即无意义，绝不写进明文 doc。
_ENVELOPE_KEYS = {"v", "body_ct", "nonce", "K_user", "K_enclave",
                  "enclave_pk_fpr", "content_pk_fpr"}
# enclave 解密时需要随信封一起递交的字段（AEAD 附加数据绑定 owner||v||id）。
_DECRYPT_EXTRA = {"owner_user_id", "id", "visibility"}
_SUB_PREFIXES = (("thinking_", "thinking"), ("caption_", "caption"))


class PendingDeviceMigration(Exception):
    """local_only / 无 K_enclave：enclave 解不了，转设备重传流程（D1）。"""


def _decryptable(env: dict) -> bool:
    """enclave 能解密 ⟺ 非 local_only 且带 K_enclave（否则只有设备端 K_user 能解）。"""
    return env.get("visibility") != "local_only" and bool(env.get("K_enclave"))


def _envelope_subset(env: dict) -> dict:
    """挑出交给 enclave 的字段（加密学字段 + AEAD 绑定的 owner/id/visibility）。"""
    return {k: v for k, v in env.items() if k in _ENVELOPE_KEYS or k in _DECRYPT_EXTRA}


def _strip_envelope(doc: dict) -> dict:
    """丢加密学字段，保留一切语义字段。"""
    return {k: v for k, v in doc.items() if k not in _ENVELOPE_KEYS}


def _sub_envelope(doc: dict, prefix: str) -> dict | None:
    """把 ``thinking_*`` / ``caption_*`` 前缀展开成独立信封 dict；缺 body_ct 视为不存在。"""
    if f"{prefix}body_ct" not in doc:
        return None
    return {k[len(prefix):]: v for k, v in doc.items() if k.startswith(prefix)}


def _decrypt_body(decrypt, env: dict, purpose: str) -> str:
    return decrypt(_envelope_subset(env), purpose=purpose).decode("utf-8", "replace")


def plaintext_chat_doc(doc: dict, decrypt) -> dict:
    """chat 行：主信封 + 可选 thinking / caption 子信封，全部明文化。"""
    if not _decryptable(doc):
        raise PendingDeviceMigration(str(doc.get("id", "")))
    msg_id = str(doc.get("id", ""))
    out = _strip_envelope(doc)
    # 子信封的前缀字段不该留在顶层——它们各自嵌套进 out[key]。
    out = {k: v for k, v in out.items()
           if not (k.startswith("thinking_") or k.startswith("caption_"))}
    out["body"] = _decrypt_body(decrypt, doc, f"tee_replicate:chat:{msg_id}")
    for prefix, key in _SUB_PREFIXES:
        sub = _sub_envelope(doc, prefix)
        if sub is None:
            continue
        if not _decryptable(sub):
            raise PendingDeviceMigration(f"{msg_id}:{key}")
        meta = {k: v for k, v in sub.items() if k not in _ENVELOPE_KEYS}
        meta["body"] = _decrypt_body(decrypt, sub, f"tee_replicate:chat_{key}:{msg_id}")
        meta.setdefault("visibility", out.get("visibility", "shared"))
        out[key] = meta
    return out


def _plaintext_single(doc: dict, decrypt, purpose_prefix: str) -> dict:
    if not _decryptable(doc):
        raise PendingDeviceMigration(str(doc.get("id", "")))
    out = _strip_envelope(doc)
    out["body"] = _decrypt_body(decrypt, doc, f"{purpose_prefix}:{doc.get('id', '')}")
    return out


def plaintext_memory_doc(doc: dict, decrypt) -> dict:
    return _plaintext_single(doc, decrypt, "tee_replicate:memory")


def plaintext_world_book_doc(doc: dict, decrypt) -> dict:
    return _plaintext_single(doc, decrypt, "tee_replicate:world_book")


def plaintext_identity_doc(doc: dict, decrypt) -> dict:
    """user_blobs kind=identity 的信封，与 memory 同型（单信封）。"""
    return _plaintext_single(doc, decrypt, "tee_replicate:identity")
