# Runtime V2 Completion Design

Date: 2026-07-18
Branch: `codex/runtime-v2-completion`
Base: `origin/pre` at `9721b226`

## Objective

Finish the hosted Runtime V2 migration as one reversible integration batch:

1. Hosted traffic and hosted background lanes have no resident-process runtime dependency.
2. Encrypted raw chat and attachment history is durable source data and is never deleted by prompt compaction or a row cap.
3. Capture and Dream persist real outputs and fail visibly when persistence fails.
4. The native V2 loop gains a pluggable encrypted workspace, lazy sandbox acquisition, editable working memory, conflict-aware parallel file operations, and bounded subagents.
5. Stable system/tool/skills/working-memory prefixes remain cacheable, including an Amazon Bedrock Converse adapter with cache checkpoints and normalized cache telemetry.

## Scope challenge

This touches more than eight files and introduces more than two interfaces, but the scope is split into independently testable commits rather than one cross-cutting rewrite. The existing V2 job loop, outbox, provider types, capability registry, encryption envelope, and deployment topology remain authoritative. No LangGraph or DeepAgents runtime is introduced.

## What already exists

- V2 worker heartbeat, deadlines, child-process wedge recovery, admission ceiling, and terminal error surfacing.
- A unified provider-native loop with stable tool transcripts and late-message folding.
- Parallel read dispatch and serial platform/MCP mutations.
- Encrypted chat, conversation-summary, memory-card, and effect envelopes.
- Anthropic/OpenRouter cache breakpoints, OpenAI cache affinity, and provider-neutral cache telemetry.
- Memory Garden tools and Capture/Dream parsers, though the automatic persistence adapters are currently mismatched.
- A server-side file text extractor, but no model-visible workspace or strong file-processing sandbox.

## Architecture

```text
                                       +-----------------------------+
 iOS / wake / schedule --------------> | durable V2 job + deadline   |
                                       +--------------+--------------+
                                                      |
                                                      v
 +------------------------+        +------------------+------------------+
 | encrypted raw ledger   |------->| native V2 agent loop                |
 | never compaction-GCed  | decrypt| stable prefix + summary + tail      |
 +------------------------+        +-----------+-------------------------+
                                                |
                     +--------------------------+-------------------------+
                     |                          |                         |
                     v                          v                         v
          +----------+---------+     +----------+---------+    +----------+---------+
          | platform/MCP tools |     | WorkspaceBackend  |    | bounded subagents  |
          | reads parallel     |     | encrypted VFS      |    | isolated overlays  |
          | effects ordered    |     +----------+---------+    +----------+---------+
          +--------------------+                |                         |
                                                v                         |
                                     +----------+---------+               |
                                     | lazy SandboxProvider|<-------------+
                                     | CVM/E2B pluggable   |
                                     +---------------------+
```

### Durable history invariant

`chat_messages` and referenced encrypted bodies are immutable source records except for explicit user/account deletion. `v2_conversation_summary.watermark_seq` is only a prompt-view boundary. Hot in-memory caches and UI reads remain bounded through recent-page queries and pagination, never destructive retention.

### Workspace namespaces

```text
/artifacts/          immutable user artifacts; materialization requires sandbox
/workspace/          editable task files
/memory/WORKING.md   encrypted, editable operational memory; not Memory Garden
/skills/             versioned, trusted, read-only instructions
```

The VFS protocol is backend-neutral. A DB/object-store backend can serve safe text operations without acquiring a VM. Physical artifact materialization, untrusted binary parsing, shell/code execution, and OS-dependent transforms acquire a sandbox lazily. The worker never receives a raw Docker socket.

### Tool scheduling contract

Every tool declares a conflict class/key:

- reads and independent subagents: bounded parallel;
- workspace writes to disjoint paths/revisions: parallel;
- same path, ancestor/descendant paths, or same external resource: ordered/conflict checked;
- platform effects, schedules, Memory Garden mutations, and mutating MCP calls: provider order, one at a time;
- results are always reconstructed in provider order.

Workspace writes use revision preconditions/CAS. Subagents receive a workspace snapshot or overlay and must merge through the same conflict checks.

### Subagent contract

`task` is a native V2 tool, not a second runtime. A child gets the same provider route, a bounded tool/call/token budget, an isolated transcript, and a restricted workspace overlay. It cannot send user replies or directly perform irreversible platform/MCP mutations. Multiple independent task calls may run concurrently. The parent receives only bounded final results and explicit workspace merge outcomes.

### Prompt and cache layout

Provider requests preserve deterministic ordering:

```text
tools -> runtime system policy -> skills -> identity/working memory
      -> summary/checkpoints -> verbatim tail -> live runtime/tool data
```

The stable layers are versioned and serialized deterministically. Dynamic summary, tail, perception, and tool results stay after stable cache boundaries. Memory Garden remains retrieval-based; it is not dumped into the prefix. Bedrock Converse uses provider-native `cachePoint` blocks and normalizes `cacheReadInputTokens` / `cacheWriteInputTokens`.

### Resident retirement invariant

For hosted/pre/prod:

- runtime selection is V2-only;
- no supervisor starts per-user resident consumers;
- no scheduled/background lane enqueues resident work;
- no deploy manifest ships a required resident service;
- no admin/account flip can reactivate resident execution;
- health/canary tests fail if a hosted resident process or queue consumer is present.

Self-hosted tooling may remain only where it is explicitly a different product surface and cannot be selected by hosted accounts.

## Test diagram

```text
raw send -> encrypted row -> compaction -> encrypted row still exists
                                      -> prompt uses summary + tail

capture parser -> real action mapper -> validator -> encrypted card persisted
dream parser   -> consolidation map  -> validator -> supersede/merge persisted
write failure  ----------------------------------> job failed + visible error

text-only turn -------------------------------> no sandbox acquired
artifact materialize / binary parse / shell --> sandbox acquired once
workspace disjoint writes --------------------> parallel + both revisions commit
workspace same-path writes -------------------> deterministic conflict/ordering

parent task batch -> child overlays in parallel -> bounded results -> explicit merge

stable prefix round 1 -> cache write
stable prefix round 2 -> cache read
working-memory version change -> earlier stable prefix still reusable

hosted boot -> V2 worker only -> no resident process/queue/admin escape hatch
```

## Failure modes and required coverage

| Code path | Production failure | Required behavior/test |
|---|---|---|
| Raw history | summary watermark advances after a lossy bullet | original encrypted rows and bodies remain present |
| History pagination | one user has millions of rows | bounded page/recent query; no full-list load in V2 hot path |
| Capture | action metadata missing | fail job visibly; never report completed |
| Dream | parser/action schema drift | real parser-to-validator integration test |
| Sandbox | provider unavailable or times out | explicit tool error; no fallback to host execution |
| Artifact | malicious/invalid binary | contained parser failure; turn remains recoverable |
| Workspace | concurrent same-path edits | CAS conflict, no lost update |
| Subagent | child loops forever or overspends | deadline/call/token limits and cancellation |
| Subagent | child attempts external mutation/reply | schema omission plus dispatch-time denial |
| Bedrock | cache checkpoint unsupported/rejected | bounded compatibility fallback without dropping tools/messages |
| Bedrock | bearer key invalid/expired | provider-config error; no retry hammering |
| Retirement | stale flag/admin route selects resident | fail closed to V2/error; no process launch |

## Rollout

Each concern lands as a separate commit and is integrated on `codex/runtime-v2-completion`. Focused suites run per commit; the integrated branch runs the complete backend suite, docs checks, migration checks, dependency-direction checks, and provider wire/cache tests. Only then is the exact reviewed HEAD pushed to `pre`. Deployment verification checks the V2 heartbeat/capacity endpoints and confirms no resident process or consumer is running.

## NOT in scope

- Replacing the V2 loop with DeepAgents/LangGraph: it would discard Feedling job/effect/encryption invariants.
- Building a general collaborative source-control system: workspace overlays support bounded task merges, not arbitrary Git hosting.
- Automatically sending private artifacts to E2B: E2B remains an explicit provider because it changes the data trust boundary.
- Rewriting Memory Garden as files: Garden remains structured semantic memory; working memory is a separate VFS projection.
- UI redesign: backend contracts and existing iOS-compatible APIs are the scope of this batch.
