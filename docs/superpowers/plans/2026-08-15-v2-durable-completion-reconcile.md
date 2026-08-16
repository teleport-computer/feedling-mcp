# Runtime V2 Durable-Completion Reconcile Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent exact-claim reconciliation from restarting a slot after its Job has durably completed but before the following idle progress message arrives.

**Architecture:** Keep the progress protocol unchanged. Filter `durable_completion` snapshots out of the exact-claim reconciliation input; all other active stages retain the existing exact `(job_id, claimed_by)` validation and snapshot-fenced single-slot restart behavior.

**Tech Stack:** Python 3.12, asyncio, pytest, Runtime V2 `SlotFleet`/`SlotProgress` protocol.

## Global Constraints

- Target branch is `test`; do not target `main` directly.
- Do not change database schema, queue state transitions, TTLs, worker counts, Enclave concurrency, or pre/prod configuration.
- Preserve `durable_completion` progress events and their active-job diagnostic identity.
- Preserve immediate recovery for invalid claimed/running snapshots in every non-completion stage.

---

### Task 1: Reproduce and fence the completion-state race

**Files:**
- Modify: `tests/test_v2_pool_supervisor.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py:5073-5099`

**Interfaces:**
- Consumes: `slot_protocol.SlotProgress.stage`, `slot_protocol.ActiveJobIdentity`, `SlotFleet.snapshots()`, and `jobs_store.valid_active_claims(claims: list[tuple[int, str]]) -> set[tuple[int, str]]`.
- Produces: `_reconcile_fleet_claims_once(fleet) -> int` that excludes `stage == "durable_completion"` snapshots while retaining current behavior for all executing stages.

- [ ] **Step 1: Write the failing regression test**

Add this test beside `test_periodic_reconcile_restarts_only_the_invalid_exact_claim`:

```python
def test_periodic_reconcile_ignores_durable_completion_snapshot(monkeypatch):
    fleet = _fleet()
    fleet.start_all()
    completed_key = pool_supervisor.SlotKey("wake", 0)
    invalid_key = pool_supervisor.SlotKey("heavy", 0)
    completed = slot_protocol.ActiveJobIdentity(
        5291, "scheduled", "worker:wake:0:g9"
    )
    invalid = slot_protocol.ActiveJobIdentity(
        5292, "profile", "worker:heavy:0:g8"
    )
    fleet.supervisor(completed_key)._snapshot = slot_protocol.SlotProgress(
        "wake-0", "g9", 12.0, 9.0, "durable_completion", completed
    )
    fleet.supervisor(invalid_key)._snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g8", 12.0, 9.0, "profile.provider", invalid
    )
    queried = []

    def _valid_active_claims(pairs):
        queried.extend(pairs)
        return set()

    monkeypatch.setattr(
        serve_worker.jobs_store, "valid_active_claims", _valid_active_claims
    )
    before = {key: fleet.supervisor(key).pid for key in fleet.keys()}

    assert asyncio.run(serve_worker._reconcile_fleet_claims_once(fleet)) == 1
    assert queried == [(invalid.job_id, invalid.claimed_by)]

    after = {key: fleet.supervisor(key).pid for key in fleet.keys()}
    assert after[completed_key] == before[completed_key]
    assert after[invalid_key] != before[invalid_key]
```

- [ ] **Step 2: Run the test and verify the race is reproduced**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_supervisor.py::test_periodic_reconcile_ignores_durable_completion_snapshot -q
```

Expected: FAIL because `queried` contains both the completed and invalid pairs, and both slots are restarted.

- [ ] **Step 3: Implement the minimal stage fence**

Change the snapshot comprehension in `_reconcile_fleet_claims_once` to:

```python
snapshots = {
    key: snapshot
    for key, snapshot in fleet.snapshots().items()
    if snapshot is not None
    and snapshot.active_job is not None
    and snapshot.stage != "durable_completion"
}
```

Add a short comment explaining that the Job is already terminal at this stage and the immediately following idle progress clears the active identity; process liveness remains the fallback if the child wedges.

- [ ] **Step 4: Run the focused tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_supervisor.py tests/test_v2_child_supervisor.py tests/test_v2_capacity_health.py tests/test_v2_worker.py -q
```

Expected: all tests pass; the existing invalid-claim test still restarts only the invalid slot.

- [ ] **Step 5: Run static checks and commit**

Run:

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m compileall -q backend/model_api_runtime/v2
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pyflakes backend/model_api_runtime/v2/serve_worker.py tests/test_v2_pool_supervisor.py
git diff --check
```

Expected: all commands exit 0.

Commit:

```bash
git add backend/model_api_runtime/v2/serve_worker.py tests/test_v2_pool_supervisor.py
git commit -m "fix(v2): ignore durable completion during claim reconcile"
```

### Task 2: Integrate through test and verify the live invariant

**Files:**
- No code files beyond Task 1.

**Interfaces:**
- Consumes: GitHub PR checks, test deployment workflow, test `serve-worker` logs and process tree.
- Produces: a PR targeting `test` plus live evidence that normal durable completion no longer triggers an exact-claim restart.

- [ ] **Step 1: Push and create a PR targeting test**

Run:

```bash
git push -u origin fix/v2-durable-completion-reconcile
gh pr create --base test --head fix/v2-durable-completion-reconcile --title "fix(v2): fence durable completion reconciliation" --body-file /tmp/v2-durable-completion-pr.md
```

The PR body must state the observed Job 5291 race, the stage-only fence, focused test evidence, and that no configuration/database contract changes are included.

- [ ] **Step 2: Require all PR checks to pass**

Run:

```bash
gh pr checks --watch
```

Expected: Runtime V2 profile=0/profile=1, `test_api.py`, syntax/static, Docker, and docs checks pass.

- [ ] **Step 3: Merge to test and monitor deployment**

After explicit merge authorization, merge the PR and monitor the resulting `test` CI run through main CVM deploy, attestation, canary, and runner CVM deploy.

- [ ] **Step 4: Verify live test state**

Run read-only checks:

```bash
curl -sS -i https://test-api.feedling.app/healthz
phala ps feedling-io-test
phala ps feedling-io-agents-test
phala logs serve-worker --cvm-id feedling-io-test --since 15m --stderr -n 1200
```

Expected: health returns 200, all containers run, parent plus four turn children remain live, and a normal completed canary/turn is not followed by `restarted invalid exact claim`. A deliberately invalid non-completion claim remains covered by the automated regression rather than injected into live test data.
