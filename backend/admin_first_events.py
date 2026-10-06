"""Index-directed first-event reads for admin aggregates.

Keep the historical MIN semantics: NULL timestamps do not participate, the
first genuine reply is selected before checking a reporting window, and
proactive agent replies remain eligible for the onboarding milestone/feed.
Only the no-reply queue excludes proactive replies; that is a different ruler.
"""
from __future__ import annotations


def onboarding_rows(conn, *, registered_at_sql: str, routes_cte: str,
                    registered_cutoff_ts: float | None):
    # Correlated ordered LIMITs stop at the first matching event on existing
    # (user_id, ts, seq)/(user_id, stream, ts) indexes. Grouped MINs can scan
    # every historical chat row (including JSON reads) before returning one.
    if registered_cutoff_ts is None:
        u_filter = ""
        cohort_and = ""
        params = None
    else:
        # t0 is not visible inside its own CTE's WHERE, so the parse
        # expression repeats; the cutoff itself stays a bound param.
        u_filter = (
            "\n                      WHERE EXTRACT(EPOCH FROM "
            f"({registered_at_sql})) >= %s"
        )
        cohort_and = " AND user_id IN (SELECT user_id FROM u)"
        params = (float(registered_cutoff_ts),)
    return conn.execute(f"""
        {routes_cte},
        u AS (SELECT user_id,
                EXTRACT(EPOCH FROM ({registered_at_sql})) AS t0
              FROM users{u_filter}),
        gen_started AS (SELECT user_id, MIN(EXTRACT(EPOCH FROM updated_at)) AS t
                  FROM genesis_import_jobs
                  WHERE COALESCE(NULLIF(metadata->>'mode',''),'onboarding')='onboarding'{cohort_and}
                  GROUP BY user_id),
        gen AS (SELECT user_id, MIN(EXTRACT(EPOCH FROM updated_at)) AS t
                FROM genesis_import_jobs
                WHERE status IN ('done','completed')
                  AND COALESCE(NULLIF(metadata->>'mode',''),'onboarding')='onboarding'{cohort_and}
                GROUP BY user_id),
        mem AS (SELECT user_id,
                MIN(EXTRACT(EPOCH FROM (COALESCE(NULLIF(doc->>'created_at',''), occurred_at))::timestamptz)) AS t
                FROM memory_moments
                WHERE COALESCE(NULLIF(doc->>'created_at',''), occurred_at) ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'{cohort_and}
                GROUP BY user_id)
        SELECT u.user_id, COALESCE(r.route,'resident') AS route, u.t0,
               CASE WHEN COALESCE(r.route,'resident')='model_api' THEN gen_started.t
                    ELSE LEAST(
                        (SELECT ts FROM chat_messages
                         WHERE user_id=u.user_id AND ts IS NOT NULL
                         ORDER BY ts LIMIT 1),
                        (SELECT ts FROM user_logs
                         WHERE user_id=u.user_id AND stream='proactive_jobs'
                           AND ts IS NOT NULL ORDER BY ts LIMIT 1)
                    ) END AS t1,
               CASE WHEN COALESCE(r.route,'resident')='model_api' THEN gen.t ELSE mem.t END AS t2,
               (SELECT ts FROM chat_messages
                WHERE user_id=u.user_id AND ts IS NOT NULL
                  AND doc->>'role' IN ('agent','openclaw')
                  AND COALESCE(doc->>'source','')
                      NOT IN ('foreground_fallback','proactive_fallback')
                ORDER BY ts LIMIT 1) AS t3
        FROM u
        LEFT JOIN routes r ON r.user_id = u.user_id
        LEFT JOIN gen_started ON gen_started.user_id = u.user_id
        LEFT JOIN gen ON gen.user_id = u.user_id
        LEFT JOIN mem ON mem.user_id = u.user_id
    """, params).fetchall()


def recent_first_reply_rows(conn, *, window_epoch: float):
    return conn.execute(
        """
        WITH cand AS (
            SELECT DISTINCT user_id FROM chat_messages
            WHERE ts >= %s
              AND doc->>'role' IN ('agent','openclaw')
              AND COALESCE(doc->>'source','')
                  NOT IN ('foreground_fallback','proactive_fallback')
        )
        SELECT cand.user_id, first_reply.ts
        FROM cand CROSS JOIN LATERAL (
            SELECT ts FROM chat_messages cm
            WHERE cm.user_id = cand.user_id AND ts IS NOT NULL
              AND cm.doc->>'role' IN ('agent','openclaw')
              AND COALESCE(cm.doc->>'source','')
                  NOT IN ('foreground_fallback','proactive_fallback')
            ORDER BY ts LIMIT 1
        ) first_reply
        WHERE first_reply.ts >= %s
        """,
        (window_epoch, window_epoch),
    ).fetchall()
