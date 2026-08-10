# Multi-tenant Work Bucket Expectation Fix

## Problem

`test_capture_job_add_card_writes_envelope_without_chat_or_delivery` still
expects the English bucket name `work`. Commit `1c8293cd` intentionally made
common English bucket matching case-insensitive and canonicalizes that value to
`Work`, so the production behavior is correct and the older assertion is stale.

## Design

Update only the captured envelope plaintext expectation from `work` to `Work`.
Keep the input fixture as lowercase `work` so the integration test continues to
prove that the capture path applies canonical bucket normalization. Do not
change production code or remove the broader capture-flow test.

## Verification

Run the failing test directly, then run the same resident-consumer test set used
by the `test_api.py (multi-tenant)` CI job. The known assertion must pass without
introducing new failures.
