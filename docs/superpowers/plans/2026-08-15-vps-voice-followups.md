# VPS Voice Follow-ups

## P0 - Cancel the previous model task on barge-in

- [x] When a new user turn starts in the same voice call, stop the previous
  model task even when the new turn has a different logical turn ID.
- [x] Keep the newest active voice turn as the source of truth for each call.
- [x] Cancel CLI subprocesses directly; close HTTP streams and call the
  configured runtime cancellation endpoint when available.
- [x] Never speak or persist output that arrives after cancellation.
- [x] Make cancellation idempotent for repeated interruption and hang-up events.
- [x] Add CLI, streaming HTTP, late-delta, and rapid double-interruption tests.

Acceptance: after the user interrupts, the old audio stops immediately, the old
model task stops promptly, and only the new turn can be spoken or saved.

## P0 - Reuse model sessions across voice turns

- [x] Reuse each runtime's native session when it supports one.
- [x] Add capability-detected `codex exec resume` without breaking older Codex.
- [x] Retry one missing upstream session as a fresh turn, then persist the new id.
- [x] Clear an interrupted in-flight session and rebuild from canonical history.
- [x] Keep feature flags and the stateless transcript path as rollback.

Acceptance: the second turn resumes the first model session without resending the
full recent transcript, while stale or interrupted sessions recover once.

## P0 - Bound Enclave stalls

- [x] Separate connect/TLS timeout from the successful response-read budget.
- [x] Keep existing transient retry behavior and make the worst case observable.
- [x] Add regression coverage for the independent timeout values.

Acceptance: a broken TLS handshake gives up in 5 seconds per attempt instead of
using the full 20-second decrypt-read budget.

## P0 - Prevent duplicate speech at stream completion

- [x] Retry the idempotent final stream marker three times.
- [x] Compare streamed and canonical final text without whitespace/punctuation.
- [x] Never replay a rewritten final answer that cannot retract spoken audio.

Acceptance: formatting differences or a lost completion marker cannot cause the
same paragraph to be spoken twice; the final chat record remains canonical.

## P0 - Reduce first-turn prompt and model round trips

- [x] Keep a compact live capability catalog for discovery.
- [x] Call cataloged verbs directly; use `--help` only after missing detail or a
  parameter error, with at most one correction.
- [x] Use the resident's exact Python interpreter path instead of assuming a
  `python` command exists.
- [x] Inject the catalog once per reusable session, with stateless fallback.
- [x] Bound the cold-session history bridge to eight meaningful messages.
- [x] Keep voice-call archive cards out of the automatic bridge; retain the
  canonical archives for `voice-transcript-list` / `voice-transcript-read`.
- [x] Make foreground screen and World Book context model-invoked by default,
  while keeping explicit eager modes as rollback.
- [x] Leave semantic tool choice to the model; do not add keyword routing.

Acceptance: an ordinary first voice turn has no screen/World Book prefetch, no
mandatory help round, and a small continuity bridge; relevant canonical context
remains available through model-visible tools.

## API Key voice parity

- [x] Record that Runtime V2 already gives API Key models native tool schemas,
  so it has no VPS-style `python` lookup or mandatory `--help` round.
- [x] Record that Runtime V2 already keeps screen content pull-only and exposes
  native `screen_recent` / `screen_read` plus `history_search` / `history_fetch`.
- [ ] Replace Runtime V2's eager foreground World Book read with a model-visible
  read capability, while preserving proactive/always-on setting consistency.
- [ ] Run the same first-audio, first-delta, model-call-count, and input-token
  acceptance matrix for API Key voice and VPS voice before calling parity done.

## Remaining latency and reliability TODO

- [x] Add a minimal voice-runtime profile for every VPS adapter so irrelevant
  optional catalogs are not injected; preserve configured user skills/settings.
- [x] Measure and cap reusable-session growth by a documented token budget, then
  rotate with a compact continuity handoff instead of silently accumulating.
- [ ] Finish the shared native delta-streaming acceptance matrix. The common VPS
  callback and Codex App Server delta path are implemented; API Key providers and
  every supported VPS adapter still need measured first-delta/first-audio proof.
- [ ] Cache stable World Book data for proactive/always-on paths without making
  ordinary foreground chat eager again; preserve invalidation and canonical data.
- [ ] Add per-turn TTS/PCM observability and a bounded recovery path for the case
  where subtitles arrive but no audible audio is produced.
