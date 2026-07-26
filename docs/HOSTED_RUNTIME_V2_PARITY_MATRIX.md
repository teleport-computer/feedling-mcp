# Hosted Runtime V2 — Current Parity and Completion Matrix

> **CURRENT SOURCE OF TRUTH — 2026-07-20.** This page describes the current
> Runtime V2 source and managed deployment manifests. A live environment changes
> only after this source is deployed. Use
> [`deploy/HOSTED_RUNTIME_V2_ROLLOUT.md`](../deploy/HOSTED_RUNTIME_V2_ROLLOUT.md)
> for operational gates and
> [`docs/superpowers/specs/runtime-v2-parity-matrix.md`](superpowers/specs/runtime-v2-parity-matrix.md)
> for the detailed capability-to-facade mapping. The dated design plans and
> [`HOSTED_RUNTIME_V2_AUDIT_HANDOFF_2026-07-11.md`](HOSTED_RUNTIME_V2_AUDIT_HANDOFF_2026-07-11.md)
> are historical evidence, not live status.

## Status legend

| Mark | Meaning |
|---|---|
| ✅ | Implemented and guarded in the current source |
| ⚠️ | Correctness exists, but a rollout, scale, or long-horizon gate remains |
| ❌ | Explicitly deferred or not implemented |
| ➖ | Deliberately removed from the Runtime V2 release scope |

## Original V2 vision

| Requirement | Current status | Evidence and remaining gap |
|---|---|---|
| No silent wedges | ✅ | Admission checks the turn-worker heartbeat; pending jobs have queue deadlines and a reaper; terminal failures publish `error` status and `last_runtime_error`; the worker contains turn exceptions; provider I/O is async. A dead pool must fail visibly rather than leave a message in `processing` forever. |
| Full conversation, not a fixed message window | ✅ | The prompt is built from an encrypted hierarchical summary frontier plus a verbatim tail. Raw encrypted chat rows and attached R2 bodies are retained independently of compaction; Clear Chat moves them to an immutable non-prompt archive rather than erasing them, and account deletion is the full retention boundary. The former 5,000-row value bounds only the process hot window. Exact immutable leaf segments and immutable higher-level checkpoints keep the model-facing view bounded without deleting children or rewriting the source transcript. |
| One native agent loop for every model | ✅ | Chat and wake use the same in-process provider-native tool loop. There is no `official`/`rule` tier, planner, or separate responder. A model that does not call a tool naturally returns once; malformed tool output gets bounded compatibility handling, not pre-assigned model routing. |
| Reply inside the loop; eager late-message folding | ✅ | `reply` is a loop tool, so the model may acknowledge the user and continue working. New user messages are claimed without debounce and folded at every round boundary. |
| Parallel tool use | ✅ | A provider turn may request a batch of tools. Independent reads and bounded `task` subagents execute concurrently. Disjoint workspace writes can execute in conflict-free waves while same/ancestor/descendant paths serialize; externally effectful platform/MCP mutations remain provider-ordered. Results are reconstructed in provider order. |
| Executable action vocabulary | ✅ | The exposed native catalog maps to registered executable capabilities. Scheduling, web search/fetch, and exact memory search are present; obsolete planner-only `sleep`/`capture_memory` vocabulary is absent. |
| One deployment topology | ✅ | Local, test, pre, and production hosted model-API deployments are `v2_only`. A bounded `serve-worker` pool runs in the runner CVM, separate from the main backend/enclave CVM; there is no hosted per-account runtime flip. |
| Prompt caching and cache telemetry | ⚠️ | Provider-aware cache controls/affinity and per-turn read/write/miss telemetry are implemented for OpenAI-compatible, Anthropic/OpenRouter, Gemini, and Bedrock paths. The existing Pre canary proves a route-bound OpenRouter cache read; the trusted `/skills` prefix and native Bedrock path still need post-deploy live cache-hit proof. Editable `WORKING.md` is deliberately pull-only and is not part of the eager cache prefix. |
| Tokens/turn and admission ceiling | ✅ | Whole-turn token/call/latency metrics and an admission ceiling are implemented. The offline token regression gate is live, and `/v1/admin/v2-metrics.turn_health` exposes bounded queue/lease expiry, failure/expiry rates, pending age, p95 latency, and trajectory completeness/gaps. |
| Concurrent CVM-class load proof | ⚠️ | The harness exists, but the authoritative concurrent run on the target CVM class remains an operational gate. |
| Typing-signal pre-warm | ➖ | Removed from the release scope by product decision. Ordinary Send remains the only foreground trigger; V2 does not create speculative jobs or provider spend while the user types. |
| Encrypted full trajectories and failure review | ✅ | Immutable encrypted per-job provider/tool/reply/fold/error capture is implemented. Oversized model-visible events use exact digest-verified encrypted chunks; every async provider HTTP attempt carries its effective request model, exact JSON body, result/error status and duration without an extra DB write. Tool timing/effect evidence and reply disposition are captured. There is no time-based trajectory GC; Chat Clear preserves historical evidence and account deletion is the complete-erasure boundary. Exact-job break-glass inspection is runner-local, decryptable, and durably audited. Provider-backed offline review remains explicit opt-in and side-effect-free; it is analysis, not deterministic replay. |
| Fleet-wide resident-process retirement | ⚠️ | Source and managed topology are complete: hosted supervisors, per-user homes/leases/CLI toolchains, selectors, and the admin rollback flip are removed, and every managed manifest can launch only pooled V2 workers. Live closure still requires deploying the reviewed image to each environment, provisioning the required second production runner failure domain, and verifying that no legacy hosted process remains. The independent user-operated `/v1/chat/*` resident consumer is a separate product path. |

## Current turn shape

```text
claim immediately
  -> load encrypted summary + verbatim uncovered tail
  -> render the complete prompt frontier
  -> provider-native model round (same loop for every model)
  -> execute a requested tool batch
       reads + bounded subagents: parallel
       disjoint workspace writes: conflict-free parallel waves
       external/conflicting mutations: provider-ordered and durability-fenced
       reply: publish immediately, then the loop may continue
  -> fold newly arrived user messages at the round boundary
  -> repeat within round/token/deadline limits
  -> terminal reply or visible terminal failure
```

The retired `planner -> executor -> responder` architecture is not a live V2
path. `planner.py`, `responder.py`, `agent_loop.py`, and `invalidation.py` have
been deleted. `backend/model_api_runtime/v2/tool_loop.py` is the unified loop,
and `tests/test_v2_no_dispatch_tiering.py` prevents the old dispatch split from
returning.

## Tool and lane parity

The model-visible native catalog contains **23** built-in tools: 21 platform
tools plus the synthetic `task` and `reply` loop tools:

- identity read/write;
- memory index/fetch/write/exact search;
- perception snapshot/trend/history;
- screen and photo list/read;
- web search/fetch;
- schedule/cancel wake;
- workspace list/read/write/delete;
- bounded read-only `task` subagents;
- `reply`; and
- eligible user-connected MCP tools, subject to approval, taint, ordering, and
  crash-recovery controls.

Chat image/file reads are internal ingestion capabilities, not additional
model-selectable tools. Images enter the provider request as multimodal content.
Uploaded artifacts are represented through the encrypted virtual workspace;
reading an existing encrypted text view needs no sandbox, while a cache miss
must acquire a configured sandbox before decrypted bytes are materialized or an
untrusted binary is parsed. Runtime V2 does not currently expose a generic shell
or arbitrary code-execution tool. The detailed facade and enclave mapping lives in
[`docs/superpowers/specs/runtime-v2-parity-matrix.md`](superpowers/specs/runtime-v2-parity-matrix.md).

| Lane | Status | Note |
|---|---|---|
| Chat | ✅ | Unified native loop with durable reply/effect handling |
| Manual, heartbeat, scheduled wake | ✅ | Same native loop as chat |
| Screen watch | ✅ | Producer and wake handler are live |
| Maintenance/compaction | ✅ | Encrypted summary compaction path is live |
| Capture | ➖ | Automatic Capture is not a Runtime V2 release requirement and every managed default, including Pre, keeps it off. The dormant implementation remains available behind an explicit deployment and per-user gate. Its correctness path consumes the exact oldest contiguous raw-seq window, seals provider output into an encrypted prepared journal, and atomically commits Memory rows, canonical logs, frontier, and terminal job state under disclosure, consent, generation, and cross-process Memory Garden fences. |
| Memory Dream | ➖ | Native `op/card_ids/result` consolidations map to multi-card supersede actions and pass the real Garden validator, but activation is a separate optional product lane rather than a Runtime V2 release requirement. All managed defaults keep it off. This Dream organizes memory cards and is not runtime failure replay. |
| Genesis import | ✅ | Rehomed under `serve-worker` with a dedicated heartbeat |
| Trajectory review | ✅ | Encrypted capture is always on and retained until account deletion; Chat Clear preserves it. Provider-backed offline review is opt-in/default-off, globally capped, tools-disabled, and has no live effect surfaces; operators have a separate default-off, exact-job, runner-local audited inspector. |

## Incident-hardened guards — ported?

Lane parity above means that work can execute; it does not by itself prove that
the incident-driven interruption, retry, and publication guards from Runtime V1
survived the pooled Runtime V2 rewrite. This table tracks that second dimension
explicitly.

| Guard | Runtime V1 source / incident | Runtime V2 counterpart | Status |
|---|---|---|---|
| Consecutive AI self-wake limit | `tools/chat_resident_consumer.py:527-558`; `ee81d317` (2026-07-21 self-wake loop) | Wake-turn `schedule_wake` effects carry encrypted internal provenance; `jobs_store.reserve_self_wake` persists a per-user streak and idempotent effect decision; `_sink_schedule` rejects the fourth schedule until genuine user input advances. Heartbeat, screen/event, and user/chat scheduling never consume the counter. | ✅ |
| Post-time proactive/chat collision gate | `tools/chat_resident_consumer.py:536,9011-9055,11157-11187`; `0c5daa6b` / `usr_a0b7` duplicate wake+chat reply | `effect_outbox._wake_reply_chat_collision` runs inside the reply publication transaction after the model turn. Fresh user or foreground-agent chat within 90 seconds discards heartbeat/manual/screen-watch replies without folding and retrying. User-requested scheduled reminders retain V1's exemption. | ✅ |
| Provider payment cooldown | V1 600-second cooldown;欠费 retry storm (`usr_d98b`) | `v2_wake_schedule.payment_cooldown_until`, `_WAKE_COOLDOWN_SEC`, and due-user filtering | ✅ |
| Wake job coalescing | `c635d87d` duplicate proactive work | `jobs_store.coalesce_or_insert_on_cursor` and the per-user active-job uniqueness boundary | ✅ |
| Short-horizon proactive failure backoff | `tools/chat_resident_consumer.py:518-589,10898-10910`; `PROACTIVE_FAIL_BACKOFF_*`; transient upstream storms (Seven, 2026-07-16/21) | Runtime V2 persists `proactive_fail_streak`, the genuine-user sequence frontier, and `proactive_backoff_until` in `v2_wake_schedule`. Owned heartbeat/scheduled failures atomically arm a 60s exponential delay capped at 3600s; owned success clears it. Due heartbeat and scheduled-timer scans defer while active, but a newer genuine user row invalidates it immediately. Manual and screen-watch wakes bypass this guard; provider health remains the separate long-horizon circuit. | ✅ |
| Foreground failure notice throttling / background silence | `FOREGROUND_NOTICE_WINDOW_SEC` three-bucket fixed window; `usr_6f5a` | V2 satisfies the required property through architecture rather than copying the timer: `_run_wake` (`worker.py:4519-4549`) and capture/dream dispatch (`worker.py:6432-6493`) terminalize silently; only `lane == "chat"` calls `_surface_terminal_error` (`worker.py:7874-7894`). Repeated background failures create no chat rows/chips; each foreground user turn has at most its own terminal chip and a single overwritten `last_runtime_error`. | ✅ |
| Stale proactive context fallback | `PROACTIVE_STALE_CHAT_FALLBACK_LIMIT`; offline backlog talking past the user | The encrypted summary frontier plus bounded verbatim tail adapts the old fallback to V2's architecture. Residual: the at-most-20-row tail has no freshness annotation, so several-hours-old turns can still read as current. Deferred until user evidence justifies prompt-semantics work. | ⚠️ |
| Stale WHOAMI/content-key rejection | `WHOAMI_STALE_KEYS_MAX_AGE_SEC`; `usr_f13f` replies sealed to a retired key | V2 does not cache consumer WHOAMI keys: reply/tool envelopes resolve the current account/enclave keys at write time. The V1 cache-age mechanism is therefore not applicable. | ➖ |
| Degenerate punctuation fragment drop | `tools/chat_resident_consumer.py:9192-9220,10900-10928,12025-12068`; stream-cut incident `usr_6f5a` emitted punctuation-only bubbles | `worker._is_degenerate_reply` preserves V1's Unicode-category decision. Chat replaces a terminal fragment with the standard fallback and `upstream_unavailable` / `provider_transient` attribution; intermediate fragments are dropped before a later real reply. Wake lanes fail silently with `degenerate_reply_suppressed`, without a bubble or error chip (`worker.py:5143-5163,7335-7359`). | ✅ |
| `verify_ping` garbage collection by source | `7941eeb2` real reply accidentally collected | Shared backend `chat_core._verify_synthetic_ids_to_gc` and `_verify_reply_matches_ping` require `source == "verify_ping"` (`backend/chat/chat_core.py:1003-1033`); both runtimes use this route. | ✅ |
| Reply finalize compare-and-swap | V1 `a5296ef7` finalization race | V2 has the stronger ownership/generation/source-job/seq frontier in `effect_outbox._reply_fence_matches` (`effect_outbox.py:275-519`) plus atomic job completion in `_complete_final_reply_job_on_cursor` (`effect_outbox.py:522-564`). | ✅ |
| Dream execution consent | Dream could run after opt-out during queued work | `_dream_enabled_for_user` reads the current per-user switch and `process_job` re-checks the extraction gate before provider work (`serve_worker.py:257-266`; `worker.py:6453-6483`). | ✅ |
| Screen-watch canonical trigger and consent | Screen-watch queried the wrong proactive control during the V2 rewrite | `_tick_screen_watch_for_user` asks `_wake_decision_for_user(..., trigger="screen_watch")` before enqueue (`serve_worker.py:1727-1782`), preserving independent screen-watch consent. | ✅ |

## Workspace, working memory, and subagents

Runtime V2 exposes a backend-pluggable virtual filesystem. Production stores
file bodies as user+enclave shared encrypted envelopes in PostgreSQL; the
in-memory backend is test-only. `/artifacts` and `/skills` are read-only,
`/workspace` is model-editable with exact revision CAS, and the only editable
Memory path is `/memory/WORKING.md`. That Markdown file is operational scratch
state for plans and continuation, not a replacement or file projection of
Memory Garden's structured semantic cards.

The native `task` tool starts bounded child loops with isolated transcripts and
the same provider route. Children may use approved read tools, but cannot reply
to the user, recurse into more tasks, load user MCP mutations, or perform
platform/workspace writes. Multiple independent tasks can run concurrently and
return bounded results to the parent.

Eager perception grounding contains only fixed-field numeric, boolean, or null
readings. Calendar/reminder titles, app/audio/place/weather text, screen
captions, and photo text are pull-only. After an explicit text-bearing
perception, screen, or photo read, later web, MCP, and `task` tools are removed
for that turn; numeric health snapshot/trend reads remain composable with
outbound tools. Same-batch calls are allowed because their arguments were chosen
before the private read result existed in the model transcript.

## Conversation storage and prompt frontier

V2 does not send ciphertext to the model. Chat content, immutable summary
segments, higher-level checkpoints, and the bounded materialized summary view
are encrypted at rest in database-backed storage. The trusted runtime decrypts
the selected view and verbatim tail in memory, renders ordinary model-readable
messages, and sends those plaintext messages over the user's configured
provider connection.

Every source message must be covered: either by the committed summary watermark
or verbatim in the tail. New compaction batches become exact immutable leaf
segments carrying their source-sequence range and row-count witness. A
checkpoint names the exact ordered child IDs it summarizes; the children remain
stored and immutable. The head CAS binds one encrypted, bounded materialized
prompt view to the exact canonical segment IDs, so a partial or stale writer
cannot silently publish an incomplete history. A pre-segmentation deployed
summary is retained as an encrypted `legacy_opaque` leaf; even an oversized old
blob can be reduced through bounded map/reduce provider calls without requiring
a new chat message.

Coverage is a prompt invariant, never a retention authorization. The durable
`chat_messages` rows and their encrypted attachment bodies are not automatically
deleted at 5,000 rows or after a summary watermark advances. `MAX_CHAT_MESSAGES`
only bounds each process's recent working set; iOS history uses bounded database
pages, and a message body can be fetched by stable id outside that hot window.
Clear Chat is a visibility boundary, not a retention authorization. It is
generation-fenced and atomically moves raw encrypted rows into an immutable
non-prompt archive while removing the summary, chat-derived artifact views,
pending effects/status, and reply cursor, so an old worker cannot resurrect
cleared context. Archived raw chat and R2 bodies, encrypted trajectory/review
evidence, job metadata, aggregate metrics, and prior access audits remain
available for debugging. Memory Garden, Identity, user-authored workspace/
working memory, and schedules also remain; account deletion is the complete
per-user erasure boundary.

The **total prompt frontier** is the complete per-round budget calculation over
the rendered system text, summary, verbatim messages, images, exact tool
schemas, tool transcript, newly folded input, output reserve, and safety
headroom. If required context no longer fits, V2 fails visibly. It does not
silently truncate required history. The hierarchical summary frontier targets a
48,000-character materialized history view, but that storage/maintenance target
does not replace the exact provider/model-specific total prompt calculation.
Checkpoint creation changes only the dynamic summary portion after the stable
system/tool/skill cache boundary.

## Prompt-cache boundaries and live evidence

Tool schemas, runtime policy, and canonical-path-sorted trusted `/skills`
content are rendered deterministically before dynamic summary, tail,
perception, and tool results. Provider adapters place cache controls or cache
points at supported stable boundaries; OpenAI-compatible routes also use a
route-bound cache-affinity key. Cache read/write/miss tokens are normalized
into whole-turn telemetry.

Editable `/memory/WORKING.md` is persistent but pull-only. Production does not
eagerly place it in the prompt or cache prefix: after an explicit
`workspace_read`, later outbound web/MCP/`task` tools are removed for that turn.

The checked-in live canary currently uses OpenRouter with an OpenAI-family model
and an Anthropic-family model. Each model probe places a fresh random nonce near
the front of a long synthetic user prefix, then runs one warm-up plus three
sequential follow-ups on the same account/session. This prevents an earlier CI
run from pre-warming the probe. At least one follow-up must report a cache read
covering the first turn's complete stable prefix; all four turns must stay on one
Feedling route, make exactly one logical model call, make no hidden retry, and
report complete usage/cache telemetry. A cold miss on only the first follow-up
does not fail the deployment because OpenRouter may move to a fallback upstream.
Failures emit content-free per-turn token diagnostics while preserving `NULL`
versus explicit zero. Both model probes run before failures are aggregated.

The canary does **not** yet prove native Anthropic, native Bedrock, or mutation
of the newly added trusted `/skills` prefix in a deployed environment. Those
remain explicit post-deploy canary work rather than inferred success from unit
tests. Editable working memory is intentionally pull-only and therefore is not
an eager prompt-cache boundary.

## Aggregate telemetry and encrypted full trajectories

The current telemetry is valuable. `v2_turn_metrics` is deliberately
content-free and aggregated to one best-effort row per job; the other persisted
records below each capture only one operational slice of the turn.

| Persisted record | What it answers | Scope boundary |
|---|---|---|
| `v2_turn_metrics` | Total prompt/completion/cache tokens, calls, retries, latency, configured provider/model, final status | Aggregate and content-free; failed sends may still have unknown provider usage; account deletion cascades these rows |
| `agent_jobs` and status events | Whether/when a job queued, ran, failed, or expired | Status vocabulary is coarse and original content is intentionally excluded |
| Runtime action digest | Counts and success by tool name | No arguments, results, ordering, or context |
| Encrypted effect outbox | Durable replies and platform mutations | Captures business effects, not read tools or model exchanges |
| MCP mutation frontier | Whether a remote mutation may have an ambiguous outcome | Stores hashes/status, not the remote arguments or result |
| In-process native transcript | Enough ordered tool context for the next model round | Discarded when the turn/process ends; the encrypted trajectory is the durable copy |
| `v2_trajectory_events` | Immutable per-job request/response, provider-attempt, fold, timed tool/effect, reply-disposition, exception, and terminal chronology | Sensitive payload is a user+enclave shared envelope; only fixed ordering/type/size metadata is plaintext |

`GET /v1/admin/v2-metrics.turn_health` derives a bounded, content-free 24-hour
snapshot from jobs, metrics, and trajectory metadata. It includes real
queue/lease expiries, pending age, p95 latency, and complete/partial/missing/gap
counts; jobs with an explicit lifecycle tombstone are excluded from the capture
denominator so intentional erasure cannot masquerade as missing telemetry.

Telemetry is the **odometer/instrument panel**: it tells us how much, how long,
and whether the turn failed. The encrypted trajectory is the **flight
recorder**: it records the exact model-visible causal chronology and explicit
completeness state. Token telemetry alone therefore does not explain a failed
turn. The flight recorder still is not a deterministic replay engine: external
read changes, provider-side state, and fresh model sampling can make exact
re-execution impossible.

Runtime V2 now also writes the flight recorder: each causal event is compressed,
sealed to the user's content key and enclave key in the trusted worker, then
appended under an immutable per-job index. A logical event above the physical
part boundary is stored losslessly as digest-verified encrypted chunks in one
stream transaction and one multi-row INSERT. Provider credentials, HTTP
headers/base URLs, and raw
exception strings are never part of an event. Capture-state metadata is
explicitly `open`, `complete`, `partial`, or `missing` and exposes the terminal
event index, explicit required-or-best-effort `capture_gap` flag, and legacy
truncation bit.
A terminal source job without a terminal event—or with a gap marker—is
`partial`; per-event idempotency preserves the durable prefix.
Attempt-scoped idempotency plus deterministic call-ID child scopes keep parallel
subagent/tool events stable across redelivery. Each child provider round, every
underlying compatibility/transient HTTP attempt, and each parallel call's
start/result/error/duration is captured independently, so one failing sibling
cannot erase evidence already produced by another. Attempt wire evidence is
accumulated in memory and joins the existing encrypted provider response/error
event, avoiding one database write per retry. Attempt `model` is the effective
wire model; aggregate metrics deliberately retain the user-configured model.

One physical part's pre-compression JSON is capped (512 KiB by default, with a
safe 64 KiB batching floor); larger
logical events are exact ordered chunks rather than clipped prefixes. Review
reads at most the newest 256 physical rows and has its own 128 KiB prompt
frontier; an incomplete chunk window is labeled incomplete. Those are analysis
bounds, not retention policies. Raw encrypted event/review content has no
time-based GC and survives Chat Clear; account deletion removes it. The store
has no public plaintext read API and is never injected into
live conversation context. A default-off runner-local inspector can decrypt one
exact user/job pair only after a durable requested-access audit; it records a
success phase before returning plaintext to the controlled invoking terminal.

## Optional or separately gated capabilities

Typing-signal pre-warm is intentionally not a Runtime V2 completion item. The
runtime starts on authenticated Send and does not speculatively create work,
reserve foreground capacity, call a provider, or spend tokens while the user is
typing. Reconsidering a local-only latency experiment later would be a separate
product project with its own benefit and waste measurements.

### Encrypted full trajectories and failure review (lifecycle and inspection implemented; review opt-in)

The existing `dream` lane remains user-memory housekeeping. Encrypted
trajectory capture remains on independently. Provider-backed review defaults
off and requires `FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED=1`; a database-serialized
`FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE` pending+running ceiling (64 by
default when enabled) rejects overflow without creating a runner. Invalid
configuration fails closed, and execution rechecks the flag as a cost kill
switch before provider boundaries. A runner stopped while disabled returns its
review to durable `pending`; the parent reconciler recreates at most 64 missing
runners per tick after re-enable, with database single-flight preventing fleet
duplicates. When enabled and admitted, terminal failed or expired turns enqueue
a distinct low-priority `trajectory_review` lane. It
decrypts the bounded failed trajectory only inside the trusted worker, makes at
most one tools-disabled provider call per attempt, and persists the result as a
second shared encrypted envelope. A failed review is retried at most three
times with bounded exponential backoff; a crashed runner releases its claim
through the ordinary lease reaper. Lease renewal and watchdog progress cover
long decrypt frontiers. Review commit compares the exact captured stream
frontier, so a concurrent or later trajectory append discards/reopens stale
analysis instead of silently blessing an incomplete prefix.

The prohibition on side effects is structural, not prompt-only: this handler
never enters `process_job` or the native tool loop and has no reply callback,
capability/MCP loader, effect outbox, or workspace backend. Review output is not
fed to the user or a later agent turn automatically.

This is analysis, not production retry. The existing effect outbox handles
durable effect recovery. Any future replay mechanism must never resend replies,
rewrite memory/identity/schedules, or repeat remote MCP mutations.

Encrypted trajectory/review content has no time-based GC and Chat Clear
preserves it; account deletion remains the complete per-user erasure boundary.
The runner-local inspector is independently
default-off and requires an exact user/job pair plus a durable operator, reason,
case, and outcome audit. Keep provider review opt-in until its BYOK budget is
approved; capture itself remains available while review is off or globally
capped.

### Fleet-wide resident-process retirement

Fleet-wide hosted retirement is complete in source and deployment topology.
Local, test, pre, and production backend manifests force literal `v2_only`;
their runner manifests contain one `serve-worker` service and no resident
supervisor, per-user child, home, checkpoint, lease, roster, or data volume. The
historically named `feedling-agent-runner` image package now contains only the
Python Runtime V2 worker, so an old hosted process cannot be relaunched from the
new image. Test, pre, and production worker-CVM deploy jobs are mandatory for
hosted changes, and structural tests fail if a retired selector or service
returns.

`resident_cli` remains only as a dormant database fence while a model route is
deleted or replaced and for explicitly independent `/v1/chat/*` consumers. A
hosted send requires the exact `db_action_v2` + `v2` ownership tuple and fails
before persistence otherwise. Deploying the new manifests to each live fleet is
a release operation, not an alternate runtime implementation or rollback path.
The repository requires at least two production worker CVM IDs but currently
records only one provisioned ID, so the production rollout gate intentionally
remains closed until the second independent failure domain exists. Final closure
also requires a live process inventory proving zero old hosted resident
processes. Once provisioned, the production deploy now assigns every runner a
stable inventory-CVM/current-build identity and refuses to start on missing or
mismatched identity inputs. Its post-deploy gate waits beyond old heartbeat
freshness, then requires an exact positive-capacity turn + Genesis pair for
every listed CVM at the deployed build; aggregate liveness, a previous-build
row, an ephemeral identity, a truncated metrics response, or one listed
identity cannot stand in for the fleet.

## Remaining work, in order

1. Run the authoritative concurrent workload on target CVM-class hardware and
   complete the fault/recovery and cohort-soak gates.
2. Soak Pre's independently enabled automatic Memory Capture, while keeping
   Memory Dream disabled; record queue, failure/backoff, cost, and card-quality
   behavior before any production enablement.
3. Wire external polling/alerting to the content-free `turn_health` snapshot
   (queue expiry, terminal error/expiry rate, p95 latency, and unexplained
   trajectory `missing`/`capture_gap`) rather than relying on manual inspection.
4. Extend the live prompt-cache canary to exercise trusted `/skills` mutation
   boundaries and native Bedrock where credentials are available. Editable
   working memory remains pull-only by design rather than an eager cache prefix.
5. Provision the second production runner, deploy the reviewed V2-only images
   across every live environment, and verify zero hosted resident processes.
6. If binary artifact reading is offered on Pre, provision the E2B credential
   and versioned 25 MiB template and approve its data boundary; otherwise leave
   the fail-closed sandbox disabled without blocking text/chat Runtime V2.

The encrypted workspace, artifact materialization boundary, optional E2B
adapter, and bounded subagents are implemented source capabilities. E2B remains
configuration- and policy-dependent because decrypted artifact bytes leave the
Feedling CVM. Generic shell/code execution is still not model-visible; adding it
would require the same sandbox boundary and a separate permission/billing
contract. Do not treat the retired resident CLI's host filesystem access as an
intended hosted capability.

## Traceability guards

At minimum, current-status changes should remain covered by:

- `tests/test_v2_no_dispatch_tiering.py`
- `tests/test_v2_p0_unified_loop.py`
- `tests/test_v2_tool_loop.py`
- `tests/test_v2_tool_loop_mcp.py`
- `tests/test_v2_mixed_tool_dispatch.py`
- `tests/test_provider_client_async.py`
- `tests/test_v2_prompt_cache_key.py`
- `tests/test_provider_prompt_cache.py`
- `tests/test_provider_tools_bedrock.py`
- `tests/test_prompt_cache_canary.py`
- `tests/test_v2_turn_metrics.py`
- `tests/test_v2_summary_store.py`
- `tests/test_v2_summary_watermark_seq.py`
- `tests/test_v2_summary_frontier_unit.py`
- `tests/test_v2_summary_frontier_store.py`
- `tests/test_v2_prompt_invariant.py`
- `tests/test_v2_p0_history_safety.py`
- `tests/test_v2_gc_coverage_gate.py`
- `tests/test_v2_compaction_integration.py`
- `tests/test_v2_extraction_memory_integration.py`
- `tests/test_memory_readside_core.py`
- `tests/test_v2_worker_files.py`
- `tests/test_v2_workspace_db.py`
- `tests/test_v2_workspace_unit.py`
- `tests/test_v2_subagents.py`
- `tests/test_v2_trajectory_db.py`
- `tests/test_v2_trajectory_unit.py`
- `tests/test_v2_trajectory_access_audit.py`
- `tests/test_v2_trajectory_inspect_unit.py`
- `tests/test_v2_metrics_endpoint.py`
- `tests/test_v2_atomic_reply_cursor.py`
- `tests/test_v2_scheduler_wiring.py`
- `tests/test_v2_capture_lifecycle.py`
- `tests/test_v2_capture_batch_protocol.py`
- `tests/test_v2_extraction_lanes.py`
- `tests/test_v2_kill_switch.py`
- `tests/test_v2_e2b_template_extractor.py`
- `tests/test_hosted_runtime_policy.py`
- `tests/test_prod_runner_topology.py`
- `tests/test_no_litellm_anywhere.py`

When architecture or rollout state changes, update this page in the same commit.
