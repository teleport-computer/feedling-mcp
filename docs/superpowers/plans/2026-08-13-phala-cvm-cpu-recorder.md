# Phala CVM CPU Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a low-overhead, container-level CPU recorder to the Feedling test and production Phala CVMs that writes one-minute samples to persistent daily CSV files and retains 30 UTC calendar days without alerts or public endpoints.

**Architecture:** A standalone Python recorder in the existing Feedling image reads host counters from a read-only /proc mount and exact Docker stats endpoints through a narrowly allowlisted socket proxy. The recorder and proxy share an internal-only Compose network, write to a dedicated Phala named volume, have strict resource ceilings, and are not dependencies of any business service.

**Tech Stack:** Python 3.12 standard library, pytest, Docker Engine HTTP API, wollomatic/socket-proxy 1.13.0, Docker Compose, Phala named volumes, MDX.

## Global Constraints

- Sample every 60 seconds with a 10-second Docker API timeout.
- Retain the current UTC day plus the previous 29 UTC dates; do not compress files.
- Record host busy/idle/I/O-wait/load plus per-container CPU cores and total-CVM capacity percentage.
- Record no environment values, labels, command lines, logs, credentials, user IDs, requests, or business content.
- Limit cpu-socket-proxy to 0.05 CPU/64 MB and cpu-recorder to 0.10 CPU/128 MB.
- Permit only GET /containers/json?all=0 and GET requests matching
  /containers/[0-9a-f]{64}/stats?stream=false.
- Publish no proxy/recorder ports. Only the recorder and proxy join their internal network.
- No business service depends on either observability service.
- Pin the proxy to ghcr.io/wollomatic/socket-proxy:1.13.0@sha256:be7a61fc50baf0add95d94442c3d40cddc4594925a564f22ba870eb017ceae9f.
- The recorder image must equal the backend release-pinned Feedling image in each Compose file.
- Follow test-first development, test→prod branch flow, and measured Compose-hash authorization.

## File Map

- Create ops/__init__.py: standalone operations package.
- Create ops/cpu_recorder.py: CPU math, Docker reader, CSV/retention, and process loop; no backend imports.
- Modify deploy/Dockerfile: copy ops into the release image.
- Create tests/test_cpu_recorder.py: pure/unit coverage.
- Create tests/test_cpu_recorder_compose.py: strict topology/security assertions.
- Create tests/test_cpu_socket_proxy_integration.py: live allowlist contract.
- Modify tests/test_release_pin_cas.py: recorder/backend release pin equality.
- Modify deploy/docker-compose.phala.test.yaml and deploy/docker-compose.phala.yaml.
- Modify deploy/DEPLOYMENTS.md and public architecture, self-hosting, changelog pages.
- Create docs/superpowers/reports/2026-08-13-phala-cvm-cpu-recorder-test-evidence.md after the test soak.

---

### Task 1: CPU counter model and Docker stats reader

**Files:**
- Create: ops/__init__.py
- Create: ops/cpu_recorder.py
- Test: tests/test_cpu_recorder.py

**Interfaces:**
- HostCounters(total_ticks: int, idle_ticks: int, iowait_ticks: int)
- HostCpuUsage(busy_pct: float, idle_pct: float, iowait_pct: float)
- ContainerRef(container_id: str, name: str)
- ContainerCpuSnapshot(total_ns: int, system_ns: int, online_cpus: int)
- ContainerCpuUsage(container_id: str, name: str, cores: float, capacity_pct: float)
- parse_proc_stat(text: str) -> HostCounters
- parse_logical_cpu_count(text: str) -> int
- calculate_host_usage(previous, current) -> HostCpuUsage | None
- calculate_container_usage(ref, previous, current) -> ContainerCpuUsage | None
- DockerStatsClient(base_url, timeout_sec=10.0, urlopen_fn=urlopen)

- [ ] **Step 1: Write failing host-counter tests**

Create empty ops/__init__.py and tests proving:

~~~python
def test_parse_proc_stat_ignores_guest_fields():
    assert parse_proc_stat("cpu 100 20 30 400 10 5 15 20 999 888\n") == (
        HostCounters(total_ticks=600, idle_ticks=400, iowait_ticks=10)
    )

def test_host_usage_splits_the_interval():
    usage = calculate_host_usage(
        HostCounters(1000, 600, 50), HostCounters(1200, 700, 70)
    )
    assert (usage.busy_pct, usage.idle_pct, usage.iowait_pct) == (40, 50, 10)

def test_host_counter_reset_is_skipped():
    assert calculate_host_usage(
        HostCounters(1000, 600, 50), HostCounters(900, 500, 40)
    ) is None
~~~

- [ ] **Step 2: Verify RED**

Run: uv run pytest tests/test_cpu_recorder.py -q

Expected: import fails because ops.cpu_recorder is absent.

- [ ] **Step 3: Implement host parsing and math**

Use frozen dataclasses. Parse exactly the aggregate cpu line and its first eight fields: user, nice, system, idle, iowait, irq, softirq, steal. Reject missing/short/non-numeric/negative input with ValueError. Do not double-count guest fields. Return None for regressing counters, non-positive total delta, negative idle/I/O-wait deltas, or materially invalid percentages.

Count host logical CPUs from the same mounted stat text by matching only lines
whose first field is cpu followed by one or more decimal digits. Reject a zero
count. Do not use os.cpu_count(), because the recorder's 0.10-CPU cgroup quota
must not be mistaken for the CVM's physical 8-vCPU capacity.

- [ ] **Step 4: Verify GREEN**

Run the focused file; expect the three tests to pass.

- [ ] **Step 5: Add failing Docker and container-math tests**

Use an injected fake URL opener and assert the exact requests are:

~~~text
GET http://cpu-socket-proxy:2375/containers/json?all=0 timeout=10
GET http://cpu-socket-proxy:2375/containers/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/stats?stream=false timeout=10
~~~

Test 400 ns container delta over 800 ns system delta on 8 CPUs yields 4.0 cores and 50.0 capacity percent. Test reset/zero delta returns None. Test invalid IDs are rejected before a request. Test malformed JSON shapes raise ValueError without including the payload in the message.

- [ ] **Step 6: Verify RED, implement, verify GREEN**

Use urllib.request and json only. Normalize the first Names entry by stripping one leading slash. Require positive online_cpus, falling back to positive percpu_usage length. Calculate capacity_pct = 100 * cpu_delta / system_delta and cores = capacity_pct * online_cpus / 100. Run the focused tests.

- [ ] **Step 7: Commit**

~~~bash
git add ops/__init__.py ops/cpu_recorder.py tests/test_cpu_recorder.py
git commit -m "feat: add CPU recorder metric primitives"
~~~

---

### Task 2: CSV persistence, retention, and resilient sampling loop

**Files:**
- Modify: ops/cpu_recorder.py
- Modify: tests/test_cpu_recorder.py
- Modify: deploy/Dockerfile

**Interfaces:**
- CSV_FIELDS exact schema below.
- SampleBatch(timestamp_utc: datetime, host: HostCpuUsage,
  host_logical_cpus: int, loads: tuple[float, float, float],
  containers: tuple[ContainerCpuUsage, ...])
- DailyCsvStore(data_dir: Path, cvm_name: str, retention_days: int = 30),
  with append(batch: SampleBatch) -> Path and
  prune(today_utc: date) -> list[Path]
- CpuRecorder(client: DockerStatsClient, proc_root: Path,
  store: DailyCsvStore, interval_sec: float = 60.0), with
  sample_once(now_utc: datetime) -> bool and run_forever() -> None
- main() -> int

- [ ] **Step 1: Write failing persistence tests**

Lock this schema:

~~~text
timestamp_utc,cvm_name,host_logical_cpus,host_cpu_busy_pct,
host_cpu_idle_pct,host_cpu_iowait_pct,load1,load5,load15,
container_id,container_name,container_cpu_cores,container_cpu_capacity_pct
~~~

Test one row per container, one host-only row when no container delta is ready, one header per daily file, RFC3339 UTC, CSV escaping, flush plus fsync, and filename cpu-YYYY-MM-DD.csv.

On 2026-08-13, prove prune keeps 2026-07-15 through 2026-08-13, deletes 2026-07-14, and ignores notes.txt, malformed CPU filenames, symlinks, and directories. Reject relative paths, /, missing/non-directory paths, and invalid CVM names. Never recurse.

- [ ] **Step 2: Verify RED, implement, verify GREEN**

Use csv.DictWriter and Path.unlink only. Validate data_dir before any write/delete. Set oldest_kept = today - 29 days. Return deleted exact paths for content-free logging.

- [ ] **Step 3: Write failing loop tests**

Cover:
- first successful sample establishes baselines and writes nothing;
- second sample writes host and stable containers;
- new/reset containers wait for a second snapshot;
- removed containers leave prior state;
- one container failure does not discard valid peers;
- Docker timeout logs a fixed slug plus exception class and next sample recovers;
- invalid /proc does not replace the last good host baseline;
- daily change runs one prune;
- monotonic scheduling advances fixed 60-second deadlines without drift.

Logs must not contain Docker payloads, environment values, or response bodies.

- [ ] **Step 4: Implement the loop and entrypoint**

Read /host/proc/stat and /host/proc/loadavg, deriving host logical CPU count from
the per-CPU lines in the same stat snapshot. Maintain cumulative baselines in
memory. Catch network/JSON/parse/filesystem errors at the sampling boundary and
continue. Individual container failures skip only that container. A failed host
read preserves the prior good host counter. Overruns skip missed deadlines
rather than compressing intervals.

Read these environment values, with exact shipped defaults:

~~~text
CPU_RECORDER_CVM_NAME             required
CPU_RECORDER_DOCKER_URL           http://cpu-socket-proxy:2375
CPU_RECORDER_PROC_ROOT            /host/proc
CPU_RECORDER_DATA_DIR             /var/lib/feedling-cpu
CPU_RECORDER_INTERVAL_SEC         60
CPU_RECORDER_RETENTION_DAYS       30
CPU_RECORDER_DOCKER_TIMEOUT_SEC   10
~~~

Invalid startup configuration exits non-zero; runtime sampling failures do not.

- [ ] **Step 5: Copy ops into the existing image**

Immediately after COPY backend/ ./backend/ in deploy/Dockerfile add:

~~~dockerfile
COPY ops/ ./ops/
~~~

Extend the existing mkdir/chown layer to create and assign UID 1000 ownership
to /var/lib/feedling-cpu before USER feedling. This lets a newly initialized
named volume inherit writable ownership while the recorder root filesystem
remains read-only. Do not add dependencies or alter the backend default command.

- [ ] **Step 6: Verify and commit**

~~~bash
uv run pytest tests/test_cpu_recorder.py -q
python -m py_compile ops/cpu_recorder.py
git diff --check
git add ops/cpu_recorder.py tests/test_cpu_recorder.py deploy/Dockerfile
git commit -m "feat: persist bounded CVM CPU history"
~~~

---

### Task 3: Hardened Phala Compose topology

**Files:**
- Create: tests/test_cpu_recorder_compose.py
- Create: tests/test_cpu_socket_proxy_integration.py
- Modify: tests/test_release_pin_cas.py
- Modify: deploy/docker-compose.phala.test.yaml
- Modify: deploy/docker-compose.phala.yaml

- [ ] **Step 1: Write failing strict-YAML topology tests**

For both Compose files, assert:

- proxy image equals the global pinned digest;
- proxy command equals:
  - -loglevel=INFO
  - -listenip=0.0.0.0
  - -allowfrom=cpu-recorder
  - -allowGET=/containers/json\?all=0
  - -allowGET=/containers/[0-9a-f]{64}/stats\?stream=false
  - -allowhealthcheck
  - -watchdoginterval=60
  - -stoponwatchdog
- proxy has Docker socket :ro, read_only, cap_drop ALL, no-new-privileges, 0.05 CPU, 64m, no ports/expose, and only cpu-observability;
- proxy container_name is cpu-socket-proxy; recorder container_name is cpu-recorder;
- recorder image equals backend image and command equals python -u ops/cpu_recorder.py;
- recorder has read_only, cap_drop ALL, no-new-privileges, 0.10 CPU, 128m, /proc:/host/proc:ro, the environment volume, no Docker socket, and only cpu-observability;
- recorder environment is exactly CVM name, URL, paths, 60/30/10 and has no secret-like keys;
- recorder may depend on proxy health, but business services depend on neither;
- cpu-observability is internal and contains no business service;
- test volume is feedling_cpu_history_test; prod is feedling_cpu_history.

- [ ] **Step 2: Verify RED**

Run: uv run pytest tests/test_cpu_recorder_compose.py -q

- [ ] **Step 3: Add the services, network, and volumes**

Place both services after serve-worker. Proxy healthcheck runs ./healthcheck every 30s with 5s timeout and three retries. Recorder depends only on healthy proxy. Use CVM names feedling-io-test and feedling-enclave-v2. Use restart: unless-stopped for self-recovery.

- [ ] **Step 4: Validate topology**

~~~bash
uv run pytest tests/test_cpu_recorder_compose.py -q
docker compose -f deploy/docker-compose.phala.test.yaml config --quiet
docker compose -f deploy/docker-compose.phala.yaml config --quiet
~~~

- [ ] **Step 5: Extend release-pin CAS tests**

Add a cpu-recorder Feedling image to the synthetic main Compose in
tests/test_release_pin_cas.py. Parse the pinned result and assert backend and
cpu-recorder both equal the string built as
f"ghcr.io/teleport-computer/feedling:{release.trigger_sha[:7]}". Assert retries
accept only that same content and the proxy digest remains unchanged.

- [ ] **Step 6: Add a live proxy contract test**

Skip unless FEEDLING_RUN_DOCKER_SOCKET_TESTS=1, Docker is reachable, and the socket exists. Start only the pinned proxy on an exact temporary network/container ID with loopback test access. Assert:

- intended container list/stats GETs return 200;
- container inspect, logs, images, info, and version GETs return 403;
- restart POST returns 403 and the target remains running.

Use explicit subprocess argument arrays and try/finally cleanup of only IDs created by the test. Never issue an allowed mutation.

- [ ] **Step 7: Run and commit**

~~~bash
uv run pytest tests/test_release_pin_cas.py tests/test_cpu_recorder_compose.py -q
FEEDLING_RUN_DOCKER_SOCKET_TESTS=1 uv run pytest tests/test_cpu_socket_proxy_integration.py -q
git diff --check
git add deploy/docker-compose.phala.test.yaml deploy/docker-compose.phala.yaml tests/test_cpu_recorder_compose.py tests/test_cpu_socket_proxy_integration.py tests/test_release_pin_cas.py
git commit -m "deploy: add isolated Phala CPU recorder"
~~~

---

### Task 4: Deployment and public trust documentation

**Files:**
- Modify: deploy/DEPLOYMENTS.md
- Modify: docs-site/content/docs/architecture.mdx
- Modify: docs-site/content/docs/self-hosting.mdx
- Modify: docs-site/content/docs/changelog.mdx

- [ ] **Step 1: Update internal deployment inventory**

Add both services to current test/prod source topology. Document volume names,
/var/lib/feedling-cpu/cpu-YYYY-MM-DD.csv, current+29-day retention, no
alert/public endpoint, resource limits, and this Python-only retrieval pattern
(Python is guaranteed by the recorder image; no shell dependency):

~~~bash
phala ssh feedling-io-test -- docker exec cpu-recorder python -c 'from pathlib import Path; files=sorted(Path("/var/lib/feedling-cpu").glob("cpu-*.csv")); print(files[-1].read_text()[-4000:] if files else "missing")'
phala ssh feedling-enclave-v2 -- docker exec cpu-recorder python -c 'from pathlib import Path; files=sorted(Path("/var/lib/feedling-cpu").glob("cpu-*.csv")); print(files[-1].read_text()[-4000:] if files else "missing")'
~~~

Do not print more than the bounded tail.

- [ ] **Step 2: Update public architecture and self-hosting trust text**

Architecture must state: only infrastructure identifiers and numeric CPU/load are stored; the recorder has no Docker socket; the regex allowlisted proxy remains a privileged component inside the measured CVM boundary; no public endpoint exists.

Self-hosting must add an optional component-map row and say operators provide their own protected proxy/persistent volume. Copying this topology does not add tenant isolation.

- [ ] **Step 3: Add this Unreleased changelog entry**

~~~markdown
- **Managed Phala deployments can retain bounded CPU history.** The measured
  test and production CVM topology records one-minute host and container CPU
  samples in a private persistent volume for 30 UTC calendar days. A narrowly
  allowlisted internal socket proxy exposes only the Docker reads required by
  the recorder; no public metrics endpoint, application content collection, or
  alerting is introduced.
~~~

- [ ] **Step 4: Validate and commit**

~~~bash
cd docs-site
npm run types:check
npm run lint
npm run build
cd ..
git diff --check
git add deploy/DEPLOYMENTS.md docs-site/content/docs/architecture.mdx docs-site/content/docs/self-hosting.mdx docs-site/content/docs/changelog.mdx
git commit -m "docs: document Phala CPU history boundary"
~~~

OpenAPI regeneration is not required because no public API changes.

---

### Task 5: Local verification and review gate

- [ ] **Step 1: Run focused verification**

~~~bash
uv run pytest tests/test_cpu_recorder.py tests/test_cpu_recorder_compose.py tests/test_cpu_socket_proxy_integration.py tests/test_release_pin_cas.py tests/test_enclave_domain_compose.py tests/test_gunicorn_conf.py -q
FEEDLING_RUN_DOCKER_SOCKET_TESTS=1 uv run pytest tests/test_cpu_socket_proxy_integration.py -q
python -m py_compile ops/cpu_recorder.py
docker compose -f deploy/docker-compose.phala.test.yaml config --quiet
docker compose -f deploy/docker-compose.phala.yaml config --quiet
git diff --check
~~~

- [ ] **Step 2: Run the database-backed L1 suite**

~~~bash
FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
~~~

Compare against the documented baseline and investigate new failures. Never call a DB-skipped run a full pass.

- [ ] **Step 3: Re-run docs validation**

Run npm run types:check, npm run lint, and npm run build from docs-site.

- [ ] **Step 4: Security diff review**

Inspect test...HEAD and confirm: no socket on recorder; no proxy public/default network; exact anchored paths; no secrets; unchanged business dependency direction; exact non-recursive cleanup; backend/recorder release pins match.

- [ ] **Step 5: Request review**

Invoke superpowers:requesting-code-review with the design, plan, commits, and exact verification results. Resolve all correctness/security findings and rerun affected verification before any deployment.

---

### Task 6: Test soak and production gate

- [ ] **Step 1: Ask for explicit push/deployment authorization**

Show commits, verification, measured Compose impact, and that pushing test triggers deployment. Do not push, deploy, publish hashes, or mutate a CVM without approval.

- [ ] **Step 2: Deploy through the normal test path**

After approval, push reviewed work to test and monitor deploy-test-cvm. Do not bypass deploy/pin-runtime-release.sh.

- [ ] **Step 3: Capture initial read-only evidence**

Check phala ps, docker stats --no-stream, recorder/proxy logs, recent CSV rows, test healthz, proxy denials, and representative Runtime V2 latency.

- [ ] **Step 4: Soak at least 24 hours**

Create docs/superpowers/reports/2026-08-13-phala-cvm-cpu-recorder-test-evidence.md recording:

- successful intervals normally 50–75 seconds;
- plausible host/container CPU versus contemporaneous top/docker stats;
- resource ceilings respected;
- recorder restart preserves rows;
- Compose update preserves the named volume;
- intended reads succeed and unintended reads/mutations return 403;
- UTC rollover preserves schema;
- health and representative Runtime V2 latency show no material regression.

State that live 30-day deletion remains unverified after a 24-hour soak; cite unit boundary coverage.

- [ ] **Step 5: Commit test evidence**

~~~bash
git add docs/superpowers/reports/2026-08-13-phala-cvm-cpu-recorder-test-evidence.md
git commit -m "docs: record Phala CPU recorder test evidence"
~~~

- [ ] **Step 6: Ask for explicit production-promotion authorization**

Present evidence, remaining risks, exact prod additions, and rollback. Production PR must originate from test or pre. Never push a locally assembled main.

- [ ] **Step 7: Verify authorized production rollout**

Use sxysuns-projects, verify feedling-enclave-v2 and first samples, record the authorized Compose hash, then switch back to amiller-users-projects. Rollback removes the two services/network but preserves the history volume unless the user explicitly authorizes deletion.
