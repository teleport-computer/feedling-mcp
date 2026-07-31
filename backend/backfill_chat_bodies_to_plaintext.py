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

## 当前状态：只读盘点，apply 被硬禁用

R2 helper 的公开契约是「对象里存 raw ciphertext，读出时重新 base64 成
``body_ct``」；数据库指针也只有密文形状 ``{body_key, body_ct_len, K_enclave...}``。
因此只覆盖 R2 对象会同时破坏三件事：

1. 普通上传 helper 会生成另一个 key，覆盖不到行里持久化的 versioned ``body_key``；
2. 原地覆盖在「对象已改、数据库未改」的崩溃窗口会不可逆地丢掉原密文；
3. 读侧仍会把对象装回 ``body_ct``，客户端继续按密文解，明文对象必然读坏。

在定义并实现「新 key 写明文 → CAS 切数据库指针/形状 → 旧 key 异步清理」之前，
``--apply`` 会直接拒绝执行。本文件暂时只做默认 dry-run 盘点：确认候选行所属档位、
指针归属和 R2 对象是否存在。它不会调用 enclave，也不会写 R2/数据库。

## 用法

    # 从 backend 目录，加载目标环境的 DATABASE_URL + R2_* + enclave 配置
    python backfill_chat_bodies_to_plaintext.py                    # 全量盘点
    python backfill_chat_bodies_to_plaintext.py --user usr_x       # 单用户
    python backfill_chat_bodies_to_plaintext.py --limit 50         # 小批盘点
"""

from __future__ import annotations

import argparse
import sys
import time

import db
import object_storage
from accounts import registry

_DEFAULT_RATE = 20.0  # rows/sec；enclave 有被迁移工具打爆的前科
_APPLY_BLOCKED = (
    "--apply is disabled: plaintext R2 pointers need an atomic "
    "new-key/CAS/old-key-cleanup protocol before writes are safe"
)


def _tier_allows_plaintext(user_id: str) -> bool:
    """只有显式选明文的用户才处理。查不到用户按加密档处理（fail-safe）。"""
    return registry._get_user_content_encryption(user_id) == "off"


def _rows_to_process(user_filter: str | None, limit: int) -> list[tuple]:
    """带 body_key 指针、且信封还带 K_enclave（enclave 解得开）的 chat 行。"""
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真正写入；缺省是 dry-run")
    ap.add_argument("--user", default=None, help="只处理这一个 user_id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=全部）")
    ap.add_argument("--rate", type=float, default=_DEFAULT_RATE,
                    help=f"每秒处理行数上限（默认 {_DEFAULT_RATE}）")
    args = ap.parse_args()
    if args.apply:
        ap.error(_APPLY_BLOCKED)

    rows = _rows_to_process(args.user, args.limit)
    print(f"[backfill] candidate rows: {len(rows)}"
          "  (INVENTORY ONLY — apply is disabled)")

    tallies: dict[str, int] = {}
    failures: list[str] = []
    interval = 1.0 / args.rate if args.rate > 0 else 0.0

    for user_id, msg_id, doc in rows:
        if not _tier_allows_plaintext(user_id):
            status = "skipped_encrypted_tier"
        else:
            try:
                status = _inspect_one(user_id, msg_id, doc)
            except Exception as e:  # noqa: BLE001  单行失败不阻塞整趟
                status = f"failed_unexpected:{type(e).__name__}"
        bucket = status.split(":", 1)[0]
        tallies[bucket] = tallies.get(bucket, 0) + 1
        if bucket.startswith("failed"):
            failures.append(f"{user_id}/{msg_id}: {status}")
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
