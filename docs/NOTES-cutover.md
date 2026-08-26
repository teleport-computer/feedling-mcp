# The arrival-time -> measured-time cutover: closing the read-side gap

Scope: `docs/NOTES-measured-at-ingest.md` landed the *write* side of the
cutover (batch 2). This batch closes the gap that made it not actually work
end to end: the marker existed on write, but nothing on read stopped it from
being pooled back together with the data it exists to separate from.

## What I found already built (write side)

Everything the brief describes as "the marker mechanism" was already fully
implemented and already tested in `backend/perception/health_measurement.py`:

- `_ts_kind: "measured"` on a `perception_daily` day-doc — exactly the marker
  the brief hinted might already exist ("something like `_ts_kind`"). I built
  on it; I did not add a second mechanism.
- `apply_group_update`'s cutover: a day-doc not yet tagged `_ts_kind ==
  "measured"` is replaced outright (not folded onto) the moment a
  measurement-time-aware report lands on it —
  `test_apply_group_update_cutover_replaces_arrival_tagged_doc_outright` and
  the wiring-level `test_cutover_replaces_poisoned_arrival_tagged_row` both
  already covered this.
- Dedup via `identity_key`/`_seen` — a re-upload of the exact same sample
  (same `sample_id`) never re-folds, tested at both layers.
- Old app builds (no measurement metadata at all) — untouched legacy path,
  tested (`test_old_app_build_without_any_metadata_keeps_legacy_behavior`).

So the *write* side of "never compare old and new meanings" was solid: the
first measured write per day-doc wins outright instead of being numerically
compared against the poisoned arrival-tagged aggregate sitting there.

## The gap: the read side still pooled them into one series

`_ts_kind` only ever lived inside ONE day-doc. It said nothing about the
OTHER day-docs already sitting in `perception_daily` under different dates —
every day, before this rollout, that the phone re-uploaded the same stale
sample and got a fresh (fabricated) arrival-tagged row for that day.

Those old rows are never touched by the write-side cutover (they're not the
row currently being written to), and they're a *different quantity* from
measured rows in exactly the sense the task brief describes — an arrival
instant, not a measurement instant. But every reader that folds multiple
`perception_daily` rows into one series pulled them all in with no
distinction:

- `perception_trend_payload` (`GET .../trend`) -> `perceptkit.trend_models
  .read_drift` for `health_body` (drifting model — the brief's own worked
  example is body measurements) and `perceptkit.history.read_trend`
  otherwise.
- `perception_digest_payload` -> `perceptkit.history.notable_changes` /
  `cross_domain_recent`, which call `read_trend` per (signal, field)
  internally.

Concretely: a year of `health_body` rows for one user could contain months of
fabricated-fresh arrival-tagged `weight_kg: 68.4` entries (poisoned, one per
re-upload day) sitting right next to the one correctly-dated measured row —
and `read_drift`'s "first/last/rate-per-month" math would treat all of them
as one continuous weight trajectory. That is precisely "ordering them is
meaningless" from the task brief, just on the read side instead of the write
side.

Both `read_trend` and `read_drift` live in the installed `perceptkit`
package — out of bounds to edit per the task constraints, and correctly so:
the fix belongs at the call site that knows about `_ts_kind`, which is this
repo's `backend/agent/perception_core.py`, not the framework-neutral
history/trend math.

## The fix

New pure function, `health_measurement.select_rollup_rows_after_cutover`
(`backend/perception/health_measurement.py`):

- If ANY row in a signal's date-ascending row list carries `_ts_kind ==
  "measured"`, keep ONLY the measured-tagged rows for series math. The old
  arrival-tagged rows are dropped from that series (but not deleted from the
  table — see below).
- If NO row carries the tag (old app traffic only, or a signal
  `health_measurement.py` doesn't cover), it's a no-op — byte-identical to
  today's behavior. This preserves the additive guarantee: nothing changes
  for traffic the new contract never touches.

Wired into `backend/agent/perception_core.py` at the two multi-day
aggregation call sites:

- `perception_trend_payload` — filters `rows` before dispatching to
  `read_drift` / `read_trend`.
- `perception_digest_payload` — filters each signal's rows before building
  `rows_by_signal`, so both `notable_changes` and `cross_domain_recent`
  (which both fold multiple days per signal) inherit the same guard.

**Deliberately NOT applied to** `perception_history_payload` (the raw
per-day rollup read). That endpoint returns day-docs verbatim, `_ts_kind`
included — this is what "stay readable" and "distinguishable" mean in the
brief: a debugging/support view can still see every row, old and new, with
the tag telling you which is which. Filtering only happens where multiple
days get folded into one number (trend/drift/digest), which is where
"silently mixed into one series" would actually happen.

This runs through the shared `backend/agent/perception_core.py` builders,
which both the V2 tool-schema capability (`backend/capabilities/
perception.py`) and the ASGI routes call — no separate V1/V2 read path
exists for trend/digest, so this fix covers both runtimes without a second
implementation.

## Tests added

**Pure** (`tests/test_health_measurement.py`):
- `test_select_rollup_rows_keeps_only_measured_once_any_row_is_measured`
- `test_select_rollup_rows_is_a_no_op_when_nothing_is_measurement_aware`
- `test_select_rollup_rows_tolerates_empty_and_malformed_input`

**Wiring** (`tests/test_perception_health_measurement_wiring.py`):
- `test_stale_resample_after_cutover_is_rejected_not_refolded` — the
  "genuinely stale new-meaning record rejected after cutover" case from the
  task brief: first report establishes the cutover for a day-doc, then TWO
  more re-uploads of the exact same (already-cut-over) sample must not
  re-fold — `count` stays 1, `_seen` stays a single entry.

**Read-side / trend dispatch** (`tests/test_perception_trend_dispatch.py`):
- `test_drifting_trend_drops_arrival_tagged_rows_once_any_row_is_measured` —
  the brief's own body-measurement scenario end to end: 4 poisoned
  arrival-tagged rows + 2 real measured rows go into `list_perception_daily`,
  and `perception_trend_payload`'s drift output matches `read_drift` called
  with ONLY the 2 measured rows — first/last dates and total_delta prove the
  poisoned rows never entered the computation.
- `test_trend_keeps_all_rows_when_none_are_measurement_aware_yet` — the
  additive guarantee on the read side: no `_ts_kind` anywhere -> unfiltered,
  byte-identical to `read_trend`'s output on the raw rows.

No new test file was created — all three scenarios landed in existing,
already-registered files (`test_health_measurement.py`,
`test_perception_health_measurement_wiring.py`,
`test_perception_trend_dispatch.py`), all three already present in both
`tests/conftest.py`'s `_PURE_UNIT` and `.github/workflows/ci.yml` before this
change (verified — see commands below), so no new registration was needed.

## Commands run

```
$ python3 -m pytest tests/test_health_measurement.py tests/test_perception_health_measurement_wiring.py tests/test_perception_trend_dispatch.py tests/test_perception_prompt_golden.py -q -p no:randomly
65 passed, 2 warnings in 1.85s

$ grep -n "test_health_measurement\|test_perception_health_measurement_wiring\|test_perception_trend_dispatch" tests/conftest.py .github/workflows/ci.yml
tests/conftest.py:318:        "test_health_measurement.py",
tests/conftest.py:319:        "test_perception_health_measurement_wiring.py",
tests/conftest.py:446:        "test_perception_trend_dispatch.py",
.github/workflows/ci.yml:397:            tests/test_health_measurement.py \
.github/workflows/ci.yml:398:            tests/test_perception_health_measurement_wiring.py \
.github/workflows/ci.yml:486:            tests/test_perception_trend_dispatch.py \

$ FEEDLING_TEST_PG=postgresql://localhost:1/none python3 -m pytest --collect-only tests/test_perception_trend_dispatch.py tests/test_health_measurement.py tests/test_perception_health_measurement_wiring.py -q
42 tests collected in 5.76s   (no DB reachable -> confirms all three files collect DB-free)
```

Full-suite run and pre-existing baseline comparison: see final report /
commit message (command: `python3 -m pytest tests/ -q -p no:randomly
--ignore=tests/test_api.py --ignore=tests/test_redis_pool.py
--ignore=tests/test_image_generation_model.py`).

## Schema / migration

**None needed.** `_ts_kind` (and `_seen`, `_observed`) are plain keys inside
the existing `perception_daily.doc` JSONB column, already written by the
prior batch. This change touches no table, no column, no index — nothing to
migrate, nothing to run twice, no lock to take.

## What contradicts the brief, if anything

Nothing found that contradicts the brief. One thing worth flagging
explicitly since it wasn't obvious from the brief alone: the brief's example
("stored says Aug 26, incoming says Jan 14 -> incoming looks older ->
rejected") reads like a literal timestamp comparison, but no such comparison
actually exists anywhere in this codebase — `perception_daily` rows are keyed
by *attributed local date* (a bucket, chosen by `attributed_date()`), not by
a single mutable "latest timestamp" column that gets compared across
uploads. The real mechanism that produces the brief's described symptom is
subtler and was already correctly identified and fixed by the prior batch's
write-side cutover (comparing an old *aggregate* against a new single sample
would be meaningless, not merely "look older"); what remained broken was
purely the read-side pooling across day-docs, closed by this batch.

## Known non-goals (unchanged from the prior batch)

- `perception_state` (Tier 1 live snapshot) is untouched by this cutover —
  it deliberately tracks "last reported value" by report-arrival order
  (`merge_state_guarded`'s ts guard uses the report's own `client_ts`, not
  any field's `measured_at`), which is correct for a live snapshot and out of
  scope for the Tier 2 rollup cutover this task is about.
- `health_activity`, `health_cycle`, `health_mood` remain outside
  `MEASUREMENT_GROUPS` (unchanged, per the prior batch's notes) — the read
  filter is a no-op for them exactly as it is for any other signal that never
  gets `_ts_kind`.

## Full suite result

```
$ python3 -m pytest tests/ -q -p no:randomly --ignore=tests/test_api.py --ignore=tests/test_redis_pool.py --ignore=tests/test_image_generation_model.py
...
FAILED tests/test_e2b_template_contract.py::test_tracked_template_tag_matches_extractor_and_pinned_contract
FAILED tests/test_file_text.py::test_pdf_extracts_text_via_pypdf
FAILED tests/test_plaintext_shadow_config.py::test_live_identity_rejects_hostname_aliases_to_same_database
FAILED tests/test_plaintext_shadow_config.py::test_live_identity_accepts_different_databases_on_same_server
FAILED tests/test_provider_client.py::test_dedicated_url_answer_is_fetched_and_must_decode
FAILED tests/test_provider_client.py::test_a_link_inside_a_chat_reply_is_never_fetched
FAILED tests/test_provider_client.py::test_links_are_capped_and_share_one_byte_budget
FAILED tests/test_provider_client.py::test_official_providers_also_fetch_a_url_answer
FAILED tests/test_provider_client.py::test_links_share_one_wall_clock_budget
FAILED tests/test_v2_downloadable_files.py::test_workspace_file_result_renders_real_word_and_pdf_documents
FAILED tests/test_v2_downloadable_files.py::test_process_job_commits_single_generated_image_without_empty_followups
11 failed, 11556 passed, 8 skipped, 9 xfailed, 68 warnings, 3 subtests passed in 405.66s
```

11 failed, matching the stated pre-existing baseline of 11 exactly by count.
None are perception-related (e2b template pinning, PDF text extraction,
plaintext-shadow hostname/database identity config, provider-client URL
fetching, v2 downloadable-file rendering/job-commit) — unrelated to this
change and to `perception_daily`/health measurement.
