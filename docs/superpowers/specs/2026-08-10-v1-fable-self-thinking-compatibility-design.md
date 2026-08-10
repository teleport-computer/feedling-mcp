# V1 Fable 5 Self-Thinking Compatibility Design

## Problem

Runtime V1 foreground chat prepends the mandatory visible self-thinking
instruction to every model whenever self-thinking is enabled. Fable 5 is known
to refuse requests that require it to expose a genuine chain of thought, and
Runtime V2 already omits that instruction for the exact Fable 5 model family.
The V1 pi path lacks the same compatibility boundary, so a route can pass the
minimal provider connection test and then fail during a real resident turn.

## Scope

- Apply the compatibility rule only to Runtime V1 foreground-chat prompt
  assembly.
- Treat `claude-fable-5` and namespace-qualified forms such as
  `anthropic/claude-fable-5` as Fable 5.
- Do not treat similar identifiers such as `claude-fable-50` or
  `foo-claude-fable-5-bar` as Fable 5.
- Preserve pi native reasoning and the route's existing `--thinking` behavior.
- Do not change connection testing, Runtime V2, background lanes, provider
  routing, or non-Fable prompt behavior.

## Design

Add a small model predicate beside the V1 foreground self-thinking injection.
It reads the authoritative runtime model from `AGENT_RUNTIME_METADATA`, strips
any provider namespace by taking the final slash-delimited component, and
compares that basename exactly with `claude-fable-5`.

The foreground prompt builder injects `self_thinking.INSTRUCTION` only when the
feature is enabled and the selected model supports mandatory visible
self-thinking. Fable 5 therefore receives the original user turn without the
forced `<think>` protocol, while all other models retain the current prompt.

## Error Handling

Missing model metadata remains backward compatible: it is treated as
supporting mandatory self-thinking, so arbitrary self-hosted or legacy Runtime
V1 configurations do not silently change behavior. Provider refusals and empty
replies continue through the existing bounded failure path.

## Tests

Use the real V1 foreground message assembly path rather than testing the helper
alone:

1. With self-thinking enabled and runtime model
   `anthropic/claude-fable-5`, assert the dispatched foreground content does not
   contain `self_thinking.INSTRUCTION`.
2. With self-thinking enabled and model `claude-fable-50`, assert the instruction
   is still present.
3. Run the focused V1 consumer tests and the existing V2 Fable prompt tests to
   confirm parity without changing V2 behavior.

## Acceptance Criteria

- A Runtime V1 pi Fable 5 foreground turn is not forced to emit a visible
  `<think>` block.
- Pi native reasoning remains enabled according to the route's existing
  reasoning configuration.
- Non-Fable V1 models receive byte-equivalent self-thinking injection behavior.
- Exact and namespaced Fable identifiers are covered by regression tests, with
  a near-match negative boundary.
