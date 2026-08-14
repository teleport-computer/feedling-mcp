# Runtime V2 Slot Restart Race Fix Design

## Problem

Runtime V2 can spawn two replacement children for one slot when cancellation
routing and periodic exact-claim reconciliation observe the same stale slot
snapshot concurrently. Both paths currently split compare/kill from start, so
the second `start()` overwrites the supervisor's process handle and leaves the
first replacement alive but untracked.

## Design

Make one `ChildSupervisor` own a serialized lifecycle transaction for each
slot. Add a lifecycle lock and keep process creation, fenced termination, pipe
cleanup, and replacement creation inside that transaction.

Expose `restart_if_snapshot(expected)`: while holding the lifecycle lock it
compares the full immutable slot snapshot, kills that exact generation, cleans
up its pipe and reader, then starts exactly one replacement. A concurrent call
with the same old snapshot must return `False` after the first replacement has
changed the generation.

Route cancellation fallback and fleet reconciliation through this same
operation. Keep unconditional watchdog recovery serialized through the same
lifecycle lock so it cannot overlap a fenced replacement and create an orphan.

## Safety and acceptance

- Preserve exact job/generation fencing; never kill a newer slot generation
  because of an older cancellation event.
- Preserve broker generation cleanup and reader/pipe cleanup.
- Treat `stop()` as a lifecycle fence: invalidate its snapshot/generation so
  callbacks queued before unwatch cannot resurrect a stopped slot.
- Never discard the tracked process or start a replacement until bounded join
  confirms that the killed process is no longer alive.
- Expose confirmed termination to the watchdog explicitly. DB claim recovery
  must not run while the original child may still execute, and enclave broker
  permits remain reserved until that child is confirmed dead.
- Add a deterministic concurrent regression test proving that two restart
  attempts for one snapshot yield one successful restart and one live child.
- Run supervisor, pool, cancellation, reconciliation, and watchdog tests.
- Deploy only to `test`, reproduce Profile preemption/account teardown, and
  verify the configured four slots correspond to exactly four child processes
  with no repeated `progress pipe closed` errors.
