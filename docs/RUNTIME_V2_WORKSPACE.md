# Runtime V2 workspace, working memory, and sandbox boundary

Runtime V2 owns a backend-pluggable virtual workspace. Production uses the
encrypted PostgreSQL backend; the in-memory backend is test-only. A
model-visible path is never evidence that the same path exists on the runner
host.

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

The durable sink groups a provider-authored run of workspace mutations into
conflict-free waves. Disjoint paths can commit concurrently at the database CAS
boundary. The same path and ancestor/descendant paths serialize in provider
order, so a directory delete cannot race a nested write. Each child mutation
has a deterministic sink identity and its own claim/complete state, making
retries idempotent without hiding an uncertain sibling outcome. Tool results
are reconstructed in the provider's original call order. Platform effects,
schedules, Memory Garden writes, and mutating MCP calls remain provider-ordered.

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

The E2B adapter is source-wired but configuration-dependent. It requires
`FEEDLING_V2_SANDBOX_PROVIDER=e2b`, `E2B_API_KEY`, and
`FEEDLING_V2_E2B_TEMPLATE`. It always requests `secure=True`, disables internet
access by default, caps lifetime/command/output/artifact size, and kills the
sandbox on close. Its template must provide the fixed
`/opt/feedling/bin/extract-artifact` contract: read bytes and JSON metadata from
the fixed `/tmp/feedling-artifact*` paths and write UTF-8 text to the fixed
output path. User filenames, MIME types, and model content are never appended
to that extractor command. `FEEDLING_V2_E2B_ALLOW_INTERNET=1` is an explicit
deployment-level opt-in, not a model option.

The materialization and extractor defaults both match the iOS upload ceiling of
25 MiB (26,214,400 bytes). The extractor limit is part of the content-addressed
template digest, so changing it requires rebuilding and deploying the new
template tag rather than mutating an existing alias.

The repository's older resident `container` strategy is not a usable adapter,
and the old memory-sandbox compose is a local backend+enclave validation stack,
not an artifact execution API. When the sandbox provider is disabled,
misconfigured, or unavailable, uploads remain durably stored but a Runtime V2
binary/text attachment cache miss is surfaced as `sandbox_unavailable`; it is
never parsed in the backend or shared worker process.

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

The current model-facing catalog exposes artifact/workspace operations but not a
generic shell or arbitrary code-execution tool. The shell/code bullets above
define the mandatory sandbox boundary if those tools are added later; they do
not claim that arbitrary execution is already available.

The sandbox lifecycle itself should not be a generic model-managed tool. Future
model-visible capabilities should be named for the operation (`run_code`,
`shell`, `transform_image`, or artifact extraction). The runtime broker acquires
one `SandboxProvider` session on the first risky capability call, reuses it only
within that bounded turn/task, and closes it at the deadline. It materializes
only explicitly referenced encrypted artifacts, exposes no host mounts or
ambient secrets, defaults network egress off, and re-ingests outputs into the
encrypted VFS. Usage/billing follows the provider-session lifetime rather than
charging text-only turns.

## Bounded subagents

`task` is a native loop tool. Each child uses the same provider route with an
isolated transcript, a deadline, and bounded provider/tool-call budgets.
Children can inspect approved workspace/artifact/Memory/web reads, but cannot
reply to the user, recurse into more tasks, load mutating MCP tools, or perform
platform/workspace mutations. Multiple independent task calls can run in
parallel, and the parent receives bounded results in provider order.

The budget is shared across every task batch in one parent turn, not reset per
child: by default at most 12 child provider calls and 131,072 reported tokens.
Each concurrent provider call reserves a 32,768-token context ceiling before
I/O; reported usage refunds unused reservation, while missing usage telemetry
consumes the full reservation. The deployment knobs are
`FEEDLING_V2_SUBAGENT_MAX_TOTAL_LLM_CALLS`,
`FEEDLING_V2_SUBAGENT_MAX_TOTAL_TOKENS`, and
`FEEDLING_V2_SUBAGENT_MAX_TOKENS_PER_CALL`. Exhaustion becomes the bounded tool
result `subagent_budget_exhausted`, never an unbounded retry.

The same outbound-data boundary covers non-workspace private text. Eager
perception contains fixed numeric/boolean/null readings only, and screen-watch
eagerly receives frame counts rather than captions. Calendar/reminder/app/place
text and screen/photo content require an explicit read; after such a read,
later web, MCP, and `task` tools are unavailable for the remainder of the turn.
Numeric health snapshot/trend reads do not trigger that restriction.

## Prompt caching seam

Production eagerly renders only deterministic, versioned,
canonical-path-sorted `/skills/*` documents. Dynamic `/workspace`,
`/artifacts`, and editable `/memory/WORKING.md` data is excluded from the base
prompt. Skills remain trusted system instructions, so the tool/system/skills
prefix is both stable and safe for provider caching.

`WORKING.md` is persistent agent-authored state and therefore an untrusted
prompt-injection surface. It stays encrypted at rest and is pull-only through
`workspace_read`; it is not silently injected into later turns and is not
claimed as part of the stable cache prefix. After a turn explicitly reads
private workspace or working-memory content, the loop removes outbound web,
MCP, and `task` tools for later rounds of that turn.

Unit and wire tests cover OpenAI-compatible cache affinity,
Anthropic/OpenRouter cache controls, Gemini cache telemetry, and Bedrock Converse
`cachePoint` blocks. The deployed Pre canary currently proves only an
OpenRouter route-bound cache read over a stable synthetic conversation prefix;
it does not yet prove native Bedrock or a deployed `/skills` prefix mutation.
