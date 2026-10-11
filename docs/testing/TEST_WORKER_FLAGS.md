# Test worker process flag observation

This is an operator diagnostic, not a live-test admission or launcher receipt.
The fixed test CVM and HTTPS origin are constants in the producer and collector.
Only test inventory enables the producer. Existing bounded heartbeat sampling
reads the worker and supervisor-owned children, retains three environment keys,
and publishes only release SHA, closed flag values, PID/PPID/start ticks and safe
namespace hostname. It never publishes full environments or command lines.

Linux `/proc/PID/environ` describes the initial environment. Parent capture's
actual imported boolean is also reported. Child inheritance is supported by
PPID, supervisor ownership, process start identity and equal measured values;
it is not proof of later in-process mutations. Hostname is not a verified Docker
container ID. The authenticated heartbeat binds these samples to the worker boot
nonce; this is not hardware attestation. Missing/replaced/oversized/wrong-release
processes yield unavailable, never a synthetic successful receipt.

After review, test merge and separately coordinated deployment, the supervisor
may authorize exactly one read on a stable release:

```sh
gh workflow run memory-pipeline-daily.yml --ref test \
  -f operation=test-worker-flags -f expected_sha=<full-deployed-40-hex-SHA>
```

The existing registered observability workflow is used because a new
workflow_dispatch file must exist on the default branch before it can be run.
The selected test job never executes the daily report, posts to Lark or enters a
deployment workflow. Schedule and ordinary report modes retain their old behavior.
The only sampler inputs are the closed operation selector and exact expected SHA;
existing day/dry_run inputs apply only to the separate report operation.

The sampler uses existing TEST_FEEDLING_ADMIN_PASSWORD for stateless admin login,
then only fixed HTTPS health/metrics GETs. It refuses redirects, disables inherited
proxies, verifies TLS, bounds responses, and writes a projected artifact only.
Credentials and arbitrary response bodies never enter logs/artifacts. Wrong ref,
SHA, inventory, worker, stale evidence or incomplete foreground slots fail closed.
A sampling success proves only the listed fresh flags, not the future release,
10-start budget, paid/fixture permission or source-only causal recall.

Without `--run`, the script only validates its SHA and prints PLAN_ONLY. `--run`
requires test workflow_dispatch context; do not forge this locally to bypass
coordination. CI runs deploy/tests/test_worker_flags.py in the existing deployment
contract test job, with synthetic procfs and offline urllib transport fixtures.
