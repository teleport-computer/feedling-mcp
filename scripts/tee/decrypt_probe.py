#!/usr/bin/env python3
"""解密探针：在真正回填之前，先量出每张密文表有多少行是 enclave 解得开的。

为什么必须先探：v2_trajectory_events 的 payload_envelope 里 enclave_pk_fpr 全为空
（2026-07-27 prod 实测 567/567 行），也就是说没有任何字段记录它是用哪把 enclave
钥封的——解不解得开只能实际试。历史上正是这类无 fpr 自检的行造成过 790 行毒行
队头阻塞（memory tee-replicate-poison-row-headofline-quarantine）。

只读：不写 TEE、不改 RDS，只调 enclave 解密接口统计成败。

用法：
    python scripts/tee/decrypt_probe.py --dsn "$TEST_DATABASE_URL" --limit 200
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
import psycopg


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import db  # noqa: E402  — chat_message_archive 的 R2 offload 水合要用
from tee_replicator import transforms  # noqa: E402
from tee_replicator import worker  # noqa: E402
from tee_shadow import table_registry as reg  # noqa: E402

# 表 -> (信封列名, 主键列元组)。探针要知道从哪一列取信封。
_ENVELOPE_COLUMN = {
    "chat_message_archive": "doc",
    "v2_trajectory_events": "payload_envelope",
    "model_api_credentials": "api_key_envelope",
    "v2_conversation_summary": "summary_envelope",
    "v2_conversation_summary_segments": "summary_envelope",
    "v2_trajectory_reviews": "review_envelope",
    "v2_workspace_entries": "content_envelope",
}


def probe_table(dsn: str, table: str, column: str, limit: int) -> Counter:
    tally: Counter = Counter()
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            f"SELECT user_id, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL LIMIT %s", (limit,)
        ).fetchall()
    for uid, env in rows:
        # decrypt 回调是 per-user 的（enclave token 按用户签发），worker._get_decrypt
        # 带进程内缓存，探针复用它 → 与真实 replicate 走完全同一条通道，探针结果
        # 才有代表性。
        decrypt = worker._get_decrypt(uid)
        try:
            if table == "chat_message_archive":
                # R2-offloaded 行（content_type="file"）的 doc 只带 body_key 指针、
                # 没有 body_ct——直接送去解密必然失败。必须先水合，与
                # worker._chat_unpack 的处理一致（prod 实测 897 行里有 1 行是这种）。
                # 少了这一步，探针 gate 会因为一行 false negative 而永远不到 100%。
                if db._is_chat_file_pointer(env):
                    env = db.hydrate_chat_file_body(uid, env)
                transforms.plaintext_chat_doc(env, decrypt)
            else:
                transforms.plaintext_envelope_column(
                    env, decrypt, purpose=f"tee_probe:{table}")
            tally["ok"] += 1
        except transforms.PendingDeviceMigration:
            tally["pending_device"] += 1
        except Exception as exc:  # noqa: BLE001
            # transforms.plaintext_chat_doc / plaintext_envelope_column 从不自己
            # raise PermanentDecryptFailure——那个重分类只发生在 worker._transform_
            # with_retry（真实 replicate 主循环）里：裸 decrypt() 失败先抛
            # RuntimeError("enclave_http_403:...decrypt_failed...")，
            # _transform_with_retry 用 _is_permanent_decrypt_failure 判定后才转成
            # PermanentDecryptFailure。探针不经过 _TABLES/_transform_with_retry
            # （这 7 张表还没接线），所以必须在这里直接复用同一个判定函数，否则
            # enclave 确定性拒解会和网络抖动一起落进 other:*——这两类处置相反
            # （前者是毒行、该表要回退成密文原样搬；后者重试就好），混在一起
            # gate 就问不出正确结论。
            if worker._is_permanent_decrypt_failure(exc):
                tally["permanent_fail"] += 1
            else:
                tally[f"other:{type(exc).__name__}"] += 1
    return tally


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()

    worst = 0
    for table in reg.tables_in_lane(reg.CIPHERTEXT):
        column = _ENVELOPE_COLUMN.get(table)
        if column is None:
            continue  # chat_messages 等既有表不在本次探针范围
        tally = probe_table(args.dsn, table, column, args.limit)
        total = sum(tally.values())
        ok = tally.get("ok", 0)
        pct = 100.0 * ok / total if total else 100.0
        status = "PASS" if ok == total else "FAIL"
        print(f"{status} {table}: {ok}/{total} ({pct:.1f}%) {dict(tally)}")
        if ok != total:
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
