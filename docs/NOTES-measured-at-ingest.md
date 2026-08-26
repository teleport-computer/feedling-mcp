# measured_at / sample_id ingest — parsing-only landing

Scope: accept the new optional per-field metadata the iOS app has started
sending, and carry it through to the `perception_daily` rollup. No change to
existing behavior when a report carries none of these fields.

## Where I started, and what I actually found

Per the task brief I started at `backend/perception/ios_contract_v2.py` and
followed into `backend/perception/service.py`. `ios_contract_v2.py` needed no
change: it classifies signals at the *envelope* level (encrypted/changed/
plaintext-rejected). The new `<field>_measured_at` / `<field>_sample_id` /
`<field>_start` / `<field>_end` fields live one level deeper, inside the
already-decrypted per-signal JSON object (alongside `weight_kg`,
`asleep_minutes`, etc.), so they never reach `ios_contract_v2.py` at all —
they only exist after decrypt, in the `resolve.py` / `history.py` path.

`backend/perception/health_measurement.py` and its two call sites in
`service.py`'s Tier 2 rollup block (`_apply`, around line 601 and
622–678) already existed in this worktree, fully wired: extraction
(`extract_group_metadata`), attribution (`attributed_date`, delegating to
`perceptkit.attribution`), dedup identity (`identity_key`, delegating to
`perceptkit.identity`), and the arrival→measured cutover
(`apply_group_update`). Tests (`tests/test_health_measurement.py`,
`tests/test_perception_health_measurement_wiring.py`) were already registered
in both `tests/conftest.py`'s `_PURE_UNIT` and `.github/workflows/ci.yml`.

**What was missing, and the one bug I fixed:** `extract_group_metadata()`
read interval fields as `"<group>_interval_start"` / `"<group>_interval_end"`
— but the brief (and the already-shipped iOS contract) sends
`sleep_start`/`sleep_end` and `workout_start`/`workout_end`, no `_interval_`
infix. That mismatch meant `health_sleep` and `health_workout` were silently
falling through to the legacy arrival-date path forever — no attribution, no
dedup — despite `MEASUREMENT_GROUPS` already declaring both as `INTERVAL`
groups and `is_measurement_aware` gating on exactly this field. No existing
test exercised the real wire names for these two signals, so nothing caught
it. Fixed in `backend/perception/health_measurement.py`
(`extract_group_metadata`) and added regression coverage at both the pure
function and the `service.ingest()` wiring layer.

## Where the parsed metadata lives

Per (user, attributed-local-date, signal) in the existing `perception_daily`
rollup doc — no parallel structure:

- `_ts_kind: "measured"` — tags a day-doc as having been written by the new,
  measurement-time-aware path (vs. the old arrival-time path). A doc without
  this tag on its first measurement-aware write is **replaced outright**, not
  folded onto — see `apply_group_update`'s cutover comment. This is what
  stops a Jan 15 weight from re-appearing "fresh" under every day it happens
  to get re-uploaded.
- `_seen: {identity_key: ts}` — the dedup set per group, capped at 32 entries
  (`SEEN_CAP`) as headroom against a bug or hostile client, not a real-world
  limit.
- `_observed: {field: "observed"|"no_observation"|"unavailable"}` — sidecar
  distinguishing "queried, no sample" from "couldn't query at all" (decrypt
  failure), independent of whether anything numeric folded this report.

This follows the same shape the surrounding code already uses for per-field
day-doc state (`perception/history.py`'s `record_daily` merging straight into
the day-doc); the three `_`-prefixed keys are bookkeeping the rollup readers
already know to strip (`_strip_reserved` / `_reserved` in
`health_measurement.py`).

Grouping: most fields get their own independent group (the phone fetches
"newest sample" per metric independently), except blood pressure, whose one
`blood_pressure_measured_at`/`_sample_id` pair covers both
`blood_pressure_systolic` and `blood_pressure_diastolic` — declared once in
`MEASUREMENT_GROUPS["health_metabolic"]` so they can never be split.

## Malformed timestamp: carry the value forward, drop only its metadata

`perceptkit.attribution` raises on a timestamp with no UTC offset or that
fails to parse — deliberately, to avoid guessing a timezone and silently
shifting a day when the user travels. I did **not** reject the report at the
edge for this. `attributed_date()` catches exactly `(ValueError, TypeError)`
around the `perceptkit.attribution` call and falls back to `fallback`
(today's report-arrival date, the same date the legacy path would have used).
The numeric value itself is never touched by this — `field_values` is built
from the resolved/decrypted observation independently of whether metadata
parsed, so a malformed `weight_kg_measured_at` still folds `weight_kg` into
today's bucket, just without the backdating. This matches "the reading is
still good even if its metadata is not," and it's also the same posture the
module already takes everywhere else (unmapped signal, non-dict raw value,
missing sample_id): never raise, always degrade to legacy behavior for the
one broken field.

## Additive guarantee

`test_no_metadata_report_uses_todays_date_and_folds_normally` and
`test_old_app_build_without_any_metadata_keeps_legacy_behavior` assert that a
report with none of the new fields produces a doc with no `_ts_kind`/`_seen`/
`_observed` keys at all — byte-identical to the pre-existing rollup shape.

## Commands run

```
$ FEEDLING_TEST_PG=postgresql://localhost:1/none python3 -m pytest --collect-only tests/ 2>&1 | grep -c "test_health_measurement.py\|test_perception_health_measurement_wiring.py"
2

$ python3 -m pytest tests/test_health_measurement.py tests/test_perception_health_measurement_wiring.py tests/test_perception_prompt_golden.py -q -p no:randomly
55 passed, 2 warnings in 2.20s
```

Full suite result and pre-existing baseline comparison: see commit message /
final report (run: `python3 -m pytest tests/ -q -p no:randomly
--ignore=tests/test_api.py --ignore=tests/test_redis_pool.py
--ignore=tests/test_image_generation_model.py`).

## What I deliberately left alone

- `health_activity`, `health_cycle`, `health_mood` — not in
  `MEASUREMENT_GROUPS`, matching the brief ("the whole activity signal" sends
  nothing extra) and the pre-existing module comment about `health_cycle`/
  `health_mood` being out of scope for this batch.
- `step_count` already has an (unused-by-the-phone-today) group entry in
  `health_vitals`'s `MEASUREMENT_GROUPS`. The brief says step count sends
  nothing extra; leaving the declared-but-never-populated group in place is
  harmless (it degrades to the legacy path whenever the phone sends no
  `step_count_measured_at`/`step_count_sample_id`, which is always, today) —
  removing it isn't required by this task and risks touching more than
  necessary.
- No behavior change to how days are attributed for *existing* traffic
  (reports with no new fields), how duplicates are handled, or freshness —
  this lands only the field-name fix and its regression coverage.
