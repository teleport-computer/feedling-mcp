# Lost account credentials recovery

Date: 2026-08-10

## Purpose

Recover an existing Feedling account when all of the following are true:

- support knows the old `user_id`;
- the user no longer has the old API key;
- the user no longer has the old X25519 content private key;
- no other device can access the old account; and
- the user can register a new, otherwise empty account on the current device.

This runbook is a recovery design. It does not authorize ad-hoc production SQL.
Implement the credential reassignment as a narrowly scoped, audited tool with a
dry-run mode before using this procedure on production.

## Recovery boundary

The recovery preserves the old account and reuses the current device's newly
generated credentials:

- the new account's raw API key remains unchanged on the phone;
- the server atomically reassigns that key's hash from the new account to the
  old account;
- the phone keeps its newly generated content private key; and
- the existing `/v1/content/rewrap-to-current-key` flow rewraps old shared
  content to the matching new public key.

Do not move chat, memory, identity, or other encrypted rows to the new
`user_id`. Envelope bodies are authenticated with AAD containing
`owner_user_id | version | item_id`. Changing row ownership without decrypting
and resealing the envelope breaks that binding.

Only `shared` content with a usable `K_enclave` can be recovered. `local_only`
content has no `K_enclave`; without the old private key it is cryptographically
unrecoverable. Tell the user this before starting.

## Security prerequisites

Knowing an old `user_id` is not proof of account ownership. Before changing any
credential mapping, support must verify that the requester owns the old account
using the strongest evidence available outside this procedure. Record the
evidence type, operator, timestamp, old `user_id`, and new `user_id` in the
recovery ticket. Do not put private keys, raw API keys, or sensitive user
content in the ticket or command output.

Stop if any of these conditions is not met:

- ownership cannot be established;
- the new account already contains data that must be preserved;
- the new account has more than one unexplained live API key;
- the old account cannot be identified unambiguously; or
- the old shared envelopes can no longer be opened by the current enclave key.

## User procedure

1. Register a new account in the current App installation.
2. Do not chat, import data, configure providers, reset the account, reinstall
   the App, or clear its local storage.
3. Send support the new `user_id`, old `user_id`, and the agreed ownership
   evidence. Never send the raw API key or content private key.
4. Keep the App installed. It currently holds the new account's API key and the
   new device-local content private key; both are needed for recovery.
5. Wait for support to complete the server-side credential reassignment.
6. Force-quit and reopen the App. Do not send a message until the old account
   identity and history have loaded.
7. If a page reports encrypted content that cannot yet be read, use its retry
   action or reopen it. Large histories may require multiple idempotent rewrap
   attempts.
8. Confirm that old chat, Memory Garden, identity/agent configuration, and a
   newly sent test message are readable.
9. Export and securely back up the new content private key after recovery.

## Operator procedure

### 1. Read-only preflight

Resolve and inspect both user rows. Capture a metadata-only baseline:

- old and new `user_id`;
- creation timestamps and public-key fingerprints;
- live API-key counts and key IDs;
- access modes and active route;
- counts for old and new chat, memory, identity, and other per-user data;
- counts of `shared` and `local_only` envelopes; and
- the current TEE replication/decrypt health relevant to the old user.

Verify that the new account is the just-created empty claimant account. Back up
the two complete `users` documents to the approved protected recovery location.
Do not print credential hashes in routine logs.

### 2. Dry-run the reassignment

The recovery tool should accept explicit `--old-user-id` and `--new-user-id`
arguments and produce a plan without writing by default. It must fail closed
unless all of the following hold:

- both rows exist;
- the IDs differ;
- the claimant account is empty under the tool's documented definition;
- the claimant has exactly one selected live key entry;
- that key hash resolves only to the claimant before the operation; and
- the destination does not already contain the same key hash.

Review the plan with a second operator for production recovery.

### 3. Atomically reassign the claimant key

In one database transaction, the tool must:

1. copy the selected live `api_keys[]` entry from the new account to the old
   account, preserving its raw hash, access mode, and creation metadata while
   adding a recovery audit label;
2. create or update the old account's corresponding `access_bindings` entry;
3. remove that key entry from the new account;
4. remove the new account's top-level legacy `api_key_hash` if it is the same
   selected hash, so the hash cannot resolve to two users; and
5. persist both user documents.

Do not change the old account's `public_key` during this transaction. Do not
change any encrypted content row's `user_id`. Do not delete either user row.

After commit, publish targeted `users` reload notifications for the new user
first and the old user second, or use an equivalent safe registry refresh. This
order ensures workers converge on the old account as the final owner of the
hash. Verify against the database that the selected hash matches the old row
only. If worker convergence cannot be proven, perform the approved backend
rolling restart before asking the user to reopen the App.

### 4. Let the existing client rewrap

When the user reopens the App:

1. its unchanged raw API key authenticates as the old `user_id`;
2. `/v1/users/whoami` corrects the locally cached `user_id` to the old one;
3. iOS sees that the old account public key does not match its new local key and
   correctly refuses to overwrite it directly;
4. history loads but cannot be opened through the new device's `K_user`;
5. the decrypt-failure recovery calls
   `/v1/content/rewrap-to-current-key` with the new device public key; and
6. the enclave opens eligible shared envelopes through `K_enclave`, reseals
   them under the new public key while retaining the old owner/AAD, and finally
   advances the old account's registered public key.

The rewrap endpoint may make partial progress. Partial progress is safe and
must be retried until the pending set is empty or every remaining item has a
documented terminal reason.

### 5. Observe and verify

Monitor metadata and structured status only. Confirm:

- `whoami` resolves the claimant key to the old `user_id`;
- rewrap returns `ok`, or repeated `partial` results make monotonic progress;
- the pending/error count reaches zero or has an explained terminal remainder;
- no `owner_user_id` mismatch is reported;
- the old account's registered public-key fingerprint becomes the new device
  fingerprint;
- eligible chat, memory, identity, and supported sub-envelopes converge to the
  new content-key fingerprint; and
- the user's functional checks pass.

Do not claim recovery of `local_only` records. Report their count separately.

### 6. Close out

After the user confirms recovery:

- revoke obsolete old-account API-key entries, including handling any legacy
  top-level hash so it cannot remain an unrevoked back door;
- retain the reassigned claimant key as the active credential;
- mark the new account as a recovery-created empty shell;
- keep the shell for the agreed observation window before any separate,
  reviewed deletion;
- attach preflight and postflight counts to the recovery ticket; and
- remind the user to back up the new private key.

## Rollback boundary

Before rewrap starts, the credential reassignment can be reversed atomically
using the saved user documents and the same targeted registry refresh.

After any envelope has been rewrapped, do not move the API key back to the new
account. Some old-account records now require the new private key while the
business rows still belong to the old `user_id`. Continue forward with
idempotent rewrap retries and investigate the remaining errors.

Restoring an entire old database snapshot after recovery traffic begins can
overwrite new user activity. Treat it as disaster recovery, not the normal
rollback path.

## Why the legacy orphan tool is not suitable

`tools/recover_orphan_accounts.py` was designed for duplicate accounts sharing
the same content public key. It changes the owning `user_id` of selected rows.
This case has different old and new content keys plus current owner-bound AAD,
so that tool must not be used for this recovery.

## Relevant implementation references

- `backend/accounts/registry.py`: API-key indexing, persistence, and targeted
  user reload behavior.
- `backend/content/content_core.py`: enclave-assisted
  `/v1/content/rewrap-to-current-key` implementation.
- `docs/CONTENT_ENCRYPTION_INTERACTION_CURRENT.md`: current envelope and
  recovery boundaries.
- iOS `FeedlingAPI.swift`: `whoami` key-mismatch guard and automatic content
  rewrap after decrypt failure.
- iOS `ContentEncryption.swift`: AAD construction and local envelope unsealing.
