# Test API Dream Card-Gate Fixture Repair

## Problem

The `test_api.py (multi-tenant)` CI job fails in its Round 3 Runtime V2
regression step because three `tests/test_card_text_gate.py` fixtures predate
the Dream proposal `rationale` requirement introduced by `97d4adce`.

`parse_dream_consolidations()` now intentionally drops proposals without a
non-empty rationale before validating their result text. The stale fixtures
therefore exercise the missing-rationale branch and return a valid empty result
instead of reaching the placeholder-content gate they are meant to test.

## Decision

Update only the affected test fixtures so every otherwise valid Dream proposal
contains a short, substantive `rationale`. Do not change the production parser,
relax the rationale requirement, reorder validation, weaken assertions, or
remove the tests from CI.

This preserves both contracts independently:

- rationale-free destructive proposals remain rejected;
- proposals with valid structure but placeholder summary/content still produce
  `invalid_card_content:*` on the first attempt and
  `invalid_card_content_after_retry:*` when the relaxed retry is entirely dirty.

## Scope

- Modify `tests/test_card_text_gate.py` only.
- Add rationale fields to `_CARD_A`, `_CARD_B`, both rows in the mixed
  dirty/clean fixture, and the all-dirty retry fixture.
- Keep all existing assertions unchanged.
- Do not change public APIs or documentation.

## Verification

1. Record the existing three-test failure as the TDD red state.
2. Run the three failed tests after the fixture repair.
3. Run all of `tests/test_card_text_gate.py`.
4. Run the exact CI Round 3 pytest command from `.github/workflows/ci.yml`.
5. Run Ruff and `git diff --check`.
6. Push the resulting commit to `test` and verify the new CI run.
