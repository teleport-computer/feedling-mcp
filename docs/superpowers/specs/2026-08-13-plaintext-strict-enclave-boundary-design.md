# Plaintext Strict Enclave Boundary Design

**Date:** 2026-08-13

## Goal

Make plaintext-tier content fully usable without depending on the enclave. Normal
backend and hosted-runtime workflows must process persisted `body` / `body_b64`
locally and send only `body_ct` envelopes to enclave decrypt endpoints, while
preserving encrypted-tier behavior and mixed-history compatibility.

## Context

The first PRE regression was caused by Genesis staged payload and checkpoint
loaders sending a plaintext `{body}` envelope to `/v1/envelope/decrypt`, whose
sealed-envelope contract correctly requires `body_ct`. The immediate fix routed
those two reads through `core.envelope.read_envelope_body`.

A follow-up audit found the same assumption in other workflows:

- History Search/Fetch projects away plaintext chat bodies before enclave scan.
- Genesis persona backfill indexes `envelope["body_ct"]` after a shape-aware
  builder; existing persona/voice reads in the Genesis worker always decrypt.
- resident agent persona loading rejects an envelope without `body_ct` before
  reaching its shape-aware reader.
- plaintext Genesis and legacy History Import reject plaintext Identity before
  calling their shape-aware reader.
- voice hangup reconstruction always decrypts chat rows through the enclave.
- Runtime V2 reasoning flattening persists only `thinking_body_ct`.
- memory-upgrade CAS hashes only `body_ct`, giving every plaintext body the same
  token.

Memory, World Book, Chat, and Identity also have batch or decrypt-and-serve paths
that can successfully read plaintext inside the enclave. They are not immediate
data-shape failures, but they violate the stronger product boundary: an enclave
outage must not make plaintext-tier content unavailable.

## Invariants

1. A persisted envelope with a non-empty `body_ct` is authoritative and may be
   sent to the enclave.
2. A persisted envelope with `body_b64` or string `body`, and no authoritative
   `body_ct`, is decoded locally and must not trigger enclave HTTP or crypto
   calls.
3. Rows with both ciphertext and plaintext fields follow the existing
   migration rule: ciphertext wins; stale plaintext is never used as fallback.
4. Unknown, malformed, wrong-owner, and invalid-base64 shapes fail explicitly;
   they are not treated as empty content.
5. Mixed histories remain supported. Routing is per row, not per account,
   because toggles and migrations leave old encrypted rows beside new plaintext
   rows.
6. Encrypted-tier behavior, authorization, ordering, cursor advancement,
   filtering, caps, and public response shapes remain unchanged.
7. Compatibility enclave routes may retain plaintext-aware defensive decoding,
   but normal backend and hosted-runtime call paths must not send plaintext to
   them.

## Architecture

### 1. Shared shape primitives

`core.envelope` remains the source of truth for shape precedence. Add small
public helpers for:

- classifying an envelope as `sealed`, `plaintext_text`, `plaintext_binary`, or
  `invalid`;
- reading only a plaintext shape locally with owner validation where the caller
  has an authenticated user id;
- deriving a stable content CAS token from the authoritative stored shape.

`read_envelope_body` continues to be the single-row convenience API: sealed
rows invoke the enclave; plaintext rows call the local primitive. Callers must
not place a `body_ct` guard in front of it.

The helpers do not infer content from arbitrary `content`, `text`, or
`plaintext` keys. They accept only the persisted envelope contract.

### 2. Direct single-row fixes

Replace residual sealed-only assumptions with the shared primitives:

- Genesis persona backfill stores plaintext chunk bytes when the builder returns
  `body`, and ciphertext bytes only when it returns `body_ct`.
- Genesis worker persona/voice reads route by shape before any enclave call.
- resident persona loading accepts every recognized envelope shape.
- Genesis Identity merge and History Import preferred-name lookup accept
  plaintext Identity rows.
- voice transcript reconstruction reads plaintext chat rows locally.
- Runtime V2 reasoning uses `envelope_prefixed_fields`, matching the existing
  chat service implementation and preserving `thinking_body`.
- memory migration and action CAS tokens hash a canonical tuple containing the
  authoritative shape name and body value; concurrent plaintext edits therefore
  change the token.

### 3. Batch readside partitioning

Batch workflows partition each bounded candidate set by actual row shape before
transport.

#### Memory Index/Fetch

Plaintext memory inners are parsed locally and passed through the same pure item
builders and filters used for encrypted results. Only sealed candidates are sent
to `/v1/memory/index` or `/v1/memory/fetch`. Results are merged by the original
candidate/request order, and unavailable/sensitive ids retain existing public
semantics. An all-plaintext request performs zero enclave calls.

#### History Search/Fetch

The backend projection keeps plaintext text/caption fields locally and sends
only sealed projections to `/v1/history/*`. Local scanning uses the same
normalization, matching, snippet, and truncation rules as the enclave route.

For a mixed scan batch, both sides process the same bounded original prefix.
Encrypted responses retain row sequence ids; the backend merges local and
encrypted matches by the original descending sequence order. Cursor advancement
uses the greatest contiguous prefix confirmed as checked by both paths. If the
encrypted sub-scan times out or fails, the mixed batch does not advance beyond
the last safe confirmed row. An all-plaintext batch performs zero enclave calls.

Summary leaf hints follow the same rule: plaintext summary envelopes are matched
locally; only sealed summary envelopes are sent to the enclave.

#### World Book

Plaintext entries are length-checked and matched locally. Only sealed entries are
sent to `/v1/worldbook/match`. The internal enclave response is extended with a
per-entry rendered projection carrying entry id, match status, ordering metadata,
and the same model-visible text already present in the aggregate block. The
backend interleaves local and encrypted projections in canonical World Book
order, then emits the unchanged public aggregate response. No additional
encrypted content is exposed beyond the existing returned block.

Plaintext upsert validation checks the content cap locally and performs no
enclave request.

### 4. Hosted Chat and Identity routing

Hosted processes must stop using decrypt-and-serve endpoints as an unconditional
first hop.

- Resident Chat fetches the raw authenticated backend history window. It reads
  plaintext rows locally and sends only sealed main/caption envelopes to the
  existing enclave decrypt capability. It then reconstructs the same ordered
  history response and context metadata.
- Identity action/supervisor reads fetch the raw Identity document first.
  Plaintext Identity is parsed locally; sealed Identity uses the enclave.
- Runtime V2 paths already using `read_envelope_body` keep that behavior and gain
  regression tests proving the no-enclave plaintext branch.

The enclave Chat/Identity compatibility endpoints remain available for encrypted
clients and defensive mixed-shape handling, but server-owned plaintext workflows
do not call them.

## Error Handling

- Plaintext JSON decoding errors remain visible under the workflow's existing
  stable error category; they must not be relabeled as decrypt failures.
- A sealed-row enclave failure retains the current retryability and error code.
- Mixed batches do not silently drop one shape. Partial encrypted failure either
  returns the existing explicit upstream failure or advances only the proven
  contiguous prefix.
- Best-effort workflows may retain their documented fallback, but tests must
  assert that the fallback is not triggered merely because the row is plaintext.
- No exception message, trace, or debug payload includes plaintext content.

## Testing Strategy

Every production change follows red-green TDD. Tests use enclave/network spies
that raise immediately when a plaintext branch touches them.

Required regression coverage:

- Genesis staged/checkpoint tests remain green.
- persona backfill, existing persona/voice, and resident persona work for
  plaintext and still decrypt sealed rows.
- plaintext Identity contributes to Genesis merge and History Import naming.
- voice reconstruction preserves plaintext turns.
- V2 reasoning persists and reads `thinking_body`.
- plaintext memory CAS tokens differ when content differs.
- Memory Index/Fetch, History Search/Fetch including leaf hints, World Book
  upsert/match, hosted Chat, and Identity perform zero enclave calls for
  all-plaintext input.
- mixed-shape tests prove encrypted rows still call the enclave, results keep
  canonical order, and cursor/error semantics do not regress.
- malformed and dual-shape tests preserve ciphertext precedence and explicit
  failure behavior.

Verification gates before PRE deployment:

1. focused red-green tests for each changed subsystem;
2. all tests selected by `-k plaintext` with `tests/test_api.py` excluded;
3. adjacent Genesis, Memory, History, World Book, Chat, Identity, Voice, and V2
   suites;
4. full backend suite against the real local Postgres baseline;
5. pyflakes for changed backend packages;
6. OpenAPI contract tests;
7. documentation `types:check`, `lint`, and `build`.

## Documentation

This changes architecture and the plaintext-tier trust boundary without changing
the public request/response contract. Update the relevant pages under
`docs-site/content/docs/` to state that plaintext content is processed by the
backend path and only ciphertext is submitted to enclave decryption. Review the
architecture diagram, workflow pages, self-hosting trust model, and add an
`Unreleased` changelog entry. OpenAPI regeneration is unnecessary unless the
implementation changes an endpoint schema.

## Deployment and PRE Validation

Implement on a branch based on current `origin/pre`, keep unrelated worktree
changes out of the commits, then push the verified task commits to `pre` using
the repository's allowed promotion flow.

After CI deploys PRE:

- verify workflow, image tag, attestation, canary, and `/healthz` release commit;
- repeat plaintext long-term-memory and chat-history upload validation;
- exercise at least one plaintext Memory read, History search/fetch, World Book
  match, Identity read, and hosted chat/persona path using metadata-only or
  purpose-built test fixtures;
- confirm logs contain no plaintext-purpose calls to `/v1/envelope/decrypt`, no
  `envelope missing body_ct`, and no new stable-error regressions.

## Non-Goals

- Removing encrypted-tier enclave decryption.
- Migrating or rewriting existing ciphertext rows.
- Changing client-visible envelope schemas or encryption preferences.
- Returning decrypted encrypted-tier content beyond existing model/API outputs.
- Refactoring unrelated runtime, storage, or deployment infrastructure.
