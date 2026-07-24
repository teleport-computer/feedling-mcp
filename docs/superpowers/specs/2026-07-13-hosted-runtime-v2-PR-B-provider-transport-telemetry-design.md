# Hosted Runtime V2 — PR B: Provider Transport / Telemetry — Design

**Status:** LANDED / HISTORICAL DESIGN RECORD
**Depends on:** PR A (generation-fenced effect foundation, merged @ `d7e19a9`)
**Directive:** @sxysun 4-PR next round (PR A control plane · **PR B transport/telemetry** · PR C unified native tool loop · PR D pool/history safety). Product hard decision: NO official/rule behavior tiering by provider/model — all models go through ONE provider-native tool loop (the loop itself is PR C; PR B ships the transport it needs).

> **Not to be confused with** the `litellm-removal-pi-native-providers` spec/plan (`docs/superpowers/…2026-07-0{7,8}…`). That targets `backend/agent_runtime/` CVM driver selection + deleting a subprocess LiteLLM proxy; it is 0% implemented, disjoint from, and non-blocking for this PR. `backend/provider_client.py` already calls all four providers natively (no LiteLLM in its path).

---

## Goal

Turn `backend/provider_client.py` from a **text-only** transport into a **normalized, tool-capable** transport, and make per-turn telemetry a **single idempotent whole-turn metric per job**. Everything is **additive**: existing text-only callers (`responder`, `planner`, `extraction`, `compaction`, `hosted/turn`, `genesis/llm_client`) keep working unchanged — they pass no `tools`, and `tool_calls` comes back empty.

**Delivery boundary (mirrors PR A):** PR B ships the transport codecs + the whole-turn metric and proves them with codec round-trip tests. It wires **no live tool producer** into the turn loop, removes **no** `is_official`/`rule_plan` dispatch, and adds **no** real tool-execution loop — those are PR C. Acceptance is proven at the codec boundary, not end-to-end through a live loop.

## Current state (grounding — verified)

- **Single transport:** `backend/provider_client.py` (~1295 lines). Dispatch on `provider` at `chat_completion` (`:1015`) / `chat_completion_async` (`:1180`): `anthropic`→`_chat_completion_anthropic` (`:881`), `gemini`→`_chat_completion_gemini` (`:945`), `openai`+reasoning-id→`_chat_completion_openai_responses` (`:705`), else→`_chat_completion_openai_compatible` (`:834`). All four are **native httpx** calls to the real provider endpoint.
- **No tool support anywhere:** none of the 4 wire builders accept `tools`; none of the 4 reply extractors read tool-call blocks (they pull text only). Confirmed by grep (zero `tool_calls`/`tools=`/`tool_result`/`function_call` in the module).
- **Async is uneven:** only OpenAI-compat is natively async; Anthropic/Gemini/Responses bridge through `anyio.to_thread.run_sync(chat_completion, …)` (`:1199-1210`) — fundamentally sync.
- **Usage not normalized:** each wire returns the provider's raw usage blob; `responder.py:165-169` reads only OpenAI keys (`prompt_tokens`/`completion_tokens`), so Anthropic (`input_tokens`/`output_tokens`) and Gemini (`promptTokenCount`/`candidatesTokenCount`) usage silently becomes NULL in the metric.
- **Telemetry:** `v2_turn_metrics` (migration `0017`) is append-only, **not idempotent** (no unique key), and `jobs_store.record_turn_metric` (`jobs_store.py:677`) is a plain INSERT fired **once**, only after a successful `responder.respond()` (`worker.py:904`). Planner/extraction/compaction LLM calls and **all failed turns** (mark_failed paths) record nothing.
- **Load-test gate** (`scripts/loadtest/collect.py`, `run_loadtest.py`) already queries `v2_turn_metrics` — so this PR fixes the **write** side; the read side already points at the right table.
- **PR C seam (do NOT touch):** `planner.plan()` (`planner.py:107`) branches on injected `is_official` → `rule_plan` (no LLM) / `official_plan` (LLM). PR B stays transparent to this — it only changes what `provider_client` returns (additively).

---

## Components

### B1 — Normalized types (`backend/provider_types.py`, new; pure)

Lives **alongside `provider_client.py` at top-level `backend/`** (not under `model_api_runtime/v2/`), because `provider_client` is a top-level module shared by v2 AND legacy callers (`hosted/turn.py`, `genesis/llm_client.py`); a top-level module must not reach up into the `v2/` subpackage. Pure — no `hosted`/`agent_runtime`/`db` imports. Plain frozen dataclasses:

```python
@dataclass(frozen=True)
class ToolSpec:      name: str; description: str; parameters: dict   # parameters = JSON Schema object
@dataclass(frozen=True)
class ToolCall:      id: str; name: str; args: dict; args_raw: str = ""; args_ok: bool = True
@dataclass(frozen=True)
class ToolResult:    call_id: str; content: str
@dataclass(frozen=True)
class Usage:         prompt_tokens: int | None; completion_tokens: int | None; total_tokens: int | None
@dataclass(frozen=True)
class ProviderResponse:  text: str; tool_calls: list[ToolCall]; usage: Usage; raw: dict
```

`ToolCall.args_ok=False` + `args_raw` when the provider emitted unparseable JSON arguments — decode never raises; the caller (PR C) decides fallback.

### B2 — Per-wire tool codecs (in `provider_client.py`, extend each of the 4 wire handlers)

Each wire gets an **encode** (normalized `tools`/`tool_results` → wire request) and a **decode** (wire response → `ProviderResponse`). Kept as pure functions so sync and async share them.

| wire | tools → request | tool_call ← response | tool_result → request |
|---|---|---|---|
| OpenAI Chat | `tools:[{type:"function",function:{name,description,parameters}}]` | `choices[0].message.tool_calls[]` (`{id,function:{name,arguments}}`) | prior turn as `{role:"tool",tool_call_id,content}` |
| OpenAI Responses | `tools:[{type:"function",name,description,parameters}]` | output items `{type:"function_call",call_id,name,arguments}` | input item `{type:"function_call_output",call_id,output}` |
| Anthropic | `tools:[{name,description,input_schema}]` | content blocks `{type:"tool_use",id,name,input}` | user content block `{type:"tool_result",tool_use_id,content}` |
| Gemini | `tools:[{functionDeclarations:[{name,description,parameters}]}]` | `candidates[].content.parts[].functionCall{name,args}` | `parts[].functionResponse{name,response}` |

**Gemini has no call id.** Decode synthesizes a deterministic id `call_{index}_{name}` (index = position among functionCall parts in that response). The turn keeps a name↔synthetic-id map so that when encoding tool_results back, Gemini's `functionResponse` (which keys on **name**, not id) is reconstructed from the id the caller holds. Collisions (same tool name twice in one response) are disambiguated by index in the id, and the result-encode matches on the map, not on name alone. All other wires carry a real provider id through unchanged.

`args` is always decoded to a `dict`; OpenAI/Responses `arguments` (a JSON string) and Anthropic `input` (already an object) and Gemini `args` (already an object) all normalize to `dict`, with `args_ok=False`/`args_raw` set on a JSON parse failure.

### B3 — Native async for Anthropic / Gemini / Responses (`provider_client.py`)

Replace the three `anyio.to_thread.run_sync` bridges (`:1199-1210`) with real `httpx.AsyncClient` calls. Factor each wire into pure `_build_<wire>_payload(...)` + `_parse_<wire>_response(...) -> ProviderResponse` shared by the sync and async entry points (the pattern OpenAI-compat already uses, per `:769-772`). Sync `chat_completion` and the legacy sync callers keep working; async callers (the v2 worker path) now get a real async round-trip with no thread-pool hop.

### B4 — Usage normalization (`provider_client.py` + read-side fixes)

`_normalize_usage(provider: str, raw: dict|None) -> Usage` maps: OpenAI/compat/Responses `prompt_tokens`/`completion_tokens`/`total_tokens`; Anthropic `input_tokens`/`output_tokens` (total = sum); Gemini `promptTokenCount`/`candidatesTokenCount`/`totalTokenCount`. Each wire's decode populates `ProviderResponse.usage` via this. Fix the read sites (`responder.py:165`, and planner/extraction/compaction where they read usage) to consume the normalized `Usage`, closing the Anthropic/Gemini NULL-usage gap.

### B5 — Idempotent whole-turn metric (migration `0029` + `jobs_store` + worker)

**Migration `0029_v2_turn_metrics_whole_turn`:** on `v2_turn_metrics` add columns `model_calls INT NOT NULL DEFAULT 0`, `retries INT NOT NULL DEFAULT 0`, `failed BOOLEAN NOT NULL DEFAULT false`, `status TEXT`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`, and a `UNIQUE (job_id)` constraint. (Existing rows: `v2_turn_metrics` is best-effort append-only instrumentation; a clean reinstall / dedup of any pre-existing duplicate `job_id` rows is acceptable per repo convention — the migration dedups keeping the newest before adding the unique constraint.)

**Whole-turn accumulator:** a small per-turn object (created in `worker.process_job`, passed through `TurnDeps` or the turn context) accumulates, across every model call in the turn — planner `official_plan`, responder `respond`, any future tool-loop calls, and retries — `prompt_tokens += … / completion_tokens += … / model_calls += 1 / retries += …`. At the turn's **terminal point** (success OR any `mark_failed` path), it flushes **once** via an idempotent upsert:

```sql
INSERT INTO v2_turn_metrics
  (job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms,
   model_calls, retries, failed, status)
VALUES (%s, …)
ON CONFLICT (job_id) DO UPDATE SET
  prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens,
  latency_ms=EXCLUDED.latency_ms, model_calls=EXCLUDED.model_calls,
  retries=EXCLUDED.retries, failed=EXCLUDED.failed, status=EXCLUDED.status,
  updated_at=now();
```

Idempotent by `job_id` (one row per job); a re-drive (redelivery / retry of the same job) **replaces** with the recomputed whole-turn total rather than appending a duplicate. Failed turns record what they burned. `jobs_store.record_turn_metric` is replaced by `record_whole_turn_metric(job_id, user_id, lane, *, prompt_tokens, completion_tokens, latency_ms, model_calls, retries, failed, status)`; the per-call `record_turn_metric` INSERT at `worker.py:904` is removed in favor of the terminal flush.

### B6 — Load-test gate (`scripts/loadtest/`)

`collect.py`'s existing `SELECT … FROM v2_turn_metrics` queries keep working (the `prompt_tokens`/`completion_tokens`/`latency_ms` columns are unchanged). Add optional reads of the new columns (`model_calls`, `failed`) for a richer report, and update `run_loadtest.py`'s simulated processor to write the new whole-turn shape via `record_whole_turn_metric` (one upsert per synthetic job).

---

## Data flow (one v2 chat turn, PR B lens)

1. Worker resolves provider config (unchanged) and creates a whole-turn accumulator for the job.
2. Each `provider_client.chat_completion_async(...)` in the turn (planner/responder/…) may pass `tools=[ToolSpec…]` (PR C will; PR B passes none) and returns a `ProviderResponse{text, tool_calls, usage, raw}`.
3. The accumulator folds in `usage` + increments `model_calls` after each call.
4. At turn terminal (success or mark_failed), one idempotent upsert writes the whole-turn metric keyed by `job_id`.
5. Load-test gate reads the same `v2_turn_metrics` rows.

Tool calls flow (PR C consumes; PR B only proves the codec): a response's `tool_calls` are ordered, each with a stable `id`; the caller runs them and hands back `ToolResult{call_id, content}` which the next request's encode places into the wire by that id.

## Error handling

- **Unparseable tool-call args:** decode sets `args_ok=False` + `args_raw`, `args={}`; never raises. PR C decides the one-turn fallback.
- **Relay 400/422** on tool-bearing requests: unchanged from today's error surfacing; the one-turn, non-persistent fallback is PR C.
- **Metric write failure:** best-effort (same posture as today's append-only metric) — a metric upsert failure logs and never fails the turn.
- **Native-async conversion:** must preserve the existing retry/reliability wrapper semantics (`reliable_chat_completion_async`) and timeout behavior for the 3 converted wires — no regression in retry/backoff.

## Testing

- **Codec round-trip (CI, per provider × 4):** encode 2 `ToolSpec`s → assert the wire request carries both in the wire's tool shape; decode a **recorded** 2-tool-call wire response fixture → 2 `ToolCall`s with correct `id`/`name`/`args` (incl. Gemini synthetic ids); encode 2 `ToolResult`s → assert both are placed in the wire by `call_id`/name. This is the acceptance proof.
- **Usage normalization (per provider):** feed each provider's raw usage blob → assert normalized `Usage` keys.
- **Native async (3 converted wires):** mock `httpx.AsyncClient` → assert the request payload + parsed `ProviderResponse`; assert no `anyio.to_thread` hop remains.
- **Whole-turn metric:** upsert idempotency (same `job_id` twice → 1 row, latest values); cross-call accumulation (planner + responder folded into one row); failed-turn records a row with `failed=true`.
- **Additive-safety:** existing text-only callers still get `text` + empty `tool_calls`; the full existing suite stays at its baseline.
- **Manual live script `scripts/provider_probe/probe.py`** (NOT in CI, needs real BYOK keys): hits all 4 real providers with 2 tools, asserts each returns 2 `tool_calls` and accepts 2 `tool_results` by `call_id`. Mirrors the load-test A/B fidelity split (CI = codec fidelity; manual = live fidelity).

## Acceptance (from @sxysun, PR B subset)

- Normalized provider API returns assistant text + ordered `tool_calls(id/name/args)` + usage.
- OpenAI Chat, OpenAI Responses, Anthropic, Gemini all encode/decode tools and tool results.
- Anthropic/Gemini/Responses are native async (no thread-pool bridge).
- Usage normalized across providers.
- One idempotent whole-turn metric per job, covering all model calls, retries, failed turns.
- Load-test gate queries that production whole-turn metric.
- **All four providers can return two tool_calls in one response and receive two results by call_id** — proven by the codec round-trip tests (CI) and the manual live probe.

## Out of scope (later PRs)

- Removing `is_official`/`rule_plan`/`official_plan` dispatch and the unified tool loop, the reply-as-tool special tool, per-call message folding, parallel reads/ordered writes — **PR C**.
- Turn pool crash domain, kill switch, summary/retention coverage, compaction CAS retry — **PR D**.
- No live production caller passes `tools` in PR B; the outbox/effect producers stay unwired (PR C writes effects; PR A shipped that outbox).

## Interfaces PR C consumes from PR B

- `provider_client.chat_completion_async(..., tools: list[ToolSpec] | None) -> ProviderResponse` (and sync mirror).
- `provider_types.{ToolSpec,ToolCall,ToolResult,Usage,ProviderResponse}` (top-level `backend/provider_types.py`).
- The whole-turn accumulator + `jobs_store.record_whole_turn_metric(...)` so PR C's tool-loop calls fold into the same per-job metric.
