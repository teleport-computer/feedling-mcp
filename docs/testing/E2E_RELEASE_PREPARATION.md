# E2E release preparation boundaries

The existing P0 entry point supports `--required` to name a reviewed subset of
cells. Each required cell must appear exactly once and return `ok`. Empty
results, missing/duplicate required cells and required skips/warnings block.
Optional historical cells retain their status semantics. This is coverage, not
candidate/deployment proof. The inspected configuration has six Hosted and two
VPS cells; the release owner freezes the applicable subset against the candidate.

`--release` currently fails before key loading, registration or cleanup. There
is no verified end-to-end run-bound upstream call cap or durable absence oracle
in this client. A logical dialogue count is not an upstream-call ceiling. The
missing boundary must cover setup probes, retries, tool continuations, background
work and the VPS CLI. A client environment variable cannot enforce those paths.
Existing per-request provider time/token controls do not establish that aggregate
contract. Keep actual remaining funded credit and consumed markers separately.

Hosted callers can pin `exact_model`; release lifecycle callers must pin it.
Model setup transport failures are not retried: a lost response may already have
spent a provider self-test. This limits client setup submissions, not upstream
provider requests. No implicit paid/model fallback is authorized by this option.

All supported release entries share `require_release_execution`: P0 CLI,
Hosted lane API and provisioning API refuse before external work. None accepts a
boolean or receipt path as proof that a missing mechanism exists. The new release
mode is unavailable until its prerequisites are actually implemented and reviewed.

The internal release cleanup seam checks the new run's recovery manifest and can
preserve a 0600 metadata receipt. That receipt is not the necessary scenario
diagnostic original. With no verified safe capture path bound, cleanup stops
before deletion even when metadata writing succeeded; the marker remains.
Ownership mismatch and capture write failure also stop deletion. There is no
successful cleanup result to record in this unavailable path. Historical default
seven-day failure retention remains unchanged.

Required aggregate results must include nonempty, well-formed step evidence.
Failed/fallback steps cannot hide under top-level `ok`. An explicit
`required_assertions` mapping lets callers distinguish required assertions from
optional warnings; required assertions must each appear once and pass. Without
that mapping all steps in a required cell must pass. Optional historical cells
remain separately scoped.

The real admin endpoint accepts `X-Admin-Token` (and Bearer as another supported
form). Its `admin_core.user_payload` reads the process registry: its 404 is not
lease-attested durable absence. The client now reports this as an instrument gap,
keeps recovery manifests and cannot report successful cleanup from it. Reset 200
alone is insufficient; a successful cleanup also needs independent durable
absence and an old-key 401. No new delete or attestation API is introduced.
VPS descendant termination and lease/process ownership proof remain prerequisites
for a release execution path; the existing local consumer harness does not prove
them. A release receipt is not evidence that these missing mechanisms ran.

Current required security/runtime regression, candidate-dependent P1, nonblocking
parent resource/seven-day observation, and post-release L3 are separate scopes.
Do not wait seven days merely to submit a local candidate; do not mark parent
observation complete from local tests. All live cells remain unmeasured for this
candidate until the execution contract is independently bound and run.

## Bounded first slice (T779 local candidate)

`python -m tools.release_window.bounded_v2 --manifest PATH --out NEW_DIRECTORY`
validates a manifest without network. `--run` is the separate execution opt-in;
actual values, model availability, remaining funded credit, exact deployment,
and receipt hashes must be independently frozen before using it. No live result
is included in this implementation. The first slice is `openai-official-v2-text`
with fixed `gpt-5.2-mini`, using the actual OpenAI `/responses` encoding. It does
not reduce the remaining Hosted/VPS required matrix.

This adapts the prior T801/T802 bounded relay and two-dialogue executor, not a
second general driver. No old run/limits/journal is reused. Required manifest
fields are validated in `tools/release_window/contract.py`. Each stage has an
explicit allocation of primary/retry/background starts and a stage ceiling;
all actual requests while that stage is active, including continuations, consume
that ceiling. Category labels are planned allocations, not inferred wire
attribution. Total starts, request/total bytes, per-start/total requested output,
wall/drain/read limits are mandatory. A reservation is fsynced before dispatch;
unused/failed reservations are not refunded. The same exclusive claim path and
journal cannot be reopened. Requested output caps are not billing settlement.

Only the relay holds the real provider key. The fixture receives a distinct
relay token and pinned base URL. Model/stream/media/hosted-tool variants are
rejected; no model rotation or fallback is attempted. HTTPS relay routing must
already exist and pass the authenticated nonce challenge. This command neither
creates a tunnel nor changes DNS/network/account configuration.

Before creating a fixture or spending, the executor verifies candidate/worker,
DB identity/TLS/read-only transaction and required schema. The test DB connector
reuses the pinned T801 read-only connection configuration; queries for actual
observations are parameterized by the newly created UID only. Receipts contain
membership, active job counts, active lease state, runtime control tuple,
observation time, exact run/environment/expected source and creation-credential
digest. The digest is of account creation credentials, not a fabricated process
lease attestation. SQL sources are users, agent_jobs, agent_runtime_instances,
and v2_runtime_state/user_blobs' control fields; no user bodies are selected.

Ownership requires the fresh provision response digest plus the exclusive
private recovery manifest and creation receipt. No arbitrary UID/resume/delete
CLI is exposed. Failure before ownership is complete preserves any recovery
manifest and reports incomplete. Existing diagnostics that copy raw history are
not used: dialogue bodies are checked in memory; only assertions, status, IDs,
phases and counts persist. Turn settlement and final reply are reread within the
same explicit read/deadline budget.

Cleanup first requires the safe bound scenario receipt, closes relay admission,
drains the actual held exchange, and observes zero owned active/pending work and
leases. It then uses the existing account reset, queries authoritative absence,
and requires old-key401. Any failure remains failed and preserves originals;
cleanup status is written to the run result. The original private recovery
manifest is retained even on confirmation. No global sweep or broad delete API
is introduced. VPS process/session handling remains a separate required slice.


### R5 runtime admission and drain (candidate, not live evidence)

The bounded manifest must explicitly authorize `drain_scope.config_action` as
`delete-model-api`, declare `exclusive_test_runner: true`, and pin exact
`worker_ids` and `pool_slots` for foreground/wake/heavy on that reviewed test
parent. This declaration is an input to independent approval, not evidence of
exclusivity by itself. No real values or live approval are supplied here.

Cleanup closes paid relay admission, saves pre-fence metadata, deletes only the
fresh owned account's Model API configuration, and verifies route absence and
Resident generation state through the read-only DB connection. Resident lease
ownership blocks cleanup even after lease expiry. V2 requires a causal sequence:
a post-fence DB heartbeat, a later heartbeat iteration from the same parent,
then idle child progress later than that iteration's monotonic sample. Every
pinned pool and slot must be present with unchanged process/generation identity.
A stale idle snapshot carried in a fresh heartbeat is insufficient. Any missing,
unknown, restarted or busy witness retains the fixture; no reset is sent.
Only after this proof does the original reset + authoritative absence + old-key
401 sequence run. This is a bounded exclusive-runner acceptance path, not a
shared-fleet shutdown or production cleanup API.

#### Execution environment evidence and invalidation

`execution_environment_receipt` is a Supervisor-reviewed local JSON receipt,
whose SHA-256 is pinned alongside the other execution receipts. It must bind
`run_id`, `api_url`, `expected_release`, and the entire `drain_scope` including
`parent_generation`. It has finite `valid_from_epoch` / `valid_until_epoch` and
three `evidence` entries: `deployment_inventory`, `process_inventory`, and
`exclusive_ownership`, each with an absolute local `path` and `sha256`.

The Supervisor must verify these sources against the actual test deployment:

- Deployment inventory identifies the exact API, V2 and separate Hosted
  Resident releases and their machines/containers, service managers and replica
  counts. It accounts for retired replicas and rollback/autoscaling paths.
- Process inventory links the expected V2 parent generation and all three
  worker IDs to actual host/container identity, parent PID/start identity and
  child slots. It accounts for old processes even when heartbeats expired,
  and identifies the guarded Resident owner processes at the exact release.
- Exclusive ownership records who controls this test runner for the whole run,
  the absence of other processes eligible to claim this fixture's jobs, and the
  prohibition on concurrent deploy/restart/scale/rollback during the window.
  The fixture must be freshly created in this run, with no Resident instance
  history. This path rejects any observed Resident instance history, even when
  the row's owner has released or its lease has expired.

Hashes establish binding, not authenticity or truth. Independent approval must
review these concrete sources; a manifest boolean or recent metrics inventory
cannot establish that an expired-heartbeat process is absent. Missing, changed,
not-yet-valid or expired evidence is rejected before the consumed claim/key
loading and rechecked at candidate observations and during drain. A parent
restart, unexpected visible turn parent, missing slot, changed generation,
release mismatch, lost exclusive control or deployment change invalidates the
run. External loss of control must be communicated by the owner and invalidate
its receipt; the executor cannot infer invisible deployment changes from HTTP.
Uncertainty retains the fixture and blocks cleanup confirmation. These are
execution prerequisites, not a claim that the current environment is verified.

#### Input, cost and read budgets

The implemented input method reserves a verified provider-enforced input hard
cap covering the complete request and any provider-added context. Wire bytes
are not input tokens. A separately verified full-request tokenizer or proven
upper bound is another possible design, but is not implemented here. Missing
proof of the selected hard cap refuses execution; a cap larger than a proposal
requires a different verified counting design or separately reviewed plan, not
an automatic increase in paid limits.

The billing receipt binds the exact model, uncached input rate, all billable
output/reasoning, additional per-start fees, exclusive account funding and its
remaining eligible balance. Each start reserves worst-case input/output cost,
rounded upward to integer nanodollars, before dispatch. Unknown outcomes and
timeouts do not refund reservations. No current rates, balances or server caps
are supplied by this repository. This short text slice forces `tool_choice`
`none`; it does not establish tool-execution regression coverage.

Each HTTP observation/probe and each explicit SELECT consumes the same bounded
read counter before I/O. Provision's internal whoami GET is reserved before
registration. Normal ideal-path source arithmetic is 48 reads, not a measured
execution and not a recommended cap; additional convergence, dialogue and drain
polls require more. Failed reads consume reservations. Before account reset,
at least five reads must remain for four absence-query SELECTs and old-key401;
otherwise the fixture is retained. The historical QA proposal of 30 reads and
earlier estimate of 47 do not establish a sufficient execution budget. Local
plan revision requires review of actual bounded paths, with no implied live GO.

Resident credential resolution captures the admission version before fetching
or decrypting a key, then verifies it again after resolution. The binding
includes control generation, route/credential IDs, the microsecond route
version and encrypted-envelope digest; it contains no plaintext key. Cached
envelopes and decrypted keys are reusable only under that exact binding.
Fetched ciphertext must match the bound envelope. A rotation during resolution
or before acquire/spawn invalidates the entry, including a same-provider/model/
base-URL rotation. First admission cannot assign a new version to an already
resolved old key. Explicit boot roster entries retain their boot-time binding;
a later configuration change requires fresh resolution before readmission.

Admission lock/read exceptions are an unknown observation, distinct from a
confirmed invalid configuration. The supervisor isolates failures while entering
each user's guard, defers that user's new admission, and continues the roster
and deferred-spawn batch. It preserves tracked processes and acquired lease
evidence. During renewal, an already tracked process confirmed alive continues
ownership renewal even if configuration observation is temporarily unavailable;
this does not admit a new process. Later passes retry the configuration check.
Exceptions from the guarded acquire/spawn body are not swallowed by this entry
isolation. Per-user lock waits remain bounded by the existing database policy;
this change does not claim a fixed whole-fleet renewal latency under contention.

Admission uses strict route and route-version database readers: successful
queries with no row return `None`, while database failures propagate into the
per-user unknown-observation boundary. Existing best-effort readers retain their
prior exception-to-`None` behavior for other callers. Local fault-injection tests
exercise the real strict helper query/row handling for both reads, through
`tick` and `renew_live`, including recovery and confirmed absence. They do not
replace integration tests against an approved isolated database.

Discovery itself uses a strict inventory query. A failed query aborts that
reconciliation pass instead of supplying an empty roster. A discovered UID whose
credential resolution is unknown remains in the effective roster as an unresolved
marker: it cannot acquire/spawn, but existing ownership is not treated as removed.
The marker is excluded from persona/backfill follow-up work. A subsequent
successful inventory that genuinely omits a UID still triggers confirmed-removal
handling, and a successful resolution clears the marker naturally.

The relay records the first paid authenticated failure as a durable `run_failed`
event and includes its reason in the closed-admission receipt. Budget refusals,
invalid paid payloads, unknown usage and upstream errors all make the final
executor verdict `INCOMPLETE`, even if the short foreground dialogue assertions
already passed. Confirmed safe cleanup can still complete independently. Unauthenticated
traffic, probe noise and ordinary closure do not create a paid-failure verdict.
The closure verifier checks consistency between the failure event and receipt.

Runnable manifests with fewer than the 48 source-path minimum read units are
rejected before fixture creation. This is only a lower bound: the independently
reviewed plan must still include finite polling and cleanup headroom. Synthetic
unit fixtures using 64 reads are not real execution approval or a funded plan.
