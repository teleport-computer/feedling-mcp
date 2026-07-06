# Proactive Perception Runtime V2 Migration

This branch starts the V2 runtime spine without switching production traffic.

## Canonical Design

The source of truth for Round 3 is
`docs/PROACTIVE_PERCEPTION_SPEC_V2.md`.

The PR-by-PR execution and audit plan is
`docs/PROACTIVE_PERCEPTION_ROUND3_EXECUTION_PLAN.md`.

Older proactive docs are historical context only:

- `PROACTIVE_V2_ARCHITECTURE.md`（已删，见 git 历史）described the previous
  Proactive V2 architecture and contained superseded
  `user_state/ai_state/proactive_jobs` concepts.
- `PROACTIVE_GATE_V1.md`（已删，见 git 历史）was the archived platform-gate design.

## Contract

- The new center is
  `WakeEventV2 -> WakeInboxV2 -> MergedWakeContextV2 -> TurnRunnerV2`.
- Legacy `proactive_jobs` are compatibility inputs only. They should be adapted
  into `WakeEventV2` and must not remain the strategic runtime surface.
- Per-user single-flight and wake merging are correctness primitives, not
  "less proactive" gates.
- Turn leases and background-worker leases must be reclaimable after owner
  crash. A busy turn without lease recovery is a correctness bug.
- Perception does not directly create turns. Raw/coarse signals first pass
  through `PerceptionDifferV2`, which owns delta, discrete wake selection,
  presence hints, connectivity anchors, and screen pHash changes.
- Background slow-path results must never write chat directly. They return as
  `background_result` wakes and re-enter the same inbox/merge/turn arbitration.
- Every hosted/resident turn should see the same tool catalog and `cost_class`.
- `enabled/dnd/user_state/ai_state` are legacy settings/state names. New code
  should model Ambient, Scheduled, and Delivery controls explicitly.
- `scheduled_wake` must not get a hard-coded product priority over other
  same-episode event wakes until reviewed episodes prove the merge policy.

## First Milestone

The first milestone is intentionally small:

1. Define the runtime dataclasses and in-memory inbox.
2. Define the v2 tool catalog.
3. Define the Perception Differ skeleton.
4. Define adapters from legacy jobs into `WakeEventV2`.
5. Add contract tests so later work cannot accidentally extend the old shape.

Production routes should be wired to this spine only after the contract tests
cover merge semantics, settings semantics, and hosted/resident parity.

## Round 3 Execution Order

Use a strangler-fig migration: the V2 spine is the new trunk; old code is only
an input adapter or output compatibility layer.

1. Build `TurnRunnerV2`: foreground single-flight, reclaimable turn lease, and
   reclaimable background-worker lease. Keep `run_agent` injectable; do not
   connect production LLMs yet.
2. Build background slow path: foreground turn releases its turn lease while
   background work runs; completion submits a `background_result` wake. No
   background worker may write chat directly.
3. Build V2 controls: Ambient, Scheduled, and Delivery. Do not mix these with
   legacy `enabled/dnd/user_state/ai_state` semantics.
4. Strangle hosted wake first: legacy hosted wake input adapts into
   `WakeEventV2`, `TurnRunnerV2` executes it, and chat/push delivery remains
   the output compatibility layer. Ship the live cutover behind a per-user flag
   with the old executor retained as a dormant fallback; delete that executor
   only in a follow-up PR after the observation window is healthy.
5. Route perception report/photo/device-event inputs through
   `PerceptionDifferV2` before any wake is emitted.
6. Strangle resident wake second, using the same V2 catalog, differ semantics,
   and lease contract as hosted.
7. Move dashboard/status to V2 turn storage. Old `proactive_jobs` status may
   temporarily receive a projection for the old dashboard, but V2 lease and
   turn-state semantics must not be shaped around that legacy table. Delete the
   projection after the dashboard cutover and healthy observation window.
