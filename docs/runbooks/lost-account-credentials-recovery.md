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

### Plaintext TEE-primary import into a new UID

If the requested outcome is to retain the **new** UID, inspect the live database
contract first. The credential-reassignment procedure below retains the old UID
and does not implement that outcome. On a verified `tee` primary with
`FEEDLING_PLAINTEXT_WRITES_ACCEPTED=1`, plaintext content can instead be copied
with `tools/import_plaintext_account_content.py` inside the backend container.
This tool is distinct from the same-public-key orphan migration.

Its default invocation is read-only and verifies every attachment's size and
SHA-256. It imports chat, memory, and identity; `--include-settings` also copies
`proactive_settings` and `web_settings`. It preserves source rows and both user
documents, including credentials and public keys. Sealed or `local_only`
material in the selected content fails closed. Provider credentials, model
routes, MCP configuration, perception/frame history, import checkpoints,
runtime state, and logs are outside its scope and require separate review.

Attachments must be copied into the destination UID's object-storage prefix;
reusing the source pointer fails the ownership check. The tool allocates fresh
versioned keys in the destination storage generation and verifies copied bytes.
Its database transaction fences concurrent writes and refuses to proceed when
the source/destination snapshot differs from the reviewed `--expected-plan`.
The plan contains counts and a digest, never content or credentials.
For a new destination without a chat-object lifecycle row, the plan reports
initialization at generation zero; only the apply transaction creates that row.
Concurrent lifecycle initialization invalidates the reviewed plan.

Before applying, establish ownership, approve the concrete import scope, and
review the dry-run plan with another operator. Supply `--operator`, `--ticket`,
`--backup-dir` (existing mode `0700` directory), `--expected-plan`, and `--apply`.
The backup is mode `0600` and contains both users, source content, and overwritten
destination settings. Keep it inside the protected production environment.
Production application requires final confirmation after plan review.

After application, verify counts, source preservation, destination ownership,
attachment access, unchanged credential/public-key documents, and functional
history/Memory Garden/identity checks in the App. Reopen the App before checking.
Do not rerun an import after a committed or ambiguous result: the destination is
no longer empty. Investigate from the protected backup and database first.
On failure, copied objects are retained because a COMMIT response may be lost;
cleanup and rollback require a separately reviewed manifest-based operation.

Synthetic integration checks:

```bash
ACCOUNT_IMPORT_TEST_DSN=<isolated-local-pg-dsn> \
  python3 tests/test_import_plaintext_account_content.py -v
```

### Expanded copy into the new UID

Use `tools/import_full_account_content.py` only after explicitly reviewing the
expanded scope. Run it inside the backend of a verified plaintext TEE primary.
The default is read-only, including every external object's full SHA-256 check
and an enclave decrypt/reseal/decrypt roundtrip for each selected sealed
envelope. It never accepts raw user credentials or private keys.

The explicit allowlist covers live and archived chat, memories, world book,
perception items and daily history, authoritative `frame_envelopes`, model
credentials and routes, terminal genesis import jobs and outputs, identity,
persona/voice, MCP and user settings. Model UUIDs are remapped together with
their foreign keys. Sealed envelopes receive fresh body keys and wraps for the
destination public key and current enclave, with destination-owner AAD.
Objects are copied into the destination UID's prefix and read back to verify.
The legacy `frames` mirror is not an additional source of business history.
PerceptKit's `subject_id`-owned observations, daily aggregates, deduplication
identities, ingest receipts, retractions and calendar/reminder mirrors are
included too. Its current-value projection, rule/sync state and delivery outbox
are preserved on the destination rather than replayed from the source.

Existing destination perception history is retained; colliding primary keys
fail closed. For daily aggregates only, the explicitly reviewed
`--archive-aggregate-conflicts` policy may retain the destination's display
values and preserve complete conflicting source rows in the destination's
`account_import_aggregate_history` blob. This is preservation, not arithmetic
merging: trends on those dates still show the destination values. Use it when
source observations are incomplete or overlap and recomputation cannot be
proven correct. The plan reports the archived count and display policy.
Core destination content/configuration must be empty. Selected
settings and frame indexes are explicitly shown in the overwrite plan.
Capture progress is mapped from the source watermark to newly assigned live
chat sequence numbers; dream watermarks are retained without pending/error
state. Both complete user documents, device tokens, current device observations,
runtime ownership/allowlists and runtime profile remain unchanged. Logs, old
worker state, consumed staging tombstones and legacy model configuration
projections remain with the source and are not replayed.

The final plan includes a policy version and snapshot token. Production apply
requires another operator's review and final confirmation of that plan, then
`--expected-plan`, `--operator`, `--ticket`, an existing mode `0700`
`--backup-dir` inside the protected production environment, and `--apply`.
The mode `0600` backup contains both users, both selected database snapshots,
and source-object manifests. A separate mode `0600` append-only upload journal
records destination object keys, sizes and hashes as copies finish. Journal
records are serialized while at most six objects are copied and verified
concurrently. All copy workers finish before the database write fence begins.
A short table write fence rechecks the complete snapshot before inserts.
Source preservation, exact destination rows, unchanged
user documents and selected blob values are checked within the transaction and
again after commit. Policy v2 excludes only the idle source capture loop's
`capture_state.updated_at` from the snapshot token: it is not imported, while
the actual capture watermark and every other field remain guarded. This avoids
an idle timestamp refresh invalidating a large copy. Real chat insert triggers rebuild the chat change stream;
transactional wake notifications refresh the affected store sections.

An ambiguous commit or postflight failure must be investigated from the backup;
never rerun apply blindly or delete uploaded objects. Any rollback or orphan
object cleanup needs a separate manifest-based review. Keep the App closed
during the final plan/apply interval; source or destination changes invalidate
the reviewed token. Afterward reopen the App and verify chat, memory, persona,
world book, images, MCP and model operation before declaring recovery complete.

Synthetic integration checks (real PostgreSQL, real envelope crypto, production
chat change triggers):

```bash
ACCOUNT_IMPORT_TEST_DSN=<isolated-local-pg-dsn> \
  python3 tests/test_import_full_account_content.py -v
```

### Encrypted lost-credential recovery preserving the old UID

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
