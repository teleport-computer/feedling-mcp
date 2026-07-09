# Runtime V2 Parity Matrix

Phase 0 deliverable — the acceptance checklist mapping each current resident/io_cli
capability to its existing framework-neutral core, the new capability facade
function, the V2 action-type string, and whether it hits the enclave.

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
| chat-image | GET {ENCLAVE}/v1/chat/history | (none — enclave direct) | capabilities.chat.image_read | chat_image_read | yes |
| identity-write | POST /v1/identity/actions | identity_core.run_actions | capabilities.identity.patch | identity_patch | no |
| (GET /v1/identity/get) | GET /v1/identity/get | identity_core.get_identity | capabilities.identity.get | identity_get | no |
| (web search — legacy runtime tool-call, no endpoint) | (none — in-process) | model_api_runtime.tools.web_search_duckduckgo | capabilities.web.search | web_search | no |
| (web fetch — no legacy endpoint) | (none — in-process) | model_api_runtime.tools._strip_html_text (+ direct httpx.get) | capabilities.web.fetch | web_fetch | no |

Notes:
- `recent_chat_digest` (spec §4.3 read word list) is **not** a capability — it is a
  deterministic worker-side transform over decrypted messages (no endpoint, no LLM).
- Enclave-bound rows (memory index/fetch, screen read, photo read w/ image, chat-image)
  must be wrapped in the shared `ENCLAVE_SEMAPHORE` by the V2 worker (spec §11 R3).
- **Deferred to subproject D (scheduling/proactive) — Task 4 vocab reconcile:**
  `schedule_followup`, `schedule_wake`, `cancel_wake` were in the planner's
  `_WRITE_ACTIONS` LLM vocabulary but have **no** `registry.CAPABILITIES` entry — the
  executor silently `skipped` them (never ran, never surfaced), so the planner was
  promising scheduling the foreground chat path could never deliver. They are removed
  from the planner vocabulary/prompt here; real scheduling/wake capabilities belong to
  subproject D, out of scope for this step.
- **`capture_memory` — pure removal, not deferred:** it duplicated `memory_write` (the
  real, already-registered memory-write capability) and never had its own capability
  fn. Removed from the planner vocabulary; do not reintroduce it or add a
  `capture_memory` capability — write through `memory_write` instead.
- **`sleep` is a deterministic wake-lane control signal, not a foreground chat
  action:** it is emitted only by `rule_plan`'s non-chat/no-input WAKE branch (never
  by the official LLM planner's vocabulary, and never by the chat-lane rule path). The
  executor treats it like any other non-capability action — gracefully `skipped`, not
  run, not a failure. Interpreting "sleeping" (deciding not to wake/notify) is a
  subproject D concern.
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
  in the planner's `_READ_ACTIONS`/prompt vocabulary so the official BYOK planner may
  emit them.
- **Task 6 — `memory_search` (merge-review condition 4c): better than parity, not just
  parity.** The legacy runtime has no memory search at all — `memory-index`/`memory-fetch`
  are the only read verbs, and neither takes a keyword query. `memory_core.index` already
  forwards a `query` field from `params` to the enclave read-side, which does the actual
  keyword/relevance matching over decrypted card text; the Python side never touches
  matching logic. `capabilities.memory.search` is a **thin alias of `index`** that (a)
  requires a non-empty `query` (empty/missing → `capability_invalid_input`, no enclave
  call) and (b) otherwise forwards straight through to `memory_core.index` with the
  query merged into params. Read-only (`READ_ACTIONS`), enclave-bound like `memory_index`/
  `memory_fetch`, and in the planner's `_READ_ACTIONS`/prompt vocabulary (preferred over
  `memory_index` when the user wants to find something specific).
