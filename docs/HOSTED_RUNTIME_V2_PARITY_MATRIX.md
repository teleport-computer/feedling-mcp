# Hosted Runtime V2 — Current Parity and Completion Matrix

> **CURRENT SOURCE OF TRUTH — 2026-07-18.** This page describes the current
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

## Original V2 vision

| Requirement | Current status | Evidence and remaining gap |
|---|---|---|
| No silent wedges | ✅ | Admission checks the turn-worker heartbeat; pending jobs have queue deadlines and a reaper; terminal failures publish `error` status and `last_runtime_error`; the worker contains turn exceptions; provider I/O is async. A dead pool must fail visibly rather than leave a message in `processing` forever. |
| Full conversation, not a fixed message window | ⚠️ | The prompt is built from an encrypted, append-only itemized summary plus a verbatim tail. Raw encrypted chat rows and attached R2 bodies are retained independently of compaction; the former 5,000-row value now bounds only the process hot window. The summary itself still grows forever; immutable encrypted summary segments and higher-level checkpoints are the remaining long-horizon task. |
| One native agent loop for every model | ✅ | Chat and wake use the same in-process provider-native tool loop. There is no `official`/`rule` tier, planner, or separate responder. A model that does not call a tool naturally returns once; malformed tool output gets bounded compatibility handling, not pre-assigned model routing. |
| Reply inside the loop; eager late-message folding | ✅ | `reply` is a loop tool, so the model may acknowledge the user and continue working. New user messages are claimed without debounce and folded at every round boundary. |
| Parallel tool use | ✅ | A provider turn may request a batch of tools. Independent read tools execute with bounded parallelism; writes and externally mutating MCP calls remain ordered/serialized for safety. |
| Executable action vocabulary | ✅ | The exposed native catalog maps to registered executable capabilities. Scheduling, web search/fetch, and exact memory search are present; obsolete planner-only `sleep`/`capture_memory` vocabulary is absent. |
| One deployment topology | ✅ | Local, test, pre, and production hosted model-API deployments are `v2_only`. A bounded `serve-worker` pool runs in the runner CVM, separate from the main backend/enclave CVM; there is no hosted per-account runtime flip. |
| Prompt caching and cache telemetry | ✅ | Provider-aware caching and per-turn cache-token telemetry are live. A route-bound two-request cache-hit canary has succeeded on Pre. |
| Tokens/turn and admission ceiling | ✅ | Whole-turn token/call/latency metrics and an admission ceiling are implemented. The offline token regression gate is live. |
| Concurrent CVM-class load proof | ⚠️ | The harness exists, but the authoritative concurrent run on the target CVM class remains an operational gate. |
| Typing-signal pre-warm | ❌ | Not implemented. See the definition below. |
| Encrypted full trajectories and failure replay | ❌ | Aggregate telemetry and durable effects exist, but the complete ordered model/tool execution is not persisted. See the distinction below. |
| Fleet-wide resident-process retirement | ✅ | Hosted resident source, supervisor services, per-user homes/leases/CLI toolchain, rollout selectors, and the admin rollback flip are removed. Every managed environment can launch only the pooled V2 worker. The independent user-operated `/v1/chat/*` resident consumer remains a separate product path and cannot be selected for hosted accounts. |

## Current turn shape

```text
claim immediately
  -> load encrypted summary + verbatim uncovered tail
  -> render the complete prompt frontier
  -> provider-native model round (same loop for every model)
  -> execute a requested tool batch
       reads: bounded parallelism
       mutations: ordered and durability-fenced
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

The model-visible native catalog contains 17 static platform tools plus the
synthetic `reply` tool:

- identity read/write;
- memory index/fetch/write/exact search;
- perception snapshot/trend/history;
- screen and photo list/read;
- web search/fetch;
- schedule/cancel wake;
- `reply`; and
- eligible user-connected MCP tools, subject to approval, taint, ordering, and
  crash-recovery controls.

Chat image/file reads are internal ingestion capabilities, not model-selectable
tools. Images enter the provider request as multimodal content. Supported
uploaded text/PDF/DOCX/XLSX files are decrypted, text-extracted, and injected by
the worker (up to the configured file limit); this is not arbitrary filesystem
or shell access. The detailed facade and enclave mapping lives in
[`docs/superpowers/specs/runtime-v2-parity-matrix.md`](superpowers/specs/runtime-v2-parity-matrix.md).

| Lane | Status | Note |
|---|---|---|
| Chat | ✅ | Unified native loop with durable reply/effect handling |
| Manual, heartbeat, scheduled wake | ✅ | Same native loop as chat |
| Screen watch | ✅ | Producer and wake handler are live |
| Maintenance/compaction | ✅ | Encrypted summary compaction path is live |
| Capture | ⚠️ | The real parser now emits validator-complete encrypted actions (`type`, `occurred_at`, ranking/source metadata), non-empty captures persist, and a rejected write fails the job rather than being marked completed. Rollout remains default-off pending lifecycle soak. |
| Memory Dream | ⚠️ | Native `op/card_ids/result` consolidations now map to multi-card supersede actions and pass the real Garden validator. Rollout remains default-off pending lifecycle soak; this Dream organizes memory cards and is not runtime failure replay. |
| Genesis import | ✅ | Rehomed under `serve-worker` with a dedicated heartbeat |

## Conversation storage and prompt frontier

V2 does not send ciphertext to the model. Chat content and the append-only
summary are encrypted at rest in database-backed storage. The trusted runtime
decrypts the selected summary and verbatim tail in memory, renders ordinary
model-readable messages, and sends those plaintext messages over the user's
configured provider connection.

Every source message must be covered: either by the committed append-only
summary watermark or verbatim in the tail. Compaction appends itemized facts;
it does not rewrite the full conversation into a new lossy summary.

Coverage is a prompt invariant, never a retention authorization. The durable
`chat_messages` rows and their encrypted attachment bodies are not automatically
deleted at 5,000 rows or after a summary watermark advances. `MAX_CHAT_MESSAGES`
only bounds each process's recent working set; iOS history uses bounded database
pages, and a message body can be fetched by stable id outside that hot window.
Only explicit user/account deletion removes source chat history.

The **total prompt frontier** is the complete per-round budget calculation over
the rendered system text, summary, verbatim messages, images, exact tool
schemas, tool transcript, newly folded input, output reserve, and safety
headroom. If required context no longer fits, V2 fails visibly. It does not
silently truncate required history. The remaining long-horizon design is to
replace one ever-growing summary blob with immutable encrypted segments and
append-only higher-level checkpoints while preserving this coverage invariant.

## Telemetry is not a full trajectory

The current telemetry is valuable. `v2_turn_metrics` is deliberately
content-free and aggregated to one best-effort row per job; the other persisted
records below each capture only one operational slice of the turn.

| Persisted today | What it answers | Why it is not a full trajectory |
|---|---|---|
| `v2_turn_metrics` | Total prompt/completion/cache tokens, calls, retries, latency, provider/model, final status | No per-round request, response, tool I/O, provider-attempt lineage, or exact context; failed sends may have unknown token usage |
| `agent_jobs` and status events | Whether/when a job queued, ran, failed, or expired | Status vocabulary is coarse and original content is intentionally excluded |
| Runtime action digest | Counts and success by tool name | No arguments, results, ordering, or context |
| Encrypted effect outbox | Durable replies and platform mutations | Captures business effects, not read tools or model exchanges |
| MCP mutation frontier | Whether a remote mutation may have an ambiguous outcome | Stores hashes/status, not the remote arguments or result |
| In-process native transcript | Enough ordered tool context for the next model round | Discarded when the turn/process ends |

Telemetry is the **odometer/instrument panel**: it tells us how much, how long,
and whether the turn failed. A trajectory is the **flight recorder**: it tells
us exactly what happened, in what order, with which context and tool inputs and
outputs. Token telemetry therefore does not make a failed turn replayable.

A complete encrypted trajectory store still needs immutable ordered records for
the rendered context/version of every provider call, offered tool schemas,
provider attempts and responses, tool call IDs/arguments/results/timing,
late-message folds, replies and effects, retry/error/stop reasons, and explicit
capture-completeness metadata. It must exclude provider secrets and have
content-equivalent at-rest protection, retention, consent, and access controls.
With the current external database that means application envelope encryption;
if the store moves inside the trusted CVM, storage-adapted full-disk encryption
may provide that boundary instead.

## Deferred D items, precisely defined

### Typing-signal pre-warm

This is a latency optimization triggered by an authenticated, short-lived iOS
“user started typing” event before Send. It may warm deterministic work such as
worker capacity/heartbeat checks, database/enclave reads, summary decryption,
memory indexes, provider routing configuration, and HTTP/TLS connections. The
speculative state must have a short TTL, be invalidated on conversation/config
version drift, remain process-local, never occupy reserved foreground capacity,
and fall back normally on a miss.

It must not create a chat row/job/reply, execute a tool, call the model, reserve
a slot indefinitely, or incur token billing before Send. In particular, a
remote provider prompt cache normally cannot be populated without a provider
request; provider-side speculative calls require a separate explicit budget and
waste policy. The first implementation should warm only local/TEE/network work
and preserve ordinary prompt-cache affinity for real turns.

### Encrypted full trajectories and failure-replay Dream lane

The current Dream lane is user-memory housekeeping: it consolidates memory
cards and does not run the conversation agent or write chat. Failure replay is
a separate offline evaluation lane built on the future encrypted trajectory
store. It would select a failed/anomalous turn, decrypt it only inside the
trusted runtime, reconstruct the historical context, and disable every external
side effect. A deterministic runtime replay can reuse the recorded provider and
tool outputs; an evaluation of a new prompt/model may make a fresh explicitly
budgeted model call while replaying recorded or mocked read-tool results. Both
modes suppress every write. Reviewed cases could become versioned regression
fixtures.

This is analysis, not production retry. The existing effect outbox handles
durable effect recovery; failure replay must never resend replies, rewrite
memory/identity/schedules, or repeat remote MCP mutations.

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

## Remaining work, in order

1. Run the authoritative concurrent workload on target CVM-class hardware and
   complete the fault/recovery and cohort-soak gates.
2. Add immutable encrypted summary segments and higher-level checkpoints before
   a real conversation reaches the total prompt frontier.
3. Design and instrument safe typing pre-warm; measure first-request/first-token
   p50/p95 and wasted-prewarm rate.
4. Implement encrypted full trajectories with explicit completeness, retention,
   consent, and access controls.
5. Add the side-effect-disabled failure-replay evaluation lane on top of those
   trajectories.

Generic local file/bash access, remote artifact download, and an on-demand
artifact sandbox are separate harness-expansion decisions, not unfinished
claims in the original V2 parity contract. Do not accidentally treat the
resident CLI's sandbox-bypassed local filesystem access as an intended hosted
capability.

## Traceability guards

At minimum, current-status changes should remain covered by:

- `tests/test_v2_no_dispatch_tiering.py`
- `tests/test_v2_p0_unified_loop.py`
- `tests/test_v2_tool_loop.py`
- `tests/test_v2_tool_loop_mcp.py`
- `tests/test_v2_mixed_tool_dispatch.py`
- `tests/test_provider_client_async.py`
- `tests/test_v2_prompt_cache_key.py`
- `tests/test_v2_turn_metrics.py`
- `tests/test_v2_summary_store.py`
- `tests/test_v2_summary_watermark_seq.py`
- `tests/test_v2_prompt_invariant.py`
- `tests/test_v2_p0_history_safety.py`
- `tests/test_v2_gc_coverage_gate.py`
- `tests/test_v2_compaction_integration.py`
- `tests/test_v2_extraction_memory_integration.py`
- `tests/test_memory_readside_core.py`
- `tests/test_v2_worker_files.py`
- `tests/test_v2_atomic_reply_cursor.py`
- `tests/test_hosted_runtime_policy.py`
- `tests/test_hosted_resident_retirement.py`
- `tests/test_no_litellm_anywhere.py`

When architecture or rollout state changes, update this page in the same commit.
