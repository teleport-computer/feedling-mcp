# Runtime V2 Slot Restart Race Fix Implementation Plan

> Execute on a branch based on `test`; do not change pre/prod configuration.

1. Add a deterministic concurrency test in `tests/test_v2_child_supervisor.py`.
   Start one real supervised child, publish an active snapshot, issue two
   concurrent restarts against that exact snapshot, and assert one `True`, one
   `False`, a single replacement spawn, and no surviving orphan.

2. Run the new test alone and confirm it fails on the current split
   compare/kill/start lifecycle.

3. Add a per-supervisor lifecycle lock and private lock-held lifecycle helpers.
   Implement atomic `restart_if_snapshot`; serialize start, stop, kill, and
   watchdog respawn through the same lock. Invalidate stopped generations and
   refuse replacement, DB claim recovery, and broker permit release when
   SIGKILL plus bounded join cannot confirm death.

4. Change `SlotFleet.restart_if_snapshot` and `_JobCancelRouter` fallback to
   call the supervisor's atomic fenced restart operation.

5. Run targeted child-supervisor, pool-supervisor, serve-worker cancellation,
   reconcile, and watchdog tests; then run the repository's approved local
   Postgres suite.

6. Commit, open a PR to `test`, merge after CI, monitor the test deployment,
   and perform a fresh V2-user chat/Profile-preemption validation. Confirm four
   configured slots equal four actual child processes and update test evidence.
