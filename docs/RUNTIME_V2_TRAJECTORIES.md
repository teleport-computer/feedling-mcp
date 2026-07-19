# Runtime V2 encrypted trajectories

Runtime V2 stores a durable flight recorder separately from aggregate
`v2_turn_metrics`. The metric row answers how much and how long; the trajectory
answers what happened and in which causal order.

## Storage and encryption

`v2_trajectory_events` is ordered by `(job_id, event_index)`. The only plaintext
columns are tenant/job identity, an event kind, an idempotency key, size and
legacy truncation metadata, and timestamps. The sensitive document is JSON
serialized, zlib-compressed, and passed to
`core_envelope._build_shared_envelope_for_store` inside the trusted worker before
the append. PostgreSQL receives only the resulting shared envelope; the schema
and store reject extra envelope keys that could become an accidental plaintext
sibling.

Event updates are rejected by a database trigger. Foreign-key cascades still
allow explicit job/account deletion. There is no public plaintext trajectory
read endpoint and trajectory data is not copied to `runtime_state` or ordinary
logs.

The default pre-compression limit is 512 KiB per **physical encrypted part**.
An oversized logical event is preserved exactly as digest-verified ordered
chunks and all chunks append in one stream transaction and one multi-row INSERT.
JSON conversion, compression, and envelope sealing run off the shared asyncio
loop. Accepted production event values are serialized without a nesting-depth
omission rule; an unsupported non-JSON value fails capture visibly instead of
being replaced by a silent placeholder. `FEEDLING_V2_TRAJECTORY_EVENT_MAX_BYTES`
may tune the physical-part cap from the safe 64 KiB batching floor up to 900
KiB. The `truncated` column remains
readable for older bounded rows, but the production recorder writes exact parts
with `truncated=false`.

Absence of `turn_terminal` is the completeness signal for a process that died
after earlier events had already committed. If any required or post-effect
best-effort append fails, the recorder emits a content-free encrypted
`capture_gap` marker before the terminal event; a terminal plus that marker
remains `partial`, never `complete`. The content-free capture-state query makes
this explicit as `open`,
`complete`, `partial`, or `missing`, and also reports the source job status,
terminal event index, physical event count, last index, gap flag, and whether a
legacy event was truncated. A terminal source job without a `turn_terminal`
event is likewise `partial`.

## Captured boundaries

The unified Chat and wake loop awaits an encrypted append for:

- each rendered provider request and offered tool catalog;
- each provider response or exception;
- every underlying async HTTP compatibility/transient attempt, including the
  exact JSON wire body, effective request provider/model, status or normalized
  error class, fallback code, ordinals, and monotonic duration;
- round-boundary late-message folds;
- planned tool batches and returned results;
- per-call start/result/error evidence and duration for parallel platform, MCP,
  and subagent work (so a failed sibling does not erase settled evidence), with
  durable platform/MCP effect disposition attached to the model-visible result;
- every provider round inside a bounded subagent loop;
- intermediate and final planned replies plus reply-effect disposition/duration;
  and
- fallback, supersession, exhaustion, exception, and terminal events.

Compaction and memory extraction capture the same provider request/response/
error and per-attempt evidence. Attempt tracing is accumulated in memory and
folded into the already-existing encrypted response/error event, so retries do
not add trajectory database round trips. Provider credentials, HTTP headers,
route base URLs, and raw exception strings are deliberately never serialized;
provider errors retain only their class and a normalized runtime failure code.
The attempt's `model` is the effective wire model. Aggregate `v2_turn_metrics`
continues to record the user's configured provider/model; an OpenRouter-selected
upstream vendor is unknown unless the provider exposes it explicitly.

Idempotency keys include the persisted source-job attempt identity. Parallel
tool and subagent work then uses a deterministic call-ID scope with its own local
ordinal, so scheduler interleaving cannot remap child events on redelivery. An
ambiguous append acknowledgement retries the same scope/ordinal key; a genuinely
new job attempt gets a distinct immutable event series.

Tool `duration_ms` starts after the durable start event and covers dispatcher
work plus any synchronous durable-effect confirmation. Bounded subagents are
dispatched as one batch, so each child result currently carries that batch's
observed wall duration rather than a provider-only per-child duration. Provider
attempt duration separately measures the individual HTTP attempt.

## Failure review lane

Trajectory capture is always on, but provider-backed failure review is
fail-closed and **off by default**. It runs only when
`FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED=1` is explicitly configured with a valid
`FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE` fleet ceiling (default 64 when
enabled; valid range 1–10,000). A database transaction advisory lock makes the
pending-plus-running count and admission insert atomic across every worker.
When review is disabled, misconfigured, or at the ceiling, failed jobs retain
their encrypted trajectory but create neither a review request nor a provider
runner. The enable flag is also checked at execution fences, so turning it off
stops queued work before its next provider call. A disabled in-flight runner
returns its durable request to `pending` without spawning a successor. After
re-enable, the parent process reconciles at most 64 pending-review users per
sweep (every 60 seconds by default) and idempotently recreates one runner per
user; the partial active-job unique index prevents duplicate runners across
worker CVMs.

When enabled and admitted, a failed or expired source job transactionally
creates one `v2_trajectory_reviews` request and ensures a low-priority
`trajectory_review` runner job. A runner claims the oldest request, decrypts at
most the newest 256 events, and builds at most a 128 KiB review prompt. It makes
one provider call with `tools=None`, then encrypts the response into
`review_envelope`. Failures retry at most three times; if the runner process
dies, the ordinary job lease reaper releases the review claim and enqueues a
successor atomically. Provider failures use bounded exponential backoff. Review
runners renew their lease and report decrypt/provider progress to the process
watchdog just like live turns.

Review completion is fenced by the trajectory stream's exact event frontier.
If another event lands after analysis began, the stale analysis is discarded
and a successor reviews the advanced frontier. An event that arrives just after
a completed review atomically reopens that review if admission is available;
otherwise it invalidates the stale analysis without calling the provider. Thus
a late terminal event cannot leave an older prefix marked fully reviewed.

This is offline analysis, not production retry or deterministic replay. The
encrypted event stream is an exact record of model-visible inputs and accepted
provider wire attempts, but time-varying external reads, model sampling, and
intentional normalization of tool results to exactly what the next model round
saw mean it cannot promise byte-for-byte re-execution of external systems. The
handler does not call
`process_job` or `run_tool_loop`, and receives no reply callback, effect outbox,
platform capability dispatcher, MCP loader, Memory writer, schedule writer, or
workspace backend. Its encrypted output is not added to Chat, Memory Garden,
working memory, or later prompts automatically.

There is deliberately no time-based trajectory/review retention job yet;
encrypted rows survive until their owning job/account is explicitly deleted.
`DELETE /v1/chat/history` clears the transcript and every live derived prompt
surface, but deliberately retains these encrypted flight-recorder rows as
telemetry; they are never reintroduced into conversation context. Account reset
or explicit job deletion is the complete-erasure boundary. Production review
should remain opt-in until the product has chosen a BYOK cost budget and
retention/GC policy. Disabling review never disables capture.
