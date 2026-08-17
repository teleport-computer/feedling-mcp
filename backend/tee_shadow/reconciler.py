"""RDS→TEE 明文表全表收敛：首次=存量回填，周期=双写失败补偿（spec §5.1【补充】）。

reconciler 直接用 db.get_pool() / mirror.get_tee_pool() 读写，绕开
mirror.execute()：那是「尽力而为、失败只计数」的影子写路径，reconciler 恰恰是
用来补它漏写的坑，所以自己必须在失败时炸掉（不能重蹈同样的静默丢弃）。

列清单以 backend/alembic_tee/versions/0001_tee_baseline.py 为准（逐表对照过
backend/alembic/versions/ 的原始 RDS DDL，二者列定义字节对齐——TEE baseline
文档本身也说明是照抄）。brief 里附带的 TABLES 草稿有多处失真，已在下面每表
纠正（见 P2T4 报告）。
"""
from __future__ import annotations

import logging

import db
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from tee_shadow import mirror

log = logging.getLogger("feedling.tee_shadow")

# table -> (pk 列元组, 全列 SELECT 列表)。
# 列清单核对来源：backend/alembic_tee/versions/0001_tee_baseline.py（等价于
# backend/alembic/versions/ 0001/0002/0004x2/0006/0008/0010 的原始 RDS DDL）。
TABLES: dict[str, tuple[tuple[str, ...], str]] = {
    "server_config": (("key",), "key, value"),
    "global_blobs": (("key",), "key, doc"),
    "users": (("user_id",), "user_id, created_at, doc"),
    "user_blobs": (("user_id", "kind"), "user_id, kind, doc"),
    # PK 是 (user_id, stream, seq)，不是 (user_id, seq)——多了 stream 列；
    # seq 是 GENERATED ALWAYS AS IDENTITY，见下面 _IDENTITY_TABLES。
    "user_logs": (("user_id", "stream", "seq"), "user_id, stream, seq, ts, item_key, doc"),
    # 主键列叫 item_id，不是 id；还漏了 kind、expires_at。
    "perception_items": (("user_id", "kind", "item_id"), "user_id, kind, item_id, ts, expires_at, doc"),
    # 列叫 date，不是 day；PK 还要带 signal。
    "perception_daily": (("user_id", "date", "signal"), "user_id, date, signal, doc, updated_at"),
    # PK 是 (key, lang)，不是 (key,)——否则同一 key 不同 lang 的行会互相冲突覆盖。
    "copytext_strings": (("key", "lang"), "key, lang, value, updated_at"),
    # 完全不是 key/value 结构：单行哨兵表，PK 是布尔 id，另一列是 revision 计数器。
    "copytext_meta": (("id",), "id, revision"),
    "genesis_import_jobs": (
        ("user_id", "job_id"),
        "user_id, job_id, status, source_kind, file_manifest_hash, total_chunks, "
        "received_chunks, processed_chunks, total_bytes, received_bytes, privacy_mode, "
        "metadata, output, memory_action_count, identity_status, persona_ref, "
        "persona_sha256, error, created_at, updated_at, finalized_at, completed_at",
    ),
    # PK 有三列 (user_id, job_id, output_type)，不是两列；且没有单一 doc 列。
    "genesis_import_outputs": (
        ("user_id", "job_id", "output_type"),
        "user_id, job_id, output_type, ref, status, doc, created_at, updated_at",
    ),
    # Notify Relay（自部署推送中继，0020）。configs 的 PK 是 auth_token（明文中继
    # 凭证）；logs 的 id 是 GENERATED ALWAYS AS IDENTITY，见下 _IDENTITY_TABLES。
    # 注意：TEE 侧 configs 有意不带主库的 device_token UNIQUE（见 alembic_tee
    # 0002 注释）——否则换机顶替 + 镜像漏删 DELETE 时，copy 阶段按 auth_token
    # upsert 会在 prune 之前撞唯一约束，reconciler 恰好治不了漏写。
    "notify_relay_configs": (
        ("auth_token",),
        "auth_token, device_token, user_id, apns_env, disabled, "
        "created_at, updated_at, last_used_at",
    ),
    "notify_relay_logs": (
        ("id",),
        "id, auth_token, push_type, target_token, apns_env, status, err_msg, "
        "content, created_at, updated_at",
    ),
}

# 每表可选的辖区 WHERE 子句：不满足的行完全不归本 reconciler 管——既不 copy、
# 不 prune，也不计入 rds_rows/tee_rows（两侧计数套同一 WHERE，CLI 的
# rds_rows != tee_rows → exit 1 判据才有意义；代价是报告口径 = 辖区内行数，
# 不是全表行数）。
#
# user_blobs 排除 kind='identity'：identity 归 tee_replicator 明文化——RDS 里
# 是密文信封、TEE 里是 replicator 落的明文版本，密文绝不能盖明文；TEE 侧的
# 明文 identity 行也不能被当 orphan prune 掉。
#
# 排除 kind='consumer_state'：db.set_blob 有意不镜像它（它是每次 /v1/chat/poll 都写
# 一次的最热写，且只是 runner 侧运维状态、不是用户数据——见该函数注释）。辖区必须与
# 镜像端一致：否则 reconciler 会把镜像端故意不写的行 copy 回 TEE、并在两侧计数中要求
# 它存在，等于把刚摘掉的负载又装回去。已在 TEE 里的历史 consumer_state 行会被 prune
# 收敛掉（辖区外 = orphan），这是期望行为。
#
# 其余 kind（如 model_api 的 provider-key 信封）有意原样镜像（credential 保持加密）。
_SCOPE_WHERE: dict[str, str] = {
    "user_blobs": "kind NOT IN ('identity', 'consumer_state')",
}

# 有 GENERATED ALWAYS AS IDENTITY 列的表 → 该 IDENTITY 列名。普通 INSERT 不能
# 显式塞值，必须 OVERRIDING SYSTEM VALUE 才能让 RDS 的 seq 原样搬到 TEE（否则
# TEE 会按自己的序列生成一个不同的 seq，破坏行内容一致性 + 打破依赖 seq 排序的
# 读路径）。回填后还必须 setval 该序列到已复制的 MAX（见 reconcile_table 末尾），
# 否则 TEE 侧后续普通 INSERT 会从序列起点重新发号、与已搬入的高 seq 撞 PK。
_IDENTITY_TABLES: dict[str, str] = {"user_logs": "seq", "notify_relay_logs": "id"}

BATCH = 1000


def _wrap_jsonb(row: tuple) -> tuple:
    """psycopg3 读回的 jsonb 值是普通 dict 或 list（JSON 数组，例如
    global_blobs 的 access_link_tokens 整个 doc 就是顶层数组）；原样作为参数
    回插会被拒绝或被 psycopg 误适配成 PG 数组（"cannot adapt type 'dict'" /
    静默按数组类型走）,必须显式包一层 Jsonb()。"""
    return tuple(Jsonb(v) if isinstance(v, (dict, list)) else v for v in row)


def _json_safe_cursor(vals: tuple) -> list | None:
    """The pk tuple as a JSON-serializable list for the resume cursor, or None if
    any component isn't natively JSON (e.g. perception_daily's DATE pk). None ⇒
    skip cursor persistence for that table — it reconciles from the top each
    window, which is fine for the small non-text/number-pk tables. bool is a JSON
    scalar (and an int subclass) so copytext_meta's boolean pk is fine."""
    out: list = []
    for v in vals:
        if v is None or isinstance(v, (str, int, float, bool)):
            out.append(v)
        else:
            return None
    return out


def reconcile_table(table: str, *, prune: bool = True) -> dict:
    pk, cols = TABLES[table]
    col_list = [c.strip() for c in cols.split(",")]
    pk_idx = [col_list.index(c) for c in pk]
    placeholders = ", ".join(["%s"] * len(col_list))
    conflict = ", ".join(pk)
    non_pk = [c for c in col_list if c not in pk]
    overriding = " OVERRIDING SYSTEM VALUE" if table in _IDENTITY_TABLES else ""
    if non_pk:
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
        upsert = (f"INSERT INTO {table} ({cols}){overriding} VALUES ({placeholders}) "
                  f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}")
    else:
        upsert = (f"INSERT INTO {table} ({cols}){overriding} VALUES ({placeholders}) "
                  f"ON CONFLICT ({conflict}) DO NOTHING")

    order_by = ", ".join(pk)
    pk_placeholders = ", ".join(["%s"] * len(pk))
    scope = _SCOPE_WHERE.get(table)  # 辖区过滤：copy/prune/两侧计数全部套用
    scope_and = f"{scope} AND " if scope else ""
    scope_where = f" WHERE {scope}" if scope else ""
    select_page = f"SELECT {cols} FROM {table}{scope_where} ORDER BY {order_by} LIMIT %s"
    select_page_after = (f"SELECT {cols} FROM {table} WHERE {scope_and}"
                         f"({order_by}) > ({pk_placeholders}) "
                         f"ORDER BY {order_by} LIMIT %s")

    copied = pruned = rds_rows = 0
    with db.get_pool().connection() as src, mirror.get_tee_pool().connection() as dst:
        # Keyset pagination on the pk (not a server-side named cursor): both
        # pools run autocommit=True, and named cursors need an explicit
        # transaction wrapper under autocommit — plain LIMIT/keyset avoids
        # that entirely and handles composite pks via row-value comparison.
        #
        # Resume from a persisted cursor if one exists (db.save/load_reconcile_
        # cursor): the scheduler lives in a max_requests-recycled worker, so a
        # large table's backfill spans several worker lifetimes — without this it
        # restarted from row 1 every recycle and never finished (see alembic 0018).
        # Already-copied rows before the cursor stay in TEE; we just skip re-scanning
        # them. (rds_rows below then counts only THIS window's rows, not the whole
        # table — an informational metric; convergence is checked by verify, and
        # prune still does a full two-sided keyset diff regardless.)
        saved = db.load_reconcile_cursor(table)
        last: tuple | None = tuple(saved) if saved else None
        skipped = 0
        while True:
            if last is None:
                rows = src.execute(select_page, (BATCH,)).fetchall()
            else:
                rows = src.execute(select_page_after, (*last, BATCH)).fetchall()
            if not rows:
                break
            rds_rows += len(rows)
            try:
                with dst.transaction(), dst.cursor() as cur:
                    if table == "notify_relay_configs":
                        # Once the TEE schema is primary-capable it carries the
                        # same UNIQUE(device_token) contract as RDS.  A missed
                        # shadow DELETE can therefore leave an old auth_token
                        # occupying the token before the normal PK upsert runs.
                        # Remove only that displaced binding, in the same
                        # transaction as its replacement; the source UNIQUE
                        # guarantees at most one replacement per device token.
                        auth_idx = col_list.index("auth_token")
                        device_idx = col_list.index("device_token")
                        cur.executemany(
                            "DELETE FROM notify_relay_configs "
                            "WHERE device_token = %s AND auth_token <> %s",
                            [(row[device_idx], row[auth_idx]) for row in rows],
                        )
                    # executemany(而非逐行 execute):psycopg3 对同一条 SQL 的多组参数
                    # 自动走 pipeline,把整批的往返压成一次网络交换。reconcile 经 Phala
                    # 网关 direct-TLS,单行往返延迟正是大表(user_logs 38 万行)回填慢到
                    # 几小时的根源(2026-07-14 prod 实测),批量化把它砍掉一两个数量级。
                    # 注意:executemany 是 cursor 方法(connection 只有 execute 快捷方式)。
                    cur.executemany(upsert, [_wrap_jsonb(row) for row in rows])
                copied += len(rows)
            except pg_errors.ForeignKeyViolation:
                # 并发账号删除(CASCADE)会让本批个别子行的 parent(users)在 TEE 里
                # 已消失 —— reconciler 逐表读快照、非单事务,故读到瞬时跨表不一致。
                # 严格外键拒绝整批。降级逐行:好行照写,孤儿行跳过(它们在 RDS 也
                # 会被 CASCADE 删,下一趟 prune 收敛)。绝不因此让整个 reconcile 崩。
                for row in rows:
                    try:
                        with dst.transaction():
                            dst.execute(upsert, _wrap_jsonb(row))
                        copied += 1
                    except pg_errors.ForeignKeyViolation:
                        skipped += 1
            last = tuple(rows[-1][i] for i in pk_idx)
            # Persist progress AFTER the TEE write committed, so the cursor never
            # points past rows that aren't in TEE; an interrupted backfill resumes
            # from here, not row 1. Skipped orphans don't matter (prune reconciles
            # them). Non-JSON-safe pk (perception_daily DATE) ⇒ no cursor, table
            # just re-scans from the top next window (it's small).
            cur_json = _json_safe_cursor(last)
            if cur_json is not None:
                db.save_reconcile_cursor(table, cur_json)
        if skipped:
            log.warning("[reconcile] %s: 跳过 %d 条孤儿行(parent 并发删除)", table, skipped)

        if prune:
            pk_cols = ", ".join(pk)
            # 两侧 key 集合都套辖区 WHERE：辖区外的行（如 replicator 拥有的
            # TEE 明文 identity 行）根本不进集合，自然不会被当 orphan 删掉。
            src_keys = {tuple(r) for r in
                        src.execute(f"SELECT {pk_cols} FROM {table}{scope_where}")}
            tee_keys = {tuple(r) for r in
                        dst.execute(f"SELECT {pk_cols} FROM {table}{scope_where}")}
            cond = " AND ".join(f"{c} = %s" for c in pk)
            for key in tee_keys - src_keys:
                dst.execute(f"DELETE FROM {table} WHERE {cond}", key)
                pruned += 1

        # IDENTITY 列的表：回填后把 TEE 侧序列 setval 到已复制的 MAX(seq)，
        # 否则后续在 TEE 直接 INSERT 的行会从序列起点重新发号、与搬入的高 seq
        # 撞 PK。GREATEST(...,1) 兜住空表（COALESCE(MAX,1) 也已兜 NULL）。
        seq_col = _IDENTITY_TABLES.get(table)
        if seq_col:
            dst.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                f"GREATEST((SELECT COALESCE(MAX({seq_col}), 1) FROM {table}), 1))",
                (table, seq_col))

        # tee_rows 与 rds_rows 同口径（都只数辖区内的行），CLI 的收敛判据
        # rds_rows == tee_rows 才不会被辖区外的 identity 行数差打破。
        tee_rows = dst.execute(
            f"SELECT count(*) FROM {table}{scope_where}").fetchone()[0]

    # This table's backfill pass finished (copy loop exhausted + prune done) —
    # drop the resume cursor so the NEXT periodic reconcile starts from the top,
    # re-catching updates the incremental resume would otherwise skip. Best-effort;
    # a stale cursor at worst costs one redundant re-scan on the next pass.
    db.clear_reconcile_cursor(table)

    report = {"table": table, "copied": copied, "pruned": pruned,
              "skipped": skipped, "rds_rows": rds_rows, "tee_rows": tee_rows}
    log.info("[reconcile] %s", report)
    return report


def reconcile_all() -> list[dict]:
    return [reconcile_table(t) for t in TABLES]
