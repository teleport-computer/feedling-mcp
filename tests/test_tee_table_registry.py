"""TEE 表同步注册表的完备性守卫。

为什么需要它：TEE 同步是三层手工白名单登记制（mirror 写点 / worker._TABLES /
reconciler.TABLES），三处互不校验、谁都不是全集。结果是 Runtime V2 的 19 张新表
在 2026-07-27 之前从未被任何人登记，而没有任何测试会因此变红。

本守卫把"漏登记"变成红灯：RDS 迁移链建出的每一张表，都必须在 table_registry
里有且只有一条 lane 登记。红灯的修法不是加白名单，而是回答"这张表进不进 TEE、
走哪条 lane、为什么"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from tee_shadow import table_registry as reg


def _tables_in(dsn_env: str) -> set[str]:
    with psycopg.connect(os.environ[dsn_env]) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        ).fetchall()
    return {r[0] for r in rows}


# conftest 建测试库时对 RDS 侧跑 db.init_schema()、对 TEE 侧跑
# alembic_tee.upgrade_head()，所以这两个库的表集合就是两条迁移链的真实产物。
# 不静态解析迁移文件——迁移是增量 op 序列，静态推导最终表集合既脆弱又易错。
#
# 刻意不豁免 alembic_version：它照样要在注册表里登记（为 SKIP，理由是 TEE 有
# 独立的 alembic_tee_version）。豁免它会让 test_no_phantom_entries 反过来把那条
# 合法的 SKIP 登记判成幽灵条目。凡是 RDS 里存在的表，一律走同一条登记规则。


def test_every_rds_table_is_registered():
    """推导集合 − 注册表 ≠ ∅ → 有 RDS 表没登记 lane。守卫的主职责。"""
    actual = _tables_in("DATABASE_URL")
    missing = sorted(actual - set(reg.REGISTRY))
    assert not missing, (
        f"这些 RDS 表未在 table_registry 登记（共 {len(missing)} 张）：\n  "
        + "\n  ".join(missing)
        + "\n\n修法不是加白名单，是回答：这张表进不进 TEE、走哪条 lane、为什么。"
    )


def test_no_phantom_entries():
    """注册表 − 推导集合 ≠ ∅ → 注册表里有迁移链外的表。

    允许（如 bak_20260710_* 这类人工建的备份表），但必须显式 manual=True——
    否则一个打错的表名会被当成"人工表"蒙混过关。
    """
    actual = _tables_in("DATABASE_URL")
    phantom = sorted(t for t in set(reg.REGISTRY) - actual if not reg.REGISTRY[t].manual)
    assert not phantom, (
        f"注册表里有迁移链中不存在的表且未标 manual=True：{phantom}\n"
        "如果确实是人工建的表（备份表等），加 manual=True 并写明理由；"
        "否则就是表名打错了。"
    )


def test_lanes_are_valid_and_reasons_nonempty():
    bad_lane = {t: e.lane for t, e in reg.REGISTRY.items() if e.lane not in reg.LANES}
    assert not bad_lane, f"未知 lane：{bad_lane}"
    no_reason = sorted(t for t, e in reg.REGISTRY.items() if not e.reason.strip())
    assert not no_reason, f"这些条目没写理由：{no_reason}"


def test_perception_signal_state_uses_snapshot_lane():
    """Mutable fingerprint state must converge; a SKIP/MIRROR lane can drift."""
    entry = reg.REGISTRY.get("perception_signal_state_v2")
    assert entry is not None
    assert entry.lane == reg.SNAPSHOT


def test_skip_entries_must_justify():
    """SKIP 是"这张表永远不进 TEE"的承诺，理由必须具体（不是"暂不需要"）。"""
    vague = {"暂不需要", "不需要", "TODO", "待定", "以后再说"}
    lazy = sorted(t for t in reg.tables_in_lane(reg.SKIP)
                  if reg.REGISTRY[t].reason.strip() in vague)
    assert not lazy, f"这些 SKIP 条目的理由太含糊，需要写清为什么永远不进 TEE：{lazy}"


# RDS 表名 → TEE 侧对应表名，仅用于两侧不同名的少数情况。
# frame_envelopes：RDS 存 inline 密文信封，TEE 侧是形状完全不同的 frames
# （R2 存储层指针，见 alembic_tee 0001 baseline 的说明）。worker 用
# row_writer 处理这条特殊路径，不是同名表的直译。
TEE_TABLE_ALIAS = {"frame_envelopes": "frames"}


def test_tee_schema_covers_every_synced_table():
    """DDL 也要被守卫覆盖：非 SKIP lane 的表必须在 TEE 库里真实存在。

    这一条专治 0002 那类事故——revision 写了、合并了，但从未在实库执行。
    conftest 的 TEE 测试库是 alembic_tee.upgrade_head() 的产物，所以这里等价于
    断言"迁移链真的建了这些表"。
    """
    tee_actual = _tables_in("TEE_DATABASE_URL")
    want = {TEE_TABLE_ALIAS.get(t, t) for t in reg.synced_tables()}
    missing = sorted(want - tee_actual)
    assert not missing, (
        f"这些表登记了非 SKIP lane，但 alembic_tee 没建（共 {len(missing)} 张）：\n  "
        + "\n  ".join(missing)
    )


def test_logical_lane_is_empty_for_now():
    """LOGICAL lane 是 PG 逻辑复制的预留接口。上它需要改 RDS 参数组并重启实例
    （rds.logical_replication=off / wal_level=replica，2026-07-27 实测），属独立
    运维窗口。有表被划进这条 lane 时，说明有人以为它已经生效了——拦下来。"""
    assert reg.tables_in_lane(reg.LOGICAL) == (), (
        "LOGICAL lane 尚未实现（RDS 未开 logical replication）。"
        "在通道打通前不要往这条 lane 放表。"
    )
