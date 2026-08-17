# Plaintext Genesis Envelope Read Design

## Problem

Genesis estimate/commit stages an import with
`core_envelope._build_shared_envelope_for_store`. Encrypted accounts receive a
sealed `body_ct` envelope, while plaintext accounts receive a UTF-8 `body`
envelope. The commit loader bypasses the repository's shape-aware read boundary
and always calls the enclave decrypt endpoint, so plaintext commits fail before
a Genesis job is created. Checkpoint loading repeats the same assumption.

## Decision

Both staged-payload and checkpoint loading will call
`core_envelope.read_envelope_body`. That existing boundary reads `body` and
`body_b64` locally and sends only `body_ct` envelopes to the enclave. The
enclave API will remain ciphertext-only; it will not be expanded to accept
plaintext.

## Error and security behavior

Digest validation, JSON validation, staged expiry, consumption, ownership, and
encrypted-envelope authentication remain unchanged. Plaintext content stays in
the backend process that already received and staged it, avoiding an unnecessary
backend-to-enclave round trip. Encrypted content retains the current enclave
decrypt path and credentials.

## Tests

- A staged plaintext envelope must round-trip through the real loader without
  calling enclave decryption.
- A plaintext checkpoint must round-trip through the real loader without
  calling enclave decryption.
- Existing encrypted staged-payload and checkpoint tests must continue to pass,
  proving ciphertext still routes through the enclave.

## Rollout

Run the focused Genesis service tests, the broader Genesis test set, and the
documentation checks required for a user-visible behavior fix. Commit and push
directly to `pre` as explicitly authorized, then confirm PRE deployment and the
plaintext estimate/commit boundary.
