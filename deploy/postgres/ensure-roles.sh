#!/bin/bash
# ensure-roles.sh — 每次启动幂等：对齐 owner 密码；建/对齐 app、tee_replicator、
# monitoring 三角色。POSTGRES_PASSWORD 只在 initdb 生效（spec §3 密码漂移坑），
# 这里是唯一让轮换真正生效的地方。
#
# 密码值绝不拼进任何一层手写的 SQL/shell 字符串字面量（原实现的坑：密码含
# '/"/\$/反斜杠会破坏字符串插值，SQL 侧甚至构成注入）——
#   - SQL 侧：SQL 模板只含 psql 的 :'pw' 字面量引用占位符，从不含密码本身；
#     psql 把 pw 变量代入时做正确的 SQL 字面量转义（等价于参数化查询）。
#   - shell 侧：密码经环境变量 ENSURE_ROLE_PW 传给 su 派生出的 postgres 子
#     shell，在那唯一一层里以 "$ENSURE_ROLE_PW" 双引号展开取用一次——bash
#     的双引号展开只是把值原样塞进这一个 argv token，不会把内容重新当 shell
#     语法解析，因此对密码里的任何字符都免疫。
#
# readiness 联动（Round 6 P2）：本脚本被 entrypoint-wrapper.sh 扔后台运行，
# healthcheck 只看 sentinel 文件是否存在——任一关键步骤失败都不落 sentinel，
# 让 CVM 停在 unhealthy 而不是假装角色/密码已经对齐。
set -uo pipefail
log() { echo "[ensure-roles] $*"; }

SENTINEL=/var/run/postgresql/feedling-roles-ok
failed=0

for _ in $(seq 1 60); do
    su postgres -c "pg_isready -h /var/run/postgresql -q" && break
    sleep 2
done
su postgres -c "pg_isready -h /var/run/postgresql -q" || { log "pg never ready"; exit 1; }

run_sql() { su postgres -c "psql -h /var/run/postgresql -U \"${POSTGRES_USER}\" -d \"${POSTGRES_DB}\" -v ON_ERROR_STOP=1 -c \"$1\""; }

# run_password_sql $1=SQL 模板（含 :'pw' 占位符，绝不含密码本身） $2=密码
#
# SQL 模板写一个 0600、属主为 postgres 的临时文件，密码经环境变量 + psql
# --set=pw= 传入，SQL 里用 :'pw' 取值——psql 的字面量引用语法会做正确的 SQL
# 转义（等价于参数化查询），密码全程不出现在任何被解释执行的字符串文本里。
run_password_sql() {
    local sql_tpl="$1" password="$2"
    local sql_file
    sql_file="$(mktemp /tmp/ensure-role-XXXXXX.sql)" || { log "mktemp failed"; return 1; }
    chmod 600 "$sql_file"
    printf '%s\n' "$sql_tpl" > "$sql_file"
    chown postgres:postgres "$sql_file"
    ENSURE_ROLE_PW="$password" su postgres -c \
        "psql -h /var/run/postgresql -U \"${POSTGRES_USER}\" -d \"${POSTGRES_DB}\" -v ON_ERROR_STOP=1 --set=pw=\"\$ENSURE_ROLE_PW\" -f \"${sql_file}\""
    local status=$?
    rm -f "$sql_file"
    return $status
}

if run_password_sql "ALTER USER \"${POSTGRES_USER}\" WITH PASSWORD :'pw';" "${POSTGRES_PASSWORD}"; then
    log "owner password aligned"
else
    log "owner ALTER failed"
    failed=1
fi

ensure_role() {  # $1=role $2=password $3=extra grant SQL（可空）
    local role="$1" role_failed=0
    if ! run_sql "DO \\\$\\\$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$role') THEN
            CREATE ROLE \\\"$role\\\" LOGIN;
        END IF; END \\\$\\\$;"; then
        log "role $role CREATE failed"
        role_failed=1
    fi
    if ! run_password_sql "ALTER ROLE \"$role\" WITH LOGIN PASSWORD :'pw';" "$2"; then
        log "role $role ALTER failed"
        role_failed=1
    fi
    if [ -n "$3" ]; then
        if ! run_sql "$3"; then
            log "role $role GRANT failed"
            role_failed=1
        fi
    fi
    if [ "$role_failed" -eq 0 ]; then
        log "role $role ensured"
    else
        failed=1
    fi
}

# app：业务 CRUD，非 owner（DDL 会被拒 → Phase 1 负向验收）
ensure_role app "${APP_DB_PASSWORD}" \
  "GRANT USAGE ON SCHEMA public TO app;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app;"

# tee_replicator：与 app 同权（写明文表 + 游标表；游标表由 alembic_tee 建）
ensure_role tee_replicator "${REPLICATOR_DB_PASSWORD}" \
  "GRANT USAGE ON SCHEMA public TO tee_replicator;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tee_replicator;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO tee_replicator;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tee_replicator;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO tee_replicator;"

# monitoring：只有 pg_monitor（读 pg_stat_archiver / pg_ls_waldir），读不了业务表
ensure_role monitoring "${MONITORING_DB_PASSWORD}" \
  "GRANT pg_monitor TO monitoring;"

if [ "$failed" -eq 0 ]; then
    install -m 644 /dev/null "$SENTINEL"
    log "all roles/passwords aligned — sentinel written ($SENTINEL)"
else
    log "one or more steps FAILED — sentinel NOT written, healthcheck will stay unhealthy"
    exit 1
fi
