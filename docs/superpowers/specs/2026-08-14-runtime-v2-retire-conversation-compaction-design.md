# Runtime V2 Conversation Compaction Retirement Design

**Date:** 2026-08-14
**Status:** Approved for implementation planning
**Target branch:** `refactor/v2-deterministic-only-coverage`
**Base branch:** `test`

## Objective

Retire Runtime V2 conversation compaction completely. Long-term prompt context
comes only from the Memory Garden-derived `MEMORY` and `USER` profile fields;
short-term continuity comes from a bounded replay of recent complete turns.

This supersedes the intermediate metadata-only coverage design. The current PR
already removed provider-authored conversation folding, but still advances a
summary watermark with deterministic count sentinels and checkpoints. That
remaining maintenance path is no longer part of the desired architecture.

## Product contract

Runtime V2 builds model context from:

- stable system, persona, workspace, tool, and runtime context;
- the latest usable `MEMORY` and `USER` profile fields; and
- bounded recent complete turns.

Chat replays at most 40 complete turns. Wake lanes replay at most 16 complete
turns. Existing model-aware prompt admission may reduce these optional replay
windows further when required context and tools consume the model budget.

Encrypted Chat rows remain the durable archive and the incremental source for
Capture. They are not a promise that every historical message will appear in a
future model prompt. Information that is no longer in the recent-turn window is
available to future turns only if Capture retained it in the Memory Garden and
Profile distilled it into `MEMORY` or `USER`.

The explicit `history_search` / `history_fetch` tools remain a separate,
user-authorized archive access path. They may scan bounded raw encrypted Chat
through the enclave when the model deliberately calls them; they do not inject
history automatically into a turn.

## Data flow

The only long-term semantic path is:

```text
Chat -> Capture job -> Memory Garden -> Profile job -> MEMORY/USER -> Prompt
```

Profile does not read raw Chat directly. Capture reads eligible Chat rows and
selectively writes Memory Garden cards. Profile reads every eligible Garden
card, then atomically writes the two bounded prompt fields.

Recent-turn replay bridges asynchronous lag: information from a newly committed
turn remains directly visible while Capture and Profile converge in the
background.

## Prompt assembly

### Chat

Chat assembles `MEMORY`, `USER`, and no more than the newest 40 complete turns.
The existing complete-turn reader, row-cap protection, attachment injection,
ordered-reply rules, and model-aware prompt frontier remain authoritative.

Chat does not read a conversation summary, summary watermark, coverage
sentinel, or summary frontier. It does not synchronously advance historical
coverage before calling the provider.

### Wake

Wake assembles `MEMORY`, `USER`, and no more than the newest 16 complete turns.
It uses the same bounded-replay contract as Chat and must not require an exact
historical coverage prefix before a scheduled, heartbeat, perception, manual,
screen, or broadcast wake can proceed.

The existing genuine-user-history gate remains. An automatic heartbeat with no
real user history sleeps without calling a provider.

### Omitted history

Omission beyond the recent-turn window is intentional, not a coverage failure.
The prompt does not include a summary sentinel or coverage-hole notice. No code
attempts to prove that the Memory Garden contains every omitted Chat fact.

## Profile availability and failure behavior

Profile follows a last-known-good contract:

- A successful refresh atomically replaces both `MEMORY` and `USER`.
- A refresh failure, timeout, invalid provider response, or transient read
  failure retains and uses the previous valid pair.
- If no valid pair has ever been generated, Chat and wake continue with recent
  turns only.
- An empty Memory Garden produces empty long-term fields and recent turns only.
- A per-turn Profile read failure degrades that turn to recent turns and emits
  content-free telemetry.
- Capture, Dream, and Profile failures never block Chat or wake.

Existing Profile retry and backoff behavior remains. This change does not make
Profile generation synchronous with a user turn.

## Phase one: runtime retirement in the current PR

The current PR establishes the new prompt contract and removes every active
conversation-compaction path.

### Remove

- maintenance/compaction job enqueue call sites;
- provider-backed and deterministic `_run_compaction` implementations;
- inline prompt coverage catch-up and its retry/deadline wrappers;
- conversation-summary and summary-frontier prompt reads;
- summary watermark, leaf, checkpoint, and frontier writes;
- exact coverage assertions and `prompt_coverage_incomplete` failures;
- count-sentinel rendering and coverage-hole prompt text;
- summary/frontier callbacks from `TurnDeps` and production dependency wiring;
- compaction/frontier modules with no remaining non-compatibility callers;
- summary-leaf hinting, summary watermark cursor state, and legacy coverage-gap
  inference from `history_search`;
- compaction, watermark, checkpoint, frontier-level, coverage-hole, catch-up,
  and summary-CAS runtime metrics.

### Preserve temporarily

- existing PostgreSQL summary/frontier tables, indexes, and stored rows;
- migration history that created those objects; and
- a narrow legacy maintenance-job tombstone for rolling deployment safety.

The tombstone may inspect only the job identity and ownership required to mark
an already-enqueued maintenance job `completed` with a retired status. It must
not read or decrypt Chat, read or write summary/frontier state, mint an enclave
token, resolve a provider, or enqueue a successor. No new maintenance job may be
created by the new release.

This compatibility path exists because old workers can enqueue maintenance jobs
during a mixed-version rollout after a one-time database cleanup. New workers
must harmlessly drain those jobs until all old workers have exited.

### Raw-only history search

`history_search` keeps its existing explicit tool contract, authentication,
runtime generation binding, cursor HMAC, row/byte/deadline budgets, enclave
decryption, pagination, and attachment-caption rules. Its scan shape becomes a
single recent-to-old raw Chat scan bounded by the frozen `snapshot_through_seq`.

It no longer reads summary/frontier state, decrypts summary leaves, prioritizes
leaf-hit ranges, stores a summary watermark in new cursors, or reports a
summary-derived `coverage_gap`. Exhausting the raw scan budget remains an
ordinary incomplete page with a continuation cursor; an empty complete raw scan
means no matching retained Chat row was found.

Existing cursors minted by the summary-aware implementation may be rejected as
`cursor_invalid` after deployment. They are short-lived and callers already
restart from the first page on that stable error.

## Phase two: schema and compatibility deletion

A separate follow-up PR runs after at least one stable release proves that:

- no new maintenance jobs are created;
- no summary/frontier rows are read or written; and
- no rollback to a compaction-capable release is required.

That PR:

- removes the maintenance tombstone and legacy job kind;
- deletes remaining summary/frontier database helpers and metrics;
- adds the migration that drops the retired tables and indexes; and
- deletes obsolete compatibility tests and documentation.

Phase one must not drop or rewrite historical summary/frontier data.

## Observability

All new observations are content-free. Retain or add:

- selected Profile state: `ok`, `last_good`, `empty`, or `unavailable`;
- `MEMORY` and `USER` character counts and generation age;
- effective complete-turn replay count for Chat and wake;
- row-cap or model-budget truncation of the recent-turn source;
- Capture and Profile freshness lag; and
- the count of legacy maintenance jobs retired by the tombstone.

Remove or stop emitting observations whose subject no longer exists, including
compaction batches, summary watermarks, frontier/checkpoint levels, coverage
holes, catch-up retries, and summary CAS loss.

## Security and privacy properties

The change reduces historical plaintext movement:

- no conversation-maintenance path decrypts old Chat rows;
- no conversation history is sent to a provider for compaction;
- Profile continues to send rendered Memory Garden cards to the user's
  configured provider under its existing disclosure contract; and
- recent Chat plaintext is still decrypted and sent for the active Chat or wake
  turn according to the existing bounded replay policy; and
- explicit `history_search` / `history_fetch` calls continue to decrypt bounded
  raw Chat candidates inside the enclave under their existing authorization and
  budget contract.

The raw encrypted Chat archive and Capture authorization/fencing behavior are
unchanged.

## Testing and CI gates

### Prompt behavior

- Long-history Chat uses `MEMORY`/`USER` plus no more than 40 complete turns.
- Long-history wake uses `MEMORY`/`USER` plus no more than 16 complete turns.
- Model-aware admission may reduce optional old turns without dropping required
  system context, tools, or the active user input.
- Neither path renders a conversation summary, count sentinel, or coverage-hole
  notice.

### Failure behavior

- Missing, empty, pending, degraded, failed, or unreadable Profile state does
  not block Chat or wake.
- A failed refresh preserves a valid last-known-good profile.
- Capture lag leaves recent information available through bounded replay.
- No long-history scenario fails with `prompt_coverage_incomplete`.

### Retirement behavior

- No runtime path enqueues a new maintenance job.
- A legacy maintenance job is completed as retired without Chat decryption,
  provider resolution, enclave token minting, summary/frontier writes, or a
  successor job.
- Raw encrypted Chat row count and payloads are unchanged by prompt assembly and
  tombstone execution.
- Static CI guards reject reintroduction of compaction, summary/frontier prompt
  reads, coverage catch-up, and their deployment switches.

### History tools

- `history_search` scans raw Chat newest-to-oldest without summary/frontier
  reads or leaf-hint enclave calls.
- Pagination remains deterministic against one frozen raw-Chat snapshot.
- Row, byte, deadline, result, and per-turn lease budgets remain enforced.
- `history_fetch` behavior and neighbor ordering remain unchanged.
- A pre-deployment summary-aware cursor fails with the existing stable
  `cursor_invalid` contract and can be restarted from page one.

### Regression scope

Run focused Chat, wake, Profile, recent-turn, prompt-frontier, job lifecycle,
multi-tenant, and deploy/config tests, then the full PostgreSQL-backed backend
suite and public documentation checks required by the repository.

## Documentation

Update the public architecture, Chat workflow, Memory workflow, self-hosting
guide, and changelog in the same PR. Use one consistent statement:

> Runtime V2 gets long-term context from Memory Garden-derived MEMORY/USER and
> short-term continuity from bounded complete-turn replay. Encrypted Chat is the
> durable archive and Capture source; it is not exhaustively replayed into future
> prompts.

Internal Runtime V2 flow and parity documents must remove summary/frontier and
conversation-maintenance claims while keeping Profile's disclosure and failure
semantics explicit.

## Non-goals

- Changing Capture selection semantics or making Capture lossless.
- Making Profile synchronous with Chat or wake.
- Changing the 40-turn Chat or 16-turn wake defaults.
- Deleting raw encrypted Chat history.
- Removing `history_search` or `history_fetch`; they become raw-archive-only.
- Dropping summary/frontier schema in phase one.
- Changing Dream, memory search, attachment replay, tool admission, or provider
  routing behavior beyond removing conversation-summary dependencies.

## Acceptance criteria

Phase one is complete when Runtime V2 has no active conversation compact,
summary, frontier, watermark, checkpoint, or exact-coverage path; Chat and wake
use only the latest usable `MEMORY`/`USER` plus bounded recent complete turns;
Profile unavailability never blocks a turn; legacy maintenance jobs are safely
retired; all required regression and documentation checks pass; and the retired
database objects remain untouched for rollback.
