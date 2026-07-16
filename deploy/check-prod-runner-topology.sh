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
  echo "::error::production has $count standalone runner CVM(s); at least 2 are required before any CVM is updated"
  echo "::error::provision and validate a second independent runner, then add its ID to $ids_file"
  exit 1
fi

echo "production runner topology gate passed: $count independent CVM IDs"
