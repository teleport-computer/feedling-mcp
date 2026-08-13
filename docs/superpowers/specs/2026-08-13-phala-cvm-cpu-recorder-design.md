# Phala CVM CPU Recorder Design

**Date:** 2026-08-13  
**Status:** Approved for implementation planning

## Goal

Record CPU history for the Feedling Phala CVMs without adding alerting or a
general-purpose monitoring stack. The recorder must retain one-minute host and
container-level samples for 30 days, survive application-container restarts,
and have no runtime dependency from the production services.

The primary production target is `feedling-enclave-v2`. The same components
must be deployable to the test CVM first so their operational impact can be
measured before production promotion.

## Non-goals

- No alerting, paging, or notification rules.
- No public metrics endpoint or remotely accessible dashboard.
- No application-level request, user, prompt, or content metrics.
- No process control, container restart, deployment, or Docker mutation API.
- No Prometheus, Grafana, cAdvisor, or other general-purpose monitoring stack.

## Architecture

Add two isolated services to the Phala Compose stack:

1. `cpu-socket-proxy` mounts `/var/run/docker.sock` and exposes only the Docker
   read APIs required to enumerate containers and read their statistics.
2. `cpu-recorder` connects to the proxy over a private Compose network, samples
   host and per-container CPU once per minute, and writes CSV files to a named
   persistent volume.

The recorder must never mount the Docker socket directly. The proxy must deny
all mutation APIs and must not publish a host or public port. Neither component
may be listed as a dependency of `backend`, `enclave`, `enclave-domain`,
`serve-worker`, or `ingress`.

Data flow:

```text
/proc CPU counters ───────────────┐
                                  ├─ cpu-recorder ── daily CSV ── named volume
Docker socket ── cpu-socket-proxy ┘                         │
                                                           └─ 30-day cleanup
```

The recorder obtains aggregate CVM CPU counters from a read-only host `/proc`
mount with a distinct container path. It obtains per-container cumulative CPU
counters and container metadata through the restricted Docker API. CPU usage
is calculated from counter deltas rather than trusting a one-shot presentation
value from `docker stats`.

## Recorded data

Each sample records only infrastructure metadata and numeric resource values:

- UTC timestamp in RFC 3339 format.
- CVM logical environment/name supplied as non-secret configuration.
- Host logical CPU count.
- Host CPU busy, idle, and I/O-wait percentages over the sample interval.
- Host load averages for 1, 5, and 15 minutes.
- Docker container ID prefix and container name.
- Container CPU consumption in cores.
- Container CPU as a percentage of total CVM CPU capacity.

The data must not contain environment variables, labels, command lines, image
registry credentials, application logs, user identifiers, request data, or any
other business content.

## Storage and retention

Samples are written to one CSV file per UTC day under a dedicated Phala named
volume. Files use a stable schema and include a header. A temporary file must
not be used as the authoritative daily record; each completed sample is flushed
so a recorder restart loses at most the in-progress sample.

At startup and after each successful daily rollover, the recorder deletes only
its own daily CPU files older than 30 days. Cleanup must use an exact filename
pattern and a validated recorder-owned directory. It must never recursively
delete an unresolved or broad path.

At the expected scale of roughly five containers, one-minute sampling produces
about 216,000 container rows over 30 days. The expected storage footprint is
well below 100 MB without compression. The initial implementation does not
compress active or retained files; compression can be evaluated separately if
measured storage usage makes it necessary.

Operators retrieve data through existing Phala access paths, for example by
using `phala ssh` to inspect the recorder volume or `phala cp` to copy selected
daily files. The design exposes no HTTP endpoint.

## Resource and failure isolation

The initial resource ceilings are:

| Service | CPU ceiling | Memory ceiling |
| --- | ---: | ---: |
| `cpu-socket-proxy` | 0.05 CPU | 64 MB |
| `cpu-recorder` | 0.10 CPU | 128 MB |

The recorder samples every 60 seconds and applies a 10-second timeout to a
Docker API sampling cycle. A failed sample is logged and skipped; the next
minute is attempted normally. Missing, stopped, newly created, or renamed
containers must not terminate the recorder. Container restarts naturally
create a new container ID while preserving the container name in later rows.

Failure or resource exhaustion in either observability service must not restart,
block, or mark any business service unhealthy. Both services may use
`restart: unless-stopped` for their own recovery.

## Security model

Mounting the Docker socket, even read-only, grants a caller broad effective
control through the Docker API. The recorder therefore receives no socket
mount. Only the proxy holds the mount, and its allowlist is limited to the
minimum endpoints required for container enumeration, daemon information if
needed for CPU normalization, and container stats.

The proxy is reachable only on an internal Compose network shared with the
recorder. It has no published port and no credentials or Feedling application
secrets. The recorder likewise receives no database, R2, API, runtime-token, or
Cloudflare secrets.

All container images must be pinned to immutable digests. The implementation
must verify the selected proxy's actual allowlist semantics against its pinned
version; environment-variable names alone are not sufficient evidence that
mutation endpoints are denied.

Because adding services changes the measured Compose configuration, production
promotion requires the repository's normal test-to-production branch flow and
the existing on-chain Compose hash authorization process.

## Implementation boundaries

- Keep sampling and CSV/retention logic in a small standalone program with no
  dependency on Feedling application modules.
- Add equivalent recorder wiring to the test and production Phala Compose files
  so test evidence matches the production topology.
- Prefer an image built by the existing repository release pipeline for the
  recorder rather than executing an unversioned script fetched at runtime.
- Do not alter backend, enclave, ingress, or Runtime V2 behavior.
- Review and update public deployment/trust documentation under
  `docs-site/content/docs/` because the measured production topology and Docker
  socket trust boundary change. Record the operator-visible addition under
  `Unreleased` in the public changelog.

## Verification

### Automated checks

- Unit-test `/proc/stat` delta calculation, including counter reset and invalid
  input handling.
- Unit-test Docker cumulative-counter normalization for multi-core hosts.
- Unit-test stable CSV schema and escaping.
- Unit-test exact 30-day retention boundaries and safe filename filtering.
- Unit-test Docker API failures, timeouts, missing containers, and container ID
  replacement.
- Validate both Phala Compose files and assert that neither monitoring service
  publishes ports or becomes a dependency of a business service.
- Verify the socket proxy rejects representative mutation requests while the
  required read requests succeed.

### Test CVM soak

Deploy through the normal `test` branch path and run for at least 24 hours.
Collect evidence that:

- Consecutive successful samples are 50–75 seconds apart during normal load.
- Host CPU and named container CPU values are plausible when compared with
  contemporaneous `top` and `docker stats` snapshots.
- Daily rollover preserves the schema and recorder restart preserves existing
  data.
- The named volume survives a Compose update.
- Recorder and proxy stay within their CPU and memory ceilings.
- Public health checks and representative Runtime V2 latency show no material
  regression.
- Docker read endpoints work and mutation endpoints are denied.

### Production rollout

Promote only after recording the test evidence. Follow the repository branch
rules: ordinary work targets `test`; a production PR must originate from
`test` or `pre`. Publish the new measured Compose hash through the established
authorization workflow, deploy during an appropriate window, then verify the
first production samples and normal service health.

## Operational use

The initial interface is deliberately file-based. Operators can inspect recent
rows, copy a selected date range, or load the CSVs into DuckDB for ad-hoc trend
analysis. Adding a dashboard, remote upload, or alerts is a separate future
decision and is not part of this implementation.
