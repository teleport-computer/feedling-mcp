#!/usr/bin/env bash
set -euo pipefail

ids_file="${1:-deploy/prod-runner-cvm-ids.txt}"

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
  echo "::error::production has $count Runtime V2 worker CVM(s); at least 2 independent failure domains are required"
  echo "::error::hosted Chat has no resident fallback; provision and validate another worker before updating any CVM"
  exit 1
fi

echo "production Runtime V2 topology gate passed: $count independent worker CVM IDs"
