---
document_lifecycle: current
canonical_owner: self
---
# Self-thinking bilingual prompt evaluation

This evaluation compares the currently installed self-thinking instruction with
the bilingual renderings in memgarden source. It does not publish a wheel,
change a dependency pin, change a deployment, or claim that production users
already receive the bilingual prompt.

## Arms and provenance

- `baseline`: `INSTRUCTION` from the installed `agent-protocol-core`
  distribution. The loader requires the imported module's `__file__` to equal
  the path reported by `importlib.metadata` for that installed distribution.
- `candidate`: `instruction_for_language(...)` loaded by exact file path from
  `packages/agent-protocol-core/src/agent_protocol_core/self_thinking.py` in the
  supplied memgarden checkout.

The two resolved module paths must differ. Every planned and measured row stores
both paths, both language-specific text SHA-256 values, and both module SHA-256
values. Immediately before execution, the harness rereads both artifacts and
rejects a plan whose path or hash has drifted. Equal text hashes alone are not
an error: a valid rewrite may preserve one language byte-for-byte. Resolving
both arms to the same file is always an error.

Every plan row, result row, and summary cell also carries the derived boolean
`arms_text_identical`. One language may legitimately preserve its rendering.
If both `zh` and `en` are text-identical across arms, however, the entire A/B run
has no treatment variable. Plan generation, execution revalidation, and offline
summarization reject that artifact as `VACUOUS`; it is never reported as
`UNABLE_TO_DISTINGUISH`.

## Matrix

The full matrix contains both `zh` and `en` for every row below.

| Access cell | Provider wire | Tier | Model |
|---|---|---|---|
| anthropic-official | anthropic | flagship | claude-sonnet-4-6 |
| anthropic-official | anthropic | small | claude-haiku-4-5-20251001 |
| openai-official | openai | flagship | gpt-5.2 |
| openai-official | openai | small | gpt-5.2-mini |
| gemini-official | gemini | flagship | gemini-3.1-pro-preview |
| gemini-official | gemini | small | gemini-3.6-flash |
| deepseek-official | deepseek | flagship | deepseek-v4-pro |
| deepseek-official | deepseek | small | deepseek-v4-flash |
| openrouter | openrouter | flagship | anthropic/claude-sonnet-4.6 |
| openrouter | openrouter | small | openai/gpt-5.2-mini |
| relay-openai-compatible | openai_compatible | relay | `E2E_RELAY_MODEL` |
| hojimi-relay | openai_compatible | relay | claude-haiku-4-5-20251001 |

The provider prompt contains no assistant prefill and no tools. The only A/B
variable is the self-thinking instruction; the user prompt and reply-language
rule are fixed within a language cell.

## Run sizes and gates

1. `canary`: only `anthropic-small`, one output per arm and language (four
   calls). A larger run is forbidden until at least one measured canary output
   passes all three metrics. “The script completed” is not a canary pass. If
   that route is unavailable, `--only` may explicitly select one other cell;
   the canary still remains a four-call, single-cell run.
2. `probe`: all small and relay cells, two independent replicates with two
   outputs per arm/language/replicate. This is the default small run.
3. `full`: every matrix row, two independent replicates with five outputs per
   arm/language/replicate. Increasing beyond five requires a separately
   reviewed plan; the harness does not silently expand to 30 rounds.

Plan generation and summarization are offline. Real execution additionally
requires both `--execute` and
`FEEDLING_T403_PROVIDER_RUN_ALLOWED=P0_COMPLETE`. This environment gate may be
set only after the P0 provider-quota owner gives the completion signal.

## Metrics and noise floor

Every measured provider output enters three separate numerators over the same
measured-output denominator:

- `think_first_char`: raw output begins with exact `<think>`;
- `think_language_follow`: a complete thinking block has the requested dominant
  writing system;
- `reply_language_follow`: the visible reply has the requested dominant writing
  system.

There is no combined score. A missing or malformed thinking block is a measured
failure, not an excluded row. A missing credential, relay URL/model, or provider
transport response is `UNMEASURED`, listed separately, and excluded from all
success-rate denominators. In particular, a network failure in
`relay-openai-compatible` cannot lower or raise another cell's rate.

For each metric and provider/model/language cell, let `r0` and `r1` be the two
same-prompt replicate rates. The noise floor is:

```text
max(abs(baseline.r0 - baseline.r1), abs(candidate.r0 - candidate.r1))
```

The cross-arm delta is `candidate pooled rate - baseline pooled rate`. When its
absolute value is less than or equal to the noise floor, the only permitted
verdict is `UNABLE_TO_DISTINGUISH`. Missing either replicate yields
`UNMEASURED`, not a zero noise floor.

## Commands

Before the provider-run signal, only the first command is allowed:

```bash
python3 -m tools.e2e.self_thinking_prompt_probe \
  --plan /tmp/t403-canary-plan.json \
  --candidate-repo /path/to/memgarden \
  --profile canary
```

After the explicit signal, the canary may run:

```bash
FEEDLING_T403_PROVIDER_RUN_ALLOWED=P0_COMPLETE \
python3 -m tools.e2e.self_thinking_prompt_probe \
  --execute /tmp/t403-canary-plan.json \
  --output /tmp/t403-canary.jsonl
```

Only after the canary contains a true product success, build and execute the
probe or full plan. Non-canary execution requires `--canary-results` and rejects
a canary that merely contains transport-successful rows.

```bash
python3 -m tools.e2e.self_thinking_prompt_probe \
  --plan /tmp/t403-probe-plan.json \
  --candidate-repo /path/to/memgarden \
  --profile probe

FEEDLING_T403_PROVIDER_RUN_ALLOWED=P0_COMPLETE \
python3 -m tools.e2e.self_thinking_prompt_probe \
  --execute /tmp/t403-probe-plan.json \
  --canary-results /tmp/t403-canary.jsonl \
  --output /tmp/t403-probe.jsonl

python3 -m tools.e2e.self_thinking_prompt_probe \
  --summarize /tmp/t403-probe.jsonl \
  --output /tmp/t403-probe-summary.json
```
