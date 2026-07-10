# Hosted Runtime V2 — tokens/turn baseline (pre-agent-loop)

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
