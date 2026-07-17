# Production deploy availability and admin debug scaling

Date: 2026-07-17
Status: implementation proposed for Claude 3 gatekeep

## Incident evidence

On 2026-07-15 the user-visible failure sequence had two distinct server-side
windows.

1. The main production CVM update began at 05:55:58 UTC. The first device
   connection failure was recorded at 05:56:20 UTC, followed by TLS errors,
   connection loss, and timeouts across identity, token, tracking, memory, and
   chat endpoints. Requests recovered at 06:00:42 UTC. This is an in-place
   update of the only production ingress/backend CVM, not a store-only failure.
2. The only standalone production runner began its in-place update at
   06:01:27 UTC. The first `hosting_runtime_unavailable` response was recorded
   at 06:03:21 UTC. The CVM remained `updating` until the deploy job failed its
   300-second ready timeout at 06:06:32 UTC. The main CVM no longer has an
   inline runner and `deploy/prod-runner-cvm-ids.txt` names one runner, so the
   deployment removed the fleet's only hosting path.

The retained per-user debug trace is a 200-event ring, not a durable incident
log. By 2026-07-17 none of the retained traces for the 216 current `model_api`
users contained this window. Exact affected-user counts and later historical
windows therefore cannot be reconstructed from this surface. The confirmed
lower bound is the reporting user; the potential blast radius was every
`model_api` send during the singleton runner outage.

Three later successful CI runs updated that same singleton runner in place:
14:55:43–14:59:17 UTC on July 15, 03:22:20–03:25:44 UTC on July 16, and
07:26:28–07:29:41 UTC on July 16. Each is a confirmed runner-unavailable risk
window by topology, although rolled traces cannot establish how many sends
landed in those windows.

The reporting user's proactive snapshot contains 500 retained jobs: 346
completed, 97 posted, 10 pending, and 47 failed. Five failed jobs are in the
heartbeat lane, none in the screen lane, and 42 in other lanes. The admin
snapshot does not expose `status_reason`, although the underlying job rows do,
so the 47-job reason distribution cannot be recovered through the current
production admin API.

## Invariants

### Production runner safety

- Removing the inline main-CVM runner requires at least two independent live
  standalone runner CVMs.
- CI must fail before mutating either production CVM when the standalone runner
  deployment is enabled, the inline runner is absent, and fewer than two runner
  IDs are configured.
- Runner CVMs are updated one at a time. A later improvement must verify a
  fresh, capable heartbeat after each update before advancing to the next ID.
- Provisioning the second runner and testing lease takeover remain an operator
  action; repository code cannot create the second trusted CVM safely.

### Main CVM availability

The main ingress/backend CVM is also a singleton. A true fix for its four-minute
deploy outage requires blue/green main CVMs behind a stable external endpoint,
with readiness and drain before removing the old target. This implementation
does not claim that a post-deploy canary provides zero downtime. Until the
topology exists, main-CVM deploys remain a known maintenance window.

### Admin debug behavior

- Global debug requests retain their current filtering, sorting, pagination,
  and response shape.
- Reading trace state for N users must use one set-based database query for the
  two relevant blob kinds, rather than 2N pool acquisitions and SQL round trips.
- A single-user request may use the same batch path; missing blobs keep the
  existing default-enabled behavior.
- The proactive admin summary exposes failed job reasons as a `job_failed_reasons`
  count map for terminal `failed` and `skipped` jobs, with
  empty reasons grouped under `unknown`. This is an internal admin contract and
  contains no user-authored job payload.

## Implementation

1. Add a `db.get_blobs_for_users` set-based read helper.
2. Refactor `_data_track_debug_payload` to load all requested trace blobs once
   and parse them per user without changing output semantics.
3. Add proactive failed-reason aggregation to both the exact per-user path and
   the fast admin snapshot path.
4. Add a production topology preflight job and make `deploy-cvm` depend on it,
   so an invalid singleton-runner topology blocks the deployment before the
   main or runner CVM is changed.
5. Update deployment documentation to reflect the active topology and the
   enforced two-runner minimum.

## Verification

- Unit-test batched blob reads, trace default-enabled semantics, filters, and
  pagination.
- Unit-test proactive failed-reason classification.
- Parse the GitHub Actions YAML and assert the production deploy depends on the
  topology preflight and that the preflight rejects fewer than two IDs when the
  standalone deployment is enabled.
- Run the focused admin/data-track and workflow tests before gatekeep.
