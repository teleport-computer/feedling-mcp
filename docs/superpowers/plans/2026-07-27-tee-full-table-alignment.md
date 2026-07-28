# TEE 全量对齐 + 表同步机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 RDS 缺失的 45 张表按 lane 归类后全量对齐进 TEE 影子库，并建立一套"漏登记即 CI 红"的表同步机制与 alembic_tee 落地通道。

**Architecture:** 新增 `table_registry.py` 作为"哪张表走哪条同步 lane"的单一真源，现有三层白名单（mirror 写点 / `worker._TABLES` / `reconciler.TABLES`）退化为它的下游；新增 SNAPSHOT lane（整表 `TRUNCATE`+`COPY` 原子替换）承载 25 张明文表，7 张密文表并入既有 CIPHERTEXT lane；一个 CI 守卫比对"迁移链实际建出的表"与注册表，差一张即红。

**Tech Stack:** Python 3.12 / psycopg3 / PostgreSQL 17 / alembic（双链：`backend/alembic` 管 RDS、`backend/alembic_tee` 管 TEE）/ pytest / GitHub Actions / Phala dstack CVM（direct-TLS 网关）

**Spec:** `docs/superpowers/specs/2026-07-27-tee-full-table-alignment-design.md`
**上游 program plan:** `docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`（本计划落实其 Task 0.6 与 Phase 1 的 v2 表迁移策略）

## Global Constraints

- **commit 策略（2026-07-27 用户在 SDD pre-flight 拍板，覆盖全局规则）**：本计划在
  隔离 worktree `.claude/worktrees/tee-full-table-alignment`（分支
  `worktree-tee-full-table-alignment`）中执行，**该分支上允许 commit**。用户全局
  "绝不主动 commit/add" 规则的本意是保护主分支，隔离分支不在其列；且 SDD 的 review
  与断点恢复全靠 commit 锚定。

  规矩：**只 commit、绝不 push**；每个 Task 一个（或数个）语义完整的 commit，message
  用中文；不碰 `test` / `main` 分支。最终如何并入（squash / cherry-pick / 丢弃）由用户
  决定。**本 worktree 之外的任何场景，全局规则依然完全适用。**
- **影子期铁律：任何 TEE 侧失败只 log + 计数，绝不传染主路径。** 新增代码一律遵守（`snapshot.py` 的每表失败必须被吞并计数）。
- **守卫测试在没有 PG 时必须红，不能静默跳过。** 本仓库有过"无 PG 静默跳过约 2000 用例、391 passed 全绿是假象"的先例（memory `pytest-silently-skips-db-modules-without-pg`）。
- **`verify` 范围必须随注册表扩展**，否则新增表会产生"全绿假象"（上游 plan Phase 1 出口 gate 的硬性要求）。
- **不改动 RDS 侧任何写路径、不改 enclave。** SNAPSHOT lane 对 RDS 只读。
- **不开 PG 逻辑复制**（需改 RDS 参数组并重启实例，另排窗口）。`LOGICAL` lane 建出来但保持为空。
- **L1 测试基线**：起 PG 后 `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`。

  **2026-07-27 实测基线：`6745 passed, 1 skipped, 9 xfailed, 0 failed`，约 5.4 分钟。**

  **每个 Task 结束时必须仍是零失败。** 出现任何红都是本计划引入的回归，不得放行。

  基线一开始是 `4 failed / 6741 passed`，四条都是测试侧问题（非生产 bug），已在开工前
  单独修掉并全量验证（详见本节末的"基线清理"）。所以现在有一条干净基线可用——
  这是刻意先做的：脏基线会让后面每个 Task 的"有没有引入回归"变成猜谜。

  ⚠️ 旧版本的本文件曾写"约 2440 passed / 7 红"，那是从早期 memory 引用的过时数字，
  已被实测推翻。若你看到的基线与上面不一致，以你自己跑出来的为准并更新这一节。

  **⚠️ CI 绿 ≠ 本地全量绿**：CI 不跑全量 `tests/`，只跑四批显式点名的清单（合计 77 个
  文件，仓库有 533 个）。本计划的验收以**本地全量**为准；而新增的守卫必须**显式接进
  CI 清单**才有任何约束力（Task 2 Step 5）——现有的 `test_no_flask_anywhere` /
  `test_no_app_py_regression` / `test_v2_dependency_direction` 都不在清单里，从未在 CI
  上跑过，那是本计划要避免重蹈的覆辙。

  **基线清理（2026-07-27，开工前完成，不属于 TEE 范围）**：
  - `test_e2b_template_contract` — 本地缺 `e2b==2.34.0`（它在 `requirements.lock` 里）。
    装包即可，未改代码。
  - `test_v2_summary_watermark_seq` — 断言钉死 alembic head 名，被 0059/0060/0061 撞红。
    改为断言"链只有一个 head"（防多头才是本意，head 叫什么没有约束价值）。
  - `test_v2_capture_lifecycle` — 测试桩 `_ReplyStore._build_chat_message` 少
    `content_type` / `extra` 两个参数，没跟上 `core/store.py:398` 的签名。补齐。
  - `test_chat_response_finalize_cas` — 断言 EXPLAIN 计划含 `chat_messages_pkey`，
    因跨测试表统计漂移而在全量跑时红、单跑绿。改为断言"不出现 Seq Scan 且走了索引"
    （不退化成全表扫描才是本意，用哪个索引是 planner 的实现细节）。
- **本地 PG 起法**：`docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16`（端口 55432 是 `tests/conftest.py` 写死的默认）。
- **prod 侧任何写操作前必须先在 test 完成同一动作并验证。**

---

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/tee_shadow/table_registry.py` | **新建**。单一真源：每张 RDS 表 → lane + 理由。纯数据 + 查询 helper，零 I/O，零依赖。 |
| `tests/test_tee_table_registry.py` | **新建**。完备性守卫（双向差异）+ TEE DDL 覆盖断言。需要 PG。 |
| `tests/test_tee_registry_guard_enforced.py` | **新建**。元守卫：CI 上 PG 必须可用，否则红。纯单元，无 PG 也跑。 |
| `backend/tee_shadow/snapshot.py` | **新建**。SNAPSHOT lane：整表 `TRUNCATE`+`COPY` 原子替换。 |
| `tests/test_tee_snapshot.py` | **新建**。snapshot 的幂等性、事务原子性、FK 顺序。 |
| `backend/alembic_tee/versions/0004_*.py` | **新建**。34 张表的 TEE 侧 DDL。 |
| `scripts/tee/derive_tee_ddl.py` | **新建**。从 RDS 实库派生 TEE DDL 草稿，人工审校后落进 revision。 |
| `.github/workflows/tee-migrate.yml` | **新建**。alembic_tee 落地通道（含 head 断言）。 |
| `backend/tee_replicator/transforms.py` | 修改。新增通用单信封 transform。 |
| `backend/tee_replicator/worker.py:167` | 修改。`_TABLES` 增补 7 张密文表。 |
| `backend/admin/tee_replication.py:36,64` | 修改。`_ACTIONS` 增加 `snapshot`，`_validate` 相应放行。 |
| `backend/admin/tee_sync_scheduler.py` | 修改。`_CIPHERTEXT_TABLES` 改为从注册表派生；新增 snapshot tick。 |
| `backend/tee_shadow/verify.py` | 修改。verify 覆盖 SNAPSHOT lane。 |

---

## Task 1: alembic_tee 落地通道 + 还账 0002/0003

上游 plan Task 0.6。`alembic_tee` 至今零 CI 钩子，`0002_notify_relay` / `0003_merge_tee_heads` 合并后从未执行——test 与 prod 的 `alembic_tee_version` 实测都停在 `0001_tee_baseline`。没有这条通道，后面 Task 4 的建表全是空转。

**Files:**
- Create: `.github/workflows/tee-migrate.yml`
- Test: 手动 dispatch 验证（无单测——它是运维通道）

**Interfaces:**
- Consumes: 无
- Produces: test 与 prod 的 `alembic_tee_version` = `0003_merge_tee_heads`；后续 Task 4 的 revision 靠本通道落地。

- [ ] **Step 1: 确认两个环境的当前版本（取证基线）**

用 skill `feedling-ops-recon` §5 的 `ftee` helper：

```bash
ftee test "select version_num from alembic_tee_version;"
ftee prod "select version_num from alembic_tee_version;"
```

预期两者都输出 `0001_tee_baseline`。若已经是 `0003_merge_tee_heads`，说明有人手工跑过，跳到 Step 5 只建通道。

- [ ] **Step 2: 写 workflow**

`alembic_tee` 需要 owner 角色的 `TEE_MIGRATION_DATABASE_URL`，而 CVM 里的 backend 容器只有 app 角色——**所以不能套用 `tee-replicate.yml` 的"curl 打 admin 端点"模式**，必须在 runner 上直连 TEE。TEE 走 Phala 网关的 direct-TLS，要求 libpq ≥ 17 且 `sslnegotiation=direct`。

创建 `.github/workflows/tee-migrate.yml`：

```yaml
# .github/workflows/tee-migrate.yml — alembic_tee 迁移落地通道
#
# 为什么不套 tee-replicate.yml 的 admin-端点遥控器模式：alembic_tee 要 owner
# 角色（TEE_MIGRATION_DATABASE_URL），CVM 里的 backend 只有 app 角色。故本
# workflow 在 runner 上直连 TEE 跑迁移。TEE 走 dstack 网关 direct-TLS，需要
# libpq >= 17（psycopg[binary] 3.2 自带的 libpq 满足）+ sslnegotiation=direct。
#
# ⚠️ 这里刻意用 sslmode=verify-full + CA，**不照抄生产 backend 的 sslmode=require**
# （docker-compose.phala.yaml 里 TEE_DATABASE_URL 的注释说明它因"无 CA 分发"而用
# require）。两者处境不同：backend 与 TEE 同在 Phala prod9 内网、且只有 app 角色
# （无 DDL 权限）；而本 workflow 从**公网**的 GitHub runner 连过去、拿的是 **owner
# 角色**执行 DDL。对这种权限的连接不验证服务端身份，等于给 MITM 开一扇能改 schema
# 的门。CA 从机密文件取（见 Step 3），两环境各一套、不通用。
#
# 历史教训：0002/0003 合并后从未在实库执行（2026-07-27 实测两环境都停在 0001）。
# 故最后一步强制断言 alembic_tee_version == 代码里的 head，对不上就红。
name: TEE migrate

on:
  workflow_dispatch:
    inputs:
      environment:
        description: 目标环境
        type: choice
        options: [test, prod]
        required: true
      confirm:
        description: '输入 MIGRATE-TEE（prod 需输入 MIGRATE-TEE-PROD）确认（防误触）'
        required: true

permissions:
  contents: read

jobs:
  migrate:
    runs-on: ubuntu-24.04
    concurrency: tee-migrate
    steps:
      - name: Typo guard
        env:
          CONFIRM: ${{ inputs.confirm }}
          ENVIRONMENT: ${{ inputs.environment }}
        run: |
          if [ "$ENVIRONMENT" = "prod" ]; then WANT=MIGRATE-TEE-PROD; else WANT=MIGRATE-TEE; fi
          test "$CONFIRM" = "$WANT" || { echo "::error::confirm mismatch — ${ENVIRONMENT} 需要输入 ${WANT}"; exit 1; }

      - uses: actions/checkout@v4
        # test 环境跑 test 分支、prod 跑 main：与 app 发布流向一致。
        with:
          ref: ${{ inputs.environment == 'prod' && 'main' || 'test' }}

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install alembic + psycopg
        run: |
          python -m pip install --upgrade pip
          pip install --require-hashes -r backend/requirements.lock

      # ⚠️ 下面三个 step 一律「两套机密都注入、在 shell 里按环境挑」，**绝不**写成
      # `${{ inputs.environment == 'prod' && secrets.PROD_X || secrets.TEST_X }}`。
      # GH 表达式的 &&/|| 是 JS 语义（返回值而非布尔，空串是 falsy）：PROD_X 恰好
      # 为空时会静默短路 fallback 到 TEST_X，于是非空校验照样通过，而一次标记为
      # prod 的 dispatch 实际跑在 test 库上、Assert 还全部匹配、job 绿灯——正好复现
      # 本 workflow 要根治的那类「以为执行了其实没执行」的故障，只是换了个方向。
      # 本仓库已有同款教训与既定写法：见 pg-deploy.yml:96-99（那次是注进 prod CVM
      # 的却是 test 的密码）。按环境挑错只会挑到空值 → fail-closed。
      # 注意 pg-deploy.yml:43 的 `ref: ${{ ... && 'main' || 'test' }}` 不受此影响：
      # 它的两个分支是字面量，永远非空。危险的只是把它用在可能为空的 secrets 上。
      - name: Write CA cert
        env:
          ENVIRONMENT: ${{ inputs.environment }}
          TEST_CA_PEM: ${{ secrets.TEST_TEE_PG_CA_PEM }}
          PROD_CA_PEM: ${{ secrets.PROD_TEE_PG_CA_PEM }}
        run: |
          umask 077
          if [ "$ENVIRONMENT" = "prod" ]; then CA_PEM="$PROD_CA_PEM"; else CA_PEM="$TEST_CA_PEM"; fi
          printf '%s' "$CA_PEM" > /tmp/tee-ca.crt
          test -s /tmp/tee-ca.crt || { echo "::error::${ENVIRONMENT} 的 CA cert secret 为空或未创建"; exit 1; }

      - name: Run alembic_tee upgrade head
        env:
          ENVIRONMENT: ${{ inputs.environment }}
          TEST_DSN: ${{ secrets.TEST_TEE_MIGRATION_DSN }}
          PROD_DSN: ${{ secrets.PROD_TEE_MIGRATION_DSN }}
        run: |
          if [ "$ENVIRONMENT" = "prod" ]; then DSN="$PROD_DSN"; else DSN="$TEST_DSN"; fi
          test -n "$DSN" || { echo "::error::${ENVIRONMENT} 的 TEE_MIGRATION_DSN secret 为空或未创建"; exit 1; }
          # DSN 里必须已含 sslmode=verify-full&sslnegotiation=direct；这里只补 CA 路径。
          export TEE_MIGRATION_DATABASE_URL="${DSN}&sslrootcert=/tmp/tee-ca.crt"
          python -m backend.alembic_tee upgrade

      - name: Assert version == code head
        env:
          ENVIRONMENT: ${{ inputs.environment }}
          TEST_DSN: ${{ secrets.TEST_TEE_MIGRATION_DSN }}
          PROD_DSN: ${{ secrets.PROD_TEE_MIGRATION_DSN }}
        run: |
          if [ "$ENVIRONMENT" = "prod" ]; then DSN="$PROD_DSN"; else DSN="$TEST_DSN"; fi
          test -n "$DSN" || { echo "::error::${ENVIRONMENT} 的 TEE_MIGRATION_DSN secret 为空或未创建"; exit 1; }
          export TEE_MIGRATION_DATABASE_URL="${DSN}&sslrootcert=/tmp/tee-ca.crt"
          python - <<'PY'
          import os, sys, psycopg
          sys.path.insert(0, "backend")
          from alembic.config import Config
          from alembic.script import ScriptDirectory
          cfg = Config("backend/alembic_tee/alembic.ini")
          cfg.set_main_option("script_location", "backend/alembic_tee")
          want = ScriptDirectory.from_config(cfg).get_current_head()
          with psycopg.connect(os.environ["TEE_MIGRATION_DATABASE_URL"]) as c:
              got = c.execute("select version_num from alembic_tee_version").fetchone()[0]
          print(f"code head={want} db={got}")
          assert got == want, f"alembic_tee 未落到 head: db={got} want={want}"
          PY
```

- [ ] **Step 3: 登记所需 secrets**

需要 4 个仓库 secret（**由用户创建，agent 不得代填凭证**）：
`TEST_TEE_MIGRATION_DSN`、`TEST_TEE_PG_CA_PEM`、`PROD_TEE_MIGRATION_DSN`、`PROD_TEE_PG_CA_PEM`。

2026-07-27 `gh secret list` 实测：这 4 个**都不存在**。仓库现有的相关 secret 是
`TEST_TEE_DATABASE_URL` / `PROD_TEE_DATABASE_URL`（**app 角色，无 DDL 权限，不能拿来跑
迁移**）和 `TEST_PG_OWNER_PASSWORD` / `PROD_PG_OWNER_PASSWORD`（owner 口令，但不是完整
DSN）。所以不能复用现成的，必须新建这 4 个。

值的来源（**只读取、不回显、不写进任何会提交的文件**）：
- test：`~/documents/teleport/feedling-pg-test-secrets.txt` 里的 `TEE_MIGRATION_DATABASE_URL` 与内嵌 PEM 块。
- prod：`~/feedling-io-db-prod/prod-pg-secrets.env` 的 `PG_OWNER_PASSWORD` 拼 DSN，CA 在 `~/feedling-io-db-prod/certs/ca.crt`。

DSN 形状（host 用完整 app_id，端口固定 443，dbname 固定 feedling）：
```
postgresql://feedling_owner:<PW>@<APP_ID>-5432s.dstack-pha-prod9.phala.network:443/feedling?sslmode=verify-full&sslnegotiation=direct
```

- [ ] **Step 4: 在 test 上 dispatch 一次（还账）**

`environment=test`、`confirm=MIGRATE-TEE`。预期最后一步打印 `code head=0003_merge_tee_heads db=0003_merge_tee_heads`。

**若 direct-TLS 连接失败**（`server closed the connection unexpectedly`），说明 runner 上 psycopg 捆绑的 libpq < 17 不认 `sslnegotiation`。降级方案：在 workflow 里 `apt-get install -y postgresql-client-17`（加 PGDG 源），改用 `psql -f` 跑 alembic 生成的 SQL（`alembic upgrade head --sql`）。这条分支在实施时若触发，需先向用户报告再改。

- [ ] **Step 5: 验证 test 的 notify_relay 两表已建**

```bash
ftee test "select count(*) from information_schema.tables where table_schema='public' and table_name like 'notify_relay%';"
```
预期 `2`。

- [ ] **Step 6: prod 还账**

`environment=prod`、`confirm=MIGRATE-TEE-PROD`。同样验证：

```bash
ftee prod "select version_num from alembic_tee_version;"
ftee prod "select count(*) from information_schema.tables where table_schema='public' and table_name like 'notify_relay%';"
```

- [ ] **Step 7: commit 并报告改动**

改动只有一个新文件 `.github/workflows/tee-migrate.yml`。向用户报告：文件路径、已 dispatch 的两次运行结果、两环境 `alembic_tee_version` 的前后值。在本 worktree 分支上 commit（只 commit，不 push）。

---

## Task 2: 表注册表骨架 + 完备性守卫

先建"空"注册表和守卫，让守卫**红**——红灯的内容就是 45 张未登记表的清单，Task 3 再把它填绿。先红后绿是刻意的：它证明守卫真的会拦。

**Files:**
- Create: `backend/tee_shadow/table_registry.py`
- Create: `tests/test_tee_table_registry.py`
- Create: `tests/test_tee_registry_guard_enforced.py`
- Modify: `tests/conftest.py`（把元守卫加进 `_PURE_UNIT`）
- Modify: `.github/workflows/ci.yml`（**把两个守卫加进 CI 点名清单——见 Step 5，漏了这步整个 Task 白做**）

> **⚠️ 2026-07-27 实测发现，必读**：本仓库的 CI **从不跑全量 `tests/`**。
> `ci.yml` 里是四批**显式点名的文件清单**，合计 77 个文件，而仓库有 533 个测试
> 文件。后果是——现有的守卫测试 `test_no_flask_anywhere`、
> `test_no_app_py_regression`、`test_v2_dependency_direction` **一个都不在 CI
> 清单里，从来没在 CI 上跑过**，只有人本地跑全量时才可能发现。
>
> 所以本 Task 照搬 `test_no_flask_anywhere` 的**写法**（grep/断言风格），但**绝不
> 照搬它的接线**——它恰恰漏了最关键的一步。守卫不进 `ci.yml` 的点名清单，就等于
> 没有守卫，本 Task 的全部价值主张（"漏登记即 CI 红"）也就落空了。

**Interfaces:**
- Produces:
  - `table_registry.Entry(lane: str, reason: str, manual: bool = False)` — frozen dataclass
  - `table_registry.REGISTRY: dict[str, Entry]`
  - `table_registry.MIRROR / CIPHERTEXT / SNAPSHOT / SKIP / LOGICAL: str` — lane 常量
  - `table_registry.LANES: tuple[str, ...]`
  - `table_registry.tables_in_lane(lane: str) -> tuple[str, ...]` — 按表名排序
  - `table_registry.synced_tables() -> tuple[str, ...]` — 所有非 SKIP lane 的表
- Consumes: 无（纯数据模块）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tee_table_registry.py`（**注意：光写这个文件不够，Step 5 必须把它接进 `ci.yml`**）：

```python
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
```

- [ ] **Step 2: 写元守卫（无 PG 必红）**

上面那个测试文件需要 PG，而 `conftest.py` 在无 PG 时会把它 `collect_ignore` 掉——**一个会静默跳过的守卫等于没有守卫**。所以再加一个不需要 PG 的元守卫。

创建 `tests/test_tee_registry_guard_enforced.py`：

```python
"""元守卫：保证注册表守卫在 CI 上真的跑了。

tests/conftest.py 在连不上 Postgres 时会 collect_ignore 掉所有非纯单元的测试
模块，且零 skipped 计数——本仓库有过"391 passed 全绿"其实少跑约 2000 个用例的
先例（memory pytest-silently-skips-db-modules-without-pg）。

test_tee_table_registry.py 需要 PG，因此在无 PG 时会被静默忽略。本文件不需要
PG，所以永远会被收集；它断言"在 CI 上 PG 必须可用"，把静默跳过变成红灯。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import conftest  # noqa: E402  — tests/conftest.py，取其 provisioning 结果


def test_guard_module_exists():
    """守卫文件不能被误删——删了它，漏登记的表就再也没人拦。"""
    guard = Path(__file__).parent / "test_tee_table_registry.py"
    assert guard.is_file(), "注册表守卫文件不存在；它是 TEE 同步机制的唯一拦截点"


def test_postgres_is_provisioned_on_ci():
    """CI 上没有 PG = 注册表守卫被静默跳过 = 机制失效。直接红。

    本地开发机没起 PG 时不红（只是那趟跑不到守卫），但 CI 必须硬性保证。
    """
    if os.environ.get("CI", "").lower() not in ("1", "true"):
        return
    assert conftest._provisioned, (
        "CI 上 Postgres 未就绪，TEE 注册表守卫（及约 2000 个 DB 用例）被静默跳过。"
        f"provisioning 错误：{conftest._PROVISION_ERROR!r}"
    )
```

- [ ] **Step 3: 把元守卫登记进 conftest 的 `_PURE_UNIT`**

元守卫必须在无 PG 时也能被收集，否则它自己也会被 ignore 掉（那就成了自指的空洞）。

修改 `tests/conftest.py`，在 `_PURE_UNIT` 集合里加一行（放在 `"test_redis_pool.py",` 之后）：

```python
        "test_redis_pool.py",
        # TEE 注册表守卫的元守卫：断言 CI 上 PG 真的起了（守卫本体需要 PG，
        # 无 PG 时会被下面的 collect_ignore 静默忽略）。它自己不碰 DB，必须
        # 留在可收集列表里，否则连它也会被忽略。
        "test_tee_registry_guard_enforced.py",
```

- [ ] **Step 4: 写注册表骨架（先留空，让守卫红）**

创建 `backend/tee_shadow/table_registry.py`：

```python
"""RDS 表 → TEE 同步 lane 的单一真源。

背景：TEE 同步原本是三层手工白名单登记制——DDL 手写 alembic_tee revision、
数据流分别登记在 db.py 的 mirror 写点 / tee_replicator.worker._TABLES /
tee_shadow.reconciler.TABLES。三处互不校验，谁都不是全集，所以 Runtime V2 的
19 张新表可以一张都没登记而无人发现（2026-07-27 实测 RDS 61 张 / TEE 20 张）。

本模块是那个"全集"。规则：**每一张 RDS 表必须有且只有一条登记**，由
tests/test_tee_table_registry.py 强制。加了 RDS 表却没登记 lane 的改动合不进去。

lane 语义见下面常量的注释。注册表是纯数据 + 查询 helper，不做任何 I/O——它被
scheduler、verify、snapshot 三处消费，必须能在任何上下文里安全 import。
"""
from __future__ import annotations

from dataclasses import dataclass

# 热路径双写：写主库的同时经 tee_shadow.mirror.execute 尽力而为地写 TEE。
# 适用于低频写的明文运维表。
MIRROR = "MIRROR"

# 游标驱动的密文→明文复制：tee_replicator.worker 拉 RDS 密文行、过 enclave
# 解密、写 TEE 明文行。适用于装信封的表。
CIPHERTEXT = "CIPHERTEXT"

# 整表快照刷：TRUNCATE + COPY 原子替换（tee_shadow.snapshot）。适用于数据量
# 小、但有 UPDATE/DELETE 的明文表——全量替换天然处理可变行，不需要 requeue
# 补偿和 prune。
SNAPSHOT = "SNAPSHOT"

# 不同步。理由必填且必须具体（守卫会拒绝"暂不需要"这类含糊理由）。
SKIP = "SKIP"

# PG 原生逻辑复制。**预留，尚未实现**：需要把 RDS 的 rds.logical_replication
# 打开（当前 off、wal_level=replica）并重启实例，属独立运维窗口。通道打通后，
# 把表从 SNAPSHOT 改成 LOGICAL 即可，不需要重做机制——这正是这个空 lane 存在
# 的意义。守卫强制它保持为空。
LOGICAL = "LOGICAL"

LANES = (MIRROR, CIPHERTEXT, SNAPSHOT, SKIP, LOGICAL)


@dataclass(frozen=True)
class Entry:
    lane: str
    reason: str
    # True = 这张表不由 alembic 迁移链创建（人工 SQL 建的，如一次性备份表）。
    # 守卫据此放行"注册表里有、迁移链里没有"的条目；不标就当成表名打错。
    manual: bool = False


REGISTRY: dict[str, Entry] = {
    # Task 3 填充。
}


def tables_in_lane(lane: str) -> tuple[str, ...]:
    """某条 lane 下的表名，按字典序。"""
    return tuple(sorted(t for t, e in REGISTRY.items() if e.lane == lane))


def synced_tables() -> tuple[str, ...]:
    """所有会进 TEE 的表（即非 SKIP），按字典序。"""
    return tuple(sorted(t for t, e in REGISTRY.items() if e.lane != SKIP))
```

- [ ] **Step 5: 把守卫接进 CI 点名清单（漏了这步整个 Task 白做）**

CI 不跑全量 `tests/`，只跑点名的文件。守卫必须显式登记，否则它永远不会在 CI 上执行。

加进第一批清单——那批已经配好了 Postgres service（`FEEDLING_TEST_PG` 指向 CI 的 PG），正是守卫需要的。修改 `.github/workflows/ci.yml`，把 `Run db-layer + isolation suites` 那一步的 `run: >-` 块改成：

```yaml
        run: >-
          python -m pytest
          tests/test_db.py
          tests/test_multi_tenant_isolation.py
          tests/test_asgi_cors.py
          tests/openapi/test_public_openapi.py
          tests/test_tee_table_registry.py
          tests/test_tee_registry_guard_enforced.py
          -v
```

（前四行是原有内容，原样保留；只追加后两行。行的相对顺序无所谓，pytest 会自己排。）

- [ ] **Step 6: 跑守卫，确认它红**

```bash
docker start feedling-test-pg 2>/dev/null || docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
python -m pytest tests/test_tee_table_registry.py tests/test_tee_registry_guard_enforced.py -v
```

预期：
- `test_every_rds_table_is_registered` **FAIL**，错误信息列出全部未登记的表（约 61 张——注册表现在是空的）。
- `test_tee_schema_covers_every_synced_table` PASS（`synced_tables()` 为空，空集被覆盖）。
- `test_logical_lane_is_empty_for_now` PASS。
- 元守卫两条 PASS。

**把 FAIL 输出里的表清单存下来**，Task 3 要按它逐张登记。

- [ ] **Step 7: 验证守卫真的进了 CI 清单**

```bash
grep -c "test_tee_table_registry\|test_tee_registry_guard_enforced" .github/workflows/ci.yml
```
预期 `2`。这一条是本 Task 的**真正验收标准**——守卫写得再好，不在 CI 清单里就等于没写
（`test_no_flask_anywhere` 就是活的反例，它至今没在 CI 上跑过一次）。

- [ ] **Step 8: commit 并报告改动**

改动：3 个新文件 + `tests/conftest.py` 一处增行 + `.github/workflows/ci.yml` 两行。报告"守卫已就位、已接进 CI、且如预期报红，列出 N 张待登记表"。在本 worktree 分支上 commit（只 commit，不 push）。

---

## Task 3: 登记全部 61 张表

把 Task 2 的红灯填绿。这一步是纯数据录入，但**每条理由都要能经得起复审**——SKIP 的理由尤其，它是"这张表永远不进 TEE"的承诺。

**Files:**
- Modify: `backend/tee_shadow/table_registry.py`（填充 `REGISTRY`）

**Interfaces:**
- Consumes: `Entry` / lane 常量（Task 2）
- Produces: 填满的 `REGISTRY`；后续 Task 5/6/7 用 `tables_in_lane()` 派生各自的表清单。

- [ ] **Step 1: 核对当前 RDS 表清单**

注册表必须覆盖**当前**的 RDS 表集合，而不是本计划写作时（2026-07-27）的快照——两天 5.5 万行的变更速度下，实施时很可能已经多出新表。

```bash
python -m pytest tests/test_tee_table_registry.py::test_every_rds_table_is_registered -v 2>&1 | tail -80
```

以这个输出为准。若出现下面清单里没有的表，**停下来问用户该表走哪条 lane**，不要自行归类。

- [ ] **Step 2: 填充 REGISTRY**

把 `REGISTRY: dict[str, Entry] = {` 的空 body 替换为：

```python
REGISTRY: dict[str, Entry] = {
    # ---------------------------------------------------------------- #
    # MIRROR —— 明文运维表，热路径双写（db.py 的 mirror.execute 写点）。
    # 这 13 张是 alembic_tee 0001 baseline 的"13 张明文运维表"，加 0002 的
    # notify_relay 两张。列定义与 RDS 逐列对齐。
    # ---------------------------------------------------------------- #
    "server_config": Entry(MIRROR, "全局配置，低频写；reconciler.TABLES 已覆盖"),
    "global_blobs": Entry(MIRROR, "全局 blob，低频写；reconciler.TABLES 已覆盖"),
    "users": Entry(MIRROR, "账号父表，所有 per-user 表的 FK 目标，必须最先同步"),
    "user_blobs": Entry(MIRROR, "per-user 杂项；kind='identity' 归 CIPHERTEXT、"
                                "kind='consumer_state' 有意不镜像（见 reconciler._SCOPE_WHERE）"),
    "user_logs": Entry(MIRROR, "per-user 日志流；seq 是 IDENTITY 列，靠 OVERRIDING SYSTEM VALUE 搬"),
    "perception_items": Entry(MIRROR, "感知条目，明文；reconciler.TABLES 已覆盖"),
    "perception_daily": Entry(MIRROR, "感知日聚合，明文；reconciler.TABLES 已覆盖"),
    "copytext_strings": Entry(MIRROR, "文案表，明文；reconciler.TABLES 已覆盖"),
    "copytext_meta": Entry(MIRROR, "文案版本哨兵单行表；reconciler.TABLES 已覆盖"),
    "genesis_import_jobs": Entry(MIRROR, "入住导入作业元数据，明文；reconciler.TABLES 已覆盖"),
    "genesis_import_outputs": Entry(MIRROR, "入住导入产物，明文；reconciler.TABLES 已覆盖"),
    "agent_runtime_instances": Entry(MIRROR, "runtime 实例登记，明文运维表"),
    "agent_runtime_supervisor_heartbeats": Entry(MIRROR, "supervisor 心跳，明文运维表"),
    "notify_relay_configs": Entry(MIRROR, "自部署推送中继配置；alembic_tee 0002 已建表"),
    "notify_relay_logs": Entry(MIRROR, "推送中继日志；id 是 IDENTITY 列"),

    # ---------------------------------------------------------------- #
    # CIPHERTEXT —— 装信封的表，经 enclave 解密成明文写进 TEE。
    # ---------------------------------------------------------------- #
    "chat_messages": Entry(CIPHERTEXT, "对话正文信封 doc；worker._TABLES 已覆盖"),
    "memory_moments": Entry(CIPHERTEXT, "记忆信封 doc；worker._TABLES 已覆盖"),
    "world_book_entries": Entry(CIPHERTEXT, "世界书信封 doc；worker._TABLES 已覆盖"),
    "frame_envelopes": Entry(
        CIPHERTEXT,
        "帧信封；TEE 侧对应物是形状不同的 frames（R2 存储层指针），由 worker "
        "的 frames row_writer 处理，不是同名表",
    ),
    # 以下 7 张是 2026-07-27 全量对齐新增（用户拍板解密成明文存）。
    "chat_message_archive": Entry(
        CIPHERTEXT,
        "归档对话，doc 与 chat_messages.doc 完全同形（prod 897 行中 896 行是完整"
        "信封、1 行是 R2 offload 指针需先水合 body_ct）；复用 plaintext_chat_doc",
    ),
    "v2_trajectory_events": Entry(
        CIPHERTEXT,
        "V2 轨迹事件 payload_envelope；表级 CHECK ck_v2_trajectory_envelope 强制 "
        "K_enclave + visibility='shared'，故服务端可解",
    ),
    "model_api_credentials": Entry(
        CIPHERTEXT,
        "BYOK provider key 信封 api_key_envelope。2026-07-27 用户拍板解密成明文存"
        "（设计文档 §8 已记录其安全含义：TEE owner 角色可读全库）",
    ),
    "v2_conversation_summary": Entry(CIPHERTEXT, "V2 对话摘要 summary_envelope，标准单信封"),
    "v2_conversation_summary_segments": Entry(CIPHERTEXT, "V2 摘要分段 summary_envelope，标准单信封"),
    "v2_trajectory_reviews": Entry(
        CIPHERTEXT,
        "V2 轨迹复核 review_envelope（可空，CHECK 允许 NULL）；prod 当前 0 行",
    ),
    "v2_workspace_entries": Entry(CIPHERTEXT, "V2 工作区条目 content_envelope；prod 当前 0 行"),

    # ---------------------------------------------------------------- #
    # SNAPSHOT —— 明文、数据量小、但有 UPDATE/DELETE 的表。整表原子替换。
    # 已实测确认这批表的 jsonb 列（payload_json / result_json / detail_json /
    # state_json / actions_json / v2_effect_outbox.payload）装的是明文，不含信封。
    # ---------------------------------------------------------------- #
    "agent_action_queue": Entry(SNAPSHOT, "动作队列，行状态流转（UPDATE 密集），明文 payload_json"),
    "agent_jobs": Entry(SNAPSHOT, "agent 作业表，status 流转，明文"),
    "agent_status_events": Entry(SNAPSHOT, "agent 状态事件，明文 detail_json"),
    "chat_r2_cleanup": Entry(SNAPSHOT, "R2 清理队列，行会被删，明文"),
    "chat_r2_lifecycle": Entry(SNAPSHOT, "R2 生命周期状态，UPDATE 密集，明文"),
    "dau_daily_snapshot": Entry(SNAPSHOT, "DAU 日快照，每日批量写，明文"),
    "model_api_routes": Entry(SNAPSHOT, "BYOK 路由配置（不含凭证，凭证在 model_api_credentials），明文"),
    "provider_health": Entry(SNAPSHOT, "provider 健康状态，UPDATE 密集，明文"),
    "retention_cohort_snapshot": Entry(SNAPSHOT, "留存 cohort 快照，批量写，明文"),
    "runtime_state": Entry(SNAPSHOT, "runtime 状态 state_json，UPDATE 密集，明文"),
    "user_growth_daily_snapshot": Entry(SNAPSHOT, "增长日快照，批量写，明文"),
    "v2_capture_batches": Entry(SNAPSHOT, "V2 捕获批次 actions_json，status 流转，明文"),
    "v2_effect_outbox": Entry(SNAPSHOT, "V2 效果 outbox，投递后删行，明文 payload（实测非信封）"),
    "v2_effect_sink_applied": Entry(SNAPSHOT, "V2 效果幂等标记，明文"),
    "v2_mcp_mutation_attempts": Entry(SNAPSHOT, "V2 MCP 变更尝试记录，明文"),
    "v2_runtime_control": Entry(SNAPSHOT, "V2 运行时总控单行表，明文"),
    "v2_runtime_state": Entry(SNAPSHOT, "V2 per-user 运行时 fence，UPDATE 密集，明文"),
    "v2_sandbox_usage_events": Entry(SNAPSHOT, "V2 sandbox 用量事件，明文"),
    "v2_terminal_failure_outbox": Entry(SNAPSHOT, "V2 终态失败 outbox，投递后删行，明文"),
    "v2_trajectory_access_audit": Entry(SNAPSHOT, "V2 轨迹访问审计（审计元数据本身是明文）"),
    "v2_trajectory_streams": Entry(SNAPSHOT, "V2 轨迹流游标，UPDATE 密集，明文"),
    "v2_turn_metrics": Entry(SNAPSHOT, "V2 回合指标，明文"),
    "v2_user_allowlist": Entry(SNAPSHOT, "V2 灰度名单，UPDATE 密集，明文"),
    "v2_wake_schedule": Entry(SNAPSHOT, "V2 唤醒排程，UPDATE 密集，明文"),
    "v2_worker_heartbeats": Entry(SNAPSHOT, "V2 worker 心跳，UPDATE 密集，明文"),

    # ---------------------------------------------------------------- #
    # SKIP —— 永远不进 TEE。理由必须具体。
    # ---------------------------------------------------------------- #
    "alembic_version": Entry(
        SKIP, "RDS 迁移链自己的版本表；TEE 有独立的 alembic_tee_version，两条链互不感知"),
    "genesis_import_chunks": Entry(
        SKIP, "入住导入的 staging 数据，冻结窗口内处理完即弃，非用户资产（上游 plan 已决定不复制）"),
    "tee_sync_runs": Entry(
        SKIP, "TEE 同步自身的控制面/指标表，必须住在 RDS——复制到被它监控的库里没有意义"),
    "tee_reconcile_state": Entry(SKIP, "TEE reconcile 的控制面状态，同上，必须住 RDS"),
    "tee_reconcile_cursors": Entry(SKIP, "TEE reconcile 的游标，同上，必须住 RDS"),
    "bak_20260710_usr450_blobs": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr450_chat": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr450_memory": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr450_users": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr5d4a_users": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
}
```

- [ ] **Step 3: 跑守卫**

```bash
python -m pytest tests/test_tee_table_registry.py -v
```

预期此时的状态：
- `test_every_rds_table_is_registered` **PASS**
- `test_no_phantom_entries` **PASS**（`bak_*` 在本地测试库不存在，靠 `manual=True` 放行）

  ⚠️ **若这条报出 `bak_*` 以外的表，不要随手加 `manual=True` 糊过去。** 守卫读的是
  本地 `db.init_schema()` 建的库，而上面的登记清单来自 prod 实库；多出来的表说明
  `init_schema()` 与生产 schema 存在漂移（某张表只由 alembic 迁移建、`init_schema()`
  没跟上，或反之）。那是一个独立的、值得单独修的问题——**停下来报告用户**。
  `manual=True` 的语义是"人工 SQL 建的表"，不是"守卫报红的消音开关"。
- `test_lanes_are_valid_and_reasons_nonempty` PASS
- `test_skip_entries_must_justify` PASS
- `test_logical_lane_is_empty_for_now` PASS
- `test_tee_schema_covers_every_synced_table` **FAIL** —— 列出 34 张登记了非 SKIP lane 但 alembic_tee 还没建的表。这是预期的，Task 4 修它。

- [ ] **Step 4: 跑全量 L1，确认没引入回归**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py 2>&1 | tail -5
```
预期：**恰好 1 failed** —— 新增的 `test_tee_schema_covers_every_synced_table`（Task 4 会修它）。基线是零失败，所以多出的任何其他红都是本 Task 引入的回归。

- [ ] **Step 5: commit 并报告改动**

报告：登记了多少张表、各 lane 各多少、剩下的那条红是 Task 4 的入口。在本 worktree 分支上 commit（只 commit，不 push）。

---

## Task 4: 派生并落地 34 张表的 TEE DDL

**Files:**
- Create: `scripts/tee/derive_tee_ddl.py`
- Create: `backend/alembic_tee/versions/0004_full_table_alignment.py`

**Interfaces:**
- Consumes: `table_registry.synced_tables()`（Task 3）
- Produces: TEE 库多出 34 张表 → Task 2 的 `test_tee_schema_covers_every_synced_table` 转绿；Task 5/7 有表可写。

- [ ] **Step 1: 写派生脚本**

手抄 DDL 是 0002 之外的另一半事故源（`reconciler.TABLES` 的注释里记着"brief 里附带的 TABLES 草稿有多处失真"）。从实库派生。

创建 `scripts/tee/derive_tee_ddl.py`：

```python
#!/usr/bin/env python3
"""从 RDS 实库派生 TEE 侧 DDL 草稿。

**产出是草稿，必须人工审校后再落进 alembic_tee revision。** 脚本做不了的判断：
- 信封列在 TEE 侧是明文（CIPHERTEXT lane）还是原样保留，以及对应的 CHECK 约束
  要不要重写——解密后 payload_envelope 变成明文 doc，原 CHECK 必然失效。
- per-user FK 要指向 TEE 自己的 users 表。
- 哪些索引值得带过去（TEE 是副本，读模式与主库不同）。

用法：
    python scripts/tee/derive_tee_ddl.py --dsn "$PROD_DATABASE_URL" > /tmp/ddl.sql
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from tee_shadow import table_registry as reg  # noqa: E402


def _columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default,
               is_identity, identity_generation
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    out = []
    for name, dtype, nullable, default, is_identity, id_gen in rows:
        frag = f"    {name} {dtype.upper()}"
        if is_identity == "YES":
            frag += f" GENERATED {id_gen} AS IDENTITY"
        elif default is not None:
            frag += f" DEFAULT {default}"
        if nullable == "NO":
            frag += " NOT NULL"
        out.append(frag)
    return out


def _has_user_fk(conn, table: str) -> bool:
    """RDS 侧这张表有没有 user_id → users 的外键。

    TEE 的 FK 必须与 RDS 一致，不能"有 user_id 列就加"——见 main() 里的详细说明。
    """
    row = conn.execute(
        """
        SELECT 1
        FROM pg_constraint con
        JOIN pg_class child ON child.oid = con.conrelid
        JOIN pg_class parent ON parent.oid = con.confrelid
        WHERE con.contype = 'f'
          AND child.relname = %s
          AND parent.relname = 'users'
        LIMIT 1
        """,
        (table,),
    ).fetchone()
    return row is not None


def _pk(conn, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()

    want = [t for t in reg.synced_tables()]
    with psycopg.connect(args.dsn) as conn:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        for table in want:
            if table not in existing:
                print(f"-- SKIP {table}: 不在该 RDS 实库中", file=sys.stderr)
                continue
            cols = _columns(conn, table)
            pk = _pk(conn, table)
            print(f"\nCREATE TABLE IF NOT EXISTS {table} (")
            body = list(cols)
            if pk:
                body.append(f"    PRIMARY KEY ({', '.join(pk)})")
            # FK 规则：**照搬 RDS 实际有的 user_id→users 外键，RDS 没有就不加**。
            #
            # 绝不能写成"有 user_id 列就补一条 CASCADE FK"（本计划初版就是这么写的，
            # 2026-07-28 Task 4 审查抓出）：那会让 TEE 副本比源库更严格，而复制系统里
            # 副本一旦比源严格，源里合法的数据到副本就插不进去。实测 6 张表在 RDS 侧
            # 根本没有 user_id→users 的 FK（agent_action_queue / chat_r2_cleanup /
            # chat_r2_lifecycle / v2_terminal_failure_outbox / v2_trajectory_events /
            # v2_user_allowlist），其中 chat_r2_lifecycle 在 prod 有 32/344 行孤儿
            # （user_id 在 users 里已不存在）——给它加 FK，SNAPSHOT 的
            # TRUNCATE+COPY 单事务会因这 32 行整表失败，且每个 tick 都失败。
            if _has_user_fk(conn, table):
                body.append(
                    "    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE")
            print(",\n".join(body))
            print(");")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 跑派生，产出草稿**

```bash
mkdir -p scripts/tee
python scripts/tee/derive_tee_ddl.py \
  --dsn "$(grep -m1 '^PROD_DATABASE_URL=' .env | cut -d= -f2-)" > /tmp/tee_ddl_draft.sql
wc -l /tmp/tee_ddl_draft.sql
```

用 prod 派生（它是表最全的库：61 张，test 只有 56）。

- [ ] **Step 3: 人工审校草稿——三件必须改的事**

逐张过 `/tmp/tee_ddl_draft.sql`，改这三类：

1. **CIPHERTEXT lane 的信封列改成明文列。** 7 张表里带 `*_envelope` / `doc` 的列，在 TEE 侧存的是解密后的明文 JSONB，列类型不变（仍是 `JSONB`）但**必须删掉派生出来的信封 CHECK 约束**（`ck_v2_trajectory_envelope` 等要求 `body_ct`/`K_enclave` 存在，明文行必然违反）。派生脚本不导出 CHECK，所以这里主要是确认没有漏带。
2. **`user_id` 外键：以 RDS 实际有没有为准，不是以"有没有 user_id 列"为准。**
   脚本的 `_has_user_fk()` 已按 RDS 的 `pg_constraint` 判定。核对时确认这 6 张
   **RDS 侧无 user_id→users FK 的表，TEE 侧也没有**：`agent_action_queue`、
   `chat_r2_cleanup`、`chat_r2_lifecycle`、`v2_terminal_failure_outbox`、
   `v2_trajectory_events`、`v2_user_allowlist`。

   > 为什么这条是硬性的：TEE 是 RDS 的副本，副本比源库更严格就意味着"源里合法、
   > 副本插不进"。实测 `chat_r2_lifecycle` 在 prod 有 **32/344 行孤儿**（`user_id`
   > 在 `users` 里已不存在，疑似账号 reset 未清净的残留，与 memory
   > `sql-delete-account-misses-worldbook-and-r2` 同类）。给它加 FK，SNAPSHOT 的
   > `TRUNCATE`+`COPY` 单事务会因这 32 行整表失败，而且**每个 tick 都失败**。
   >
   > 孤儿本身是个独立问题，值得单独处理，但不该由 TEE 复制来暴露和阻塞。
3. **IDENTITY 列**：带 `GENERATED ALWAYS AS IDENTITY` 的列要与 RDS 一致（复制时靠 `OVERRIDING SYSTEM VALUE` 原样搬 seq，见 `reconciler._IDENTITY_TABLES` 的注释）。

- [ ] **Step 4: 写 revision**

创建 `backend/alembic_tee/versions/0004_full_table_alignment.py`：

```python
"""TEE 全量对齐：补齐 34 张缺失表（spec 2026-07-27-tee-full-table-alignment）

Revision ID: 0004_full_table_alignment
Revises: 0003_merge_tee_heads
Create Date: 2026-07-27

DDL 由 scripts/tee/derive_tee_ddl.py 从 prod RDS 实库派生后人工审校，不是手抄
（手抄失真有前科，见 reconciler.TABLES 的注释）。

⚠️ 本 revision 会重新创建 0002_drop_retired_supervisor 刚删掉的两张表
（agent_runtime_instances / agent_runtime_supervisor_heartbeats）。这**不是**误操作，
是 2026-07-27 用户在"TEE 全量对齐"目标下的明确决定：0002 当时按"V1 supervisor 已退役"
撤销了这两张镜像，但实测 V1 在 RDS 侧仍然活着——prod 有 220 行 agent_runtime_instances
和 1 行心跳，backend 也仍在写（db.py:335 / agent_runtime/leases.py:51）。既然目标是让
TEE 成为 RDS 的完整副本以备扶正，就不能留这个缺口。
已知代价（用户接受）：V1 真正退役后这两张表会在 RDS 侧消失，届时 TEE 侧需要再删一次。

三类改动相对 RDS 原始 DDL：
1. CIPHERTEXT lane（7 张）的信封列在 TEE 侧存明文，故不带 RDS 那边的信封 CHECK
   约束（ck_v2_trajectory_envelope 等要求 body_ct/K_enclave 存在，明文行必然违反）。
2. per-user FK 指向 TEE 自己的 users 表，ON DELETE CASCADE。
3. 索引只带 PK 与 replicate/snapshot 读路径实际用到的，不照搬主库的全部索引
   （TEE 是副本，写模式是批量替换，多余索引只是写放大）。

DDL 幂等（IF NOT EXISTS），与 0001 baseline 的安全性质一致。
"""

from alembic import op

revision = "0004_full_table_alignment"
down_revision = "0003_merge_tee_heads"
branch_labels = None
depends_on = None


_DDL = """
-- 此处粘贴 Step 3 审校后的 DDL。
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    # 与 0001 baseline 同策略：影子库的 downgrade 不实现——回滚手段是重建库 +
    # 从 WAL-G 恢复，不是逐表 DROP（DROP 会连带毁掉已复制的数据）。
    raise NotImplementedError("alembic_tee 不支持 downgrade；回滚走 WAL-G 恢复")
```

把 Step 3 审校后的 DDL 粘进 `_DDL`。

- [ ] **Step 5: 本地验证 revision 可用**

```bash
docker rm -f feedling-test-pg 2>/dev/null
docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
sleep 5
python -m pytest tests/test_tee_table_registry.py -v
```

预期全部 6 条 PASS——包括 `test_tee_schema_covers_every_synced_table`（conftest 会在全新测试库上跑 `alembic_tee.upgrade_head()`，走到 0004）。

- [ ] **Step 6: 跑全量 L1**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py 2>&1 | tail -5
```
预期回到**零失败**（新增的守卫全绿），passed 数比基线的 6745 多出本 Task 新增的用例数。

- [ ] **Step 7: 落到 test 实库**

dispatch Task 1 建的 `TEE migrate` workflow，`environment=test`、`confirm=MIGRATE-TEE`。然后核对：

```bash
ftee test "select version_num from alembic_tee_version;"
ftee test "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE';"
```
预期 `0004_full_table_alignment` 与 `52`（20 + 32）。

- [ ] **Step 8: commit 并报告改动**

**prod 先不落**——等 Task 7 的解密探针过了再落，否则 prod TEE 会多出 7 张空的密文表却没有数据管线。

---

## Task 5: SNAPSHOT lane 实现

**Files:**
- Create: `backend/tee_shadow/snapshot.py`
- Create: `tests/test_tee_snapshot.py`

**Interfaces:**
- Consumes: `table_registry.tables_in_lane(SNAPSHOT)`；`db.get_pool()`；`mirror.get_tee_pool()`
- Produces:
  - `snapshot.snapshot_table(table: str) -> dict` — 返回 `{"table", "rows", "ok", "error"}`
  - `snapshot.snapshot_all() -> dict` — 返回 `{"tables": [...], "copied": int, "failures": int}`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tee_snapshot.py`：

```python
"""SNAPSHOT lane：整表 TRUNCATE + COPY 原子替换。

为什么是全量替换而不是增量：这批表数据量极小（prod 实测 25 张合计 1340 行 /
约 2 MB），但有大量 UPDATE/DELETE（队列 status 流转、心跳、allowlist）。现有的
append-only 游标模型处理可变行要靠 requeue 补偿 + prune，成本线性于表数且永久；
全量替换天然正确。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from tee_shadow import mirror, snapshot


@pytest.fixture
def sample_table():
    """在两侧建一张同形的测试表，避免依赖真实业务表的 schema 演进。"""
    ddl = ("CREATE TABLE IF NOT EXISTS _snap_probe ("
           "  k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    with db.get_pool().connection() as c:
        c.execute(ddl)
        c.execute("TRUNCATE _snap_probe")
    with mirror.get_tee_pool().connection() as c:
        c.execute(ddl)
        c.execute("TRUNCATE _snap_probe")
    yield "_snap_probe"
    with db.get_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS _snap_probe")
    with mirror.get_tee_pool().connection() as c:
        c.execute("DROP TABLE IF EXISTS _snap_probe")


def _tee_rows(table: str) -> list[tuple]:
    with mirror.get_tee_pool().connection() as c:
        return c.execute(f"SELECT k, v FROM {table} ORDER BY k").fetchall()


def test_snapshot_copies_rows(sample_table):
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1'), ('b','2')")
    rep = snapshot.snapshot_table(sample_table)
    assert rep["ok"] is True
    assert rep["rows"] == 2
    assert _tee_rows(sample_table) == [("a", "1"), ("b", "2")]


def test_snapshot_is_idempotent(sample_table):
    """连跑两趟结果相同——全量替换不该产生重复行或 PK 冲突。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1')")
    snapshot.snapshot_table(sample_table)
    snapshot.snapshot_table(sample_table)
    assert _tee_rows(sample_table) == [("a", "1")]


def test_snapshot_propagates_update_and_delete(sample_table):
    """这是 SNAPSHOT lane 存在的理由：游标模型做不到的两件事。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1'), ('b','2')")
    snapshot.snapshot_table(sample_table)

    with db.get_pool().connection() as c:
        c.execute("UPDATE _snap_probe SET v='changed' WHERE k='a'")
        c.execute("DELETE FROM _snap_probe WHERE k='b'")
    snapshot.snapshot_table(sample_table)

    assert _tee_rows(sample_table) == [("a", "changed")]


def test_failed_snapshot_leaves_old_data_intact(sample_table, monkeypatch):
    """TRUNCATE + COPY 必须在同一事务：中途失败时 TEE 侧保留旧的完整快照，
    绝不出现空表窗口（读路径会读到空表 = 用户数据凭空消失）。

    ⚠️ 注入点必须落在 **dst 侧、TRUNCATE 之后、COPY 执行中途**，否则这条测试
    是假阳性。本计划初版把 `_stream_rows` 整个换成抛异常的桩——那是**源库读取
    阶段**就失败，异常从未到达 TEE 侧，`TRUNCATE` 和 `with dst.transaction():`
    根本没被执行过。2026-07-28 审查实测证实：把 `with dst.transaction():` 整个
    删掉，那一版测试**照样全绿**，对它声称守护的性质完全不敏感。

    改法：让 `_stream_rows` 正常返回一段**损坏的 COPY 二进制载荷**，使失败真实
    发生在 dst 的 `COPY FROM STDIN` 执行中途（此时 TRUNCATE 已经跑完）。这样
    一旦有人拿掉事务边界，TEE 侧就会变成空表，本测试立刻变红。
    """
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1')")
    snapshot.snapshot_table(sample_table)
    assert _tee_rows(sample_table) == [("a", "1")]

    with db.get_pool().connection() as c:
        c.execute("UPDATE _snap_probe SET v='2' WHERE k='a'")

    # 合法的 PGCOPY 签名开头 + 垃圾字节：COPY FROM 会在 TRUNCATE 之后解析失败。
    monkeypatch.setattr(
        snapshot, "_stream_rows",
        lambda *a, **kw: b"PGCOPY\n\xff\r\n\x00\x00\x00\x00\x00GARBAGE")
    rep = snapshot.snapshot_table(sample_table)
    assert rep["ok"] is False
    # 旧快照原样还在，没被 TRUNCATE 掉——证明事务回滚真的生效了。
    assert _tee_rows(sample_table) == [("a", "1")]


def test_snapshot_refuses_when_table_exceeds_max_rows(sample_table, monkeypatch):
    """超限必须失败，且**不能碰 TEE**。

    静默截断会让 TEE 缺数据而无人察觉；正确的处理是干净失败，由人把这张表改判到
    CIPHERTEXT（游标增量）或 LOGICAL（逻辑复制）lane。
    """
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO _snap_probe (k, v) VALUES ('a','1')")
    snapshot.snapshot_table(sample_table)          # 先放一份旧快照进去
    assert _tee_rows(sample_table) == [("a", "1")]

    monkeypatch.setattr(snapshot, "_row_count", lambda *a, **kw: snapshot.MAX_ROWS + 1)
    rep = snapshot.snapshot_table(sample_table)
    assert rep["ok"] is False
    assert "MAX_ROWS" in (rep["error"] or "")
    # 早退分支绝不能清源或碰 TEE：旧快照必须原样保留。
    assert _tee_rows(sample_table) == [("a", "1")]


def test_snapshot_all_continues_past_a_failing_table(monkeypatch):
    """影子期铁律：单表失败不能中断其余表，也不能上抛污染主路径。"""
    from tee_shadow import table_registry as reg

    calls = []

    def fake(table):
        calls.append(table)
        if table == "provider_health":
            return {"table": table, "rows": 0, "ok": False, "error": "boom"}
        return {"table": table, "rows": 1, "ok": True, "error": None}

    monkeypatch.setattr(snapshot, "snapshot_table", fake)
    rep = snapshot.snapshot_all()
    assert rep["failures"] == 1
    assert len(calls) == len(reg.tables_in_lane(reg.SNAPSHOT))


def test_users_is_snapshotted_before_per_user_tables():
    """FK 顺序：TEE 侧这些表带指向 users 的 CASCADE FK。users 归 MIRROR lane
    （由 reconcile 灌），所以 SNAPSHOT lane 自己不含 users——但顺序断言仍然要有，
    防止将来有人把 users 挪进 SNAPSHOT 后忘了排序。"""
    from tee_shadow import table_registry as reg

    order = snapshot.snapshot_order()
    snap = set(reg.tables_in_lane(reg.SNAPSHOT))
    assert set(order) == snap
    if "users" in snap:
        assert order.index("users") == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_tee_snapshot.py -v
```
预期：全部 FAIL / ERROR，`ModuleNotFoundError: No module named 'tee_shadow.snapshot'`。

- [ ] **Step 3: 实现 snapshot.py**

创建 `backend/tee_shadow/snapshot.py`：

```python
"""SNAPSHOT lane：明文小表的整表原子替换（RDS → TEE）。

为什么不做增量：这批表数据量极小（2026-07-27 prod 实测 25 张合计 1340 行 /
约 2 MB），但有大量 UPDATE/DELETE——队列 status 流转、心跳、allowlist、路由配置。
现有的 append-only 游标模型（tee_replicator.worker）处理可变行要靠
tee_pending_device_migration 的 requeue lane 补偿 + reconciler prune，每张表都得
配一套，成本线性于表数且永久。整表替换天然正确，且实现只有一个循环。

原子性：TRUNCATE + COPY 必须在同一事务里。中途失败时 TEE 侧保留旧的完整快照，
绝不出现"表已清空、新数据还没写完"的窗口——那个窗口里的读路径会看到用户数据
凭空消失。

影子期铁律：任何失败只 log + 计入报告，绝不上抛污染主路径。
"""
from __future__ import annotations

import io
import logging

import db
from tee_shadow import mirror
from tee_shadow import table_registry as reg

log = logging.getLogger("feedling.tee_shadow")

# 单表行数上限。SNAPSHOT lane 的前提是"表很小"；一旦某张表长过这个数，全量刷
# 的代价就不再可忽略，应该把它改判到 CIPHERTEXT（游标增量）或 LOGICAL（逻辑
# 复制）lane。超限不静默截断——那会让 TEE 悄悄缺数据；直接失败并要求人来判。
MAX_ROWS = 200_000


def snapshot_order() -> tuple[str, ...]:
    """SNAPSHOT lane 的执行顺序。

    TEE 侧这些表带指向 users 的 ON DELETE CASCADE FK，父表必须先在。users 归
    MIRROR lane（由 reconcile 灌），本 lane 通常不含它；但若将来有人把 users
    挪进来，它必须排第一。其余表之间没有互相的 FK，字典序即可（可重现）。
    """
    tables = sorted(reg.tables_in_lane(reg.SNAPSHOT))
    if "users" in tables:
        tables.remove("users")
        tables.insert(0, "users")
    return tuple(tables)


def _row_count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _stream_rows(src_conn, table: str) -> bytes:
    """把整表读成 COPY 的二进制载荷。

    单独抽成函数是为了让测试能注入"读到一半炸掉"——事务原子性是这个模块最
    关键的性质，必须有测试守着。
    """
    buf = io.BytesIO()
    with src_conn.cursor().copy(f"COPY {table} TO STDOUT (FORMAT BINARY)") as cp:
        for chunk in cp:
            buf.write(chunk)
    return buf.getvalue()


def snapshot_table(table: str) -> dict:
    """整表替换一张 SNAPSHOT lane 的表。绝不抛——失败信息落在返回值里。"""
    rep = {"table": table, "rows": 0, "ok": False, "error": None}
    try:
        with db.get_pool().connection() as src:
            n = _row_count(src, table)
            if n > MAX_ROWS:
                rep["error"] = (
                    f"row count {n} > MAX_ROWS {MAX_ROWS} — 这张表已经不适合整表"
                    f"快照，改判到 CIPHERTEXT 或 LOGICAL lane")
                log.warning("[tee-snapshot] %s: %s", table, rep["error"])
                return rep
            payload = _stream_rows(src, table)

        with mirror.get_tee_pool().connection() as dst:
            # 显式事务：TRUNCATE 与 COPY 要么一起生效、要么一起回滚。
            with dst.transaction():
                dst.execute(f"TRUNCATE {table}")
                with dst.cursor().copy(f"COPY {table} FROM STDIN (FORMAT BINARY)") as cp:
                    cp.write(payload)
        rep["rows"] = n
        rep["ok"] = True
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        rep["error"] = str(exc)[:300]
        log.warning("[tee-snapshot] %s 失败: %s", table, exc)
    return rep


def snapshot_all() -> dict:
    """刷完整条 SNAPSHOT lane。单表失败不中断其余表。"""
    tables = []
    copied = 0
    failures = 0
    for table in snapshot_order():
        rep = snapshot_table(table)
        tables.append(rep)
        if rep["ok"]:
            copied += rep["rows"]
        else:
            failures += 1
    log.info("[tee-snapshot] done: tables=%d copied=%d failures=%d",
             len(tables), copied, failures)
    return {"tables": tables, "copied": copied, "failures": failures}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_tee_snapshot.py -v
```
预期 7 条全 PASS。

- [ ] **Step 5: 跑全量 L1**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py 2>&1 | tail -5
```

- [ ] **Step 6: commit 并报告改动**

---

## Task 6: 把 SNAPSHOT 接进调度器与 admin 通道

**Files:**
- Modify: `backend/tee_shadow/table_registry.py`（新增 `PSEUDO_CIPHERTEXT_TABLES` 常量）
- Modify: `backend/admin/tee_replication.py:36`（`_ACTIONS`）、`:64`（`_validate`）、`:110`（`run_action` 分派）
- Modify: `backend/admin/tee_sync_scheduler.py`（`_CIPHERTEXT_TABLES` 改为派生；新增 snapshot 段；`_LOG_KEYS` / `_blank_summary` 补字段）
- Test: `tests/test_tee_sync_scheduler.py`（已存在，增补用例）

**Interfaces:**
- Consumes: `snapshot.snapshot_all()`（Task 5）、`table_registry.tables_in_lane()`（Task 3）
- Produces: `run_action(action="snapshot", …)`；`tee_sync_runs.report["snapshot"]`；summary 新增 `snapshot_copied` / `snapshot_failures`

- [ ] **Step 1: 写失败测试**

在 `tests/test_tee_sync_scheduler.py` 末尾追加：

```python
def test_snapshot_action_is_accepted():
    from admin import tee_replication as tr

    # 不触发真实运行——只验证校验层放行 snapshot 这个 action。
    tr._validate("snapshot", None, True)


def test_snapshot_action_rejects_unknown_table():
    from admin import tee_replication as tr

    with pytest.raises(tr.BadRequest) as e:
        tr._validate("snapshot", "not_a_real_table", True)
    assert e.value.error == "unknown_table"


def test_ciphertext_tables_are_derived_from_registry():
    """scheduler 不再手工维护密文表清单——它必须来自注册表，否则又会出现
    '加了表但某一处没登记' 的老问题。"""
    from admin import tee_sync_scheduler as sched
    from tee_shadow import table_registry as reg

    want = set(reg.tables_in_lane(reg.CIPHERTEXT)) | set(reg.PSEUDO_CIPHERTEXT_TABLES)
    assert set(sched._ciphertext_tables()) == want


def test_scheduler_covers_every_worker_table():
    """调度器必须覆盖 worker._TABLES 的**每一个 key**，包括非表名的伪 key。

    这条是上一条的反向守卫，缺了它就会重演 2026-07-28 的回归：把写死的
    _CIPHERTEXT_TABLES 改成"按 lane 派生"时，静默丢掉了 "identity" 这个伪表名
    （它实际操作 user_blobs WHERE kind='identity'，而 user_blobs 整表登记在
    MIRROR lane，因此永远不会出现在 CIPHERTEXT lane 里）。后果是整条用户身份/
    人设的同步路径消失，而上一条测试是同义反复、抓不到。
    """
    from admin import tee_sync_scheduler as sched
    from tee_replicator import worker

    missing = sorted(set(worker._TABLES) - set(sched._ciphertext_tables()))
    assert not missing, (
        f"worker 配了这些复制目标，但调度器不会去跑它们：{missing}\n"
        "若某个 key 不是 RDS 表名（伪表名），把它加进 "
        "table_registry.PSEUDO_CIPHERTEXT_TABLES。"
    )


def test_sync_tick_records_snapshot_metrics(monkeypatch):
    from admin import tee_sync_scheduler as sched

    monkeypatch.setattr(sched, "_ciphertext_tables", lambda: ())

    calls = {}

    def fake_run_action(**kw):
        calls[kw["action"]] = kw
        if kw["action"] == "snapshot":
            return {"tables": [{"table": "provider_health", "rows": 3, "ok": True}],
                    "copied": 3, "failures": 0}
        return {"tables": []}

    from admin import tee_replication as tr

    monkeypatch.setattr(tr, "run_action", fake_run_action)
    monkeypatch.setattr(sched.mirror, "probe", lambda: {"ok": True, "latency_ms": 1.0})
    monkeypatch.setattr(sched.db, "record_tee_sync_run", lambda s: None)
    monkeypatch.setattr(sched.db, "mark_reconcile_success", lambda: None)

    sched._sync_tick(do_reconcile=True)
    assert "snapshot" in calls
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_tee_sync_scheduler.py -k "snapshot or derived" -v
```
预期：`BadRequest: unknown_action` / `AttributeError: _ciphertext_tables`。

- [ ] **Step 3: 扩 admin 通道**

修改 `backend/admin/tee_replication.py`：

把 `_ACTIONS = ("reconcile", "replicate", "verify")` 改成：

```python
_ACTIONS = ("reconcile", "replicate", "verify", "snapshot")
```

在 `_validate` 里，`if action == "replicate":` 那段之后追加：

```python
    if action == "snapshot" and table is not None:
        from tee_shadow import table_registry as reg

        if table not in reg.tables_in_lane(reg.SNAPSHOT):
            raise BadRequest("unknown_table")
```

在 `run_action` 的分派链里（`elif action == "replicate":` 之后、`else:` 之前）插入：

```python
        elif action == "snapshot":
            from tee_shadow import snapshot as tee_snapshot

            if dry_run:
                # dry-run 短路，与 _run_reconcile 同款：只回计划、绝不碰 TEE。
                #
                # ⚠️ 这条分支是**安全必需**，不是锦上添花。confirm 门的条件是
                # `action != "verify" and not dry_run and confirm != "MIGRATE"`——
                # 也就是说 dry_run=True 时**根本不检查 confirm**。而 HTTP 端点
                # `POST /v1/admin/tee-replication/run`（routes_asgi.py:286）是
                # `dry_run=payload.get("dry_run", True)`，默认 True。
                # 少了这条短路，一个 `{"action": "snapshot"}` 的探测请求（admin 按
                # reconcile/replicate 的既有习惯，会以为这是安全的演练）就会直接对
                # TEE 侧 25 张表做真实 TRUNCATE+COPY，且绕过全部确认门。
                tables = list(tee_snapshot.snapshot_order()) if table is None else [table]
                report = {"planned": tables, "tables": [], "copied": 0, "failures": 0}
            else:
                report = (tee_snapshot.snapshot_all() if table is None
                          else {"tables": [tee_snapshot.snapshot_table(table)]})
```

同时把模块顶部 docstring 里列的三个入口补上 snapshot 一行。

- [ ] **Step 4: 改调度器**

修改 `backend/admin/tee_sync_scheduler.py`：

把写死的 `_CIPHERTEXT_TABLES` 元组替换成从注册表派生的函数：

```python
# 密文表 —— 经 enclave 解密成明文。**从注册表派生**，不再手工维护：手工清单正是
# "加了表但某一处没登记"的老问题的来源（2026-07-27 之前 V2 的 19 张表一处都没登记）。
#
# ⚠️ 必须并上 PSEUDO_CIPHERTEXT_TABLES。worker._TABLES 的 key 不全是 RDS 表名——
# "identity" 是个伪表名，实际操作的是 `user_blobs WHERE kind='identity'`
# （RDS 侧密文信封 → enclave 解密 → TEE 侧 user_blobs 明文行）。注册表按表名登记，
# user_blobs 整表归 MIRROR lane（reconciler 的 _SCOPE_WHERE 又特意排除 kind='identity'
# 把它让给 replicator），所以 CIPHERTEXT lane 里**不会**出现 "identity" 这个 key。
# 只按 lane 派生会静默丢掉整条用户身份/人设的同步路径（2026-07-28 Task 6 实施期发现）。
def _ciphertext_tables() -> tuple[str, ...]:
    from tee_shadow import table_registry as reg

    return tuple(sorted(
        set(reg.tables_in_lane(reg.CIPHERTEXT)) | set(reg.PSEUDO_CIPHERTEXT_TABLES)
    ))
```

同时在 `backend/tee_shadow/table_registry.py` 末尾（`synced_tables()` 之后）加这个常量：

```python
# worker._TABLES 里那些**不是 RDS 表名**的 key。
#
# "identity" 实际操作 `user_blobs WHERE kind='identity'`：RDS 侧是密文信封，经 enclave
# 解密后写进 TEE 侧 user_blobs 的明文行。它没法作为 REGISTRY 的一条登记——REGISTRY 按
# 表名索引，而 user_blobs 整表已登记为 MIRROR（reconciler 的 _SCOPE_WHERE 排除了
# kind='identity'，正是把这部分让给 replicator）。
#
# 放在这里而不是散落在调度器里，是为了让"CIPHERTEXT 通道要跑哪些东西"仍然只有一个出处。
# 它刻意不进 REGISTRY，因此不受完备性守卫约束（守卫比的是真实表名）。
PSEUDO_CIPHERTEXT_TABLES: tuple[str, ...] = ("identity",)
```

并在它下面再加一个函数，供 replicate 循环使用：

```python
def _replicable_tables() -> tuple[str, ...]:
    """本 tick 真正要 replicate 的表 = 调度清单 ∩ worker 已配置的表。

    取交集而不是直接用 _ciphertext_tables()：注册表可能已经把某张表登记为 CIPHERTEXT，
    而 tee_replicator.worker._TABLES 里还没有它的复制配置（跨 Task 的中间态，或有人
    先登记后接线）。直接调过去会让 tee_replication._validate 抛
    BadRequest("unknown_table")，被兜底 except 吞掉后计入 replicate_table_failures
    并触发指数退避（封顶 1 小时）——每个 tick 白报错一次，既污染失败率指标，又让
    "真失败"和"还没接线"在指标里混成一团。

    取了交集之后，未接线的表被静默跳过、接线后自动被吸收，不需要任何跨 Task 的部署
    时序协调（否则就得靠"某某 Task 完成前不要部署"这种人工记忆——本仓库已经有多次
    "改完忘了部署/提交"的前科）。反向守卫 test_scheduler_covers_every_worker_table
    的语义不受影响：它管的是"worker 配了的表调度器不能漏"，方向相反。
    """
    from tee_replicator import worker as tee_worker

    return tuple(t for t in _ciphertext_tables() if t in tee_worker._TABLES)
```

并把 `_sync_tick` 里 `for table in _CIPHERTEXT_TABLES:` 改成 `for table in _replicable_tables():`。

> ⚠️ **同时必须更新既有测试里对这个名字的全部引用**，否则它们会集体 `AttributeError`
> （2026-07-28 实测：漏掉这步会让 `tests/test_tee_sync_scheduler.py` 从全绿变成 6 failed）。
> 常量变成函数是个破坏性改名，`grep -rn "_CIPHERTEXT_TABLES" --include="*.py" .` 找全部引用：
>
> - `tests/test_tee_sync_scheduler.py:44` 与 `:121` — `list(sched._CIPHERTEXT_TABLES)`
>   → `list(sched._replicable_tables())`（它们断言的是 replicate 循环**实际遍历**的表，
>   所以用交集那个，不是调度清单那个）
> - `tests/test_tee_sync_metrics.py:114` 与 `:147` — `len(sched._CIPHERTEXT_TABLES)`
>   → `len(sched._replicable_tables())`；`:147` 旁边"其余 4 张各 1"的注释也要跟着改
>   （密文表已从 5 张增到 12 张）
>
> `backend/tee_shadow/verify.py` 里也有个 `_CIPHERTEXT_TABLES`，但那是 verify 自己的 dict、
> 与调度器无关，**不要动**；`backend/db.py:8955` 那处只是注释里提到它，也不要动。

> ⚠️ **还要更新 5 个既有测试对 tick 行为契约的断言**（2026-07-28 实测：漏掉会剩 5 failed）。
> 本 Task 改变了两件可观测的事：① tick 多了一个 snapshot 阶段；② 密文表从 5 张增到 12 张。
> 这些测试把两者都写死了：
>
> | 测试 | 原断言 | 改成 |
> |---|---|---|
> | `test_reconcile_runs_before_replicate_then_verify` | `["reconcile"] + ["replicate"]*5 + ["verify"]` | `["reconcile", "snapshot"] + ["replicate"]*len(sched._replicable_tables()) + ["verify"]` |
> | `test_reconcile_failure_does_not_block_replicate` | `calls.count("replicate") == 5` | `== len(sched._replicable_tables())` |
> | `test_one_table_error_does_not_stop_the_pass` | `seen == list(...)` | `seen == ["snapshot"] + list(sched._replicable_tables())` |
> | `test_already_running_aborts_replicate_phase` | `[("reconcile",None), ("replicate","chat_messages")]` | 见下方代码块（**不要写死表名**） |
> | `test_unconfigured_aborts_silently` | `["reconcile", "replicate"]` | `["reconcile", "snapshot"]` |
>
> 最后两条有**语义变化**，不是简单插一项，改的时候要理解为什么：
> - `AlreadyRunning`：snapshot 段只 log 不 return（与 replicate 循环里会 return 不同），
>   所以流程继续走到 replicate，仍在第一张表上中止。
> - `Unconfigured`：snapshot 段 `return reconcile_ok`，**replicate 根本不会被调用**——
>   中止点从"第一次 replicate"提前到了"snapshot"。测试名所表达的"Unconfigured 时静默中止"
>   这个意图不变，变的只是在哪一步中止。
>
> `test_already_running_aborts_replicate_phase` 的正确写法——**首张表名不能写死**：
>
> ```python
>     # 中止发生在 replicate 循环的第一张表上；表名随注册表内容变化，别写死。
>     # （2026-07-28：密文表从手写元组改为按字母序派生后，首张已从 chat_messages
>     #  变成 chat_message_archive。）
>     first = sched._replicable_tables()[0]
>     assert calls == [("reconcile", None), ("snapshot", None), ("replicate", first)]
> ```
>
> 另外**新增一条测试守住交集语义**（否则谁把 `_replicable_tables()` 改回
> `_ciphertext_tables()` 都不会红）：
>
> ```python
> def test_replicate_skips_tables_without_a_worker_config():
>     """replicate 只跑 worker 真正配了的表。
>
>     注册表可能先登记、worker 后接线（跨 Task 的中间态）。若直接对全部
>     CIPHERTEXT lane 调 replicate，未接线的表会让 _validate 抛 unknown_table，
>     被吞掉后计入 replicate_table_failures 并触发指数退避——每 tick 白报错，
>     还把"真失败"和"没接线"混进同一个指标。
>     """
>     from admin import tee_sync_scheduler as sched
>     from tee_replicator import worker
>
>     replicable = set(sched._replicable_tables())
>     assert replicable <= set(worker._TABLES), "replicate 目标必须都有 worker 配置"
>     # 已登记但未接线的表必须被跳过，不能出现在 replicate 目标里。
>     unwired = set(sched._ciphertext_tables()) - set(worker._TABLES)
>     assert not (replicable & unwired)
> ```
>
> 顺带把硬编码的 `5` 全部换成 `len(sched._replicable_tables())`，免得 Task 7 往
> `worker._TABLES` 里加表时这些测试又集体变红。同理，凡是断言里出现具体密文表名的地方，
> 都改成从 `sched._ciphertext_tables()` 取——这批清单以后还会变。

在 `_blank_summary` 的 dict 里补两个字段（放在 `"reconcile_skipped": 0,` 之后）：

```python
        "snapshot_copied": 0, "snapshot_failures": 0,
```

在 `_LOG_KEYS` 里补（放在 `"reconcile_skipped", "mirror_failures", "tee_healthy",` 之前）：

```python
    "snapshot_copied", "snapshot_failures",
```

**还要让这两个指标真正落库**，否则它们只出现在日志行里、永远不是 `tee_sync_runs` 的可查询列
（与 `reconcile_copied` / `replicate_copied` 等同类字段的既有约定不一致，按列做趋势查询的人
看不到 snapshot 数据）。两处：

1. `backend/db.py:737` 的 `_TEE_SYNC_RUN_COLS` 末尾补上这两个列名——`record_tee_sync_run()`
   只按这个常量取值插库，不在清单里的 summary key 会被静默丢弃。
2. 新建 RDS 侧 alembic revision（`backend/alembic/versions/`，**不是 alembic_tee**——
   `tee_sync_runs` 登记在 SKIP lane，是同步机制自己的控制面表，住在 RDS）：

```sql
ALTER TABLE tee_sync_runs ADD COLUMN IF NOT EXISTS snapshot_copied   INTEGER;
ALTER TABLE tee_sync_runs ADD COLUMN IF NOT EXISTS snapshot_failures INTEGER;
```

revision 的 `down_revision` 要指向当时 RDS 链的 head（用
`python -c "import sys; sys.path.insert(0,'backend'); from alembic.config import Config; from alembic.script import ScriptDirectory; c=Config('backend/alembic.ini'); c.set_main_option('script_location','backend/alembic'); print(ScriptDirectory.from_config(c).get_heads())"`
现查，**不要照抄本文里的任何 revision 号**）。

在 `_sync_tick` 里，**(1) reconcile 之后、(2) replicate 之前**插入 snapshot 段。位置是刻意的：snapshot 表带指向 `users` 的 FK，必须在 reconcile 灌完父表之后；而它比 replicate 便宜得多（无 enclave 往返），放前面能让它不被慢的密文复制饿死。

> **这个顺序是一条真实风险的缓解措施，不要随意调整（2026-07-28 Task 5 审查发现）**：
> SNAPSHOT lane 的 25 张表里有 21 张带指向 `users` 的 FK，而 `users` 归 MIRROR lane、
> 由独立的异步路径灌入 TEE。若某新用户的 `users` 行尚未镜像过去，而 ta 在某张 SNAPSHOT 表
> （如 `agent_jobs`）里已有行，该表整个 `COPY FROM STDIN` 会因 FK 违反而回滚——
> **爆炸半径是这张表全体用户的快照，不只是那一个新用户**。
>
> 把 snapshot 排在 reconcile（灌 `users` 的那一步）之后，绝大多数情况下父行已就位。
> 残余窗口是"reconcile 跑完到 snapshot 跑完之间新注册的用户"，代价是该表这一轮快照失败、
> 下一轮自动追平（全量刷天然幂等，无需断点续传），属可接受的最终一致退化。
>
> 排查提示：某张 SNAPSHOT 表长期追不平时，第一个要查的就是它有没有 FK 违反、`users` 是否落后。

```python
    # (1.5) snapshot 明文小表 —— 在 reconcile 之后（FK 父表已在）、replicate 之前
    # （不被慢的密文复制饿死；snapshot 无 enclave 往返，是整个 tick 里最便宜的一段）。
    try:
        rep = tr.run_action(action="snapshot", dry_run=False, confirm="MIGRATE")
        summary["snapshot_copied"] = rep.get("copied") or 0
        summary["snapshot_failures"] = rep.get("failures") or 0
        summary["report"]["snapshot"] = rep.get("tables") or []
        if summary["snapshot_failures"]:
            failed = [t.get("table") for t in (rep.get("tables") or [])
                      if isinstance(t, dict) and not t.get("ok")]
            log.warning("[tee-sync] snapshot 有 %d 张表失败: %s",
                        summary["snapshot_failures"], failed)
        else:
            log.info("[tee-sync] snapshot done: copied=%s", summary["snapshot_copied"])
    except tr.AlreadyRunning:
        log.info("[tee-sync] 手动 run 持锁中 — 跳过本 tick 的 snapshot")
    except tr.Unconfigured:
        return reconcile_ok
    except Exception as e:  # noqa: BLE001 — 影子期铁律：绝不传染主路径
        log.warning("[tee-sync] snapshot 失败: %s", e)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_tee_sync_scheduler.py -v
```

- [ ] **Step 6: 跑全量 L1 + pyflakes**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py 2>&1 | tail -5
python -m pyflakes backend/tee_shadow backend/admin
```
pyflakes 全仓恒剩 1 条 unused 是预期（见 memory `autoflake-kills-module-attr-reexports`）。

- [ ] **Step 7: commit 并报告改动**

---

## Task 7: CIPHERTEXT lane 扩展 + 解密探针 gate

7 张密文表并入 replicator。**这个 Task 有一个硬 gate：探针不过就停下来找用户，不要自行改判 lane。**

> ### ⚠️ 执行顺序：探针必须先做（2026-07-28 修订）
>
> 下面的 Step 编号是按"实现→验证"写的，但**实际执行顺序要反过来**：
> **先做 Step 6（写探针脚本）+ Step 7（跑 gate），过了再回头做 Step 1-5 的接线。**
>
> **一个必须先行的例外**：探针会调用 `transforms.plaintext_envelope_column`（Step 3 的产出）。
> 所以顺序是 **Step 3 的那个纯函数 → Step 6 探针 → Step 7 gate → Step 1/2/4/5 接线**。
> 先落地那个函数是安全的：它是无副作用的纯函数、此刻还没有任何调用者，加进去不改变
> 任何现有行为。（2026-07-28 实施期发现——重排 Step 顺序时漏看了这条依赖。）
>
> 理由：探针是唯一能推翻整个 lane 归类的 gate。`v2_trajectory_events` 的
> `enclave_pk_fpr` 实测**全部为空**（prod 567/567、test 124/124）——没有任何字段记录它是用
> 哪把 enclave 钥封的，解不解得开只能实跑。若解不开，这张表（乃至其它几张）就要回退成
> "密文原样搬"，届时 Step 1-5 写的 transform 与 `_TABLES` 配置全部作废。先花十几分钟跑探针，
> 好过先写几百行注定要删的接线代码。
>
> ### 这 7 张表的真实结构（实测，**不要照抄下文示例**）
>
> | 表 | PK | 信封列 | 备注 |
> |---|---|---|---|
> | `chat_message_archive` | `(user_id, source_seq)` | `doc` | 有 `msg_id`/`ts`，与 `chat_messages` 同形，可复用 `plaintext_chat_doc` + `_chat_unpack` |
> | `v2_trajectory_events` | `(job_id, event_index)` | `payload_envelope` | **PK 不含 user_id**（表里有该列）；无单一 id 列 |
> | `model_api_credentials` | `(id uuid)` | `api_key_envelope` | **无单一 doc 列**，另有 provider/label/base_url 等业务列 |
> | `v2_conversation_summary` | `(user_id)` | `summary_envelope` | 每用户一行 |
> | `v2_conversation_summary_segments` | `(segment_id)` | `summary_envelope` | `segment_id` 是 **IDENTITY ALWAYS**，upsert 必须带 `OVERRIDING SYSTEM VALUE` |
> | `v2_trajectory_reviews` | `(source_job_id)` | `review_envelope` | 信封可为 NULL |
> | `v2_workspace_entries` | `(user_id, path)` | `content_envelope` | |
>
> ### ⚠️⚠️ 冷启动游标必须给类型安全的下界（2026-07-28 审查发现，7 张表曾 100% 必崩）
>
> `_read_cursor()` 对**从未跑过的表**返回 `(0.0, "")`，而 `_decode_cursor()` 的
> `text`/`single` 分支会把那个**空字符串原样**当 SQL 参数塞进
> `WHERE (sort_col, id_col) > (%s, %s)`。
>
> 这对既有 5 张表恰好是安全的——它们的排序列/id 列在 RDS 里都是 **TEXT**，空串是合法的
> 文本下界。但本次新增的 7 张表排序列是 **TIMESTAMPTZ**（`created_at`/`cleared_at`/
> `updated_at`）或 **BIGINT**，空串无法 cast，**第一次执行 select_sql 就抛
> `InvalidDatetimeFormat` / `InvalidTextRepresentation`**。
>
> 实测（用真实 `_TABLES` + `_decode_cursor` 对 test RDS 跑冷启动游标）：新增 7 张
> **全部 FAIL**，既有 5 张**全部 OK**。后果不是"漏几行"，是**一行都进不去**；而且
> scheduler 的 per-table try/except 会把它吞掉并计入退避，于是每个 tick 永远失败、
> 永远 0 行同步，没有任何人会发现。
>
> **为什么之前没抓到**：所有验证（包括实施者的实库链路验证和我跑的 45 passed）都假设
> "游标已存在"，没有一条覆盖 `(0.0, "")` 这个新表必经的初始态；新增的单元测试也没有
> 一条真正调用 `worker.run_table()`。这是整片验证盲区，不是某个人的疏忽。
>
> **修法**：给 `_Table` 加一个字段，让每张表显式声明自己"从头开始"时的游标参数：
>
> ```python
>     # 冷启动（tee_replication_cursors 里还没有本表的行）时喂给 select_sql 的参数元组。
>     # None = 沿用通用逻辑（既有表：排序列是 TEXT，空串就是合法下界）。
>     # 排序列是 TIMESTAMPTZ / BIGINT 的表**必须**显式给，否则空串 cast 失败、整表永不同步。
>     cursor_zero: tuple | None = None
> ```
>
> `_decode_cursor` 开头加一句短路：
>
> ```python
>     if not wm_id and cfg.cursor_zero is not None:
>         return cfg.cursor_zero
> ```
>
> 7 张新表的取值（已实测可用）：TIMESTAMPTZ 排序列用 `("-infinity", "")`，
> `v2_conversation_summary_segments` 用 `("0",)`。实测 `select … where (created_at, job_id)
> > ('-infinity', 0)` 返回全部 124 行、`where segment_id > '0'` 返回全部 6 行。
>
> **并且必须补一条真正跑 `run_table()` 冷启动的测试**——这个 bug 能活下来，正是因为没有
> 任何测试走过那条路径。
>
> ### ⚠️ `v2_conversation_summary_segments` 不要用裸序列值当游标（同次审查发现）
>
> 用 `segment_id`（`GENERATED ALWAYS AS IDENTITY`）本身排序有经典竞态：`nextval()` 的
> **取号顺序不等于事务提交顺序**。若拿到 `segment_id=5` 的事务比拿到 `6` 的晚提交，而扫描
> 恰好落在两次提交之间，游标会先推过 6，等 5 提交时已不满足 `segment_id > 6` → **永久跳过、
> 不报错、不冻结游标**。
>
> 本仓库其实已经避过这个坑一次：`chat_messages.seq` 同样是全局 IDENTITY，但 worker 明确用
> `(ts, msg_id)` 而非 `seq` 做游标（`seq` 只用于事后 setval 对齐）。该表写路径是 per-user
> 并发的多处 INSERT，具备触发条件。
>
> **改成 `(created_at, segment_id)` 复合游标**（`cursor_kind` 从 `single` 改为 `text`），
> 与其余 6 张新表统一。该表确有 `created_at` 列（已核实）。
>
> **replicator 的 `_Table` 契约是 `(user_id, item_id, sort_val, doc)` 四元组 + `(sort, item_id)`
> 两列游标**，而上面几张表要么 PK 不含 `user_id`、要么是复合 PK、要么没有单一 doc 列。
> 套进契约的既有手法是 **`chat_messages` 的 `_SEQ_KEY` 走私**（见 `worker.py:135-164`）：
> 把塞不进四元组的额外列作为保留键放进 doc dict，`transform` 会原样透传（它只剥
> `_ENVELOPE_KEYS`），`upsert_args` 再 `pop` 出来拼进 SQL。每张表都要按自己的形状设计
> `unpack`/`upsert_args`，**没有通用模板**。

**Files:**
- Modify: `backend/tee_replicator/transforms.py`（新增通用单信封 transform）
- Modify: `backend/tee_replicator/worker.py:167`（`_TABLES` 增补 7 条）
- Create: `scripts/tee/decrypt_probe.py`
- Test: `tests/test_tee_replicator_worker.py`（增补）

**Interfaces:**
- Consumes: `transforms.plaintext_chat_doc`（已有）、`worker._Table`（已有）
- Produces: `transforms.plaintext_envelope_column(doc, decrypt, *, purpose)` — 通用单信封列解密；`worker._TABLES` 新增 7 个 key

- [ ] **Step 1: 写失败测试**

在 `tests/test_tee_replicator_worker.py` 末尾追加：

```python
def test_every_ciphertext_lane_table_has_a_worker_config():
    """注册表登记了 CIPHERTEXT，worker 就必须有对应配置——否则 scheduler 会拿着
    一个 worker 不认识的表名调 run_action，每 tick 报 unknown_table。"""
    from tee_replicator import worker
    from tee_shadow import table_registry as reg

    missing = sorted(set(reg.tables_in_lane(reg.CIPHERTEXT)) - set(worker._TABLES))
    assert not missing, f"CIPHERTEXT lane 的这些表在 worker._TABLES 里没有配置：{missing}"


def test_plaintext_envelope_column_strips_crypto_keys():
    from tee_replicator import transforms

    env = {
        "v": 1, "id": "e1", "owner_user_id": "usr_x", "visibility": "shared",
        "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "event_kind": "tool_call_started",
    }
    out = transforms.plaintext_envelope_column(
        env, lambda e, purpose: b"decrypted", purpose="tee_replicate:probe")
    # 加密学字段一个都不许残留。
    for k in ("body_ct", "nonce", "K_user", "K_enclave", "v"):
        assert k not in out
    # 语义字段原样保留。
    assert out["event_kind"] == "tool_call_started"
    assert out["body"] == "decrypted"


def test_plaintext_envelope_column_rejects_local_only():
    from tee_replicator import transforms

    env = {"id": "e1", "visibility": "local_only", "body_ct": "ct", "nonce": "n",
           "K_user": "ku", "owner_user_id": "usr_x"}
    with pytest.raises(transforms.PendingDeviceMigration):
        transforms.plaintext_envelope_column(
            env, lambda e, purpose: b"x", purpose="tee_replicate:probe")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_tee_replicator_worker.py -k "ciphertext_lane or envelope_column" -v
```

- [ ] **Step 3: 实现通用 transform**

在 `backend/tee_replicator/transforms.py` 末尾追加：

```python
def plaintext_envelope_column(env: dict, decrypt, *, purpose: str) -> dict:
    """通用单信封列 → 明文 dict。

    与 plaintext_memory_doc / plaintext_world_book_doc 同型，区别只在 purpose 由
    调用方给（这些表的信封住在专用列里而不是通用 doc 列，purpose 串要能区分）。
    2026-07-27 全量对齐新增的 6 张 v2/archive 表用它；chat_message_archive 不用
    ——它的 doc 与 chat_messages.doc 完全同形，直接复用 plaintext_chat_doc（含
    thinking/caption 子信封处理）。
    """
    if not _decryptable(env):
        raise PendingDeviceMigration(str(env.get("id", "")))
    out = _strip_envelope(env)
    out["body"] = _decrypt_body(decrypt, env, purpose)
    return _scrub_nul(out)
```

- [ ] **Step 4: 增补 worker._TABLES**

在 `backend/tee_replicator/worker.py` 的 `_TABLES` dict 里追加 7 条。以 `v2_trajectory_events` 为例（其余 6 条同型，按各自的 PK / 排序列 / 信封列名调整）：

```python
    # ---- 2026-07-27 全量对齐新增的 7 张密文表 ---------------------------- #
    # 排序列的选取原则与既有表一致：(排序列, 主键) 必须构成全序，否则游标会漏行。
    "v2_trajectory_events": _Table(
        select_sql=("SELECT user_id, event_id, created_at, payload_envelope "
                    "FROM v2_trajectory_events WHERE (created_at, event_id) > (%s, %s) "
                    "ORDER BY created_at, event_id LIMIT %s"),
        cursor_kind="text",
        transform=lambda doc, decrypt: transforms.plaintext_envelope_column(
            doc, decrypt, purpose=f"tee_replicate:v2_trajectory:{doc.get('id', '')}"),
        upsert_sql=("INSERT INTO v2_trajectory_events "
                    "(user_id, event_id, created_at, payload_envelope) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, event_id) DO UPDATE SET "
                    "created_at=EXCLUDED.created_at, payload_envelope=EXCLUDED.payload_envelope"),
        unpack=lambda r: (r[0], r[1], r[2], r[3]),
        upsert_args=lambda uid, iid, sort, doc: (uid, iid, sort or "", Jsonb(doc)),
        requeue_fetch_sql=("SELECT user_id, event_id, created_at, payload_envelope "
                           "FROM v2_trajectory_events WHERE user_id = %s AND event_id = %s"),
        requeue_delete_tee_sql=("DELETE FROM v2_trajectory_events "
                                "WHERE user_id = %s AND event_id = %s"),
    ),
```

> ⚠️ **`v2_conversation_summary_segments` 有 IDENTITY 陷阱（2026-07-28 Task 4 实施期发现）**：
> 它的 `segment_id` 在 RDS 侧是 `GENERATED ALWAYS AS IDENTITY`（实测 `is_identity=YES,
> identity_generation=ALWAYS`），`alembic_tee 0004` 已如实照搬为 ALWAYS。PostgreSQL **禁止**
> 对 ALWAYS 列显式赋值，除非 INSERT 带 `OVERRIDING SYSTEM VALUE`。所以这张表的 `upsert_sql`
> 必须写成 `INSERT INTO ... OVERRIDING SYSTEM VALUE VALUES (...)`，否则每行都会硬报错。
>
> 参照现成的 `chat_messages` 配置（`worker.py:185`）——它正是这么做的，且注释解释了另一半：
> `ON CONFLICT DO UPDATE` 分支**绝不能**再赋值该列（`OVERRIDING SYSTEM VALUE` 在 UPDATE 语境
> 没有等价物，写了就是 SQL 硬错误）；冲突意味着该行首次插入时 id 已正确，保留即可。
>
> 其余带自增列的表（`agent_jobs` / `v2_turn_metrics` / `agent_action_queue` /
> `agent_status_events` / `v2_capture_batches` / `v2_sandbox_usage_events` /
> `v2_trajectory_access_audit`）在 RDS 侧是 `DEFAULT nextval(...)` 而非真 IDENTITY，
> 0004 建成了 `GENERATED BY DEFAULT AS IDENTITY`，**允许显式插值、不需要 OVERRIDING**。
> 它们都在 SNAPSHOT lane，走 `COPY FROM`，不受影响。

**每条的列名/PK 必须先从实库查准，不要照抄上面的示例**：

```bash
url=$(grep -m1 '^PROD_DATABASE_URL=' .env | cut -d= -f2-)
for t in chat_message_archive v2_trajectory_events model_api_credentials \
         v2_conversation_summary v2_conversation_summary_segments \
         v2_trajectory_reviews v2_workspace_entries; do
  echo "=== $t ==="
  psql "$url" -tA -c "select string_agg(column_name||':'||data_type,', ' order by ordinal_position) from information_schema.columns where table_name='$t';"
  psql "$url" -tA -c "select a.attname from pg_index i join pg_attribute a on a.attrelid=i.indrelid and a.attnum=any(i.indkey) where i.indrelid='$t'::regclass and i.indisprimary order by array_position(i.indkey, a.attnum);" | paste -sd, -
done
```

`chat_message_archive` 用 `transform=transforms.plaintext_chat_doc` 和 `unpack=_chat_unpack`（复用 chat 那套，含 R2 offload 水合——prod 有 1 行是 file pointer）。

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_tee_replicator_worker.py -v
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py 2>&1 | tail -5
```

- [ ] **Step 6: 写解密探针脚本**

创建 `scripts/tee/decrypt_probe.py`：

```python
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
            # ⚠️ 不能写成 `except transforms.PermanentDecryptFailure`——那个异常
            # **transforms 自己从不抛**。把"裸 403 decrypt_failed"重分类成它的逻辑长在
            # worker._transform_with_retry（worker.py:366/386），而探针刻意不走那一层
            # （它需要 _TABLES 里的 _Table 配置，而这 7 张表在探针阶段还没接线）。
            # 所以这里直接复用 worker 的判定函数，否则「enclave 确定性拒解」会和网络抖动
            # 一起落进 other:*——而这两类的处置完全相反：前者是毒行、要隔离并从 lane 里
            # 剔除，后者重试就好。混在一桶里，gate 的结论就没有意义了。
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
```

**关于 `_get_decrypt`**：它在 `backend/tee_replicator/worker.py:310`，签名是 `_get_decrypt(user_id: str, *, fresh: bool = False) -> Callable[[dict, str], bytes]`，内部带 per-user 缓存并复用 enclave token。探针刻意用它而不是自建通道——只有走同一条路径，探针的成功率才能代表真实 replicate 的成功率。

探针**不需要** `fresh=True`：那是 replicate 在解密失败后换 token 重试用的（`worker.py:369`）。探针要量的是"首次尝试就能解开的比例"，换 token 重试会掩盖真实成败分布。

- [ ] **Step 7: 在 test 上跑探针（硬 gate）**

```bash
python scripts/tee/decrypt_probe.py \
  --dsn "$(grep -m1 '^TEST_DATABASE_URL=' .env | cut -d= -f2-)" --limit 1000
```

**gate 判据：7 张表全部 PASS（100%）才能继续。**

任一张不是 100%：**停下来向用户报告**，报告内容为该表的成败分布（`pending_device` / `permanent_fail` / 其他异常各多少）以及建议——回退选项是把该表改判为"密文原样搬"（TEE 建同形 jsonb 列不解密，注册表理由改写）。**不要自行改判。**

- [ ] **Step 8: 在 test 跑一轮真实复制**

```bash
# 逐表跑，先 dry_run 看计划，再实跑
for t in chat_message_archive v2_trajectory_events model_api_credentials \
         v2_conversation_summary v2_conversation_summary_segments \
         v2_trajectory_reviews v2_workspace_entries; do
  echo "=== $t ==="
  ftee test "select count(*) from $t;"
done
```

对照 test RDS 侧同名表行数，应逐张相等。

- [ ] **Step 9: commit 并报告改动**

报告必须包含探针的完整输出（7 张表各自的成功率）。

---

## Task 8: verify 扩范围 + 端到端验收 + prod 落地

没有这一步，新增的 34 张表不进 `verify`，就会出现上游 plan 点名的"**全绿假象**"——`tee_sync_runs.verify_ok=true` 但那 34 张表压根没被核对过。

> **⚠️ 2026-07-28 开工前实测修订（controller）**：本 Task 的原文低估了缺口，实测
> `covered = reconciler.TABLES ∪ verify._CIPHERTEXT_TABLES ∪ SNAPSHOT` 相对
> `registry.synced_tables()`（51 张）**缺 9 张**，不是原文假设的"只差 SNAPSHOT 25 张"：
>
> 1. **7 张 Task 7 新接入的 CIPHERTEXT 表**（`chat_message_archive` /
>    `model_api_credentials` / `v2_conversation_summary` /
>    `v2_conversation_summary_segments` / `v2_trajectory_events` /
>    `v2_trajectory_reviews` / `v2_workspace_entries`）——它们进了
>    `worker._TABLES`，但 `verify._CIPHERTEXT_TABLES` 仍是老的 5 条。
> 2. **2 张登记成 MIRROR 但根本没有同步路径的表**（`agent_runtime_instances` /
>    `agent_runtime_supervisor_heartbeats`）：MIRROR lane 的含义是"`db.py` 热路径双写
>    + `reconciler.TABLES` 兜底"，实测这两张表**两边都没有**——`db.py:335` 与
>    `agent_runtime/leases.py:51` 只写 RDS，`reconciler.TABLES` 里也没有它们。
>    0004 把表建出来了，但没有任何东西往里灌数据。
>
> **修订决定（controller 判断，非用户新决策）**：这两张表改判 **SNAPSHOT lane**。
> 用户当初的决定是"这两张表要在 TEE 里有数据"，lane 标签是我在以为存在双写路径时
> 贴的；实际要补 MIRROR 就得给 `leases.py` 的 8 处租约热路径写点逐个挂双写，代价和
> 风险都远高于每 tick 整表刷一遍（prod 实测 230 行 + 1 行，且 **0 孤儿**，SNAPSHOT
> 的 FK 前提满足）。SNAPSHOT 同样满足"TEE 里有数据且持续跟进"的原始意图。

**Files:**
- Modify: `backend/tee_shadow/verify.py`（`covered_tables()` + SNAPSHOT lane + 7 张新密文表）
- Modify: `backend/tee_shadow/table_registry.py`（2 张 supervisor 表 MIRROR → SNAPSHOT）
- Test: `tests/test_tee_verify.py`（增补）

**Interfaces:**
- Consumes: `table_registry.tables_in_lane(SNAPSHOT)`、`verify._ciphertext_table_report`（已有）
- Produces: `verify.covered_tables()`；`verify.run()` 的 `tables` 字典多出 34 个 key

- [ ] **Step 1: 写失败测试**

在 `tests/test_tee_verify.py` 末尾追加：

```python
def test_verify_covers_every_synced_table():
    """verify 范围必须随注册表扩展——否则新增表会产生'全绿假象'：
    verify_ok=true 但那些表压根没被核对过。这是上游 plan 的 Phase 1 出口 gate。"""
    from tee_shadow import table_registry as reg
    from tee_shadow import verify

    covered = set(verify.covered_tables())
    missing = sorted(set(reg.synced_tables()) - covered)
    assert not missing, f"这些表进了 TEE 但 verify 不核对它们：{missing}"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_tee_verify.py -k covers_every -v
```
预期 `AttributeError: module 'tee_shadow.verify' has no attribute 'covered_tables'`。

- [ ] **Step 3a: 两张 supervisor 表改判 SNAPSHOT**

`backend/tee_shadow/table_registry.py`，把这两条从 `MIRROR` 改成 `SNAPSHOT`，理由写进
`reason`（注册表的 `reason` 是给未来的人看的，不能只写 lane 名）：

```python
    "agent_runtime_instances": Entry(
        SNAPSHOT,
        "V1 runtime 实例登记（租约/状态高频原地 UPDATE）。不走 MIRROR：leases.py 有 8 处"
        "租约热路径写点，逐个挂双写的代价和风险都高于整表刷；prod 实测 230 行 0 孤儿"),
    "agent_runtime_supervisor_heartbeats": Entry(
        SNAPSHOT,
        "V1 supervisor 心跳（单行，每次心跳原地 UPDATE），同上走整表刷"),
```

- [ ] **Step 3b: verify 覆盖 7 张新密文表 + 25+2 张 SNAPSHOT 表**

`backend/tee_shadow/verify.py`。

先给 `_CIPHERTEXT_TABLES` 的注释块补一条 key 说明，然后追加 7 条配置。**`kind=None`
表示不做内容抽样**——`_sample_ciphertext_content` 写死了读 `doc` 列，而这 7 张表的信封
列各不相同（`payload_envelope` / `api_key_envelope` / `summary_envelope` /
`review_envelope` / `content_envelope`），要抽样得先把列名参数化，那是独立一档工作；
本 Task 只做行数核算（`rds == tee + pending`），已经足以抓住"整张表没被同步"：

```python
    # kind=None：只核行数不做内容抽样，见下方 run() 的分支注释。
    # strict=False：行数不进硬 gate，见 _rows_ok_advisory 的注释。
    "chat_message_archive": dict(
        rds_table="chat_message_archive", tee_table="chat_message_archive",
        item_col="msg_id", pending_table="chat_message_archive",
        kind=None, strict=False),
    "model_api_credentials": dict(
        rds_table="model_api_credentials", tee_table="model_api_credentials",
        item_col="id", pending_table="model_api_credentials",
        kind=None, strict=False),
    "v2_conversation_summary": dict(
        rds_table="v2_conversation_summary", tee_table="v2_conversation_summary",
        item_col="user_id", pending_table="v2_conversation_summary",
        kind=None, strict=False, pending_by_user_only=True),
    "v2_conversation_summary_segments": dict(
        rds_table="v2_conversation_summary_segments",
        tee_table="v2_conversation_summary_segments",
        item_col="segment_id", pending_table="v2_conversation_summary_segments",
        kind=None, strict=False),
    "v2_trajectory_events": dict(
        rds_table="v2_trajectory_events", tee_table="v2_trajectory_events",
        item_col="job_id", pending_table="v2_trajectory_events",
        kind=None, strict=False),
    "v2_trajectory_reviews": dict(
        rds_table="v2_trajectory_reviews", tee_table="v2_trajectory_reviews",
        item_col="source_job_id", pending_table="v2_trajectory_reviews",
        kind=None, strict=False),
    "v2_workspace_entries": dict(
        rds_table="v2_workspace_entries", tee_table="v2_workspace_entries",
        item_col="path", pending_table="v2_workspace_entries",
        kind=None, strict=False),
```

`v2_conversation_summary` 的 `pending_by_user_only=True` 与 Task 9 给它配的
`requeue_by_user_only=True` 是同一回事（一行/用户，item_id 就是 user_id）。

在 `run()` 之前加两个函数：

```python
def _rows_ok_advisory(rds_rows: int, tee_rows: int) -> bool:
    """新增 lane 的行数硬判据：只抓"这张表从没被同步过 / 一直在失败"。

    不能用 rds_rows == tee_rows。SNAPSHOT 是 tick 级整表替换、CIPHERTEXT 是游标
    增量，两者都在"上一趟同步的瞬间"与 RDS 一致，而 RDS 一直在变——
    agent_action_queue / v2_turn_metrics / v2_trajectory_events 这类表每分钟都在写。
    严格相等会让 verify_ok 永久为 false，把 gate 变成没人再看的红灯，这跟
    "全绿假象"是同一个病的两面。真正的"这趟同步失败了"由
    tee_sync_runs.snapshot_failures / replicate 的错误计数负责，不该由 verify 兜。

    保留严格结果供人看：调用方把它写进报告的 strict_rows_ok 字段。
    """
    return not (rds_rows > 0 and tee_rows == 0)


def covered_tables() -> tuple[str, ...]:
    """verify 实际会核对的表名集合。

    抽成函数是为了让守卫测试能断言它覆盖了注册表里所有非 SKIP 的表——verify
    漏掉一张表不会让任何东西变红，只会让那张表悄悄不被核对（"全绿假象"）。
    """
    from tee_shadow import table_registry as reg

    return tuple(sorted(
        set(reconciler.TABLES)
        | set(_CIPHERTEXT_TABLES)
        | set(reg.tables_in_lane(reg.SNAPSHOT))
    ))
```

在 `run()` 里，`for table in reconciler.TABLES:` 那个循环之后、密文表循环之前，插入
SNAPSHOT lane 的行数核对：

```python
    # SNAPSHOT lane：整表替换，只核行数（字段抽样对全量替换没有增量信息——
    # 两侧要么整表一致，要么上一趟 snapshot 整个失败了）。
    from tee_shadow import table_registry as reg

    for table in reg.tables_in_lane(reg.SNAPSHOT):
        with db.get_pool().connection() as src, mirror.get_tee_pool().connection() as dst:
            rds_n = src.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            tee_n = dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        tables[table] = {
            "rds_rows": rds_n, "tee_rows": tee_n, "row_drift": rds_n - tee_n,
            "user_diffs": {},
            "strict_rows_ok": rds_n == tee_n,
            "rows_ok": _rows_ok_advisory(rds_n, tee_n),
        }
```

再改密文表循环，让 `kind=None` 的表跳过抽样、`strict=False` 的表走 advisory 判据。
把现有的 `if cfg["kind"] == "frames": ... else: ...` 改成：

```python
        if not cfg.get("strict", True):
            table_report["strict_rows_ok"] = table_report["rows_ok"]
            table_report["rows_ok"] = _rows_ok_advisory(
                table_report["rds_rows"], table_report["tee_rows"])
        tables[key] = table_report
        if cfg["kind"] is None:
            # Task 7 新接入的 7 张表：信封列名各不相同，_sample_ciphertext_content
            # 写死读 doc，参数化是独立一档工作。行数核算已能抓住整表失联。
            continue
        if cfg["kind"] == "frames":
            ...
```

⚠️ 注意 `tables[key] = table_report` 与 `table_report["requeue_backlog"] = ...` 的既有
顺序：advisory 覆写必须发生在 `tables[key] = table_report` **之前**（dict 是引用，
之后改也生效，但读代码的人会以为报告已经定稿——按上面的顺序写）。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_tee_verify.py tests/test_tee_table_registry.py tests/test_tee_snapshot.py -v
```

顺带确认 `snapshot_order()` 自动带上了新改判的两张表（它从注册表派生，无需改代码）：

```bash
python -c "import sys; sys.path.insert(0,'backend'); from tee_shadow import snapshot; \
o=snapshot.snapshot_order(); print(len(o), 'agent_runtime_instances' in o, \
'agent_runtime_supervisor_heartbeats' in o)"
```
预期 `27 True True`。

- [ ] **Step 5: 跑全量 L1**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py 2>&1 | tail -5
python -m pyflakes backend/tee_shadow backend/admin backend/tee_replicator
```
预期**零失败**。

> **⚠️ Step 6-7 由 controller 亲自执行，implementer 不做**：这两步是部署 + 生产操作
> （push 分支触发 CVM 部署、跑 prod 解密探针、读写 prod 库），implementer 只做到
> Step 5 + Step 8 的文档 + commit。另外 **Step 7.1 的 prod alembic_tee 迁移已经在
> 2026-07-27 由 controller 在本地跑完了**（两个 TEE 实库都已 0001→0004、各 54 张表），
> 不要重复 dispatch workflow。

- [ ] **Step 6: test 端到端验收**（controller）

部署到 test（push `test` 分支触发 `deploy-test-cvm`），等一个完整 tick 后：

```bash
url=$(grep -m1 '^TEST_DATABASE_URL=' .env | cut -d= -f2-)
psql "$url" -tA -F'|' -c "select ran_at, snapshot_copied, snapshot_failures, verify_ok, unconverged_tables from tee_sync_runs order by ran_at desc limit 3;"
```

验收判据（全部要满足）：
- `snapshot_failures = 0`
- `verify_ok = t`（含新增的 advisory 判据：任何一张表 RDS 有行而 TEE 空 → false）
- `unconverged_tables = 0`
- TEE test 库表数 = 54（实测已对齐值，非 52）

```bash
ftee test "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE';"
```

- [ ] **Step 7: prod 落地**（controller）

顺序不能反：

1. ~~dispatch `TEE migrate`，`environment=prod`~~ —— **已于 2026-07-27 本地跑完**，
   prod TEE 库已是 0004 head / 54 张表。此步跳过。
2. 在 prod 跑解密探针（**先只读探，不回填**）：
   ```bash
   python scripts/tee/decrypt_probe.py \
     --dsn "$(grep -m1 '^PROD_DATABASE_URL=' .env | cut -d= -f2-)" --limit 2000
   ```
   prod 的量更大（合计 1849 行密文），毒行概率高于 test。**不是 100% 就停下来报告用户。**
3. 部署 backend 到 prod（走正常发布流程），让 scheduler 自己跑第一趟 snapshot + replicate。
4. 核对：
   ```bash
   psql "$(grep -m1 '^PROD_DATABASE_URL=' .env | cut -d= -f2-)" -tA -F'|' \
     -c "select ran_at, snapshot_copied, snapshot_failures, verify_ok, unconverged_tables, requeue_backlog from tee_sync_runs order by ran_at desc limit 3;"
   ```

- [ ] **Step 8: 更新文档**

- `docs/CHANGELOG.md` 顶部加一条 `[DONE]`，记录：45 张表的归类结果、新增的注册表机制与 CI 守卫、alembic_tee 落地通道、探针结果。
- `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`：补 §"迁移落地通道"一节，写明 `TEE migrate` workflow 的用法与 head 断言。
- 上游 plan `2026-07-23-tee-promotion-decrypt-removal.md`：勾掉 Task 0.6 的两个 checkbox，并在 Phase 1 的 v2 表迁移策略处注明"由 2026-07-27-tee-full-table-alignment 完成"。

公开文档（`docs-site/`）与 OpenAPI **不涉及**：本改动不碰公开 API 契约、不改架构对外叙事、不改信任边界（TEE 库的定位与可见性没变）。

- [ ] **Step 9: commit 并报告改动**

---

## Task 9: CIPHERTEXT 可变表补 requeue 双写

**2026-07-28 新增**（Task 7 实施期发现，用户拍板单独立 Task）。

CIPHERTEXT lane 是 **append-only 游标**模型，游标永不回头。行被**原地 UPDATE** 后，必须靠
`mirror.mark_pending(user_id, table, item_id, "requeue")` 打标记，replicator 下一趟才会
按 PK 重新拉取（见 `worker._consume_requeue`）。没有这个标记，TEE 侧就**永久停在首次复制
时的状态**。

实测这 4 张表有 UPDATE 写路径但**零 requeue 双写**：

| 表 | UPDATE 写点 | prod 行数 | 后果 |
|---|---|---|---|
| `model_api_credentials` | `db.py:8571` | **365** | **最严重**：BYOK 凭证轮换后 TEE 里仍是旧 key |
| `v2_conversation_summary` | `jobs_store.py:3843, 4113, 4201, 4405` | 4 | 对话摘要更新不同步 |
| `v2_trajectory_reviews` | `db.py:7998`；`jobs_store.py:285, 4796, 5133, 5223` | 0 | 审阅完成态永不进 TEE |
| `v2_workspace_entries` | `jobs_store.py:4566` | 0 | 工作区内容更新不同步 |

另 3 张（`chat_message_archive` / `v2_trajectory_events` /
`v2_conversation_summary_segments`）实测是 append-only，不需要。

> **根因（计划缺陷）**：设计 CIPHERTEXT lane 扩展时只考虑了读侧复制，没检查这些表是否
> UPDATE 密集。SNAPSHOT lane 明确论证过"整表替换天然处理 UPDATE/DELETE"，CIPHERTEXT lane
> 漏了对偶的那一问。

> ### ⚠️ 光补 `mark_pending` 不够——消费端也得配（2026-07-28 实测）
>
> requeue 是**两半**：写侧 `mirror.mark_pending()` 打标记，读侧 `worker._consume_requeue()`
> 按 `_Table.requeue_fetch_sql` 把该 PK 的当前行重新拉回来。
>
> 实测这 4 张表里**只有 `v2_trajectory_reviews` 配了 `requeue_fetch_sql`**（Task 7 的实施者
> 防御性地加的），其余 3 张是 `None`。而 `_consume_requeue` 在
> `requeue_fetch_sql is None` 时**直接 `return (0,0,0,0)` 静默跳过**（`worker.py:1040-1041`）。
>
> 所以只补写侧会比不补更糟：pending 行永远堆积在 `tee_pending_device_migration` 里没人消费，
> 还会把 `requeue_backlog` 指标顶上去，让人以为复制在积压。
>
> **两半必须一起补**，且守卫要同时守住两边。

**Files:**
- Modify: `backend/tee_replicator/worker.py`（给 3 张表补 `requeue_fetch_sql` /
  `requeue_delete_tee_sql`：`model_api_credentials`、`v2_conversation_summary`、
  `v2_workspace_entries`；`v2_trajectory_reviews` 已有）
- Modify: `backend/db.py:8571`、`backend/db.py:7998`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（上表 6 个写点）
- Modify: `backend/tee_shadow/table_registry.py`（新增 `MUTABLE_CIPHERTEXT_TABLES`）
- Create: `tests/test_tee_requeue_coverage.py`

**Interfaces:**
- Consumes: `tee_shadow.mirror.mark_pending(user_id, table_name, item_id, reason)`（已存在）
- Produces: `table_registry.MUTABLE_CIPHERTEXT_TABLES: tuple[str, ...]`

- [ ] **Step 1: 先写守卫测试（会红）**

创建 `tests/test_tee_requeue_coverage.py`。守卫用 grep 源码的方式，仿
`test_no_flask_anywhere` 的既有写法——它要防的是"以后有人给 CIPHERTEXT lane 加了可变表却
忘了打 requeue 标记"，那种遗漏不会让任何现有测试变红。

```python
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
```

- [ ] **Step 2: 在注册表登记可变表**

`backend/tee_shadow/table_registry.py` 末尾（`PSEUDO_CIPHERTEXT_TABLES` 之后）加：

```python
# CIPHERTEXT lane 里会被**原地 UPDATE** 的表。
#
# 这条清单存在的理由：CIPHERTEXT lane 是 append-only 游标模型，游标永不回头，所以
# 原地改写的行必须由写侧显式 mirror.mark_pending(..., "requeue") 打标记，replicator
# 下一趟才会按 PK 重新拉取。漏打标记不会有任何报错或红灯——TEE 侧只是永久停在首次
# 复制时的状态，数据悄悄陈旧。
#
# tests/test_tee_requeue_coverage.py 守着这条清单与实际 mark_pending 调用的一致性。
# 给 CIPHERTEXT lane 加了会被 UPDATE 的表时，登记到这里并在写点补上 mark_pending。
MUTABLE_CIPHERTEXT_TABLES: tuple[str, ...] = (
    "model_api_credentials",
    "v2_conversation_summary",
    "v2_trajectory_reviews",
    "v2_workspace_entries",
)
```

- [ ] **Step 3: 在 6 个写点补 mark_pending**

每个写点在 UPDATE 成功之后（同一函数内、拿得到 user_id 与该表 PK 的位置）补一行：

```python
    mirror.mark_pending(user_id, "<表名>", "<item_id>", "requeue")
```

`item_id` 取值必须与该表在 `worker._TABLES` 里的 `requeue_fetch_sql` 参数**一致**
（Task 7 已配好，逐表核对：`model_api_credentials` 用 `id`、`v2_conversation_summary`
用 `user_id`、`v2_trajectory_reviews` 用 `source_job_id`、`v2_workspace_entries` 用 `path`），
否则 requeue 行拉不回正确的源行。

`mirror.mark_pending` 内部已是尽力而为（失败只计数不抛），符合影子期铁律，
**不要**再包一层 try/except。

写点清单见本节开头的表格。注意 `jobs_store.py:285` 那处是批量 UPDATE
（`UPDATE ... r SET ... FROM ...`），要按返回的行集逐行打标记，不是打一次。

- [ ] **Step 4: 验证**

- `python -m pytest tests/test_tee_requeue_coverage.py -v` → 全绿
- 全量回归零失败
- 人工核对：每个写点补的 `item_id` 与 `worker._TABLES` 对应表的 `requeue_fetch_sql`
  参数一致

- [ ] **Step 5: commit 并报告改动**

---

## 遗留与后续

本计划**不**处理下面两件事，它们是独立工作：

1. **`reconcile_ok` 长期 false + `requeue_backlog` 增长**（2026-07-27 prod 实测 717 → 776 → 3028，单趟 reconcile 耗时 11 分钟）。这是上游 plan 的 Phase 0 Task 0.2。本计划给 tick 增加了 snapshot 段（虽然便宜），建议紧接着处理这个慢性病。实施期间每个 Task 的 prod 验收都要顺带记一次 `requeue_backlog`，观察趋势有没有被本计划恶化。
2. **PG 逻辑复制（`LOGICAL` lane）**。需要把 RDS 的 `rds.logical_replication` 改为 on 并重启实例（静态参数），属独立运维窗口。通道打通后，把 SNAPSHOT lane 的表逐张改判为 LOGICAL 即可——注册表就是为这个切换而设计的。
