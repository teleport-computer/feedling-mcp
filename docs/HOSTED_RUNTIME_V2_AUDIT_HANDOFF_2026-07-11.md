# Hosted Runtime V2 audit and engineer handoff — 2026-07-11

> Audited upstream: `feat/hosted-runtime-v2` at
> `0333bc4f8a251d99570a6e0df57cafee751d99b7`
> Follow-up branch: `codex/hosted-runtime-v2-p0-followup`
> Intended PR base: `feat/hosted-runtime-v2`, not `main`

## Executive verdict

The engineer did push new work during this audit. After the earlier `bfc8862`
multi-profile/R2 update, the feature branch advanced to `0333bc4`
(`feat: Update hosted runtime mode handling and exclusivity logic for V2 cutover`).
That commit made an absent or invalid runtime-mode value mean V2 globally, before
the rollout gates below were satisfied. The follow-up branch merges that commit
to preserve shared history and then explicitly reverts its implicit fleet cutover.
V2 remains per-user explicit opt-in; no user was flipped or deployed here.

Recommendation:

- **GO** to open this bounded follow-up as a draft child PR into
  `feat/hosted-runtime-v2`.
- **NO-GO** to enroll an internal user today.
- **NO-GO** to merge the feature branch into `main` or retire resident.

The original three reported blockers are addressed in the child branch:

1. Chat admission now has a queue deadline, including old-app fallbacks; an
   independent backend/worker reaper expires overdue pending work visibly.
2. The AnyIO limiter is sized to configured worker slots, so Anthropic/Gemini
   no longer inherit the silent default pool ceiling. This is containment;
   provider-native async remains the target architecture.
3. The runbook invokes the loop-aware token measurer. Current shared-fixture
   output is 574.0 tokens/turn and 2.3333 model calls/turn, not the stale
   responder-only number.

The child also closes several defects found during the deeper audit: migration
ancestry for already-deployed databases, active-route error visibility, strict
chat durability, old/new worker deadline compatibility, per-user cross-lane
serialization, slot-death supervision, replica-unique worker identity, atomic
runtime-profile key patches, rollback fences before background effects, bounded
private memory search, response-size caps, and plaintext error scrubbing.

It still does not satisfy the full product vision. The largest remaining work is
architectural rather than a safe follow-up patch.

The reconciliation also closes the hazards exposed by `0333bc4`: missing or
invalid mode values remain resident; strict routing reads fail with 503 instead
of guessing; resident discovery propagates database failures so the supervisor
does not tear down a live fleet on an outage; and the grouped admin/scheduler
view now includes active-route users whose runtime flag is absent.

## Storage boundary — current adapter versus target architecture

Encryption is not an extra conversation-compaction requirement. Today the
external RDS adapter stores user conversation content and the V2 summary as
envelopes; the runner/enclave path decrypts that content before it reaches the
model. The target architecture in the walkthrough is different: Postgres moves
inside its own CVM and stores plaintext on a LUKS2 full-disk-encrypted volume,
with encrypted backups, while the envelope/decrypt/rewrap layer is deleted.

Therefore the durable requirement is **storage-agnostic summary and trajectory
persistence**. The current RDS implementation must encrypt content at its
adapter boundary; the future pg-CVM adapter must not preserve application-level
envelopes just for historical compatibility. In both designs the model receives
plaintext conversation context inside the authorized CVM trust boundary.

## Vision scorecard

**Overall: 1 PASS / 2 PARTIAL / 3 FAIL.**

| # | Vision requirement | Verdict | Current state |
|---|---|---|---|
| 1 | No silent hangs, visible deadlines/errors, outer fences, scalable provider concurrency | **PARTIAL** | Pending and active deadlines, independent reaping, safe terminal codes, owner fences, cross-lane serialization, slot supervision, capacity heartbeats, unique worker IDs, and a sized AnyIO limiter are present. Permanently hung calls can still consume every slot while static heartbeat capacity remains positive; native async and a hard watchdog/dynamic capacity remain open. |
| 2 | Entire conversation, not a fixed-count window; storage-agnostic append-only itemized summary and automatic compaction | **FAIL** | Append-and-merge compaction, CAS, oldest-first catch-up, and a verbatim tail exist. The current RDS adapter envelope-encrypts the summary; the target pg-CVM adapter should store it as plaintext on LUKS2 FDE. Prompt assembly still uses hard message caps, and the base store deletes rows/R2 objects beyond 5,000 without proving summary coverage. |
| 3 | One native tool-calling loop for every model; no official/rule tier; parallel tool batches; reply in loop; immediate message folding | **FAIL** | `agent_loop.py` provides bounded inner-round orchestration, but `planner.py` still branches on `is_official`, emits JSON plans, and calls a separate responder. New user input is reloaded only after the entire inner loop returns, not at every round boundary. Provider-native `tools=`, reply-as-tool, mid-turn acknowledgements, and observed-behavior fallback are absent. |
| 4 | Planner/executor vocabulary parity, including schedule, web, and exact memory search | **PARTIAL** | Schedule, web search/fetch, and enclave-private memory search exist. Web fetch now has SSRF, redirect, and body-size controls. Memory matching is exact only inside the configured hard candidate cap (default 1,000); pagination/indexing and a single generated capability schema remain open. |
| 5 | One deployment topology | **PASS** | Executable configuration places `serve-worker` in the runner CVM as a sibling entrypoint. It is not hosted in the main FastAPI process. Genesis is a dedicated thread in `serve-worker`. |
| 6 | Prompt caching, real whole-turn load metrics, admission proof, typing prewarm, durable trajectories/dream review, resident retirement | **FAIL** | Slot-aware admission and a loop-aware offline comparator exist. Production whole-turn metrics, prompt caching, typing prewarm, durable storage-adapted trajectories, dream-lane failure replay, a real kill switch, and the final resident-retirement proof do not. |

## What this child branch changes

### Queue, lease, and mixed-version liveness

- Migration `0023_v2_job_liveness` adds `queue_deadline_at`,
  `lease_expires_at`, and `input_generation`.
- `queue_deadline_at` is separate from legacy `deadline_at`. Pending rows leave
  legacy `deadline_at` null so an old worker claiming near the queue SLA still
  receives its full execution lease.
- Active new-worker leases mirror into legacy `deadline_at`. The reaper reads
  `COALESCE(lease_expires_at, deadline_at)`, so a post-migration old worker that
  dies is still recoverable.
- Old backends that insert no queue deadline are covered by
  `created_at + PENDING_CHAT_TTL_SEC` for chat admission and reaping.
- The reaper runs in both the main ASGI lifespan and `serve-worker`; chat timeout
  terminalization emits an error status and updates the active route's
  `last_runtime_error`.
- Lifecycle transitions require `claimed_by` and an unexpired lease.
- Per-user execution is serialized across every lane while queue single-flight
  remains per `(user_id, lane)`. Chat and wake can no longer race two assistant
  replies for the same user.
- At least one worker slot is always unrestricted; a single-worker deployment
  cannot accidentally reserve its only slot for chat.
- A double failure during slot recovery cannot kill the slot silently. Any
  unexpected slot escape propagates to the process supervisor, and heartbeat
  shutdown publishes capacity zero.
- Default worker IDs include hostname, PID, and a startup UUID, avoiding PID-1
  collisions across replicas.
- Non-positive/invalid worker, enclave, TTL, poll, heartbeat, scheduler, and
  reaper settings fail startup.

### Late input, durable chat, and terminal visibility

- Coalescing increments `input_generation` without postponing the oldest chat's
  queue deadline.
- Finalization atomically completes the old chat job and creates exactly one
  pending successor if input arrived after the worker's observed generation.
- Chat reads use `runtime_state.last_replied_ts`; they do not slice after the
  latest assistant row.
- Production V2 reads and writes are strict: database failures are propagated,
  and a reply cannot be reported successful after only mutating process cache.
- The empty/redundant-chat path now emits terminal `done` and wakes the chat
  poller instead of leaving a visible `processing` event.
- Persisted failure strings are stable codes, not exception bodies. Migration
  `0022_v2_action_queue_privacy` scrubs legacy action payload/result/error data
  and legacy `agent_jobs.last_error`.

### Rollout and control-plane isolation

- Runtime-mode reads used for routing are strict; a control-plane database
  failure returns 503 instead of silently defaulting to resident or V2.
- Top-level runtime-profile patches are atomic JSONB key merges. Concurrent mode
  flips and error/trace updates do not overwrite each other with a stale whole
  document.
- Chat, wake, compaction, and extraction re-check runtime mode and ownership at
  durable-effect boundaries.
- Scheduler decisions treat `runtime_mode` as a no-mutation skip and perform a
  final strict mode check before heartbeat side effects.
- Capture/dream autonomous producers are default-off.
- Runtime errors are written to the active `model_api_routes` row, which is the
  current iOS/read-side truth, while retaining the old blob as a rollback mirror.

### Migration reconciliation

Upstream added a second revision named `0014_model_api_profiles` and rewired the
historical V2 migration underneath it. That would let a database already stamped
at V2 head `0020` skip the profiles schema. The child restores the deployed
parent of `0014_hosted_runtime_v2` and joins the two branches explicitly:

```text
0013_genesis_resident_claim
├── 0014_hosted_runtime_v2 → 0015 … 0020_v2_heartbeat_kind ─┐
└── 0014_model_api_profiles ─────────────────────────────────┤
                                      0021_merge_v2_profiles ┘
                                      0022_v2_action_queue_privacy
                                      0023_v2_job_liveness
                                      0024_v2_worker_capacity (head)
```

An actual old-tree `de9e111` database was upgraded to `0020`, seeded with legacy
plaintext action/job errors, then upgraded by the child to `0024`. The profiles
tables, queue/lease/capacity columns, and privacy scrubs were verified.

### Context, network, memory, and measurement

- Normal prompt reads select the bounded tail before enclave decryption;
  compaction reads the oldest contiguous unsummarized batch and re-enqueues
  catch-up work.
- Private-content memory filtering happens inside the enclave before applying
  the requested result limit. The temporary search field is stripped.
- `web_fetch` validates global destinations and each redirect hop, disables
  automatic redirects, and streams into a hard response-body cap.
- MCP probing uses the same network policy.
- The offline token gate uses the same three workloads as the resident measurer,
  runs planner rounds plus responder, reports call count, and rejects zero,
  negative, NaN, and infinity inputs.

## Remaining P0 blockers before any user flip

These are the next engineer tasks. They are not waived by the passing child-PR
tests.

1. **Safe resident-to-V2 cutover cursor.** Existing resident users do not have a
   trustworthy V2 `last_replied_ts`. Starting at zero can replay all retained
   user messages; seeding to the latest row can skip a resident turn in flight.
   Drain the resident, establish a stable `(sequence/message_id)` handoff, seed
   the cursor transactionally, and only then flip mode.
2. **Transactional visible effects.** Assistant reply append, terminal job
   transition, status/outbox records, and notification are separate commits. A
   crash after reply append can still duplicate or contradict visible state.
   Introduce idempotent effect IDs and a transactional outbox.
3. **Hard recovery for permanently wedged calls.** Queue deadlines make failure
   visible, but N never-returning provider/enclave/DB calls can occupy all N slots
   forever while the process heartbeat advertises static capacity. Add a hard
   turn watchdog/process restart design or dynamic healthy-slot capacity. Chaos
   test N permanent hangs; thread-backed calls require special care because
   cancelling the coroutine does not stop the underlying thread.
4. **Atomic runtime generation fence.** Repeated strict mode reads narrow the
   rollback window but do not make “mode still V2” and a durable reply/memory
   write one atomic transaction. Store a runtime generation and require the
   expected generation in every terminal/effect CAS.
5. **Tool-output trust boundary.** Raw web/tool observations feed the next model
   round, which can request a write. Add deterministic taint/user-intent policy
   so prompt injection in fetched content cannot authorize memory/schedule or
   other durable writes.
6. **Summary-coverage and retention invariant.** No prompt may omit a message
   unless it is below the committed summary-coverage watermark. Replace
   fixed-count context truncation, use a stable ordered cursor, and stop deleting
   DB/R2 chat history merely because it exceeds 5,000 rows.
7. **Live turn-pool kill switch.** It must stop new admissions and claims,
   publish zero turn capacity immediately, drain/fence active work, and leave
   the co-located Genesis importer healthy.

## Required architectural follow-ups

### One provider-native agent loop

- Remove the `official`/rule planner split. Every model receives the same native
  tool schema and loop contract.
- Add provider-native tool calls/results for OpenAI Chat/Responses, Anthropic,
  Gemini, and supported compatible relays.
- Allow parallel independent read calls; serialize guarded writes.
- Make reply/final text a loop-native action, including an optional early
  acknowledgement followed by continued work.
- Fold new user input at every round boundary with no debounce.
- Fall back for one turn when a model emits malformed calls or a relay rejects
  tools; do not pre-classify users/models into capability tiers.
- Add prompt caching for stable prefixes where supported.

### Native async providers and whole-turn telemetry

- Replace thread bridges for Anthropic, Gemini, and OpenAI Responses with native
  async transports while preserving payloads, parsing, retries, cancellation,
  error classes, and reasoning fallbacks.
- Prove more than 32 concurrent slow calls per provider without event-loop
  blocking or a thread limiter ceiling.
- Aggregate every planner/tool-loop/final call, including failed turns, into one
  idempotent per-job metric row with model-call count and latency breakdown.
- Re-run the shared resident/V2 workloads on CVM-class hardware; the offline
  574-token result is not an admission-capacity result.

### Full-history compaction and D-lane completion

- Keep the summary itemized and append/merge only; never wholesale
  rewrite conversation meaning.
- Use token budget plus summary coverage rather than a fixed message count.
- Make CAS loss requeue/retry without advancing the losing watermark.
- Persist durable trajectory/effect records behind the same storage adapter:
  envelope-encrypted on current external RDS, plaintext on the target LUKS2-FDE
  pg CVM, with redacted observability in either topology.
- Give capture/dream immutable input identity, durable pending/backoff state,
  idempotent memory effects, and failure replay into a dream/regression lane.
- Add typing-signal prewarm and complete resident shutdown only after parity,
  soak, and rollback evidence.

## Verification completed on the child branch

- `alembic heads`: one head, `0024_v2_worker_capacity`.
- Real migration: old `de9e111` schema at `0020` → current `0024`, including
  model profiles and representative privacy scrubs.
- Focused regression suite: **385 passed, 1 deprecation warning**.
- Additional focused control/liveness slice: **211 passed**.
- Post-`0333bc4` reconciliation slice: **109 passed**, covering missing,
  invalid, unreadable, explicit-resident, and explicit-V2 modes plus discovery
  outage preservation of a live resident child.
- Broader changed-surface run: **680 passed, 1 expected xfail, 3 known baseline
  failures**; the same registration-key fixture failure and two stale
  `is_verify_reply` monkeypatch signatures were reproduced on the untouched
  engineer tree.
- Loop-aware token command: **574.0 tokens/turn**, **2.3333 calls/turn**, exit 0
  against the measured 9303.0 resident baseline and +10% threshold.

These checks support opening the child PR. They do not replace fault injection,
a real mixed-image deployment drill, or the remaining P0 acceptance tests.

## Rollout gates

| Gate | Required evidence | Stop condition |
|---|---|---|
| Child PR | Reviewable diff against `bfc8862`, one Alembic head, focused tests and migration proof | PR targets `main`, contains unrelated history, or a regression remains |
| Cutover | Resident drained; stable cursor/generation seeded; no double consumer | Any replay, skipped message, or ambiguous owner |
| Fault injection | Kill at pending, claim, provider wait, each tool write, reply append, finalization, and notification | Silent `processing`, duplicate effect, stale-owner write, or wedged single-flight |
| Permanent-hang chaos | Every configured slot is made non-returning | Admission still sees healthy capacity or the fleet cannot recover |
| Provider concurrency | Each direct provider exceeds 32 slow calls with bounded resources | Thread ceiling, event-loop block, changed retry/error behavior |
| Conversation integrity | Long/equal-timestamp history with compaction crash/CAS races and R2 attachments | Any omitted message above the committed watermark |
| Security | Hostile web/tool output followed by attempted write tools | External content authorizes a durable write without deterministic user intent |
| Rollback | Per-user and pool-wide drill; Genesis stays alive; active effects fence | New work accepted, stale V2 effect lands, or Genesis stops |
| Soak | One consented internal user for 24–48h, then 5 users | Any silent error, duplicate, privacy leak, cursor anomaly, or rollback doubt |
| Main | All prior gates and vision-critical parity accepted in writing | Any gate is waived implicitly |

## Recommended work split

Land the bounded child PR first because it is reviewable and removes immediate
hazards without pretending the architecture is complete. Give the engineer the
remaining P0/architecture list above as the next iteration. Do not combine the
native multi-provider tool loop, outbox, full-history retention, and watchdog
redesign into this already-large safety PR.

## Copy/paste message to the engineer

```text
Please continue Hosted Runtime V2 from upstream
0333bc4f8a251d99570a6e0df57cafee751d99b7 and review the child branch
codex/hosted-runtime-v2-p0-followup. The child PR must target
feat/hosted-runtime-v2, not main.

The child preserves 0333bc4 in history but explicitly reverts its implicit
default-to-V2 fleet cutover. Missing/invalid mode values remain resident, control
read failures fail closed, resident discovery outages no longer look like an
empty fleet, and the admin/scheduler mode view includes unset active-route users.
It also addresses the three immediate audit blockers and the bounded follow-on
defects: pending queue deadlines plus independent visible reaping;
separate queue/lease clocks with old/new worker fallbacks; owner-fenced and
per-user cross-lane execution; late-input successor jobs; strict chat reads and
writes; active-route runtime errors; unique worker IDs and slot supervision;
atomic runtime-profile key patches; rollback fences before wake/summary/memory
effects; AnyIO limiter sizing; loop-aware/fail-closed token measurement;
migration-graph repair; privacy scrubs; SSRF/redirect/body caps; and bounded
enclave-private memory search. The focused suite is 385 passed, and an actual
0020→0024 PostgreSQL upgrade was verified.

Do not flip an internal user yet. Close these P0s first:
1. drained resident→V2 cutover with a stable message cursor/runtime generation;
2. transactional outbox and idempotent assistant/status/memory effects;
3. hard recovery or dynamic capacity for N permanently wedged calls;
4. atomic runtime-generation CAS at every durable effect;
5. deterministic prompt-injection/taint policy before tool-derived writes;
6. summary-coverage/retention invariant with no fixed-window history loss;
7. a live turn-pool kill switch that leaves Genesis healthy.

Then finish the vision work: one provider-native tool loop for every model with
no official/rule tier split; native async Anthropic/Gemini/OpenAI Responses;
whole-turn production metrics; prompt caching; durable storage-adapted trajectories;
capture/dream lifecycle and failure replay; typing prewarm; and resident
retirement only after fault injection, rollback, load, and soak gates pass.

Storage terminology is deliberate: current external RDS uses envelope-encrypted
content, while the target pg CVM stores plaintext on LUKS2 FDE and deletes the
application envelope/decrypt layer. Do not turn encryption into a permanent
compaction or trajectory requirement.

Use docs/HOSTED_RUNTIME_V2_AUDIT_HANDOFF_2026-07-11.md as the acceptance
contract and attach evidence for every rollout gate. Treat 574.0 tokens/turn and
2.3333 calls/turn as the current offline shared-fixture result, not production
capacity proof.
```
