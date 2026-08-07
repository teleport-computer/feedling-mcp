# Runtime V2 Empty Provider Response Recovery

**Date:** 2026-08-07  
**Status:** Approved design  
**Scope:** Runtime V2 foreground chat, provider response parsing, encrypted trajectory telemetry  
**Observed provider/model:** Anthropic `claude-fable-5` in the test environment

## Summary

Runtime V2 foreground chat currently fails when a provider returns HTTP 200 with
a structurally valid response that contains neither visible text nor a standard
client `tool_use`. Anthropic Fable 5 reproduces this consistently with the full
V2 prompt and tool catalog. The provider parser raises before the V2 tool loop
can inspect the successful response, and the status-less parser error is then
misclassified as `upstream_unavailable`.

V2 will take ownership of foreground empty-response policy. The provider parser
will return structurally valid text-free responses to V2, and the V2 tool loop
will perform at most one bounded semantic correction with the original tool
catalog. If the correction is also empty, the turn will fail through the
existing `provider_empty_reply` path. Non-V2 callers retain their current
default behavior.

## User-visible problem

The model configuration probe succeeds, but foreground chat fails with the
generic message that the upstream model service is temporarily unavailable.
This is misleading because the provider accepted and completed the request.

The failure also prevents tool use. The model may have been considering a tool,
but V2 never receives a parsed response and therefore never reaches the tool
dispatcher.

## Evidence

### Test environment results

| Provider and model | Foreground chat | Tool behavior | Result |
| --- | --- | --- | --- |
| Anthropic Opus 4.8 | Successful | Successful | Compatible |
| Anthropic Opus 5 | Successful | Successful | Compatible |
| OpenRouter Opus 4.8 | Successful | Successful | Compatible |
| OpenRouter Opus 5 | Successful | Successful | Compatible |
| Anthropic Fable 5 | Failed | No tool event | Reproduces this issue |
| Anthropic Fable 5, reduced synthetic prompt | Successful | Returned `thinking` and `tool_use` | Tool use is supported |
| OpenRouter Fable 5 | Not run | Not run | Absent from the key's model catalog |

For a real Fable 5 V2 turn, the encrypted attempt trace recorded two upstream
HTTP 200 responses. Both attempts ended as `postprocess_error` with
`ProviderError("provider response had no usable reply text")`. The V2 turn
recorded one failed logical model call and no tool event.

### Confirmed findings

- The Anthropic API key is valid.
- The Fable 5 model identifier is accepted.
- The account can invoke and be billed for the model.
- The provider returned HTTP 200, not an authentication, quota, rate-limit,
  network, or 5xx error.
- The request was not rejected as an invalid tool schema with HTTP 400 or 422.
- The failure occurred during local response post-processing.
- The real V2 request did not reach tool execution.
- Fable 5 can emit a valid client `tool_use` with a reduced synthetic prompt.

### Inference boundary

The real V2 response was most likely thinking-only, or contained a successful
content block type not recognized as visible text or client `tool_use`. A
minimal Fable 5 request separately produced a confirmed thinking-only response.

The failed production-shaped response body was not retained in the attempt
trace, so its exact content-block sequence is not confirmed. The implementation
must therefore be based on the provider-independent condition “structurally
valid success with no visible text and no client tool call,” not on an
Fable-specific assumption.

## Root cause

The foreground response policy is enforced at the wrong layer.

1. Anthropic returns a structurally valid HTTP 200 response.
2. `provider_client._parse_anthropic_body()` tries to extract client tool calls.
3. With no recognized tool call, `_extract_anthropic_reply(required=True)`
   requires non-empty visible text.
4. It raises `ProviderError("provider response had no usable reply text")`.
5. Because the error was produced after an HTTP 200, it has no HTTP status.
6. `classify_provider_error()` treats status-less shape errors as transient.
7. V2 maps transient provider failures to `upstream_unavailable`.

This prevents `tool_loop.run_tool_loop()` from seeing the response's
`stop_reason`, reasoning presence, assistant turn, or content-block shape. It
can neither correct the response nor classify it accurately.

## Existing behavior that must remain intact

- A pure client-tool response with no visible text is valid and must continue
  into tool dispatch.
- Malformed JSON or a 2xx body without the provider's success container remains
  an error even when visible text is not required.
- Wake lanes may intentionally return no text and must continue to complete
  silently.
- Foreground chat must ultimately produce a visible reply or an attributed
  failure; it must not silently complete.
- Tool-schema rejection fallback remains limited to qualifying HTTP 400 or 422
  errors.
- Invalid or over-budget tool exchanges retain the existing tools-disabled
  fallback.
- Network errors, rate limits, and 5xx responses retain the existing transport
  retry and error classification.

## Considered approaches

### Error classification only

Map `no usable reply text` to `provider_empty_reply` instead of
`upstream_unavailable`.

This improves the user message but does not restore chat or tool use. It is not
sufficient.

### Fable-specific prompt or model allowlist

Recognize Fable 5 model identifiers and append a provider-specific instruction
requiring visible text or tool use.

This is brittle across aliases, relays, and future models. It also cannot handle
an intermittent empty success from an otherwise compatible model. It is not
selected.

### Provider-independent V2 semantic recovery

Allow V2 to receive a structurally valid empty success and perform one bounded
correction with the original tools. This preserves tool capability, produces an
accurate terminal error when recovery fails, and generalizes to future models.

This is the selected approach.

## Design

### Provider parsing boundary

`run_tool_loop()` will call the provider with `require_reply=False` for Runtime
V2 foreground turns. This flag changes only the provider parser's handling of a
valid text-free response. It does not change foreground chat's requirement to
eventually reply.

The provider result must preserve:

- `reply`, which may be an empty string;
- `reasoning`, if the provider exposes it;
- `stop_reason`;
- normalized usage;
- decoded client tool calls;
- the provider-native assistant turn.

Malformed success bodies continue to raise. The default value of
`require_reply=True` remains unchanged for callers outside this V2 policy.

### Empty-success predicate

The V2 tool loop recognizes an empty success only after parsing succeeds:

```text
visible text is empty
AND decoded client tool calls are empty
AND the provider response has a valid success shape
```

The presence of reasoning does not make the foreground response usable.

### One-shot correction

The loop adds per-turn state equivalent to:

```python
empty_response_recovery_used = False
empty_response_retry_instruction = ""
```

On the first empty success in a foreground turn:

1. Record an encrypted `protocol_fallback` trajectory event with reason
   `empty_provider_success`.
2. Mark the recovery as used.
3. Clear reasoning accumulated from the unusable response.
4. Rebuild the current prompt from the same chronological transcript.
5. Temporarily append the correction instruction below to the system message.
6. Keep the same offered tool catalog.
7. Spend the next call from the existing per-turn call budget.

Correction instruction:

```text
The previous response completed without visible text or a client tool call.
Complete the user's request now. Return either non-empty visible answer text or
a valid call to one of the offered client tools. Do not return a thinking-only
response.
```

The instruction is runtime-only. It is not persisted as a user or assistant
message. It is removed as soon as the correction produces visible text or tool
calls.

### Preserve tools during correction

The correction request keeps the original tool catalog. Disabling tools would
break requests that genuinely require memory, web, workspace, scheduling, or
MCP capabilities and could encourage the model to claim work it did not do.

The existing tools-disabled terminal fallback remains reserved for malformed,
undeclared, duplicate, mixed mutation/reply, or over-budget tool exchanges and
for qualifying tool-schema rejection.

### Terminal behavior

If the correction is also empty, the loop does not retry again. It passes the
empty terminal candidate through the existing foreground reply boundary, which
raises `TurnError("empty_reply")`. The worker already maps this to
`provider_empty_reply`.

No new public error class is required.

### Reasoning isolation

Reasoning from the unusable response must not be merged into the corrected final
reply. Before the correction call, clear both the accumulated reasoning list and
its deduplication set.

If the correction later emits a tool call, only reasoning associated with the
successful tool path may be eligible for the normal final reasoning channel.

### Call and cost budget

Recovery is allowed at most once per foreground turn and counts against the
existing `max_calls` limit. It does not establish an independent retry loop.

The observed failure path already performs two identical provider attempts
because the first post-processing error is classified as transient. After this
change, the same upper bound becomes one original request plus one informed
semantic correction. For this incident, the worst-case provider-call count does
not increase.

Transport failures retain their existing bounded retry policy. Semantic empty
recovery must not multiply transport attempts beyond the existing limits.

## Error classification

| Condition | Error class |
| --- | --- |
| HTTP 401 or 403 | `auth_invalid` |
| HTTP 402 | `quota_insufficient` |
| HTTP 400 or 422 incompatibility | `provider_incompatible` |
| HTTP 429 | `rate_limited` |
| HTTP 5xx or network failure | `upstream_unavailable` |
| Two valid successes with no text or tool call | `provider_empty_reply` |
| Local parsing or protocol implementation failure | `reply_parse_failed` |

The first empty success is not reported as a provider failure because V2 still
has one bounded recovery opportunity.

## Observability and privacy

The encrypted trajectory must make the recovery diagnosable without adding
plaintext prompt or response logging. The provider response or its encrypted
summary should expose:

```json
{
  "stop_reason": "max_tokens",
  "content_block_types": ["thinking"],
  "has_visible_text": false,
  "reasoning_present": true,
  "tool_call_count": 0,
  "recovery_used": true
}
```

The exact fields depend on information available from each provider parser.
They must be content-free and bounded. Unknown content block names may be
recorded, but block contents must not be copied into plaintext logs.

Expected encrypted event sequence:

```text
provider_request
provider_response
protocol_fallback(reason=empty_provider_success)
provider_request(recovery=true)
provider_response
```

API keys, user messages, system prompts, reasoning text, tool arguments, and
tool results must not be added to ordinary logs or metrics.

## Configuration probe

`test_provider_key()` intentionally uses `require_reply=False` because its
contract is credential, model, access, and billing validation. It is not a full
foreground-chat compatibility test.

This design does not change that public contract. Adding a separate
`chat_compatible` probe result would be an API and product change and is outside
this fix. The runtime recovery should first make valid empty successes usable or
accurately attributable.

## Test plan

### Provider parser tests

- Anthropic thinking-only plus `require_reply=False` returns a structured result.
- Anthropic thinking-only plus `require_reply=True` retains the current error for
  non-V2 callers.
- Anthropic tool-only responses remain valid with `require_reply=True`.
- Malformed 2xx responses still fail with `require_reply=False`.
- Mixed text and thinking responses remain unchanged.
- Equivalent valid-empty parser cases for other provider wires do not regress.

### Tool-loop tests

- Thinking-only followed by visible text completes successfully.
- Thinking-only followed by `memory_index` dispatches the tool and later returns
  final text.
- The correction retains the original tool names and schemas.
- The correction instruction appears only on the correction request and not in
  the persisted transcript.
- Reasoning from the unusable response is not attached to the final reply.
- Two consecutive empty successes make exactly two semantic calls and terminate
  as `empty_reply`.
- A later empty response in the same turn cannot re-arm recovery.
- Network, rate-limit, and HTTP 5xx retries remain unchanged.
- Existing tool-schema and malformed-tool-exchange fallbacks remain unchanged.
- Wake-lane intentional silence remains successful.
- File-delivery recovery and terminal fallback remain unchanged.

### Worker and trajectory tests

- Terminal `empty_reply` maps to `provider_empty_reply`, not
  `upstream_unavailable`.
- The encrypted trajectory records `empty_provider_success` and whether recovery
  was used.
- Model-call and usage accounting include both successful provider responses.
- A recovered turn reports provider success and does not set a provider cooldown.
- A terminal empty response produces only one user-visible attributed failure.

### Test environment acceptance

Use isolated V2 test users to verify:

- Anthropic Fable 5 ordinary foreground chat;
- Fable 5 with an explicit `memory_index` request;
- Fable 5 final text after a real tool result;
- Anthropic Opus 4.8 and Opus 5 regression;
- OpenRouter Opus 4.8 and Opus 5 regression;
- repeated empty success produces one `provider_empty_reply` failure;
- trajectory telemetry is useful without plaintext secret or conversation leaks.

## Expected code changes

Primary implementation:

- `backend/provider_client.py`
- `backend/model_api_runtime/v2/tool_loop.py`
- `backend/model_api_runtime/v2/worker.py`, only if classification or trajectory
  plumbing requires it

Tests:

- `tests/test_provider_client.py`
- `tests/test_v2_tool_loop.py`
- `tests/test_v2_worker.py`
- the existing V2 trajectory test module

Documentation:

- add an Unreleased changelog entry under `docs-site/content/docs/changelog.mdx`
- no OpenAPI regeneration is required unless implementation changes a public
  response schema

## Acceptance criteria

- Fable 5 completes ordinary Runtime V2 foreground chat in the test environment.
- Fable 5 can execute at least one real platform tool and return final text.
- A valid thinking-only success no longer becomes `upstream_unavailable`.
- Empty-response recovery runs at most once per foreground turn.
- Recovery remains within the existing model-call budget.
- Correction retains tools and does not persist its runtime instruction.
- Unusable first-round reasoning is not attached to the final reply.
- Opus and OpenRouter regression cases remain green.
- Wake-lane intentional silence remains unchanged.
- Repeated empty successes terminate as `provider_empty_reply`.
- Encrypted diagnostics distinguish empty/thinking-only response shapes without
  adding sensitive plaintext telemetry.
- Test-environment evidence is recorded before any production promotion.

## Out of scope

- Provider-specific Fable prompt tuning.
- A new public provider compatibility probe or API field.
- Changes to wake-lane silence policy.
- Changes to tool authorization, mutation safety, or tool-result encoding.
- Production deployment before test-environment validation.
