"""TEE 表同步注册表的完备性守卫。

为什么需要它：TEE 同步是三层手工白名单登记制（mirror 写点 / worker._TABLES /
reconciler.TABLES），三处互不校验、谁都不是全集。结果是 Runtime V2 的 19 张新表
在 2026-07-27 之前从未被任何人登记，而没有任何测试会因此变红。

本守卫把"漏登记"变成红灯：RDS 迁移链建出的每一张独立表，都必须在
table_registry 里有且只有一条 lane 登记。声明式分区的物理子表从 pg_catalog
解析并继承已登记根表的 lane，不按动态日期表名加白名单。红灯的修法是回答
"这张表进不进 TEE、走哪条 lane、为什么"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from tee_shadow import table_registry as reg


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    ).fetchall()
    return {r[0] for r in rows}


def _tables_in(dsn_env: str) -> set[str]:
    with psycopg.connect(os.environ[dsn_env]) as conn:
        return _tables(conn)


def _partition_parents(conn: psycopg.Connection) -> dict[str, str]:
    """Return declarative partition child -> immediate parent from the catalog."""
    rows = conn.execute(
        "SELECT child.relname, parent.relname "
        "FROM pg_catalog.pg_inherits inheritance "
        "JOIN pg_catalog.pg_class child ON child.oid=inheritance.inhrelid "
        "JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid=child.relnamespace "
        "JOIN pg_catalog.pg_class parent ON parent.oid=inheritance.inhparent "
        "JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace "
        "WHERE child_ns.nspname='public' AND parent_ns.nspname='public' "
        "AND child.relispartition AND child.relkind IN ('r', 'p') "
        "AND parent.relkind IN ('r', 'p')"
    ).fetchall()
    return {child: parent for child, parent in rows}


def _partition_parents_in(dsn_env: str) -> dict[str, str]:
    with psycopg.connect(os.environ[dsn_env]) as conn:
        return _partition_parents(conn)


def _partition_root(child: str, parents: dict[str, str]) -> str:
    """Resolve nested declarative partitions to the independently registered root."""
    seen: set[str] = set()
    current = child
    while current in parents:
        assert current not in seen, f"partition inheritance cycle at {current}"
        seen.add(current)
        current = parents[current]
    return current


# conftest 建测试库时对 RDS 侧跑 db.init_schema()、对 TEE 侧跑
# alembic_tee.upgrade_head()，所以这两个库的表集合就是两条迁移链的真实产物。
# 不静态解析迁移文件——迁移是增量 op 序列，静态推导最终表集合既脆弱又易错。
#
# 刻意不豁免 alembic_version：它照样要在注册表里登记（为 SKIP，理由是 TEE 有
# 独立的 alembic_tee_version）。豁免它会让 test_no_phantom_entries 反过来把那条
# 合法的 SKIP 登记判成幽灵条目。只有 pg_catalog 证明为声明式分区子表的物理表
# 才继承根表登记；普通表和传统 INHERITS 子表仍必须逐张回答 lane。


def test_every_rds_table_is_registered():
    """推导集合 − 注册表 ≠ ∅ → 有 RDS 表没登记 lane。守卫的主职责。"""
    actual = _tables_in("DATABASE_URL")
    partition_children = set(_partition_parents_in("DATABASE_URL"))
    missing = sorted(actual - partition_children - set(reg.REGISTRY))
    assert not missing, (
        f"这些 RDS 表未在 table_registry 登记（共 {len(missing)} 张）：\n  "
        + "\n  ".join(missing)
        + "\n\n修法不是加白名单，是回答：这张表进不进 TEE、走哪条 lane、为什么。"
    )


def test_partition_children_inherit_a_registered_root_lane():
    """Physical partitions may omit independent entries only via catalog ancestry."""
    actual = _tables_in("DATABASE_URL")
    parents = _partition_parents_in("DATABASE_URL")
    assert set(parents) <= actual

    missing_roots = {
        child: _partition_root(child, parents)
        for child in parents
        if _partition_root(child, parents) not in reg.REGISTRY
    }
    assert not missing_roots, (
        "这些声明式分区找不到已登记 lane 的根表："
        f"{missing_roots}"
    )


def test_traditional_inherits_child_does_not_inherit_parent_lane():
    """Only declarative partitions may inherit a registry entry."""
    probe = "tee_registry_traditional_inherits_probe"
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute(
                "CREATE TABLE tee_registry_traditional_inherits_probe "
                "() INHERITS (server_config)"
            )
            actual = _tables(conn)
            partition_children = set(_partition_parents(conn))

            assert probe in actual
            assert probe not in partition_children
            missing = actual - partition_children - set(reg.REGISTRY)
            assert probe in missing


def test_trace_events_partition_family_is_selected_primary_local():
    """Trace partitions inherit one local-authority contract, not replication lanes."""
    entry = reg.REGISTRY["trace_events"]
    assert entry.lane == reg.SKIP
    assert entry.tee_required is True
    assert "selected primary" in entry.reason
    assert "不搬旧主库历史" in entry.reason

    parents = _partition_parents_in("DATABASE_URL")
    trace_children = {
        child for child in parents
        if _partition_root(child, parents) == "trace_events"
    }
    assert "trace_events_default" in trace_children
    assert any(child.startswith("trace_events_p") for child in trace_children)


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


def test_growth_lane_reasons_match_actual_failure_and_cleanup_semantics():
    """Registry guidance must not invent cleanup or an all-table hard failure."""
    for table in ("v2_effect_outbox", "v2_terminal_failure_outbox"):
        reason = reg.REGISTRY[table].reason
        assert "投递后删行" not in reason
        assert "投递后更新状态而非删行" in reason

    rollup_reason = reg.REGISTRY["lane_daily_rollup"].reason
    assert "超限整轮失败" not in rollup_reason
    assert "仅该表复制失败并停止更新、其余表继续" in rollup_reason


def test_perception_signal_state_uses_snapshot_lane():
    """Mutable fingerprint state must converge; a SKIP/MIRROR lane can drift."""
    entry = reg.REGISTRY.get("perception_signal_state_v2")
    assert entry is not None
    assert entry.lane == reg.SNAPSHOT


def test_contract_rejection_stats_uses_mirror_lane_and_reconciler_key():
    from tee_shadow import reconciler

    entry = reg.REGISTRY.get("contract_rejection_stats")
    expected_key = (
        "contract_domain", "boundary", "fallback", "release_sha", "writer_id"
    )
    assert entry is not None
    assert entry.lane == reg.MIRROR
    assert entry.key_columns == expected_key
    assert reconciler.TABLES["contract_rejection_stats"][0] == expected_key


def test_tee_required_tables_include_synced_and_primary_local_tables():
    """A local-only SKIP lane must not exempt a TEE-primary runtime table."""
    required = set(reg.tee_required_tables())
    assert set(reg.synced_tables()) <= required
    assert {
        "genesis_import_chunks",
        "v2_wake_shadow_decisions",
        "voice_turn_results",
        "voice_turn_streams",
        "voice_call_sessions",
        "trace_events",
    } <= required
    assert {
        "alembic_version",
        "tee_sync_runs",
        "tee_reconcile_state",
        "tee_reconcile_cursors",
        "bak_20260710_usr450_blobs",
    }.isdisjoint(required)


def test_synced_lane_cannot_opt_out_of_tee_schema():
    """A mistaken override must not recreate the synced-without-DDL failure."""
    entry = reg.Entry(
        reg.SNAPSHOT,
        "test contract",
        required_in_tee=False,
    )
    assert entry.tee_required is True


def test_voice_call_sessions_use_snapshot_lane():
    """The source lifecycle state must converge before primary promotion."""
    entry = reg.REGISTRY["voice_call_sessions"]
    assert entry.lane == reg.SNAPSHOT
    assert entry.key_columns == ("user_id", "call_id")
    assert entry.capture_key_columns == ("user_id", "call_id")


def test_skip_entries_must_justify():
    """SKIP 是“不做跨库复制”的承诺，理由必须具体（不是“暂不需要”）。"""
    vague = {"暂不需要", "不需要", "TODO", "待定", "以后再说"}
    lazy = sorted(t for t in reg.tables_in_lane(reg.SKIP)
                  if reg.REGISTRY[t].reason.strip() in vague)
    assert not lazy, f"这些 SKIP 条目的理由太含糊，需要写清为什么永远不进 TEE：{lazy}"


def test_chat_change_control_is_primary_local_but_required_after_promotion():
    for table in ("chat_change_state", "chat_change_events"):
        entry = reg.REGISTRY[table]
        assert entry.lane == reg.SKIP
        assert entry.tee_required is True


# RDS 表名 → TEE 侧对应表名，仅用于两侧不同名的少数情况。
# frame_envelopes：RDS 存 inline 密文信封，TEE 侧是形状完全不同的 frames
# （R2 存储层指针，见 alembic_tee 0001 baseline 的说明）。worker 用
# row_writer 处理这条特殊路径，不是同名表的直译。
TEE_TABLE_ALIAS = {"frame_envelopes": "frames"}


def test_tee_schema_covers_every_required_table():
    """DDL 守卫同时覆盖同步表和 TEE-primary 本地表。

    这一条专治 0002 那类事故——revision 写了、合并了，但从未在实库执行。
    conftest 的 TEE 测试库是 alembic_tee.upgrade_head() 的产物，所以这里等价于
    断言"迁移链真的建了这些表"。
    """
    tee_actual = _tables_in("TEE_DATABASE_URL")
    want = {TEE_TABLE_ALIAS.get(t, t) for t in reg.tee_required_tables()}
    missing = sorted(want - tee_actual)
    assert not missing, (
        f"这些 TEE-primary 必需表未由 alembic_tee 创建（共 {len(missing)} 张）：\n  "
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
