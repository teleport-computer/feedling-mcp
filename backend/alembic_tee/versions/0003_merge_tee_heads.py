"""TEE 影子库：合并 test 与 pre 两条分叉迁移链（无操作）。

`pre` 分支加了 0002_drop_retired_supervisor（下线 resident/supervisor 影子表），
`test` 分支独立加了 0002_notify_relay（Notify Relay 两表），两者都从
0001_tee_baseline 分叉、各自占用 0002 编号，merge 后成两个 head。本 revision
只把两条链重新汇合成单一 head，让 alembic_tee upgrade_head 可线性化；两条链
改的是互不相干的表，任意应用顺序都安全。

Revision ID: 0003_merge_tee_heads
Revises: 0002_drop_retired_supervisor, 0002_notify_relay
"""

revision = "0003_merge_tee_heads"
down_revision = ("0002_drop_retired_supervisor", "0002_notify_relay")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 仅汇合两条独立链，无 schema 变更。
    pass


def downgrade() -> None:
    pass
