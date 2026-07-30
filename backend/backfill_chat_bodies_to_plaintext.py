"""One-off: rewrite R2-offloaded chat bodies from ciphertext to plaintext.

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

## 安全与可重入

- **默认 dry-run。** 真跑要显式 ``--apply``。
- **断点续跑**：每行独立事务；已是明文的对象直接跳过（幂等）。判据是解密不再需要
  ——即 R2 对象内容不是 base64 密文而是明文正文。
- **限速**：每行之间 sleep，默认 20 rows/s。enclave 有 502 前科（迁移工具打爆
  enclave），不限速会重演。
- **失败不阻塞**：单行失败记进清单继续跑，最后打印汇总。毒行不该冻住整趟。

## 用法

    # 从 backend 目录，加载目标环境的 DATABASE_URL + R2_* + enclave 配置
    python backfill_chat_bodies_to_plaintext.py                    # dry-run 全量盘点
    python backfill_chat_bodies_to_plaintext.py --user usr_x       # 单用户
    python backfill_chat_bodies_to_plaintext.py --apply --limit 50 # 小批真跑
    python backfill_chat_bodies_to_plaintext.py --apply            # 全量
"""

from __future__ import annotations

import argparse
import base64
import binascii
import sys
import time

import db
import object_storage
from accounts import registry
from core import enclave as core_enclave

_DEFAULT_RATE = 20.0  # rows/sec；enclave 有被迁移工具打爆的前科


def _is_probably_ciphertext(body: str) -> bool:
    """R2 对象里放的还是密文吗？

    明文重写后对象内容是正文本身；重写前是信封 ``body_ct`` 的 base64。用「能否
    严格 base64 解码」判别：正文极少恰好是合法 base64，而 ``body_ct`` 必然是。
    判错的方向是安全的——误判成密文只会多走一次解密，解不开则记失败不写坏数据。
    """
    if not body:
        return False
    try:
        base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


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


def _rewrite_one(user_id: str, msg_id: str, doc: dict, *, apply: bool) -> str:
    """返回状态字符串：ok / skipped_* / failed_*。"""
    key = str(doc.get("body_key") or "")
    if not key:
        return "skipped_no_body_key"

    body = object_storage.get_chat_body(key, user_id)
    if body is None:
        # 孤儿指针：R2 对象已不在。不是本工具能修的，交给对账。
        return "skipped_r2_object_missing"
    if not _is_probably_ciphertext(body):
        return "skipped_already_plaintext"

    if not apply:
        return "would_rewrite"

    envelope = {**{k: v for k, v in doc.items() if k != "body_key"},
                "body_ct": body}
    try:
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            envelope, None, purpose=f"backfill_chat_body_plaintext:{msg_id}",
            runtime_token=_runtime_token(user_id))
    except Exception as e:  # noqa: BLE001
        return f"failed_decrypt:{type(e).__name__}:{str(e)[:120]}"

    try:
        text = plaintext.decode("utf-8")
    except UnicodeDecodeError:
        # 二进制重体（图片）无法以明文 text 形式承载——这类留给单独的方案，
        # 不在本工具范围内，且绝不能写成乱码。
        return "skipped_binary_body"

    object_storage.put_chat_body(user_id, msg_id, text,
                                 str(doc.get("content_type") or "file"))
    return "ok"


def _runtime_token(user_id: str) -> str:
    from tee_replicator import worker as tee_worker

    return tee_worker._mint_runtime_token(user_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真正写入；缺省是 dry-run")
    ap.add_argument("--user", default=None, help="只处理这一个 user_id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=全部）")
    ap.add_argument("--rate", type=float, default=_DEFAULT_RATE,
                    help=f"每秒处理行数上限（默认 {_DEFAULT_RATE}）")
    args = ap.parse_args()

    rows = _rows_to_process(args.user, args.limit)
    print(f"[backfill] candidate rows: {len(rows)}"
          f"{'' if args.apply else '  (DRY-RUN — 加 --apply 才写)'}")

    tallies: dict[str, int] = {}
    failures: list[str] = []
    interval = 1.0 / args.rate if args.rate > 0 else 0.0

    for user_id, msg_id, doc in rows:
        if not _tier_allows_plaintext(user_id):
            status = "skipped_encrypted_tier"
        else:
            try:
                status = _rewrite_one(user_id, msg_id, doc, apply=args.apply)
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
