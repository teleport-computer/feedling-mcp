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

The larger run is manual and local:

```bash
python -m scripts.loadtest.run_loadtest --users 100 --workers 16
```

Results from a development machine are indicative only. RSS, latency, queue
wait, and capacity claims used for an environment decision must be measured on
an equivalently sized CVM and bound to its exact commit and configuration.

The original “load test, cut over everyone, then kill Resident” rollout plan is
historical. The current dual-runtime topology retains the hosted Resident path
and per-user rollback; this harness must not be used as authority to remove it.
