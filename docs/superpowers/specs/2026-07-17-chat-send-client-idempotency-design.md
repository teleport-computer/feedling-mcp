# Chat send client idempotency design

**Date:** 2026-07-17
**Status:** Approved for implementation
**Scope:** `POST /v1/model_api/chat/send`, `POST /v1/chat/message`

## Problem

Both chat-send paths persist the user row before the caller necessarily receives
the response. A lost response can therefore make a successful send look failed.
If the client retries with a newly sealed envelope, the envelope ID is also new,
so the existing `(user_id, msg_id)` primary key cannot recognize the logical
retry. Two rows are stored, two consumer wakes are emitted, and two assistant
turns may run.

The encrypted envelope ID cannot be replaced with a deterministic server ID:
it participates in the envelope's AEAD additional data. The retry identity must
therefore be separate plaintext metadata with no content semantics.

## Public contract

Both request bodies accept an optional `client_msg_id`:

- It must be a UUID string. The server parses and stores its canonical lowercase
  representation. An invalid value returns `400 client_msg_id_invalid` before a
  chat row is written.
- It is plaintext routing metadata, not message content and not part of the E2EE
  envelope.
- For the same authenticated user, the same value identifies one accepted user
  row for 600 seconds from that row's original timestamp.
- A retry during that window returns the original row and does not append, wake
  a consumer, schedule capture work, or start another logical turn.
- After 600 seconds, the same value may create a new row. A different value
  creates a new row immediately.
- When the field is absent, all existing validation, append, notification, and
  response behavior remains unchanged.

`/v1/chat/message` returns the original row's existing `{id, ts, v}` shape.
`/v1/model_api/chat/send` passes the original user row through the existing
hosted response builder, so it remains HTTP 202 and may report an already-stored
assistant row with `reply_ready: true`.

The key is scoped only by authenticated `user_id`, not by route. This is
intentional: a retry that accidentally switches between the hosted and resident
ingest endpoints must still collapse onto the first accepted row.

## Storage and concurrency

`client_msg_id` is allowlisted into the chat row document. The idempotent store
path constructs a candidate row and then performs the decision in one PostgreSQL
transaction:

1. Take `pg_advisory_xact_lock` over a stable hash of `user_id` plus the
   canonical UUID.
2. Query that user's newest chat row with the key whose `ts` is within the
   600-second window.
3. If found, return its authoritative document with `inserted=false`.
4. Otherwise insert the candidate, apply the normal chat-ring trim, commit, and
   return it with `inserted=true`.

The transaction-scoped advisory lock serializes contenders across processes and
hosts that share PostgreSQL. Hash collisions can only serialize unrelated
operations; they cannot merge their rows because the subsequent query still
compares the full user ID and UUID. The per-user ring is capped at 5,000 rows,
so the JSON metadata lookup is bounded without a new durable index or reservation
table.

Database errors on this path fail closed. Treating an unavailable lookup as a
miss would violate the guarantee by inserting a second row. The legacy no-key
append retains its existing best-effort behavior.

Large image/file ciphertext keeps the existing crash-safe object-storage flow:
insert inline, upload after commit, then atomically replace `body_ct` with the
pointer. Only a winning insert performs that work. Trimmed pointer objects and
TEE pending rows are cleaned exactly as in the legacy append path.

## Cache and side effects

The winner document is reconciled into the current worker's `chat_messages`
cache by message ID. Only `inserted=true` performs cross-worker wake and capture
scheduler side effects; endpoint-local waiter notification and route debug trace
are also emitted only for the winner.

On a hosted duplicate, the store is reloaded before the existing reply lookup so
an assistant row written through another worker can be returned immediately.

## Verification

Tests cover:

- same user/key twice stores one row and returns the same row pointer;
- simultaneous calls through independent stores still produce one winner;
- different keys produce two rows;
- no key preserves duplicate legacy behavior;
- an expired key produces a new row;
- the two endpoints accept/validate the field and retain their response shapes;
- a duplicate produces no second waiter/wake/capture side effect;
- the public OpenAPI schemas and client retry documentation describe the new
  contract.
