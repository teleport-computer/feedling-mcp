# T138 block 0: the seven-day trace-rate ruler

The old trace ring cannot measure a true seven-day rate: it retains at most 48
hours and, for active users, the 1,000/2,500-event cap truncates it much sooner.
Block 0 therefore is not a query over existing blobs. It installs a persistent
daily counter that survives process restarts, then observes at least 168 hours
before capacity planning uses the result.

This block measures the existing write path; it does not replace or reroute
trace storage. That belongs to block A.

## Dimensions and retry identity

The counter is keyed by Beijing calendar day, process-start `writer_id`,
`subsystem`, `event_type`, and `lane`. Missing lanes are the explicit
`unknown` bucket; they are not inferred from route or actor. `route` is omitted
because it is absent on many current events and would turn missing data into a
misleading distribution.

Each process writes monotonic absolute totals. The database applies
`GREATEST(existing, submitted)` for every total. A retry after an ambiguous
database commit is therefore idempotent, while a restart gets a new writer ID
whose rows are added by the report. This is the mechanism that makes the ruler
persistent across restarts without one synchronous database write per event.

## Three precision classes

The table never collapses these classes into one asserted total:

- `persisted_*`: the old ring append returned successfully; this is the
  confirmed lower bound.
- `known_drop_*`: only `queue.Full`; the event and its byte size are known not
  to have entered the queue.
- `at_risk_*`: a ring flush raised; the involved count and bytes are known, but
  commit ambiguity means the storage outcome is unknown.

`*_bytes` is deterministic compact UTF-8 JSON payload size. It is not a claim
about PostgreSQL heap, index, WAL, or backup bytes; the later capacity model
must apply measured storage overhead.

An abnormal process exit can lose both queued events and their unflushed
counters. No counter inside that same process can measure its own death. The
report states this limitation explicitly and detects its boundary from a
separate per-writer success heartbeat. Active writers refresh
`last_success_at` every 60 seconds even when idle. A graceful exit writes
`stopped_at`; a crash or persistent database failure cannot refresh the row and
therefore becomes `stale` after three missed heartbeats without needing the
failing path to publish "I failed".

The health row also carries cumulative failures, the largest recovered failure
streak, the most recent failure time, and a durable drain acknowledgement.
A successful production flush writes `dirty_rows=0`; non-zero in-memory backlog
during a database failure is visible only in the writer's local warning and
`trace_stats_health()`, because the failing channel cannot durably report it.
Once the success heartbeat is stale, the report therefore returns
`dirty_rows=null` / `unknown_stale` instead of presenting the last stored zero
as current evidence.

## Capacity decision rule

Run `scripts/trace-write-rate-report.py --days 7` against test. `--days` changes
only the displayed detail window; it cannot weaken the readiness gate. The
report marks itself ready only after at least 168 hours since its first
persisted sample **and** only when every writer overlapping the seven-day
measurement window has a registered, non-stale heartbeat (or a graceful stop).
It returns:

- confirmed daily totals (`persisted_*`);
- a conservative daily observation (`persisted + known_drop + at_risk`), where
  at-risk may overlap persisted because its outcome is unknown;
- the day with the largest conservative byte total, broken down by lane,
  subsystem, and event type.
- every writer's success-heartbeat age, failure history, durable drain
  acknowledgement (or an explicit stale/unknown marker), and graceful-stop
  state.

Capacity planning uses that observed daily peak, never the seven-day average.
The measurement remains a lower bound with respect to any externally observed
abnormal-exit gap and must be annotated with those liveness windows.
