# CPU Recorder Cycle Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the managed CPU recorder finish sequential stats reads for all seven test-CVM containers while retaining a ten-second per-request timeout.

**Architecture:** Separate the complete Docker sampling-cycle budget from the Docker client's per-request timeout. Fix the measured cycle budget at thirty seconds, expose it through the managed Compose environment, and leave collection order, storage, isolation, and resource limits unchanged.

**Tech Stack:** Python 3.12, pytest, Docker Compose YAML, Phala CVM deployment.

## Global Constraints

- Individual Docker requests remain bounded to ten seconds.
- A complete Docker sampling cycle is bounded to thirty seconds.
- Sampling stays sequential, private, read-only, content-free, and non-blocking for business services.
- Retention remains thirty UTC dates and sampling remains once per minute.

---

### Task 1: Separate request and cycle timeout budgets

**Files:**
- Modify: `tests/test_cpu_recorder.py`
- Modify: `ops/cpu_recorder.py`

**Interfaces:**
- Consumes: `DockerStatsClient.timeout_sec: float` as the per-request bound.
- Produces: `CpuRecorder.docker_cycle_timeout_sec: float` with a thirty-second default and `CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC=30` configuration.

- [ ] **Step 1: Write the failing regression tests**

Add a seven-container delayed client test that constructs `CpuRecorder` without an explicit cycle timeout. Advance a fake monotonic clock by 0.5 seconds for listing and 2 seconds per stats read, perform two cycles, and assert the second cycle writes seven container rows. Extend the main configuration test to assert a thirty-second cycle budget while the client timeout remains ten seconds.

- [ ] **Step 2: Run tests to verify RED**

Run: `PYTHONPATH=backend python -m pytest tests/test_cpu_recorder.py -k 'seven_delayed_containers or main_builds' -v`

Expected: failure because the current implicit cycle budget is ten seconds.

- [ ] **Step 3: Implement the minimal timeout separation**

Set `CpuRecorder`'s default complete-cycle timeout to thirty seconds rather than inheriting the client timeout. In `main()`, read fixed `CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC=30` and pass it as `docker_cycle_timeout_sec`; keep `CPU_RECORDER_DOCKER_TIMEOUT_SEC=10` on `DockerStatsClient`.

- [ ] **Step 4: Run the CPU recorder unit tests**

Run: `PYTHONPATH=backend python -m pytest tests/test_cpu_recorder.py -v`

Expected: all tests pass.

### Task 2: Pin and validate the managed Compose configuration

**Files:**
- Modify: `deploy/docker-compose.phala.test.yaml`
- Modify: `deploy/docker-compose.phala.yaml`
- Modify: `tests/test_cpu_recorder_compose.py`

**Interfaces:**
- Consumes: `CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC` from the recorder process.
- Produces: measured test and production Compose definitions fixed at `"30"`.

- [ ] **Step 1: Write the failing Compose assertions**

Require `CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC` in the recorder environment and assert its value is `"30"` for both managed Compose files.

- [ ] **Step 2: Run the Compose tests to verify RED**

Run: `PYTHONPATH=backend python -m pytest tests/test_cpu_recorder_compose.py -v`

Expected: failure because the variable is absent.

- [ ] **Step 3: Add the fixed Compose value**

Add `CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC: "30"` beside the existing ten-second request timeout in both managed Compose files.

- [ ] **Step 4: Run the full regression suite**

Run: `PYTHONPATH=backend python -m pytest tests/test_cpu_recorder.py tests/test_cpu_recorder_compose.py tests/test_cpu_socket_proxy_integration.py -v`

Expected: all runnable tests pass; the Docker integration test may skip only when Docker is unavailable.

- [ ] **Step 5: Validate formatting and commit**

Run: `git diff --check`

Commit: `fix: allow CPU recorder to finish container sampling`

### Task 3: Push and live-verify test

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: pushed `test` branch and its GitHub Actions deployment.
- Produces: live evidence of fresh, complete CPU CSV samples.

- [ ] **Step 1: Push test directly**

Run: `git push origin test`

- [ ] **Step 2: Monitor GitHub Actions through deployment completion**

Confirm CI and `deploy-test-cvm` complete successfully for the pushed commit.

- [ ] **Step 3: Verify the live test CVM**

Confirm recorder logs have no new `TimeoutError`, the daily CSV grows across two one-minute observations, every latest timestamp contains all running containers, `/healthz` is HTTP 200, and all containers have zero restart/OOM events.
