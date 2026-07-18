# Runtime V2 workspace, working memory, and sandbox boundary

Runtime V2 owns a virtual workspace. A model-visible path is never evidence that
the same path exists on the runner host.

## Namespaces

| Namespace | Authority | Mutability |
| --- | --- | --- |
| `/artifacts/*` | Text views derived from user artifacts | Read-only to the model |
| `/skills/*` | Operator-installed agent instructions | Read-only to the model |
| `/workspace/*` | Task documents and generated files | Model-editable with revision CAS |
| `/memory/WORKING.md` | Agent-maintained continuation state | Model-editable with revision CAS |

`/memory/WORKING.md` is not Memory Garden. Garden cards are user-facing semantic
memories about people, events, and relationships. Working memory is an agent
scratchpad for plans and ongoing project state. A useful item can later be
promoted deliberately into Memory Garden; it is not copied automatically.

## Encryption and plaintext

`v2_workspace_entries.content_envelope` stores a v1 shared envelope. PostgreSQL
sees ciphertext plus plaintext routing metadata (`user_id`, canonical path,
kind, MIME type, source reference, revision, timestamps). The hosted runner asks
the enclave to decrypt an entry for the current user's short-lived runtime token.
Plaintext then exists briefly in the trusted runner and is sent to the user's
chosen model when the turn uses it. The model never receives ciphertext.

Workspace write-tool arguments are also encrypted before they enter the V2
effect outbox. At apply time the runner decrypts that effect, validates it again,
and re-encrypts the file content into the durable workspace entry. This preserves
generation fencing, replay safety, and model-order mutation semantics.

Paths and filenames are metadata. Do not put secrets or document content in a
path. If hiding names becomes a product requirement, add an encrypted directory
index with opaque lookup IDs; do not pretend the current schema hides them.

## Concurrency

Every create, replace, or delete uses an exact revision:

- `expected_revision=0` creates a new file;
- replacing or deleting requires the last observed revision;
- a stale revision returns a conflict instead of silently overwriting.

This is the backend authority needed for future parallel file writes. Reads can
run concurrently. External mutations remain serialized in model order. File
writes may later run concurrently when paths/revisions are disjoint; same-path
conflicts remain explicit.

## Sandbox trigger boundary

These operations do **not** acquire a sandbox:

- ordinary chat and Memory Garden reads;
- reading an already-stored encrypted text view;
- listing VFS metadata;
- editing VFS Markdown/text entries.

These operations **must** acquire a sandbox lazily:

- materializing an uploaded artifact into physical bytes;
- parsing an untrusted PDF, DOCX, XLSX, image, archive, or other binary;
- shell commands;
- user/model-authored code execution.

Cold text-only turns therefore create no sandbox. `SandboxProvider` is a
deployment seam: E2B has an optional conditional-import adapter, while a CVM or
another provider is registered by deployment assembly. There is intentionally
no Docker-socket adapter. The bundled memory provider is test-only, holds bytes
in memory, and refuses all shell/code execution.

The optional E2B adapter requires `E2B_API_KEY` and
`FEEDLING_V2_E2B_TEMPLATE`. It always requests `secure=True`, disables internet
access by default, caps lifetime/command/output/artifact size, and kills the
sandbox on close. Its template must provide the fixed
`/opt/feedling/bin/extract-artifact` contract: read bytes and JSON metadata from
the fixed `/tmp/feedling-artifact*` paths and write UTF-8 text to the fixed
output path. User filenames, MIME types, and model content are never appended
to that extractor command. `FEEDLING_V2_E2B_ALLOW_INTERNET=1` is an explicit
deployment-level opt-in, not a model option.

The repository's older resident `container` strategy is not a usable adapter:
it only builds a `docker run` argv and the live spawner explicitly falls back to
the shared process strategy. The old memory-sandbox compose is a local
backend+enclave validation stack, not an artifact execution API. Until an
audited `cvm` or `e2b` adapter is registered, uploads remain durably stored but a
Runtime V2 binary/text attachment cache miss is surfaced as
`sandbox_unavailable`; it is never parsed in the backend process.

Every successful provider acquisition appends a content-free
`v2_sandbox_usage_events` row (`user_id`, provider, purpose, timestamp). This is
the billing/usage boundary: cached encrypted text-view reads add no event, while
the first materialization does.

E2B changes the data boundary: decrypted artifact bytes leave Feedling's CVM and
enter E2B. An E2B adapter therefore needs explicit product policy for consent,
egress, retention, billing, credentials, and audit. A Feedling-controlled CVM
sandbox can preserve the existing confidential-compute boundary but still needs
resource/egress limits and a broker API; the shared runner process is not itself
a hostile-code sandbox.

## Prompt caching seam

`workspace.prompt.render_trusted_prefix_blocks()` returns deterministic,
versioned blocks in this order:

1. canonical-path-sorted `/skills/*` documents;
2. `/memory/WORKING.md`.

Dynamic `/workspace` and `/artifacts` data is excluded. Provider adapters can
therefore cache stable skills and working memory separately, invalidating only a
block whose exact revision/content digest changed.
