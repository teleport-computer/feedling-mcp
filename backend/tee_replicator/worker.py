"""游标驱动的密文→明文复制 worker（spec §5.2）。

每张表一条 ``(sort, id)`` 复合游标（存 TEE ``tee_replication_cursors``），只追加式
向前扫描 RDS 密文行，逐行调 enclave 解密（``transforms``），把明文 doc upsert 进 TEE。

游标编码（游标表只有 watermark_ts DOUBLE + watermark_id TEXT 两列）：
  - chat：sort=ts（DOUBLE）→ watermark_ts=ts，watermark_id=msg_id；``(ts,msg_id)`` 键集。
  - memory/world_book：sort=occurred_at/updated_at（TEXT）→ watermark_ts=0，
    watermark_id=``"{sort}\\x00{id}"`` 复合（NUL 分隔，ISO 时间戳与 hex id 均不含 NUL）。
  - identity：单列 user_id 游标（user_blobs kind=identity 一行/用户）。

只覆盖「向前追加」——memory/world_book 的原地改写（back-dated / rewrap 戳 / visibility
swap）由 Task 3 双写的明文安全操作 + Task 6 抽样比对兜底（brief 定案）。

reconciler 用 db.get_pool()/mirror.get_tee_pool() 直连；replicator 同理，但**写失败要
炸**（不是尽力而为的 mirror.execute）——所以整批写 + 游标推进在一个 TEE 事务里，失败
则整批回滚、游标不动，下次重跑。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import db
from psycopg.types.json import Jsonb
from tee_shadow import mirror

from tee_replicator import transforms

log = logging.getLogger("feedling.tee_replicator")

BATCH = 500
_RETRIES = 2                 # 单行 decrypt 额外重试次数（共 1+2=3 次尝试）
_NUL = "\x00"                # 文本复合游标分隔符
# runtime token 铸 900s TTL；缓存条目超过 600s 就重铸（留足余量——长跑 pass 在
# qps=2 × BATCH=500 下每批 sleep ~250s，第 2-3 批就会越过 900s TTL）。
_TOKEN_MAX_AGE = 600.0

# 注入点：测试 monkeypatch 这两个符号（decrypt 打桩 / 限速时钟）。
_sleep = time.sleep
# user_id -> (mint_time, decrypt_fn)：TTL 感知缓存，见 _get_decrypt。
_decrypt_cache: dict[str, tuple[float, Callable]] = {}
# user_id -> (mint_time, reencrypt_fn)：frames 存储层重加密的同型缓存，见 _get_reencrypt。
_reencrypt_cache: dict[str, tuple[float, Callable]] = {}

_PENDING_UPSERT = (
    "INSERT INTO tee_pending_device_migration "
    "(user_id, table_name, item_id, reason, marked_at) VALUES (%s,%s,%s,%s, now()) "
    "ON CONFLICT (user_id, table_name, item_id) DO UPDATE SET "
    "reason = EXCLUDED.reason, marked_at = now()"
)
_CURSOR_UPSERT = (
    "INSERT INTO tee_replication_cursors "
    "(table_name, watermark_ts, watermark_id, updated_at) VALUES (%s,%s,%s, now()) "
    "ON CONFLICT (table_name) DO UPDATE SET "
    "watermark_ts = EXCLUDED.watermark_ts, watermark_id = EXCLUDED.watermark_id, "
    "updated_at = now()"
)
# requeue lane consume: reason LIKE 'requeue%' 的行是「同 PK 原地改写」标记，
# 由双写侧（set_blob→identity/service、content swap、memory/world_book_replace_all）
# 落下。terminal 的 visibility_local_only / PendingDeviceMigration（固定
# "pdm:" 前缀，见 _pdm_reason）reason 都不匹配 'requeue%' 前缀，不被消费。
_REQUEUE_SELECT = (
    "SELECT user_id, item_id FROM tee_pending_device_migration "
    "WHERE table_name = %s AND reason LIKE %s"
)
_PENDING_DELETE = (
    "DELETE FROM tee_pending_device_migration "
    "WHERE user_id = %s AND table_name = %s AND item_id = %s"
)
_PENDING_UPDATE_REASON = (
    "UPDATE tee_pending_device_migration SET reason = %s, marked_at = now() "
    "WHERE user_id = %s AND table_name = %s AND item_id = %s"
)
# Terminal-reason prefix for PendingDeviceMigration rows (local_only / no
# K_enclave). transforms.py raises PendingDeviceMigration(str(doc["id"])) —
# the message is a client-controlled item id, which in principle could itself
# start with "requeue". Without a fixed, never-"requeue"-prefixed marker here,
# such a row would falsely match _REQUEUE_SELECT's ``reason LIKE 'requeue%'``
# and get wrongly picked up (and "consumed") by _consume_requeue as if it were
# an in-place-rewrite marker, instead of staying terminal.
_PDM_REASON_PREFIX = "pdm:"


def _pdm_reason(exc: Exception) -> str:
    return f"{_PDM_REASON_PREFIX}{str(exc) or 'local_only_or_no_k_enclave'}"


@dataclass(frozen=True)
class _Table:
    select_sql: str                              # WHERE <cursor> ORDER BY ... LIMIT %s
    cursor_kind: str                             # "numeric" | "text" | "single"
    upsert_sql: str
    unpack: Callable[[tuple], tuple]             # row -> (user_id, item_id, sort_val, doc)
    # decrypt-and-plaintext path (chat/memory/world_book/identity):
    transform: Callable[[dict, Callable], dict] | None = None
    upsert_args: Callable[[str, str, object, dict], tuple] | None = None
    # storage-re-encrypt path (frames): produces the upsert args tuple directly,
    # doing its own R2/enclave side effects. Takes (user_id, item_id, sort_val,
    # doc, dry_run) → upsert args tuple, or None for a dry_run "would copy".
    # When set, transform/upsert_args are unused. See _frames_row_writer.
    row_writer: Callable | None = None
    # requeue lane (in-place-rewrite compensation, see _consume_requeue): fetch
    # the CURRENT RDS row by its stable PK (the append-only cursor never revisits
    # a same-PK rewrite). identity has no per-item id column → fetch by user_id.
    requeue_fetch_sql: str | None = None       # params: (user_id, item_id) or (user_id,)
    requeue_delete_tee_sql: str | None = None  # drop the TEE row when RDS row is gone
    requeue_by_user_only: bool = False          # identity: key on user_id alone


_SEQ_KEY = "_replicator_seq"  # smuggled through the plaintext doc dict, see below


def _chat_unpack(r: tuple) -> tuple:
    """(user_id, msg_id, ts, doc, seq) row -> (user_id, msg_id, ts, doc').

    ``seq`` has no slot in the generic (user_id, item_id, sort_val, doc) contract
    that run_table/_consume_requeue destructure into, so it rides along as a
    reserved key inside the doc dict handed to transforms.plaintext_chat_doc.
    That's safe: transforms only strips known envelope-crypto keys (_ENVELOPE_KEYS)
    and copies everything else through untouched, so ``_replicator_seq`` survives
    decryption intact; chat_messages' upsert_args below pops it back out before
    the row is written, so it never lands in the stored plaintext ``doc`` JSONB.

    R2-offloaded file rows（content_type="file"，doc 只带 ``body_key`` 指针、无
    ``body_ct``，见 db.chat_append 的 offload）在这里水合回 body_ct 再交给
    transform——否则 plaintext_chat_doc 送 enclave 解密一个没有 body_ct 的信封，
    失败按传输错误处理会把游标永久冻在这行上。水合失败（R2 瞬时故障）时 doc 原样
    返回、transform 照常失败 → freeze → 下个 pass 重试，与其余瞬时错误同策略。
    unpack 同时服务 run_table 游标环和 _consume_requeue，两条路径一并覆盖。
    """
    uid, msg_id, ts, doc, seq = r
    if db._is_chat_file_pointer(doc):
        doc = db.hydrate_chat_file_body(uid, doc)
    return (uid, msg_id, ts, {**doc, _SEQ_KEY: seq})


def _chat_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    seq = doc.pop(_SEQ_KEY)
    return (uid, seq, iid, sort, Jsonb(doc))


_TABLES: dict[str, _Table] = {
    "chat_messages": _Table(
        select_sql=("SELECT user_id, msg_id, ts, doc, seq FROM chat_messages "
                    "WHERE (ts, msg_id) > (%s, %s) ORDER BY ts, msg_id LIMIT %s"),
        cursor_kind="numeric",
        transform=transforms.plaintext_chat_doc,
        # seq is GENERATED ALWAYS AS IDENTITY on TEE too (0001_tee_baseline.py) —
        # the INSERT branch carries RDS's seq verbatim via OVERRIDING SYSTEM
        # VALUE (so replay order matches RDS, not TEE arrival order). The
        # ON CONFLICT DO UPDATE branch deliberately does NOT touch seq:
        # PostgreSQL rejects any explicit assignment to a GENERATED ALWAYS
        # identity column outside of an INSERT's OVERRIDING SYSTEM VALUE
        # clause — that clause has no equivalent for UPDATE/ON CONFLICT DO
        # UPDATE, so ``SET seq = EXCLUDED.seq`` here would be a hard SQL
        # error. This is fine: a conflict means the same (user_id, msg_id)
        # row was already inserted with the correct seq the first time
        # (upserts are idempotent replays of the same watermark range), so
        # the existing seq is already right and simply needs to survive.
        upsert_sql=("INSERT INTO chat_messages (user_id, seq, msg_id, ts, doc) "
                    "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id, msg_id) DO UPDATE SET ts=EXCLUDED.ts, doc=EXCLUDED.doc"),
        unpack=_chat_unpack,
        upsert_args=_chat_upsert_args,
        requeue_fetch_sql=("SELECT user_id, msg_id, ts, doc, seq FROM chat_messages "
                           "WHERE user_id = %s AND msg_id = %s"),
        requeue_delete_tee_sql="DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s",
    ),
    "memory_moments": _Table(
        select_sql=("SELECT user_id, moment_id, occurred_at, doc FROM memory_moments "
                    "WHERE (occurred_at, moment_id) > (%s, %s) "
                    "ORDER BY occurred_at, moment_id LIMIT %s"),
        cursor_kind="text",
        transform=transforms.plaintext_memory_doc,
        upsert_sql=("INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, moment_id) DO UPDATE SET "
                    "occurred_at=EXCLUDED.occurred_at, doc=EXCLUDED.doc"),
        unpack=lambda r: (r[0], r[1], r[2], r[3]),
        upsert_args=lambda uid, iid, sort, doc: (uid, iid, sort or "", Jsonb(doc)),
        requeue_fetch_sql=("SELECT user_id, moment_id, occurred_at, doc FROM memory_moments "
                           "WHERE user_id = %s AND moment_id = %s"),
        requeue_delete_tee_sql="DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s",
    ),
    "world_book_entries": _Table(
        select_sql=("SELECT user_id, entry_id, updated_at, doc FROM world_book_entries "
                    "WHERE (updated_at, entry_id) > (%s, %s) "
                    "ORDER BY updated_at, entry_id LIMIT %s"),
        cursor_kind="text",
        transform=transforms.plaintext_world_book_doc,
        upsert_sql=("INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, entry_id) DO UPDATE SET "
                    "updated_at=EXCLUDED.updated_at, doc=EXCLUDED.doc"),
        unpack=lambda r: (r[0], r[1], r[2], r[3]),
        upsert_args=lambda uid, iid, sort, doc: (uid, iid, sort or "", Jsonb(doc)),
        requeue_fetch_sql=("SELECT user_id, entry_id, updated_at, doc FROM world_book_entries "
                           "WHERE user_id = %s AND entry_id = %s"),
        requeue_delete_tee_sql="DELETE FROM world_book_entries WHERE user_id = %s AND entry_id = %s",
    ),
    # identity：user_blobs kind=identity，一行/用户，无排序列 → 单列 user_id 游标。
    "identity": _Table(
        select_sql=("SELECT user_id, doc FROM user_blobs "
                    "WHERE kind = 'identity' AND user_id > %s ORDER BY user_id LIMIT %s"),
        cursor_kind="single",
        transform=transforms.plaintext_identity_doc,
        upsert_sql=("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, 'identity', %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET doc=EXCLUDED.doc"),
        unpack=lambda r: (r[0], "identity", r[0], r[1]),
        upsert_args=lambda uid, iid, sort, doc: (uid, Jsonb(doc)),
        requeue_fetch_sql=("SELECT user_id, doc FROM user_blobs "
                           "WHERE kind = 'identity' AND user_id = %s"),
        requeue_delete_tee_sql="DELETE FROM user_blobs WHERE user_id = %s AND kind = 'identity'",
        requeue_by_user_only=True,
    ),
}


# --------------------------------------------------------------------------- #
# enclave decrypt 回调（每用户一个，缓存复用 token）。测试 monkeypatch _make_decrypt。
# --------------------------------------------------------------------------- #
def _make_decrypt(user_id: str) -> Callable[[dict, str], bytes]:
    """铸一枚 user 作用域的 runtime token，返回 ``decrypt(envelope, purpose)->bytes``。

    真实签名：``core.enclave._decrypt_envelope_via_enclave(envelope, api_key, *,
    purpose, runtime_token)`` 成功返回明文 bytes、失败 raise RuntimeError
    （brief 骨架里的 ``pt, err =`` 元组返回形是错的，实际是 raise-or-bytes）。
    托管零 roster（host-all）下没有 per-user api_key，只能用 runtime token
    （scope=envelope_decrypt，见 supervisor.py 的 mint_token 用法）。
    """
    from core import enclave as core_enclave

    token = _mint_runtime_token(user_id)

    def decrypt(envelope: dict, purpose: str) -> bytes:
        return core_enclave._decrypt_envelope_via_enclave(
            envelope, None, purpose=purpose, runtime_token=token)

    return decrypt


def _mint_runtime_token(user_id: str) -> str:
    """铸一枚 user 作用域的 runtime token（decrypt / storage-reencrypt 共用）。

    whoami_live 只做本地 HMAC 校验取 user_id，不校验 scope——decrypt 与
    reencrypt 端点都只认 owner==caller，故 scope 名沿用 ``envelope_decrypt``。"""
    import os

    from core import runtime_token

    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET unset — cannot mint enclave token")
    return runtime_token.mint(
        secret, user_id=user_id, runtime_instance_id="tee_replicator",
        scope=["envelope_decrypt"], ttl=900.0)


def _make_reencrypt(user_id: str) -> Callable[[dict, str], dict]:
    """铸 token，返回 ``reencrypt(envelope, key_version) -> dict``（frames D4）。
    真实签名：``core.enclave._reencrypt_frame_via_enclave`` 成功返回
    ``{body_ct_storage, key_version, sha256, size}``、失败 raise RuntimeError。
    测试 monkeypatch 本符号。"""
    from core import enclave as core_enclave

    token = _mint_runtime_token(user_id)

    def reencrypt(envelope: dict, key_version: str) -> dict:
        return core_enclave._reencrypt_frame_via_enclave(
            envelope, None, key_version=key_version, runtime_token=token)

    return reencrypt


def _get_reencrypt(user_id: str, *, fresh: bool = False) -> Callable[[dict, str], dict]:
    """TTL 感知的 per-user reencrypt 缓存（与 _get_decrypt 同型，独立字典避免
    与 decrypt 闭包串味）。"""
    now = time.time()
    hit = _reencrypt_cache.get(user_id)
    if not fresh and hit is not None and now - hit[0] <= _TOKEN_MAX_AGE:
        return hit[1]
    fn = _make_reencrypt(user_id)
    _reencrypt_cache[user_id] = (now, fn)
    return fn


def _get_decrypt(user_id: str, *, fresh: bool = False) -> Callable[[dict, str], bytes]:
    """TTL 感知的 per-user decrypt 缓存。

    token 铸 900s TTL，缓存条目超过 _TOKEN_MAX_AGE(600s) 即重铸——否则长跑 pass
    （批间 sleep 数百秒）第 2-3 批起 token 必过期，重试复用同一枚 stale token
    → 行必败 → 游标冻结 → pass 中断。``fresh=True`` 强制重铸（auth 失败路径）。
    """
    now = time.time()
    hit = _decrypt_cache.get(user_id)
    if not fresh and hit is not None and now - hit[0] <= _TOKEN_MAX_AGE:
        return hit[1]
    fn = _make_decrypt(user_id)
    _decrypt_cache[user_id] = (now, fn)
    return fn


def _is_auth_error(exc: Exception) -> bool:
    """401/403 或 token 形状的失败——重试前值得换枚新 token 再试。

    enclave 客户端把 HTTP 错误包成 ``RuntimeError("enclave_http_<code>:<body>")``
    （core/enclave.py），过期 token 走 401；错误体里也可能带 token_expired 字样。
    """
    msg = str(exc)
    return ("enclave_http_401" in msg or "enclave_http_403" in msg
            or "token_expired" in msg or "TokenError" in msg)


def _transform_with_retry(cfg: _Table, doc: dict, user_id: str) -> dict:
    """PendingDeviceMigration 是确定性的，立即上抛不重试；其余（网络/enclave）重试。

    auth 形状的失败（401/token 过期）在重试前强制重铸 token——同一枚 stale token
    重试多少次都是白试。
    """
    decrypt = _get_decrypt(user_id)
    last: Exception | None = None
    for _ in range(_RETRIES + 1):
        try:
            return cfg.transform(doc, decrypt)
        except transforms.PendingDeviceMigration:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if _is_auth_error(e):
                decrypt = _get_decrypt(user_id, fresh=True)
    assert last is not None
    raise last


def _reencrypt_with_retry(user_id: str, envelope: dict) -> dict:
    """frames 存储层重加密 + auth 重试（与 _transform_with_retry 同策略）。
    PendingDeviceMigration 由 frames.replicate 在调用本函数前分类，不到这。"""
    fn = _get_reencrypt(user_id)
    last: Exception | None = None
    for _ in range(_RETRIES + 1):
        try:
            return fn(envelope, "v1")
        except Exception as e:  # noqa: BLE001
            last = e
            if _is_auth_error(e):
                fn = _get_reencrypt(user_id, fresh=True)
    assert last is not None
    raise last


def _frames_row_writer(user_id: str, frame_id: str, sort_val, doc: dict,
                       dry_run: bool):
    """_TABLES["frame_envelopes"].row_writer：委托 frames.replicate，注入带重试的
    reencrypt 回调。返回 frames upsert_sql 的 9 元参数，或 dry_run 下 None。"""
    from tee_replicator import frames

    def reencrypt(envelope: dict, key_version: str) -> dict:
        return _reencrypt_with_retry(user_id, envelope)

    return frames.replicate(user_id, frame_id, float(sort_val or 0.0), doc,
                            reencrypt, dry_run=dry_run)


# frames：R2/inline 双形态 → 存储层重加密 → TEE frames 指针行（spec §4 / D4）。
# 排序键 (ts, frame_id) 同 chat（numeric 游标）；unpack 把整行三形态字段打包进
# "doc" 交给 row_writer。row_writer 委托 tee_replicator.frames.replicate，故这里
# 无需 transform/upsert_args。upsert_sql 的 9 列与 frames.replicate 返回元组对齐，
# TEE 写 + 游标推进仍在 run_table 的单事务里（本体密文已先落 R2，只写指针）。
#
# requeue_delete_tee_sql：frame_envelopes 没有 requeue_fetch_sql（/v1/content/swap
# 只支持 chat/memory，frames 没有 visibility-swap 入口；见 content_core.swap 的
# itype in ("chat","memory") 校验），所以本表永远不会走 _consume_requeue。这条
# SQL 只在 run_table 的游标环 PDM 分支里被复用（run_table 对所有表通用地删 TEE
# 行）——纯防御性：分析下来 frames 没有「先被复制成 TEE 明文指针、后来变成不可
# 解」的现实路径（orphan/r2_body_missing 与 local_only/无 K_enclave 都发生在
# *首次*复制判定时，此时 TEE frames 行还不存在，DELETE 是 no-op）。若该行确实
# 带 body_storage_key（说明真出现了此前未预见到的路径），本应同时清 frames-tee
# R2 对象，但那不在本次修复范围内——先保证 TEE 明文指针行被删，R2 对象清理留给
# 后续若观测到非空命中再补（reconciler 的抽样比对会发现孤儿 R2 key）。
_TABLES["frame_envelopes"] = _Table(
    select_sql=("SELECT user_id, frame_id, ts, doc, env_meta, body_key FROM frame_envelopes "
                "WHERE (ts, frame_id) > (%s, %s) ORDER BY ts, frame_id LIMIT %s"),
    cursor_kind="numeric",
    upsert_sql=(
        "INSERT INTO frames (user_id, frame_id, ts, meta, body_storage_key, "
        "body_storage_key_version, body_mime, body_sha256, body_size_bytes) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (user_id, frame_id) DO UPDATE SET ts=EXCLUDED.ts, meta=EXCLUDED.meta, "
        "body_storage_key=EXCLUDED.body_storage_key, "
        "body_storage_key_version=EXCLUDED.body_storage_key_version, "
        "body_mime=EXCLUDED.body_mime, body_sha256=EXCLUDED.body_sha256, "
        "body_size_bytes=EXCLUDED.body_size_bytes"),
    unpack=lambda r: (r[0], r[1], r[2], {"doc": r[3], "env_meta": r[4], "body_key": r[5]}),
    row_writer=_frames_row_writer,
    requeue_delete_tee_sql="DELETE FROM frames WHERE user_id = %s AND frame_id = %s",
)


def _produce_write(cfg: _Table, user_id: str, item_id: str, sort_val, doc: dict,
                   dry_run: bool):
    """一行 → TEE upsert 参数元组（或 None=已计数但不写，frames dry_run 用）。
    抛 PendingDeviceMigration / 其余异常的语义与 decrypt 路径一致，供 run_table
    的 freeze/pending 共用。"""
    if cfg.row_writer is not None:
        return cfg.row_writer(user_id, item_id, sort_val, doc, dry_run)
    pt_doc = _transform_with_retry(cfg, doc, user_id)
    return cfg.upsert_args(user_id, item_id, sort_val, pt_doc)


# --------------------------------------------------------------------------- #
# 游标编解码：把 (sort_val, item_id) ↔ (watermark_ts, watermark_id) 两列互转。
# --------------------------------------------------------------------------- #
def _encode_cursor(cfg: _Table, sort_val, item_id: str) -> tuple[float, str]:
    if cfg.cursor_kind == "numeric":
        return (float(sort_val or 0.0), str(item_id))
    if cfg.cursor_kind == "single":
        return (0.0, str(sort_val or ""))
    return (0.0, f"{sort_val or ''}{_NUL}{item_id}")


def _decode_cursor(cfg: _Table, wm_ts: float, wm_id: str) -> tuple:
    """返回喂给 select WHERE 占位符的参数元组（arity 与该表 cursor 列数匹配）。"""
    if cfg.cursor_kind == "numeric":
        return (wm_ts, wm_id)
    if cfg.cursor_kind == "single":
        return (wm_id,)
    sort_val, _, item_id = wm_id.partition(_NUL)
    return (sort_val, item_id)


def _read_cursor(table: str) -> tuple[float, str]:
    with mirror.get_tee_pool().connection() as c:
        row = c.execute(
            "SELECT watermark_ts, watermark_id FROM tee_replication_cursors "
            "WHERE table_name = %s", (table,)).fetchone()
    if not row:
        return (0.0, "")
    return (float(row[0] or 0.0), str(row[1] or ""))


def _log_row_error(table: str, user_id: str, item_id: str, exc: Exception) -> None:
    """落一条错误到 user_logs（复用 db.log_append 风格），便于事后排查 + 重跑追踪。"""
    try:
        db.log_append(user_id, "tee_replication_errors", {
            "table": table,
            "item_id": item_id,
            "error": f"{type(exc).__name__}:{str(exc)[:200]}",
            "ts": time.time(),
        }, ts=time.time(), item_key=f"{table}:{item_id}")
    except Exception as e:  # noqa: BLE001 — 日志失败不该拖垮复制
        log.warning("[tee-replicate] failed to log row error for %s/%s: %s", table, item_id, e)


def _consume_requeue(cfg: _Table, table: str) -> tuple[int, int, int]:
    """Drain the requeue lane before the cursor loop (non-dry-run only).

    requeue rows (``reason LIKE 'requeue%'``) mark same-PK in-place rewrites
    (identity edits, visibility swaps, memory/world_book back-dated inserts &
    edits) that the append-only cursor never revisits. For each: re-fetch the
    CURRENT RDS row by its PK and re-derive the TEE plaintext with the same
    per-row machinery as the cursor loop.

      - RDS row gone      → DELETE the TEE row + the pending row.
      - transform ok      → upsert TEE + DELETE the pending row.
      - PendingDeviceMigration → the item is now local_only/no-K_enclave; DELETE
        any stale TEE plaintext row (privacy — see the twin cursor-loop branch
        in run_table) + UPDATE the pending reason to that terminal state (the
        requeue is consumed).
      - any other failure → log/count, LEAVE the pending row so the next pass
        retries (never freezes the cursor — requeue rows are independent).

    Returns (copied, pending, errors) deltas folded into the run report.
    """
    if cfg.requeue_fetch_sql is None:
        return (0, 0, 0)
    with mirror.get_tee_pool().connection() as dst:
        pend = dst.execute(_REQUEUE_SELECT, (table, "requeue%")).fetchall()
    if not pend:
        return (0, 0, 0)

    copied = pending = errors = 0
    with db.get_pool().connection() as src:
        for user_id, item_id in pend:
            key = (user_id,) if cfg.requeue_by_user_only else (user_id, item_id)
            rds_row = src.execute(cfg.requeue_fetch_sql, key).fetchone()
            with mirror.get_tee_pool().connection() as dst:
                if rds_row is None:
                    with dst.transaction():
                        dst.execute(cfg.requeue_delete_tee_sql, key)
                        dst.execute(_PENDING_DELETE, (user_id, table, item_id))
                    continue
                uid, iid, sort_val, doc = cfg.unpack(rds_row)
                try:
                    args = _produce_write(cfg, uid, iid, sort_val, doc, False)
                except transforms.PendingDeviceMigration as e:
                    # The row is now local_only/no-K_enclave — terminal. Any
                    # plaintext left over in TEE from a prior (pre-rewrite)
                    # replication pass is now a privacy leak: it must be
                    # deleted in the same transaction as the terminal marker
                    # (see run_table's cursor-loop branch for the twin case;
                    # both keep verify's rds == tee + pending balanced).
                    with dst.transaction():
                        if cfg.requeue_delete_tee_sql is not None:
                            dst.execute(cfg.requeue_delete_tee_sql, key)
                        dst.execute(_PENDING_UPDATE_REASON,
                                    (_pdm_reason(e), user_id, table, item_id))
                    pending += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    _log_row_error(table, uid, iid, e)
                    continue
                with dst.transaction():
                    if args is not None:  # None = frames dry_run would_copy (N/A here)
                        dst.execute(cfg.upsert_sql, args)
                    dst.execute(_PENDING_DELETE, (user_id, table, item_id))
                copied += 1
    return (copied, pending, errors)


# Tables with a GENERATED ALWAYS AS IDENTITY column whose RDS values we carry
# over verbatim (OVERRIDING SYSTEM VALUE, see _TABLES["chat_messages"]) — after
# a non-dry_run pass the TEE sequence must be fast-forwarded past the highest
# replicated value, otherwise a post-cutover plain INSERT on TEE (the
# direct-write world once RDS is retired) would mint a seq that collides with
# an already-replicated high one. Same pattern as
# tee_shadow/reconciler.py's _IDENTITY_TABLES/setval for user_logs.
# Verified against alembic_tee/versions/0001_tee_baseline.py: memory_moments /
# world_book_entries key on natural (user_id, moment_id/entry_id) with no
# identity column; identity (user_blobs) keys on (user_id, kind), no identity
# column either; frame_envelopes maps to the `frames` table, which likewise has
# no identity column. chat_messages.seq is the only identity column among the
# replicated tables.
_SEQ_TABLES: dict[str, str] = {"chat_messages": "seq"}


def run_table(table: str, *, qps: float = 2.0, dry_run: bool = False,
              limit: int | None = None) -> dict:
    """把 RDS ``table`` 的密文增量解密复制进 TEE 明文库。

    失败语义（brief）：
      - 单行 decrypt 重试 2 次仍败 → 记 errors + 落 user_logs → **游标冻结在失败行之前**
        （本批后续行照常写入 TEE，但游标不越过失败行；本 run 到此批为止，下次重跑重试）。
      - local_only / 无 K_enclave → PendingDeviceMigration → upsert pending 表 → 游标照常推进。
      - dry_run：零 TEE 写入（含游标），report 给出 would_copy 计数。
      - 幂等：ON CONFLICT upsert，同水位重放不重不丢。
    """
    cfg = _TABLES[table]
    wm_ts, wm_id = _read_cursor(table)
    copied = pending = errors = 0
    # Requeue lane first (non-dry-run): drain same-PK in-place rewrites the
    # append-only cursor can't see. Independent of the cursor — its failures
    # never freeze it.
    if not dry_run:
        rq_copied, rq_pending, rq_errors = _consume_requeue(cfg, table)
        copied += rq_copied
        pending += rq_pending
        errors += rq_errors
    remaining = limit

    with db.get_pool().connection() as src:
        while True:
            page = BATCH if remaining is None else min(BATCH, remaining)
            if page <= 0:
                break
            rows = src.execute(cfg.select_sql, (*_decode_cursor(cfg, wm_ts, wm_id), page)).fetchall()
            if not rows:
                break

            writes: list[tuple] = []
            pend_rows: list[tuple] = []
            adv_ts, adv_id = wm_ts, wm_id
            frozen = False          # 一旦硬失败，游标停止前进（冻结在失败行之前）
            batch_failed = False

            for row in rows:
                user_id, item_id, sort_val, doc = cfg.unpack(row)
                try:
                    args = _produce_write(cfg, user_id, item_id, sort_val, doc, dry_run)
                except transforms.PendingDeviceMigration as e:
                    pend_rows.append((user_id, item_id, _pdm_reason(e)))
                    pending += 1
                    if not frozen:
                        adv_ts, adv_id = _encode_cursor(cfg, sort_val, item_id)
                    continue
                except Exception as e:  # noqa: BLE001 — decrypt/reencrypt 重试后仍失败
                    errors += 1
                    batch_failed = True
                    frozen = True
                    _log_row_error(table, user_id, item_id, e)
                    continue
                if args is not None:  # None = dry_run would_copy (frames)，不落写
                    writes.append(args)
                copied += 1
                if not frozen:
                    adv_ts, adv_id = _encode_cursor(cfg, sort_val, item_id)

            if not dry_run:
                # 整批写 + 游标推进单事务：写失败则整批回滚、游标不动 → 下次重跑。
                with mirror.get_tee_pool().connection() as dst:
                    with dst.transaction():
                        for args in writes:
                            dst.execute(cfg.upsert_sql, args)
                        for uid, iid, reason in pend_rows:
                            # PendingDeviceMigration is a terminal state
                            # (local_only / no-K_enclave). If an earlier pass
                            # already replicated this same PK to TEE before
                            # the row was rewritten (e.g. a requeue-lane
                            # in-place edit whose new ciphertext is now
                            # undecryptable), that stale plaintext is a
                            # privacy leak and must go — delete it in the
                            # same transaction as the terminal pending marker
                            # so verify's rds == tee + pending stays balanced.
                            # Idempotent DELETE by PK: no-op if the row was
                            # never copied (the common case — most PDM rows
                            # are local_only from creation and never touch
                            # TEE in the first place).
                            if cfg.requeue_delete_tee_sql is not None:
                                key = (uid,) if cfg.requeue_by_user_only else (uid, iid)
                                dst.execute(cfg.requeue_delete_tee_sql, key)
                            dst.execute(_PENDING_UPSERT, (uid, table, iid, reason))
                        dst.execute(_CURSOR_UPSERT, (table, adv_ts, adv_id))

            wm_ts, wm_id = adv_ts, adv_id
            if remaining is not None:
                remaining -= len(rows)
            if qps and qps > 0:
                _sleep(len(rows) / qps)

            if batch_failed:
                break
            if len(rows) < page:
                break

    if not dry_run:
        seq_col = _SEQ_TABLES.get(table)
        if seq_col:
            # See _SEQ_TABLES above: fast-forward the TEE identity sequence past
            # the highest carried-over seq so a post-cutover direct INSERT on
            # TEE can't collide with an already-replicated high value.
            # GREATEST(...,1) guards an empty table (COALESCE(MAX,1) also
            # guards NULL) — same shape as reconciler.reconcile_table's setval.
            with mirror.get_tee_pool().connection() as dst:
                dst.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                    f"GREATEST((SELECT COALESCE(MAX({seq_col}), 1) FROM {table}), 1))",
                    (table, seq_col))

    report = {"table": table, "copied": copied, "pending": pending, "errors": errors,
              "watermark_ts": wm_ts, "watermark_id": wm_id}
    log.info("[tee-replicate] %s", report)
    return report
