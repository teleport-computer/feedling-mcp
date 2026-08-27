# Trend endpoint dispatch on `perceptkit.trend_models`

Branch: `feat/perceptkit-rewire`. Commit: see `git log` (this file is
committed alongside the code change).

## Problem

`backend/agent/perception_core.py::perception_trend_payload` always called
the median-baseline `perception_history.read_trend` (an alias for
`perceptkit.history.read_trend`). That model — "compare today to the median
of recent days" — only makes sense for signals that fluctuate around a
settled level (sleep duration, resting heart rate). It produces false or
useless answers for:

- **Drifting** quantities (body weight): "10kg below your usual" when there
  is no "usual" — the honest answer is start/end + rate of change.
- **Cyclical** quantities (menstrual cycle): comparing flow level to a
  median answers a question nobody asked; the real question is the interval
  since the last onset and whether this one is late.

`perceptkit.trend_models` (installed, fully tested, unmodified here) already
declares which model each signal follows (`TREND_MODEL`) and provides
`read_drift` / `read_cycles` for the other two shapes.

## What changed

`perception_trend_payload` (backend/agent/perception_core.py) now dispatches
on `trend_models.model_for(sig)`:

```python
model = trend_models.model_for(sig)

if model == trend_models.DRIFTING:
    return {"ok": True, "model": model, "trend": trend_models.read_drift(rows, sig, field)}

if model == trend_models.CYCLICAL:
    return {"ok": True, "model": model, "fallback": "read_trend",
            "trend": perception_history.read_trend(rows, sig, field)}

# FLUCTUATING (default): unchanged.
return {"ok": True, "trend": perception_history.read_trend(rows, sig, field)}
```

`sig` here is already the canonical catalog signal name (e.g. `health_body`,
`health_cycle`) produced by `_history_signal()`, which matches the keys
`trend_models.TREND_MODEL` uses — no extra mapping needed.

`rows` (from `perception_store.list_perception_daily`) are the same
`[{"date", "doc"}]` rollup rows `read_trend` already consumed; `read_drift`
takes the identical shape, so no new query or row transformation was needed
for the drifting path.

## Response-shape decision

`read_trend`, `read_drift`, and `read_cycles` return differently-shaped
dicts (median/baseline/current/delta vs. first/last/total_delta/per_month vs.
intervals/typical_interval/overdue_by). A consumer that assumes the median
shape and silently reads a missing key (e.g. `trend["median"]` on a drift
response, which returns `None` un-erroring) would be a worse bug than the
one being fixed here.

Decision: add a **top-level `"model"` key**, present only for the two new
branches (`"drifting"` / `"cyclical"`). Its **absence** is the untouched
legacy (fluctuating) shape. This was chosen specifically so the fluctuating
path's response body does not change at all — see "byte-identical" below.
`read_drift`'s own dict also already carries a redundant nested
`"model": "drifting"` (from the library itself, unmodified); that's fine,
belt-and-suspenders for a caller inspecting `trend` directly.

The cyclical fallback additionally carries `"fallback": "read_trend"` so a
caller can distinguish "cyclical, properly computed" (future) from
"cyclical, but running the median path because we don't have onset events
yet" (now) — both report `model: "cyclical"`, but only the fallback carries
that key.

## Cyclical: why it stays on the existing path

`read_cycles(events, *, today)` needs a list of **onset event dates** (when
each period started) plus a caller-supplied `today` — the library never
reads the clock.

What this endpoint actually has is `perception_store.list_perception_daily`
rows for `health_cycle`, which uses the `MAIN_OF_DAY` shape: each day's doc
is just the latest non-null field values reported that day (e.g. a
`flow_level` value), not a derived list of cycle onsets. Turning that into
onset events would mean detecting a transition (e.g. an "active period" flag
going false→true) — but:

- The exact field name and semantics of that flag are **not specified
  anywhere in this backend**. The only trace is prose in
  `backend/proactive/tool_catalog_v2.py`: `"Menstrual cycle: flow level +
  active-period flag."` — no field key, no encoding (bool? enum? per HealthKit
  category?).
  This is a repository fact I verified by grepping the whole backend and test
  fixtures for `menstru|cycle_day|flow_level|active_period|onset`; the health
  measurement code in `backend/perception/health_measurement.py` explicitly
  excludes `health_cycle` from its field-metadata table (`"health_activity /
  health_cycle / health_mood are intentionally NOT covered"`), and the one
  test fixture that touches it (`tests/test_health_measurement.py:61`) only
  shows `{"flow_level": "light"}` — a **categorical string**, which
  `perceptkit.history.read_trend`'s numeric-only field extraction already
  silently drops today (so the pre-existing fluctuating path for
  `flow_level` was already returning an effectively empty series — not a
  regression introduced here).
- Guessing that schema and writing an onset-detection heuristic here would
  be fabricating the very kind of invented data this task explicitly forbids.

**What would be required to do this properly**: the iOS/HealthKit producer
side needs to define and document a concrete field (e.g. a boolean
`is_period_start` or an explicit onset-date list) in the `health_cycle`
report, land it in `ios_contract_v2.py`'s field contract, and only then can
a `_onset_dates(rows) -> list[str]` extractor be written and fed into
`trend_models.read_cycles(events, today=<caller-supplied date>)`. That's a
schema/product decision (per this workspace's CLAUDE.md, cross-signal/field
contract changes go through the runtime owner), not something to invent
inside this endpoint.

Until then, `health_cycle` intentionally keeps running through the existing
`read_trend` path, now explicitly tagged `model: "cyclical", fallback:
"read_trend"` so any caller/consumer already knows not to trust it as a
"usual value" comparison and can flag it for follow-up instead of silently
misreading it.

## Byte-identical proof for fluctuating signals

`tests/test_perception_trend_dispatch.py::test_fluctuating_signal_is_byte_identical_to_plain_read_trend`
asserts the full returned dict for a fluctuating signal (`health_vitals`)
equals `{"ok": True, "trend": perception_history.read_trend(rows, sig,
field)}` exactly — same keys, no `"model"` key added, same values as before
this change existed. This is a direct equality assertion (`assert body ==
{...}`), not just "the trend sub-object matches."

## Commands run + real output

```
$ cd /Users/hx/Projects/io/worktrees/feedling-mcp/feat-perceptkit-rewire
$ PYTHONPATH=backend FEEDLING_TEST_PG=postgresql://localhost:1/none \
    python3 -m pytest --collect-only tests/ 2>&1 | grep -c test_perception_trend_dispatch
1
$ PYTHONPATH=backend FEEDLING_TEST_PG=postgresql://localhost:1/none \
    python3 -m pytest --collect-only tests/test_perception_trend_dispatch.py -q
tests/test_perception_trend_dispatch.py::test_fluctuating_signal_is_byte_identical_to_plain_read_trend
tests/test_perception_trend_dispatch.py::test_drifting_signal_routes_through_read_drift
tests/test_perception_trend_dispatch.py::test_cyclical_signal_falls_back_to_read_trend_but_is_tagged
tests/test_perception_trend_dispatch.py::test_unlisted_signal_defaults_to_fluctuating
4 tests collected in 1.60s

$ PYTHONPATH=backend python3 -m pytest \
    tests/test_perception_trend_dispatch.py tests/test_perception_prompt_golden.py -v
... (27 items)
======================== 27 passed, 2 warnings in 1.89s ========================

$ PYTHONPATH=backend python3 -m pytest \
    tests/test_agent_perception_route.py tests/test_capabilities_perception.py \
    tests/test_perception_history.py -q
47 passed, 2 warnings in 0.36s
```

Full-suite run:

```
$ PYTHONPATH=backend python3 -m pytest tests/ -q -p no:randomly \
    --ignore=tests/test_api.py --ignore=tests/test_redis_pool.py \
    --ignore=tests/test_image_generation_model.py
...
FAILED tests/test_e2b_template_contract.py::test_tracked_template_tag_matches_extractor_and_pinned_contract
FAILED tests/test_file_text.py::test_pdf_extracts_text_via_pypdf - AssertionE...
FAILED tests/test_plaintext_shadow_config.py::test_live_identity_rejects_hostname_aliases_to_same_database
FAILED tests/test_plaintext_shadow_config.py::test_live_identity_accepts_different_databases_on_same_server
FAILED tests/test_provider_client.py::test_dedicated_url_answer_is_fetched_and_must_decode
FAILED tests/test_provider_client.py::test_a_link_inside_a_chat_reply_is_never_fetched
FAILED tests/test_provider_client.py::test_links_are_capped_and_share_one_byte_budget
FAILED tests/test_provider_client.py::test_official_providers_also_fetch_a_url_answer
FAILED tests/test_provider_client.py::test_links_share_one_wall_clock_budget
FAILED tests/test_v2_downloadable_files.py::test_workspace_file_result_renders_real_word_and_pdf_documents
FAILED tests/test_v2_downloadable_files.py::test_process_job_commits_single_generated_image_without_empty_followups
11 failed, 11545 passed, 8 skipped, 9 xfailed, 68 warnings, 3 subtests passed in 411.21s (0:06:51)
```

11 failures — matches the pre-existing baseline exactly (count and test
names), none perception/trend related. Ran twice (once foregrounded, once
backgrounded) with identical results.

## Files touched

- `backend/agent/perception_core.py` — the dispatch itself.
- `tests/test_perception_trend_dispatch.py` — new pure-unit test file.
- `tests/conftest.py` — registered the new file in `_PURE_UNIT`.
- `.github/workflows/ci.yml` — new "Perception trend model dispatch unit
  tests" step (also satisfies the "Guard top-level pytest discovery
  coverage" step, which hard-fails on any `tests/test_*.py` not named
  somewhere in this workflow file).
- `docs/NOTES-trend-dispatch.md` — this file.

`perceptkit/trend_models.py` itself was not modified.
