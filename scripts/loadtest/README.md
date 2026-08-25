---
document_lifecycle: current
canonical_owner: self
---
# Runtime V2 load-test harness

This directory contains a deterministic, local Runtime V2 load-test harness.
It does not define the active hosted-runtime rollout policy; use
[`docs/CURRENT_STATE.md`](../../docs/CURRENT_STATE.md), the environment compose,
and [`deploy/DEPLOYMENTS.md`](../../deploy/DEPLOYMENTS.md) for that.

`run_loadtest.py` uses a simulated processor. It exercises the real PostgreSQL
job queue and metrics table, but it does not run a real provider call or the
production worker pipeline. CI therefore runs only the small harness smoke test.
It is a simulated functional harness, **not capacity evidence**.

## Manual simulated run: disposable local database only

The larger run is manual and may write persistent synthetic users, credentials,
routes, runtime controls, jobs, and metrics. Set `DATABASE_URL` to a database
created solely for this run on a local machine. **Never run it against a shared
test database, pre, prod, or any database containing real user data.**

After confirming the target is disposable, run:

```bash
export DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/feedling_loadtest_disposable'
python -m scripts.loadtest.run_loadtest --users 100 --workers 16
```

`--workers` is report metadata only for this level-B simulated drain: it does
not create a concurrent worker pool or establish concurrency/capacity. Clean up
the synthetic records when the run is finished (their user foreign keys cascade
to the harness-created credentials, routes, controls, jobs, and metrics):

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "DELETE FROM users WHERE user_id LIKE 'loadtest_%';"
```

Results from a development machine are indicative only. RSS, latency, queue
wait, and capacity claims used for an environment decision must be measured on
an equivalently sized CVM and bound to its exact commit and configuration.

## Token comparison flow

The current token gate is offline and fixture-based. First obtain or re-measure
the resident baseline with `python scripts/loadtest/measure_resident.py`; then
run the V2 comparison with that number:

```bash
python -m scripts.loadtest.compare_tokens --resident-baseline 9303.0
```

The recorded method, fixture scope, and the warning to re-measure on the
reviewed integration commit are in
[`docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md`](../../docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md).
This comparison exercises the real V2 unified tool loop against a mock provider;
it is a token-regression check, not load or rollout authority.

The original “load test, cut over everyone, then kill Resident” rollout plan is
historical. The current dual-runtime topology retains the hosted Resident path
and per-user rollback; this harness must not be used as authority to remove it.
