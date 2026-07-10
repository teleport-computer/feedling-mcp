# Hosted Runtime V2 — tokens/turn measurements

> Current rollout-gate result (2026-07-11, shared fixtures): **574.0
> tokens/turn, 2.3333 LLM calls/turn**. The 545.3/2.0 values below are retained
> as the historical pre-loop measurement, not as the current go/no-go number.

Taken on 2026-07-10, at commit `3bfb7f2`, BEFORE `agent_loop.py` existed.
Purpose: give D4's rollback gate (`scripts/loadtest/compare_tokens.py`) a reference point,
per the agent-loop spec §12 decision 3.

## Method

`scripts/loadtest/compare_tokens.measure_turn_tokens` drives the REAL
`planner.plan` + `responder.respond` for each fixture against
`MockProvider(estimate_tokens=True)`, and reads the mock's server-side
accumulators. Token counts are a 4-chars-per-token estimate — **relative**,
not absolute. The gate compares ratios, so this is sufficient and provider-independent.

Three fixtures: a bare one-liner, a mid-length turn with a real summary + 3-message tail,
and a long-summary turn. Exact fixture bodies are in this plan, Task 3 Step 5.

## Result (single-round `plan → execute → reply`)

| metric | value |
|---|---|
| `tokens_per_turn` | 545.3333333333334 |
| `llm_calls_per_turn` | 2.0 |

This is a historical pre-loop reference. Do not paste it into the current
runbook gate.

## How to re-measure after the loop lands

Run the same snippet (Task 3 Step 5). `llm_calls_per_turn` will exceed 2.0 whenever the
planner asks for a second round. The rollback gate is `tokens_per_turn` growth > +10%
versus a *resident* baseline — this file is the *V2 single-round* reference, which is what
tells you whether a regression came from the loop or from somewhere else.

---

# After the agent loop — MEASURED, not extrapolated

Driven through the real `worker.process_job` (real `planner.official_plan`, real
`executor`, real `responder.respond`) against a `MockProvider(estimate_tokens=True)` whose
reply cycles per request, so a genuine multi-round loop is forced. Numbers are the mock's
server-side accumulators, which see every provider call regardless of who made it.

Fixture: one user message, a ~200-char summary, a 2-message tail. **Small context** — see the
caveat below, it matters.

| turn shape | LLM calls | tokens | vs single-round |
|---|---|---|---|
| single round (planner emits `final_response` immediately) | 2 | 505 | 1.00× |
| 3-round tool loop (`_LOOP_MAX_ROUNDS`) | 4 | 1336 | **2.65×** |
| worst case — budget gate (`_TURN_MAX_LLM_CALLS=6`, forced always-REPLAN) | 6 | 2066 | **4.09×** |

## Two things this measurement corrects

**1. Tokens grow FASTER than call count.** 3× the calls (2 → 6) costs 4.09× the tokens, not
3×. Each additional round's planner prompt is bigger than the last, because from round 2 on
it carries the `prior_action_results` preview (`planner._PRIOR_PREVIEW_CHARS = 600` per
action type). Any capacity estimate derived from "calls × single-round cost" **understates
the worst case by ~35%**. The plan for this round originally asserted a ≤3× analytic bound;
it was wrong, and only measuring caught it.

**2. `_TURN_MAX_LLM_CALLS` really binds.** The worst-case row is 6 calls exactly — measured,
with `invalidation.evaluate` monkeypatched to REPLAN forever and a planner that never asks
to reply. Without the gate this turn would have run `replan_budget(2) × _LOOP_MAX_ROUNDS(3)
+ 1 = 7`. There is a regression test for this (`test_turn_llm_call_budget_binds_across_replan_and_rounds`).

## Caveat — the multiplier is context-dependent, and 4.09× is the pessimistic end

Single-round cost is `planner_prompt + responder_prompt`. The responder prompt carries the
whole summary + tail; the planner prompt carries only the user message, a digest, and the
capped `prior_action_results` preview. So:

- **Small-context users** (short summary): the responder prompt is small, extra planner
  rounds dominate → multiplier approaches the 4.09× measured here.
- **Large-context users** (long summary, long tail): the responder prompt dominates the
  single-round cost, so the same extra planner rounds are a smaller *fraction* → multiplier
  falls well below 4×.

The fixture above is deliberately small-context, so treat 4.09× as an upper bound for the
typical turn, not an average.

## What actually happens in production

The typical turn is **unchanged**. A planner that emits `final_response` on round 0 — which
is what the prompt tells it to do, and what every `rule_plan` (weak/relay model) turn does
unconditionally — costs exactly 2 calls, exactly as before the loop. Only turns where the
model genuinely asks for more context pay more. That is the feature, and it is bounded.

---

# The RESIDENT baseline — measured 2026-07-10, and it unblocks the D4 gate

`deploy/HOSTED_RUNTIME_V2_ROLLOUT.md` Step 1.3 gates the rollout on
`compare_tokens.py --resident-baseline <N>`. **Nobody had ever measured N**, so that gate
was un-runnable and the +10% rollback condition had no reference point. Now it does.

## Method

`scripts/loadtest/measure_resident.py` points the resident's actual agent CLI (`codex-cli
0.142.5`) at `MockProvider(estimate_tokens=True)` and reads the mock's server-side
accumulators. Same three user messages as the V2 fixtures above, so the numbers compare.

**This has to spawn the real CLI.** The resident never calls the provider itself — it shells
out to codex, which injects its OWN system prompt and tool catalog into every request.
Estimating that from our prompt text would understate it by more than 10×, and a fabricated
baseline is worse than none: it green-lights a real regression.

Two things the harness had to learn, both the hard way:
- **codex speaks the OpenAI Responses wire** (`POST /v1/responses`), not `/chat/completions`.
  `MockProvider` now serves both; the Responses route counts `instructions` + `input` + `tools`.
- **`codex exec` reads stdin when it is a pipe.** Without `stdin=DEVNULL` it hangs forever and
  the mock sees zero requests. The first probe lost 45 seconds to exactly this.

## Result

| metric | value |
|---|---|
| `tokens_per_turn` | **9303.0** |
| `llm_calls_per_turn` | 1.0 |
| prompt / completion split | 27906 / 3 over 3 turns |

For one trivial `"say ok"` turn, codex's request body is ~38 KB: `instructions` alone is
**20,771 characters**, plus **9 tool definitions**. That overhead is re-sent every single turn
and is essentially independent of what the user said. It is the whole story.

## The gate, finally runnable

```text
                                      tokens/turn   calls/turn   vs resident
resident (codex, shared prompts)          9303.0        1.0000         —
V2 current shared-fixture gate             574.0        2.3333      -93.83%
```

The separately forced small-context stress fixtures remain useful bounds but
are not the same workload: a three-round loop measured 1336 tokens and the
six-call hard-gate case measured 2066. The current shared-fixture V2 result is
about **16.2× lower** than the measured resident baseline. This large offline
margin does not replace production whole-turn telemetry or CVM load evidence.

## Honest caveats

1. **`llm_calls_per_turn = 1.0` is a floor, not the truth.** A `"say ok"` prompt makes codex
   answer in one shot. A real chat turn where the model uses tools costs several round-trips,
   each re-sending the full ~9.3k-token overhead. So the true resident number is **higher**
   than 9303 — which only widens V2's margin. Do not read 9303 as an upper bound.
2. The harness runs codex in an **empty temp workdir on purpose**. codex folds any `AGENTS.md`
   it finds into `instructions`; running inside this repo would inflate the baseline with our
   own docs and make the number unreproducible elsewhere.
3. Token counts are the same 4-chars-per-token estimate used for the V2 numbers. The gate
   compares ratios, so the estimator cancels out.
4. This measures the **codex** driver. The `claude` driver (anthropic wire) has its own
   overhead and was not measured; `resolve_driver` picks per provider.

## Re-measure

```bash
python scripts/loadtest/measure_resident.py
```
