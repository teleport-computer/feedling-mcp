"""游标驱动的密文→明文复制 worker（spec §5.2）。

每张表一条 ``(sort, id)`` 复合游标（存 TEE ``tee_replication_cursors``），只追加式
向前扫描 RDS 密文行，逐行调 enclave 解密（``transforms``），把明文 doc upsert 进 TEE。

游标编码（游标表只有 watermark_ts DOUBLE + watermark_id TEXT 两列）：
  - chat：sort=ts（DOUBLE）→ watermark_ts=ts，watermark_id=msg_id；``(ts,msg_id)`` 键集。
  - memory/world_book：sort=occurred_at/updated_at（TEXT）→ watermark_ts=0，
    watermark_id=``"{sort}\\x1f{id}"`` 复合（\x1f 分隔——见 _SEP，绝不用 NUL:TEXT 列
    不接受 NUL）。sort_val=ISO 时间戳不含 \x1f，partition 取首个分隔符即可无歧义解码。
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
from dataclasses import dataclass, replace
from typing import Callable

import os

import db
import psycopg
from plaintext_shadow import config as plaintext_shadow_config
from plaintext_shadow.config import TargetPolicy
from psycopg.types.json import Jsonb
from tee_shadow import mirror

from tee_replicator import transforms

log = logging.getLogger("feedling.tee_replicator")

BATCH = 500
_RETRIES = 2                 # 单行 decrypt 额外重试次数（共 1+2=3 次尝试）
_SEP = "\x1f"                # 文本复合游标分隔符（ASCII Unit Separator）。
# 绝不能用 NUL(\x00):watermark_id 是 PostgreSQL TEXT 列，TEXT 不接受 NUL(0x00)，
# 写含 NUL 的游标会抛 "text fields cannot contain NUL"、整表复制崩(2026-07-14 prod
# 实测:memory_moments/world_book_entries 一有数据就撞;test 因这两表无数据从未暴露)。
# \x1f 是非 NUL 控制字符、TEXT 可存、且绝不出现在 sort_val(ISO 时间戳)里 → partition
# 取首个分隔符即可无歧义解码。历史上从未成功写过含 NUL 的复合游标，故换分隔符零迁移负担。
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
# Terminal-reason prefix for permanently-undecryptable rows (enclave 403
# decrypt_failed — wrong content key/owner mismatch/corrupt envelope). Like
# _PDM_REASON_PREFIX it is a fixed marker that never starts with "requeue", so a
# quarantined row is not falsely picked up by _REQUEUE_SELECT. verify counts it
# as terminal pending (NOT LIKE 'requeue%') → keeps rds == tee + pending
# balanced and skips it in sampling.
_DECRYPT_FAILED_REASON_PREFIX = "decrypt_failed:"


def _pdm_reason(exc: Exception) -> str:
    return f"{_PDM_REASON_PREFIX}{str(exc) or 'local_only_or_no_k_enclave'}"


def _decrypt_failed_reason(exc: Exception) -> str:
    # Truncate: the enclave body can be long; the prefix + a short detail is all
    # a later re-drive / audit needs, and the pending.reason column is TEXT.
    return f"{_DECRYPT_FAILED_REASON_PREFIX}{str(exc)[:200] or 'undecryptable'}"


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
    # prune lane（删除传播的兜底对账，见 tee_shadow/ciphertext_prune.py）。两条
    # SELECT 必须返回**同构**的主键元组，且元组顺序与
    # tee_shadow.ciphertext_prune._PRUNE_DELETE_SQL 的参数逐位对齐。
    #
    # 为什么两侧要分别给一条而不是共用：表名和辖区在两侧不一定相同。
    # frame_envelopes（RDS）在 TEE 侧叫 frames；identity 是伪表，两侧都要
    # `WHERE kind='identity'` 的辖区限定，否则会把 user_blobs 的其它 kind
    # （归 MIRROR lane 的 reconciler 管）卷进来当孤儿删掉。
    #
    # 留空 = 本表不参与 prune。没有安全删除契约的表（chat_message_archive /
    # v2_conversation_summary_segments）就该留空。
    prune_rds_keys_sql: str | None = None
    prune_tee_keys_sql: str | None = None
    # 冷启动（tee_replication_cursors 里还没有本表的行）时喂给 select_sql 的参数
    # 元组。None = 沿用通用逻辑：_read_cursor 对未跑过的表返回 ("", 0.0)，
    # _decode_cursor 把空串原样当参数——这对既有表安全，因为它们的排序列都是
    # TEXT（空串是合法文本下界）。排序列是 TIMESTAMPTZ / BIGINT 的表**必须**显式
    # 给一个该类型能接受的下界，否则空串在 WHERE (sort_col, id_col) > (%s, %s)
    # 里 cast 失败，SELECT 本身报错、整表永不同步（scheduler 的 per-table
    # try/except 会把这个错误吞掉计入退避，每个 tick 静默重复失败，见
    # 2026-07-28 审查记录）。
    cursor_zero: tuple | None = None
    # Dirty-key replay always re-reads the authoritative current row.  These
    # contracts are separate from the legacy requeue lane because the durable
    # key is the real source PK (for example model_api_credentials carries only
    # `id`, while its old requeue row also carried user_id).
    key_fetch_sql: str | None = None
    key_delete_sql: str | None = None
    key_params: Callable[[dict], tuple] | None = None


_SEQ_KEY = "_replicator_seq"  # smuggled through the plaintext doc dict, see below
_STORAGE_GENERATION_KEY = "_replicator_storage_generation"


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
    uid, msg_id, ts, doc, seq, storage_generation = r
    if (
        db._is_chat_file_pointer(doc)
        and db._chat_body_object_format(doc) == "sealed_v1"
    ):
        doc = db.hydrate_chat_file_body(uid, doc)
    return (
        uid,
        msg_id,
        ts,
        {
            **doc,
            _SEQ_KEY: seq,
            _STORAGE_GENERATION_KEY: storage_generation,
        },
    )


def _chat_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    seq = doc.pop(_SEQ_KEY)
    storage_generation = doc.pop(_STORAGE_GENERATION_KEY)
    return (uid, seq, iid, sort, Jsonb(doc), storage_generation)


_TABLES: dict[str, _Table] = {
    "chat_messages": _Table(
        select_sql=("SELECT user_id, msg_id, ts, doc, seq, storage_generation FROM chat_messages "
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
        upsert_sql=("INSERT INTO chat_messages "
                    "(user_id, seq, msg_id, ts, doc, storage_generation) "
                    "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id, msg_id) DO UPDATE SET "
                    "ts=EXCLUDED.ts, doc=EXCLUDED.doc, "
                    "storage_generation=EXCLUDED.storage_generation"),
        unpack=_chat_unpack,
        upsert_args=_chat_upsert_args,
        requeue_fetch_sql=("SELECT user_id, msg_id, ts, doc, seq, storage_generation "
                           "FROM chat_messages "
                           "WHERE user_id = %s AND msg_id = %s"),
        requeue_delete_tee_sql="DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s",
        prune_rds_keys_sql="SELECT user_id, msg_id FROM chat_messages",
        prune_tee_keys_sql="SELECT user_id, msg_id FROM chat_messages",
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
        prune_rds_keys_sql="SELECT user_id, moment_id FROM memory_moments",
        prune_tee_keys_sql="SELECT user_id, moment_id FROM memory_moments",
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
        prune_rds_keys_sql="SELECT user_id, entry_id FROM world_book_entries",
        prune_tee_keys_sql="SELECT user_id, entry_id FROM world_book_entries",
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
        # 辖区限定不可省：user_blobs 整表归 MIRROR lane，reconciler 只是把
        # kind='identity' 让给了 replicator（见 reconciler._SCOPE_WHERE）。不带
        # WHERE 就会把其它 kind 的行当成本 lane 的孤儿删掉——那些行归 reconciler 管。
        prune_rds_keys_sql="SELECT user_id FROM user_blobs WHERE kind = 'identity'",
        prune_tee_keys_sql="SELECT user_id FROM user_blobs WHERE kind = 'identity'",
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
    Runtime V2 下没有 per-user api_key，只能用 runtime token
    （scope=envelope_decrypt，见 supervisor.py 的 mint_token 用法）。
    """
    from core import envelope as core_envelope

    token = _mint_runtime_token(user_id)

    def decrypt(envelope: dict, purpose: str) -> bytes:
        # 形状路由：明文行直读、不白跑一趟 enclave。cutover 后 TEE 主库里
        # 密文/明文行的共存形态仍由 Task 2.4 定，这里只是不再假设行一定是信封。
        return core_envelope.read_envelope_body(
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


# 判据是**用户意图**而不是行形状。设计初稿写的是「按行形状搬运」，实现时发现有
# 洞：现在 PLAINTEXT_WRITES_ACCEPTED 是 False、effective 恒 "on"，所有行都是信封，
# 按形状搬运会让影子库立刻整体变密文、明文排查通道当场失效。那是过渡期回归，不是
# 终态。平台放开明文后意图与形状自然一致，本分流退化成「按行形状搬运」。
_CARRY_VERBATIM_TTL_SEC = 60.0
_carry_verbatim_cache: dict[str, tuple[float, bool]] = {}


def _carries_verbatim(
    user_id: str,
    target_policy: TargetPolicy | None = None,
) -> bool:
    """该用户的行是否原样搬运（= 显式选了加密档）。

    fail-safe 方向与写侧一致：**查不到用户就不解密**。搬运的失败方向是「多留了
    密文」，事后重放可修；解密的失败方向是「明文泄漏」，不可逆。

    带 TTL 缓存：``_get_user_content_encryption`` 是 O(用户数) 的全表扫描，按行
    调用会拖垮长跑 pass；但不能永久缓存，否则用户切档后要等进程重启才生效。
    """
    if target_policy is not None and target_policy.mode == "plaintext_all":
        return False

    now = time.time()
    hit = _carry_verbatim_cache.get(user_id)
    if hit is not None and now - hit[0] <= _CARRY_VERBATIM_TTL_SEC:
        return hit[1]

    from accounts import registry  # 延迟导入：复制层不该在模块期拉起 accounts

    # 三态：`"on"` 加密档 → 搬运；`"off"` 明文档 → 解密；`None` 查不到用户 →
    # fail-safe 搬运。所以「不等于 off」正是判据。
    verbatim = registry._get_user_content_encryption(user_id) != "off"
    _carry_verbatim_cache[user_id] = (now, verbatim)
    return verbatim


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


def _is_permanent_decrypt_failure(exc: Exception) -> bool:
    """enclave 明确「解不开这段密文」——HTTP 403 且错误体含 ``decrypt_failed``。

    这是**确定性**失败（错内容钥/owner 不符/坏信封）：密文与 enclave 钥不变，重试
    多少次、换多少枚 token 都是同一个结果。据此把它与「值得换 token 再试」的一般
    401/403（``_is_auth_error``）区分开——毒行走隔离 lane，不再空转重试也不冻结游标。
    """
    msg = str(exc)
    return "enclave_http_403" in msg and "decrypt_failed" in msg


def _transform_with_retry(
    cfg: _Table,
    doc: dict,
    user_id: str,
    target_policy: TargetPolicy | None = None,
) -> dict:
    """PendingDeviceMigration 是确定性的，立即上抛不重试；其余（网络/enclave）重试。

    auth 形状的失败（401/token 过期）在重试前强制重铸 token——同一枚 stale token
    重试多少次都是白试。
    """
    if _carries_verbatim(user_id, target_policy):
        # 加密档：整行原样搬运，复制层不解密（Task 2.4）。
        return transforms.carry_verbatim(doc)
    main_is_plaintext = (
        isinstance(doc.get("body"), str)
        or (
            bool(doc.get("body_key"))
            and doc.get("body_object_format") == "plaintext_v1"
        )
    )
    if main_is_plaintext and not transforms.needs_decrypt(doc):
        # 明文档的终态行已经是 transform 的目标形状。旁路必须放在
        # _get_decrypt 之前，否则即使 transform 本身不调用 decrypt，复制层仍会
        # 无意义地铸 token/准备 enclave，并在 enclave 故障时连坐明文用户。
        return cfg.transform(doc, None)

    decrypt = _get_decrypt(user_id)
    last: Exception | None = None
    for _ in range(_RETRIES + 1):
        try:
            return cfg.transform(doc, decrypt)
        except transforms.PendingDeviceMigration:
            raise
        except Exception as e:  # noqa: BLE001
            # 确定性解密失败：立即终态上抛，不浪费重试/换 token（也早于下面的
            # _is_auth_error——bare 403 与 403 decrypt_failed 都命中 auth 判据，
            # 必须先分流出永久毒行）。
            if _is_permanent_decrypt_failure(e):
                raise transforms.PermanentDecryptFailure(str(e)) from e
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
            # frames 存储层重加密先要 enclave 解开源密文——同 _transform_with_retry，
            # 确定性 decrypt_failed 立即终态上抛（frames 毒行占比最高，见 prod 观测）。
            if _is_permanent_decrypt_failure(e):
                raise transforms.PermanentDecryptFailure(str(e)) from e
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
    # ⚠️ 两侧表名不同：RDS 是 frame_envelopes，TEE 侧叫 frames。
    # 遗留风险与上面 requeue_delete_tee_sql 那段注释同源：prune 的独立删除契约删掉 TEE 明文指针
    # 行时，不清理该行 body_storage_key 指向的 frames-tee R2 对象，会留下孤儿对象。
    # 与 requeue lane 的既有行为一致（那条 DELETE 同样不碰 R2），不在本次修复范围。
    prune_rds_keys_sql="SELECT user_id, frame_id FROM frame_envelopes",
    prune_tee_keys_sql="SELECT user_id, frame_id FROM frames",
)


# --------------------------------------------------------------------------- #
# 2026-07-27 全量对齐新增的 7 张密文表。真实结构（实测，非照抄示例）：见
# .superpowers/sdd/2026-07-27-tee-full-table-alignment/task-7-brief.md 开头表格。
# _Table 的四元组契约 (user_id, item_id, sort_val, doc) 装不下这些表的额外列
# （复合 PK 的另一半 / 不属于信封 JSON 的业务列），统一用 _pack_extra/_pop_extra
# 走私——是 chat_messages._SEQ_KEY 手法的泛化版（同一个保留键前缀，任意个键），
# 供本轮全部 7 张表共用，而不是每张表各发明一套命名。
# --------------------------------------------------------------------------- #
_EXTRA_PREFIX = "_replicator_extra:"


def _pack_extra(doc: dict, **extra) -> dict:
    """把塞不进 (user_id, item_id, sort_val, doc) 四元组的额外列，走私进 doc dict。

    transform 只剥 _ENVELOPE_KEYS（见 transforms.py 顶部），这些前缀键原样穿透
    解密/去壳，upsert_args 用 _pop_extra 在写库前取回、绝不落进明文信封 JSONB。
    """
    return {**doc, **{f"{_EXTRA_PREFIX}{k}": v for k, v in extra.items()}}


def _pop_extra(doc: dict, *names: str) -> tuple:
    """从 doc 里取回 _pack_extra 走私的值（原地 pop，doc 剩下的就是信封本体）。"""
    return tuple(doc.pop(f"{_EXTRA_PREFIX}{n}") for n in names)


def _iso(ts) -> str | None:
    """TIMESTAMPTZ → ISO 文本（"text" 游标 sort_val 与写库前的智能透传都要）。None 原样保留。"""
    return ts.isoformat() if ts is not None else None


# ---- chat_message_archive：doc 与 chat_messages 同形，复用 plaintext_chat_doc，
# 但 unpack/游标/upsert 必须自己设计——PK 是 (user_id, source_seq)，不是
# (user_id, msg_id)。 ------------------------------------------------------- #
def _chat_archive_unpack(r: tuple) -> tuple:
    """(user_id, source_seq, msg_id, ts, doc, storage_generation, clear_generation,
    cleared_at) row -> (user_id, item_id, sort_val, doc')。

    item_id 是 source_seq，不是 msg_id：真实 PK 是 (user_id, source_seq)（见
    alembic_tee 0004）。source_seq 是用户清空历史时从 chat_messages.seq 原样
    搬过来的值（db.py 的 "INSERT INTO chat_message_archive ... SELECT
    user_id,seq,... FROM chat_messages"），而 chat_messages.seq 是全表级
    GENERATED ALWAYS AS IDENTITY——所以 source_seq *可证* 全局唯一；msg_id 只在
    某一时刻的 live chat_messages 里保证唯一，同一 msg_id 理论上可能出现在两次
    不同的清空周期里（概率上因为 msg_id 通常是 UUID 而可忽略，但 source_seq 是
    唯一有数学保证的那个，游标 tie-break 优先用它）。

    sort_val 是 cleared_at，不是 ts：ts 是原始消息的聊天时间戳，随消息原样搬过
    来，反映的是消息最初发出的时间，不是"这行何时被归档"。两个不同用户可以在
    任意真实时刻清空聊天记录、而各自消息的原始 ts 可能相差数年——如果用 ts 驱动
    全局游标，后清空的用户（消息 ts 更早）的归档行会排在已经推进过的水位线*之
    前*，被永久跳过。这与 job_id 驱动 v2_trajectory_events 游标会导致的追加序
    违规是同一类错误（见 _trajectory_events_unpack）。cleared_at 由
    clock_timestamp() 在归档 INSERT 发生的那一刻写入（0004 DDL），因此和
    chat_messages.ts 一样，与"写入这张表的真实顺序"相关。

    msg_id / ts / storage_generation / clear_generation 是普通列，不在 doc 自己
    的 JSON 里（与 chat_messages.seq 处境相同）——用 _pack_extra/_pop_extra 走私。

    R2-offloaded 行（content_type="file"，doc 只带 body_key 指针）的水合与
    _chat_unpack 对 live 表的处理完全一致，复用同一个 db helper。
    """
    (uid, source_seq, msg_id, ts, doc, storage_generation, clear_generation,
     cleared_at) = r
    if (
        db._is_chat_file_pointer(doc)
        and db._chat_body_object_format(doc) == "sealed_v1"
    ):
        doc = db.hydrate_chat_file_body(uid, doc)
    item_id = str(source_seq)
    sort_val = _iso(cleared_at) or ""
    packed = _pack_extra(doc, msg_id=msg_id, ts=ts,
                          storage_generation=storage_generation,
                          clear_generation=clear_generation)
    return (uid, item_id, sort_val, packed)


def _chat_archive_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    msg_id, ts, storage_generation, clear_generation = _pop_extra(
        doc, "msg_id", "ts", "storage_generation", "clear_generation")
    return (uid, int(iid), msg_id, ts, Jsonb(doc), storage_generation,
            clear_generation, sort)


_TABLES["chat_message_archive"] = _Table(
    select_sql=("SELECT user_id, source_seq, msg_id, ts, doc, storage_generation, "
                "clear_generation, cleared_at FROM chat_message_archive "
                "WHERE (cleared_at, source_seq) > (%s, %s) "
                "ORDER BY cleared_at, source_seq LIMIT %s"),
    cursor_kind="text",
    transform=transforms.plaintext_chat_doc,
    upsert_sql=("INSERT INTO chat_message_archive "
                "(user_id, source_seq, msg_id, ts, doc, storage_generation, "
                "clear_generation, cleared_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id, source_seq) DO UPDATE SET "
                "msg_id=EXCLUDED.msg_id, ts=EXCLUDED.ts, doc=EXCLUDED.doc, "
                "storage_generation=EXCLUDED.storage_generation, "
                "clear_generation=EXCLUDED.clear_generation, "
                "cleared_at=EXCLUDED.cleared_at"),
    unpack=_chat_archive_unpack,
    upsert_args=_chat_archive_upsert_args,
    # 常规产品路径仍是 append-only；R2 plaintext pointer backfill 会在 RDS 事务内
    # delete+insert 同一 source_seq（保留 cleared_at），因此普通游标不会再看见它。
    # 迁移完成后用 requeue lane 取回当前行，保证 TEE pointer 同步切换。
    requeue_fetch_sql=("SELECT user_id, source_seq, msg_id, ts, doc, "
                       "storage_generation, clear_generation, cleared_at "
                       "FROM chat_message_archive "
                       "WHERE user_id = %s AND source_seq = %s"),
    requeue_delete_tee_sql=("DELETE FROM chat_message_archive "
                            "WHERE user_id = %s AND source_seq = %s"),
    # 冷启动：cleared_at 是 TIMESTAMPTZ，"-infinity" 是合法字面量；source_seq 是
    # BIGINT（WHERE 里没有 ::text 转型），空串不是合法 bigint，必须给数字串。
    cursor_zero=("-infinity", "0"),
)


# ---- v2_trajectory_events：PK (job_id, event_index)，无 user_id FK、无单一 id 列。
def _trajectory_events_unpack(r: tuple) -> tuple:
    """(user_id, job_id, event_index, event_kind, idempotency_key, payload_bytes,
    truncated, created_at, payload_envelope) row -> (user_id, item_id, sort_val, doc')。

    item_id 是 "job_id:event_index"（真实 PK 的字符串拼接，两者都是纯数字，
    ":" 绝不会出现在其中，故可无歧义还原）——这个组合本身就全局唯一，不依赖
    created_at 是否恰好撞车。

    sort_val 是 created_at，不是 job_id：job_id 只反映 job *创建* 顺序，而一个
    长期运行的 job 可能在更晚创建、但更快跑完的 job 已经把全局游标推过去之后，
    才追加新事件。若用 job_id 排序，这类晚到事件的 key 会落在已推进的水位线
    *之后* 的 job_id 更小处，被 "(job_id,event_index) > 水位线" 的比较条件永久
    跳过。created_at 由 clock_timestamp() 在每条事件自己的 INSERT 时刻写入
    （alembic_tee 0004 DDL），与 chat_messages.ts / memory_moments.occurred_at
    一样和真实写入顺序相关。

    event_kind / idempotency_key / payload_bytes / truncated 是普通 SQL 列，不在
    payload_envelope 自己的 JSON 里——实测一行真实 envelope 只有
    crypto 字段 + id/visibility/owner_user_id，没有这几个键（2026-07-28 对
    test 环境一行实测验证）。用 _pack_extra/_pop_extra 走私。
    """
    (uid, job_id, event_index, event_kind, idempotency_key, payload_bytes,
     truncated, created_at, envelope) = r
    item_id = f"{job_id}:{event_index}"
    sort_val = _iso(created_at) or ""
    doc = _pack_extra(envelope, job_id=job_id, event_index=event_index,
                       event_kind=event_kind, idempotency_key=idempotency_key,
                       payload_bytes=payload_bytes, truncated=truncated)
    return (uid, item_id, sort_val, doc)


# ---- voice_transcripts：PK (user_id, call_id)，envelope 是独立列，其余是普通
# 业务列（与 v2_trajectory_events 同型，走 _pack_extra/_pop_extra 走私）。
def _voice_transcripts_unpack(r: tuple) -> tuple:
    """(user_id, call_id, chat_message_id, turn_count, duration_sec, char_count,
    created_at, transcript_envelope) row -> (user_id, item_id, sort_val, doc')。

    item_id 是 call_id（secrets.token_urlsafe 生成、PK 的一半，per-user 唯一）。
    sort_val 是 created_at：归档行只写一次、此后永不改写，所以时间戳游标没有
    "晚提交但更早取到 now()" 的跳过风险，也因此不需要 requeue。
    """
    (uid, call_id, chat_message_id, turn_count, duration_sec, char_count,
     created_at, envelope) = r
    sort_val = _iso(created_at) or ""
    doc = _pack_extra(envelope, chat_message_id=chat_message_id,
                      turn_count=turn_count, duration_sec=duration_sec,
                      char_count=char_count)
    return (uid, str(call_id), sort_val, doc)


def _voice_transcripts_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    (chat_message_id, turn_count, duration_sec, char_count) = _pop_extra(
        doc, "chat_message_id", "turn_count", "duration_sec", "char_count")
    return (uid, iid, chat_message_id, turn_count, duration_sec, char_count,
            sort, Jsonb(doc))


def _trajectory_events_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    (job_id, event_index, event_kind, idempotency_key, payload_bytes,
     truncated) = _pop_extra(doc, "job_id", "event_index", "event_kind",
                              "idempotency_key", "payload_bytes", "truncated")
    return (uid, job_id, event_index, event_kind, idempotency_key,
            payload_bytes, truncated, sort, Jsonb(doc))


_TABLES["v2_trajectory_events"] = _Table(
    select_sql=("SELECT user_id, job_id, event_index, event_kind, idempotency_key, "
                "payload_bytes, truncated, created_at, payload_envelope "
                "FROM v2_trajectory_events "
                "WHERE (created_at, job_id::text || ':' || event_index::text) > (%s, %s) "
                "ORDER BY created_at, job_id::text || ':' || event_index::text LIMIT %s"),
    cursor_kind="text",
    transform=lambda doc, decrypt: transforms.plaintext_envelope_column(
        doc, decrypt, purpose=f"tee_replicate:v2_trajectory_events:{doc.get('id', '')}"),
    upsert_sql=("INSERT INTO v2_trajectory_events "
                "(user_id, job_id, event_index, event_kind, idempotency_key, "
                "payload_bytes, truncated, created_at, payload_envelope) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id, event_index) DO UPDATE SET "
                "event_kind=EXCLUDED.event_kind, idempotency_key=EXCLUDED.idempotency_key, "
                "payload_bytes=EXCLUDED.payload_bytes, truncated=EXCLUDED.truncated, "
                "created_at=EXCLUDED.created_at, payload_envelope=EXCLUDED.payload_envelope"),
    unpack=_trajectory_events_unpack,
    upsert_args=_trajectory_events_upsert_args,
    # 全仓搜索 "UPDATE v2_trajectory_events" 零命中——纯追加的轨迹事件日志，没有
    # 原地改写路径，不需要 requeue。
    requeue_fetch_sql=None,
    requeue_delete_tee_sql=None,
    # 实际运行中 agent_jobs 清理会让 RDS 轨迹事件消失；TEE 表刻意没有跨表 FK，
    # 因此不能再把它视作永不删除。全量 reflow/prune 用真实复合 PK 收敛孤儿行。
    prune_rds_keys_sql="SELECT job_id, event_index FROM v2_trajectory_events",
    prune_tee_keys_sql="SELECT job_id, event_index FROM v2_trajectory_events",
    # 冷启动：created_at 是 TIMESTAMPTZ，"-infinity" 合法；第二列在 SQL 里已经
    # ::text 转型成字符串拼接，空串本身就是合法 TEXT 且排在任何非空拼接结果之前。
    cursor_zero=("-infinity", ""),
)


_TABLES["voice_transcripts"] = _Table(
    select_sql=("SELECT user_id, call_id, chat_message_id, turn_count, duration_sec, "
                "char_count, created_at, transcript_envelope FROM voice_transcripts "
                "WHERE (created_at, call_id) > (%s, %s) "
                "ORDER BY created_at, call_id LIMIT %s"),
    cursor_kind="text",
    transform=lambda doc, decrypt: transforms.plaintext_envelope_column(
        doc, decrypt, purpose=f"tee_replicate:voice_transcripts:{doc.get('id', '')}"),
    upsert_sql=("INSERT INTO voice_transcripts "
                "(user_id, call_id, chat_message_id, turn_count, duration_sec, "
                "char_count, created_at, transcript_envelope) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id, call_id) DO UPDATE SET "
                "chat_message_id=EXCLUDED.chat_message_id, "
                "turn_count=EXCLUDED.turn_count, duration_sec=EXCLUDED.duration_sec, "
                "char_count=EXCLUDED.char_count, created_at=EXCLUDED.created_at, "
                "transcript_envelope=EXCLUDED.transcript_envelope"),
    unpack=_voice_transcripts_unpack,
    upsert_args=_voice_transcripts_upsert_args,
    # 归档行只在挂断时写一次、此后永不改写（全仓无 "UPDATE voice_transcripts"），
    # 没有原地改写路径 → 不需要 requeue，与 v2_trajectory_events 同理。
    requeue_fetch_sql=None,
    requeue_delete_tee_sql=None,
    prune_rds_keys_sql="SELECT user_id, call_id FROM voice_transcripts",
    prune_tee_keys_sql="SELECT user_id, call_id FROM voice_transcripts",
    cursor_zero=("-infinity", ""),
)


# ---- model_api_credentials：PK 是单列 id(uuid)，无单一 doc 列（envelope 只是
# 众多业务列之一）。
def _model_api_credentials_unpack(r: tuple) -> tuple:
    """(id, user_id, provider, label, base_url, api_key_envelope, api_key_hint,
    supports_responses, created_at, updated_at) row -> (user_id, item_id, sort_val, doc')。

    item_id 是 id（uuid 本身单列全局唯一，不需要复合）。

    sort_val 是 updated_at：db.model_api_credential_update 的每一条 UPDATE 分支
    都硬编码 "updated_at = now()"（backend/db.py 验证过），但这只保证"改了内容
    updated_at 一定跟着动"，不保证"游标一定能追上"——并发写下，晚提交但更早
    拿到 now() 的事务可能被别的写入推过的水位线永久跳过，是时间戳型 CDC 游标的
    通病（chat_messages 自己都不敢只靠这一点）。db.model_api_credential_update
    已接 mirror.mark_pending(..., "model_api_credentials", ...) requeue 兜底
    （2026-07-28，与 memory/world_book 同一套机制）。
    """
    (cred_id, uid, provider, label, base_url, envelope, api_key_hint,
     supports_responses, created_at, updated_at) = r
    item_id = str(cred_id)
    sort_val = _iso(updated_at) or ""
    doc = _pack_extra(envelope, cred_id=cred_id, provider=provider, label=label,
                       base_url=base_url, api_key_hint=api_key_hint,
                       supports_responses=supports_responses,
                       created_at=_iso(created_at))
    return (uid, item_id, sort_val, doc)


def _model_api_credentials_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    (cred_id, provider, label, base_url, api_key_hint, supports_responses,
     created_at) = _pop_extra(doc, "cred_id", "provider", "label", "base_url",
                               "api_key_hint", "supports_responses", "created_at")
    return (cred_id, uid, provider, label, base_url, Jsonb(doc), api_key_hint,
            supports_responses, created_at, sort)


_TABLES["model_api_credentials"] = _Table(
    select_sql=("SELECT id, user_id, provider, label, base_url, api_key_envelope, "
                "api_key_hint, supports_responses, created_at, updated_at "
                "FROM model_api_credentials WHERE (updated_at, id) > (%s, %s) "
                "ORDER BY updated_at, id LIMIT %s"),
    cursor_kind="text",
    transform=lambda doc, decrypt: transforms.plaintext_envelope_column(
        doc, decrypt, purpose=f"tee_replicate:model_api_credentials:{doc.get('id', '')}"),
    upsert_sql=("INSERT INTO model_api_credentials "
                "(id, user_id, provider, label, base_url, api_key_envelope, "
                "api_key_hint, supports_responses, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "provider=EXCLUDED.provider, label=EXCLUDED.label, "
                "base_url=EXCLUDED.base_url, api_key_envelope=EXCLUDED.api_key_envelope, "
                "api_key_hint=EXCLUDED.api_key_hint, "
                "supports_responses=EXCLUDED.supports_responses, "
                "updated_at=EXCLUDED.updated_at"),
    unpack=_model_api_credentials_unpack,
    upsert_args=_model_api_credentials_upsert_args,
    # 2026-07-28 修正：unpack 文档原先论证 updated_at 全覆盖故不需要 requeue——
    # 这个论证只在"没有并发写"的理想情况下成立（时间戳游标下，晚提交但更早
    # 拿到 now() 的事务可能被并发写推过的水位线永久跳过，是时间戳型 CDC 游标的
    # 通病，chat_messages 自己都不敢只靠这一点，见 worker.py:172 起的注释）。
    # model_api_credential_update（db.py）已接 mirror.mark_pending 兜底。
    requeue_fetch_sql=("SELECT id, user_id, provider, label, base_url, "
                       "api_key_envelope, api_key_hint, supports_responses, "
                       "created_at, updated_at FROM model_api_credentials "
                       "WHERE user_id = %s AND id = %s"),
    requeue_delete_tee_sql="DELETE FROM model_api_credentials WHERE user_id = %s AND id = %s",
    prune_rds_keys_sql="SELECT user_id, id FROM model_api_credentials",
    prune_tee_keys_sql="SELECT user_id, id FROM model_api_credentials",
    # 冷启动：updated_at 是 TIMESTAMPTZ，"-infinity" 合法；id 是 UUID（WHERE 里
    # 没有 ::text 转型），空串不是合法 uuid，用全零 nil UUID 当下界（Postgres 按
    # 128 位整数比较 uuid，全零是可能的最小值）。
    cursor_zero=("-infinity", "00000000-0000-0000-0000-000000000000"),
)


# ---- v2_conversation_summary：PK 单列 user_id（一行/用户），summary_envelope
# 可为 NULL（还没生成过摘要）——通用 plaintext_envelope_column 假设信封总存在，
# 直接调用会在 doc=None 时于 transforms._decryptable 里 NPE，需要一层判空。
def _summary_unpack(r: tuple) -> tuple:
    (uid, envelope, watermark_ts, version, updated_at, watermark_seq,
     materialized_segment_ids) = r
    item_id = uid
    sort_val = _iso(updated_at) or ""
    doc = _pack_extra(
        envelope or {}, has_envelope=envelope is not None,
        watermark_ts=watermark_ts, version=version, watermark_seq=watermark_seq,
        materialized_segment_ids=materialized_segment_ids)
    return (uid, item_id, sort_val, doc)


def _summary_transform(doc: dict, decrypt) -> dict:
    (has_envelope,) = _pop_extra(doc, "has_envelope")
    if has_envelope:
        return transforms.plaintext_envelope_column(
            doc, decrypt, purpose=f"tee_replicate:v2_conversation_summary:{doc.get('id', '')}")
    # summary_envelope 本就是 NULL（这个用户还没被生成过摘要）——不是"待解密"，
    # 原样返回（此时 doc 只剩 _pack_extra 塞进来的保留键），upsert_args 据此写 NULL。
    return doc


def _summary_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    watermark_ts, version, watermark_seq, materialized_segment_ids = _pop_extra(
        doc, "watermark_ts", "version", "watermark_seq", "materialized_segment_ids")
    envelope_col = Jsonb(doc) if doc else None
    return (uid, envelope_col, watermark_ts, version, sort, watermark_seq,
            materialized_segment_ids)


_TABLES["v2_conversation_summary"] = _Table(
    select_sql=("SELECT user_id, summary_envelope, watermark_ts, version, updated_at, "
                "watermark_seq, materialized_segment_ids FROM v2_conversation_summary "
                "WHERE (updated_at, user_id) > (%s, %s) ORDER BY updated_at, user_id LIMIT %s"),
    cursor_kind="text",
    transform=_summary_transform,
    upsert_sql=("INSERT INTO v2_conversation_summary "
                "(user_id, summary_envelope, watermark_ts, version, updated_at, "
                "watermark_seq, materialized_segment_ids) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "summary_envelope=EXCLUDED.summary_envelope, "
                "watermark_ts=EXCLUDED.watermark_ts, version=EXCLUDED.version, "
                "updated_at=EXCLUDED.updated_at, watermark_seq=EXCLUDED.watermark_seq, "
                "materialized_segment_ids=EXCLUDED.materialized_segment_ids"),
    unpack=_summary_unpack,
    upsert_args=_summary_upsert_args,
    # 2026-07-28 修正：原注释"每条 UPDATE 都带 updated_at=now()，不需要 requeue"
    # 只在无并发写时成立，见 model_api_credentials 同日修正的同一条理由。
    # jobs_store.py 四处 CAS UPDATE 已接 mirror.mark_pending 兜底。PK 是单列
    # user_id（一行/用户），按 user_id 取回即可，不需要复合 item_id。
    requeue_fetch_sql=("SELECT user_id, summary_envelope, watermark_ts, version, "
                       "updated_at, watermark_seq, materialized_segment_ids "
                       "FROM v2_conversation_summary WHERE user_id = %s"),
    requeue_delete_tee_sql="DELETE FROM v2_conversation_summary WHERE user_id = %s",
    prune_rds_keys_sql="SELECT user_id FROM v2_conversation_summary",
    prune_tee_keys_sql="SELECT user_id FROM v2_conversation_summary",
    requeue_by_user_only=True,
    # 冷启动：updated_at 是 TIMESTAMPTZ，"-infinity" 合法；第二列是 user_id
    # （TEXT），空串本身合法且排在任何真实 user_id（都以 "usr_" 开头，非空）之前。
    cursor_zero=("-infinity", ""),
)


# ---- v2_conversation_summary_segments：segment_id 是 GENERATED ALWAYS AS
# IDENTITY，upsert 必须 OVERRIDING SYSTEM VALUE；ON CONFLICT DO UPDATE 分支绝不
# 能碰 segment_id（UPDATE 语境没有等价写法，写了是硬错误——同 chat_messages.seq
# 的坑，见 worker.py:172 起的注释）。纯 INSERT-only（全仓搜索没有一条 UPDATE）。
#
# ⚠️ 不能只用裸 segment_id 当游标（2026-07-28 审查抓到）：它是 GENERATED ALWAYS
# AS IDENTITY，但 nextval() 的**取号顺序不等于事务提交顺序**——拿到 5 号的事务
# 比拿到 6 号的事务晚提交，而扫描恰好落在两次提交之间，游标推过 6 之后 5 就永远
# 不满足 "segment_id > 6"，永久跳过、不报错、不冻结。这正是本仓库已经在
# chat_messages.seq 上避过的坑（该表的游标用 (ts, msg_id)，seq 只用于事后
# setval 对齐，从不参与游标比较）；segments 的写路径同样是 per-user 并发的多处
# INSERT（jobs_store.py 多个调用点），具备触发条件。改用 (created_at, segment_id)
# 复合游标，与其余 6 张表统一：created_at 是写入这一行那一刻的真实时间，序列号的
# 取号/提交倒挂问题不再影响游标是否漏行（即使两行 created_at 恰好相同，
# segment_id 仍然全局唯一，tie-break 不会漏）。
def _summary_segments_unpack(r: tuple) -> tuple:
    (segment_id, uid, format_version, coverage_kind, level, start_seq, end_seq,
     source_message_count, legacy_opaque_through_seq, child_segment_ids,
     envelope, created_at) = r
    item_id = str(segment_id)
    sort_val = _iso(created_at) or ""
    doc = _pack_extra(
        envelope, segment_id=segment_id, format_version=format_version,
        coverage_kind=coverage_kind, level=level, start_seq=start_seq,
        end_seq=end_seq, source_message_count=source_message_count,
        legacy_opaque_through_seq=legacy_opaque_through_seq,
        child_segment_ids=child_segment_ids, created_at=_iso(created_at))
    return (uid, item_id, sort_val, doc)


def _summary_segments_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    (segment_id, format_version, coverage_kind, level, start_seq, end_seq,
     source_message_count, legacy_opaque_through_seq, child_segment_ids,
     created_at) = _pop_extra(
        doc, "segment_id", "format_version", "coverage_kind", "level",
        "start_seq", "end_seq", "source_message_count",
        "legacy_opaque_through_seq", "child_segment_ids", "created_at")
    return (segment_id, uid, format_version, coverage_kind, level, start_seq,
            end_seq, source_message_count, legacy_opaque_through_seq,
            child_segment_ids, Jsonb(doc), created_at)


_TABLES["v2_conversation_summary_segments"] = _Table(
    select_sql=("SELECT segment_id, user_id, format_version, coverage_kind, level, "
                "start_seq, end_seq, source_message_count, legacy_opaque_through_seq, "
                "child_segment_ids, summary_envelope, created_at "
                "FROM v2_conversation_summary_segments "
                "WHERE (created_at, segment_id) > (%s, %s) "
                "ORDER BY created_at, segment_id LIMIT %s"),
    cursor_kind="text",
    transform=lambda doc, decrypt: transforms.plaintext_envelope_column(
        doc, decrypt,
        purpose=f"tee_replicate:v2_conversation_summary_segments:{doc.get('id', '')}"),
    upsert_sql=("INSERT INTO v2_conversation_summary_segments "
                "(segment_id, user_id, format_version, coverage_kind, level, start_seq, "
                "end_seq, source_message_count, legacy_opaque_through_seq, "
                "child_segment_ids, summary_envelope, created_at) "
                "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (segment_id) DO UPDATE SET "
                "user_id=EXCLUDED.user_id, format_version=EXCLUDED.format_version, "
                "coverage_kind=EXCLUDED.coverage_kind, level=EXCLUDED.level, "
                "start_seq=EXCLUDED.start_seq, end_seq=EXCLUDED.end_seq, "
                "source_message_count=EXCLUDED.source_message_count, "
                "legacy_opaque_through_seq=EXCLUDED.legacy_opaque_through_seq, "
                "child_segment_ids=EXCLUDED.child_segment_ids, "
                "summary_envelope=EXCLUDED.summary_envelope, "
                "created_at=EXCLUDED.created_at"),
    unpack=_summary_segments_unpack,
    upsert_args=_summary_segments_upsert_args,
    requeue_fetch_sql=None,
    requeue_delete_tee_sql=None,
    # 冷启动：created_at 是 TIMESTAMPTZ，"-infinity" 合法；segment_id 是 BIGINT
    # （WHERE 里没有 ::text 转型），空串不是合法 bigint，用数字串 "0"。
    cursor_zero=("-infinity", "0"),
)


# ---- v2_trajectory_reviews：PK 单列 source_job_id，review_envelope 可为 NULL
# （审阅还没跑）。这张表没有 updated_at 之类的列，status/review_envelope/
# claimed_by_job_id 等字段在创建之后由 jobs_store.py/db.py 多处 UPDATE 原地改写，
# 单靠 created_at 排序的追加游标永远看不到这些改写。requeue_fetch_sql/
# requeue_delete_tee_sql 配好之后（Task 7 防御性接的），写侧的
# mirror.mark_pending(..., "v2_trajectory_reviews", ...) 双写已在 Task 9 补上
# （db.py:chat_clear + jobs_store.py 的 claim/finish/reopen/recover 各处 UPDATE），
# 两半齐了这张表才真正吃到 requeue 补偿。
def _trajectory_reviews_unpack(r: tuple) -> tuple:
    (uid, source_job_id, status, attempt_count, claimed_by_job_id, envelope,
     last_error, created_at, started_at, finished_at) = r
    item_id = str(source_job_id)
    sort_val = _iso(created_at) or ""
    doc = _pack_extra(
        envelope or {}, has_envelope=envelope is not None,
        source_job_id=source_job_id, status=status, attempt_count=attempt_count,
        claimed_by_job_id=claimed_by_job_id, last_error=last_error,
        started_at=_iso(started_at), finished_at=_iso(finished_at))
    return (uid, item_id, sort_val, doc)


def _trajectory_reviews_transform(doc: dict, decrypt) -> dict:
    (has_envelope,) = _pop_extra(doc, "has_envelope")
    if has_envelope:
        return transforms.plaintext_envelope_column(
            doc, decrypt, purpose=f"tee_replicate:v2_trajectory_reviews:{doc.get('id', '')}")
    return doc


def _trajectory_reviews_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    (source_job_id, status, attempt_count, claimed_by_job_id, last_error,
     started_at, finished_at) = _pop_extra(
        doc, "source_job_id", "status", "attempt_count", "claimed_by_job_id",
        "last_error", "started_at", "finished_at")
    envelope_col = Jsonb(doc) if doc else None
    return (source_job_id, uid, status, attempt_count, claimed_by_job_id,
            envelope_col, last_error, sort, started_at, finished_at)


_TABLES["v2_trajectory_reviews"] = _Table(
    select_sql=("SELECT user_id, source_job_id, status, attempt_count, "
                "claimed_by_job_id, review_envelope, last_error, created_at, "
                "started_at, finished_at FROM v2_trajectory_reviews "
                "WHERE (created_at, source_job_id) > (%s, %s) "
                "ORDER BY created_at, source_job_id LIMIT %s"),
    cursor_kind="text",
    transform=_trajectory_reviews_transform,
    upsert_sql=("INSERT INTO v2_trajectory_reviews "
                "(source_job_id, user_id, status, attempt_count, claimed_by_job_id, "
                "review_envelope, last_error, created_at, started_at, finished_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (source_job_id) DO UPDATE SET "
                "status=EXCLUDED.status, attempt_count=EXCLUDED.attempt_count, "
                "claimed_by_job_id=EXCLUDED.claimed_by_job_id, "
                "review_envelope=EXCLUDED.review_envelope, "
                "last_error=EXCLUDED.last_error, created_at=EXCLUDED.created_at, "
                "started_at=EXCLUDED.started_at, finished_at=EXCLUDED.finished_at"),
    unpack=_trajectory_reviews_unpack,
    upsert_args=_trajectory_reviews_upsert_args,
    # 读侧配置（Task 7 防御性接的）；写侧 mark_pending 已在 Task 9 补上
    # （db.py:chat_clear + jobs_store.py 的 claim/finish/reopen/recover 各处
    # UPDATE，见上方大段注释），两半齐了，不再是死代码。
    requeue_fetch_sql=("SELECT user_id, source_job_id, status, attempt_count, "
                       "claimed_by_job_id, review_envelope, last_error, created_at, "
                       "started_at, finished_at FROM v2_trajectory_reviews "
                       "WHERE user_id = %s AND source_job_id = %s"),
    requeue_delete_tee_sql=("DELETE FROM v2_trajectory_reviews "
                            "WHERE user_id = %s AND source_job_id = %s"),
    prune_rds_keys_sql="SELECT user_id, source_job_id FROM v2_trajectory_reviews",
    prune_tee_keys_sql="SELECT user_id, source_job_id FROM v2_trajectory_reviews",
    # 冷启动：created_at 是 TIMESTAMPTZ，"-infinity" 合法；source_job_id 是
    # BIGINT（WHERE 里没有 ::text 转型），空串不是合法 bigint，用数字串 "0"。
    cursor_zero=("-infinity", "0"),
)


# ---- v2_workspace_entries：PK (user_id, path)。path 只在单个 user 内唯一，两个
# 不同用户可能取同名 path，item_id 必须把 user_id 也编进去才能全局唯一。
def _workspace_entries_unpack(r: tuple) -> tuple:
    (uid, path, kind, envelope, mime_type, source_ref, revision, created_at,
     updated_at) = r
    # 用 _SEP（\x1f，"text" 游标本来就用它分隔 sort_val/item_id）复合 user_id+path
    # ——与 SQL 侧 "user_id || chr(31) || path" 是同一个字符（十进制 31）。
    item_id = f"{uid}{_SEP}{path}"
    sort_val = _iso(updated_at) or ""
    doc = _pack_extra(envelope, path=path, kind=kind, mime_type=mime_type,
                       source_ref=source_ref, revision=revision,
                       created_at=_iso(created_at))
    return (uid, item_id, sort_val, doc)


def _workspace_entries_upsert_args(uid: str, iid: str, sort, doc: dict) -> tuple:
    path, kind, mime_type, source_ref, revision, created_at = _pop_extra(
        doc, "path", "kind", "mime_type", "source_ref", "revision", "created_at")
    return (uid, path, kind, Jsonb(doc), mime_type, source_ref, revision,
            created_at, sort)


_TABLES["v2_workspace_entries"] = _Table(
    select_sql=("SELECT user_id, path, kind, content_envelope, mime_type, source_ref, "
                "revision, created_at, updated_at FROM v2_workspace_entries "
                "WHERE (updated_at, user_id || chr(31) || path) > (%s, %s) "
                "ORDER BY updated_at, user_id || chr(31) || path LIMIT %s"),
    cursor_kind="text",
    transform=lambda doc, decrypt: transforms.plaintext_envelope_column(
        doc, decrypt, purpose=f"tee_replicate:v2_workspace_entries:{doc.get('id', '')}"),
    upsert_sql=("INSERT INTO v2_workspace_entries "
                "(user_id, path, kind, content_envelope, mime_type, source_ref, "
                "revision, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id, path) DO UPDATE SET "
                "kind=EXCLUDED.kind, content_envelope=EXCLUDED.content_envelope, "
                "mime_type=EXCLUDED.mime_type, source_ref=EXCLUDED.source_ref, "
                "revision=EXCLUDED.revision, updated_at=EXCLUDED.updated_at"),
    unpack=_workspace_entries_unpack,
    upsert_args=_workspace_entries_upsert_args,
    # 2026-07-28 修正：原注释"revision UPDATE 分支硬编码 updated_at=now()，追加
    # 友好，不需要 requeue"只在无并发写时成立，见 model_api_credentials 同日
    # 修正的同一条理由。jobs_store.py:put_workspace_entry_cas 的 UPDATE 分支已接
    # mirror.mark_pending 兜底。PK 是 (user_id, path)：requeue 的 pending 行已经
    # 单独带 user_id 列，item_id 只需存裸 path（不需要 unpack 那种 user_id+path
    # 复合键——那是给全局游标 tie-break 用的，跟这里按 (user_id, path) 两列做
    # WHERE 是两回事）。
    requeue_fetch_sql=("SELECT user_id, path, kind, content_envelope, mime_type, "
                       "source_ref, revision, created_at, updated_at "
                       "FROM v2_workspace_entries WHERE user_id = %s AND path = %s"),
    requeue_delete_tee_sql="DELETE FROM v2_workspace_entries WHERE user_id = %s AND path = %s",
    prune_rds_keys_sql="SELECT user_id, path FROM v2_workspace_entries",
    prune_tee_keys_sql="SELECT user_id, path FROM v2_workspace_entries",
    # 冷启动：updated_at 是 TIMESTAMPTZ，"-infinity" 合法；第二列在 SQL 里已经
    # 用 || 拼成字符串，空串本身就是合法 TEXT 且排在任何非空拼接结果之前。
    cursor_zero=("-infinity", ""),
)


def _key_values(*columns: str) -> Callable[[dict], tuple]:
    def values(key: dict) -> tuple:
        missing = [column for column in columns if column not in key]
        if missing:
            raise ValueError(f"dirty key missing columns: {','.join(missing)}")
        return tuple(key[column] for column in columns)

    return values


# Current-row keyed replay contracts.  The SELECT projection must match each
# table's existing `unpack` function exactly; tests require every ciphertext
# config (including the identity pseudo-table) to have a complete contract.
_KEY_CONTRACTS: dict[str, tuple[str, str, Callable[[dict], tuple]]] = {
    "chat_messages": (
        "SELECT user_id, msg_id, ts, doc, seq, storage_generation FROM chat_messages "
        "WHERE user_id=%s AND msg_id=%s",
        "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
        _key_values("user_id", "msg_id"),
    ),
    "memory_moments": (
        "SELECT user_id, moment_id, occurred_at, doc FROM memory_moments "
        "WHERE user_id=%s AND moment_id=%s",
        "DELETE FROM memory_moments WHERE user_id=%s AND moment_id=%s",
        _key_values("user_id", "moment_id"),
    ),
    "world_book_entries": (
        "SELECT user_id, entry_id, updated_at, doc FROM world_book_entries "
        "WHERE user_id=%s AND entry_id=%s",
        "DELETE FROM world_book_entries WHERE user_id=%s AND entry_id=%s",
        _key_values("user_id", "entry_id"),
    ),
    "identity": (
        "SELECT user_id, doc FROM user_blobs WHERE kind='identity' AND user_id=%s",
        "DELETE FROM user_blobs WHERE user_id=%s AND kind='identity'",
        _key_values("user_id"),
    ),
    "frame_envelopes": (
        "SELECT user_id, frame_id, ts, doc, env_meta, body_key FROM frame_envelopes "
        "WHERE user_id=%s AND frame_id=%s",
        "DELETE FROM frames WHERE user_id=%s AND frame_id=%s",
        _key_values("user_id", "frame_id"),
    ),
    "chat_message_archive": (
        "SELECT user_id, source_seq, msg_id, ts, doc, storage_generation, "
        "clear_generation, cleared_at FROM chat_message_archive "
        "WHERE user_id=%s AND source_seq=%s",
        "DELETE FROM chat_message_archive WHERE user_id=%s AND source_seq=%s",
        _key_values("user_id", "source_seq"),
    ),
    "v2_trajectory_events": (
        "SELECT user_id, job_id, event_index, event_kind, idempotency_key, "
        "payload_bytes, truncated, created_at, payload_envelope "
        "FROM v2_trajectory_events WHERE job_id=%s AND event_index=%s",
        "DELETE FROM v2_trajectory_events WHERE job_id=%s AND event_index=%s",
        _key_values("job_id", "event_index"),
    ),
    "voice_transcripts": (
        "SELECT user_id, call_id, chat_message_id, turn_count, duration_sec, "
        "char_count, created_at, transcript_envelope FROM voice_transcripts "
        "WHERE user_id=%s AND call_id=%s",
        "DELETE FROM voice_transcripts WHERE user_id=%s AND call_id=%s",
        _key_values("user_id", "call_id"),
    ),
    "model_api_credentials": (
        "SELECT id, user_id, provider, label, base_url, api_key_envelope, "
        "api_key_hint, supports_responses, created_at, updated_at "
        "FROM model_api_credentials WHERE id=%s",
        "DELETE FROM model_api_credentials WHERE id=%s",
        _key_values("id"),
    ),
    "v2_conversation_summary": (
        "SELECT user_id, summary_envelope, watermark_ts, version, updated_at, "
        "watermark_seq, materialized_segment_ids FROM v2_conversation_summary "
        "WHERE user_id=%s",
        "DELETE FROM v2_conversation_summary WHERE user_id=%s",
        _key_values("user_id"),
    ),
    "v2_conversation_summary_segments": (
        "SELECT segment_id, user_id, format_version, coverage_kind, level, "
        "start_seq, end_seq, source_message_count, legacy_opaque_through_seq, "
        "child_segment_ids, summary_envelope, created_at "
        "FROM v2_conversation_summary_segments WHERE segment_id=%s",
        "DELETE FROM v2_conversation_summary_segments WHERE segment_id=%s",
        _key_values("segment_id"),
    ),
    "v2_trajectory_reviews": (
        "SELECT user_id, source_job_id, status, attempt_count, claimed_by_job_id, "
        "review_envelope, last_error, created_at, started_at, finished_at "
        "FROM v2_trajectory_reviews WHERE source_job_id=%s",
        "DELETE FROM v2_trajectory_reviews WHERE source_job_id=%s",
        _key_values("source_job_id"),
    ),
    "v2_workspace_entries": (
        "SELECT user_id, path, kind, content_envelope, mime_type, source_ref, "
        "revision, created_at, updated_at FROM v2_workspace_entries "
        "WHERE user_id=%s AND path=%s",
        "DELETE FROM v2_workspace_entries WHERE user_id=%s AND path=%s",
        _key_values("user_id", "path"),
    ),
}

if set(_KEY_CONTRACTS) != set(_TABLES):
    raise RuntimeError(
        "plaintext shadow keyed replay contracts do not cover every ciphertext table"
    )

_TABLES = {
    name: replace(
        cfg,
        key_fetch_sql=_KEY_CONTRACTS[name][0],
        key_delete_sql=_KEY_CONTRACTS[name][1],
        key_params=_KEY_CONTRACTS[name][2],
    )
    for name, cfg in _TABLES.items()
}


def _produce_write(
    cfg: _Table,
    user_id: str,
    item_id: str,
    sort_val,
    doc: dict,
    dry_run: bool,
    target_policy: TargetPolicy | None = None,
):
    """一行 → TEE upsert 参数元组（或 None=已计数但不写，frames dry_run 用）。
    抛 PendingDeviceMigration / 其余异常的语义与 decrypt 路径一致，供 run_table
    的 freeze/pending 共用。"""
    if cfg.row_writer is not None:
        return cfg.row_writer(user_id, item_id, sort_val, doc, dry_run)
    pt_doc = _transform_with_retry(cfg, doc, user_id, target_policy)
    return cfg.upsert_args(user_id, item_id, sort_val, pt_doc)


# --------------------------------------------------------------------------- #
# 游标编解码：把 (sort_val, item_id) ↔ (watermark_ts, watermark_id) 两列互转。
# --------------------------------------------------------------------------- #
def _encode_cursor(cfg: _Table, sort_val, item_id: str) -> tuple[float, str]:
    if cfg.cursor_kind == "numeric":
        return (float(sort_val or 0.0), str(item_id))
    if cfg.cursor_kind == "single":
        return (0.0, str(sort_val or ""))
    return (0.0, f"{sort_val or ''}{_SEP}{item_id}")


def _decode_cursor(cfg: _Table, wm_ts: float, wm_id: str) -> tuple:
    """返回喂给 select WHERE 占位符的参数元组（arity 与该表 cursor 列数匹配）。

    冷启动短路：_read_cursor 对从未跑过的表返回 wm_id=""，下面几个分支会把这个
    空串原样当参数塞进 WHERE (sort_col, id_col) > (%s, %s)。既有 5 张表的排序列
    都是 TEXT，空串是合法下界，没问题；但本轮新增的 7 张表排序列是 TIMESTAMPTZ /
    BIGINT，空串在那两种类型上 cast 直接报错（InvalidDatetimeFormat /
    InvalidTextRepresentation）——SELECT 本身就失败，一行都同步不进去，而
    scheduler 的 per-table try/except 会把这个错误吞掉计入退避，每个 tick 静默
    重复失败、没有任何人能从日志之外发现（2026-07-28 审查抓到）。cursor_zero
    就是给这类表配的、该类型能接受的下界参数元组。
    """
    if not wm_id and cfg.cursor_zero is not None:
        return cfg.cursor_zero
    if cfg.cursor_kind == "numeric":
        return (wm_ts, wm_id)
    if cfg.cursor_kind == "single":
        return (wm_id,)
    sort_val, _, item_id = wm_id.partition(_SEP)
    return (sort_val, item_id)


def _target_pool(target_policy: TargetPolicy | None = None):
    """Keep the legacy pool seam when the new plaintext target is disabled."""
    if target_policy is None:
        return mirror.get_tee_pool()
    return mirror.get_target_pool(target_policy)


def _read_cursor(
    table: str,
    target_policy: TargetPolicy | None = None,
) -> tuple[float, str]:
    with _target_pool(target_policy).connection() as c:
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


def _consume_requeue(
    cfg: _Table,
    table: str,
    target_policy: TargetPolicy | None = None,
) -> tuple[int, int, int, int]:
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
      - PermanentDecryptFailure → the new ciphertext is undecryptable (403
        decrypt_failed); DELETE any stale TEE plaintext row + UPDATE the pending
        reason to the terminal ``decrypt_failed:`` state so it is no longer
        re-consumed as a requeue (mirrors the cursor-loop quarantine branch).
      - any other failure → log/count, LEAVE the pending row so the next pass
        retries (never freezes the cursor — requeue rows are independent).

    Returns (copied, pending, errors, quarantined) deltas folded into the run report.
    """
    if cfg.requeue_fetch_sql is None:
        return (0, 0, 0, 0)
    with _target_pool(target_policy).connection() as dst:
        pend = dst.execute(_REQUEUE_SELECT, (table, "requeue%")).fetchall()
    if not pend:
        return (0, 0, 0, 0)

    copied = pending = errors = quarantined = 0
    with db.get_pool().connection() as src:
        for user_id, item_id in pend:
            key = (user_id,) if cfg.requeue_by_user_only else (user_id, item_id)
            rds_row = src.execute(cfg.requeue_fetch_sql, key).fetchone()
            with _target_pool(target_policy).connection() as dst:
                if rds_row is None:
                    with dst.transaction():
                        dst.execute(cfg.requeue_delete_tee_sql, key)
                        dst.execute(_PENDING_DELETE, (user_id, table, item_id))
                    continue
                uid, iid, sort_val, doc = cfg.unpack(rds_row)
                try:
                    args = _produce_write(
                        cfg, uid, iid, sort_val, doc, False, target_policy
                    )
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
                except transforms.PermanentDecryptFailure as e:
                    # New ciphertext is permanently undecryptable — quarantine
                    # terminal (delete stale TEE plaintext + flip the marker to
                    # decrypt_failed so the requeue lane stops re-consuming it).
                    with dst.transaction():
                        if cfg.requeue_delete_tee_sql is not None:
                            dst.execute(cfg.requeue_delete_tee_sql, key)
                        dst.execute(_PENDING_UPDATE_REASON,
                                    (_decrypt_failed_reason(e), user_id, table, item_id))
                    quarantined += 1
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
    return (copied, pending, errors, quarantined)


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


def finalize_identity_sequence(
    table: str,
    target_policy: TargetPolicy | None = None,
) -> None:
    """Fast-forward a carried identity sequence after a successful full pass."""
    seq_col = _SEQ_TABLES.get(table)
    if not seq_col:
        return
    with _target_pool(target_policy).connection() as dst:
        dst.execute(
            f"SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"GREATEST((SELECT COALESCE(MAX({seq_col}), 1) FROM {table}), 1))",
            (table, seq_col),
        )


def _write_pending_and_cursor(dst, cfg, table, pend_rows, adv_ts, adv_id) -> None:
    """把 PendingDeviceMigration 标记 + 游标推进写进当前(调用方开的)事务。

    pend_rows 是 (user_id, item_id, reason) 三元组。PDM 是终态(local_only /
    无 K_enclave):若同 PK 之前已被复制成 TEE 明文(如 requeue 原地改写后新密文
    不可解),那残留明文是隐私泄漏,须删——与终态 pending 标记同事务删,使 verify
    的 rds == tee + pending 保持平衡。按 PK 幂等 DELETE:没复制过就是 no-op。"""
    for uid, iid, reason in pend_rows:
        if cfg.requeue_delete_tee_sql is not None:
            key = (uid,) if cfg.requeue_by_user_only else (uid, iid)
            dst.execute(cfg.requeue_delete_tee_sql, key)
        dst.execute(_PENDING_UPSERT, (uid, table, iid, reason))
    dst.execute(_CURSOR_UPSERT, (table, adv_ts, adv_id))


def _batch_conn_retries() -> int:
    try:
        return max(1, int(os.environ.get("FEEDLING_TEE_REPLICATE_CONN_RETRIES", "3") or 3))
    except (TypeError, ValueError):
        return 3


def _conn_lost(dst, exc: Exception) -> bool:
    """连接级故障判定:连接已断(broken/closed)或 psycopg OperationalError。
    与「毒行」(明文含 NUL 等,连接仍活、抛 DataError)区分——前者换连接重试有意义,
    后者只能逐行跳过。"""
    if getattr(dst, "broken", False) or getattr(dst, "closed", False):
        return True
    return isinstance(exc, psycopg.OperationalError)


def _flush_batch(
    cfg,
    table,
    writes,
    pend_rows,
    adv_ts,
    adv_id,
    target_policy: TargetPolicy | None = None,
) -> int:
    """整批写 upsert + pending 标记 + 游标推进,返回本批跳过的毒行数。

    两类失败分开处理(2026-07-14 test 根因:direct-TLS 连接经 Phala 网关掉线,
    ``SSL error: unexpected eof`` / ``the connection is lost``,让 chat/memory 整表挂):
      - **连接断**:换一条新连接重试整批(有界 ``_batch_conn_retries`` 次 + 小退避)。
        整批未提交、游标未推进,重试在健康连接上应整批成功。全部重试仍连不上 → 抛,
        由 ``run_table`` 让整表本 tick 失败、游标不动、下 tick 再来(不丢不重)。
      - **活连接上批失败**(疑似毒行):逐行 savepoint 跳过存不了的行(计 skipped),
        其余照写、pending/游标照常推进——毒行不拖垮整表(原有语义,保留)。
    """
    attempts = _batch_conn_retries()
    for attempt in range(1, attempts + 1):
        try:
            with _target_pool(target_policy).connection() as dst:
                try:
                    with dst.transaction():
                        for args in writes:
                            dst.execute(cfg.upsert_sql, args)
                        _write_pending_and_cursor(dst, cfg, table, pend_rows, adv_ts, adv_id)
                    return 0
                except Exception as batch_exc:  # noqa: BLE001
                    if _conn_lost(dst, batch_exc):
                        raise  # 冒到外层 → 换连接重试整批
                    # 活连接、批失败 → 疑似毒行,逐行跳。
                    log.warning("[tee-replicate] %s 批量写失败,降级逐行: %s",
                                table, str(batch_exc)[:80])
                    skipped = 0
                    with dst.transaction():
                        for args in writes:
                            try:
                                with dst.transaction():
                                    dst.execute(cfg.upsert_sql, args)
                            except Exception as row_exc:  # noqa: BLE001
                                skipped += 1
                                log.warning("[tee-replicate] %s 跳过存不了的毒行: %s",
                                            table, str(row_exc)[:80])
                        _write_pending_and_cursor(dst, cfg, table, pend_rows, adv_ts, adv_id)
                    return skipped
        except Exception as conn_exc:  # noqa: BLE001 — 连接级故障:换连接重试
            if attempt >= attempts:
                raise
            log.warning("[tee-replicate] %s 连接掉线,换连接重试 %d/%d: %s",
                        table, attempt, attempts, str(conn_exc)[:80])
            _sleep(0.5 * attempt)
    return 0


def run_keys(
    table: str,
    keys: list[dict],
    *,
    target_policy: TargetPolicy | None = None,
) -> dict:
    """Re-read and apply current authoritative rows for durable dirty keys.

    The event operation is deliberately ignored.  If a delete was followed by
    a reinsertion, the current row is upserted; if an update was followed by a
    delete, the target row is deleted.  Replaying the same key is idempotent.
    """
    cfg = _TABLES[table]
    if cfg.key_fetch_sql is None or cfg.key_delete_sql is None or cfg.key_params is None:
        raise RuntimeError(f"keyed replay is not configured for {table}")
    if target_policy is None:
        target_policy = plaintext_shadow_config.load_target()

    applied = deleted = pending = 0
    with db.get_pool().connection() as src, _target_pool(
        target_policy
    ).connection() as dst:
        for dirty_key in keys:
            params = cfg.key_params(dirty_key)
            source_row = src.execute(cfg.key_fetch_sql, params).fetchone()
            if source_row is None:
                with dst.transaction():
                    dst.execute(cfg.key_delete_sql, params)
                deleted += 1
                continue

            user_id, item_id, sort_val, doc = cfg.unpack(source_row)
            try:
                args = _produce_write(
                    cfg,
                    user_id,
                    item_id,
                    sort_val,
                    doc,
                    False,
                    target_policy,
                )
            except transforms.PendingDeviceMigration:
                # local_only/no-K_enclave rows must have no plaintext target.
                with dst.transaction():
                    dst.execute(cfg.key_delete_sql, params)
                pending += 1
                continue

            with dst.transaction():
                if args is not None:
                    dst.execute(cfg.upsert_sql, args)
            applied += 1

    return {
        "table": table,
        "applied": applied,
        "deleted": deleted,
        "pending": pending,
    }


def run_table(
    table: str,
    *,
    qps: float = 2.0,
    dry_run: bool = False,
    limit: int | None = None,
    target_policy: TargetPolicy | None = None,
) -> dict:
    """把 RDS ``table`` 的密文增量解密复制进 TEE 明文库。

    失败语义（brief）：
      - 单行 decrypt **暂态**失败（网络/502/token）重试 2 次仍败 → 记 errors + 落
        user_logs → **游标冻结在失败行之前**（本批后续行照常写入 TEE，但游标不越过
        失败行；本 run 到此批为止，下次重跑重试）。
      - 单行 decrypt **永久**失败（enclave 403 decrypt_failed：错钥/owner 不符/坏信封）
        → PermanentDecryptFailure → 隔离（终态 pending 行 ``decrypt_failed:`` reason）
        → **游标照常越过**（否则整表回填被队头毒行永久卡死）→ 记 quarantined。
      - local_only / 无 K_enclave → PendingDeviceMigration → upsert pending 表 → 游标照常推进。
      - dry_run：零 TEE 写入（含游标），report 给出 would_copy 计数。
      - 幂等：ON CONFLICT upsert，同水位重放不重不丢。
    """
    if target_policy is None:
        target_policy = plaintext_shadow_config.load_target()
    cfg = _TABLES[table]
    wm_ts, wm_id = _read_cursor(table, target_policy)
    copied = pending = errors = skipped = quarantined = 0
    # Requeue lane first (non-dry-run): drain same-PK in-place rewrites the
    # append-only cursor can't see. Independent of the cursor — its failures
    # never freeze it.
    if not dry_run:
        rq_copied, rq_pending, rq_errors, rq_quarantined = _consume_requeue(
            cfg, table, target_policy
        )
        copied += rq_copied
        pending += rq_pending
        errors += rq_errors
        quarantined += rq_quarantined
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
                    args = _produce_write(
                        cfg,
                        user_id,
                        item_id,
                        sort_val,
                        doc,
                        dry_run,
                        target_policy,
                    )
                except transforms.PendingDeviceMigration as e:
                    pend_rows.append((user_id, item_id, _pdm_reason(e)))
                    pending += 1
                    if not frozen:
                        adv_ts, adv_id = _encode_cursor(cfg, sort_val, item_id)
                    continue
                except transforms.PermanentDecryptFailure as e:
                    # 永久毒行：终态隔离（同 PDM 走 pend_rows→pending 表），游标越过它，
                    # **不** frozen/batch_failed——否则整表被队头毒行永久卡死。
                    pend_rows.append((user_id, item_id, _decrypt_failed_reason(e)))
                    quarantined += 1
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
                # 整批写 + pending + 游标推进单事务(见 _flush_batch)。两类失败分开:
                # 连接断 → 换连接重试整批;活连接批失败(毒行) → 逐行 savepoint 跳过,
                # 让毒行不拖垮整表。返回本批跳过的毒行数。
                skipped += _flush_batch(
                    cfg,
                    table,
                    writes,
                    pend_rows,
                    adv_ts,
                    adv_id,
                    target_policy,
                )

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
        # See _SEQ_TABLES above: keep future TEE-primary inserts collision-free.
        finalize_identity_sequence(table, target_policy)

    report = {"table": table, "copied": copied, "pending": pending, "errors": errors,
              "skipped": skipped, "quarantined": quarantined,
              "watermark_ts": wm_ts, "watermark_id": wm_id}
    log.info("[tee-replicate] %s", report)
    return report
