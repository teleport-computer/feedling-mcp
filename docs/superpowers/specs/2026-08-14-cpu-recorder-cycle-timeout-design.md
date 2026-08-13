# CPU Recorder Cycle Timeout Fix

## Problem

The test CVM runs seven containers. Docker's non-streaming stats endpoint takes
roughly one to two seconds per container, while the recorder currently gives
the entire sequential Docker sampling cycle only ten seconds. The cycle times
out before all containers are sampled, so no baseline completes and no CPU CSV
is written.

## Design

Keep the existing ten-second timeout as the upper bound for any individual
Docker request, but give the complete Docker sampling cycle a fixed thirty-
second budget. Configure that budget explicitly in the managed test and
production Compose files so the measured deployment configuration remains
deterministic.

The recorder remains sequential, private, read-only, content-free, and capped
at its existing CPU and memory limits. Business services do not depend on it,
and no alerting or public endpoint is added.

## Verification

Add a regression test that models seven containers whose stats reads each take
two seconds and proves that a cycle can complete under the thirty-second
budget. Preserve tests proving that individual requests remain bounded and
that an exhausted cycle fails without writing partial data.

After deployment to `test`, verify that:

- `cpu-recorder` no longer emits per-cycle `TimeoutError` messages;
- a daily CSV exists and receives fresh rows at one-minute intervals;
- each completed timestamp includes all running containers;
- business health remains HTTP 200 and containers show no restart or OOM.
