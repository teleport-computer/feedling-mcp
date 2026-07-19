#!/usr/bin/env bash
set -euo pipefail

ids_file="${1:-deploy/prod-runner-cvm-ids.txt}"
main_id_file="${2:-deploy/prod-cvm-id.txt}"

if [ ! -f "$ids_file" ]; then
  echo "::error::$ids_file is missing"
  exit 1
fi

if [ ! -f "$main_id_file" ]; then
  echo "::error::$main_id_file is missing"
  exit 1
fi

main_id=$(tr -d '[:space:]' < "$main_id_file")
if [ -z "$main_id" ]; then
  echo "::error::$main_id_file is empty"
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

if grep -vE '^[[:space:]]*(#|$)' "$ids_file" \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
  | grep -Fxq "$main_id"; then
  echo "::error::production main CVM $main_id must never appear in the runner inventory"
  echo "::error::deploying runner-only compose to the main CVM would destroy the API/enclave release unit"
  exit 1
fi

echo "production Runtime V2 topology gate passed: $count independent worker CVM IDs"
