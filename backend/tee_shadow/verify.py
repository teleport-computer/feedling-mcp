"""RDS↔TEE 一致性验证 job（停 RDS gate 硬条件，spec §5.3 / Task 7）。

只读：两库零写入（连游标/pending 表都不碰）。两层核验：

1. **行数对比**（per-table per-user）——明文表复用 ``reconciler.TABLES`` /
   ``reconciler._SCOPE_WHERE`` 同一辖区口径（user_blobs 排除 identity），
   RDS/TEE 计数直接相等；密文表额外算上 ``tee_pending_device_migration``：
   ``rds == tee + pending``（每张表每个 user_id 一个三元组，见 ``_table_report``）。
   这里的 ``pending`` **只算终态 reason**（``NOT LIKE 'requeue%'`` ——
   ``visibility_local_only`` / PendingDeviceMigration 的 ``pdm:...``），**不含**
   requeue 行——见 ``_split_pending`` 的详细口径说明；requeue 行数单独作为
   ``requeue_backlog`` 计入每张密文表的报告（信息性，不参与 ``rows_ok``）。
   没有 user_id 列的明文表（server_config/global_blobs/copytext_strings/
   copytext_meta/agent_runtime_supervisor_heartbeats）退化成单一全局桶
   （``_GLOBAL`` 哨兵 key）。

2. **抽样字段比对**——按 ``sample_rate`` 抽 RDS 行：明文表直接逐列比（无需
   decrypt）；密文表（chat/memory/world_book/identity）经注入的
   ``decrypt(envelope, purpose) -> bytes``（与 ``tee_replicator.transforms``
   /``tee_replicator.worker`` 同一回调形状，生产走 enclave、测试打桩）解密后，
   用 ``tee_replicator.transforms`` 同一套函数生成期望明文 doc，与 TEE 行逐
   字段比对（覆盖 chat 主体 + thinking/caption 子信封）；frames 只比
   meta/指针字段存在性 + sha256 非空——本体是存储层重加密，verify 不重拉 R2、
   不调 enclave storage 接口（该逻辑正被并行任务改动，verify 只读两库的行）。

抽样标记为**终态** pending（``tee_pending_device_migration``，reason 不是
``requeue%``）的行会被跳过，不当 mismatch：它们本就不该有 TEE 对应行。
requeue 行则**不**跳过——backlog 未清空时，当前 RDS 明文与尚未刷新的 TEE 行
比对可能真的对不上，这是「该刷新了」的诚实信号，不是 verify 的 bug（见
``_split_pending``）。停 RDS gate 前应先跑 replicate 把 requeue 清空
（``requeue_backlog`` 应读 0）——见 deploy/DEPLOYMENTS.md。
"""
from __future__ import annotations

import logging
import math
from typing import Callable

import db
from tee_replicator import transforms
from tee_replicator import worker as _worker
from tee_shadow import mirror, reconciler

log = logging.getLogger("feedling.tee_shadow")

# 没有 user_id 列的明文表：行数核算退化成单一全局桶。
_GLOBAL = "__global__"

_TRANSFORM: dict[str, Callable[[dict, Callable], dict]] = {
    "chat": transforms.plaintext_chat_doc,
    "memory": transforms.plaintext_memory_doc,
    "world_book": transforms.plaintext_world_book_doc,
    "identity": transforms.plaintext_identity_doc,
}

# key -> 密文表映射配置。key 同时是 tables/mismatches 报告里用到的表名。
# - rds_table/tee_table：两侧物理表名（frames 与 identity 都不是简单同名）。
# - rds_where/tee_where：识别子集的 WHERE 子句（仅 identity 用到：user_blobs
#   两侧都要按 kind='identity' 过滤，identity 的 TEE 落点见 tee_replicator.worker
#   的 "identity" _Table 项）。
# - item_col：RDS 侧的行内 item 列名；identity 没有独立 id 列（一行/用户），
#   借用 user_id 本身。
# - pending_table：tee_pending_device_migration.table_name 的取值——与
#   tee_replicator.worker._TABLES 的 dict key 一致（frames 用 "frame_envelopes"，
#   identity 用 "identity"，其余同表名）。
# - pending_by_user_only：identity 的 pending 行 item_id 是常量 "identity"
#   （worker.py 的 unpack 把它写死），不对应 RDS 侧的 item 值，只能按 user_id 判定。
_CIPHERTEXT_TABLES: dict[str, dict] = {
    "chat_messages": dict(
        rds_table="chat_messages", tee_table="chat_messages",
        item_col="msg_id", pending_table="chat_messages", kind="chat"),
    "memory_moments": dict(
        rds_table="memory_moments", tee_table="memory_moments",
        item_col="moment_id", pending_table="memory_moments", kind="memory"),
    "world_book_entries": dict(
        rds_table="world_book_entries", tee_table="world_book_entries",
        item_col="entry_id", pending_table="world_book_entries", kind="world_book"),
    "frame_envelopes": dict(
        rds_table="frame_envelopes", tee_table="frames",
        item_col="frame_id", pending_table="frame_envelopes", kind="frames"),
    "identity": dict(
        rds_table="user_blobs", tee_table="user_blobs",
        rds_where="kind = 'identity'", tee_where="kind = 'identity'",
        item_col="user_id", pending_table="identity", kind="identity",
        pending_by_user_only=True),
}


def _make_decrypt(user_id: str) -> Callable[[dict, str], bytes]:
    """注入点：测试 monkeypatch 本符号。默认委托 tee_replicator.worker 同款
    per-user enclave decrypt token 铸造路径（生产走 enclave），保持与
    replicator 一致的行为，不重新发明一套 token 铸造逻辑。verify 是一次性只读
    扫描，不需要 worker 那套 TTL 缓存，每个 user_id 在一次 run() 内只铸一次。"""
    return _worker._make_decrypt(user_id)


def _get_decrypt(cache: dict[str, Callable], user_id: str) -> Callable[[dict, str], bytes]:
    fn = cache.get(user_id)
    if fn is None:
        fn = _make_decrypt(user_id)
        cache[user_id] = fn
    return fn


def _sample_size(total: int, sample_rate: float) -> int:
    if total <= 0:
        return 0
    return max(1, math.ceil(total * sample_rate))


def _table_report(rds_counts: dict, tee_counts: dict, pending_counts: dict | None = None) -> dict:
    """rds == tee + pending 的核算，per user_id（或 _GLOBAL 哨兵桶）。"""
    pending_counts = pending_counts or {}
    users = set(rds_counts) | set(tee_counts) | set(pending_counts)
    diffs = {}
    for u in users:
        r, t, p = rds_counts.get(u, 0), tee_counts.get(u, 0), pending_counts.get(u, 0)
        if r != t + p:
            diffs[u] = {"rds": r, "tee": t, "pending": p}
    return {
        "rds_rows": sum(rds_counts.values()),
        "tee_rows": sum(tee_counts.values()),
        "pending_rows": sum(pending_counts.values()),
        "user_diffs": diffs,
        "rows_ok": not diffs,
    }


# --------------------------------------------------------------------------- #
# 明文表：行数 + 抽样字段比对（无需 decrypt）。
# --------------------------------------------------------------------------- #
def _grouped_counts(conn, table: str, scope_where: str, has_user: bool) -> dict:
    if has_user:
        rows = conn.execute(
            f"SELECT user_id, count(*) FROM {table}{scope_where} GROUP BY user_id").fetchall()
        return {r[0]: r[1] for r in rows}
    n = conn.execute(f"SELECT count(*) FROM {table}{scope_where}").fetchone()[0]
    return {_GLOBAL: n} if n else {}


def _plaintext_table_report(table: str) -> dict:
    pk, cols = reconciler.TABLES[table]
    col_list = [c.strip() for c in cols.split(",")]
    has_user = "user_id" in col_list
    scope = reconciler._SCOPE_WHERE.get(table)
    scope_where = f" WHERE {scope}" if scope else ""
    with db.get_pool().connection() as src, mirror.get_tee_pool().connection() as dst:
        rds_counts = _grouped_counts(src, table, scope_where, has_user)
        tee_counts = _grouped_counts(dst, table, scope_where, has_user)
    return _table_report(rds_counts, tee_counts)


def _sample_plaintext(table: str, sample_rate: float) -> list[dict]:
    pk, cols = reconciler.TABLES[table]
    col_list = [c.strip() for c in cols.split(",")]
    scope = reconciler._SCOPE_WHERE.get(table)
    scope_where = f" WHERE {scope}" if scope else ""

    with db.get_pool().connection() as src:
        total = src.execute(f"SELECT count(*) FROM {table}{scope_where}").fetchone()[0]
        n = _sample_size(total, sample_rate)
        if n == 0:
            return []
        rows = src.execute(
            f"SELECT {cols} FROM {table}{scope_where} ORDER BY random() LIMIT %s", (n,)
        ).fetchall()
    if not rows:
        return []

    mismatches: list[dict] = []
    cond = " AND ".join(f"{c} = %s" for c in pk)
    with mirror.get_tee_pool().connection() as dst:
        for row in rows:
            rds_row = dict(zip(col_list, row))
            pk_vals = [rds_row[c] for c in pk]
            user_id = rds_row.get("user_id", _GLOBAL)
            item_id = ":".join(str(v) for v in pk_vals)
            tee_row = dst.execute(f"SELECT {cols} FROM {table} WHERE {cond}", pk_vals).fetchone()
            if tee_row is None:
                mismatches.append({"table": table, "user_id": user_id,
                                    "item_id": item_id, "field": "<row-missing>"})
                continue
            tee_dict = dict(zip(col_list, tee_row))
            for c in col_list:
                if rds_row[c] != tee_dict[c]:
                    mismatches.append({"table": table, "user_id": user_id,
                                        "item_id": item_id, "field": c})
    return mismatches


# --------------------------------------------------------------------------- #
# 密文表：行数（含 pending 三元组）+ 抽样解密比对（或 frames 的存在性检查）。
# --------------------------------------------------------------------------- #
def _pending_rows(cfg: dict) -> list[tuple[str, str, str]]:
    """ALL pending rows for the table, with their ``reason`` (needed by
    ``_split_pending`` below to tell terminal markers from requeue-lane ones)."""
    with mirror.get_tee_pool().connection() as dst:
        return dst.execute(
            "SELECT user_id, item_id, reason FROM tee_pending_device_migration "
            "WHERE table_name = %s", (cfg["pending_table"],)).fetchall()


def _split_pending(
    pending_rows: list[tuple[str, str, str]],
) -> tuple[list[tuple[str, str]], int]:
    """Split a table's pending rows into (terminal rows, requeue backlog count).

    Only TERMINAL reasons (``NOT LIKE 'requeue%'`` — ``visibility_local_only``,
    or a PendingDeviceMigration-derived ``pdm:...`` reason, see
    ``tee_replicator.worker._pdm_reason``) belong in the ``rds == tee + pending``
    row-count equation: those items genuinely have no TEE counterpart and never
    will (local_only / no K_enclave), so RDS's row must be balanced by exactly
    one pending row.

    Requeue-lane rows (``reason LIKE 'requeue%'``, same prefix match as
    ``tee_replicator.worker._REQUEUE_SELECT``) mark a same-PK IN-PLACE REWRITE
    (identity edit, visibility swap, memory/world_book back-dated insert or
    edit) — the row being requeued typically still HAS a TEE counterpart (the
    prior version, not yet refreshed by the next worker pass). Counting them in
    the row-count equation would double-count that row (once via `tee`, once
    via `pending`) and falsely report a missing row while a normal backlog is
    just draining. So they're excluded from the equation and reported
    separately as an informational ``requeue_backlog`` count instead (see
    ``run()``) — non-zero is expected/healthy mid-backlog, not a defect.

    Content sampling (``_sample_ciphertext_content`` / ``_sample_frames``)
    deliberately does NOT skip requeue rows either (unlike terminal rows, which
    it does skip): comparing the CURRENT RDS plaintext against a not-yet-
    refreshed TEE row can legitimately surface a content mismatch while the
    backlog hasn't drained — that's an honest signal the row is due for a
    refresh, not a verify bug. Run `python -m backend.tee_replicator run
    --table <t>` to drain the requeue lane (backlog should read 0) before
    treating a subsequent `verify` run's `ok` as a hard gate — see
    deploy/DEPLOYMENTS.md's TEE Postgres Phase 1 acceptance note.
    """
    terminal = [(uid, iid) for uid, iid, reason in pending_rows
                if not (reason or "").startswith("requeue")]
    requeue_backlog = sum(1 for _uid, _iid, reason in pending_rows
                          if (reason or "").startswith("requeue"))
    return terminal, requeue_backlog


def _ciphertext_table_report(cfg: dict, pending_rows: list[tuple[str, str]]) -> dict:
    rds_where = f" WHERE {cfg['rds_where']}" if cfg.get("rds_where") else ""
    tee_where = f" WHERE {cfg['tee_where']}" if cfg.get("tee_where") else ""
    with db.get_pool().connection() as src, mirror.get_tee_pool().connection() as dst:
        rds_counts = dict(src.execute(
            f"SELECT user_id, count(*) FROM {cfg['rds_table']}{rds_where} "
            "GROUP BY user_id").fetchall())
        tee_counts = dict(dst.execute(
            f"SELECT user_id, count(*) FROM {cfg['tee_table']}{tee_where} "
            "GROUP BY user_id").fetchall())
    pending_counts: dict[str, int] = {}
    for uid, _iid in pending_rows:
        pending_counts[uid] = pending_counts.get(uid, 0) + 1
    return _table_report(rds_counts, tee_counts, pending_counts)


def _diff_docs(table: str, user_id: str, item_id: str, expected: dict, actual, prefix: str = "") -> list[dict]:
    out = []
    if not isinstance(actual, dict):
        return [{"table": table, "user_id": user_id, "item_id": item_id, "field": prefix.rstrip(".") or "<row>"}]
    for k, v in expected.items():
        field = f"{prefix}{k}"
        av = actual.get(k)
        if isinstance(v, dict) and isinstance(av, dict):
            out.extend(_diff_docs(table, user_id, item_id, v, av, prefix=f"{field}."))
        elif v != av:
            out.append({"table": table, "user_id": user_id, "item_id": item_id, "field": field})
    return out


def _sample_ciphertext_content(key: str, cfg: dict, sample_rate: float,
                                pending_rows: list[tuple[str, str]],
                                decrypt_cache: dict[str, Callable]) -> list[dict]:
    rds_where = f" WHERE {cfg['rds_where']}" if cfg.get("rds_where") else ""
    tee_where_extra = f" AND {cfg['tee_where']}" if cfg.get("tee_where") else ""
    item_col = cfg["item_col"]
    transform = _TRANSFORM[cfg["kind"]]

    if cfg.get("pending_by_user_only"):
        skip = {uid for uid, _iid in pending_rows}
    else:
        skip = {(uid, iid) for uid, iid in pending_rows}

    with db.get_pool().connection() as src:
        total = src.execute(f"SELECT count(*) FROM {cfg['rds_table']}{rds_where}").fetchone()[0]
        n = _sample_size(total, sample_rate)
        if n == 0:
            return []
        rows = src.execute(
            f"SELECT user_id, {item_col}, doc FROM {cfg['rds_table']}{rds_where} "
            "ORDER BY random() LIMIT %s", (n,)).fetchall()

    mismatches: list[dict] = []
    item_cond = "user_id = %s" if item_col == "user_id" else f"user_id = %s AND {item_col} = %s"
    with mirror.get_tee_pool().connection() as dst:
        for user_id, item_id, doc in rows:
            if cfg.get("pending_by_user_only"):
                if user_id in skip:
                    continue
            elif (user_id, item_id) in skip:
                continue
            decrypt = _get_decrypt(decrypt_cache, user_id)
            try:
                expected = transform(doc, decrypt)
            except transforms.PendingDeviceMigration:
                # RDS 侧刚变成不可解（local_only/无 K_enclave），worker 还没来
                # 得及落 pending 行——不是一个真实的内容 mismatch，跳过（行数
                # 核算那一侧会照实反映出 rds>tee，若确实不该发生会在那里报出）。
                continue
            params = (user_id,) if item_col == "user_id" else (user_id, item_id)
            tee_row = dst.execute(
                f"SELECT doc FROM {cfg['tee_table']} WHERE {item_cond}{tee_where_extra}",
                params).fetchone()
            if tee_row is None:
                mismatches.append({"table": key, "user_id": user_id,
                                    "item_id": item_id, "field": "<row-missing>"})
                continue
            mismatches.extend(_diff_docs(key, user_id, item_id, expected, tee_row[0]))
    return mismatches


def _sample_frames(key: str, cfg: dict, sample_rate: float,
                    pending_rows: list[tuple[str, str]]) -> list[dict]:
    """frames 抽样只查存在性——meta/指针字段是否落地 + sha256 非空。不解密、
    不重拉 R2：本体密文经存储层重加密后直接躺在 R2，verify 只读两库的行。"""
    skip = {(uid, iid) for uid, iid in pending_rows}
    with db.get_pool().connection() as src:
        total = src.execute(f"SELECT count(*) FROM {cfg['rds_table']}").fetchone()[0]
        n = _sample_size(total, sample_rate)
        if n == 0:
            return []
        rows = src.execute(
            f"SELECT user_id, {cfg['item_col']} FROM {cfg['rds_table']} "
            "ORDER BY random() LIMIT %s", (n,)).fetchall()

    mismatches: list[dict] = []
    with mirror.get_tee_pool().connection() as dst:
        for user_id, item_id in rows:
            if (user_id, item_id) in skip:
                continue
            row = dst.execute(
                f"SELECT meta, body_storage_key, body_storage_key_version, body_sha256 "
                f"FROM {cfg['tee_table']} WHERE user_id = %s AND {cfg['item_col']} = %s",
                (user_id, item_id)).fetchone()
            if row is None:
                mismatches.append({"table": key, "user_id": user_id,
                                    "item_id": item_id, "field": "<row-missing>"})
                continue
            meta, storage_key, storage_key_version, sha256 = row
            if meta is None:
                mismatches.append({"table": key, "user_id": user_id, "item_id": item_id, "field": "meta"})
            if not storage_key:
                mismatches.append({"table": key, "user_id": user_id, "item_id": item_id,
                                    "field": "body_storage_key"})
            if not storage_key_version:
                mismatches.append({"table": key, "user_id": user_id, "item_id": item_id,
                                    "field": "body_storage_key_version"})
            if not sha256:
                mismatches.append({"table": key, "user_id": user_id, "item_id": item_id,
                                    "field": "body_sha256"})
    return mismatches


def run(*, sample_rate: float = 0.02) -> dict:
    """RDS↔TEE 一致性验证：只读扫描，返回
    ``{"tables": {...}, "mismatches": [...], "ok": bool}``。

    ``ok`` = 全部表的行数核算相等（含密文表的 rds==tee+pending）且抽样零字段
    mismatch。CLI（``python -m tee_shadow verify``）拿 ``ok`` 做 exit code。
    """
    tables: dict[str, dict] = {}
    mismatches: list[dict] = []
    decrypt_cache: dict[str, Callable] = {}

    for table in reconciler.TABLES:
        tables[table] = _plaintext_table_report(table)
        mismatches.extend(_sample_plaintext(table, sample_rate))

    for key, cfg in _CIPHERTEXT_TABLES.items():
        pending_rows = _pending_rows(cfg)
        terminal_rows, requeue_backlog = _split_pending(pending_rows)
        table_report = _ciphertext_table_report(cfg, terminal_rows)
        # Informational only — does NOT feed rows_ok/ok (see _split_pending's
        # docstring for why requeue rows are excluded from the row-count
        # equation itself).
        table_report["requeue_backlog"] = requeue_backlog
        tables[key] = table_report
        if cfg["kind"] == "frames":
            mismatches.extend(_sample_frames(key, cfg, sample_rate, terminal_rows))
        else:
            mismatches.extend(
                _sample_ciphertext_content(key, cfg, sample_rate, terminal_rows, decrypt_cache))

    rows_ok = all(t["rows_ok"] for t in tables.values())
    report = {"tables": tables, "mismatches": mismatches, "ok": rows_ok and not mismatches}
    log.info("[verify] ok=%s tables=%d mismatches=%d", report["ok"], len(tables), len(mismatches))
    return report
