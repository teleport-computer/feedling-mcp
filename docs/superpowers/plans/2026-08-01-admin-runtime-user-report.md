# Admin Runtime User Token/Model and Delivery Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-`user_id`, per-provider/model/route usage report with per-user delivery reliability to the existing Admin Runtime health page.

**Architecture:** `model_api_runtime.v2.jobs_store` owns three content-free PostgreSQL aggregations (metrics, effect outbox, terminal-failure outbox) and merges them into one stable report. `admin.data_track` owns delivery severity policy and HTML rendering, while `admin.admin_core` and `asgi_app` preserve the existing dependency-injection and independent-failure-domain pattern. No migration or public API change is required.

**Tech Stack:** Python 3, PostgreSQL/psycopg, server-rendered HTML, pytest, Ruff.

## Global Constraints

- Attribute usage to `user_id`; never merge by `principal_id`.
- Token/model rows group by `(user_id, provider, model, cache_route_fingerprint)` across every Runtime V2 lane.
- Include prompt/output tokens, cache read/write/miss, calls, retries, provider, model, route, usage coverage, and cache coverage.
- Preserve `None` for missing provider telemetry; do not convert unknown Token/cache values to zero.
- Include current delivery backlog even when it predates the selected window.
- Keep reply, status, runtime-error, and all-effect delivery obligations distinct.
- Treat `applied` and `applied_with_results` as applied effect terminal states.
- `needs_reconciliation` is bad; unfinished age is warn at 1 hour and bad at 6 hours; fresh pending count alone does not degrade health.
- The report is content-free and must not expose endpoints, credentials, prompts, replies, tool payloads, or outbox payloads.
- Report failure must not take down the Runtime health page or its existing health/token/delivery sections.
- Preserve unrelated staged workspace changes. Every commit command in this plan uses `git commit --only` with exact task paths.
- No OpenAPI or public docs update: this is an internal Admin-only view and changes no public contract, trust boundary, or topology.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `backend/model_api_runtime/v2/jobs_store.py` | Query and merge per-user Token/model facts and delivery facts |
| `backend/admin/data_track.py` | Injection stub, delivery-level policy, escaping, and report HTML |
| `backend/admin/admin_core.py` | Runtime-page orchestration and independent query failure handling |
| `backend/asgi_app.py` | Bind the Admin stub to the real jobs-store function |
| `tests/test_v2_runtime_health.py` | PostgreSQL aggregation and time-semantics tests |
| `tests/test_data_track_runtime_view.py` | Pure rendering, route orchestration, and binding tests |

---

### Task 1: Aggregate per-user Token/model facts

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py` (after `recent_token_usage_by_lane`)
- Modify: `tests/test_v2_runtime_health.py` (next to existing Token-by-lane tests)

**Interfaces:**
- Consumes: `record_whole_turn_metric(...)` rows in `v2_turn_metrics`
- Produces: `recent_runtime_user_report(*, within_hours: int = 24) -> dict`; delivery objects are zero/empty defaults until Task 2 fills them

- [ ] **Step 1: Extend the existing metric test helper without changing old-test defaults**

Add optional `provider`, `model`, `route`, `retries`, and `cache_write` parameters to `_add_metric`, and pass them to `record_whole_turn_metric`:

```python
def _add_metric(
    user_id: str,
    lane: str,
    *,
    prompt: int | None,
    completion: int | None,
    model_calls: int = 1,
    retries: int = 0,
    failed: bool = False,
    cache_read: int | None = None,
    cache_write: int | None = None,
    cache_miss: int | None = None,
    usage_reported: int = 1,
    cache_reported: int = 0,
    provider: str | None = None,
    model: str | None = None,
    route: str | None = None,
    age_hours: int = 0,
) -> None:
    seed_user(user_id)
    jobs_store.record_whole_turn_metric(
        None,
        user_id,
        lane,
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=1000,
        model_calls=model_calls,
        retries=retries,
        failed=failed,
        status="turn_failed:providererror" if failed else "ok",
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_miss_tokens=cache_miss,
        usage_reported_calls=usage_reported,
        cache_reported_calls=cache_reported,
        provider=provider,
        model=model,
        cache_route_fingerprint=route,
    )
    if age_hours:
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_turn_metrics SET created_at=clock_timestamp()"
                "-make_interval(hours => %s) WHERE user_id=%s",
                (age_hours, user_id),
            )
```

- [ ] **Step 2: Write failing aggregation tests**

Add focused tests that assert grouping, lane collection, all counters, nullable telemetry, windowing, and stable sorting:

```python
def test_runtime_user_report_groups_each_users_models_and_lanes():
    _add_metric(
        "u_report_a", "chat", prompt=100, completion=10,
        cache_read=60, cache_write=5, cache_miss=40,
        usage_reported=1, cache_reported=1,
        provider="anthropic", model="claude-a", route="route-a", retries=2,
    )
    _add_metric(
        "u_report_a", "heartbeat", prompt=200, completion=20,
        cache_read=100, cache_write=10, cache_miss=100,
        usage_reported=1, cache_reported=1,
        provider="anthropic", model="claude-a", route="route-a", retries=1,
    )
    _add_metric(
        "u_report_a", "chat", prompt=50, completion=5,
        provider="openai", model="gpt-b", route="route-b",
    )

    report = jobs_store.recent_runtime_user_report(within_hours=24)
    user = next(row for row in report["users"] if row["user_id"] == "u_report_a")

    assert user["known_total_tokens"] == 385
    assert user["model_calls"] == 3
    assert [(m["provider"], m["model"], m["route"]) for m in user["models"]] == [
        ("anthropic", "claude-a", "route-a"),
        ("openai", "gpt-b", "route-b"),
    ]
    model = user["models"][0]
    assert model["lanes"] == ["chat", "heartbeat"]
    assert model["turns"] == 2
    assert model["model_calls"] == 2
    assert model["retries"] == 3
    assert model["prompt_tokens"] == 300
    assert model["completion_tokens"] == 30
    assert model["cache_read_tokens"] == 160
    assert model["cache_write_tokens"] == 15
    assert model["cache_miss_tokens"] == 140
    assert model["cache_hit_ratio"] == pytest.approx(160 / 300)
    assert model["usage_coverage"] == pytest.approx(1.0)
    assert model["cache_coverage"] == pytest.approx(1.0)


def test_runtime_user_report_keeps_unknown_usage_and_identity():
    _add_metric(
        "u_report_unknown", "maintenance",
        prompt=None, completion=None, model_calls=1,
        usage_reported=0, cache_reported=0,
    )
    user = jobs_store.recent_runtime_user_report()["users"][0]
    model = user["models"][0]
    assert (model["provider"], model["model"], model["route"]) == (
        "unknown", "unknown", "unknown",
    )
    assert user["known_total_tokens"] is None
    assert model["total_tokens"] is None
    assert model["cache_hit_ratio"] is None
    assert model["usage_coverage"] == pytest.approx(0.0)
    assert model["cache_coverage"] == pytest.approx(0.0)


def test_runtime_user_report_respects_window_and_orders_known_before_unknown():
    _add_metric("u_report_small", "chat", prompt=10, completion=1)
    _add_metric("u_report_big", "chat", prompt=100, completion=10)
    _add_metric(
        "u_report_unknown", "chat", prompt=None, completion=None,
        usage_reported=0,
    )
    _add_metric(
        "u_report_old", "chat", prompt=1000, completion=100,
        age_hours=48,
    )
    users = jobs_store.recent_runtime_user_report(within_hours=24)["users"]
    assert [u["user_id"] for u in users] == [
        "u_report_big", "u_report_small", "u_report_unknown",
    ]
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_v2_runtime_health.py -k 'runtime_user_report' -q
```

Expected: FAIL because `jobs_store.recent_runtime_user_report` does not exist.

- [ ] **Step 4: Implement the minimal Token/model aggregation**

Implement `recent_runtime_user_report` with this grouped query over
`v2_turn_metrics` (use `dict_row`):

```sql
SELECT
  user_id,
  COALESCE(NULLIF(provider, ''), 'unknown') AS provider,
  COALESCE(NULLIF(model, ''), 'unknown') AS model,
  COALESCE(NULLIF(cache_route_fingerprint, ''), 'unknown') AS route,
  array_agg(
    DISTINCT COALESCE(NULLIF(lane, ''), 'unknown')
    ORDER BY COALESCE(NULLIF(lane, ''), 'unknown')
  ) AS lanes,
  count(*)::int AS turns,
  coalesce(sum(model_calls), 0)::bigint AS model_calls,
  coalesce(sum(retries), 0)::bigint AS retries,
  coalesce(sum(usage_reported_calls), 0)::bigint AS usage_reported_calls,
  coalesce(sum(cache_reported_calls), 0)::bigint AS cache_reported_calls,
  sum(prompt_tokens)::bigint AS prompt_tokens,
  sum(completion_tokens)::bigint AS completion_tokens,
  sum(cache_read_tokens)::bigint AS cache_read_tokens,
  sum(cache_write_tokens)::bigint AS cache_write_tokens,
  sum(cache_miss_tokens)::bigint AS cache_miss_tokens
FROM v2_turn_metrics
WHERE created_at >= now() - make_interval(hours => %s)
GROUP BY user_id, provider, model, cache_route_fingerprint
```

Normalize nullable numeric fields with a local `_optional_int(row, key)` helper.
Compute both coverage values only when `model_calls > 0`, and compute cache hit
ratio only when both read and miss sums are non-`None` and their sum is positive.

The function must initialize each user with this delivery shape so Task 2 can merge facts without changing the public return type:

```python
def _empty_user_delivery() -> dict:
    return {
        "reply_effects": {
            "applied_in_window": 0,
            "pending": 0,
            "needs_reconciliation": 0,
        },
        "status_effects": {
            "applied_in_window": 0,
            "pending": 0,
            "needs_reconciliation": 0,
        },
        "all_effects": {
            "applied_in_window": 0,
            "discarded_in_window": 0,
            "pending": 0,
            "needs_reconciliation": 0,
        },
        "terminal_failure": {
            "reply_delivered_in_window": 0,
            "reply_undelivered": 0,
            "status_delivered_in_window": 0,
            "status_undelivered": 0,
            "runtime_error_delivered_in_window": 0,
            "runtime_error_undelivered": 0,
        },
        "oldest_unfinished_age_sec": None,
    }
```

Sort model rows and users in Python with an explicit unknown-last key:

```python
def _known_total_sort_key(total, calls, identity):
    return (
        total is None,
        -(int(total) if total is not None else 0),
        -int(calls or 0),
        identity,
    )
```

- [ ] **Step 5: Run RED tests and the existing lane aggregation tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_v2_runtime_health.py \
  -k 'runtime_user_report or token_usage_by_lane' -q
```

Expected: PASS.

- [ ] **Step 6: Commit only Task 1 paths**

```bash
git commit --only backend/model_api_runtime/v2/jobs_store.py \
  tests/test_v2_runtime_health.py \
  -m "feat(admin): aggregate runtime usage by user and model"
```

---

### Task 2: Add per-user delivery reliability facts

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `tests/test_v2_runtime_health.py`

**Interfaces:**
- Consumes: Task 1 `recent_runtime_user_report()` and `_empty_user_delivery()`
- Produces: completed `users[*].delivery` data from `v2_effect_outbox` and `v2_terminal_failure_outbox`

- [ ] **Step 1: Add failing delivery aggregation tests**

Use direct SQL fixtures so the test proves the actual schema semantics. Cover:

```python
def test_runtime_user_report_separates_reply_status_and_all_effect_delivery():
    seed_user("u_delivery")
    with db.get_pool().connection() as conn:
        rows = [
            ("e_reply_applied", "reply", "applied", 0),
            ("e_final_applied", "reply_final_fenced_v1", "applied", 0),
            ("e_status_applied", "status", "applied", 0),
            ("e_workspace_results", "workspace_batch_encrypted_v1", "applied_with_results", 0),
            ("e_discarded", "memory", "discarded", 0),
            ("e_pending", "reply", "pending", 7200),
            ("e_reconcile", "status", "needs_reconciliation", 60),
        ]
        for effect_id, effect_type, status, age_sec in rows:
            conn.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id,user_id,effect_type,expected_generation,payload,status,created_at) "
                "VALUES (%s,%s,%s,1,'{}'::jsonb,%s,"
                "clock_timestamp()-make_interval(secs => %s))",
                (effect_id, "u_delivery", effect_type, status, age_sec),
            )

    user = jobs_store.recent_runtime_user_report()["users"][0]
    delivery = user["delivery"]
    assert delivery["reply_effects"] == {
        "applied_in_window": 2, "pending": 1, "needs_reconciliation": 0,
    }
    assert delivery["status_effects"] == {
        "applied_in_window": 1, "pending": 0, "needs_reconciliation": 1,
    }
    assert delivery["all_effects"] == {
        "applied_in_window": 4,
        "discarded_in_window": 1,
        "pending": 1,
        "needs_reconciliation": 1,
    }
    assert delivery["oldest_unfinished_age_sec"] == pytest.approx(7200, abs=5)


def test_runtime_user_report_keeps_old_unfinished_but_windows_finished_effects():
    seed_user("u_old_delivery")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_effect_outbox "
            "(effect_id,user_id,effect_type,expected_generation,payload,status,created_at) "
            "VALUES ('e_old_applied',%s,'reply',1,'{}','applied',now()-interval '48 hours'),"
            "('e_old_pending',%s,'reply',1,'{}','pending',now()-interval '48 hours')",
            ("u_old_delivery", "u_old_delivery"),
        )
    user = jobs_store.recent_runtime_user_report(within_hours=24)["users"][0]
    assert user["known_total_tokens"] is None
    assert user["models"] == []
    assert user["delivery"]["reply_effects"]["applied_in_window"] == 0
    assert user["delivery"]["reply_effects"]["pending"] == 1
```

First extend `_add_failure_outbox_row` with
`reply_delivered: bool = True` and insert `reply_delivered_at` with the same
`CASE WHEN` pattern as the other two timestamps. Then add:

```python
def test_runtime_user_report_tracks_three_failure_delivery_duties_independently():
    _add_failure_outbox_row(
        "u_failure_delivery",
        reply_delivered=True,
        status_delivered=True,
        runtime_error_delivered=False,
        age_sec=0,
    )
    _add_failure_outbox_row(
        "u_failure_delivery",
        reply_delivered=False,
        status_delivered=True,
        runtime_error_delivered=True,
        age_sec=48 * 3600,
    )
    _add_failure_outbox_row(
        "u_old_fully_delivered",
        reply_delivered=True,
        status_delivered=True,
        runtime_error_delivered=True,
        age_sec=48 * 3600,
    )

    users = {
        row["user_id"]: row
        for row in jobs_store.recent_runtime_user_report(within_hours=24)["users"]
    }
    failure = users["u_failure_delivery"]["delivery"]["terminal_failure"]
    assert failure == {
        "reply_delivered_in_window": 1,
        "reply_undelivered": 1,
        "status_delivered_in_window": 1,
        "status_undelivered": 0,
        "runtime_error_delivered_in_window": 0,
        "runtime_error_undelivered": 1,
    }
    assert "u_old_fully_delivered" not in users
```

- [ ] **Step 2: Run delivery tests and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_v2_runtime_health.py \
  -k 'runtime_user_report and delivery' -q
```

Expected: FAIL because Task 1 returns zero delivery defaults.

- [ ] **Step 3: Implement effect and terminal-failure aggregation**

Add these two grouped queries inside `recent_runtime_user_report`.

Effect query:

```sql
WITH cutoff AS (
  SELECT now() - make_interval(hours => %s) AS ts
)
SELECT
  e.user_id,
  count(*) FILTER (
    WHERE e.created_at >= cutoff.ts
      AND e.status IN ('applied', 'applied_with_results')
  )::int AS all_applied_in_window,
  count(*) FILTER (
    WHERE e.created_at >= cutoff.ts AND e.status = 'discarded'
  )::int AS all_discarded_in_window,
  count(*) FILTER (WHERE e.status = 'pending')::int AS all_pending,
  count(*) FILTER (
    WHERE e.status = 'needs_reconciliation'
  )::int AS all_needs_reconciliation,
  count(*) FILTER (
    WHERE e.created_at >= cutoff.ts
      AND e.effect_type IN ('reply', 'reply_final_fenced_v1')
      AND e.status IN ('applied', 'applied_with_results')
  )::int AS reply_applied_in_window,
  count(*) FILTER (
    WHERE e.effect_type IN ('reply', 'reply_final_fenced_v1')
      AND e.status = 'pending'
  )::int AS reply_pending,
  count(*) FILTER (
    WHERE e.effect_type IN ('reply', 'reply_final_fenced_v1')
      AND e.status = 'needs_reconciliation'
  )::int AS reply_needs_reconciliation,
  count(*) FILTER (
    WHERE e.created_at >= cutoff.ts AND e.effect_type = 'status'
      AND e.status IN ('applied', 'applied_with_results')
  )::int AS status_applied_in_window,
  count(*) FILTER (
    WHERE e.effect_type = 'status' AND e.status = 'pending'
  )::int AS status_pending,
  count(*) FILTER (
    WHERE e.effect_type = 'status'
      AND e.status = 'needs_reconciliation'
  )::int AS status_needs_reconciliation,
  extract(epoch FROM (
    clock_timestamp() - min(e.created_at) FILTER (
      WHERE e.status IN ('pending', 'needs_reconciliation')
    )
  )) AS oldest_unfinished_age_sec
FROM v2_effect_outbox e
CROSS JOIN cutoff
WHERE e.created_at >= cutoff.ts
   OR e.status IN ('pending', 'needs_reconciliation')
GROUP BY e.user_id
```

Terminal-failure query:

```sql
WITH cutoff AS (
  SELECT now() - make_interval(hours => %s) AS ts
)
SELECT
  f.user_id,
  count(*) FILTER (
    WHERE f.created_at >= cutoff.ts AND f.reply_delivered_at IS NOT NULL
  )::int AS reply_delivered_in_window,
  count(*) FILTER (
    WHERE f.reply_delivered_at IS NULL
  )::int AS reply_undelivered,
  count(*) FILTER (
    WHERE f.created_at >= cutoff.ts AND f.status_delivered_at IS NOT NULL
  )::int AS status_delivered_in_window,
  count(*) FILTER (
    WHERE f.status_delivered_at IS NULL
  )::int AS status_undelivered,
  count(*) FILTER (
    WHERE f.created_at >= cutoff.ts
      AND f.runtime_error_delivered_at IS NOT NULL
  )::int AS runtime_error_delivered_in_window,
  count(*) FILTER (
    WHERE f.runtime_error_delivered_at IS NULL
  )::int AS runtime_error_undelivered,
  extract(epoch FROM (
    clock_timestamp() - min(f.created_at) FILTER (
      WHERE f.reply_delivered_at IS NULL
         OR f.status_delivered_at IS NULL
         OR f.runtime_error_delivered_at IS NULL
    )
  )) AS oldest_unfinished_age_sec
FROM v2_terminal_failure_outbox f
CROSS JOIN cutoff
WHERE f.created_at >= cutoff.ts
   OR f.reply_delivered_at IS NULL
   OR f.status_delivered_at IS NULL
   OR f.runtime_error_delivered_at IS NULL
GROUP BY f.user_id
```

Merge by `user_id`. If a delivery-only user is absent from the Token/model query, create:

```python
{
    "user_id": user_id,
    "known_total_tokens": None,
    "model_calls": 0,
    "models": [],
    "delivery": _empty_user_delivery(),
}
```

Set `oldest_unfinished_age_sec` to the maximum of the effect and terminal-failure ages. Re-sort the user list after adding delivery-only users.

- [ ] **Step 4: Run all report tests and existing delivery-health tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_v2_runtime_health.py \
  -k 'runtime_user_report or recent_delivery_health' -q
```

Expected: PASS.

- [ ] **Step 5: Commit only Task 2 paths**

```bash
git commit --only backend/model_api_runtime/v2/jobs_store.py \
  tests/test_v2_runtime_health.py \
  -m "feat(admin): report delivery reliability by runtime user"
```

---

### Task 3: Render the per-user report and severity policy

**Files:**
- Modify: `backend/admin/data_track.py`
- Modify: `tests/test_data_track_runtime_view.py`

**Interfaces:**
- Consumes: `recent_runtime_user_report()` payload from Tasks 1-2
- Produces: `_runtime_user_delivery_level(delivery: dict) -> str`, `_render_runtime_user_report(user_report: dict | None) -> str`, `_runtime_user_report` injection stub, and the fourth optional `_render_runtime_health_page(..., user_report=None)` argument

- [ ] **Step 1: Read the test-quality rules before editing tests**

Read `/Users/zhengzhihao/.codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills/test-driven-development/writing-good-tests.md` completely.

- [ ] **Step 2: Add realistic report fixtures and failing pure tests**

Add this fixture to `tests/test_data_track_runtime_view.py`:

```python
def _user_report():
    empty_failure = {
        "reply_delivered_in_window": 0,
        "reply_undelivered": 0,
        "status_delivered_in_window": 0,
        "status_undelivered": 0,
        "runtime_error_delivered_in_window": 0,
        "runtime_error_undelivered": 0,
    }
    return {
        "window_hours": 24,
        "users": [
            {
                "user_id": "usr_report_a",
                "known_total_tokens": 12_900,
                "model_calls": 18,
                "models": [{
                    "provider": "anthropic",
                    "model": "claude-example",
                    "route": "route-fingerprint",
                    "lanes": ["chat", "heartbeat"],
                    "turns": 12,
                    "model_calls": 18,
                    "retries": 2,
                    "usage_reported_calls": 17,
                    "cache_reported_calls": 16,
                    "usage_coverage": 17 / 18,
                    "cache_coverage": 16 / 18,
                    "prompt_tokens": 12_000,
                    "completion_tokens": 900,
                    "total_tokens": 12_900,
                    "cache_read_tokens": 8_000,
                    "cache_write_tokens": 500,
                    "cache_miss_tokens": 4_000,
                    "cache_hit_ratio": 2 / 3,
                }],
                "delivery": {
                    "reply_effects": {
                        "applied_in_window": 10,
                        "pending": 1,
                        "needs_reconciliation": 0,
                    },
                    "status_effects": {
                        "applied_in_window": 4,
                        "pending": 0,
                        "needs_reconciliation": 0,
                    },
                    "all_effects": {
                        "applied_in_window": 24,
                        "discarded_in_window": 1,
                        "pending": 1,
                        "needs_reconciliation": 0,
                    },
                    "terminal_failure": dict(empty_failure),
                    "oldest_unfinished_age_sec": 3600,
                },
            },
            {
                "user_id": "usr_delivery_only",
                "known_total_tokens": None,
                "model_calls": 0,
                "models": [],
                "delivery": {
                    "reply_effects": {
                        "applied_in_window": 0,
                        "pending": 0,
                        "needs_reconciliation": 0,
                    },
                    "status_effects": {
                        "applied_in_window": 0,
                        "pending": 0,
                        "needs_reconciliation": 1,
                    },
                    "all_effects": {
                        "applied_in_window": 0,
                        "discarded_in_window": 0,
                        "pending": 0,
                        "needs_reconciliation": 1,
                    },
                    "terminal_failure": dict(empty_failure),
                    "oldest_unfinished_age_sec": 60,
                },
            },
        ],
    }
```

Then add tests for:

```python
def test_runtime_user_delivery_level_uses_reconciliation_and_age_thresholds():
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 1},
        "oldest_unfinished_age_sec": 1,
    }) == "bad"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 3600,
    }) == "warn"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 21600,
    }) == "bad"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 30,
    }) == "ok"


def test_render_runtime_health_page_shows_user_model_and_delivery_report(bound_request):
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), _user_report()
    )
    assert "用户 Token / Model 与交付可靠性" in html_out
    assert "anthropic" in html_out
    assert "claude-example" in html_out
    assert "route-fingerprint" in html_out
    assert "Retries" in html_out
    assert "Cache R / W / M" in html_out
    assert "Reply effects" in html_out
    assert "Failure reply/status/error" in html_out
    assert "needs_reconciliation" in html_out


def test_render_runtime_user_report_preserves_unknowns_and_escapes(bound_request):
    report = _user_report()
    report["users"][0]["user_id"] = "usr_<unsafe>"
    report["users"][0]["models"][0]["model"] = "<script>bad</script>"
    report["users"][0]["models"][0]["prompt_tokens"] = None
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), report
    )
    assert "<script>bad</script>" not in html_out
    assert "&lt;script&gt;bad&lt;/script&gt;" in html_out
    assert "usr_%3Cunsafe%3E" in html_out
    assert "—" in html_out


def test_render_runtime_health_page_user_report_unavailable_is_local(bound_request):
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), None
    )
    assert "用户 Token/model 与交付可靠性暂时取不到" in html_out
    assert "各 lane 健康" in html_out
    assert "端到端交付" in html_out
```

Also assert the copy says Token/model is hosted Runtime V2 only, identities are historical per-turn route facts, delivery `ok` does not mean client read, and current outstanding delivery ignores the selected time window.

- [ ] **Step 3: Run pure Admin tests and verify RED**

Run:

```bash
.venv-test/bin/python -m pytest tests/test_data_track_runtime_view.py \
  -k 'runtime_user or user_model or user_report' -q
```

Expected: FAIL because the helper, fourth render argument, and section do not exist.

- [ ] **Step 4: Implement the policy and renderer**

Add:

```python
def _runtime_user_delivery_level(delivery: dict) -> str:
    reconciliation = int(
        ((delivery or {}).get("all_effects") or {}).get("needs_reconciliation")
        or 0
    )
    if reconciliation > 0:
        return "bad"
    age = (delivery or {}).get("oldest_unfinished_age_sec")
    if age is not None and float(age) >= _RUNTIME_DELIVERY_AGE_BAD_SEC:
        return "bad"
    if age is not None and float(age) >= _RUNTIME_DELIVERY_AGE_WARN_SEC:
        return "warn"
    return "ok"
```

Add the dependency-injection stub:

```python
def _runtime_user_report(*, within_hours: int = 24) -> dict:
    return {"window_hours": within_hours, "users": []}
```

Extend `_render_runtime_health_page` with `user_report: dict | None = None` and
insert `{_render_runtime_user_report(user_report)}` after the lane table and
before Failure Top. `_render_runtime_user_report` renders exactly two tables:

1. `<table class="runtime-user-models">` with one row per model group and
   columns User, Provider/model/route, Lanes, Turns, Calls/retries, Token in/out,
   Known total, Cache R/W/M, Cache hit, Usage/cache coverage.
2. `<table class="runtime-user-delivery">` with one row per user and columns
   User, Reliability, Reply effects, Status effects, All effects, Failure
   reply/status/error, Oldest unfinished.

Use `quote(user_id, safe="")` for the path and `_data_track_qs()` for the
current Admin query string. Escape every provider/model/route/lane/user string
with `html.escape`. Use existing `_fmt_tokens_compact`, `_fmt_ratio`, and
`_fmt_duration_sec`. Do not render JSON or outbox payloads. Show the unavailable
note only when the argument is `None`; an empty `users` list must say the
selected window has no user metrics or outstanding delivery.

- [ ] **Step 5: Run the focused render tests and the full Runtime-view file**

Run:

```bash
.venv-test/bin/python -m pytest tests/test_data_track_runtime_view.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit only Task 3 paths**

```bash
git commit --only backend/admin/data_track.py \
  tests/test_data_track_runtime_view.py \
  -m "feat(admin): render runtime user usage and delivery report"
```

---

### Task 4: Wire the fourth failure domain

**Files:**
- Modify: `backend/admin/admin_core.py`
- Modify: `backend/asgi_app.py`
- Modify: `tests/test_data_track_runtime_view.py`

**Interfaces:**
- Consumes: `_runtime_user_report(within_hours=hours)` and the fourth render argument from Task 3
- Produces: Runtime Admin route that serves the report without coupling its failure to existing health sections

- [ ] **Step 1: Add failing route and binding tests**

Add tests that monkeypatch all four Runtime data functions and capture their window arguments:

```python
def test_runtime_view_passes_same_window_to_user_report(monkeypatch):
    seen = []
    monkeypatch.setattr(_dt, "_runtime_health_summary", lambda **kw: (_payload()))
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", lambda **kw: _tokens())
    monkeypatch.setattr(_dt, "_runtime_delivery_health", lambda **kw: _delivery())

    def _users(**kwargs):
        seen.append(kwargs["within_hours"])
        return _user_report()

    monkeypatch.setattr(_dt, "_runtime_user_report", _users)
    body = admin_core.page_html("view=runtime&hours=168")
    assert seen == [168]
    assert "用户 Token / Model 与交付可靠性" in body


def test_runtime_user_report_failure_does_not_hide_health(monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", lambda **kw: _payload())
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", lambda **kw: _tokens())
    monkeypatch.setattr(_dt, "_runtime_delivery_health", lambda **kw: _delivery())
    monkeypatch.setattr(
        _dt, "_runtime_user_report", lambda **kw: (_ for _ in ()).throw(RuntimeError("db"))
    )
    body = admin_core.page_html("view=runtime&hours=24")
    assert "Runtime 健康" in body
    assert "用户 Token/model 与交付可靠性暂时取不到" in body
```

Add a binding assertion:

```python
def test_runtime_user_report_is_wired_to_jobs_store():
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store
    assert _dt._runtime_user_report is jobs_store.recent_runtime_user_report
```

- [ ] **Step 2: Run wiring tests and verify RED**

Run:

```bash
.venv-test/bin/python -m pytest tests/test_data_track_runtime_view.py \
  -k 'same_window_to_user_report or user_report_failure or user_report_is_wired' -q
```

Expected: FAIL because Admin does not call or bind the new dependency.

- [ ] **Step 3: Implement route orchestration and assembly binding**

In `admin_core.page_html`, add a fourth independent `try` after global delivery:

```python
try:
    user_report = data_track._runtime_user_report(within_hours=hours)
except Exception:
    logging.exception("runtime user report failed (health still served)")
    user_report = None
return data_track._render_runtime_health_page(
    payload, tokens, delivery, user_report
)
```

In `backend/asgi_app.py`, add:

```python
_admin_data_track._runtime_user_report = (
    _v2_jobs_store.recent_runtime_user_report
)
```

- [ ] **Step 4: Run focused and full Admin Runtime tests**

Run:

```bash
.venv-test/bin/python -m pytest tests/test_data_track_runtime_view.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit only Task 4 paths**

```bash
git commit --only backend/admin/admin_core.py backend/asgi_app.py \
  tests/test_data_track_runtime_view.py \
  -m "feat(admin): wire runtime user report independently"
```

---

### Task 5: Regression and completion verification

**Files:**
- Verify only; modify a task-owned file only if a failing check exposes a defect in this feature

**Interfaces:**
- Consumes: all prior tasks
- Produces: evidence that the report is correct, isolated, and does not regress existing Runtime health behavior

- [ ] **Step 1: Run database-backed Runtime health tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_v2_runtime_health.py -q
```

Expected: all pass, no DB-module skip.

- [ ] **Step 2: Run Admin rendering and route tests**

```bash
.venv-test/bin/python -m pytest \
  tests/test_data_track_runtime_view.py tests/test_data_track.py \
  tests/test_asgi_admin.py -q
```

Expected: all pass.

- [ ] **Step 3: Run changed-file static checks**

```bash
.venv-test/bin/python -m ruff check \
  backend/model_api_runtime/v2/jobs_store.py \
  backend/admin/data_track.py backend/admin/admin_core.py \
  backend/asgi_app.py tests/test_v2_runtime_health.py \
  tests/test_data_track_runtime_view.py
.venv-test/bin/python -m compileall -q \
  backend/model_api_runtime/v2/jobs_store.py \
  backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py
git diff --check
```

Expected: zero errors.

- [ ] **Step 4: Inspect the final diff and workspace preservation**

```bash
git diff HEAD~4 -- \
  backend/model_api_runtime/v2/jobs_store.py \
  backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py \
  tests/test_v2_runtime_health.py tests/test_data_track_runtime_view.py
git status --short
```

Confirm that the pre-existing staged documentation files remain present and were not changed or committed by implementation tasks.

- [ ] **Step 5: Use verification-before-completion before reporting success**

Read and apply `superpowers:verification-before-completion`; report the exact commands and results. If implementation is complete and all checks pass, use `superpowers:requesting-code-review` before handoff.
