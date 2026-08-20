# Runtime V2 Plaintext Generated Image Delivery Design

## Problem

Runtime V2 can successfully generate an image for an account whose effective
content-encryption mode is `off`, but cannot publish that image into Chat. The
generated raster bytes are passed to `_build_shared_envelope_for_store`, whose
plaintext branch accepts UTF-8 text only. Binary image bytes therefore return
`plaintext_body_not_utf8`; the provider has already generated the image, but
the turn ends as `turn_failed:runtimeerror` without an image message.

## Scope

This change fixes only Runtime V2 generated-image delivery for effective-off
accounts. It does not change iOS uploads, screen sharing, perception photos,
ordinary file delivery, image-generation routing, or existing stored rows.

Encrypted generated-image delivery must retain its current envelope fields and
behavior.

## Design

Extend `_build_shared_envelope_for_store` with an explicit keyword-only
`content_kind` argument. Supported values are `text` and `binary`, with `text`
as the default so existing callers are unchanged.

- Effective encryption `on`: both content kinds use the existing encrypted
  envelope path without changing its wire shape.
- Effective encryption `off` plus `text`: decode UTF-8 and emit the existing
  plaintext `body` shape.
- Effective encryption `off` plus `binary`: emit the existing plaintext binary
  shape with strict base64 `body_b64`, exact `body_size_bytes`, item id, owner,
  and `visibility=shared`.
- Unknown `content_kind`: fail explicitly rather than guessing from whether the
  bytes happen to decode as UTF-8.

The Runtime V2 generated-image effect builder passes
`content_kind="binary"`. No other call site changes in this task.

## Data Flow

1. The companion calls `generate_image`.
2. The configured image route returns provider media.
3. Runtime V2 validates and normalizes the raster under the existing image size,
   format, and MIME limits.
4. The generated-image effect builder requests a binary shared envelope.
5. An effective-off account receives a `body_b64` envelope; an effective-on
   account receives the existing ciphertext envelope.
6. The existing transactional reply sink stores the image as a native
   `content_type=image` Chat message.

## Error Handling and Boundaries

- Binary plaintext output reuses the repository's established `body_b64`
  representation; it does not invent a new wire shape.
- The call site declares binary intent. Invalid UTF-8 text does not silently
  become binary content.
- Existing generated-image normalization remains authoritative for decoded
  size, pixel count, MIME, and stored-byte limits.
- Existing owner binding, message id derivation, reply fencing, transactional
  commit, and idempotency remain unchanged.
- Provider generation failures and publication failures retain their current
  separation; this change addresses only envelope construction after successful
  generation.

## Tests

Follow red-green TDD:

1. Add a builder regression proving effective-off binary bytes produce strict
   `body_b64` and exact size while the default text call still rejects invalid
   UTF-8.
2. Add a Runtime V2 generated-image effect regression proving effective-off
   image bytes build a valid image reply payload rather than raising
   `plaintext_body_not_utf8`.
3. Preserve or add an effective-on assertion proving ciphertext fields remain
   present and plaintext fields remain absent.
4. Run the focused envelope and Runtime V2 image suites, then the relevant
   broader Runtime V2 reply tests and `git diff --check`.

## Success Criteria

- A configured image model can generate and save a native Chat image for a
  Runtime V2 user with `content_encryption=off`.
- The saved plaintext image uses `body_b64`, not UTF-8 `body`.
- Runtime V2 encrypted generated-image delivery is unchanged.
- No iOS, screen, perception, ordinary-file, or public API behavior is changed.
