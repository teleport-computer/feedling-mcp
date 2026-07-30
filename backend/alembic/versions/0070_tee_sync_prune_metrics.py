"""tee_sync_runs 增加 CIPHERTEXT prune lane 的三个扁平列

Revision ID: 0070_tee_sync_prune
Revises: 0069_batch_cap

同 0063 给 snapshot lane 加列的动机：只落进 ``report`` JSONB 的指标没人会看。
07-29 刚吃过一次教训——``missing_in_tee`` 只活在 JSONB 里，于是
``model_api_routes`` 有 4 列数据一直没同步，整整一批部署无人察觉。

- prune_stale   本轮算出的残留行总数（TEE 有、RDS 没有）
- prune_deleted 实际删掉的行数
- prune_refused 因超过安全阈值被整表放弃的表数——**这一列非 0 就该有人看**，
  它意味着某张表的残留量大到不敢自动删（多半是 RDS 侧读数出了问题）

三列都 NOT NULL DEFAULT 0：历史行补 0 语义正确（那时还没有 prune lane，
确实一行都没删）。

``tee_sync_runs`` 归 SKIP lane（TEE 同步自身的控制面必须住在 RDS，复制到被它
监控的库里没有意义），所以本次不需要对应的 alembic_tee revision。
"""

from alembic import op


# ⚠️ 本条最初写作 0068/down=0067，与并行合入 test 的 0068_provider_latency 撞成
# 两个 head（同一个 down_revision）。alembic 在 `upgrade head` 会直接
# CommandError 让部署失败——0062 那次已经吃过一模一样的亏。rebase 之后必须
# 重新对齐到当时真正的链尾，不能只看自己分支上的编号。
revision = "0070_tee_sync_prune"
down_revision = "0069_batch_cap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tee_sync_runs
            ADD COLUMN IF NOT EXISTS prune_stale   INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS prune_deleted INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS prune_refused INTEGER NOT NULL DEFAULT 0;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tee_sync_runs
            DROP COLUMN IF EXISTS prune_refused,
            DROP COLUMN IF EXISTS prune_deleted,
            DROP COLUMN IF EXISTS prune_stale;
        """
    )
