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


class PermanentDecryptFailure(Exception):
    """enclave 确定性拒解（HTTP 403 decrypt_failed：错内容钥/owner 不符/坏信封）。

    与 ``PendingDeviceMigration`` 一样是**终态**——但成因不同：PDM 是「密文本就
    只有设备端能解」，本类是「密文本该 enclave 能解、但实际解不开」（翻钥后老钥封
    的密文、owner 绑定不符、结构损坏）。重试/换 token 都是白试（密文与 enclave 钥
    不变则结果不变）。worker 据此把该行**隔离**（写终态 pending 行 + 游标越过），
    不再冻结游标——否则整表回填被队头的一条毒行永久卡死。隔离行留在 pending 表里，
    将来若翻钥使其重新可解，可再驱动重试。"""


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


def _scrub_nul(obj):
    """递归剥掉一切字符串里的 NUL(0x00)——PostgreSQL text/JSONB 存不了它:存 text
    报 "cannot contain NUL",存 JSONB 时它被转义成 backslash-u-0000 报 "unsupported
    Unicode escape sequence"。NUL 不止出现在解密出的 body,也可能在客户端带来的元数据
    字段里(_strip_envelope 原样保留),所以对整个输出 doc(键和值、嵌套 dict/list)统一
    清一遍,而不是只清 body。NUL 在正文/元数据里无语义,剥掉、其余字符原样保留。"""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {_scrub_nul(k): _scrub_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_nul(v) for v in obj]
    return obj


def carry_verbatim(doc: dict) -> dict:
    """加密档用户的行：原样搬进 TEE，不解密、不剥任何字段（Task 2.4）。

    只做 NUL 清理——PostgreSQL 的 text/JSONB 存不了 0x00，这是历史事故
    （tee-sync NUL 卡死重试循环），搬运路径不能重新引入。

    注意本函数**不看行形状**：加密档用户切档前的存量明文行同样原样搬运，去解密
    一行没有 body_ct 的「信封」只会白跑一趟 enclave。
    """
    return _scrub_nul(doc)


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
    return _scrub_nul(out)


def _plaintext_single(doc: dict, decrypt, purpose_prefix: str) -> dict:
    if not _decryptable(doc):
        raise PendingDeviceMigration(str(doc.get("id", "")))
    out = _strip_envelope(doc)
    out["body"] = _decrypt_body(decrypt, doc, f"{purpose_prefix}:{doc.get('id', '')}")
    return _scrub_nul(out)


def plaintext_memory_doc(doc: dict, decrypt) -> dict:
    return _plaintext_single(doc, decrypt, "tee_replicate:memory")


def plaintext_world_book_doc(doc: dict, decrypt) -> dict:
    return _plaintext_single(doc, decrypt, "tee_replicate:world_book")


def plaintext_identity_doc(doc: dict, decrypt) -> dict:
    """user_blobs kind=identity 的信封，与 memory 同型（单信封）。"""
    return _plaintext_single(doc, decrypt, "tee_replicate:identity")


def plaintext_envelope_column(env: dict, decrypt, *, purpose: str) -> dict:
    """通用单信封列 → 明文 dict。

    与 plaintext_memory_doc / plaintext_world_book_doc 同型，区别只在 purpose 由
    调用方给（这些表的信封住在专用列里而不是通用 doc 列，purpose 串要能区分）。
    2026-07-27 全量对齐新增的 6 张 v2/archive 表用它；chat_message_archive 不用
    ——它的 doc 与 chat_messages.doc 完全同形，直接复用 plaintext_chat_doc（含
    thinking/caption 子信封处理）。
    """
    if not _decryptable(env):
        raise PendingDeviceMigration(str(env.get("id", "")))
    out = _strip_envelope(env)
    out["body"] = _decrypt_body(decrypt, env, purpose)
    return _scrub_nul(out)
