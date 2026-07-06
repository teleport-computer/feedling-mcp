# Proactive Perception Round 3 Execution Plan

This document is the implementation and audit plan for landing Round 3. It is
meant to be read by Codex / Claude Code before writing or reviewing code.

## Source Of Truth

- Canonical design: `docs/PROACTIVE_PERCEPTION_SPEC_V2.md`
- Runtime migration contract: `docs/PROACTIVE_PERCEPTION_RUNTIME_V2_MIGRATION.md`
- iOS perception producer contract: sibling repo `../feedling-mcp-ios`,
  especially `PerceptionContextSnapshot`, `PerceptionPermissionsManager`,
  `FeedlingAPI.reportPerceptionSnapshot`, and the photo/broadcast pipelines.
- Historical context only: `PROACTIVE_V2_ARCHITECTURE.md`、`PROACTIVE_GATE_V1.md`
  （两份均已删，见 git 历史）

If this plan conflicts with `PROACTIVE_PERCEPTION_SPEC_V2.md`, the spec wins.
If code already implements an older shape, use the strangler migration below
instead of extending the old shape.

## iOS Integration Discipline

Round 3 is not considered end-to-end integrated until the iOS producer contract
has been checked against the backend V2 ingress. Backend PRs may build the V2
runtime spine first, but they must not claim iOS/backend integration merely
because they read the existing perception store.

Before PR7 cuts over perception ingress, sync `../feedling-mcp-ios` to the
latest upstream `main`, capture the actual `/v1/perception/report` and
`/v1/perception/photo/evaluate` payload shapes as fixtures, and run backend
contract tests from those fixtures through `PerceptionDifferV2`. This is a hard
gate: do not let imagined payloads drive the differ implementation.

## Migration Strategy

Use a strangler-fig migration:

1. V2 spine is the new trunk.
2. Old systems may remain only as input adapters or output compatibility layers.
3. Each production path cutover must be reversible: ship the V2 path behind a
   per-user flag, keep the old executor as a dormant fallback during an
   observation window, and delete the old executor only in a separate follow-up
   PR once §10.3 system metrics confirm the V2 path is healthy. Cutover and
   deletion are two PRs, never one.
4. Legacy `proactive_jobs` status may receive temporary projections for old
   dashboard visibility, but V2 lease / turn state must not be modeled around
   that table.

## Non-Negotiable Invariants

Any PR violating these should be rejected.

1. Legacy `proactive_jobs` shape appears only at adapter/projection boundaries
   and, if preserved, lives under `payload.legacy_*`.
2. Main V2 dataclasses must not grow legacy fields.
3. Single-flight, merge, and lease are correctness primitives, not "less
   proactive" gates.
4. Turn and background leases must be reclaimable after owner crash, with tests.
5. Background slow-path workers must never write chat directly. They must submit
   a `background_result` wake back into the inbox.
6. Perception signals must pass through `PerceptionDifferV2` before becoming
   wake events.
7. Continuous signals produce zero wakes: motion, battery, now playing, time,
   raw GPS/location drift, and plain `place_label`.
8. Gate semantics must not mix:
   - Ambient controls self-initiated wake sources.
   - Scheduled controls agent-owned timers.
   - Delivery controls visible chat/push delivery.
9. Scheduled wake must not be suppressed merely because Ambient is off.
10. Hosted and resident must share the same tool catalog, cost classes, wake
    contract, and differ semantics.
11. Do not add a second judgment model. Cheap VLM/captioning may transcribe; it
    must not decide whether content is worth the companion's attention.
12. `scheduled_wake` must not get a hard-coded product priority over same-episode
    event wakes until reviewed episodes justify that policy.
13. Live cutover must be reversible. Convert a production path behind a per-user
    flag with the old executor retained as a dormant fallback; never delete the
    old executor in the same PR. Deletion is a separate follow-up PR after an
    observation window with healthy §10.3 system metrics. (This is the whole
    point of strangler-fig: same-PR deletion removes the rollback path.)
14. Perception ingress cutover must be proven against real iOS payload fixtures.
    Backend V2 code may not assume field names, envelope shape, `changed`
    semantics, or photo metadata shape without a fixture from
    `../feedling-mcp-ios`.

## PR Sequence

Land this in small PRs. Each PR should include tests, a short migration note,
and explicit legacy cleanup/projection evidence.

### PR1. DB-Backed Runtime Substrate

Scope:

- Add V2 persistent storage for wake inbox, turn state, foreground leases, and
  background leases.
- Replace in-memory lease/inbox in production paths with DB-backed CAS semantics.
- Keep existing in-memory classes as contract-test fakes if useful.

Implementation notes:

- Prefer append/patch primitives consistent with existing `user_logs` patterns,
  but do not force V2 turn state into old `proactive_jobs` fields.
- Model lease ownership, expiry, renewal if needed, and release.
- Use CAS / advisory-lock semantics so multi-worker attempts dedupe.

Acceptance tests:

- Two workers racing for the same user: only one foreground turn lease wins.
- Expired foreground lease is reclaimable.
- Expired background lease is reclaimable.
- Releasing an old/replaced lease fails.
- Wake merge survives persistence round trip.
- Crash simulation: a stale `running` turn can be recovered without duplicate
  chat writes.

Do not do:

- Do not connect hosted or resident production execution yet.
- Do not use old `proactive_jobs.status` as the real lease source.

### PR2. V2 Controls: Ambient / Scheduled / Delivery

Scope:

- Introduce V2 settings resolver and persisted settings shape.
- Map old `enabled/dnd/user_state/ai_state` only for compatibility.
- Put switch state into `MergedWakeContextV2`.

Acceptance tests:

- Ambient off blocks `heartbeat`, `perception_event`, and self-initiated
  `scene_change` when applicable.
- Ambient off does not block `user_message`, `manual`, `scheduled_wake`, or
  `background_result`.
- Scheduled off blocks or reroutes agent-owned timer execution according to the
  spec, without silent loss.
- Delivery off blocks visible chat/push delivery but lets the agent know the
  switch state.
- Manual wake bypasses all user-silencing gates.

Do not do:

- Do not build iOS UI in this PR.
- Do not keep adding behavior to old `dnd` / `away` semantics.

### PR3. Tool Catalog And Executor V2

Scope:

- Turn `ToolCatalogV2` from a static list into the shared hosted/resident
  execution contract.
- Implement first executor layer for available tools.
- Return explicit unavailable errors for blocked HealthKit tools.

Minimum tools:

- `perception.now`
- `perception.location`
- `perception.calendar`
- `perception.now_playing`
- `perception.motion`
- `perception.photo_recent`
- `memory.index`
- `memory.fetch`
- `send_message`
- `sleep`

Unavailable for now:

- `perception.steps`
- `perception.sleep_last_night`
- `perception.workout`
- `perception.vitals`

Acceptance tests:

- Hosted and resident derive from the same catalog source.
- Each tool has a stable `cost_class`.
- Unavailable tools fail explicitly and safely.
- Tool traces record name, cost class, latency, outcome, and wake/turn id.
- Fast/slow budget produces soft handoff to background, not silent truncation.

Do not do:

- Do not implement HealthKit server-side placeholders that pretend data exists.

### PR4. TurnRunnerV2 Agent Protocol

Scope:

- Implement real `run_agent` protocol for V2 turns.
- Parse actions: `send_message`, `sleep`, `schedule_wake`, `cancel_wake`,
  `needs_background`.
- Build context from tools/time/switches/digest/recent chat; do not inject
  perception values directly.

Acceptance tests:

- `user_message` uses the same runner as proactive wake.
- Manual wake returning only sleep is marked as contract violation
  (`ignored_manual` or equivalent).
- Plain background requests do not write chat during foreground turn.
- Perception snapshot values are not passively injected into the prompt.
- Agent actions are persisted as V2 turn/action records.

Do not do:

- Do not cut over hosted/resident yet; test runner with injected model stubs.

### PR5. Background Slow Path

Scope:

- Add background worker lifecycle using V2 background leases.
- Background completion submits `background_result` wake.
- Foreground turn slot must be free while background work runs.

Acceptance tests:

- Background worker cannot call `append_chat` / push directly.
- Background result enters wake inbox and merges with newer user messages.
- Late/stale background result can be dropped or folded by the agent.
- Background lease timeout is recoverable.
- Duplicate completion cannot double-send.

Do not do:

- Do not reuse old hosted proactive daemon threads as the canonical worker.

### PR5b. Observability And Eval Scaffold (must land before any live cutover)

Scope:

- Land the system-health metrics from spec §10.3: wake volume, merge rate,
  double-send rate, missed-`scheduled_wake` rate, latency distribution,
  background append success/stale rate, pHash dedupe rate.
- Land the per-episode (cross-wake) eval harness and the Round 3 review-label
  schema (see PR10) so cutovers are judged on behavior, not only unit tests.
- Seed a small set of replayable synthetic episodes.

Why before PR6/PR9:

- The scariest failures (`stutter`, double-send, `went_dark`, cross-time
  misses) are cross-wake and invisible to per-wake unit tests. A flagged live
  cutover (invariant 13) needs these metrics to define a healthy observation
  window before the old executor is deleted. Eval-first discipline (spec §10):
  do not cut a live path over with green unit tests alone.

Acceptance tests:

- Metrics increment correctly on synthetic wake/turn/background flows.
- A replayable episode runs end to end through the harness and emits labels.

Do not do:

- Do not block landing on having real production episodes; seed synthetic.

### PR6. Hosted Wake Cutover

Scope:

- Convert hosted wake input into `WakeEventV2`.
- Execute through `TurnRunnerV2`.
- Reuse chat write and push only as output compatibility.
- Ship behind a per-user flag; keep the old hosted wake executor as a dormant
  fallback. Do NOT delete it in this PR (invariant 13) — deletion is a separate
  follow-up after the observation window.

Acceptance tests:

- Hosted heartbeat/perception/manual wakes use V2 context (flag on).
- With the flag off, the old executor still runs unchanged (rollback path works).
- Hosted manual wake still requires visible response.
- Old hosted wake prompt/action parser is not used for V2 execution (flag on).
- Existing hosted smoke tests stay green.
- Old `proactive_jobs` receives only projection fields needed by old dashboard.
- Live V2 agent context includes the user's timezone or local-rendered time from
  settings so the hosted cutover cannot ship UTC-blind local-hour reasoning.
- Visible delivery emits from `outcome.messages` only; `send_message` action
  records are audit/reconstruction records and must never be re-delivered.

Cutover evidence:

- Show the per-user flag and the fallback-to-old-executor path.
- List the functions in `hosted/wake_consumer.py` slated for deletion in the
  follow-up deletion PR (after metrics confirm health).

### PR6b. iOS Perception Contract Sync And Fixtures

Scope:

- Sync `../feedling-mcp-ios` to the latest upstream `main` before touching
  perception ingress.
- Audit the actual iOS producers for `/v1/perception/report`,
  `/v1/perception/photo/evaluate`, and broadcast frame state.
- Add backend fixture coverage for the current iOS payloads.
- Map every iOS-emitted field to either a V2 differ input, a pure pull-only
  signal, or an explicit unsupported/ignored result.

Current iOS files to inspect:

- `App/FeedlingTest/Pages/Settings/Perception/PerceptionContextSnapshot.swift`
- `App/FeedlingTest/Pages/Settings/Perception/PerceptionPermissionsManager.swift`
- `App/FeedlingTest/Pages/Settings/Perception/PerceptionLocalConfig.swift`
- `App/FeedlingTest/Pages/Settings/Perception/PerceptionLocalResolver.swift`
- `App/FeedlingTest/API/FeedlingAPI.swift`
- `App/FeedlingBroadcast/SharedConfig.swift`
- `App/FeedlingBroadcast/SampleHandler*.swift`

Acceptance tests:

- Backend contract tests load real-shape iOS fixtures and pass them through
  `/v1/perception/report` parsing and `PerceptionDifferV2`.
- Fixtures cover operation signals (`time`, `battery`, `broadcast`, `focus`)
  and encrypted/sensitive signals (`location_signal`, `motion_state`,
  `calendar_next_event`, `playback`) in the current client shape.
- Fixtures cover `changed=true`, `changed=false`, missing permission,
  unsupported/unavailable, and dropped-upload cases.
- Photo fixture covers the current `/v1/perception/photo/evaluate` metadata and
  envelope shape, including the V2 removal of sensitive-scene hard blocking.
- Unknown or unimplemented signals fail explicitly instead of silently
  generating wakes.
- HealthKit absence remains explicit; do not invent steps/sleep/workout/vitals
  data until iOS implements it.

Do not do:

- Do not build new iOS UI or HealthKit in this PR.
- Do not change iOS reporting semantics inside the backend cutover PR. If iOS
  payloads must change, make that a separate iOS PR/review.
- Do not claim frontend/backend integration is complete until these fixtures
  and at least one manual device/simulator report path have been checked. The
  manual report path is a human-verified step: agents may prepare fixtures and
  tests, but only the user can certify that the iOS app actually hit the
  backend from a device or simulator.

### PR7. Perception Ingress Cutover

Scope:

- Route `/v1/perception/report`, photo ingest, and device events through
  `PerceptionDifferV2`.
- Use the PR6b iOS fixtures as the contract source for payload parsing.
- Implement WiFi/BT anchor transition semantics.
- Implement pHash scene-change wake path for broadcast mode if ready; otherwise
  leave a tested TODO boundary.

Acceptance tests:

- All PR6b iOS fixtures still parse and map to the intended differ inputs.
- Motion/battery/now-playing/time/plain-place-label produce zero wakes.
- WiFi/BT anchor transition produces `perception_event`.
- Repeated same anchor updates `last_seen` without waking.
- Photo added produces the expected discrete event.
- Photo ingest removes the v1 sensitive-scene hard block from spec §2.1; cheap
  layers may transcribe/dedupe but must not gate whether the companion can see
  the photo.
- Broadcast off prevents `scene_change` wake.
- All generated perception wakes include digest and origin refs.

Do not do:

- Do not revive named-place or raw GPS drift as wake sources.
- Do not add a model gate before wake generation.

### PR8. Scheduled Wake

Scope:

- Implement `schedule_wake` and `cancel_wake`.
- Persist timers with timezone, DST-safe scheduling, origin refs, and note.
- Trigger scheduled wakes through the same V2 wake inbox.

Acceptance tests:

- Timer fires once across multiple workers.
- Timer survives process restart.
- Cancel prevents future wake.
- Pending timer cap is enforced and visible to the agent.
- Scheduled wake works when Ambient is off.
- Delivery off is surfaced transparently instead of silently dropping.
- Scheduled-off timer decisions consume PR2's explicit `transparency_required`
  contract or reroute into a dedicated transparency notice; fire-and-forget
  callers must not be able to silently drop due timers.
- Pending timers that become due while Scheduled is off are marked
  `blocked/scheduled_disabled` and rerouted as `background_result`
  transparency wakes with the original note/origin refs. They are not retried
  automatically.
- Pending cap starts at 20. The scheduler exposes `pending_count/pending_cap`
  to the agent and reports any evicted oldest timers in the action result.

Do not do:

- Do not model scheduled wake as a user reminder command only. It is agent-owned
  intent.
- Do not decide the pending-timer-after-Scheduled-off product behavior in code
  without updating the spec/acceptance first.

### PR9. Resident Cutover

Scope:

- Convert resident consumer to the same V2 wake context, catalog, and action
  contract.
- Add resident lease/reclaim semantics so claimed jobs cannot strand forever.
- Ship behind a per-user flag with the old resident executor as fallback. Delete
  `_message_for_proactive_job` and the old resident executor in a follow-up PR
  after the observation window (invariant 13).

Acceptance tests:

- Resident and hosted see equivalent V2 context for the same synthetic wake.
- Resident uses the shared tool catalog.
- Resident crash during claimed/realizing state is recoverable.
- With the flag on, `_message_for_proactive_job` is unreachable for V2 users;
  with the flag off, the old resident path still works (rollback).

Do not do:

- Do not keep separate resident-only wake semantics.

### PR10. Dashboard, Eval, And Legacy Cleanup

Scope:

- Replace Gate dashboard with Wake / Turn / Agent Action / Tool Trace views.
- Move review labels to Round 3 terms.
- Remove old job-status projections after dashboard cutover.
- Remove dead V1/V2 executor paths.

Review labels (must match canonical spec §10.4):

- `good_presence`
- `missed_moment`
- `went_dark` — should have been present and was not. For companion users this
  is a worse sin than over-presence (D10 recall-first). Spec §10.4 requires it.
- `too_much_buzz` — Delivery-layer over-notification; a real bug to fix.
- `too_chatty` — agent chose to say more. **NOT a bad case** (D4: chattiness is
  a legitimate personality). Tracked for visibility, never penalized.
- `wrong_voice`
- `ignored_manual`
- `stutter` — multiple uncoordinated bubbles (catches §1.4 concurrency bugs).
- `late_irrelevant`
- `privacy_bad`

> Do not collapse `too_much_buzz` and `too_chatty` back into a single
> `too_much`. Merging them trains reviewers to penalize chattiness, which D4/D10
> forbid. The split is the point.

Acceptance tests:

- Dashboard reads V2 turn/action/tool records.
- Old Gate labels are not the primary review interface.
- Projection code to old `proactive_jobs` is deleted after cutover.
- A grep audit shows no old executor entrypoint remains reachable.

## Cross-Cutting Audit Checklist

Run this for every PR.

### Legacy Boundary

- Search for `proactive_jobs`, `legacy_`, `user_state`, `ai_state`,
  `set_ai_state`, `dnd`, `away`.
- Confirm each occurrence is one of:
  - adapter from old input,
  - projection for old dashboard,
  - historical doc,
  - compatibility test.
- Reject if a V2 dataclass or core runtime API grows these concepts.

### Concurrency And Lease

- Is per-user foreground turn single-flight enforced?
- Can lease ownership be recovered after TTL?
- Can stale owners release or overwrite new leases? They must not.
- Are background leases independent from foreground turn leases?
- Could multi-worker execution double-send a chat message?

### Background Path

- Search for direct chat writes inside background workers.
- Confirm completion path submits `background_result`.
- Confirm stale background results are visible to agent arbitration.
- Confirm foreground slot is released before slow work runs.

### Perception

- Does every perception-triggered wake go through `PerceptionDifferV2`?
- Are continuous signals guaranteed to produce zero wakes?
- Are WiFi/BT anchors stateful and deduped?
- Is pHash mechanical dedupe separate from any VLM captioning?
- Is any model deciding "worth attention"? If yes, reject.

### iOS Contract

- Is `../feedling-mcp-ios` synced before perception ingress work starts?
- Are backend fixtures based on actual Swift producers, not hand-written
  guesses?
- Do fixture tests cover encrypted envelope and plaintext operation-signal
  shapes separately?
- Are unsupported iOS capabilities, especially HealthKit, represented as
  explicit unavailable states?
- Has the PR avoided claiming iOS/backend integration unless fixture tests and
  a human-verified manual report path have both been checked?

### Gate Semantics

- Ambient, Scheduled, and Delivery must remain separate.
- Ambient off must not suppress scheduled commitments.
- Delivery off must not erase agent reasoning; the agent must see switch state.
- Manual/user-message paths bypass proactive silence gates.

### Tool Parity

- Hosted and resident import the same catalog source.
- Tool names and cost classes match across paths.
- Unavailable tools return explicit errors.
- Tool traces include turn/wake id and outcome.

### Prompt / Context

- Wake context should include tools, time, switches, digest, hints, recent chat.
- Wake context should not inject raw perception values by default.
- Prompts must not tell agent to be "less chatty" as a personality trait.
- Prompt must not mention internal jobs/triggers in user-facing voice.

### Privacy And TEE

- Do not assume external hosted model providers are inside the TEE boundary.
- Photo/screen data crossing provider boundaries must be explicit and reviewed.
- Sensitive data should not be copied into logs or dashboard plaintext.
- Push payloads must stay appropriate for lock-screen visibility.

### Deletion Evidence

For cutover PRs, include a section in the PR description:

- Old executor removed:
- Old route still present only as adapter:
- Old status projection still needed because:
- Planned removal PR:

## Suggested Claude Code Audit Assignments

Use these as independent review tracks.

1. DB / lease / concurrency reviewer:
   - Focus on PR1, PR5, PR8, PR9.
   - Try to prove duplicate turn or duplicate chat write is impossible.
2. Hosted / resident parity reviewer:
   - Focus on PR3, PR6, PR9.
   - Compare context, catalog, actions, and error semantics.
3. Perception reviewer:
   - Focus on PR7.
   - Verify every wake source passes through `PerceptionDifferV2`.
4. Settings / gate reviewer:
   - Focus on PR2 and PR8.
   - Look for Ambient/Scheduled/Delivery mixing.
5. Dashboard / eval / cleanup reviewer:
   - Focus on PR10 and all cutover PRs.
   - Verify old Gate review concepts do not remain primary.

## PR Description Template

```md
## Scope

## Spec Sections

## Runtime Invariants Touched

## Implementation Notes

## Tests

## Legacy Boundary / Deletion Evidence

## Known Follow-Ups
```

## Current Starting Point

The branch currently has:

- `backend/proactive/runtime_v2.py`
- `backend/proactive/tool_catalog_v2.py`
- `backend/proactive/adapters_v2.py`
- `backend/perception/differ_v2.py`
- `tests/test_proactive_runtime_v2.py`

These are contract skeletons. Production routes still use old execution paths
until the PR sequence above cuts them over.

The sibling iOS repo `../feedling-mcp-ios` was synced on 2026-06-19 to
`main`/`origin/main` at `23d1eba` (`Merge pull request #10 from
teleport-computer/encrypt`). The latest iOS changes materially affect the
perception contract: local geofence/SSID labeling, encrypted sensitive signal
upload, tighter cancellation/calendar access, and the perception permissions
view. Treat that repo as an active contract source before PR7.
