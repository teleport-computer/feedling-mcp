#!/usr/bin/env bash
set -euo pipefail

# Canonical production runner inventory: comments/blanks do not represent CVMs,
# surrounding whitespace is insignificant, and repeated IDs are one failure
# domain. Main-CVM expected-count derivation and runner-CVM deployment iteration
# both consume this output so they cannot disagree about fleet capacity.
ids_file="${1:-deploy/prod-runner-cvm-ids.txt}"

if [ ! -f "$ids_file" ]; then
  echo "::error::$ids_file is missing" >&2
  exit 1
fi

grep -vE '^[[:space:]]*(#|$)' "$ids_file" \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
  | grep . \
  | sort -u \
  || true
