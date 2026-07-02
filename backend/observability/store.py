from __future__ import annotations
import json
import logging
import db
from psycopg.types.json import Jsonb

log = logging.getLogger("feedling.observability")

_COLS = ("ts", "host_load1", "host_mem_avail_bytes", "backend_mem_bytes",
         "enclave_mem_bytes", "agentrunner_mem_bytes", "backend_cpu_pct",
         "enclave_cpu_pct", "agentrunner_cpu_pct", "live_agents", "orphan",
         "errors", "db_conns", "backend_5xx")


def insert_sample(sample: dict) -> None:
    row = {k: sample.get(k) for k in _COLS}
    extra = {k: v for k, v in sample.items() if k not in _COLS}
    cols = list(_COLS) + ["extra"]
    vals = [row[k] for k in _COLS] + [Jsonb(extra)]
    ph = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO observability_samples ({', '.join(cols)}) VALUES ({ph}) ON CONFLICT (ts) DO NOTHING"
    with db.get_pool().connection() as conn:
        conn.execute(sql, vals)


def _row_to_dict(cols, row) -> dict:
    d = dict(zip(cols, row))
    extra = d.pop("extra", None) or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    d.update(extra)
    return d


def recent_samples(hours: float) -> list[dict]:
    sql = ("SELECT * FROM observability_samples WHERE ts > now() - make_interval(secs => %s) "
           "ORDER BY ts ASC")
    with db.get_pool().connection() as conn:
        cur = conn.execute(sql, (hours * 3600,))
        cols = [c.name for c in cur.description]
        return [_row_to_dict(cols, r) for r in cur.fetchall()]


def latest_sample() -> dict | None:
    with db.get_pool().connection() as conn:
        cur = conn.execute("SELECT * FROM observability_samples ORDER BY ts DESC LIMIT 1")
        cols = [c.name for c in cur.description]
        row = cur.fetchone()
        return _row_to_dict(cols, row) if row else None


def delete_old_samples(retention_hours: float) -> int:
    with db.get_pool().connection() as conn:
        cur = conn.execute("DELETE FROM observability_samples WHERE ts < now() - make_interval(secs => %s)",
                            (retention_hours * 3600,))
        return cur.rowcount


def upsert_service_resource(service: str, cpu_usage_usec: int | None, mem_bytes: int | None) -> None:
    sql = ("INSERT INTO agent_runtime_resource (service, cpu_usage_usec, mem_bytes, updated_at) "
           "VALUES (%s, %s, %s, now()) ON CONFLICT (service) DO UPDATE SET "
           "cpu_usage_usec = EXCLUDED.cpu_usage_usec, mem_bytes = EXCLUDED.mem_bytes, updated_at = now()")
    with db.get_pool().connection() as conn:
        conn.execute(sql, (service, cpu_usage_usec, mem_bytes))


def read_service_resource(service: str) -> dict | None:
    with db.get_pool().connection() as conn:
        cur = conn.execute("SELECT cpu_usage_usec, mem_bytes, "
                            "extract(epoch FROM now()-updated_at) FROM agent_runtime_resource WHERE service=%s",
                            (service,))
        row = cur.fetchone()
    if not row:
        return None
    return {"cpu_usage_usec": row[0], "mem_bytes": row[1], "age_sec": float(row[2])}


def agent_counts() -> dict:
    with db.get_pool().connection() as conn:
        live = conn.execute(
            "SELECT driver, count(*) FROM agent_runtime_instances WHERE status='running' "
            "AND last_heartbeat_at > now()-interval '90 seconds' AND lease_expires_at > now() "
            "GROUP BY 1").fetchall()
        st = conn.execute(
            "SELECT count(*) FILTER (WHERE status='running' AND (last_heartbeat_at<=now()-interval '90 seconds' "
            "OR lease_expires_at<=now())), count(*) FILTER (WHERE status='idle'), "
            "count(*) FILTER (WHERE error IS NOT NULL AND error<>'') FROM agent_runtime_instances").fetchone()
    by_driver = {d: n for d, n in live}
    return {"live": sum(by_driver.values()), "by_driver": by_driver,
            "orphan": st[0], "idle": st[1], "errors": st[2]}


def db_activity() -> dict:
    with db.get_pool().connection() as conn:
        by_state = conn.execute("SELECT coalesce(state,'bg'), count(*) FROM pg_stat_activity GROUP BY 1").fetchall()
        slow = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE state<>'idle' AND query_start IS NOT NULL "
                             "AND now()-query_start > interval '2 seconds'").fetchone()[0]
        iit = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE state='idle in transaction' "
                            "AND now()-state_change > interval '30 seconds'").fetchone()[0]
        size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    d = {s: n for s, n in by_state}
    return {"conns": sum(d.values()), "by_state": d, "slow": slow,
            "idle_in_tx": iit, "db_size_pretty": size}


def chat_backlog(stale_sec: int) -> int:
    """最新 user 消息晚于该用户最新 agent 回复、且已超 stale_sec 未回复的用户数。

    chat_messages 的实际 schema 是 user_id/seq/msg_id/ts(epoch double)/doc(jsonb)，
    role 在 doc->>'role' 里；agent 侧回复的 role 取值是 'agent'/'openclaw'（不是
    'assistant'——参见 db.py:admin_data_track_snapshot 里同款判断）。ts 是 epoch
    double，所以用 extract(epoch from now()) 做比较，而不是 timestamptz 运算。
    """
    sql = """
    WITH last_user AS (
      SELECT user_id, max(ts) mu FROM chat_messages WHERE doc->>'role'='user' GROUP BY 1),
    last_agent AS (
      SELECT user_id, max(ts) ma FROM chat_messages WHERE doc->>'role' IN ('agent','openclaw') GROUP BY 1)
    SELECT count(*) FROM last_user u LEFT JOIN last_agent a USING (user_id)
    WHERE (a.ma IS NULL OR u.mu > a.ma) AND u.mu < extract(epoch from now()) - %s
    """
    try:
        with db.get_pool().connection() as conn:
            return conn.execute(sql, (stale_sec,)).fetchone()[0]
    except Exception as e:  # chat_messages 结构差异时不炸采样器
        log.warning("[obs] chat_backlog failed: %s", e)
        return -1
