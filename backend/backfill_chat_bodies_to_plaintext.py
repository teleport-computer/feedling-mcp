"""Inventory R2-offloaded chat bodies before a ciphertext-to-plaintext rewrite.

母计划 Task 1.3。重体（图片/文件/长文）不内联在 ``chat_messages.doc`` 里，而是
以指针形状 ``{body_key, body_ct_len}`` 指向 R2；R2 对象里存的是信封的
``body_ct``。TEE 扶正后这些对象要能被直读，故需一次性明文重写。

## v6 下的范围（与计划原文不同，务必看）

计划的这一条写于 v5「彻底删掉加密」时代。v6 之后加密是**每用户偏好**，所以
**绝不能全量明文化**——加密档用户的重体必须原样保留密文。本工具因此按档位过滤：

- 用户 ``content_encryption == "off"``（显式选明文）→ 处理
- ``"on"`` 或查不到用户 → **跳过**（fail-safe：拿不准就不解密）

与 ``tee_replicator.worker._carries_verbatim`` 同一条判据、同一个失败方向：
「多留了密文」可以事后再跑本工具补上，「明文泄漏」不可逆。

## 当前状态：协议已实现，apply 仍受 rollout 双闸保护

R2 helper 的公开契约是「对象里存 raw ciphertext，读出时重新 base64 成
``body_ct``」；数据库指针也只有密文形状 ``{body_key, body_ct_len, K_enclave...}``。
因此只覆盖 R2 对象会同时破坏三件事：

1. 普通上传 helper 会生成另一个 key，覆盖不到行里持久化的 versioned ``body_key``；
2. 原地覆盖在「对象已改、数据库未改」的崩溃窗口会不可逆地丢掉原密文；
3. 读侧仍会把对象装回 ``body_ct``，客户端继续按密文解，明文对象必然读坏。

缺省仍是只读盘点。真跑必须同时传 ``--apply --allow-plaintext-r2-rewrite``，并由部署
环境显式设置 ``FEEDLING_ENABLE_PLAINTEXT_R2_REWRITE=1``。这三个条件用于把「代码已
具备能力」和「iOS 强更窗口已完成、允许改生产数据」分开。

## 用法

    # 从 backend 目录，加载目标环境的 DATABASE_URL + R2_* + enclave 配置
    python backfill_chat_bodies_to_plaintext.py                    # 全量盘点
    python backfill_chat_bodies_to_plaintext.py --user usr_x       # 单用户
    python backfill_chat_bodies_to_plaintext.py --table all --limit 50
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import db
import object_storage

_DEFAULT_RATE = 20.0  # rows/sec；enclave 有被迁移工具打爆的前科
_APPLY_BLOCKED = (
    "--apply requires --allow-plaintext-r2-rewrite and "
    "FEEDLING_ENABLE_PLAINTEXT_R2_REWRITE=1 after the client rollout gate"
)
_APPLY_ENV = "FEEDLING_ENABLE_PLAINTEXT_R2_REWRITE"
_PAGE_SIZE = 200


def _tier_allows_plaintext(user_id: str) -> bool:
    """只有显式选明文的用户才处理。查不到用户按加密档处理（fail-safe）。"""
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id=%s "
            "AND doc->>'content_encryption'='off'",
            (user_id,),
        ).fetchone()
    return row is not None


def _rows_to_process(user_filter: str | None, limit: int) -> list[tuple]:
    """Legacy-compatible live inventory helper used by focused unit tests."""
    where = ["doc ? 'body_key'", "doc ? 'K_enclave'"]
    params: list = []
    if user_filter:
        where.append("user_id = %s")
        params.append(user_filter)
    sql = (f"SELECT user_id, msg_id, doc FROM chat_messages "
           f"WHERE {' AND '.join(where)} ORDER BY user_id, msg_id")
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with db.get_pool().connection() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def _rows_page(
    table: str,
    user_filter: str | None,
    after_id: int,
    page_size: int,
) -> list[tuple]:
    """Keyset page of pointer rows.

    live ``seq`` and archive ``source_seq`` originate from the same global
    identity sequence, so each is a stable numeric keyset within its table.
    """
    if table == "live":
        table_name, id_col = "chat_messages", "seq"
    elif table == "archive":
        table_name, id_col = "chat_message_archive", "source_seq"
    else:
        raise ValueError("table must be live or archive")
    where = [f"{id_col} > %s", "doc ? 'body_key'"]
    params: list = [int(after_id)]
    if user_filter:
        where.append("user_id = %s")
        params.append(user_filter)
    params.append(int(page_size))
    sql = (
        f"SELECT {id_col},user_id,msg_id,storage_generation,doc "
        f"FROM {table_name} WHERE {' AND '.join(where)} "
        f"ORDER BY {id_col} LIMIT %s"
    )
    with db.get_pool().connection() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def _iter_rows(
    table_filter: str,
    user_filter: str | None,
    *,
    page_size: int = _PAGE_SIZE,
):
    tables = ("live", "archive") if table_filter == "all" else (table_filter,)
    for table in tables:
        after_id = 0
        while True:
            rows = _rows_page(table, user_filter, after_id, page_size)
            if not rows:
                break
            for row_id, user_id, msg_id, generation, doc in rows:
                after_id = int(row_id)
                yield (
                    table,
                    int(row_id),
                    str(user_id),
                    str(msg_id),
                    int(generation),
                    doc,
                )


def _inspect_one(user_id: str, msg_id: str, doc: dict) -> str:
    """只读检查一条候选；绝不解密、绝不写对象。"""
    key = str(doc.get("body_key") or "")
    if not key:
        return "skipped_no_body_key"

    if not object_storage.chat_key_owned_by(key, user_id):
        return "failed_foreign_body_key"
    body = object_storage.get_chat_body(key, user_id)
    if body is None:
        # 孤儿指针：R2 对象已不在。不是本工具能修的，交给对账。
        return "skipped_r2_object_missing"
    return "would_rewrite_after_protocol"


def _decrypt_pointer(
    user_id: str,
    msg_id: str,
    doc: dict,
    decrypt,
) -> bytes:
    """Fetch sealed object bytes and decrypt without logging body/key material."""
    key = str(doc.get("body_key") or "")
    if not object_storage.chat_key_owned_by(key, user_id):
        raise ValueError("foreign_body_key")
    body_ct = object_storage.get_chat_body(key, user_id)
    if body_ct is None:
        raise RuntimeError("r2_object_missing")
    envelope = dict(doc)
    envelope.pop("body_key", None)
    envelope.pop("body_ct_len", None)
    envelope["body_ct"] = body_ct
    return decrypt(
        envelope,
        purpose=f"chat_r2_plaintext_migration:{msg_id}",
    )


def _mark_requeue(table: str, user_id: str, item_id: int | str) -> None:
    from tee_shadow import mirror

    mirror.mark_pending(
        user_id,
        "chat_messages" if table == "live" else "chat_message_archive",
        str(item_id),
        "requeue_r2_plaintext_pointer",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真正写入；缺省是 dry-run")
    ap.add_argument(
        "--allow-plaintext-r2-rewrite",
        action="store_true",
        help="确认客户端 rollout gate 已完成",
    )
    ap.add_argument(
        "--table",
        choices=("live", "archive", "all"),
        default="all",
        help="扫描 live、archive 或两者（默认 all）",
    )
    ap.add_argument("--user", default=None, help="只处理这一个 user_id")
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="apply 最多成功迁移多少行；dry-run 最多盘点多少行（0=全部）",
    )
    ap.add_argument("--rate", type=float, default=_DEFAULT_RATE,
                    help=f"每秒处理行数上限（默认 {_DEFAULT_RATE}）")
    args = ap.parse_args()
    if args.apply and (
        not args.allow_plaintext_r2_rewrite
        or os.environ.get(_APPLY_ENV, "").strip() != "1"
    ):
        ap.error(_APPLY_BLOCKED)
    if args.allow_plaintext_r2_rewrite and not args.apply:
        ap.error("--allow-plaintext-r2-rewrite is only valid with --apply")

    mode = "APPLY" if args.apply else "INVENTORY ONLY"
    print(f"[backfill] mode={mode} table={args.table}")

    tallies: dict[str, int] = {}
    failures: list[str] = []
    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    decrypt_by_user: dict[str, object] = {}
    inspected = migrated = 0

    for table, row_id, user_id, msg_id, generation, doc in _iter_rows(
        args.table,
        args.user,
    ):
        if args.limit and (
            (args.apply and migrated >= args.limit)
            or (not args.apply and inspected >= args.limit)
        ):
            break
        inspected += 1
        if not _tier_allows_plaintext(user_id):
            status = "skipped_encrypted_tier"
        else:
            try:
                marker = db._chat_body_object_format(doc)
                if marker == "plaintext_v1":
                    if args.apply:
                        # Crash recovery for the RDS-commit -> TEE-marker gap.
                        _mark_requeue(table, user_id, row_id if table == "archive" else msg_id)
                    status = "already_plaintext"
                elif not doc.get("K_enclave"):
                    status = "skipped_not_enclave_decryptable"
                elif not args.apply:
                    status = _inspect_one(user_id, msg_id, doc)
                else:
                    decrypt = decrypt_by_user.get(user_id)
                    if decrypt is None:
                        from tee_replicator import worker

                        decrypt = worker._make_decrypt(user_id)
                        decrypt_by_user[user_id] = decrypt
                    plaintext = _decrypt_pointer(
                        user_id,
                        msg_id,
                        doc,
                        decrypt,
                    )
                    applied = db.migrate_chat_r2_pointer_to_plaintext(
                        user_id,
                        table=table,
                        item_id=row_id if table == "archive" else msg_id,
                        old_body_key=str(doc.get("body_key") or ""),
                        storage_generation=generation,
                        plaintext=plaintext,
                        content_type=str(doc.get("content_type") or "file"),
                    )
                    status = "migrated" if applied else "cas_lost"
                    if applied:
                        migrated += 1
            except Exception as e:  # noqa: BLE001  单行失败不阻塞整趟
                status = f"failed_unexpected:{type(e).__name__}"
        bucket = status.split(":", 1)[0]
        tallies[bucket] = tallies.get(bucket, 0) + 1
        if bucket.startswith("failed"):
            identity = msg_id if table == "live" else str(row_id)
            failures.append(f"{table}/{user_id}/{identity}: {status}")
        if interval:
            time.sleep(interval)

    print("[backfill] summary:")
    for bucket in sorted(tallies):
        print(f"[backfill]   {bucket:<28} {tallies[bucket]}")
    if failures:
        print(f"[backfill] failures ({len(failures)}), first 20:")
        for line in failures[:20]:
            print(f"[backfill]   {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
