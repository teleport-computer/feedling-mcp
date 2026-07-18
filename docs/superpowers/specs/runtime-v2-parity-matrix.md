# Runtime V2 Capability Facade Mapping

> **Tool-level companion matrix.** This page maps the executable capability
> vocabulary to its framework-neutral facades. For current end-to-end runtime
> status, deferred D items, telemetry-versus-trajectory scope, and deployment
> state, use [`docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`](../../HOSTED_RUNTIME_V2_PARITY_MATRIX.md).
> For operational rollout gates use
> [`deploy/HOSTED_RUNTIME_V2_ROLLOUT.md`](../../../deploy/HOSTED_RUNTIME_V2_ROLLOUT.md).

This appendix maps each current resident/io_cli capability to its existing
framework-neutral core, capability facade, V2 action-type string, and enclave
boundary. It is not the complete runtime acceptance matrix.

| io_cli verb | backend endpoint | existing *_core fn | capability fn | action_type | enclave? |
|---|---|---|---|---|---|
| memory-index | POST /v1/memory/index | memory_core.index | capabilities.memory.index | memory_index | yes (decrypt) |
| memory-fetch | POST /v1/memory/fetch | memory_core.fetch | capabilities.memory.fetch | memory_fetch | yes |
| (none — legacy runtime has no memory search) | (none) | memory_core.index (query-forwarding) | capabilities.memory.search | memory_search | yes (decrypt) |
| (POST /v1/memory/actions) | POST /v1/memory/actions | memory_core.actions | capabilities.memory.write | memory_write | no |
| perception | GET /v1/agent/perception | perception_core.agent_perception_payload | capabilities.perception.snapshot | perception_snapshot | no |
| perception-trend | GET /v1/agent/perception/trend | perception_core.perception_trend_payload | capabilities.perception.trend | perception_trend | no |
| perception-history | GET /v1/agent/perception/history | perception_core.perception_history_payload | capabilities.perception.history | perception_history | no |
| screen-recent | GET /v1/screen/frames | screen_read_core.list_frames | capabilities.screen.recent | screen_recent | no |
| screen-read | GET /v1/screen/frames/{id}/decrypt | screen_read_core.frame_decrypt | capabilities.screen.read | screen_read | yes |
| photo-recent | GET /v1/perception/photos | perception_read_core.photos_recent | capabilities.photo.recent | photo_recent | no |
| photo-read | GET /v1/perception/photo/{id}/content | perception_read_core.photo_content | capabilities.photo.read | photo_read | yes (image) |
| chat-image | GET {ENCLAVE}/v1/chat/history | (none — enclave direct) | capabilities.chat.image_read | chat_image_read (internal only) | yes |
| (uploaded chat file) | GET {ENCLAVE}/v1/chat/history | (none — enclave direct) | capabilities.chat.file_read | chat_file_read (internal only) | yes |
| identity-write | POST /v1/identity/actions | identity_core.run_actions | capabilities.identity.patch | identity_patch | no |
| (GET /v1/identity/get) | GET /v1/identity/get | identity_core.get_identity | capabilities.identity.get | identity_get | no |
| (web search — legacy runtime tool-call, no endpoint) | (none — in-process) | model_api_runtime.tools.web_search_duckduckgo | capabilities.web.search | web_search | no |
| (web fetch — no legacy endpoint) | (none — in-process) | model_api_runtime.tools._strip_html_text (+ direct httpx.get) | capabilities.web.fetch | web_fetch | no |
| (scheduled wake) | POST /v1/proactive/scheduled/actions | proactive.scheduled_wake_v2 | capabilities.wake.schedule | schedule_wake | no |
| (cancel scheduled wake) | POST /v1/proactive/scheduled/actions | proactive.scheduled_wake_v2 | capabilities.wake.cancel | cancel_wake | no |

Notes:
- `chat_image_read` and `chat_file_read` are registered worker capabilities but
  intentionally have no model-facing schemas. The worker injects images as
  multimodal content and extracted supported-file text into the prompt. The
  model sees neither raw base64 nor arbitrary local filesystem access.
- The model-visible catalog adds a synthetic `reply` tool handled directly by
  the unified loop. Eligible per-user MCP tools are appended dynamically; they
  are not rows in this static platform-facade table.
- `recent_chat_digest` (spec §4.3 read word list) is **not** a capability — it is a
  deterministic worker-side transform over decrypted messages (no endpoint, no LLM).
- Enclave-bound rows (memory index/fetch, screen read, photo read w/ image,
  chat-image, and uploaded chat file)
  must be wrapped in the shared `ENCLAVE_SEMAPHORE` by the V2 worker (spec §11 R3).
- **Scheduling vocabulary is now executable:** the production native catalog exposes
  `schedule_wake` and `cancel_wake`, both are registered capabilities, and their durable
  effects are applied by the scheduled-wake sink. The obsolete `schedule_followup`
  alias remains absent so the model cannot select a verb the runtime cannot execute.
- **`capture_memory` — pure removal, not deferred:** it duplicated `memory_write` (the
  real, already-registered memory-write capability) and never had its own capability
  fn. It is absent from the provider-native tool catalog; do not reintroduce it or add a
  `capture_memory` capability — write through `memory_write` instead.
- **`sleep` is a wake-lane outcome, not a missing capability:** an empty final reply in
  the unified native loop means the weak wake naturally sleeps. It is not exposed as a
  foreground write tool and is not routed through the retired `rule_plan` path.
- **Task 5 — `web_search`/`web_fetch` (merge-review condition 4b):** today's legacy
  runtime has web access; the V2 capability layer was missing it entirely. Legacy web
  search is a **keyless** DuckDuckGo HTML scrape in
  `model_api_runtime/tools.py::web_search_duckduckgo` (no provider, no API key, no
  enclave). `capabilities.web.search` and `capabilities.web.fetch` are thin facades
  over it (+ a direct `httpx.get` + `tools._strip_html_text` for fetch) that add:
  input guards (empty query/url → `capability_invalid_input`), sensitive-query refusal
  via `tools.query_has_sensitive_data` (refused before the network call, not after),
  and size caps on untrusted external content (`fetch` truncates the raw HTML body to
  ~40KB before stripping; both cap final output via `errors.cap_data`/`errors.cap_text`/
  `errors.cap_list`). Both are read-only (`READ_ACTIONS`, never enclave-bound) and are
  in the one provider-native tool catalog used by every BYOK model.
- **Task 6 — `memory_search` (merge-review condition 4c): better than parity, not just
  parity.** The legacy runtime has no memory search at all — `memory-index`/`memory-fetch`
  are the only read verbs, and neither takes a keyword query. `memory_core.index` already
  forwards a `query` field from `params` to the enclave read-side, which does the actual
  keyword/relevance matching over decrypted card text; the Python side never touches
  matching logic. `capabilities.memory.search` is a **thin alias of `index`** that (a)
  requires a non-empty `query` (empty/missing → `capability_invalid_input`, no enclave
  call) and (b) otherwise forwards straight through to `memory_core.index` with the
  query merged into params. Read-only (`READ_ACTIONS`), enclave-bound like `memory_index`/
  `memory_fetch`, and in the unified provider-native tool catalog (preferred over
  `memory_index` when the user wants to find something specific). Query recall is exact
  across every eligible card: `FEEDLING_MEMORY_READSIDE_HARD_MAX` bounds each ordered
  enclave page, not the searchable corpus, so an old/low-score match after the first
  page is still inspected.
