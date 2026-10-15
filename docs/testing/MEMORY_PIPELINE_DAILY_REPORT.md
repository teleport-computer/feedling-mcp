---
document_lifecycle: current
canonical_owner: self
---
# Memory pipeline daily report

The stdlib renderer `tools/memory_pipeline_daily_report.py` consumes the internal,
admin-authenticated `/v1/admin/lane-rollup/summary` endpoint. The scheduled workflow
uses the default branch, reads the configured production admin endpoint and posts
to Lark. A source checkout, a local fixture, or a test merge does not prove which
renderer or backend ran a historical report. Never run the default command for
local validation: use `--fixture ... --dry-run`.

## Window and population

The requested `day` is `[00:00, next-day 00:00)` in Asia/Shanghai. V2 counts terminal
`agent_jobs` by `finished_at`. V1 counts terminal `user_logs` using the status-specific
terminal timestamp, falling back to the log timestamp when missing or invalid.
These are observed terminal attempts/records, not generated cards, model calls,
provider ledger rows, mutable metrics, or traces. Nonterminal records and records
outside source coverage do not enter the daily total; live aged jobs are separate.
User counts are distinct source identifiers within one lane/route, not additive
across lanes/routes; deleted users merged into the anonymous sentinel cannot be
reconstructed as distinct people.

## Internal additive summary contract

Each day/previous cell adds `outcomes`, `observed_total`, `reason_codes`, and
`evidence_notes`. Existing classification fields remain compatible. `active_users`
now includes replacement-only cells, which previously disappeared.

| Outcome | Evidence and meaning |
|---|---|
| `processed` | Completed minus measured Dream `silent_declared` in either route. Processing completion, not proof of generated content. |
| `skipped` | V2 Dream declared skips when `voice_from <= day`; V1 frozen `control_outcomes` (producer maps exactly status skipped), only when all outcome columns are measured and conserved. |
| `failed` | V2 failed status; V1 failed/error after removing measured skipped. Includes account/config failures and V2 disabled/control codes; it is not the operational alert numerator. |
| `expired` | Raw expired count, separate from failed. V1 producer currently emits zero. |
| `superseded` | Raw replacement count. V1 producer currently emits zero. |
| `unknown_completed` | Dream completions in either route whose silence split is unmeasured or inconsistent (`silent_declared > completed`); also measured V1 declared silence, whose processing/skip meaning is ambiguous. |
| `unknown_non_success` | V1 failed/error/skipped aggregate whose status split is not measured or conserved. |

All seven are mutually exclusive; their sum is `observed_total`, equal to source
completed + failed + expired + superseded. This is conservation of observed rows,
not a claim of complete source coverage or a reconstructed job-by-job join.
Zero buckets can accompany missing/partial coverage and must be read with its
warning. Unknown future statuses cannot be counted from the current projection.

`reason_codes` covers the source non-success aggregate (V2 failed+expired; V1
failed/error/skipped). Missing counts receive `no_code`. Excess code counts remain
visible with `reason_counts_exceed_non_success`, never silently truncated. Reasons
are not status-linked in the rollup; they cannot be assigned to individual expiry,
skip, or failure buckets. Completed Dream skip reasons are not stored here and must
not be inferred as `not_enough_new_cards` from an unrelated trace sample.

Operational / control / user-unavailable form a second, overlapping diagnostic
axis over non-success records. Do not add this axis to terminal buckets or subtract
its exclusions twice. The retained alert rate is operational / (legacy completed
minus declared skips + operational). When classification is missing it remains a
conservative alert fallback, not a proven failure result. All account-exclusion
codes are rendered; there is no top-two truncation. Zero-success users are not
labelled deadlocked. Coverage prints backfill/complete/through/voice/outcome
watermarks; unknown values remain unknown. Older summaries without new fields
render an explicit unknown detail, not reconstructed raw statuses.

No schema migration, historical backfill, production write, public API or execution
policy change is involved. This admin-only endpoint is excluded from public
OpenAPI. Historical incident cause and actual generated-content counts remain
outside this reporting correction.

## Offline checks

Run `tests/test_admin_lane_rollup_summary.py` and
`tests/test_memory_pipeline_daily_report.py` for conservation, compatibility,
classification, rendering, admin authentication and stubbed delivery. The
`tests/test_lane_rollup.py` producer suite checks real local PostgreSQL freeze
semantics (including cross-midnight terminal timestamps and Dream skips).

The Sept14 source fixture is synthetic. T825's Oct13 192/68 and 8/4/4 exclusion
case reuses accepted anonymous aggregate counts; it does not invent user counts
or join independently salted snapshots. Golden messages are presentation examples,
not saved production messages.

## Resident Dream completion boundary

At the reviewed e4434c11 source, `tools/chat_resident_consumer.py::_process_dream_jobs`
writes `status=skipped`, `wake_result=skipped` on `dream_disclosure.skip_reason`;
normal completion writes `dream_result` / action observations and does not write
`wake_result=sleep`. The dispatcher routes Dream jobs to this processor, separately
from `_process_proactive_jobs`, which writes completed+sleep (including after memory
identity actions). Thus sleep is not a universal proof of no memory generation.
`db._LANE_ROLLUP_V1_VOICE_SELECT` nevertheless measures completed+sleep as
`silent_declared` for every lane. Historical completed Dream sleep cannot be proven
normal processing or a no-generation skip from the aggregate. With a trusted voice
watermark it goes to `unknown_completed`, leaving completed-minus-declared as
`processed`. With absent/future watermark or excessive declared count, all observed
Dream completions go to `unknown_completed`. Frozen V1 control skips remain in the
separate skipped bucket; nothing is subtracted twice. This changes no stored rows.
