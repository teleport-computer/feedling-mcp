---
name: feedling-db-query
description: >-
  Query Feedling IO PostgreSQL in test, pre, or prod with read-only SQL. Use for
  查数据库、查 SQL、查用户数据、核对主库、RDS/TEE PostgreSQL 查询、memory_moments、
  chat_messages、user_blobs、proactive_jobs and tee_sync_runs. For CVM logs,
  CPU, memory, deployment or SSH, use feedling-ops-recon instead.
---

# Feedling IO 数据库查询

## 先确认要查哪一个库

三套环境各有 PostgreSQL。旧 AWS RDS 和 Phala TEE PostgreSQL 可能同时存在；
`feedling-io-db-<env>` 这个名字也不能证明它是影子库还是主库。
`docs/CURRENT_STATE.md` 和 `deploy/DEPLOYMENTS.md` 是接线说明，不能代替线上状态。

1. 明确环境（test/pre/prod）及要查的是**当前主库**、旧 RDS 快照，还是 TEE 库。
2. 查目标环境 `/healthz` 的 `release.git_commit`，对照该 commit 的部署配置与
   `FEEDLING_DATABASE_SCHEMA`、实际注入的 `DATABASE_URL` 目标；不要输出密码。
   如果无法获取 live 配置，只能陈述已知记录，不能把本地 `.env` 当成线上主库证明。
3. test 自 `82c4c019` 起记录为 TEE 主库。prod/pre 仍按各自线上 release 和配置核实。

本地 `.env` 的 `TEST_DATABASE_URL`、`PRE_DATABASE_URL`、`PROD_DATABASE_URL`
可能仍指向旧 RDS。先只看主机名，再决定是否连接：

```bash
python3 - <<'PY'
from pathlib import Path
from urllib.parse import urlsplit

for line in Path('.env').read_text().splitlines():
    key, sep, value = line.partition('=')
    if sep and key in {'TEST_DATABASE_URL', 'PRE_DATABASE_URL', 'PROD_DATABASE_URL'}:
        print(f'{key}: {urlsplit(value).hostname}')
PY
```

## 查 RDS

仅在确认目标就是该连接串指向的 RDS 后使用。`.env` 有明文凭证，别 `cat` 整份文件、
回显 DSN、开启 shell trace，或把密码写进提交文件。以 prod 为例，换环境时替换变量名：

```bash
psql "$(sed -n 's/^PROD_DATABASE_URL=//p' .env | head -1)" \
  -X -v ON_ERROR_STOP=1 \
  -c 'begin read only; select current_database(), current_user, now(); commit;'
```

RDS 常用控制/排查表有 `users`、`proactive_jobs`（含 `status_reason`）、
`tee_sync_runs` 和 `user_logs`。先用 `\d public.<table>` 核对现役 schema，
再写具体查询；不要假定 RDS 与 TEE 的行结构相同。

## 查 TEE PostgreSQL

TEE DB CVM 的网关直通地址为 `<完整 app_id>-5432s.dstack-pha-prod9.phala.network:443`，
数据库名 `feedling`。必须使用 libpq/psql 17+、`sslnegotiation=direct`、
`sslmode=verify-full` 和**该环境自己的 CA**；不需要切换 `phala` profile。
本机已知 psql 18 路径为 `/Library/PostgreSQL/18/bin/psql`。

| 环境 | DB CVM app_id | CA 和凭证位置 |
| --- | --- | --- |
| test | `ca2317945ce4c95f5d5a9df60676a74d59197dcc` | `~/documents/teleport/feedling-pg-test-secrets.txt` 含 owner/app/monitoring DSN 及 PEM CA；把 PEM 提取到权限为 600 的临时文件 |
| pre | `ade3cabf133ec3e9ee6220265843c4ac993e1e63` | `~/documents/teleport/feedling-pg-pre-secrets/ca.crt`；Keychain account `feedling-pre` 中的 `PRE_PG_OWNER_PASSWORD` / `PRE_APP_DB_PASSWORD` |
| prod | `4903a1288de38b1b5cfccda6aca1cbc3715090bf` | `~/feedling-io-db-prod/certs/ca.crt`；`~/feedling-io-db-prod/prod-pg-secrets.env` 中的 `PG_OWNER_PASSWORD` / `APP_DB_PASSWORD` |

优先用现成的只读 `monitoring` 角色；需要读业务表时使用 `feedling_owner`。
连接参数示例（先从上表选**同一个环境**的 host、CA、用户和密码；不要输出密码）：

```bash
PGPASSWORD="$DB_PASSWORD" /Library/PostgreSQL/18/bin/psql \
  "host=$DB_HOST port=443 dbname=feedling user=$DB_USER sslmode=verify-full sslnegotiation=direct sslrootcert=$DB_CA connect_timeout=15" \
  -X -v ON_ERROR_STOP=1 \
  -c 'begin read only; select current_database(), current_user, now(); commit;'
```

`DB_HOST` 必须是完整 app_id 加 `-5432s.dstack-pha-prod9.phala.network`；
`DB_PASSWORD` 从上表机密位置读取，不要放进命令文本、日志或仓库。连接失败时先检查
psql 版本、CA 与环境是否匹配、app_id 是否写全，再查网络。

TEE 表在 `public` schema；内容行通常用 `doc` jsonb，具体字段先用 `\d` 核对。
常用表：`users`（身份）、`memory_moments`（记忆）、`world_book_entries`（设定）、
`chat_messages`（对话）、`user_blobs`（如 `kind='user_mcp'`）、`perception_items`。
限定用户并限制返回量，例如：

```sql
begin read only;
select occurred_at, left(doc->>'body', 300)
from memory_moments
where user_id = 'usr_具体用户ID' and doc::text ilike '%关键词%'
order by occurred_at desc
limit 20;
commit;
```

只取排查所需的最小数据，避免整表导出、完整密钥或大段私人内容。比较身份时用数据库内
布尔比较，不返回 `public_key`、`principal_id` 或 token 原值。SQL 写入、删除及 DDL
超出本 skill 的只读查询范围，需要按具体任务另行处理。
