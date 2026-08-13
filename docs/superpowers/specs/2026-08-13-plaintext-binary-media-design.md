# Plaintext Binary Media and Capture Design

## Problem

Pre-production users whose effective content-encryption mode is `off` cannot
send hosted chat images or files. The hosted route passes the raw binary bytes
to the plaintext envelope builder, which accepts UTF-8 text only and returns
`plaintext_body_not_utf8`. The request therefore ends as a 409 before the chat
message or agent job is stored.

The iOS broadcast extension and perception-photo pipeline also always construct
encrypted v1 envelopes. When encryption context is unavailable in effective
plaintext mode, they drop captures locally even though the backend already has
plaintext frame primitives.

## Goals

- Make hosted chat images and files work when effective encryption is `off`.
- Make iOS screen sharing work in effective plaintext mode.
- Make iOS perception-photo evaluation work in effective plaintext mode.
- Preserve the existing encrypted envelope, attestation, and verification chain
  byte-for-byte when effective encryption is `on`.
- Fail closed when the effective mode is unknown or stale: clients must use the
  encrypted shape unless the server explicitly reports `off`.

## Non-goals

- Changing encrypted-mode cryptography, recipients, attestation verification,
  certificate pinning, or enclave measurements.
- Migrating existing stored rows between plaintext and encrypted shapes.
- Enabling plaintext writes outside environments whose deployment gate already
  permits them.
- Reading or replaying the affected user's private content during validation.

## Chosen Design

### Shared envelope representation

Binary plaintext content uses the existing envelope representation:

```json
{
  "v": 1,
  "id": "<random item id>",
  "body_b64": "<strict base64>",
  "body_size_bytes": 123,
  "owner_user_id": "<authenticated user>",
  "visibility": "shared"
}
```

Text plaintext content continues to use `body`. Encrypted content continues to
use `body_ct`, `nonce`, `K_user`, and `K_enclave` exactly as today.

The backend envelope builder will accept an explicit content kind rather than
guessing from byte validity. Hosted image and file callers request binary
storage; text and captions request text storage. In effective plaintext mode,
binary requests produce `body_b64`; in encrypted mode, both kinds still produce
the existing encrypted envelope.

This avoids a dangerous fallback where arbitrary invalid UTF-8 text silently
changes storage shape and makes the call site responsible for declaring the
content contract.

### Hosted chat image and file flow

`/v1/model_api/chat/send` keeps its current public payload. After validation:

1. Image/file bytes are parsed under the existing MIME and size limits.
2. The hosted route requests a binary shared envelope.
3. Effective `off` produces `body_b64`; effective `on` uses the existing
   encrypted envelope and enclave key material.
4. Captions remain UTF-8 text envelopes.
5. The existing chat persistence, R2-pointer, idempotency, job admission, and
   agent-runtime flow remains unchanged.

### iOS effective-mode publication

The main app already receives `content_encryption_effective`. It will mirror the
effective value into the App Group alongside the current user and enclave key
material. Missing, malformed, or stale state resolves to `on` for capture
writers.

The broadcast extension reads a small mode-aware context:

- `on`: require fresh verified enclave metadata and emit the current encrypted
  v1 frame.
- `off`: require the authenticated user id and emit a plaintext binary frame
  envelope with `body_b64`.
- unknown: skip the frame and log the fail-closed reason.

Only the envelope wrapper changes; the WebSocket route metadata (`type`, `ts`)
and encoded inner frame payload stay the same.

### Perception-photo flow

The photo evaluation request becomes shape-polymorphic:

- `on`: send the existing `content_envelope` and optional encrypted
  `meta_envelope`.
- `off`: send the photo bytes as a strict binary plaintext envelope. Sensitive
  derived metadata that was previously encrypted must not be promoted to
  plaintext; it is omitted unless a dedicated bounded plaintext contract
  already exists.

The backend validates exactly one recognized content shape, authenticated owner
binding, shared visibility, strict base64, decoded size, and effective-mode
gate before evaluation. Encrypted accounts cannot submit plaintext photos.

## Trust and Verification Boundaries

- The server remains authoritative for the effective encryption mode.
- Plaintext shapes are accepted only when both the deployment gate and the
  user's effective preference are `off`.
- The authenticated user id must match `owner_user_id`; clients cannot select
  another owner.
- `local_only` is rejected for plaintext because the server can inherently read
  it.
- Encrypted mode continues to require fresh attested enclave key material on
  capture writers. No Cloudflare/domain routing or enclave verification rule is
  changed.
- Size and MIME limits are enforced before persistence and after base64 decode
  where applicable.

## Error Handling

- Hosted binary requests no longer return `plaintext_body_not_utf8` in effective
  plaintext mode.
- Invalid or oversized plaintext envelopes return stable 4xx errors and do not
  create chat rows, frames, photos, or jobs.
- Unknown client encryption state fails closed to encrypted mode; if keys are
  unavailable, capture is skipped with a diagnostic log instead of sending
  plaintext.
- Switching modes affects new writes only. Queued capture work must re-read the
  current effective mode before wrapping each item.

## Test Matrix

Backend regression tests will prove:

- Effective-off hosted image and PDF sends persist binary plaintext envelopes
  and enqueue the normal job.
- Effective-on hosted image and PDF sends retain encrypted envelope fields.
- Text and caption behavior is unchanged.
- Plaintext frame and photo bodies accept valid `body_b64` only for effective-off
  users and reject wrong owner, local-only, invalid base64, and excessive size.
- Existing encrypted frame and photo contracts still pass.

iOS tests will prove:

- Broadcast wrapping emits the encrypted v1 shape for `on` and binary plaintext
  shape for `off`.
- Missing/unknown/stale mode never emits plaintext.
- The effective mode is mirrored to and read from the App Group correctly.
- Perception-photo request construction selects the correct shape without
  leaking encrypted-only sensitive metadata into plaintext requests.

Fresh backend tests, iOS unit tests, and an iOS build are required before the
change is considered complete. Pre validation must cover chat image, PDF,
screen frame arrival, and perception-photo evaluation without inspecting user
content.

## Documentation and Rollout

Because the change affects trust boundaries and deployment behavior, update the
public architecture/trust documentation and the `Unreleased` changelog. Roll
out backend compatibility before the iOS client, then validate on pre. The
encrypted path requires no coordinated cutover because its wire shape is
unchanged.
