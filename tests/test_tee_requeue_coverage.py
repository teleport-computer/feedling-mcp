"""守卫：CIPHERTEXT lane 里的可变表必须有 requeue 双写。

CIPHERTEXT lane 是 append-only 游标模型（tee_replicator.worker），游标永不回头。
被原地 UPDATE 的行只有靠 mirror.mark_pending(..., "requeue") 打标记，下一趟
_consume_requeue 才会按 PK 重新拉取。少了这一步，TEE 侧永久停在首次复制的状态——
而且不会有任何测试变红、不会有任何日志报错，只是数据悄悄陈旧。

2026-07-28 实测发现 4 张表处于这个状态（model_api_credentials 最严重：365 行 BYOK
凭证，轮换后 TEE 里还是旧 key）。本守卫防止它再次发生。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from tee_shadow import table_registry as reg


def _mark_pending_context() -> str:
    """所有 mark_pending 调用点及其后 4 行。

    抓后续行是因为这类调用常常跨行写：
        mirror.mark_pending(
            str(user_id), "memory_moments", memory_id, "requeue"
        )
    只匹配调用所在那一行会漏掉表名。
    """
    out = subprocess.run(
        ["grep", "-rn", "-A", "4", "mark_pending", str(BACKEND), "--include=*.py"],
        capture_output=True, text=True)
    return out.stdout


def test_every_mutable_ciphertext_table_has_requeue_writes():
    """登记为可变的 CIPHERTEXT 表，必须能在源码里找到给它打 requeue 的调用。"""
    ctx = _mark_pending_context()
    missing = [t for t in reg.MUTABLE_CIPHERTEXT_TABLES if f'"{t}"' not in ctx]
    assert not missing, (
        "这些表登记为可变 CIPHERTEXT，但源码里找不到给它们打 requeue 的 "
        f"mark_pending 调用：{missing}\n"
        "后果：它们被 UPDATE 之后，TEE 侧会永久停在首次复制时的状态——"
        "没有报错、没有红灯，只是数据悄悄陈旧。"
    )


def test_mutable_list_only_contains_ciphertext_lane_tables():
    """可变清单里不该混进别的 lane 的表——SNAPSHOT lane 用整表替换，天然处理 UPDATE，
    登记进来只会造成误导。"""
    ciphertext = set(reg.tables_in_lane(reg.CIPHERTEXT))
    stray = sorted(set(reg.MUTABLE_CIPHERTEXT_TABLES) - ciphertext)
    assert not stray, f"这些表不在 CIPHERTEXT lane，不该出现在可变清单里：{stray}"


def test_every_mutable_table_has_a_requeue_consumer():
    """写侧打了标记，读侧也必须能消费——否则 pending 行永远堆积、没人处理。

    requeue 是两半：mirror.mark_pending() 打标记，worker._consume_requeue() 按
    _Table.requeue_fetch_sql 把该 PK 的当前行重新拉回来。而 _consume_requeue 在
    requeue_fetch_sql 为 None 时**直接 return 静默跳过**（worker.py:1040-1041）——
    只补写侧比不补更糟：pending 行堆在 tee_pending_device_migration 里没人消费，
    还会把 requeue_backlog 指标顶上去，让人误以为复制在积压。
    """
    from tee_replicator import worker

    missing = sorted(
        t for t in reg.MUTABLE_CIPHERTEXT_TABLES
        if worker._TABLES[t].requeue_fetch_sql is None
    )
    assert not missing, (
        f"这些表登记为可变、写侧会打 requeue 标记，但 worker._TABLES 里没配 "
        f"requeue_fetch_sql，标记永远不会被消费：{missing}"
    )
