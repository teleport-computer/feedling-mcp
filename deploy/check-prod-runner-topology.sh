#!/usr/bin/env bash
set -euo pipefail

# Dual-runtime coexistence (2026-07-21 design): the prod runner CVM(s) are back
# to V1 agent-runner form (deploy/docker-compose.phala.prod.runner.yaml, see
# Task 11). Pooled Runtime V2 now lives on the main CVM itself as the
# serve-worker container, so "hosted Chat has no resident fallback" no longer
# holds — V1 resident IS the fallback again. This script's ≥2-CVM check
# therefore defaults to a warning (not a hard block): with only one runner CVM
# currently provisioned (deploy/prod-runner-cvm-ids.txt), a hard block would
# stop every future prod main-CVM deploy forever. Re-arm it per-run with
# PROD_RUNNER_TOPOLOGY_ENFORCE=true, or disable the whole standalone-runner
# preflight with the 3rd arg ("enabled", default true — CI passes
# vars.DEPLOY_PROD_RUNNER_CVM). Note this default polarity and the disable
# switch are NEW as of Task 11, not a restoration of prior behavior — origin/
# test's script never had a disable arg or a main-CVM-membership check.
#
# The "main CVM must never appear in the runner inventory" check ALWAYS runs,
# even when the ≥2-CVM preflight is disabled — it is an unconditional footgun
# guard, not part of the "is redundancy provisioned" question the disable
# switch answers.

ids_file="${1:-deploy/prod-runner-cvm-ids.txt}"
main_id_file="${2:-deploy/prod-cvm-id.txt}"
enabled="${3:-true}"

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

# Unconditional, regardless of $enabled: deploying the runner-only compose to
# the main CVM would destroy the API/enclave release unit no matter whether
# the redundancy preflight below is active.
if grep -vE '^[[:space:]]*(#|$)' "$ids_file" \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
  | grep -Fxq "$main_id"; then
  echo "::error::production main CVM $main_id must never appear in the runner inventory"
  echo "::error::deploying runner-only compose to the main CVM would destroy the API/enclave release unit"
  exit 1
fi

if [ "$enabled" != "true" ]; then
  echo "standalone prod runner deployment disabled; redundancy gate is inactive (main-CVM-membership check above still ran)"
  exit 0
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
else
  echo "production runner topology gate passed: $count independent CVM IDs"
fi
