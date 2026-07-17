#!/usr/bin/env bash
set -euo pipefail

ids_file="${1:-deploy/prod-runner-cvm-ids.txt}"
enabled="${2:-false}"

if [ "$enabled" != "true" ]; then
  echo "standalone prod runner deployment disabled; topology gate is inactive"
  exit 0
fi

if [ ! -f "$ids_file" ]; then
  echo "::error::$ids_file is missing"
  exit 1
fi

# Blank lines and comments are documentation, not runners. Duplicate IDs are
# one failure domain and therefore count once.
count=$(
  grep -vE '^[[:space:]]*(#|$)' "$ids_file" \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
    | grep . \
    | sort -u \
    | wc -l \
    | tr -d '[:space:]' \
    || true
)
count="${count:-0}"

if [ "$count" -lt 2 ]; then
  # 2026-07-17: downgraded from a hard failure to a warning (owner decision) —
  # with a single provisioned runner the hard gate blocked EVERY prod deploy.
  # The single-runner risk it guards against (2026-07-15 outage: main deploy
  # interrupts ingress, runner deploy removes the only hosting path) is real;
  # re-arm the hard gate by setting PROD_RUNNER_TOPOLOGY_ENFORCE=true once a
  # second independent runner CVM is provisioned and listed in $ids_file.
  if [ "${PROD_RUNNER_TOPOLOGY_ENFORCE:-false}" = "true" ]; then
    echo "::error::production has $count standalone runner CVM(s); at least 2 are required before any CVM is updated"
    echo "::error::provision and validate a second independent runner, then add its ID to $ids_file"
    exit 1
  fi
  echo "::warning::production has only $count standalone runner CVM(s) — deploys briefly drop the only hosting path (2026-07-15 outage mode). Provision a second runner and add it to $ids_file, then set PROD_RUNNER_TOPOLOGY_ENFORCE=true to re-arm the hard gate."
  exit 0
fi

echo "production runner topology gate passed: $count independent CVM IDs"
